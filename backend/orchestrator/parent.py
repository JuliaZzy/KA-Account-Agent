"""Chat parent run: PLAN, dispatch waves, then ANSWER. Does not execute tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_context, prompt_turns, standing_block, summary_block
from ..repositories.run_repository import latest_checkpoint, note_running_reply
from ..services.run_names import StepStatus, StepType, step_name
from ..text2sql.text2sql import prompt_view
from .gather import checkpoint_state, collect_wave, dispatch_wave, load_state, recovered_plan
from .plan import PLAN_FAILURE_MESSAGE, PLAN_UNUSABLE_NOTE, InvalidPlan, make_plan, parse_tasks, reject_plan
from .prompt import ANSWER


async def execute_parent(db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    """Advance one parent claim. None means parked for a wave (do not complete)."""
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    text = str(payload.get("message") or "")
    state = load_state(latest_checkpoint(db, run["id"]))
    saved = _saved_answer(state)
    if saved is None:
        saved = _answer_from_step(db, run)
    if saved is not None:
        return {"text": saved}

    plan_note: str | None = None
    if state is None:
        recovered = recovered_plan(db, run)
        if recovered is None:
            noted = _unusable_plan(db, run)
            if noted is not None:
                tasks, finished, results, plan_note = [], [], {}, noted
            else:
                draft = await make_plan(
                    llm, message=text,
                    attachments=[name for name in (payload.get("attachments") or []) if isinstance(name, str)],
                    history=list(session.get("history") or []),
                    folded_through=int(session.get("folded_through") or 0),
                    open_po=_open_po(session),
                    db=db, run_id=run["id"],
                    memory=_session_memory(db, session),
                    checkpoint=lambda planned, rejected, note: checkpoint_state(
                        planned,
                        [str(item["id"]) for item in rejected if item.get("id")],
                        {str(item["id"]): item for item in rejected if item.get("id")},
                        extra={"phase": "planned", **({"plan_note": note} if note else {})},
                    ),
                )
                tasks = draft.tasks
                finished = [str(item["id"]) for item in draft.rejected if item.get("id")]
                results = {str(item["id"]): item for item in draft.rejected if item.get("id")}
                plan_note = draft.note
        else:
            tasks, finished, results = recovered
    else:
        tasks = parse_tasks({"tasks": state.get("tasks") or []})
        finished = [str(item) for item in (state.get("finished") or []) if item]
        results = dict(state.get("results") or {})
        if isinstance(state.get("plan_note"), str):
            plan_note = state["plan_note"]
        wave_id = state.get("wave_id")
        session_id = str(session["id"])
        if isinstance(wave_id, str) and wave_id:
            collected = await asyncio.to_thread(collect_wave, db, run, session_id, wave_id)
            for task_id, item in collected.items():
                results[task_id] = item
                if task_id not in finished:
                    finished.append(task_id)

    rejected = reject_plan(tasks, set())
    if rejected is not None:
        _problem, code = rejected
        raise InvalidPlan(PLAN_FAILURE_MESSAGE, code=code)

    if not tasks:
        return {"text": await _answer(db, llm, run, session, text, tasks, finished, results, plan_note)}

    parked = await asyncio.to_thread(
        dispatch_wave, db, run, session, tasks, finished, results, payload,
    )
    if parked is not None:
        return None
    return {"text": await _answer(db, llm, run, session, text, tasks, finished, results, plan_note)}


async def _answer(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any], text: str,
    tasks: list[Any], finished: list[str], results: dict[str, dict[str, Any]],
    plan_note: str | None = None,
) -> str:
    reply = _answer_from_step(db, run)
    if reply is None:
        reply = await compose_answer(
            db, llm, session, text, list(results.values()), run_id=run["id"], plan_note=plan_note,
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


async def compose_answer(
    db: Database, llm: Any, session: dict[str, Any], text: str,
    results: list[dict[str, Any]], *, run_id: str, plan_note: str | None = None,
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
    opening = empty_named_period_line(text, results)
    if plan_note:
        user = f"=== 计划 ===\n{plan_note}\n\n=== 用户 ===\n{text}"
        if materials:
            user += f"\n\n=== 这一轮结果 ===\n{materials}"
    else:
        user = text if not materials else (
            f"=== 用户 ===\n{text}\n\n=== 这一轮结果 ===\n{materials}"
        )
    if opening:
        user = (
            f"=== 开口 ===\n第一句必须逐字是：{opening}\n"
            "不要改写成「月报已生成」或「数据不完整」。报告草稿里其他月份的内容可以接着说。\n\n"
            f"{user}"
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
        reply = ensure_empty_period_opening(
            (await model_calls.qwen_text_async(
                client=llm, system=system, messages=messages,
                on_text=on_text, db=db, run_id=run_id, step_id=step_id, purpose="orchestrator.answer",
            )).strip() or "How can I help?", opening,
        )
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


def _unusable_plan(db: Database, run: dict[str, Any]) -> str | None:
    """A failed PLAN step already recorded. Resume answers instead of planning again."""
    org_id = str(run.get("org_id") or "")
    if not org_id:
        return None
    trace = db.run_trace(org_id, run["id"]) or {}
    for step in trace.get("steps") or []:
        step_input = step.get("input_json") if isinstance(step.get("input_json"), dict) else {}
        if step_input.get("purpose") != "orchestrator.plan" or step.get("status") != StepStatus.FAILED:
            continue
        error = step.get("error_json") if isinstance(step.get("error_json"), dict) else {}
        if error.get("message") == PLAN_UNUSABLE_NOTE or error.get("code") in {
            "invalid_json", "tasks_not_array", "plan_cycle", "plan_truncated",
        }:
            return PLAN_UNUSABLE_NOTE
    return None


def _open_po(session: dict[str, Any]) -> bool:
    stage = str(session.get("stage") or "")
    return stage in ("gathering", "confirm") or bool(session.get("confirm_token"))


def empty_named_period_line(message: str, results: list[dict[str, Any]]) -> str | None:
    """First sentence when the report already knows the named month has no rows."""
    from ..report.charts import period_label
    from ..report.metrics import period_from_message

    period = period_from_message(message)
    empty = False
    for result in results:
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
        if not payload:
            continue
        if payload.get("named_period_empty") is True:
            empty = True
            period = period or payload.get("named_period")
    if not empty or not period:
        return None
    label = period_label(str(period))
    return f"{label}没有数据。" if label else None


def ensure_empty_period_opening(reply: str, line: str | None) -> str:
    """Keep the model's wording, but the named empty month must lead."""
    if not line:
        return reply
    required = line.rstrip("。") + "。"
    text = (reply or "").strip()
    if text.startswith(required.rstrip("。")):
        return text
    first = text.split("。", 1)[0]
    if "没有数据" in first:
        return text
    return required + text


