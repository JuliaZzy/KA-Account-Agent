"""Read and edit the standing notes for a partnership."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..db import Database
from ..deps import get_db
from ..memory import KIND_LABELS, memory_block, remember
from ..schemas import MemoryActive, MemoryIn
from ._common import dump, partnership_for_user

router = APIRouter()


@router.get("/api/partnerships/{pid}/memories")
def memories(pid: str, user_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    partnership_for_user(db, pid, user_id)
    active = db.memories(pid, active_only=True, user_id=user_id)
    inactive = [m for m in db.memories(pid, active_only=False, user_id=user_id) if not m.is_active]
    return {
        "active": [dump(m) for m in active],
        "inactive": [dump(m) for m in inactive],
        "block": memory_block(active),
    }


@router.post("/api/partnerships/{pid}/memories")
def add_memory(pid: str, body: MemoryIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    partnership_for_user(db, pid, body.user_id)
    if body.kind not in KIND_LABELS:
        raise HTTPException(400, "unknown memory kind")
    try:
        saved = remember(db, pid, body.content, kind=body.kind, source="user", user_id=body.user_id)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    return dump(saved)


def _memory_for_user(db: Database, mid: int, user_id: str) -> None:
    row = db.memory(mid)
    if row is None:
        raise HTTPException(404, "memory not found")
    partnership_for_user(db, row.partnership_id, user_id)
    if row.user_id != user_id:
        raise HTTPException(404, "memory not found")


@router.patch("/api/memories/{mid}")
def set_memory(mid: int, body: MemoryActive, user_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    _memory_for_user(db, mid, user_id)
    db.set_memory_active(mid, body.active)
    return {"id": mid, "active": body.active}


@router.delete("/api/memories/{mid}", status_code=204)
def delete_memory(mid: int, user_id: str, db: Database = Depends(get_db)) -> Response:
    _memory_for_user(db, mid, user_id)
    db.delete_memory(mid)
    return Response(status_code=204)
