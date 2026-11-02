"""Tasks the compose `worker` service runs.

Import `celery` from `backend.celery.worker` and decorate with `@celery.task`.
The worker loads this module from `celery.conf.include` when it starts.
Enqueue from the API with `task.delay(...)` — that call returns before the task runs.
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


@celery.task(bind=True, name="ka_account.execute_run", max_retries=None)
def execute_run(self, run_id: str) -> str:
    """Claim this chat run and execute it. Tests call `work_chat` instead.
    
    A second turn for a chat that is already running stays queued. The retry 
    releases this worker slot until the first turn has finished.
    """
    from ..db import Database
    from ..services.chat_run import client_for_chat, execute_chat
    from ..services.run_worker import SessionBusy, work_once

    db = Database()
    row = db.find_agent_run_by_id(run_id)
    payload = row.get("input_json") if isinstance(row, dict) and isinstance(row.get("input_json"), dict) else {}
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    worker_id = f"celery:{os.getpid()}"
    try:
        work_once(db, worker_id, execute_chat(db, client_for_chat(model)), run_id=run_id)
    except SessionBusy as e:
        raise self.retry(countdown=2) from e
    return run_id
