"""把一条聊天回复写入会话，并产出工作台要读的事件。"""

from __future__ import annotations

from typing import Any

from ..db import Database
from ..memory import record_history
from ..services.run_names import StepStatus, StepType, step_name


def failed_turn_text(error: dict[str, Any] | None) -> str:
    """What the thread keeps when a chat run ends failed. The desk reads this back."""
    body = error if isinstance(error, dict) else {}
    if body.get("code") == "QUOTA_EXCEEDED":
        return "今天的额度已经用完，请明天再试。"
    message = " ".join(str(body.get("message") or "").split())
    if not message or "Traceback" in message:
        return "这次没有完成。"
    if message.startswith("这次没有完成"):
        return message[:500]
    return f"这次没有完成。{message}"[:500]


def remember_failed_turn(db: Database, run: dict[str, Any], error: dict[str, Any] | None) -> None:
    """Keep a failed chat turn in the session. A reload otherwise shows only the greeting.

    The live page holds the question in memory until `save_reply`. A run that fails
    before that, or whose tab is already closed, never reached `save_reply`. The
    same `run_id` written twice does not add a second copy.
    """
    if run.get("owner_run_id") or run.get("run_type") != "chat":
        return
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    session = db.session(session_id)
    if not session:
        return
    run_id = str(run.get("id") or "")
    history = list(session.get("history") or [])
    if run_id and any(
        isinstance(item, dict) and item.get("role") == "assistant" and item.get("run_id") == run_id
        for item in history
    ):
        return
    text = " ".join(str(payload.get("message") or "").split())
    assistant: dict[str, Any] = {"role": "assistant", "content": failed_turn_text(error)}
    if run_id:
        assistant["run_id"] = run_id
    assistant["failed"] = True
    added: list[dict[str, Any]] = []
    if text:
        added.append({"role": "user", "content": text})
    added.append(assistant)
    fields: dict[str, Any] = {}
    if text and not str(session.get("title") or "").strip():
        fields["title"] = text[:80]
    record_history(db, session, [*history, *added], **fields)


async def save_reply(
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
            fold=False, run_id=run_id,
            **(session_fields or {}),
        )
        from ..memory import FOLD_TOKENS, _prefix_tokens, fold_session_async

        saved = db.session(session["id"]) or session
        folded = int(saved.get("folded_through") or 0)
        if _prefix_tokens(list(saved.get("history") or []), folded) >= FOLD_TOKENS:
            await fold_session_async(db, llm, str(session["id"]), run_id)
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
