"""把一条聊天回复写入会话，并产出工作台要读的事件。

每个代理做完自己的事之后调用这里。编排器用它处理工具，以及计划里没有点名任何人时的直接回答。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_context, prompt_turns, record_history, standing_block, summary_block
from ..services.run_names import StepStatus, StepType, step_name
from .prompt import ANSWER


def one_shot(
    db: Database, session: dict[str, Any], text: str, extra: dict[str, Any] | None = None,
    llm: Any | None = None,
) -> Iterator[dict[str, Any]]:
    assistant: dict[str, Any] = {"role": "assistant", "content": text}
    history = [*(session.get("history") or []), {"role": "user", "content": "(message)"}, assistant]
    record_history(db, session, history)
    yield {"type": "token", "text": text}
    yield {
        "type": "done", "stage": session.get("stage"), "slots": session.get("slots"), "draft": None,
        "gaps": [], "gaps_detail": [], "advisories": [], "confirm_token": None, "submitted": None,
        **(extra or {}),
    }


def _chat_messages(session: dict[str, Any], prompt: str) -> list[dict[str, Any]]:
    history = list(session.get("history") or [])
    messages = [
        {"role": m.get("role"), "content": str(m.get("content") or "")}
        for m in prompt_turns(history, int(session.get("folded_through") or 0))
        if m.get("role") in ("user", "assistant")
    ]
    messages.append({"role": "user", "content": prompt})
    return messages


def save_reply(
    db: Database, session: dict[str, Any], text: str, reply: str, extra: dict[str, Any] | None,
    llm: Any | None = None, session_fields: dict[str, Any] | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    history = list(session.get("history") or [])
    # The raw extraction and heading tree stay on the message for confirm. The done
    # event the desk reads does not need them.
    extra = dict(extra or {})
    if run_id and any(
        isinstance(item, dict) and item.get("role") == "assistant" and item.get("run_id") == run_id
        for item in history
    ):
        return {
            "type": "done", "stage": session.get("stage"), "slots": session.get("slots"), "draft": None,
            "gaps": [], "gaps_detail": [], "advisories": [], "confirm_token": None, "submitted": None,
            **extra,
        }
    pending = extra.pop("contract_pending", None)
    assistant: dict[str, Any] = {"role": "assistant", "content": reply}
    if run_id:
        assistant["run_id"] = run_id
    sources = extra.get("brand_hits")
    if sources:
        assistant["sources"] = sources
    mark = extra.get("data_query")
    if isinstance(mark, dict) and mark.get("log_id"):
        assistant["data_query"] = {
            "log_id": mark["log_id"], "ok": bool(mark.get("ok")),
            "columns": mark.get("columns") or [],
            "missing_columns": mark.get("missing_columns") or [],
            "truncated": bool(mark.get("truncated")),
            "rows_handle": mark.get("rows_handle"),
        }
    charts = extra.get("charts")
    if isinstance(charts, list) and charts:
        assistant["charts"] = charts
    notes = extra.get("web_notes")
    if isinstance(notes, list) and notes:
        assistant["web_notes"] = notes
    if extra.get("report_draft"):
        assistant["report_draft"] = True
    report_pending = extra.get("report_pending_id")
    if isinstance(report_pending, str) and report_pending:
        assistant["report_pending_id"] = report_pending
    filing = extra.get("contract")
    if isinstance(filing, dict) and filing:
        assistant["contract"] = filing
    if extra.get("contract_id"):
        assistant["contract_id"] = extra.get("contract_id")
    if isinstance(pending, dict) and pending.get("id"):
        assistant["contract_pending"] = pending
    if extra.get("order"):
        assistant["order"] = True
    order_draft = extra.get("order_draft")
    if isinstance(order_draft, dict):
        assistant["order_draft"] = order_draft
    report_body = extra.get("report_body")
    if isinstance(report_body, str) and report_body.strip():
        assistant["report_body"] = report_body
    report_title = extra.get("report_title")
    if isinstance(report_title, str) and report_title.strip():
        assistant["report_title"] = " ".join(report_title.split())[:80]
    if extra.get("combined"):
        assistant["combined"] = True
    series = extra.get("report_series")
    if isinstance(series, list) and series:
        assistant["report_series"] = series
    step = None
    if run_id:
        step = db.insert_step(
            run_id, StepType.MODEL, step_name(StepType.MODEL, None),
            {"purpose": "orchestrator.history"},
        )
    try:
        record_history(
            db, session, [*history, {"role": "user", "content": text}, assistant], llm=llm,
            fold=True, run_id=run_id,
            **(session_fields or {}),
        )
    except Exception as exc:
        if step is not None:
            db.finish_step(step["id"], StepStatus.FAILED, None, {"message": str(exc)})
        raise
    if step is not None:
        db.finish_step(step["id"], StepStatus.SUCCEEDED, {"saved": True})
    return {
        "type": "done", "stage": session.get("stage"), "slots": session.get("slots"), "draft": None,
        "gaps": [], "gaps_detail": [], "advisories": [], "confirm_token": None, "submitted": None,
        **extra,
    }


def answer_directly(
    db: Database, llm: Any, session: dict[str, Any], text: str, *,
    prompt_text: str | None = None, system: str | None = None, extra: dict[str, Any] | None = None,
    done_step: tuple[str, str, str] | None = None, session_fields: dict[str, Any] | None = None,
    purpose: str = "orchestrator.answer",
) -> Iterator[dict[str, Any]]:
    from .status import status

    parts: list[str] = []
    system = attach(system or ANSWER, prompt_context(
        standing_block(db, session["partnership_id"], session.get("user_id")), summary_block(session.get("summary")),
    ))
    for chunk in model_calls.school_text(
        model_calls.SCHOOL_CHAT, client=llm, system=system,
        messages=_chat_messages(session, prompt_text or text),
        purpose=purpose,
    ):
        parts.append(chunk)
        yield {"type": "token", "text": chunk}
    reply = "".join(parts).strip() or "How can I help?"
    # Close the step before the reply is saved, so the trace shows it finished.
    if done_step is not None:
        step_id, title, detail = done_step
        yield status(step_id, title, detail, state="done")
    return save_reply(db, session, text, reply, extra, llm=llm, session_fields=session_fields)
