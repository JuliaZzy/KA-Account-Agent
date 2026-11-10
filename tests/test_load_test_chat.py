"""Regression tests for load-generator outcomes and bounded run follow-up."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from scripts import load_test_chat


def test_timeout_preserves_run_id_and_server_status(monkeypatch):
    async def no_reply(*_args):
        return False

    monkeypatch.setattr(load_test_chat, "_wait_for_reply", no_reply)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-1"})
        assert request.headers["X-User-Id"] == "user-1"
        return httpx.Response(200, json={"status": "running"})

    async def run_one():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await load_test_chat._one(
                client, "http://test", "session-1", "hello", 0,
                partnership_id="partner-1", user_id="user-1",
            )

    result = asyncio.run(run_one())
    assert result["run_id"] == "run-1"
    assert result["outcome"] == "client_timeout"
    assert result["server_status_at_timeout"] == "running"


def test_enqueue_errors_are_not_counted_as_client_timeouts():
    async def run_one():
        transport = httpx.MockTransport(lambda _request: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            return await load_test_chat._one(
                client, "http://test", "session-1", "hello", 1,
                partnership_id="partner-1", user_id="user-1",
            )

    result = asyncio.run(run_one())
    assert result["outcome"] == "enqueue_error"
    assert result["completed"] is False


def test_follow_up_is_bounded_and_does_not_reclassify_a_client_timeout():
    class Ledger:
        def session_run_facts(self, _ids):
            return [{"run_id": "run-1", "status": "running"}]

    results = [{"session_id": "session-1", "run_id": "run-1", "outcome": "client_timeout"}]
    follow_up = asyncio.run(load_test_chat._settle_runs(
        Ledger(), results, timeout_seconds=0, interval_seconds=1,
    ))

    assert follow_up["unfinished_count"] == 1
    assert results[0]["outcome"] == "client_timeout"
    assert results[0]["server_status_at_followup"] == "running"
    assert results[0]["final_server_status"] is None


def test_follow_up_records_later_failure_and_related_runs():
    class Ledger:
        def session_run_facts(self, _ids):
            return [
                {"run_id": "run-1", "status": "failed", "error_code": "MODEL_ERROR"},
                {"run_id": "open-1", "status": "succeeded"},
            ]

    results = [{"session_id": "session-1", "run_id": "run-1", "outcome": "client_timeout"}]
    follow_up = asyncio.run(load_test_chat._settle_runs(
        Ledger(), results, timeout_seconds=0, interval_seconds=1,
    ))

    assert follow_up["status_counts"] == {"failed": 1, "succeeded": 1}
    assert follow_up["unfinished_count"] == 0
    assert results[0]["outcome"] == "client_timeout"
    assert results[0]["final_outcome"] == "server_failed"
    assert results[0]["final_server_error_code"] == "MODEL_ERROR"


def test_session_run_facts_are_scoped_to_experiment_sessions(db):
    db.create_agent_run("dist-1", "u-a", "chat", "load-1", {"session_id": "session-1"})
    db.create_agent_run("dist-1", "u-a", "open", "load-2", {"session_id": "session-1"})
    db.create_agent_run("dist-1", "u-a", "chat", "load-3", {"session_id": "session-2"})

    rows = db.session_run_facts(["session-1"])

    assert {row["run_type"] for row in rows} == {"chat", "open"}
    assert {row["session_id"] for row in rows} == {"session-1"}


def test_open_loop_reports_generator_drops_separately(monkeypatch):
    async def create_session(_client, _base_url, _partnership_id, _user_id):
        return "session-1"

    async def one(_client, _base_url, session_id, _message, _timeout, **_kwargs):
        await asyncio.sleep(0.03)
        return {"session_id": session_id, "run_id": "run-1", "completed": True,
                "outcome": "completed", "total_ms": 30}

    class Ledger:
        engine = SimpleNamespace(dispose=lambda: None)

        def session_run_facts(self, _ids):
            return [{"run_id": "run-1", "status": "succeeded"}]

    monkeypatch.setattr(load_test_chat, "_create_session", create_session)
    monkeypatch.setattr(load_test_chat, "_one", one)
    monkeypatch.setattr(load_test_chat, "_ledger_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backend.db.Database", Ledger)
    args = SimpleNamespace(
        base_url="http://test", messages_file=None, message_template="hello {i}",
        partnership_ids=None, user_ids=None, partnership_id="p1", user_id="u1",
        concurrency=1, requests=4, request_timeout=1, completion_timeout=1,
        arrival_rate=60000, max_pending=1, settle_timeout=0, settle_interval=1,
    )

    report = asyncio.run(load_test_chat.run(args))

    assert report["workload"]["mode"] == "open_loop"
    assert report["summary"]["offered"] == 4
    assert report["summary"]["submitted"] == 1
    assert report["summary"]["outcomes"]["generator_dropped"] == 3
