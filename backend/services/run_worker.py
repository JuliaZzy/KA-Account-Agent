"""按 id 领一条 Run，跑完再收尾。

Celery 送来 `run_id`。租约和 `run.started` 在 `execute` 之前提交。
Run 进入终态、`waiting_for_user`，或已经排上下一条 outbox 之后，任务才返回。这次返回就是 ack。

`PermanentRunError`、`ModelBudgetExceeded`、`JsonRepairExhausted` 直接判失败并写入
`dead_letter_jobs`。其余异常先按执行次数重试，次数用完再进 `dead_letter_jobs`。
基础设施错误把 Run 放回队列，不扣这次执行次数。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import OperationalError

from ..db import Database, utcnow
from ..db.database import ChatTurnBusy
from ..llm.calls import JsonRepairExhausted, ModelBudgetExceeded
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..repositories.run_repository import (
    RUN_TERMINAL_STATUSES,
    RunStatus,
    cancel_run,
    claim_chat_turn,
    complete_run,
    fail_run,
    find_agent_run_by_id,
    note_running_reply,
    renew_run_lease,
    schedule_retry,
)
from ..repositories.run_repository import (
    sweep_expired_leases as _sweep_expired_leases,
)
from ..repositories.run_repository import (
    sweep_waiting_for_tasks as _sweep_waiting_for_tasks,
)
from .entry_guard import acquire_run, queue_enabled

# How long a silent worker is treated as dead. A live worker renews inside this
# window; the sweeper only reclaims a run that has not been renewed for the whole
# interval. The beat is one fifth of the lease, so a few failed writes still leave
# a deadline in the future. Two missed beats are not a crash.
LEASE_FOR = timedelta(minutes=5)
LEASE_BEAT = LEASE_FOR / 5
RETRY_AFTER = timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)
INFRASTRUCTURE_AFTER = (timedelta(seconds=5),)
PROVIDER_AFTER = (timedelta(seconds=1),)
RETRY_AFTER_CAP = 300.0

log = logging.getLogger(__name__)


class PermanentRunError(Exception):
    """The input or the permission will not succeed on another try."""

    def __init__(self, message: str = "", *, code: str | None = None, scope: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.scope = scope


class InfrastructureError(Exception):
    """The worker can try this run again. This does not spend a business attempt."""


class LeaseLost(Exception):
    """This worker no longer holds the lease. Leave the run for the sweeper."""


class SoftBusy(Exception):
    """Transient congestion. Celery retries; does not spend a business attempt."""

    def __init__(self, reason: str = "soft_busy", *, countdown: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.countdown = countdown if countdown is not None else _soft_busy_countdown()


class SessionBusy(SoftBusy):
    """Another run for this chat is still running. Leave this run queued."""

    def __init__(self, run_id: str | None = None):
        super().__init__("session_busy", countdown=10.0)
        self.run_id = run_id


class GateFull(SoftBusy):
    """Running gate (user/org/global) has no seat. Leave the run queued."""

    def __init__(self) -> None:
        super().__init__("gate_full")


class QueueDisabled(SoftBusy):
    """QUEUE_ENABLED is off. Leave the run queued."""

    def __init__(self) -> None:
        super().__init__("queue_disabled", countdown=30.0)


def _soft_busy_countdown() -> float:
    return random.uniform(5.0, 15.0)


def _maybe_schedule_title(db: Database, run_id: str) -> None:
    row = find_agent_run_by_id(db, run_id)
    if row is None or row["status"] not in RUN_TERMINAL_STATUSES:
        return
    from .session_title import schedule_title_refine

    schedule_title_refine(db, row)


def _finish_claimed(
    db: Database,
    run: dict[str, Any],
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    retry_after: tuple[timedelta, ...],
    run_lease: Any | None = None,
    pg_lease: RunLease | None = None,
) -> bool:
    run_id = str(run["id"])
    worker_id = str(run.get("lease_owner") or "")
    if _over_duration(run):
        fail_run(db, run_id, {
            "kind": "permanent",
            "code": "COST_BUDGET_EXCEEDED",
            "message": "run duration exceeded",
        })
        _maybe_schedule_title(db, run_id)
        return True
    try:
        output = execute(run)
        if inspect.isawaitable(output):
            output = asyncio.run(output)
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
    except ModelCapacityExceeded as exc:
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        _abandon_running_reply(db, run_id)
        schedule_retry(
            db, run_id, {
                "kind": "infrastructure",
                "code": exc.code,
                "brand": exc.brand,
                "model": exc.model,
                "message": str(exc),
            },
            INFRASTRUCTURE_AFTER, spend_attempt=False,
        )
        db.note_capacity_wait(run_id)
        return True
    except ProviderError as exc:
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        if not exc.retryable:
            return _settle(db, run_id, exc, permanent=True, retry_after=retry_after)
        _abandon_running_reply(db, run_id)
        schedule_retry(
            db, run_id, {
                "kind": "infrastructure",
                "code": exc.code,
                "brand": exc.provider,
                "model": exc.model,
                "provider_kind": exc.kind,
                "status_code": exc.status_code,
                "retry_after_seconds": exc.retry_after_seconds,
                "request_id": exc.request_id,
                "message": str(exc),
            },
            PROVIDER_AFTER, spend_attempt=False, delay=_provider_delay(exc),
        )
        return True
    except (InfrastructureError, OperationalError) as exc:
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        schedule_retry(
            db, run_id, {"kind": "infrastructure", "message": str(exc)},
            INFRASTRUCTURE_AFTER, spend_attempt=False,
        )
        return True
    except PermanentRunError as exc:
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        return _settle(db, run_id, exc, permanent=True, retry_after=retry_after)
    except JsonRepairExhausted as exc:
        # JSON 修复次数已经用完。
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        return _settle(
            db, run_id,
            PermanentRunError(str(exc), code=exc.code),
            permanent=True, retry_after=retry_after,
        )
    except ModelBudgetExceeded as exc:
        # The cap is already spent. Another attempt calls the vendor the same way.
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        return _settle(
            db, run_id,
            PermanentRunError(
                "今天的额度已经用完，请明天再试。", code="QUOTA_EXCEEDED", scope=exc.scope,
            ),
            permanent=True, retry_after=retry_after,
        )
    except LeaseLost:
        return True
    except Exception as exc:
        if not _still_held(db, run_id, worker_id, run_lease, pg_lease):
            return True
        return _settle(db, run_id, exc, permanent=False, retry_after=retry_after)
    row = find_agent_run_by_id(db, run_id)
    if row is None or row["status"] in RUN_TERMINAL_STATUSES or row["status"] in (
        RunStatus.WAITING_FOR_USER, RunStatus.WAITING_FOR_TASKS,
    ):
        _maybe_schedule_title(db, run_id)
        return True
    finished = complete_run(db, run_id, output)
    if finished is None:
        cancel_run(db, run_id)
    _maybe_schedule_title(db, run_id)
    return True


def work_once(
    db: Database,
    worker_id: str,
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    retry_after: tuple[timedelta, ...] = RETRY_AFTER,
    run_id: str | None = None,
    *,
    take_run_gate: bool = True,
) -> bool:
    """Run one claimed id. False when no `run_id` was given.

    Anything that is not `queued` is acked and left alone, including a second
    delivery of a run that is already `running`. The claim is the queued-to-running
    write. The running seat is taken after that. A full seat puts the run back in
    Postgres instead of retrying inside the worker. Title OPEN runs pass
    ``take_run_gate=False``.
    """
    if not run_id:
        return False
    existing = find_agent_run_by_id(db, run_id)
    if existing is None:
        return False
    if existing["status"] != RunStatus.QUEUED:
        return True
    if not queue_enabled():
        raise QueueDisabled()
    org_id = str(existing.get("org_id") or "")
    user_id = str(existing.get("user_id") or "")
    lease = None
    try:
        try:
            run = claim_chat_turn(db, run_id, worker_id, _lease_deadline())
        except ChatTurnBusy as exc:
            raise SessionBusy(exc.run_id) from exc
        if run is None:
            return find_agent_run_by_id(db, run_id) is not None
        lease = acquire_run(org_id, user_id) if take_run_gate else None
        if take_run_gate and lease is None:
            db.park_for_capacity(run_id)
            return True
        hold = RunLease(db, str(run["id"]), worker_id)
        try:
            return _finish_claimed(db, run, execute, retry_after, run_lease=lease, pg_lease=hold)
        finally:
            hold.stop()
    finally:
        if lease is not None:
            lease.release()


def serve_worker(
    db: Database,
    worker_id: str,
    execute: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Unused by compose. Delivery is `execute_run.delay`, which passes `run_id`."""
    while True:
        try:
            if not work_once(db, worker_id, execute):
                time.sleep(0.5)
        except SoftBusy as exc:
            time.sleep(exc.countdown)


