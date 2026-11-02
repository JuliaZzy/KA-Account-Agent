"""Chat turns enter the queue. The worker claims and runs them.

`enqueue_chat_run` writes the queued row and the outbox, then dispatches.
It does not claim the run. Closing the HTTP stream is not a cancel.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..db import Database
from ..repositories.run_repository import (
    RunStatus,
    create_agent_run,
    find_agent_run_by_id,
)
from .dispatcher import dispatch_pending
from .run_names import RunType, ToolCallStatus
from .run_worker import work_once


def enqueue_chat_run(
    db: Database,
    session: dict[str, Any],
    message: str,
    *,
    images: list[str] | None = None,
    documents: list[tuple[str, bytes]] | None = None,
    model: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Create through the outbox and dispatch. Do not claim the run."""
    user_id = str(session.get("user_id") or "")
    if not user_id:
        raise ValueError("session has no user_id")
    partnership = db.partnership(str(session["partnership_id"]))
    if partnership is None:
        raise ValueError("session has no partnership")
    key = f"chat:{session['id']}:{uuid.uuid4().hex}"
    filed = [
        {"name": name, "data": base64.b64encode(data).decode()}
        for name, data in (documents or [])
        if name and data
    ]
    run, _created = create_agent_run(
        db, partnership.distributor_id, user_id, RunType.CHAT, key, {
            "session_id": session["id"],
            "message": (message or "")[:4000],
            "images": [url for url in (images or []) if isinstance(url, str)],
            "documents": filed,
            "model": (model or "").strip() or None,
            "attachments": [name for name in (attachments or []) if isinstance(name, str)],
        },
    )
    dispatch_pending(db)
    return run


def work_chat(db: Database, llm: Any, run_id: str | None = None) -> bool:
    """Claim one queued chat job and run it. Tests call this; compose uses the Celery task."""
    return work_once(db, "test-worker", execute_chat(db, llm), run_id=run_id)


def client_for_chat(model: str | None = None) -> Any:
    """Campus client for a compose worker. Tests inject FakeLLM instead."""
    from ..config import LLMSettings
    from ..llm.client import SJTUClient
    from ..llm_config import LLM_CONFIGS

    chosen = (model or "").strip() or (LLM_CONFIGS[0].id if LLM_CONFIGS else "deepseek-chat")
    client = SJTUClient(LLMSettings(base_url=None, api_key=""))
    client.model = chosen
    return client


def execute_chat(db: Database, llm: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The callable `work_once` takes for a chat run."""
    from ..orchestrator.speak import save_reply
    from ..turn_lock import hold_session
    from .run_models import execute_for

    def execute(run: dict[str, Any]) -> dict[str, Any]:
        payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
        session_id = str(payload.get("session_id") or "")
        session = db.session(session_id)
        if not session:
            raise ValueError("chat run has no session")
        text = str(payload.get("message") or "")
        images = [url for url in (payload.get("images") or []) if isinstance(url, str)]
        documents: list[tuple[str, bytes]] = []
        for item in payload.get("documents") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            raw = item.get("data")
            if not name or not isinstance(raw, str):
                continue
            documents.append((name, base64.b64decode(raw)))
        with hold_session(session_id):
            session = db.session(session_id) or session
            if not session.get("title"):
                from ..orchestrator.turn import name_new_chat
                name_new_chat(db, llm, session, run)
                session = db.session(session_id) or session
            output = execute_for(
                db, llm, session=session, text=text, images=images, documents=documents,
            )(run)
            row = find_agent_run_by_id(db, run["id"])
            if row is not None and row["status"] == RunStatus.WAITING_FOR_USER:
                return output or {}
            extra, fields, _side = desk_from_tools(db, session, run["id"])
            reply = str((output or {}).get("text") or "").strip() or "How can I help?"
            save_reply(db, session, text, reply, extra, llm=llm, session_fields=fields, run_id=run["id"])
        if output is None:
            return {"text": reply}
        return {**output, "text": reply}

    return execute


def desk_from_tools(
    db: Database, session: dict[str, Any], run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Copy the last tool results onto the session and the desk events."""
    extra: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    side: list[dict[str, Any]] = []
    partnership = db.partnership(str(session["partnership_id"]))
    org_id = partnership.distributor_id if partnership is not None else ""
    trace = (db.run_trace(org_id, run_id) or {}) if org_id else {}
    for call in trace.get("tool_calls") or []:
        if call.get("status") != ToolCallStatus.SUCCEEDED:
            continue
        name = call.get("tool_name")
        payload = call.get("result_json") or {}
        if not isinstance(payload, dict):
            continue
        if name == "po.draft" and "slots" in payload:
            expires = payload.get("confirm_expires_at")
            when = datetime.fromisoformat(expires) if isinstance(expires, str) and expires else None
            fields.update({
                "slots": payload.get("slots") or {},
                "stage": payload.get("stage") or "gathering",
                "specialist": "po",
                "confirm_token": payload.get("confirm_token"),
                "confirm_hash": payload.get("confirm_hash"),
                "confirm_expires_at": when,
            })
            if payload.get("clear_po"):
                fields["po_id"] = None
            extra.update({
                "draft": payload.get("draft"), "order_draft": payload.get("draft"), "order": True,
                "stage": payload.get("stage"), "slots": payload.get("slots"),
                "gaps": payload.get("gaps") or [], "gaps_detail": payload.get("gaps_detail") or [],
                "advisories": payload.get("advisories") or [],
                "confirm_token": payload.get("confirm_token"),
            })
        if name == "po.draft" and payload.get("po_event"):
            extra["po_event"] = payload["po_event"]
        if name == "contract.read":
            side.append({
                "type": "contract", "status": payload.get("status"),
                "contract_id": payload.get("contract_id"), "table": payload.get("table"),
            })
            if isinstance(payload.get("table"), dict):
                extra["contract"] = payload["table"]
            if isinstance(payload.get("pending"), dict):
                extra["contract_pending"] = payload["pending"]
        if name == "report.draft" and payload.get("ok"):
            extra["report_draft"] = True
            extra["report_pending_id"] = payload.get("report_pending_id")
            extra["report_body"] = payload.get("text") or ""
            if isinstance(payload.get("title"), str) and payload["title"].strip():
                extra["report_title"] = payload["title"].strip()
            if payload.get("charts"):
                extra["charts"] = payload["charts"]
            if payload.get("web_notes"):
                extra["web_notes"] = payload["web_notes"]
            for warning in payload.get("warnings") or []:
                side.append({"type": "warning", "message": warning})
        if name == "data.query":
            side.append({
                "type": "data", "ok": bool(payload.get("ok")),
                "row_count": payload.get("row_count") or 0, "log_id": payload.get("log_id"),
            })
            if payload.get("log_id"):
                extra["data_query"] = {
                    "ok": bool(payload.get("ok")), "row_count": payload.get("row_count") or 0,
                    "error": payload.get("error"), "log_id": payload["log_id"],
                }
            if payload.get("handoff"):
                side.append({"type": "warning", "message": payload["handoff"]})
        if name == "brand.search":
            hits = payload.get("hits")
            if isinstance(hits, list) and hits:
                extra["brand_hits"] = [
                    {
                        "n": hit.get("n"), "cid": hit.get("cid"), "doc": hit.get("doc"), "page": hit.get("page"),
                        "file_name": hit.get("file_name"), "source": hit.get("source"),
                    }
                    for hit in hits if isinstance(hit, dict)
                ]
    return extra, fields, side