def _materials(results: list[dict[str, Any]]) -> str:
    """Every child result reaches ANSWER, including failed and skipped ones."""
    if not results:
        return ""
    rendered: list[dict[str, Any]] = []
    for result in results:
        item = dict(result)
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        if payload is not None and isinstance(payload.get("rows"), list):
            # Same view as every other prose model; the full rows stay behind `rows_handle`.
            payload = {**payload, **prompt_view(
                payload["rows"], columns=payload.get("columns"),
                row_count=payload.get("row_count"), truncated=bool(payload.get("truncated")),
            )}
            item["payload"] = payload
        if isinstance(item.get("payload"), dict) and "charts" in item["payload"]:
            # The option JSON is for the chat window. The answer only needs to know what was drawn.
            drawn = item["payload"].get("charts") or []
            item["payload"] = {
                **{k: v for k, v in item["payload"].items() if k != "charts"},
                "charts_drawn": [str(c.get("title") or "") for c in drawn if isinstance(c, dict)],
            }
        if item.get("quota_exhausted") or item.get("error") in ("COST_BUDGET_EXCEEDED", "QUOTA_EXCEEDED"):
            item["error"] = "没有生成"
            item.pop("quota_exhausted", None)
        rendered.append(item)
    return json.dumps(rendered, ensure_ascii=False, default=str)