def _settle(
    db: Database,
    run_id: str,
    exc: Exception,
    *,
    permanent: bool,
    retry_after: tuple[timedelta, ...],
) -> bool:
    error = {"kind": "permanent" if permanent else "transient", "message": str(exc)}
    code = getattr(exc, "code", None)
    if code is not None:
        error["code"] = str(code)
    scope = getattr(exc, "scope", None)
    if scope:
        error["scope"] = str(scope)
    if not run_id:
        return True
    outcome = (
        fail_run(db, run_id, error) if permanent
        else schedule_retry(db, run_id, error, retry_after)
    )
    if outcome == "dead_letter":
        log.error("dead letter run_id=%s error=%s", run_id, error.get("message"))
    # A child the handler did not settle (ProviderError, retries exhausted) still
    # has to reach the owner's ANSWER. Retryable failures stay queued and wait.
    if (permanent and outcome is not None) or outcome == "dead_letter":
        _maybe_schedule_title(db, run_id)
        _notify_owner_of_terminal_child(db, run_id)
        _remember_failed_chat(db, run_id, error)
    return True


def _remember_failed_chat(db: Database, run_id: str, error: dict[str, Any]) -> None:
    """A failed top-level chat still belongs in the session transcript."""
    row = find_agent_run_by_id(db, run_id)
    if row is None:
        return
    from ..orchestrator.speak import remember_failed_turn

    try:
        remember_failed_turn(db, row, error)
    except Exception:
        log.warning("failed to keep chat turn run_id=%s", run_id, exc_info=True)


