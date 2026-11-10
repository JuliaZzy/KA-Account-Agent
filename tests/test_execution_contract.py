"""Drain and production delivery share status, checkpoint, and idempotency.

`work_chat` walks parent and child rows. Production publishes each run through
the outbox, and the sweeper requeues a run whose lease expired. Both then call
`work_once`. These tests run one scripted turn through each driver and require
the same ledger.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from backend.db import utcnow
from backend.llm import FakeLLM
from backend.service import handle_message, new_session
from backend.services.chat_run import execute_chat, work_chat
from backend.services.run_worker import work_once
from backend.services.sweeper import sweep

_PLAN = {
    "tasks": [{
        "id": "mail",
        "kind": "tool",
        "name": "send_email",
        "input": {"to": "buyer@example.com", "subject": "Hi", "body": "Hello"},
        "depends_on": [],
    }],
}
_KEEP = {
    "run.created", "run.started", "run.waiting_for_tasks", "run.enqueued",
    "run.requeued_by_sweeper", "run.completed", "run.failed",
    "tool.started", "tool.completed", "tool.failed", "tool.manual_review",
}


def test_drain_and_production_finish_the_same_turn(db, make_db, partnership, settings, monkeypatch):
    sent: list[str] = []

    def send(arguments, **_kwargs):
        sent.append(str(arguments.get("to") or ""))
        return {"ok": True, "data": {"sent": True, "to": arguments.get("to")}, "error": None}

    monkeypatch.setattr("backend.tools.send_email.run", send)
    drain = _prepare(db)
    production = _prepare(_database(make_db, partnership, settings))

    work_chat(drain[0], drain[1], drain[2])
    _drive_production(production[0], production[1], production[2])

    assert _ledger(drain[0], drain[2]) == _ledger(production[0], production[2])
    assert sent == ["buyer@example.com", "buyer@example.com"]
    for llm in (drain[1], production[1]):
        assert sum(1 for call in llm.calls if call["kind"] == "json") == 1


def test_a_lost_worker_resumes_without_sending_twice(db, make_db, partnership, settings, monkeypatch):
    sent: list[str] = []

    def send(arguments, **_kwargs):
        sent.append(str(arguments.get("to") or ""))
        raise SystemExit("worker lost after the side effect was claimed")

    monkeypatch.setattr("backend.tools.send_email.run", send)
    drain = _prepare(db)
    production = _prepare(_database(make_db, partnership, settings))

    with pytest.raises(SystemExit):
        work_chat(drain[0], drain[1], drain[2])
    with pytest.raises(SystemExit):
        _drive_production(production[0], production[1], production[2])
    _expire_running_leases(drain[0])
    _expire_running_leases(production[0])

    assert work_chat(drain[0], drain[1], drain[2]) is True
    _drive_production(production[0], production[1], production[2])

    drain_ledger = _ledger(drain[0], drain[2])
    assert drain_ledger == _ledger(production[0], production[2])
    assert sent == ["buyer@example.com", "buyer@example.com"]
    tool_run = next(row for row in drain_ledger if row["role"] == "tool")
    assert tool_run["tools"] == [{"name": "send_email", "status": "response_lost", "task": "mail"}]
    assert "run.requeued_by_sweeper" in tool_run["events"]
    parent = next(row for row in drain_ledger if row["role"] == "parent")
    assert parent["status"] == "succeeded"
    assert parent["phases"][0] == "planned"
    assert parent["phases"][-1] == "answer_done"
    for llm in (drain[1], production[1]):
        assert sum(1 for call in llm.calls if call["kind"] == "json") == 1


def _prepare(db):
    llm = FakeLLM(json_replies=[_PLAN], text_reply="Mail sent.")
    db.upsert_user("u-a", "dist-1", "Ada")
    session_id = new_session(db, "ps-1", "u-a")["id"]
    db.update_session(session_id, title="Hello", tags=[])
    events = list(handle_message(db, llm, session_id, "email the buyer"))
    return db, llm, events[0]["run_id"]


def _database(make_db, partnership, settings):
    db = make_db("production")
    db.upsert_partnership(partnership)
    db.put_settings("brand-1", settings)
    return db


def _drive_production(db, llm, root_id: str) -> None:
    """Sweeper pass, then one `work_once` per run the outbox would have published.

    Under pytest the dispatcher marks a row dispatched and does not call Celery.
    A queued run with a dispatched outbox is the delivery Celery would have received.
    """
    execute = execute_chat(db, llm)
    for _ in range(32):
        if _settled(db, root_id):
            return
        sweep(db)
        delivered = _due_run_ids(db)
        if not delivered:
            return
        progressed = False
        for run_id in delivered:
            before = db.find_agent_run_by_id(run_id)
            work_once(db, "celery:test", execute, run_id=run_id)
            after = db.find_agent_run_by_id(run_id)
            moved = before is None or after is None or after["status"] != before["status"]
            if after is not None and (moved or after["status"] != "queued"):
                progressed = True
        if not progressed:
            return


def _due_run_ids(db) -> list[str]:
    seen: list[str] = []
    for job in db.find_pending_outbox_jobs():
        payload = job.get("payload_json") or {}
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id not in seen:
            seen.append(run_id)
    horizon = utcnow() + timedelta(seconds=1)
    for payload in db.unclaimed_dispatches(horizon):
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id not in seen:
            seen.append(run_id)
    return seen


def _settled(db, root_id: str) -> bool:
    root = db.find_agent_run_by_id(root_id)
    if root is None or root["status"] not in ("succeeded", "failed", "cancelled"):
        return False
    return all(
        child["status"] in ("succeeded", "failed", "cancelled")
        for child in db.find_runs_by_owner(root_id)
    )


def _expire_running_leases(db) -> None:
    with db.engine.begin() as conn:
        conn.execute(text(
            "UPDATE agent_runs SET lease_expires_at = now() - interval '1 minute' "
            "WHERE status = 'running'",
        ))


def _ledger(db, root_id: str) -> list[dict]:
    parent = db.find_agent_run_by_id(root_id)
    rows = [parent, *db.find_runs_by_owner(root_id)]
    ledger = []
    for row in rows:
        trace = db.run_trace(row["org_id"], row["id"]) or {}
        phases = []
        for checkpoint in trace.get("checkpoints") or []:
            state = (checkpoint.get("state_json") or {}).get("orchestrator") or {}
            phases.append(state.get("phase"))
        tools = []
        for call in trace.get("tool_calls") or []:
            key = str(call.get("idempotency_key") or "")
            tools.append({
                "name": call.get("tool_name"),
                "status": call.get("status"),
                "task": key.split(":")[-1] if key else "",
            })
        events = [
            event["event_type"] for event in (trace.get("events") or [])
            if event["event_type"] in _KEEP
        ]
        output = row.get("output_json") if isinstance(row.get("output_json"), dict) else {}
        role = "parent" if row["id"] == root_id else row["run_type"]
        ledger.append({
            "role": role,
            "status": row["status"],
            "phases": phases,
            "tools": tools,
            "events": events,
            "text": output.get("text"),
            "ok": output.get("ok"),
            "error": output.get("error"),
        })
    return ledger
