"""Provisional titles and deferred OPEN refine."""

from __future__ import annotations

from backend.llm import FakeLLM
from backend.service import new_session
from backend.services.chat_run import enqueue_chat_run, work_chat
from backend.services.session_title import (
    mark_provisional,
    provisional_title,
    read_provisional,
    schedule_title_refine,
    title_capacity_busy,
)


def test_provisional_title_prefers_message_then_attachment():
    assert provisional_title("  hello   world  ", []) == "hello world"
    assert provisional_title("", ["agreement.pdf"]) == "agreement.pdf"
    assert provisional_title("", []) == "New chat"


def test_user_edited_title_skips_open_refine(db):
    llm = FakeLLM(json_replies=[{"tags": ["po"], "title": "model title"}], text_reply="ok")
    db.upsert_user("u-a", "dist-1", "Ada")
    session = new_session(db, "ps-1", "u-a")
    run = enqueue_chat_run(db, session, "first line for the list")
    assert db.session(session["id"])["title"] == "first line for the list"
    assert read_provisional(session["id"]) == "first line for the list"
    db.update_session(session["id"], title="I renamed this")
    assert work_chat(db, llm, run["id"]) is True
    assert [row for row in db.find_runs_by_owner(run["id"]) if row["run_type"] == "open"] == []
    assert db.session(session["id"])["title"] == "I renamed this"
    assert read_provisional(session["id"]) is None


def test_title_capacity_busy_uses_running_chat_count(db, monkeypatch):
    monkeypatch.setattr(
        "backend.services.session_title.limits",
        lambda: type("L", (), {"global_running": 10})(),
    )
    monkeypatch.setattr(db, "count_running_chat_parents", lambda: 8)
    assert title_capacity_busy(db) is True
    monkeypatch.setattr(db, "count_running_chat_parents", lambda: 7)
    assert title_capacity_busy(db) is False


def test_schedule_title_is_idempotent_per_session(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    session = new_session(db, "ps-1", "u-a")
    run = enqueue_chat_run(db, session, "hello")
    mark_provisional(session["id"], "hello")
    finished = {
        **db.find_agent_run_by_id(run["id"]),
        "status": "succeeded",
    }
    first = schedule_title_refine(db, finished)
    second = schedule_title_refine(db, finished)
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert len([row for row in db.find_runs_by_owner(run["id"]) if row["run_type"] == "open"]) == 1
