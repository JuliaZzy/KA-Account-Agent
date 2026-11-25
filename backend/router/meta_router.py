"""Desk-wide reads: service index, metadata, partnerships."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import service
from ..db import Database
from ..deps import get_db
from ..memory import KIND_LABELS
from ..schemas import PRODUCT_STATUS_VALUES
from ..services.entry_guard import org_budget

router = APIRouter()


@router.get("/")
def index() -> dict[str, str]:
    return {"service": "ka-account-agent", "ui": "http://localhost:5173", "docs": "/docs"}


@router.get("/api/meta")
def meta() -> dict[str, Any]:
    return {
        "product_statuses": list(PRODUCT_STATUS_VALUES),
        "memory_kinds": KIND_LABELS,
        "max_images": service.MAX_IMAGES,
    }


@router.get("/api/quota")
def quota(org_id: str, db: Database = Depends(get_db)) -> dict[str, bool]:
    """Whether this distributor's daily token or cost cap is already spent."""
    return {"exhausted": org_budget(db, org_id, "", warn=False) == "over"}


@router.get("/api/partnerships")
def partnerships(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return [p.model_dump() for p in db.partnerships()]


@router.get("/api/users")
def users(distributor_id: str | None = None, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return [person.model_dump() for person in db.users(distributor_id)]
