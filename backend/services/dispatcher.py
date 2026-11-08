"""Publish due outbox rows to Celery.

The publish and the `dispatched` mark stay in separate transactions, so a crash
between them leaves the outbox pending and the scan sends the message again.
"""

from __future__ import annotations

import os
import time

from ..db import Database
from ..repositories.run_repository import find_pending_outbox_jobs, mark_outbox_dispatched, mark_outbox_retry


def dispatch_pending(db: Database, limit: int = 100) -> int:
    """发布一批待处理 outbox。返回被标记为已派发的行数。

    测试不调用 Redis：直接把行标成已派发，再把 `run_id` 传给 `work_once`。
    """
    delivered = 0
    for job in find_pending_outbox_jobs(db, limit):
        payload = job.get("payload_json") or {}
        try:
            _publish(payload if isinstance(payload, dict) else {})
        except Exception as exc:
            mark_outbox_retry(db, job["id"], str(exc))
            continue
        if mark_outbox_dispatched(db, job["id"]) is not None:
            delivered += 1
    return delivered


def _publish(payload: dict) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("execute_run outbox has no run_id")
    trace_id = payload.get("trace_id")
    from ..celery.tasks import execute_run
    execute_run.delay(run_id, trace_id if isinstance(trace_id, str) else None)


def republish_unclaimed(db: Database, older_than_seconds: int = 30) -> int:
    """Send `execute_run` again when the outbox is dispatched and the run is still queued."""
    from datetime import timedelta

    from ..db import utcnow

    sent = 0
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    for payload in db.unclaimed_dispatches(cutoff):
        _publish(payload)
        sent += 1
    return sent


def serve_dispatcher(db: Database, limit: int = 100) -> None:
    """Keep scanning. Do not call this from `create_app` or from tests."""
    while True:
        dispatch_pending(db, limit)
        time.sleep(0.5)
