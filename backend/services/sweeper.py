"""Periodic sweep. It does not run the loop, call a model, or call a tool.

Compose runs this module as its own service, on the same image as the API.
Each pass releases expired leases, notes rows that have been waiting, and
publishes outbox rows that are still pending.
"""

from __future__ import annotations

import logging
import time

from ..config import env
from ..db import Database
from .dispatcher import dispatch_pending, republish_unclaimed
from .run_worker import sweep_expired_leases, sweep_waiting_for_tasks

log = logging.getLogger(__name__)


def sweep(db: Database) -> None:
    sweep_expired_leases(db)
    sweep_waiting_for_tasks(db)
    db.sweep_attention()
    republish_unclaimed(db)
    dispatch_pending(db)


def main() -> None:
    db = Database()
    pause = _seconds()
    while True:
        try:
            sweep(db)
        except Exception:
            log.exception("sweep failed")
        time.sleep(pause)


def _seconds() -> float:
    raw = env("KA_ACCOUNT_SWEEP_SECONDS", "15")
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 15.0


if __name__ == "__main__":
    main()
