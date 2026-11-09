"""Chat: open a session, enqueue a turn, confirm an order, a contract, or a report."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import service
from ..db import Database
from ..deps import get_db
from ..schemas import Confirm, ConfirmContract, ConfirmReport, DataFeedback, Inbound, InboundFile, NewSession
from ..services.chat_run import enqueue_chat_run
from ._common import partnership_or_404, user_on_partnership

router = APIRouter()

_MAX_FILE_BYTES = 12 * 1024 * 1024


def _decode_file(f: InboundFile) -> tuple[str, bytes]:
    raw = f.data.split(",", 1)[-1] if f.data.startswith("data:") else f.data
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as err:
        raise HTTPException(400, f"could not read {f.name}") from err
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(400, f"{f.name} is larger than 12 MB")
    return f.name, data


@router.get("/api/sessions")
def list_sessions(
    partnership_id: str, user_id: str | None = None, db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    partnership = partnership_or_404(db, partnership_id)
    user_on_partnership(db, partnership, user_id)
    rows = db.list_sessions(partnership_id, user_id=user_id)
    keep = ("id", "partnership_id", "user_id", "title", "tags", "stage", "updated_at", "created_at")
    return [{k: row.get(k) for k in keep} for row in rows]


@router.post("/api/sessions")
def new_session(body: NewSession, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return service.new_session(db, body.partnership_id, body.user_id)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err
    except ValueError as err:
        raise HTTPException(400, str(err)) from err


@router.post("/api/sessions/{sid}/archive", status_code=204)
def archive_session(sid: str, db: Database = Depends(get_db)) -> Response:
    """Drop the chat. Reports, contracts, and orders it filed stay on their pages."""
    try:
        service.archive_session(db, sid)
    except KeyError as err:
        raise HTTPException(404, "session not found") from err
    return Response(status_code=204)


@router.delete("/api/sessions/{sid}", status_code=204)
def delete_session(sid: str, db: Database = Depends(get_db)) -> Response:
    """Drop the chat and the records it filed. An in-flight reply may still finish and find the row gone."""
    try:
        service.delete_session(db, sid)
    except KeyError as err:
        raise HTTPException(404, "session not found") from err
    return Response(status_code=204)


@router.get("/api/sessions/{sid}")
def get_session(sid: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    s = db.session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    # The heading tree and raw extraction are for confirm, not the desk.
    history = []
    for message in s.get("history") or []:
        if isinstance(message, dict) and "contract_pending" in message:
            message = {key: value for key, value in message.items() if key != "contract_pending"}
        history.append(message)
    s["history"] = history
    return s


@router.post("/api/sessions/{sid}/message", status_code=202)
def message(
    sid: str, body: Inbound, db: Database = Depends(get_db),
) -> dict[str, str]:
    """Create a queued Run; the separate Run SSE endpoint streams its progress."""
    session = db.session(sid)
    if not session:
        raise HTTPException(404, "session not found")
    if len(body.images) > service.MAX_IMAGES:
        raise HTTPException(400, f"at most {service.MAX_IMAGES} images")
    documents = [_decode_file(f) for f in body.files]
    images = [url for url in body.images if isinstance(url, str) and url.startswith("data:image/")]
    if not body.message.strip() and not images and not documents:
        raise HTTPException(400, "message required")
    run = enqueue_chat_run(
        db, session, body.message, images=images, documents=documents,
        attachments=body.attachments, model=body.model,
    )
    return {"run_id": str(run["id"])}


@router.post("/api/sessions/{sid}/confirm")
def confirm(sid: str, body: Confirm, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return service.confirm(db, sid, body.token)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err


@router.post("/api/sessions/{sid}/confirm-contract")
def confirm_contract(sid: str, body: ConfirmContract, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return service.confirm_contract(db, sid, body.pending_id, body.filing)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err
    except ValueError as err:
        raise HTTPException(400, str(err)) from err


@router.post("/api/sessions/{sid}/confirm-report")
def confirm_report(sid: str, body: ConfirmReport, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return service.confirm_report(db, sid, body.pending_id, title=body.title, text=body.text, charts=body.charts)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err
    except ValueError as err:
        raise HTTPException(400, str(err)) from err


@router.post("/api/sessions/{sid}/data-feedback")
def data_feedback(sid: str, body: DataFeedback, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Rate the query behind one assistant turn. The log must belong to this chat."""
    session = db.session(sid)
    if not session:
        raise HTTPException(404, "session not found")
    try:
        db.set_text2sql_feedback(
            body.log_id, body.feedback, reason=body.reason, corrected_sql=body.corrected_sql,
            partnership_id=session["partnership_id"], session_id=sid,
        )
    except KeyError as err:
        raise HTTPException(404, "query log not found") from err
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    db.stamp_text2sql_feedback(sid, body.log_id, body.feedback)
    return {"ok": True, "feedback": body.feedback}
