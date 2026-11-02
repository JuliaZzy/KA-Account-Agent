"""SSE for one run. Reads `run_events` and listens for the commit notification.

The first batch is everything after the caller's cursor. Later batches run when
`NOTIFY` names this run, or when the heartbeat interval elapses. A missed
notification is caught by that interval. The stream does not call a model, a
tool, or change `run.status`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import psycopg

from ..db import Database
from .monitor import note_sse
from .run_names import StepEvent
from .run_service import list_events

# How long to block on LISTEN before checking the table again.
HEARTBEAT_SECONDS = 15.0

_TERMINAL = frozenset({
    StepEvent.RUN_COMPLETED,
    StepEvent.RUN_FAILED,
    StepEvent.RUN_CANCELLED,
})


def _dsn(db: Database) -> str:
    url = db.engine.url.render_as_string(hide_password=False)
    return url.replace("postgresql+psycopg", "postgresql", 1)


def _frame(row: dict[str, Any]) -> bytes:
    data = json.dumps(row.get("payload_json") or {}, ensure_ascii=False, separators=(",", ":"))
    return f"id: {row['sequence']}\nevent: {row['event_type']}\ndata: {data}\n\n".encode()


def _wait(conn: psycopg.Connection, run_id: str, timeout: float) -> bool:
    """True when this run was notified. False when the wait ran out."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        notified = False
        for note in conn.notifies(timeout=remaining, stop_after=1):
            notified = True
            if note.payload == run_id:
                return True
        if not notified:
            return False


def iter_event_stream(db: Database, org_id: str, run_id: str, after: int) -> Iterator[bytes]:
    """Public SSE frames. Closes after a terminal event is written out."""
    note_sse(True)
    try:
        yield from _frames(db, org_id, run_id, after)
    finally:
        note_sse(False)


def _frames(db: Database, org_id: str, run_id: str, after: int) -> Iterator[bytes]:
    cursor = after
    rows = list_events(db, org_id, run_id, cursor)
    for row in rows:
        cursor = int(row["sequence"])
        yield _frame(row)
        if row["event_type"] in _TERMINAL:
            return
    with psycopg.connect(_dsn(db), autocommit=True) as conn:
        conn.execute("LISTEN run_events")
        while True:
            notified = _wait(conn, run_id, HEARTBEAT_SECONDS)
            rows = list_events(db, org_id, run_id, cursor)
            if rows:
                for row in rows:
                    cursor = int(row["sequence"])
                    yield _frame(row)
                    if row["event_type"] in _TERMINAL:
                        return
            elif not notified:
                yield b"\n: heartbeat\n\n"
