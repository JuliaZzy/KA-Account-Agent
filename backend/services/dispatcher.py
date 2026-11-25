"""Publish due outbox rows to Celery.

The publish and the `dispatched` mark stay in separate transactions, so a crash
between them leaves the outbox pending and the scan sends the message again.

OPEN (session title) runs go to ``ka_account.title`` so the chat worker never
consumes them. Everything else stays on ``ka_account``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import timedelta

from ..db import Database, utcnow
from ..repositories.run_repository import (
    find_agent_run_by_id,
    find_pending_outbox_jobs,
    mark_outbox_dispatched,
    mark_outbox_retry,
)
from .entry_guard import limits
from .run_names import RunStatus, RunType
from .session_title import DEFAULT_QUEUE, TITLE_QUEUE

log = logging.getLogger(__name__)


def dispatch_pending(db: Database, limit: int = 100) -> int:
    """发布一批待处理 outbox。返回被标记为已派发的行数。

    测试不调用 Redis：直接把行标成已派发，再把 `run_id` 传给 `work_once`。
    """
    delivered = 0
    # A user with no seat must not sit at the head of this scan and hide everyone else.
    for _ in range(5):
        if delivered >= limit:
            break
        moved = False
        for job in find_pending_outbox_jobs(db, limit):
            if delivered >= limit:
                break
            payload = job.get("payload_json") or {}
            if not isinstance(payload, dict):
                payload = {}
            run = _queued_run(db, payload)
            if run is not None and run.get("run_type") != RunType.OPEN and not _inflight_room(db, run):
                db.note_capacity_wait(str(run["id"]))
                db.defer_pending_outbox(job["id"])
                moved = True
                continue
            try:
                _publish(payload)
            except Exception as exc:
                mark_outbox_retry(db, job["id"], str(exc))
                moved = True
                continue
            if mark_outbox_dispatched(db, job["id"]) is not None:
                delivered += 1
                moved = True
        if not moved:
            break
    return delivered


def _queued_run(db: Database, payload: dict) -> dict | None:
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    run = find_agent_run_by_id(db, run_id)
    if run is None or run.get("status") != RunStatus.QUEUED:
        return None
    return run


def _inflight_room(db: Database, run: dict) -> bool:
    """True when global, org, and user still have a running seat for this run."""
    cap = limits()
    org_id = str(run.get("org_id") or "")
    user_id = str(run.get("user_id") or "")
    seats = (
        (cap.global_running, db.count_inflight()),
        (cap.org_running, db.count_inflight(org_id=org_id)),
        (cap.user_running, db.count_inflight(org_id=org_id, user_id=user_id)),
    )
    return all(limit <= 0 or used < limit for limit, used in seats)


def _publish(payload: dict) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("execute_run outbox has no run_id")
    trace_id = payload.get("trace_id")
    queue = TITLE_QUEUE if payload.get("run_type") == RunType.OPEN else DEFAULT_QUEUE
    from ..celery.tasks import execute_run
    execute_run.apply_async(
        args=[run_id, trace_id if isinstance(trace_id, str) else None],
        queue=queue,
    )


def republish_unclaimed(db: Database, older_than_seconds: int | None = None, limit: int = 100) -> int:
    """Republish one run whose own dispatch has gone quiet.

    The decision is this run's `dispatch_at` and heartbeat, not how many messages
    are sitting in Redis. A second copy is safe: claim accepts only `queued`, and
    a run already `running` acks the duplicate. A fresh heartbeat means a worker
    still holds the message, so that run is left alone. Capacity waits fail on
    their own clock; this path does not expire a run.
    """
    grace = redispatch_grace_seconds() if older_than_seconds is None else older_than_seconds
    cutoff = utcnow() - timedelta(seconds=grace)
    sent = 0
    for _queue, open_only in ((DEFAULT_QUEUE, False), (TITLE_QUEUE, True)):
        for _ in range(limit):
            claims = db.claim_unclaimed_dispatches(cutoff, 1, open_only=open_only)
            if not claims:
                break
            payload = claims[0]
            claimed_at = payload.pop("_redispatch_claimed_at")
            try:
                _publish(payload)
            except Exception:
                db.release_unclaimed_dispatch(payload["run_id"], claimed_at)
                log.exception("unclaimed redispatch failed for run_id=%s", payload["run_id"])
                break
            sent += 1
    return sent


def queue_depth(queue: str) -> int | None:
    """Messages waiting in this Celery queue, or None when Redis cannot say.

    Unknown is treated like "busy" by the caller: redispatching blind could duplicate a
    message that is merely waiting, and a real loss is still caught by the age limit.
    """
    from redis import Redis

    from ..config import celery_broker_url

    try:
        redis = Redis.from_url(celery_broker_url(), socket_connect_timeout=1, socket_timeout=1)
        try:
            return int(redis.llen(queue))
        finally:
            redis.close()
    except Exception:
        log.warning("cannot read queue depth for %s; skipping redispatch this round", queue)
        return None


def redispatch_grace_seconds() -> int:
    return _env_int("KA_ACCOUNT_REDISPATCH_AFTER_SECONDS", 60)


def queued_max_age_seconds() -> int:
    return _env_int("KA_ACCOUNT_QUEUED_MAX_AGE_SECONDS", 7200)


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, default)), 0)
    except ValueError:
        return default


def serve_dispatcher(db: Database, limit: int = 100) -> None:
    """Keep scanning. Do not call this from `create_app` or from tests."""
    while True:
        dispatch_pending(db, limit)
        time.sleep(0.5)
