"""Claim one run by id and finish it.

Celery delivers `run_id`. The lease and `run.started` commit before `execute`.
The task returns only after the run is terminal, `waiting_for_user`, or the next
outbox row is scheduled. That return is the ack.

`PermanentRunError` fails the run. Any other exception is retried until the
execution budget, then moved to `dead_letter_jobs`. An infrastructure error
puts the run back without spending that budget.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import OperationalError

from ..db import Database, utcnow
from ..db.database import ChatTurnBusy
from ..repositories.run_repository import (
    RUN_TERMINAL_STATUSES,
    RunStatus,
    cancel_run,
    claim_chat_turn,
    complete_run,
    fail_run,
    find_agent_run_by_id,
    schedule_retry,
)
from ..repositories.run_repository import (
    sweep_expired_leases as _sweep_expired_leases,
)
from .entry_guard import acquire_run, queue_enabled, release_run

LEASE_FOR = timedelta(minutes=5)
RETRY_AFTER = timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)
INFRASTRUCTURE_AFTER = (timedelta(seconds=5),)

log = logging.getLogger(__name__)


class PermanentRunError(Exception):
    """The input or the permission will not succeed on another try."""


class InfrastructureError(Exception):
    """The worker can try this run again. This does not spend a business attempt."""


class LeaseLost(Exception):
    """This worker no longer holds the lease. Leave the run for the sweeper."""


class SessionBusy(Exception):
    """Another run for this chat is still running. Leave this run queued."""


def _finish_claimed(
    db: Database,
    run: dict[str, Any],
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    retry_after: tuple[timedelta, ...],
) -> bool:
    run_id = str(run["id"])
    if _over_duration(run):
        fail_run(db, run_id, {
            "kind": "permanent",
            "code": "COST_BUDGET_EXCEEDED",
            "message": "run duration exceeded",
        })
        return True
    try:
        output = execute(run)
    except (InfrastructureError, OperationalError) as exc:
        schedule_retry(
            db, run_id, {"kind": "infrastructure", "message": str(exc)},
            INFRASTRUCTURE_AFTER, spend_attempt=False,
        )
        return True
    except PermanentRunError as exc:
        return _settle(db, run_id, exc, permanent=True, retry_after=retry_after)
    except LeaseLost:
        return True
    except Exception as exc:
        return _settle(db, run_id, exc, permanent=False, retry_after=retry_after)
    row = find_agent_run_by_id(db, run_id)
    if row is None or row["status"] in RUN_TERMINAL_STATUSES or row["status"] == RunStatus.WAITING_FOR_USER:
        return True
    finished = complete_run(db, run_id, output)
    if finished is None:
        cancel_run(db, run_id)
    return True


def work_once(
    db: Database,
    worker_id: str,
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    retry_after: tuple[timedelta, ...] = RETRY_AFTER,
    run_id: str | None = None,
) -> bool:
    """Run one claimed id. False when no `run_id` was given.

    A terminal run is skipped. The queue switch leaves a queued run where it is.
    The running gates are taken for this claim and given back when the call returns.
    """
    if not run_id:
        return False
    existing = find_agent_run_by_id(db, run_id)
    if existing is None:
        return False
    if existing["status"] in RUN_TERMINAL_STATUSES:
        return True
    if not queue_enabled():
        return False
    org_id = str(existing.get("org_id") or "")
    user_id = str(existing.get("user_id") or "")
    if not acquire_run(org_id, user_id):
        return False
    try:
        try:
            run = claim_chat_turn(db, run_id, worker_id, _lease_deadline())
        except ChatTurnBusy as exc:
            raise SessionBusy(exc.run_id) from exc
        if run is None:
            return find_agent_run_by_id(db, run_id) is not None
        return _finish_claimed(db, run, execute, retry_after)
    finally:
        release_run(org_id, user_id)


def serve_worker(
    db: Database,
    worker_id: str,
    execute: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Unused by compose. Delivery is `execute_run.delay`, which passes `run_id`."""
    while True:
        if not work_once(db, worker_id, execute):
            time.sleep(0.5)


def _settle(
    db: Database,
    run_id: str,
    exc: Exception,
    *,
    permanent: bool,
    retry_after: tuple[timedelta, ...],
) -> bool:
    error = {"kind": "permanent" if permanent else "transient", "message": str(exc)}
    if not run_id:
        return True
    outcome = (
        fail_run(db, run_id, error) if permanent
        else schedule_retry(db, run_id, error, retry_after)
    )
    if outcome == "dead_letter":
        log.error("dead letter run_id=%s error=%s", run_id, error.get("message"))
    return True


def _lease_deadline() -> datetime:
    return utcnow() + LEASE_FOR


def _over_duration(run: dict[str, Any]) -> bool:
    cap = run.get("max_duration_seconds")
    started = run.get("started_at")
    if cap is None or started is None or not isinstance(started, datetime):
        return False
    return utcnow() >= started + timedelta(seconds=int(cap))


def sweep_expired_leases(db: Database, limit: int = 100) -> int:
    """Release runs whose lease has expired. Do not call this from `create_app`."""
    return _sweep_expired_leases(db, limit)
