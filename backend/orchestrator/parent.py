"""Chat parent run: PLAN, dispatch waves, then ANSWER. Does not execute tools."""

from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_context, prompt_turns, standing_block, summary_block
from ..repositories.run_repository import append_checkpoint, latest_checkpoint, note_running_reply
from ..services.run_names import StepStatus, StepType, step_name
from .gather import checkpoint_state, collect_wave, dispatch_wave, load_state
from .plan import make_plan, parse_tasks
from .prompt import ANSWER


def execute_parent(db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    """Advance one parent claim. None means parked for a wave (do not complete)."""
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    text = str(payload.get("message") or "")

    # 检查 checkpoint
    state = load_state(latest_checkpoint(db, run["id"]))

    # 没有 checkpoint：创建计划
    if state is None:
        tasks = make_plan(
            llm, message=text,
            attachments=[name for name in (payload.get("attachments") or []) if isinstance(name, str)],
            history=list(session.get("history") or []),
            folded_through=int(session.get("folded_through") or 0),
            open_po=_open_po(session),
            db=db, run_id=run["id"],
            memory=_session_memory(db, session),
        )
        if not tasks:
            reply = compose_answer(db, llm, session, text, [], run_id=run["id"])
            return {"text": reply}

        # 拆出任务，派发第一波子任务
        parked = dispatch_wave(db, run, session, tasks, [], {}, payload)
        return None if parked is not None else {"text": compose_answer(db, llm, session, text, [], run_id=run["id"])}

    # 有 checkpoint
    tasks = parse_tasks({"tasks": state.get("tasks") or []})                       # 全量任务清单
    finished = [str(item) for item in (state.get("finished") or []) if item]       # 已完成任务清单
    results = dict(state.get("results") or {})                           # 任务结果字典
    wave_id = state.get("wave_id")
    session_id = str(session["id"])
    if isinstance(wave_id, str) and wave_id:
        for task_id, item in collect_wave(db, run, session_id, wave_id).items():
            results[task_id] = item
            if task_id not in finished:
                finished.append(task_id)

    # 还有任务要跑
    parked = dispatch_wave(db, run, session, tasks, finished, results, payload)
    if parked is not None:
        return None

    # 先更新 checkpoint，再 reply
    append_checkpoint(db, run["id"], checkpoint_state(tasks, finished, results))
    reply = compose_answer(db, llm, session, text, list(results.values()), run_id=run["id"])
    return {"text": reply}


def compose_answer(
    db: Database, llm: Any, session: dict[str, Any], text: str,
    results: list[dict[str, Any]], *, run_id: str,
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

    def on_text(growing: str, generation_id: str | None = None) -> None:
        note_running_reply(db, run_id, growing, generation_id=generation_id)

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
    if not results:
        return ""
    return json.dumps(results, ensure_ascii=False, default=str)
