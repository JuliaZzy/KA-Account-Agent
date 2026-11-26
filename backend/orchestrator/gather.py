"""Dispatch one PLAN wave as child runs. Workers claim the children.

Redis, the wave transaction, and the outbox scan are synchronous. An async caller
runs each of `dispatch_wave`, `collect_wave`, and `finish_child` once via
`asyncio.to_thread`, so a lock wait does not pin the event loop. The Session
stays on that worker thread. The children themselves are still separate Celery tasks.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from ..db import Database
from ..redis_store import begin_turn, drop_turn, load_results, store_result
from ..repositories.run_repository import (
    RUN_TERMINAL_STATUSES,
    commit_task_wave,
    find_run_by_idempotency_key,
    find_runs_by_owner,
    latest_checkpoint,
    requeue_waiting_tasks,
)
from ..services.dispatcher import dispatch_pending
from ..services.run_names import (
    COMPLETED_TOOL_CALLS_KEY,
    CONTEXT_KEY,
    LAST_INPUT,
    LAST_KEY,
    RunType,
    StepEvent,
)
from .plan import RESULT_STATUSES, PlanTask, parse_tasks, ready_wave, run_type_for, tool_name_for

log = logging.getLogger(__name__)

STATE_KEY = "orchestrator"


def checkpoint_state(
    tasks: list[PlanTask],
    finished: list[str],
    results: dict[str, dict[str, Any]],
    wave_id: str | None = None,
    wave_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "tasks": [task.as_dict() if isinstance(task, PlanTask) else task for task in tasks],
        "finished": list(finished),
        "results": results,
        "wave_id": wave_id,
        "wave_ids": list(wave_ids or []),
    }
    if extra:
        blob.update(extra)
    return {
        CONTEXT_KEY: [],
        COMPLETED_TOOL_CALLS_KEY: [],
        LAST_KEY: LAST_INPUT,
        STATE_KEY: blob,
    }


def load_state(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(checkpoint, dict):
        return None
    blob = checkpoint.get("state_json") if "state_json" in checkpoint else checkpoint
    if not isinstance(blob, dict):
        return None
    state = blob.get(STATE_KEY)
    return state if isinstance(state, dict) else None


def dispatch_wave(
    db: Database,
    owner: dict[str, Any],
    session: dict[str, Any],
    tasks: list[PlanTask],
    finished: list[str],
    results: dict[str, dict[str, Any]],
    payload: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Enqueue the next ready wave and park the owner. None if there is nothing ready.

    The child runs, `tool.started` events, checkpoint, and `waiting_for_tasks`
    commit together. Redis is armed first so a fast child cannot store a result
    into a counter that does not exist yet. A commit that does not park drops
    that counter. Workers are published only after the commit.
    """
    started = time.perf_counter()
    ready_count = 0
    parked: dict[str, Any] | None = None
    try:
        # 计算就绪波
        ready = ready_wave(tasks, set(finished))   # 依赖全部 finished，但自己还没跑
        ready = _drop_terminal_children(db, owner, session, ready, finished, results)
        ready_count = len(ready)
        if not ready:
            return None

        wave_id = uuid.uuid4().hex
        session_id = str(session["id"])
        task_ids = [task.id for task in ready]
        begin_turn(session_id, wave_id, task_ids)

        try:
            prior = _collect_prior(ready, results)
            children = [
                _child_spec(owner, session, task, payload, wave_id, prior) for task in ready
            ]
            parked = commit_task_wave(db, owner["id"], children, checkpoint_state(
                tasks, finished, results, wave_id=wave_id, wave_ids=task_ids, extra=extra,
            ), {"wave_id": wave_id, "task_ids": task_ids})
        except Exception:
            drop_turn(session_id, wave_id)
            raise
        if parked is None:
            drop_turn(session_id, wave_id)
            return None

        # 出队，触发下一个 ready 波
        dispatch_pending(db)
        return parked
    finally:
        log.info(
            "dispatch_wave %.1fms owner=%s ready=%s parked=%s",
            (time.perf_counter() - started) * 1000,
            owner.get("id"), ready_count, parked is not None,
        )


