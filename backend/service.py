"""The agent's operations, independent of transport.

Chat turns are queued Runs; the HTTP layer returns their ids and the Run API
streams durable events separately. Confirmation is a plain call. Nothing in
here knows about requests, responses or widgets.

The legacy direct helper remains available for in-process tests; production
clients consume the durable Run events instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from .contract.confirm import confirm_contract as _confirm_contract
from .db import Database
from .memory import record_history
from .orchestrator import MAX_IMAGES
from .orchestrator.turn import handle_message as run_turn
from .po.turn import confirm_order, load_bundle
from .report.confirm import confirm_report as _confirm_report
from .turn_lock import hold_session

__all__ = [
    "MAX_IMAGES", "archive_session", "confirm", "confirm_contract", "confirm_report",
    "delete_session", "handle_message", "new_session", "orders",
]


def new_session(db: Database, partnership_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    pid = partnership_id or next((p.id for p in db.partnerships()), None)
    partnership = db.partnership(pid) if pid else None
    if not partnership:
        raise KeyError("partnership not found")
    if user_id:
        person = db.user(user_id)
        if person is None or person.distributor_id != partnership.distributor_id:
            raise ValueError("user is not on this distributor")
    return db.create_session(uuid.uuid4().hex, partnership.id, user_id)


def handle_message(
    db: Database, llm: Any, session_id: str, text: str, images: list[str] | None = None,
    attachments: list[str] | None = None, documents: list[tuple[str, bytes]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Enqueue one turn. The worker replies; a second send waits only while this enqueue holds the lock."""
    with hold_session(session_id):
        yield from run_turn(db, llm, session_id, text, images, attachments, documents)


def archive_session(db: Database, session_id: str) -> None:
    """The chat goes. The order, agreement, and write-up it filed stay."""
    db.delete_session(session_id)


def delete_session(db: Database, session_id: str) -> None:
    """The chat and the records it filed go."""
    db.purge_session(session_id)


def confirm(db: Database, session_id: str, token: str) -> dict[str, Any]:
    """The gate. Adds a code-composed `message` for the UI on the two states a person reads."""
    with hold_session(session_id):
        return _confirm(db, session_id, token)


def _confirm(db: Database, session_id: str, token: str) -> dict[str, Any]:
    session = db.session(session_id)
    if not session:
        raise KeyError("session not found")
    bundle = load_bundle(db, session["partnership_id"], session.get("user_id"))
    result = confirm_order(db, bundle, session, token)
    if result["state"] == "submitted":
        msg = f"Done — placed as {result['po_number']} ({result['currency']} {result['total']:.2f}). The brand will review it next."
        if result["advisories"]:
            msg += " It needs their approval because: " + "; ".join(result["advisories"])
        history = [*(session.get("history") or []), {"role": "user", "content": "(confirm)"}, {"role": "assistant", "content": msg}]
        record_history(db, session, history)
        result["message"] = msg
    elif result["state"] == "changed":
        result["message"] = "The order moved since you last saw it (a price or rule changed), so I didn't place it. Here's the current draft — please check it again."
    return result


def confirm_contract(db: Database, session_id: str, pending_id: str, filing: dict[str, Any]) -> dict[str, Any]:
    """File the edited contract draft. Same lock as a chat turn, so two clicks cannot race."""
    with hold_session(session_id):
        return _confirm_contract(db, session_id, pending_id, filing)


def confirm_report(
    db: Database, session_id: str, pending_id: str, *, title: str, text: str, charts: list[Any],
) -> dict[str, Any]:
    """File the edited report draft. A second click updates the same write-up."""
    with hold_session(session_id):
        return _confirm_report(db, session_id, pending_id, title=title, text=text, charts=charts)


def orders(db: Database, partnership_id: str | None = None) -> list[dict[str, Any]]:
    return [{**po.model_dump(), "items": db.po_items(po.id), "events": db.po_events(po.id)} for po in db.list_pos(partnership_id)]
