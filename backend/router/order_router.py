"""Submitted orders for one partnership, or every partnership."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import service
from ..db import Database
from ..deps import get_db
from ._common import partnership_for_user

router = APIRouter()


@router.get("/api/orders")
def orders(partnership_id: str, user_id: str, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    partnership_for_user(db, partnership_id, user_id)
    return service.orders(db, partnership_id)
