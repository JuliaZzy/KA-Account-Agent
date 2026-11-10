"""`execute_run` must retry, not silently ack, when Postgres refuses a connection."""

from __future__ import annotations

import pytest
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy.exc import OperationalError as SqlOperationalError

from backend.celery import tasks


class _Retried(Exception):
    def __init__(self, countdown: float) -> None:
        super().__init__("retry")
        self.countdown = countdown


def _fail_with(exc: Exception):
    def _boom(*_args, **_kwargs):
        raise exc

    return _boom


@pytest.mark.parametrize(
    "error",
    [
        SqlOperationalError("connect", {}, PsycopgOperationalError("too many clients already")),
        PsycopgOperationalError("FATAL: sorry, too many clients already"),
    ],
)
def test_connection_refusal_is_retried_with_jitter(monkeypatch, error):
    monkeypatch.setattr(tasks, "_execute_run", _fail_with(error))

    def fake_retry(*, countdown, exc):
        raise _Retried(countdown)

    monkeypatch.setattr(tasks.execute_run, "retry", fake_retry)

    with pytest.raises(_Retried) as caught:
        tasks.execute_run.run("run_x")

    assert 2.0 <= caught.value.countdown <= 6.0


def test_retries_stop_and_the_error_surfaces(monkeypatch):
    error = PsycopgOperationalError("still down")
    monkeypatch.setattr(tasks, "_execute_run", _fail_with(error))
    monkeypatch.setattr(tasks, "DB_CONNECT_MAX_RETRIES", 0)

    with pytest.raises(PsycopgOperationalError):
        tasks.execute_run.run("run_x")


def test_retry_countdown_grows_but_stays_spread():
    first = {tasks.db_retry_countdown(0) for _ in range(20)}
    assert len(first) > 1
    assert all(2.0 <= value <= 6.0 for value in first)
    assert min(tasks.db_retry_countdown(3) for _ in range(20)) >= 8.0


def test_database_is_built_once_per_process(monkeypatch):
    """A fresh `Database()` per task leaks one connection pool per task."""
    import backend.db as db_module

    built: list[object] = []

    def fake_database():
        built.append(object())
        return built[-1]

    monkeypatch.setattr(db_module, "Database", fake_database)
    monkeypatch.setattr(tasks, "_db", None)
    monkeypatch.setattr(tasks, "_db_pid", None)

    first = tasks._database()
    assert tasks._database() is first
    assert len(built) == 1

    # A forked child must not reuse the pool its parent opened.
    monkeypatch.setattr(tasks, "_db_pid", -1)
    assert tasks._database() is not first
    assert len(built) == 2


def test_soft_busy_retry_stamps_a_queue_heartbeat(db, monkeypatch):
    """A parked retry leaves Redis empty; the heartbeat is what tells the sweeper the run is alive."""
    from backend.services import run_worker
    from backend.services.dispatcher import dispatch_pending

    run, _ = db.create_agent_run("dist-1", "u-a", "chat", "soft-busy-key", {"session_id": "s", "message": "hi"})
    assert dispatch_pending(db) == 1
    run_id = str(run["id"])

    def busy(*_args, **_kwargs):
        raise run_worker.SessionBusy("other-run")

    def fake_retry(*, countdown, exc=None):
        raise _Retried(countdown)

    monkeypatch.setattr(tasks, "_database", lambda: db)
    monkeypatch.setattr(run_worker, "work_once", busy)
    monkeypatch.setattr(tasks.execute_run, "retry", fake_retry)

    with pytest.raises(_Retried):
        tasks.execute_run.run(run_id)

    from sqlalchemy import text

    with db.engine.connect() as conn:
        stamp = conn.execute(
            text("SELECT queue_heartbeat_at FROM agent_runs WHERE id = :id"), {"id": run_id},
        ).scalar_one()
    assert stamp is not None


def test_a_failed_heartbeat_does_not_block_the_retry(monkeypatch):
    class Broken:
        def touch_queue_heartbeat(self, _run_id):
            raise RuntimeError("db gone")

    tasks._heartbeat(Broken(), "run_x")  # must not raise
