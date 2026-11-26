"""Close a run from the service layer.

The status write is one transaction (`finalize_run_row`). Title scheduling,
parent notification, and the failed-chat transcript are side effects after
that commit. Reconciliation calls `notify_parent` alone so it does not
schedule another title or append another transcript row.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import Database
from ..db.outcome import Outcome
from ..repositories.run_repository import RUN_TERMINAL_STATUSES, find_agent_run_by_id

log = logging.getLogger(__name__)


def _maybe_schedule_title(db: Database, run_id: str) -> None:
    row = find_agent_run_by_id(db, run_id)
    if row is None or row["status"] not in RUN_TERMINAL_STATUSES:
        return
    from .session_title import schedule_title_refine

    schedule_title_refine(db, row)


def _remember_failed_chat(db: Database, run_id: str, error: dict[str, Any]) -> None:
    """A failed top-level chat still belongs in the session transcript."""
    row = find_agent_run_by_id(db, run_id)
    if row is None:
        return
    from ..orchestrator.speak import remember_failed_turn

    try:
        remember_failed_turn(db, row, error)
    except Exception:
        log.warning("failed to keep chat turn run_id=%s", run_id, exc_info=True)


def notify_parent(db: Database, run_id: str) -> None:
    """Hand a finished child to its owner. `finish_child` keeps the first result."""
    row = find_agent_run_by_id(db, run_id)
    if row is None or not row.get("owner_run_id"):
        return
    from ..orchestrator.gather import settle_terminal_child

    try:
        settle_terminal_child(db, row)
    except Exception:
        log.warning("failed to hand child result to owner run_id=%s", run_id, exc_info=True)


def after_terminal(db: Database, run_id: str, outcome: Outcome) -> None:
    """Side effects after a terminal write has committed."""
    _maybe_schedule_title(db, run_id)
    notify_parent(db, run_id)
    if outcome.kind == "failed":
        _remember_failed_chat(db, run_id, outcome.error or {})


def after_terminal_row(db: Database, run_id: str) -> None:
    """Run `after_terminal` from the status already stored on the row."""
    row = find_agent_run_by_id(db, run_id)
    if row is None:
        return
    status = str(row.get("status") or "")
    error = row.get("error_json") if isinstance(row.get("error_json"), dict) else None
    output = row.get("output_json") if isinstance(row.get("output_json"), dict) else None
    if status == "failed":
        outcome = Outcome(kind="failed", error=error)
    elif status == "cancelled":
        outcome = Outcome(kind="cancelled")
    elif status == "succeeded":
        outcome = Outcome(kind="succeeded", output=output)
    else:
        return
    after_terminal(db, run_id, outcome)


def finalize_run(
    db: Database,
    run_id: str,
    outcome: Outcome,
    *,
    lease_owner: str | None,
) -> bool:
    """Write the terminal row, then run the side effects.

    False means the row was not written. The caller leaves it for the sweeper
    and does not notify the parent.
    """
    row = db.finalize_run_row(run_id, outcome, lease_owner=lease_owner)
    if row is None:
        return False
    after_terminal(db, run_id, outcome)
    return True
