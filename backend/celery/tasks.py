"""Compose 里 `worker` / `title-worker` 服务执行的任务。

从 `backend.celery.worker` 引入 `celery`，用 `@celery.task` 装饰。
worker 启动时会通过 `celery.conf.include` 加载本模块。
API 侧用 `task.delay(...)` / `apply_async(..., queue=...)` 入队——该调用在任务实际执行前就会返回。
"""

from __future__ import annotations

import logging
import os
import random

from .worker import celery

log = logging.getLogger(__name__)


_db = None
_db_pid: int | None = None


def _database():
    """One `Database` per worker child process, built on first use.

    `Database()` makes an engine (its own connection pool) and runs the Alembic head
    check. Doing that per task left every finished task's pool open until garbage
    collection: an idle worker was holding 27 Postgres connections, the largest share
    of a 100-connection server. The pid guard keeps a forked child from inheriting a
    pool its parent opened.
    """
    global _db, _db_pid
    pid = os.getpid()
    if _db is None or _db_pid != pid:
        from ..db import Database

        _db = Database()
        _db_pid = pid
    return _db


@celery.task(name="ka_account.ping")
def ping() -> str:
    """Broker round-trip. The worker is up when this returns ``pong``."""
    return "pong"


@celery.task(
    bind=True, name="ka_account.execute_run", max_retries=None,
    # 任务跑完再 ack，at-least-once，要求任务本身幂等
    acks_late=True,
    # 若 worker 挂了，则重新入队
    reject_on_worker_lost=True,
)
def execute_run(self, run_id: str, trace_id: str | None = None) -> str:
    """Run `_execute_run`, retrying briefly when Postgres refuses the connection.

    A connection spike ("too many clients") fails the task before the run is claimed.
    Without a retry the message is acked while the run stays `queued`, and only the
    sweeper's slower redispatch can recover it. Retrying here, with jitter so the
    workers that were refused together do not reconnect together, closes the gap
    in seconds. The sweeper stays as the backstop when the retries run out.
    """
    del trace_id
    from psycopg import OperationalError as PsycopgOperationalError
    from sqlalchemy.exc import OperationalError as SqlOperationalError

    try:
        return _execute_run(self, run_id)
    except (SqlOperationalError, PsycopgOperationalError) as exc:
        if self.request.retries >= DB_CONNECT_MAX_RETRIES:
            raise
        log.warning("execute_run %s: database unavailable, retrying: %s", run_id, exc)
        raise self.retry(countdown=db_retry_countdown(self.request.retries), exc=exc) from exc


DB_CONNECT_MAX_RETRIES = 5


def db_retry_countdown(retries: int) -> float:
    """2-6s on the first retry, growing linearly, so refused workers spread out."""
    return (2.0 + random.random() * 4.0) * (retries + 1)


def _execute_run(self, run_id: str) -> str:
    """Claim this run and execute it. Returning acks the message.

    Tests call `work_chat` instead. A second turn for a chat that is already
    running stays queued and is tried again in a few seconds, without spending
    a business attempt. `trace_id` is the id written on the run at create time.

    OPEN runs are published to ``ka_account.title``. When chat capacity is high
    they retry with jitter up to three times, then keep the provisional title.
    """
    from ..repositories.run_repository import find_agent_run_by_id
    from ..services.chat_run import execute_chat
    from ..services.entry_guard import queue_enabled
    from ..services.run_names import RunType
    from ..services.run_worker import SoftBusy, work_once
    from ..services.session_title import (
        TITLE_BUSY_MAX_RETRIES,
        title_capacity_busy,
        title_retry_countdown,
    )

    # 若队列未启用，则 30 秒后重试
    db = _database()
    if not queue_enabled():
        _heartbeat(db, run_id)
        raise self.retry(countdown=30)

    # Celery does not increment max_retries when a worker process is lost.
    # Count redeliveries in Postgres so poison tasks eventually enter the
    # existing dead-letter flow while transient worker loss remains recoverable.
    if (self.request.delivery_info or {}).get("redelivered"):
        from ..db.database.queue import WORKER_LOST_MAX_REDELIVERIES

        outcome = db.record_worker_loss_redelivery(
            run_id, max_attempts=WORKER_LOST_MAX_REDELIVERIES,
        )
        if outcome == "dead_letter":
            return run_id

    row = find_agent_run_by_id(db, run_id)
    is_open = row is not None and row.get("run_type") == RunType.OPEN
    if is_open and title_capacity_busy(db):
        if self.request.retries >= TITLE_BUSY_MAX_RETRIES:
            _skip_open_keep_provisional(db, run_id, worker_id=f"celery:{os.getpid()}")
            return run_id
        _heartbeat(db, run_id)
        raise self.retry(countdown=title_retry_countdown())

    # worker 唯一标识，抢租约
    worker_id = f"celery:{os.getpid()}"

    # SoftBusy (session / gate / queue) retries without ack so the run stays queued.
    try:
        work_once(
            db, worker_id, execute_chat(db), run_id=run_id,
            take_run_gate=not is_open,
        )
    except SoftBusy as e:
        _heartbeat(db, run_id)
        raise self.retry(countdown=e.countdown) from e
    return run_id


def _heartbeat(db, run_id: str) -> None:
    """Tell the sweeper this run is held for a retry, not lost.

    `self.retry` parks the task as an ETA message inside the worker, so the Redis queue
    can read empty for minutes while the run is legitimately waiting; without a stamp the
    sweeper would redispatch it (or, after the age limit, fail it). Best effort: if the
    stamp cannot be written the retry must still be scheduled.
    """
    try:
        db.touch_queue_heartbeat(run_id)
    except Exception:
        log.warning("queue heartbeat failed for run_id=%s", run_id, exc_info=True)


def _skip_open_keep_provisional(db, run_id: str, *, worker_id: str) -> None:
    """Give up refining under load. The sidebar keeps the first-message title."""
    from ..repositories.run_repository import (
        RunStatus,
        claim_chat_turn,
        complete_run,
        find_agent_run_by_id,
    )
    from ..services.run_worker import _lease_deadline

    row = find_agent_run_by_id(db, run_id)
    if row is None or row["status"] != RunStatus.QUEUED:
        return
    claimed = claim_chat_turn(db, run_id, worker_id, _lease_deadline())
    if claimed is None:
        return
    complete_run(db, run_id, {"ok": True, "skipped": "title_busy", "title": None, "tags": []})
