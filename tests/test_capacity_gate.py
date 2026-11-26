"""Admission cap, dispatch hold, and the capacity-wait clock."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from backend.db import utcnow
from backend.services.dispatcher import dispatch_pending
from backend.services.run_service import AppError, create_run
from backend.services.run_worker import work_once


def test_the_twelfth_chat_is_accepted_and_the_next_is_refused(db, monkeypatch):
    monkeypatch.setenv("KA_ACCOUNT_QUEUE_USER", "2")
    create_run(db, "dist-1", "u-a", "chat", "cap-1", {"message": "one"})
    create_run(db, "dist-1", "u-a", "chat", "cap-2", {"message": "two"})
    with pytest.raises(AppError) as caught:
        create_run(db, "dist-1", "u-a", "chat", "cap-3", {"message": "three"})
    assert caught.value.status_code == 429
    assert caught.value.code == "RATE_LIMITED"
    # A report has its own cap, so it is not blocked by the chat backlog.
    report, created = create_run(db, "dist-1", "u-a", "report", "cap-report", {"message": "month"})
    assert created
    assert report["run_type"] == "report"


def test_dispatch_leaves_a_run_in_postgres_when_the_user_seat_is_taken(db, monkeypatch):
    monkeypatch.setenv("KA_ACCOUNT_GATE_USER", "1")
    first, _ = db.create_agent_run("dist-1", "u-a", "chat", "seat-1", {"message": "one"})
    second, _ = db.create_agent_run("dist-1", "u-a", "chat", "seat-2", {"message": "two"})
    assert dispatch_pending(db) == 1
    states = []
    for run in (first, second):
        outbox = db.run_trace("dist-1", run["id"])["outbox"]
        states.append((outbox[0]["status"], db.find_agent_run_by_id(run["id"])["capacity_wait_since"]))
    assert sorted(status for status, _since in states) == ["dispatched", "pending"]
    assert any(since is not None for _status, since in states if _status == "pending")


def test_a_chat_capacity_wait_fails_and_a_shorter_report_wait_does_not(db):
    chat, _ = db.create_agent_run("dist-1", "u-a", "chat", "wait-chat", {"message": "hi"})
    report, _ = db.create_agent_run("dist-1", "u-a", "report", "wait-report", {"message": "month"})
    old = utcnow() - timedelta(minutes=3)
    for run_id in (chat["id"], report["id"]):
        db.note_capacity_wait(run_id)
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE agent_runs SET capacity_wait_since = :stamp WHERE id = :id"),
                {"stamp": old, "id": run_id},
            )
    failed = db.fail_capacity_waits(
        utcnow() - timedelta(seconds=120),
        utcnow() - timedelta(seconds=900),
        ("report", "review", "revise"),
    )
    assert failed == [chat["id"]]
    assert db.find_agent_run_by_id(chat["id"])["error_json"]["code"] == "CAPACITY_TIMEOUT"
    assert db.find_agent_run_by_id(report["id"])["status"] == "queued"


def test_a_second_delivery_does_not_run_a_claimed_run(db):
    run, _ = db.create_agent_run("dist-1", "u-a", "chat", "dup", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], "worker-a", utcnow() + timedelta(minutes=5)) is not None

    def execute(_run):
        raise AssertionError("the running run must not start again")

    assert work_once(db, "worker-b", execute, run_id=run["id"]) is True
    assert db.find_agent_run_by_id(run["id"])["status"] == "running"
    assert db.find_agent_run_by_id(run["id"])["lease_owner"] == "worker-a"
