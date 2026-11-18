"""Helpers shared by the desk routers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, UploadFile

from ..db import Database
from ..schemas import Partnership

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def partnership_or_404(db: Database, pid: str) -> Partnership:
    found = db.partnership(pid)
    if not found:
        raise HTTPException(404, "partnership not found")
    return found


def user_on_partnership(db: Database, partnership: Partnership, user_id: str | None) -> None:
    """The caller must name a user of this partnership's distributor."""
    token = (user_id or "").strip()
    if not token:
        raise HTTPException(400, "user_id is required")
    person = db.user(token)
    if person is None or person.distributor_id != partnership.distributor_id:
        raise HTTPException(400, "user is not on this distributor")


def partnership_for_user(db: Database, partnership_id: str, user_id: str | None) -> Partnership:
    """The partnership exists and this user belongs to its distributor."""
    found = partnership_or_404(db, partnership_id)
    user_on_partnership(db, found, user_id)
    return found


def session_for_user(db: Database, session_id: str, user_id: str | None) -> dict[str, Any]:
    """The chat exists, belongs to this user, and that user is on its distributor."""
    session = db.session(session_id)
    if not session:
        raise HTTPException(404, "session not found")
    partnership_for_user(db, session["partnership_id"], user_id)
    if session.get("user_id") != (user_id or "").strip():
        raise HTTPException(404, "session not found")
    return session


async def read_upload(file: UploadFile) -> tuple[str, bytes]:
    name = file.filename or "file"
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"{name} is larger than 12 MB")
    if not data:
        raise HTTPException(400, "empty file")
    return name, data
