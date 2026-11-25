"""Drive one purchase-order draft the way the worker does: prepare, then write the session."""

from __future__ import annotations

import asyncio
from typing import Any

from backend.po.turn import prepare_order


def draft_order(db: Any, llm: Any, bundle: Any, session: dict[str, Any], message: str, images: list[str] | None = None) -> list[dict[str, Any]]:
    prepared = asyncio.run(prepare_order(db, llm, bundle, session, message, images or []))
    db.update_session(
        session["id"],
        slots=prepared.slots.model_dump(),
        stage=prepared.stage,
        specialist="po",
        confirm_token=prepared.token,
        confirm_hash=prepared.draft_hash,
        confirm_expires_at=prepared.expires,
    )
    return [
        *prepared.events,
        {
            "type": "done",
            "stage": prepared.stage,
            "slots": prepared.slots.model_dump(),
            "draft": prepared.draft,
            "gaps": prepared.check.gap_messages,
            "advisories": [rule.message for rule in prepared.check.advisory],
            "confirm_token": prepared.token,
        },
    ]
