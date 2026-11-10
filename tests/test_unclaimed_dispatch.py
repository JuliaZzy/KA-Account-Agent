"""Regression tests for the queued-run sweeper and outbox retention."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from backend.db import utcnow
from backend.services.dispatcher import dispatch_pending, republish_unclaimed
from backend.services.run_names import RunStatus


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    """Fake Redis depth and publishing: the live compose queues must not leak into a test."""
    from backend.services.session_title import DEFAULT_QUEUE, TITLE_QUEUE

    state = SimpleNamespace(depth={DEFAULT_QUEUE: 0, TITLE_QUEUE: 0}, published=[])
    monkeypatch.setattr("backend.services.dispatcher.queue_depth", lambda queue: state.depth[queue])
    monkeypatch.setattr("backend.services.dispatcher._publish", state.published.append)
    return state


def _create_dispatched_run(db, key: str) -> str:
    run, created = db.create_agent_run(
        "dist-1",
        "u-a",
        "chat",
        key,
        {"session_id": "sweep-regression", "message": key},
    )
    assert created
    assert dispatch_pending(db) == 1
    return str(run["id"])


def _age_run_and_outbox(db, run_id: str, stamp) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_runs SET queued_at = :stamp, updated_at = :stamp "
                "WHERE id = :run_id"
            ),
            {"stamp": stamp, "run_id": run_id},
        )
        conn.execute(
            text(
                "UPDATE outbox_jobs SET dispatch_at = :stamp "
                "WHERE payload_json->>'run_id' = :run_id"
            ),
            {"stamp": stamp, "run_id": run_id},
        )


def test_unclaimed_dispatches_filters_runs_before_limit(db):
    """Old dispatched rows for terminal runs cannot crowd out queued runs."""
    stale = utcnow() - timedelta(minutes=2)
    for index in range(105):
        run_id = _create_dispatched_run(db, f"terminal-{index}")
        db.transition_run("dist-1", run_id, RunStatus.RUNNING)
        assert db.complete_run(run_id, {"ok": True}) is not None
        _age_run_and_outbox(db, run_id, stale)

    queued_ids = []
    for index in range(2):
        run_id = _create_dispatched_run(db, f"queued-{index}")
        _age_run_and_outbox(db, run_id, stale)
        queued_ids.append(run_id)

    cutoff = stale + timedelta(seconds=1)
    assert {payload["run_id"] for payload in db.unclaimed_dispatches(cutoff, limit=100)} == set(queued_ids)
    assert db.find_agent_run_by_id(queued_ids[0])["redispatch_count"] == 0

    payloads = db.claim_unclaimed_dispatches(cutoff, limit=100)

    assert {payload["run_id"] for payload in payloads} == set(queued_ids)
    assert {
        db.find_agent_run_by_id(run_id)["redispatch_count"] for run_id in queued_ids
    } == {1}


def test_republish_unclaimed_is_bounded_and_exhaustion_is_dead_lettered(db):
    run_id = _create_dispatched_run(db, "bounded-redispatch")
    stale = utcnow() - timedelta(minutes=2)
    _age_run_and_outbox(db, run_id, stale)

    for count in range(1, 4):
        assert republish_unclaimed(db, older_than_seconds=0, limit=1) == 1
        row = db.find_agent_run_by_id(run_id)
        assert row["redispatch_count"] == count
        if count < 3:
            _age_run_and_outbox(db, run_id, stale)

    _age_run_and_outbox(db, run_id, stale)
    assert db.fail_unclaimed_exhausted(stale + timedelta(seconds=1), limit=1) == 1
    row = db.find_agent_run_by_id(run_id)
    assert row["status"] == RunStatus.FAILED
    assert row["error_json"]["code"] == "STUCK_REDISPATCH_EXHAUSTED"
    trace = db.run_trace("dist-1", run_id)
    assert trace["dead_letters"]
    assert trace["outbox"][0]["status"] == "failed"


def test_dispatched_outbox_cleanup_keeps_queued_runs(db):
    stale = utcnow() - timedelta(days=8)
    terminal_id = _create_dispatched_run(db, "cleanup-terminal")
    db.transition_run("dist-1", terminal_id, RunStatus.RUNNING)
    db.complete_run(terminal_id, {"ok": True})
    _age_run_and_outbox(db, terminal_id, stale)

    queued_id = _create_dispatched_run(db, "cleanup-queued")
    _age_run_and_outbox(db, queued_id, stale)

    assert db.purge_dispatched_outbox(stale + timedelta(seconds=1), limit=100) == 1
    assert db.run_trace("dist-1", terminal_id)["outbox"] == []
    assert db.run_trace("dist-1", queued_id)["outbox"][0]["status"] == "dispatched"


def test_cleanup_filters_active_runs_before_limit(db):
    stale = utcnow() - timedelta(days=9)
    queued = _create_dispatched_run(db, "cleanup-blocker")
    _age_run_and_outbox(db, queued, stale)
    terminal = _create_dispatched_run(db, "cleanup-after-blocker")
    db.transition_run("dist-1", terminal, RunStatus.RUNNING)
    db.complete_run(terminal, {})
    _age_run_and_outbox(db, terminal, stale + timedelta(seconds=1))
    assert db.purge_dispatched_outbox(utcnow() - timedelta(days=7), limit=1) == 1
    assert db.run_trace("dist-1", terminal)["outbox"] == []


def test_publish_failure_does_not_spend_batch_budget(db, monkeypatch):
    ids = [_create_dispatched_run(db, f"publish-failure-{i}") for i in range(2)]
    for run_id in ids:
        _age_run_and_outbox(db, run_id, utcnow() - timedelta(minutes=5))
    def fail(_payload):
        raise ConnectionError("broker unavailable")
    monkeypatch.setattr("backend.services.dispatcher._publish", fail)
    assert republish_unclaimed(db, limit=100) == 0
    assert all(db.find_agent_run_by_id(run_id)["redispatch_count"] == 0 for run_id in ids)


def test_runs_silent_for_two_hours_are_failed_not_rerun(db, broker):
    """A run orphaned for hours is closed by the normal sweep, with no model call and no manual SQL."""
    old = utcnow() - timedelta(hours=5)
    run_id = _create_dispatched_run(db, "legacy-orphan")
    fresh_id = _create_dispatched_run(db, "fresh-orphan")
    _age_run_and_outbox(db, run_id, old)
    _age_run_and_outbox(db, fresh_id, utcnow() - timedelta(minutes=5))
    broker.published.clear()  # drop the first-time publishes made while creating the runs

    assert republish_unclaimed(db) == 1  # only the 5-minute-old run is worth sending again
    assert [payload["run_id"] for payload in broker.published] == [fresh_id]

    legacy = db.find_agent_run_by_id(run_id)
    assert legacy["status"] == RunStatus.FAILED
    assert legacy["error_json"]["code"] == "STUCK_QUEUE_EXPIRED"
    assert db.run_trace("dist-1", run_id)["dead_letters"]
    assert db.find_agent_run_by_id(fresh_id)["status"] == RunStatus.QUEUED


def test_runs_just_under_the_age_limit_are_still_redispatched(db, broker):
    run_id = _create_dispatched_run(db, "almost-expired")
    _age_run_and_outbox(db, run_id, utcnow() - timedelta(hours=1, minutes=59))
    broker.published.clear()

    assert republish_unclaimed(db) == 1
    assert [payload["run_id"] for payload in broker.published] == [run_id]
    assert db.find_agent_run_by_id(run_id)["status"] == RunStatus.QUEUED


def _set_heartbeat(db, run_id: str, stamp) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_runs SET queue_heartbeat_at = :stamp WHERE id = :id"),
            {"stamp": stamp, "id": run_id},
        )


def test_a_worker_heartbeat_keeps_a_waiting_run_from_redispatch_and_expiry(db, broker):
    """SoftBusy retries sit in worker memory, so Redis reads empty: only the heartbeat says 'alive'."""
    run_id = _create_dispatched_run(db, "soft-busy-waiter")
    _age_run_and_outbox(db, run_id, utcnow() - timedelta(hours=5))
    broker.published.clear()

    db.touch_queue_heartbeat(run_id)  # a worker retried it a moment ago

    assert republish_unclaimed(db) == 0
    assert broker.published == []
    row = db.find_agent_run_by_id(run_id)
    assert row["status"] == RunStatus.QUEUED
    assert row["redispatch_count"] == 0
    # The queue-entry time is untouched, so monitoring still sees how long it has waited.
    assert row["queued_at"] < utcnow() - timedelta(hours=4)


def test_a_stale_heartbeat_does_not_protect_a_run_forever(db, broker):
    run_id = _create_dispatched_run(db, "heartbeat-gone-quiet")
    old = utcnow() - timedelta(hours=5)
    _age_run_and_outbox(db, run_id, old)
    _set_heartbeat(db, run_id, utcnow() - timedelta(minutes=10))  # alive 10 minutes ago
    broker.published.clear()

    assert republish_unclaimed(db) == 1  # past the grace period, so it is sent again
    assert db.find_agent_run_by_id(run_id)["status"] == RunStatus.QUEUED

    _age_run_and_outbox(db, run_id, old)
    _set_heartbeat(db, run_id, utcnow() - timedelta(hours=3))  # silent for 3 hours
    republish_unclaimed(db)
    assert db.find_agent_run_by_id(run_id)["error_json"]["code"] == "STUCK_QUEUE_EXPIRED"


def test_heartbeat_only_touches_queued_runs(db):
    run_id = _create_dispatched_run(db, "heartbeat-running")
    db.transition_run("dist-1", run_id, RunStatus.RUNNING)

    db.touch_queue_heartbeat(run_id)

    with db.engine.connect() as conn:
        stamp = conn.execute(
            text("SELECT queue_heartbeat_at FROM agent_runs WHERE id = :id"), {"id": run_id},
        ).scalar_one()
    assert stamp is None


def test_default_grace_does_not_touch_a_run_that_is_merely_waiting(db, monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr("backend.services.dispatcher._publish", published.append)
    run_id = _create_dispatched_run(db, "short-wait")
    _age_run_and_outbox(db, run_id, utcnow() - timedelta(seconds=45))
    published.clear()

    assert republish_unclaimed(db) == 0
    assert published == []
    assert db.find_agent_run_by_id(run_id)["redispatch_count"] == 0


def _create_open_run(db, key: str) -> str:
    run, created = db.create_agent_run(
        "dist-1", "u-a", "open", key, {"session_id": "sweep-regression", "message": key},
    )
    assert created
    assert dispatch_pending(db) == 1
    return str(run["id"])


def test_runs_behind_a_deep_queue_are_neither_duplicated_nor_failed(db, broker):
    """A3 r2: a 4-minute title backlog made the sweeper resend and then fail healthy runs."""
    from backend.services.session_title import TITLE_QUEUE

    run_id = _create_open_run(db, "deep-title-queue")
    # Already used up every redispatch, and is old enough to be called exhausted.
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE agent_runs SET redispatch_count = 3 WHERE id = :id"), {"id": run_id})
    _age_run_and_outbox(db, run_id, utcnow() - timedelta(minutes=5))
    broker.published.clear()
    broker.depth[TITLE_QUEUE] = 7  # messages are still waiting for a title worker

    assert republish_unclaimed(db) == 0
    assert broker.published == []
    row = db.find_agent_run_by_id(run_id)
    assert row["status"] == RunStatus.QUEUED
    assert row["redispatch_count"] == 3

    broker.depth[TITLE_QUEUE] = 0  # queue drained, run still queued: now it really is lost
    republish_unclaimed(db)
    assert db.find_agent_run_by_id(run_id)["status"] == RunStatus.FAILED


def test_each_queue_is_judged_on_its_own_depth(db, broker):
    from backend.services.session_title import DEFAULT_QUEUE, TITLE_QUEUE

    chat_id = _create_dispatched_run(db, "chat-behind-busy-chat-queue")
    open_id = _create_open_run(db, "open-with-idle-title-queue")
    stale = utcnow() - timedelta(minutes=5)
    _age_run_and_outbox(db, chat_id, stale)
    _age_run_and_outbox(db, open_id, stale)
    broker.published.clear()
    broker.depth[DEFAULT_QUEUE] = 50  # chat queue is busy, title queue is empty

    assert republish_unclaimed(db) == 1
    assert [payload["run_id"] for payload in broker.published] == [open_id]
    assert db.find_agent_run_by_id(chat_id)["redispatch_count"] == 0
    assert db.find_agent_run_by_id(open_id)["redispatch_count"] == 1
    assert TITLE_QUEUE != DEFAULT_QUEUE


def test_unknown_queue_depth_skips_redispatch_but_still_expires_old_runs(db, broker):
    from backend.services.session_title import DEFAULT_QUEUE, TITLE_QUEUE

    recent = _create_dispatched_run(db, "recent-orphan")
    ancient = _create_dispatched_run(db, "ancient-orphan")
    _age_run_and_outbox(db, recent, utcnow() - timedelta(minutes=5))
    _age_run_and_outbox(db, ancient, utcnow() - timedelta(hours=5))
    broker.published.clear()
    broker.depth[DEFAULT_QUEUE] = None  # Redis unreadable
    broker.depth[TITLE_QUEUE] = None

    assert republish_unclaimed(db) == 0
    assert broker.published == []
    assert db.find_agent_run_by_id(recent)["redispatch_count"] == 0
    assert db.find_agent_run_by_id(recent)["status"] == RunStatus.QUEUED
    # Expiry does not need Redis: an unreachable broker cannot keep an orphan alive.
    assert db.find_agent_run_by_id(ancient)["error_json"]["code"] == "STUCK_QUEUE_EXPIRED"