def _notify_owner_of_terminal_child(db: Database, run_id: str) -> None:
    """Hand a failed child to its owner. `finish_child` keeps the first result."""
    row = find_agent_run_by_id(db, run_id)
    if row is None or not row.get("owner_run_id"):
        return
    from ..orchestrator.gather import child_terminal_result, finish_child

    try:
        finish_child(db, row, child_terminal_result(row))
    except Exception:
        log.warning("failed to hand child result to owner run_id=%s", run_id, exc_info=True)


def _lease_deadline() -> datetime:
    return utcnow() + LEASE_FOR


class RunLease:
    """Keep one worker's Postgres lease alive until `stop`, or until the row is gone.

    The thread renews every `LEASE_BEAT`. A renew that loses the fence sets `lost`,
    and the worker then skips complete / fail / retry. Parking the run
    (`waiting_for_tasks`, `waiting_for_user`, or a terminal status) stops the
    thread without setting `lost`: that worker released the row on purpose.
    """

    def __init__(
        self, db: Database, run_id: str, worker_id: str, *, beat: float | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._worker_id = worker_id
        self._beat = LEASE_BEAT.total_seconds() if beat is None else beat
        self._stopped = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._pump, name=f"run-lease-{run_id[:8]}", daemon=True,
        )
        self._thread.start()

    def _pump(self) -> None:
        last_ok = time.monotonic()
        while not self._stopped.wait(self._beat):
            try:
                state = renew_run_lease(self._db, self._run_id, self._worker_id, _lease_deadline())
            except Exception:
                log.warning("run lease renew failed run_id=%s", self._run_id, exc_info=True)
                if time.monotonic() - last_ok >= LEASE_FOR.total_seconds():
                    self._lost.set()
                    return
                continue
            if state == "lost":
                log.info("run lease lost run_id=%s worker_id=%s", self._run_id, self._worker_id)
                self._lost.set()
                return
            if state == "settled":
                return
            last_ok = time.monotonic()

    @property
    def lost(self) -> bool:
        """Whether this worker no longer holds a live lease."""
        return self._lost.is_set()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


def _still_held(
    db: Database,
    run_id: str,
    worker_id: str,
    run_lease: Any | None,
    pg_lease: RunLease | None,
) -> bool:
    """Renew when we still own the row. False means do not write a new status.

    A Redis gate loss or a failed fence check both stop the worker. The run stays
    where it is so the sweeper, not this worker, decides the next attempt.
    """
    if run_lease is not None and run_lease.lost:
        log.info("running gate lease lost run_id=%s", run_id)
        return False
    if pg_lease is not None and pg_lease.lost:
        return False
    try:
        state = renew_run_lease(db, run_id, worker_id, _lease_deadline())
    except Exception:
        log.warning("run lease renew failed run_id=%s", run_id, exc_info=True)
        return False
    if state == "lost":
        log.info("run lease lost run_id=%s worker_id=%s", run_id, worker_id)
        return False
    return True


def _provider_delay(exc: ProviderError) -> timedelta | None:
    if exc.retry_after_seconds is None:
        return None
    seconds = min(RETRY_AFTER_CAP, max(0.0, float(exc.retry_after_seconds)))
    return timedelta(seconds=seconds * random.uniform(0.9, 1.1))


def _abandon_running_reply(db: Database, run_id: str) -> None:
    """Start a new generation so the desk drops tokens from the failed stream."""
    note_running_reply(db, run_id, "", generation_id=uuid.uuid4().hex)


