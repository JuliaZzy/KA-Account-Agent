"""Provisional chat titles and deferred OPEN refine on the title queue.

Enqueue writes a truncated first message (or attachment name) immediately so the
sidebar has text. The OPEN run is created only after the parent chat reaches a
terminal status, then published to ``ka_account.title`` so chat workers never
run it. When global chat load is high, the title worker retries with jitter;
after three busy retries it keeps the provisional title.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from ..db import Database
from ..llm.routing import run_budget_for
from ..redis_store import client
from ..repositories.run_repository import create_agent_run
from .entry_guard import limits
from .run_names import RunType

log = logging.getLogger(__name__)

TITLE_QUEUE = "ka_account.title"
DEFAULT_QUEUE = "ka_account"
PROVISIONAL_KEY = "session:title_provisional:{session_id}"
PROVISIONAL_TTL_SECONDS = 7 * 24 * 3600
TITLE_BUSY_RATIO = 0.8
TITLE_BUSY_MAX_RETRIES = 3
TITLE_RETRY_MIN_SECONDS = 5.0
TITLE_RETRY_MAX_SECONDS = 30.0


def provisional_title(message: str, attachments: list[str] | None = None) -> str:
    """Sidebar title before the model refine. Keep it short and language-agnostic."""
    text = " ".join((message or "").split())
    if text:
        return text[:80]
    for name in attachments or []:
        if isinstance(name, str) and name.strip():
            return name.strip()[:80]
    return "New chat"


def mark_provisional(session_id: str, title: str) -> None:
    try:
        client().set(PROVISIONAL_KEY.format(session_id=session_id), title, ex=PROVISIONAL_TTL_SECONDS)
    except Exception:
        log.exception("failed to mark provisional title session_id=%s", session_id)


def read_provisional(session_id: str) -> str | None:
    try:
        raw = client().get(PROVISIONAL_KEY.format(session_id=session_id))
    except Exception:
        log.exception("failed to read provisional title session_id=%s", session_id)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def clear_provisional(session_id: str) -> None:
    try:
        client().delete(PROVISIONAL_KEY.format(session_id=session_id))
    except Exception:
        log.exception("failed to clear provisional title session_id=%s", session_id)


def apply_provisional_title(
    db: Database,
    session: dict[str, Any],
    message: str,
    attachments: list[str] | None = None,
) -> str | None:
    """Set the session title from the first message when it is still empty."""
    if session.get("title"):
        return None
    title = provisional_title(message, attachments)
    db.update_session(str(session["id"]), title=title)
    mark_provisional(str(session["id"]), title)
    return title


def title_capacity_busy(db: Database) -> bool:
    """True when running chat parents are at or above 80% of the global gate."""
    cap = limits().global_running
    if cap <= 0:
        return False
    return db.count_running_chat_parents() >= cap * TITLE_BUSY_RATIO


def title_retry_countdown() -> float:
    return random.uniform(TITLE_RETRY_MIN_SECONDS, TITLE_RETRY_MAX_SECONDS)


def schedule_title_refine(db: Database, chat_run: dict[str, Any]) -> dict[str, Any] | None:
    """Create the OPEN run after a chat parent reaches a terminal status."""
    if chat_run.get("run_type") != RunType.CHAT or chat_run.get("owner_run_id"):
        return None
    if chat_run.get("status") not in {"succeeded", "failed", "cancelled"}:
        return None
    payload = chat_run.get("input_json") if isinstance(chat_run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return None
    provisional = read_provisional(session_id)
    if not provisional:
        return None
    session = db.session(session_id)
    if session is None:
        return None
    current = str(session.get("title") or "")
    if current and current != provisional:
        # User (or another writer) already replaced the provisional title.
        clear_provisional(session_id)
        return None
    attachments = [name for name in (payload.get("attachments") or []) if isinstance(name, str)]
    message = str(payload.get("message") or "")
    budget = run_budget_for(RunType.OPEN)
    opened, created = create_agent_run(
        db,
        str(chat_run["org_id"]),
        str(chat_run["user_id"]),
        RunType.OPEN,
        f"title:{session_id}",
        {
            "session_id": session_id,
            "message": message[:4000],
            "attachments": attachments,
            "provisional_title": provisional,
            "chat_run_id": chat_run["id"],
        },
        owner_run_id=str(chat_run["id"]),
        max_model_calls=budget.max_model_calls,
        max_tokens=budget.max_tokens,
    )
    if created:
        from .dispatcher import dispatch_pending

        dispatch_pending(db)
    return opened
