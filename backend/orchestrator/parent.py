"""Chat parent run: PLAN, dispatch waves, then ANSWER. Does not execute tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_context, prompt_turns, standing_block, summary_block
from ..repositories.run_repository import latest_checkpoint, note_running_reply
from ..services.run_names import StepStatus, StepType, step_name
from .gather import checkpoint_state, collect_wave, dispatch_wave, load_state, recovered_plan
from .plan import InvalidPlan, graph_error, make_plan, parse_tasks
from .prompt import ANSWER


def execute_parent(db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    """Advance one parent claim. None means parked for a wave (do not complete)."""
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    text = str(payload.get("message") or "")
    state = load_state(latest_checkpoint(db, run["id"]))
    saved = _saved_answer(state)
    if saved is None:
        saved = _answer_from_step(db, run)
    if saved is not None:
        return {"text": saved}

    if state is None:
        recovered = recovered_plan(db, run)
        if recovered is None:
            tasks = make_plan(
                llm, message=text,
                attachments=[name for name in (payload.get("attachments") or []) if isinstance(name, str)],
                history=list(session.get("history") or []),
                folded_through=int(session.get("folded_through") or 0),
                open_po=_open_po(session),
                db=db, run_id=run["id"],
                memory=_session_memory(db, session),
                checkpoint=lambda planned: checkpoint_state(
                    planned, [], {}, extra={"phase": "planned"},
                ),
            )
            finished = []
            results = {}
        else:
            tasks, finished, results = recovered
    else:
        tasks = parse_tasks({"tasks": state.get("tasks") or []})
        finished = [str(item) for item in (state.get("finished") or []) if item]
        results = dict(state.get("results") or {})
        wave_id = state.get("wave_id")
        session_id = str(session["id"])
        if isinstance(wave_id, str) and wave_id:
            for task_id, item in collect_wave(db, run, session_id, wave_id).items():
                results[task_id] = item
                if task_id not in finished:
                    finished.append(task_id)

    problem = graph_error(tasks)
    if problem is not None:
        raise InvalidPlan(problem)

    if not tasks:
        return {"text": _answer(db, llm, run, session, text, tasks, finished, results)}

    parked = dispatch_wave(db, run, session, tasks, finished, results, payload)
    if parked is not None:
        return None
    return {"text": _answer(db, llm, run, session, text, tasks, finished, results)}


def _answer(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any], text: str,
    tasks: list[Any], finished: list[str], results: dict[str, dict[str, Any]],
) -> str:
    reply = _answer_from_step(db, run)
    if reply is None:
        reply = compose_answer(
            db, llm, session, text, list(results.values()), run_id=run["id"],
            checkpoint_for=lambda text: checkpoint_state(
                tasks, finished, results, extra={"phase": "answer_done", "answer": text},
            ),
        )
    return reply


def _saved_answer(state: dict[str, Any] | None) -> str | None:
    if not isinstance(state, dict) or state.get("phase") != "answer_done":
        return None
    reply = state.get("answer")
    if isinstance(reply, str) and reply.strip():
        return reply
    return None


def _answer_from_step(db: Database, run: dict[str, Any]) -> str | None:
    """A finished ANSWER step is the full text. A partial `output_json` is not."""
    org_id = str(run.get("org_id") or "")
    if not org_id:
        return None
    trace = db.run_trace(org_id, run["id"]) or {}
    for step in reversed(trace.get("steps") or []):
        if step.get("status") != StepStatus.SUCCEEDED:
            continue
        step_input = step.get("input_json") if isinstance(step.get("input_json"), dict) else {}
        if step_input.get("purpose") != "orchestrator.answer":
            continue
        output = step.get("output_json") if isinstance(step.get("output_json"), dict) else {}
        reply = output.get("text")
        if isinstance(reply, str) and reply.strip():
            return reply
    return None


def compose_answer(
    db: Database, llm: Any, session: dict[str, Any], text: str,
    results: list[dict[str, Any]], *, run_id: str,
    checkpoint_for: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    memory = _session_memory(db, session)
    system = attach(ANSWER, memory)
    messages = [
        {"role": m.get("role"), "content": str(m.get("content") or "")}
        for m in prompt_turns(list(session.get("history") or []), int(session.get("folded_through") or 0))
        if m.get("role") in ("user", "assistant")
    ]
    materials = _materials(results)
    user = text if not materials else (
        f"=== 用户 ===\n{text}\n\n=== 这一轮结果 ===\n{materials}"
    )
    messages.append({"role": "user", "content": user})
    step = db.insert_step(run_id, StepType.MODEL, step_name(StepType.MODEL, None), {"purpose": "orchestrator.answer"})
    step_id = None if step is None else step["id"]

    def on_text(
        growing: str,
        generation_id: str | None = None,
        reasoning: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"generation_id": generation_id}
        if reasoning:
            kwargs["reasoning"] = reasoning
        note_running_reply(db, run_id, growing, **kwargs)

    caught: Exception | None = None
    reply = ""
    try:
        parts: list[str] = []
        for chunk in model_calls.school_text(
            model_calls.SCHOOL_CHAT, client=llm, system=system, messages=messages,
            on_text=on_text, db=db, run_id=run_id, step_id=step_id, purpose="orchestrator.answer",
        ):
            parts.append(chunk)
        reply = "".join(parts).strip() or "How can I help?"
    except Exception as err:
        caught = err
        raise
    finally:
        if step_id:
            db.finish_step(
                step_id,
                StepStatus.FAILED if caught is not None else StepStatus.SUCCEEDED,
                None if caught is not None else {"text": reply},
                {"message": str(caught)} if caught is not None else None,
                None if caught is not None or checkpoint_for is None else checkpoint_for(reply),
            )
    return reply


def _session_memory(db: Database, session: dict[str, Any]) -> str:
    return prompt_context(
        standing_block(db, session["partnership_id"], session.get("user_id")),
        summary_block(session.get("summary")),
    )


def _open_po(session: dict[str, Any]) -> bool:
    stage = str(session.get("stage") or "")
    return stage in ("gathering", "confirm") or bool(session.get("confirm_token"))


def _materials(results: list[dict[str, Any]]) -> str:
    """Every child result reaches ANSWER, including failed and skipped ones."""
    if not results:
        return ""
    return json.dumps(results, ensure_ascii=False, default=str)
