"""Clock, row snapshots, and the run transition table.

`utcnow` is the one timestamp written into `timestamptz` columns.
`InvalidRunTransition` is raised from `Database._transition` and mapped to HTTP 409.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, inspect, true

from ...config import db_url
from ...services.run_names import RunStatus, RunType, StepEvent
from .. import orm


def utcnow() -> datetime:
    """The one clock the tables are stamped from — always UTC-aware, so what goes into a
    `timestamptz` column carries its offset instead of inheriting the server's."""
    return datetime.now(UTC)


def _queue_last_seen():
    """Latest sign of life for a queued run: queue entry or the last worker retry heartbeat.

    Postgres `GREATEST` skips NULL, so a run that was never heartbeated reads as `queued_at`.
    """
    return func.greatest(orm.AgentRun.queued_at, orm.AgentRun.queue_heartbeat_at)


def _open_scope(open_only: bool | None):
    """Which queue a sweeper pass is looking at: OPEN (title) runs ride their own queue."""
    if open_only is None:
        return true()
    if open_only:
        return orm.AgentRun.run_type == RunType.OPEN
    return orm.AgentRun.run_type != RunType.OPEN


class InvalidRunTransition(Exception):
    """The locked run cannot take this edge. The HTTP layer maps it to 409."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Invalid transition from {from_status} to {to_status}")

class ChatTurnBusy(Exception):
    """This chat already has a running run. The queue row stays queued."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(run_id)

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.QUEUED,
        RunStatus.WAITING_FOR_USER,
        RunStatus.WAITING_FOR_TASKS,
        RunStatus.CANCELLED,
    }),
    RunStatus.WAITING_FOR_USER: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    RunStatus.WAITING_FOR_TASKS: frozenset({
        RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.FAILED,
    }),
}

RUN_TRANSITION_EVENTS: dict[tuple[RunStatus, RunStatus], StepEvent] = {
    (RunStatus.QUEUED, RunStatus.RUNNING): StepEvent.RUN_STARTED,
    (RunStatus.QUEUED, RunStatus.CANCELLED): StepEvent.RUN_CANCELLED,
    (RunStatus.RUNNING, RunStatus.SUCCEEDED): StepEvent.RUN_COMPLETED,
    (RunStatus.RUNNING, RunStatus.FAILED): StepEvent.RUN_FAILED,
    (RunStatus.RUNNING, RunStatus.QUEUED): StepEvent.RUN_RETRY_SCHEDULED,
    (RunStatus.RUNNING, RunStatus.WAITING_FOR_USER): StepEvent.RUN_WAITING_FOR_USER,
    (RunStatus.RUNNING, RunStatus.WAITING_FOR_TASKS): StepEvent.RUN_WAITING_FOR_TASKS,
    (RunStatus.RUNNING, RunStatus.CANCELLED): StepEvent.RUN_CANCELLED,
    (RunStatus.WAITING_FOR_USER, RunStatus.QUEUED): StepEvent.RUN_RESUMED,
    (RunStatus.WAITING_FOR_USER, RunStatus.CANCELLED): StepEvent.RUN_CANCELLED,
    (RunStatus.WAITING_FOR_TASKS, RunStatus.QUEUED): StepEvent.RUN_RESUMED,
    (RunStatus.WAITING_FOR_TASKS, RunStatus.CANCELLED): StepEvent.RUN_CANCELLED,
    (RunStatus.WAITING_FOR_TASKS, RunStatus.FAILED): StepEvent.RUN_FAILED,
}


def _as_url(target: str | None) -> str:
    return db_url() if target is None else str(target)


def _row_dict(row: Any) -> dict[str, Any]:
    """Column values, skipping ones not loaded. A deferred blob (the uploaded file) stays
    out of every list and detail read that did not ask for it."""
    unloaded = inspect(row).unloaded
    return {c.key: getattr(row, c.key) for c in row.__table__.columns if c.key not in unloaded}


def _history_ids(history: Any, key: str) -> list[str]:
    """Distinct string ids stored on transcript messages, in first-seen order."""
    found: list[str] = []
    if not isinstance(history, list):
        return found
    for message in history:
        if not isinstance(message, dict):
            continue
        value = message.get(key)
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return found