def _collect_prior(
    ready: list[PlanTask], results: dict[str, Any],
) -> list[dict[str, Any]]:
    """Collect dependency results and reject an incomplete or malformed envelope."""
    prior: list[dict[str, Any]] = []
    for task in ready:
        for dependency_id in task.depends_on:
            if dependency_id not in results:
                raise ValueError(
                    f"missing result for dependency {dependency_id!r} of task {task.id!r}"
                )
            result = results[dependency_id]
            if not isinstance(result, dict):
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: expected object"
                )
            required = ("id", "kind", "name", "ok", "error", "payload")
            if any(key not in result for key in required):
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: "
                    f"missing one of {required!r}"
                )
            if result["id"] != dependency_id:
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: id mismatch"
                )
            if not isinstance(result["kind"], str) or not result["kind"]:
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: "
                    "kind must be a string"
                )
            if not isinstance(result["name"], str) or not result["name"]:
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: "
                    "name must be a string"
                )
            if not isinstance(result["ok"], bool):
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: ok must be boolean"
                )
            if result["error"] is not None and not isinstance(result["error"], str):
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: "
                    "error must be string or null"
                )
            status = result.get("status")
            if status is not None and status not in RESULT_STATUSES:
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: "
                    f"status must be one of {sorted(RESULT_STATUSES)!r}"
                )
            if not isinstance(result["payload"], dict):
                raise ValueError(
                    f"invalid result for dependency {dependency_id!r} of task {task.id!r}: "
                    "payload must be object"
                )
            prior.append(result)
    return prior


def collect_wave(
    db: Database | None, owner: dict[str, Any] | str | None,
    session_id: str, wave_id: str,
) -> dict[str, dict[str, Any]]:
    """Redis is the wave mailbox. A result already on the owner checkpoint stays first.

    Child `output_json` and Redis fill ids the checkpoint does not have yet.
    A later Redis write does not replace the checkpoint copy.
    """
    started = time.perf_counter()
    boxed: dict[str, dict[str, Any]] = {}
    saved: dict[str, dict[str, Any]] = {}
    owner_id = owner if isinstance(owner, str) else (owner or {}).get("id")
    try:
        return _collect_wave(db, owner_id, session_id, wave_id, boxed, saved)
    finally:
        log.info(
            "collect_wave %.1fms owner=%s wave=%s results=%s",
            (time.perf_counter() - started) * 1000,
            owner_id, wave_id, len(boxed),
        )


