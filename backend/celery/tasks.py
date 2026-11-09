"""Compose 里 `worker` 服务执行的任务。

从 `backend.celery.worker` 引入 `celery`，用 `@celery.task` 装饰。
worker 启动时会通过 `celery.conf.include` 加载本模块。
API 侧用 `task.delay(...)` 入队——该调用在任务实际执行前就会返回。
"""

from __future__ import annotations

import logging
import os

from .worker import celery

log = logging.getLogger(__name__)


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
    """Claim this run and execute it. Returning acks the message.

    Tests call `work_chat` instead. A second turn for a chat that is already
    running stays queued and is tried again in a few seconds, without spending
    a business attempt. `trace_id` is the id written on the run at create time.
    """
    del trace_id
    from ..db import Database
    from ..services.chat_run import client_for_chat, execute_chat
    from ..services.entry_guard import queue_enabled
    from ..services.run_worker import SessionBusy, work_once

    # 若队列未启用，则 30 秒后重试
    if not queue_enabled():
        raise self.retry(countdown=30)
    db = Database()

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

    # worker 唯一标识，抢租约
    worker_id = f"celery:{os.getpid()}"

    # 领租约保证幂等，再跑 execute_chat
    try:
        work_once(db, worker_id, execute_chat(db, client_for_chat()), run_id=run_id)

    # 若会话繁忙，则 2 秒后重试
    except SessionBusy as e:
        raise self.retry(countdown=10) from e
    return run_id
