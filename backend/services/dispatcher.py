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
from ..repositories.run_repository import find_pending_outbox_jobs, mark_outbox_dispatched, mark_outbox_retry
from .run_names import RunType
from .session_title import DEFAULT_QUEUE, TITLE_QUEUE

log = logging.getLogger(__name__)


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
    queue = TITLE_QUEUE if payload.get("run_type") == RunType.OPEN else DEFAULT_QUEUE
    from ..celery.tasks import execute_run
    execute_run.apply_async(
        args=[run_id, trace_id if isinstance(trace_id, str) else None],
        queue=queue,
    )


def republish_unclaimed(db: Database, older_than_seconds: int | None = None, limit: int = 100) -> int:
    """Republish queued runs whose message is gone; fail the ones redispatch cannot help.

    A run that is still `queued` after the grace period has one of two causes: its
    message is lost, or the message is waiting in a deep queue. Only the first needs a
    redispatch, and sending a second copy for the second only deepens the backlog (A3 r2:
    the title queue ran 4 minutes deep, the sweeper added ~1.5 duplicate messages per
    run, and 24 healthy runs were failed as "exhausted"). Each queue is checked first:
    if Redis still holds messages for it, its runs are left alone.

    A run with no sign of life for `queued_max_age_seconds` (default 2h) is failed instead
    of re-executed: whoever sent that message stopped waiting long ago, and the model call
    would be paid for a reply nobody reads. Sign of life includes the worker heartbeat on
    SoftBusy retries, so a run that is only waiting for capacity does not expire. Expiry
    is checked first and does not need Redis, so an unreachable broker cannot keep
    orphaned runs alive.
    """
    grace = redispatch_grace_seconds() if older_than_seconds is None else older_than_seconds
    cutoff = utcnow() - timedelta(seconds=grace)
    max_age = timedelta(seconds=queued_max_age_seconds())
    not_before = utcnow() - max_age
    failed = db.fail_unclaimed_exhausted(cutoff, limit, max_redispatches=None, max_age=max_age)
    sent = 0
    for queue, open_only in ((DEFAULT_QUEUE, False), (TITLE_QUEUE, True)):
        depth = queue_depth(queue)
        if depth is None or depth > 0:
            continue
        failed += db.fail_unclaimed_exhausted(cutoff, limit, open_only=open_only, not_before=not_before)
        for _ in range(limit):
            claims = db.claim_unclaimed_dispatches(cutoff, 1, open_only=open_only, not_before=not_before)
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
    if failed:
        # Keep this visible in the sweeper log; the run and dead-letter rows carry the detail.
        log.warning("failed %s queued runs: redispatch exhausted or queue age exceeded", failed)
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
    return _env_int("KA_ACCOUNT_REDISPATCH_AFTER_SECONDS", 120)


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
