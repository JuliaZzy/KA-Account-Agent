"""Celery process. The compose service `worker` runs this; the API process does not.

Redis database 0 is the app's own keys (session locks, turn results, FX cache).
The broker is database 1 and task results are database 2, both derived from
`KA_ACCOUNT_REDIS_URL`, so there is no second URL to point at localhost.

A task runs here, after the request that enqueued it has finished. Chat turns
are claimed from `job_queue` here. Side effects in a task have to be idempotent
— the worker can retry.

Tasks live in `backend.celery.tasks`. This process imports them once, at start.
The source tree is bind-mounted; `docker compose restart worker` picks up a change.
"""

from __future__ import annotations

from celery import Celery

from ..config import celery_broker_url, celery_result_url

celery = Celery("ka_account", broker=celery_broker_url(), backend=celery_result_url())
celery.conf.update(
    include=["backend.celery.tasks"],
    task_default_queue="ka_account",
    # One reserved task per child. A long model call should not sit on a prefetch.
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=86_400,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    enable_utc=True,
)
