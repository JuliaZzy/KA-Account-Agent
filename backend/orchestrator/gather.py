"""Dispatch one PLAN wave as child runs. No thread pool — workers pick the children up."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..db import Database
from ..redis_store import begin_turn, drop_turn, load_results, store_result
from ..repositories.run_repository import (
    commit_task_wave,
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
from .plan import PlanTask, ready_wave, run_type_for, tool_name_for

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

    # 计算就绪波
    ready = ready_wave(tasks, set(finished))   # 依赖全部 finished，但自己还没跑
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
    """Redis is the wave mailbox. Child `output_json` and the owner checkpoint are the record."""
    boxed: dict[str, dict[str, Any]] = {}
    owner_id = owner if isinstance(owner, str) else (owner or {}).get("id")
    if db is not None and owner_id:
        state = load_state(latest_checkpoint(db, str(owner_id))) or {}
        saved = state.get("results") or {}
        if isinstance(saved, dict):
            boxed.update({str(key): value for key, value in saved.items() if isinstance(value, dict)})
        for child in find_runs_by_owner(db, str(owner_id)):
            payload = child.get("input_json") if isinstance(child.get("input_json"), dict) else {}
            if str(payload.get("wave_id") or "") != wave_id:
                continue
            task_id = str(payload.get("task_id") or "")
            output = child.get("output_json")
            if task_id and isinstance(output, dict) and output:
                boxed[task_id] = output
    if session_id and wave_id:
        boxed.update(load_results(session_id, wave_id))
    return boxed


def finish_child(
    db: Database,
    run: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Persist the result, then use Redis only to coordinate the fast wake-up path."""
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    wave_id = str(payload.get("wave_id") or "")
    task_id = str(payload.get("task_id") or "")
    owner_id = str(run.get("owner_run_id") or payload.get("owner_run_id") or "")
    if not session_id or not wave_id or not task_id:
        return
    if owner_id:
        event_type = StepEvent.TOOL_COMPLETED if result.get("ok") else StepEvent.TOOL_FAILED
        db.record_child_wave_result(
            str(run.get("org_id") or ""),
            owner_id,
            task_id,
            result,
            event_type=event_type,
            event_payload={
                "tool_name": result.get("name"),
                "task_id": task_id,
                "ok": bool(result.get("ok")),
            },
        )
    try:
        left = store_result(session_id, wave_id, task_id, result)
    except Exception:
        log.exception("failed to write wave result to Redis")
        left = None
    if left == 0 and owner_id:
        woke = requeue_waiting_tasks(db, owner_id, {"wave_id": wave_id})
        if woke is not None:
            drop_turn(session_id, wave_id)
            dispatch_pending(db)


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
            "model": payload.get("model"),
        },
        "started": {"tool_name": tool_name, "task_id": task.id, "display": tool_name},
    }
