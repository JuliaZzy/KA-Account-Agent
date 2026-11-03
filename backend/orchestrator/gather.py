"""Dispatch one PLAN wave as child runs. No thread pool — workers pick the children up."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..db import Database
from ..redis_store import begin_turn, load_results, store_result
from ..repositories.run_repository import (
    append_checkpoint,
    append_run_event,
    create_agent_run,
    find_agent_run_by_id,
    find_runs_by_owner,
    latest_checkpoint,
    mark_waiting_for_tasks,
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
from .plan import PlanTask, parse_tasks, ready_wave, run_type_for, tool_name_for

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
    """Enqueue the next ready wave and park the owner. None if there is nothing ready."""
    ready = ready_wave(tasks, set(finished))
    if not ready:
        return None
    wave_id = uuid.uuid4().hex
    session_id = str(session["id"])
    begin_turn(session_id, wave_id, [task.id for task in ready])
    prior = [results[dep] for task in ready for dep in task.depends_on if dep in results]
    for task in ready:
        _enqueue_child(db, owner, session, task, payload, wave_id, prior)
        append_run_event(db, owner["org_id"], owner["id"], StepEvent.TOOL_STARTED, {
            "tool_name": tool_name_for(task),
            "task_id": task.id,
            "display": tool_name_for(task),
        })
    saved = append_checkpoint(db, owner["id"], checkpoint_state(
        tasks, finished, results, wave_id=wave_id, wave_ids=[task.id for task in ready], extra=extra,
    ))
    if saved is None:
        return None
    parked = mark_waiting_for_tasks(db, owner["id"], {
        "wave_id": wave_id, "task_ids": [task.id for task in ready],
    })
    dispatch_pending(db)
    return parked


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
    """Write this task into Redis and the owner checkpoint. The last one wakes the owner."""
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    wave_id = str(payload.get("wave_id") or "")
    task_id = str(payload.get("task_id") or "")
    owner_id = str(run.get("owner_run_id") or payload.get("owner_run_id") or "")
    if not session_id or not wave_id or not task_id:
        return
    left = store_result(session_id, wave_id, task_id, result)
    if owner_id:
        _persist_owner_result(db, owner_id, task_id, result)
        append_run_event(db, str(run.get("org_id") or ""), owner_id, (
            StepEvent.TOOL_COMPLETED if result.get("ok") else StepEvent.TOOL_FAILED
        ), {
            "tool_name": result.get("name"),
            "task_id": task_id,
            "ok": bool(result.get("ok")),
        })
    if left == 0 and owner_id:
        woke = requeue_waiting_tasks(db, owner_id, {"wave_id": wave_id})
        if woke is not None:
            dispatch_pending(db)


def _persist_owner_result(db: Database, owner_id: str, task_id: str, result: dict[str, Any]) -> None:
    """Keep the wave result on the owner checkpoint so Redis is not the only copy."""
    owner = find_agent_run_by_id(db, owner_id)
    if owner is None:
        return
    state = load_state(latest_checkpoint(db, owner_id)) or {}
    results = dict(state.get("results") or {})
    results[task_id] = result
    finished = [str(item) for item in (state.get("finished") or []) if item]
    if task_id not in finished:
        finished.append(task_id)
    extra = {
        key: value for key, value in state.items()
        if key not in {"tasks", "finished", "results", "wave_id", "wave_ids"}
    }
    append_checkpoint(db, owner_id, checkpoint_state(
        parse_tasks({"tasks": state.get("tasks") or []}),
        finished, results,
        wave_id=state.get("wave_id") if isinstance(state.get("wave_id"), str) else None,
        wave_ids=[str(item) for item in (state.get("wave_ids") or [])],
        extra=extra,
    ))


def _enqueue_child(
    db: Database,
    owner: dict[str, Any],
    session: dict[str, Any],
    task: PlanTask,
    payload: dict[str, Any],
    wave_id: str,
    prior: list[dict[str, Any]],
) -> dict[str, Any]:
    session_id = str(session["id"])
    key = f"chat:{session_id}:{owner['id']}:{task.id}"
    child_input = {
        "session_id": session_id,
        "owner_run_id": owner["id"],
        "task_id": task.id,
        "kind": task.kind,
        "name": task.name,
        "tool_name": tool_name_for(task),
        "input": task.input,
        "prior": prior,
        "wave_id": wave_id,
        "message": payload.get("message") or "",
        "images": payload.get("images") or [],
        "documents": payload.get("documents") or [],
        "attachments": payload.get("attachments") or [],
        "model": payload.get("model"),
    }
    run_type = run_type_for(task)
    try:
        RunType(run_type)
    except ValueError:
        run_type = RunType.TOOL
    child, _created = create_agent_run(
        db, owner["org_id"], owner["user_id"], run_type, key, child_input,
        owner_run_id=owner["id"],
    )
    return child