def _collect_wave(
    db: Database | None, owner_id: Any, session_id: str, wave_id: str,
    boxed: dict[str, dict[str, Any]], saved: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if db is not None and owner_id:
        state = load_state(latest_checkpoint(db, str(owner_id))) or {}
        raw = state.get("results") or {}
        if isinstance(raw, dict):
            saved = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
        for child in find_runs_by_owner(db, str(owner_id)):
            payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
            if str(payload.get("wave_id") or "") != wave_id:
                continue
            task_id = str(payload.get("task_id") or "")
            output = child.get("output_json")
            if task_id and isinstance(output, dict) and output and task_id not in saved:
                boxed[task_id] = output
    if session_id and wave_id:
        for task_id, item in load_results(session_id, wave_id).items():
            if task_id not in saved and isinstance(item, dict):
                boxed[task_id] = item
    boxed.update(saved)
    return boxed


def recovered_plan(
    db: Database, run: dict[str, Any],
) -> tuple[list[PlanTask], list[str], dict[str, dict[str, Any]]] | None:
    """Rebuild a plan when the checkpoint row is gone but the PLAN step or children remain.

    None means there is nothing to resume, so the caller may PLAN again.
    Tasks come from the succeeded PLAN step. Child `output_json` fills results.
    """
    tasks = _tasks_from_plan_step(db, run)
    children = [
        child for child in find_runs_by_owner(db, str(run.get("id") or ""))
        if _task_id(child)
    ]
    if tasks is None and not children:
        return None
    if tasks is None:
        tasks = []
        for child in children:
            payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
            raw_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
            tasks.append(PlanTask(
                id=_task_id(child),
                kind=str(payload.get("kind") or "tool"),
                name=str(payload.get("name") or payload.get("tool_name") or ""),
                input=dict(raw_input),
            ))
    finished: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    for item in _rejected_from_plan_step(db, run):
        task_id = str(item.get("id") or "")
        if task_id:
            results[task_id] = item
            finished.append(task_id)
    for child in children:
        payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
        task_id = _task_id(child)
        output = child.get("output_json")
        if task_id and isinstance(output, dict) and output:
            results[task_id] = output
            if task_id not in finished:
                finished.append(task_id)
    return tasks, finished, results


def _rejected_from_plan_step(db: Database, run: dict[str, Any]) -> list[dict[str, Any]]:
    org_id = str(run.get("org_id") or "")
    if not org_id:
        return []
    trace = db.run_trace(org_id, str(run.get("id") or "")) or {}
    for step in trace.get("steps") or []:
        step_input = step.get("input_json") if isinstance(step.get("input_json"), dict) else {}
        if step_input.get("purpose") != "orchestrator.plan" or step.get("status") != "succeeded":
            continue
        output = step.get("output_json") if isinstance(step.get("output_json"), dict) else {}
        rejected = output.get("rejected")
        if isinstance(rejected, list):
            return [item for item in rejected if isinstance(item, dict)]
    return []


def _task_id(child: dict[str, Any]) -> str:
    payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
    return str(payload.get("task_id") or "")


def _tasks_from_plan_step(db: Database, run: dict[str, Any]) -> list[PlanTask] | None:
    org_id = str(run.get("org_id") or "")
    if not org_id:
        return None
    trace = db.run_trace(org_id, str(run.get("id") or "")) or {}
    for step in trace.get("steps") or []:
        step_input = step.get("input_json") if isinstance(step.get("input_json"), dict) else {}
        if step_input.get("purpose") != "orchestrator.plan" or step.get("status") != "succeeded":
            continue
        output = step.get("output_json") if isinstance(step.get("output_json"), dict) else {}
        return parse_tasks({"tasks": output.get("tasks") or []})
    return None


def finish_child(
    db: Database,
    run: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Persist the result, then use Redis only to coordinate the fast wake-up path."""
    started = time.perf_counter()
    wrote = False
    try:
        wrote = _finish_child(db, run, result)
    finally:
        log.info(
            "finish_child %.1fms run=%s wrote=%s",
            (time.perf_counter() - started) * 1000,
            run.get("id"), wrote,
        )


def settle_terminal_child(db: Database, child: dict[str, Any]) -> dict[str, Any]:
    """Write one finished child's envelope onto its owner. The first result wins."""
    result = child_terminal_result(child)
    finish_child(db, child, result)
    return result


def child_terminal_result(child: dict[str, Any]) -> dict[str, Any]:
    """Envelope for a child the worker already finished without `finish_child`.

    A handler result already stored on `output_json` is kept. A bare failure
    (`error_json` only) becomes `{ok: false}` so ANSWER still receives it.
    """
    payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
    output = child.get("output_json")
    if isinstance(output, dict) and output and (
        "ok" in output or "error" in output or "status" in output
    ):
        result = dict(output)
        result.setdefault("id", payload.get("task_id"))
        result.setdefault("kind", payload.get("kind"))
        result.setdefault("name", payload.get("tool_name") or payload.get("name"))
        if result.get("status") not in RESULT_STATUSES:
            result["status"] = "succeeded" if result.get("ok") is True else "failed"
        return result
    error = child.get("error_json") if isinstance(child.get("error_json"), dict) else {}
    code = error.get("code")
    message = str(error.get("message") or f"child {child.get('id')} ended as {child.get('status')}")
    if code:
        message = f"{code}: {message}"
    return {
        "id": payload.get("task_id"),
        "kind": payload.get("kind"),
        "name": payload.get("tool_name") or payload.get("name"),
        "ok": False,
        "error": message,
        "payload": {},
        "status": "failed",
    }


def _finish_child(db: Database, run: dict[str, Any], result: dict[str, Any]) -> bool:
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    wave_id = str(payload.get("wave_id") or "")
    task_id = str(payload.get("task_id") or "")
    owner_id = str(run.get("owner_run_id") or payload.get("owner_run_id") or "")
    # The checkpoint is the record. Redis only wakes the owner early; a missing
    # wave id must not drop the result on the floor.
    if not owner_id or not task_id:
        return False
    if result.get("status") not in RESULT_STATUSES:
        result["status"] = "succeeded" if result.get("ok") is True else "failed"
    event_type = StepEvent.TOOL_COMPLETED if result.get("ok") else StepEvent.TOOL_FAILED
    event_payload = {
        "tool_name": result.get("name"),
        "task_id": task_id,
        "ok": bool(result.get("ok")),
    }
    if result.get("ok") is not True and result.get("error"):
        event_payload["error"] = str(result["error"])
        event_payload["display"] = str(result["error"])
    wrote = db.record_child_wave_result(
        str(run.get("org_id") or ""),
        owner_id,
        task_id,
        result,
        event_type=event_type,
        event_payload=event_payload,
    )
    left = None
    if session_id and wave_id:
        try:
            left = store_result(session_id, wave_id, task_id, result)
        except Exception:
            log.exception("failed to write wave result to Redis")
    if left == 0:
        woke = requeue_waiting_tasks(db, owner_id, {"wave_id": wave_id})
        if woke is not None:
            drop_turn(session_id, wave_id)
            dispatch_pending(db)
    return wrote is True


def _drop_terminal_children(
    db: Database,
    owner: dict[str, Any],
    session: dict[str, Any],
    ready: list[PlanTask],
    finished: list[str],
    results: dict[str, dict[str, Any]],
) -> list[PlanTask]:
    """A reused child that already ended is a result, not another wait.

    The idempotency key would otherwise park the owner on a run that will never
    call `finish_child` again.
    """
    org_id = str(owner.get("org_id") or "")
    owner_id = str(owner.get("id") or "")
    session_id = str(session.get("id") or "")
    if not org_id or not owner_id:
        return ready
    live: list[PlanTask] = []
    for task in ready:
        key = f"chat:{session_id}:{owner_id}:{task.id}"
        existing = find_run_by_idempotency_key(db, org_id, key)
        if existing is None or existing.get("status") not in RUN_TERMINAL_STATUSES:
            live.append(task)
            continue
        result = settle_terminal_child(db, existing)
        results[task.id] = result
        if task.id not in finished:
            finished.append(task.id)
    return live


def _child_spec(
    owner: dict[str, Any],
    session: dict[str, Any],
    task: PlanTask,
    payload: dict[str, Any],
    wave_id: str,
    prior: list[dict[str, Any]],
) -> dict[str, Any]:
    session_id = str(session["id"])
    tool_name = tool_name_for(task)
    run_type = run_type_for(task)
    try:
        RunType(run_type)
    except ValueError:
        run_type = RunType.TOOL
    return {
        "run_type": run_type,
        "idempotency_key": f"chat:{session_id}:{owner['id']}:{task.id}",
        "input": {
            "session_id": session_id,
            "owner_run_id": owner["id"],
            "task_id": task.id,
            "kind": task.kind,
            "name": task.name,
            "tool_name": tool_name,
            "input": task.input,
            "prior": prior,
            "wave_id": wave_id,
            "message": payload.get("message") or "",
            "images": payload.get("images") or [],
            "documents": payload.get("documents") or [],
            "attachments": payload.get("attachments") or [],
        },
        "started": {"tool_name": tool_name, "task_id": task.id, "display": tool_name},
    }