def _over_duration(run: dict[str, Any]) -> bool:
    cap = run.get("max_duration_seconds")
    started = run.get("started_at")
    if cap is None or started is None or not isinstance(started, datetime):
        return False
    return utcnow() >= started + timedelta(seconds=int(cap))


def sweep_expired_leases(db: Database, limit: int = 100) -> int:
    """Release runs whose lease has expired. Do not call this from `create_app`."""
    return _sweep_expired_leases(db, limit)


# Same unfinished set this many sweeps in a row: stop re-parking and let ANSWER speak.
STALE_WAKE_LIMIT = 3


def sweep_waiting_for_tasks(
    db: Database, limit: int = 100, stale_after: timedelta | None = None,
) -> int:
    """Requeue owners left in `waiting_for_tasks` after every child finished.

    A child that reached a terminal status without `finish_child` is written onto
    the owner checkpoint first. Waking the owner alone makes it dispatch the same
    task again.
    """
    _repair_stale_owners(db, limit, stale_after if stale_after is not None else timedelta(minutes=15))
    return _sweep_waiting_for_tasks(db, limit, stale_after)


def _repair_stale_owners(db: Database, limit: int, stale_after: timedelta) -> None:
    from ..orchestrator.gather import child_terminal_result, finish_child, load_state
    from ..repositories.run_repository import RUN_TERMINAL_STATUSES, latest_checkpoint

    busy = {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_FOR_TASKS}
    owners = db.list_stale_waiting_runs(limit, stale_after)
    for owner in owners:
        children = db.find_runs_by_owner(str(owner["id"]))
        if any(child.get("status") in busy for child in children):
            continue
        before = _result_ids(load_state(latest_checkpoint(db, str(owner["id"]))))
        for child in children:
            if child.get("status") not in RUN_TERMINAL_STATUSES:
                continue
            payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
            task_id = str(payload.get("task_id") or "")
            if not task_id or task_id in before:
                continue
            try:
                finish_child(db, child, child_terminal_result(child))
            except Exception:
                log.warning(
                    "sweeper failed to record child result owner=%s child=%s",
                    owner.get("id"), child.get("id"), exc_info=True,
                )
        state = load_state(latest_checkpoint(db, str(owner["id"]))) or {}
        results = state.get("results") if isinstance(state.get("results"), dict) else {}
        unfinished = _unfinished_task_ids(state, results)
        if not unfinished:
            db.note_stale_wake(str(owner["id"]), progressed=True)
            continue
        wakes = db.note_stale_wake(str(owner["id"]), progressed=set(results) != before)
        if wakes < STALE_WAKE_LIMIT:
            continue
        _force_unfinished(db, owner, state, unfinished, finish_child)


def _result_ids(state: dict[str, Any] | None) -> set[str]:
    raw = (state or {}).get("results") if isinstance(state, dict) else None
    if not isinstance(raw, dict):
        return set()
    return {str(key) for key in raw}


def _unfinished_task_ids(state: dict[str, Any], results: dict[str, Any]) -> list[str]:
    unfinished: list[str] = []
    for task in state.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        if task_id and task_id not in results:
            unfinished.append(task_id)
    return unfinished


def _force_unfinished(
    db: Database,
    owner: dict[str, Any],
    state: dict[str, Any],
    unfinished: list[str],
    finish_child: Any,
) -> None:
    """Give ANSWER a failure for each task that never produced a result."""
    payload = owner.get("input_json") if isinstance(owner.get("input_json"), dict) else {}
    tasks = {
        str(task.get("id")): task
        for task in (state.get("tasks") or [])
        if isinstance(task, dict) and task.get("id")
    }
    for task_id in unfinished:
        task = tasks.get(task_id) or {}
        name = str(task.get("name") or "task")
        synthetic = {
            "id": f"missing:{owner.get('id')}:{task_id}",
            "org_id": owner.get("org_id") or "",
            "owner_run_id": owner.get("id"),
            "input_json": {
                "session_id": payload.get("session_id") or "",
                "wave_id": state.get("wave_id") or "",
                "task_id": task_id,
                "kind": task.get("kind"),
                "name": name,
                "tool_name": name,
            },
        }
        result = {
            "id": task_id,
            "kind": task.get("kind"),
            "name": name,
            "ok": False,
            "error": f"task {task_id} ended without a result",
            "payload": {},
            "status": "failed",
        }
        try:
            finish_child(db, synthetic, result)
        except Exception:
            log.warning(
                "sweeper failed to force a child result owner=%s task=%s",
                owner.get("id"), task_id, exc_info=True,
            )
