"""The exercise Run API stores rows in PostgreSQL, not in process memory."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from backend.db import InvalidRunTransition, utcnow
from backend.db.outcome import Outcome
from backend.main import create_app
from backend.schemas import Partnership
from backend.services.run_finalize import finalize_run
from backend.services.run_names import RunStatus, ToolCallStatus

HEADERS = {"X-Org-Id": "org-1", "X-User-Id": "user-1"}
def _db(make_db, name: str):
    db = make_db(name)
    db.upsert_distributor("org-1", "Org 1")
    db.upsert_distributor("org-2", "Org 2")
    db.upsert_user("user-1", "org-1", "User 1")
    db.upsert_user("user-2", "org-2", "User 2")
    return db
BODY = {
    "run_type": "po",
    "idempotency_key": "po:partnership_01:draft:v1",
    "input": {"partnership_id": "partnership_01", "message": "帮我下两托盘纯净水"},
}
def test_create_run_commits_the_event_and_hides_other_orgs(make_db):
    db = _db(make_db, "runs")
    client = TestClient(create_app(db))

    created = client.post("/runs", json=BODY, headers=HEADERS)
    assert created.status_code == 201
    run_id = created.json()["id"]
    assert created.json()["status"] == "queued"
    assert created.json()["input_json"]["partnership_id"] == "partnership_01"
    assert created.json()["started_at"] is None
    assert created.json()["updated_at"] == created.json()["created_at"]

    replay = client.post("/runs", json=BODY, headers=HEADERS)
    assert replay.status_code == 200
    assert replay.json()["id"] == run_id

    events = client.get(f"/runs/{run_id}/events", headers=HEADERS)
    assert events.status_code == 200
    assert [(e["sequence"], e["event_type"]) for e in events.json()] == [(1, "run.created")]

    same_person = client.get(f"/runs/{run_id}", headers={"X-Org-Id": "org-2", "X-User-Id": "user-1"})
    assert same_person.status_code == 200
    assert same_person.json()["org_id"] == "org-1"
    hidden = client.get(f"/runs/{run_id}", headers={"X-Org-Id": "org-1", "X-User-Id": "user-2"})
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "RUN_NOT_FOUND"

    # A new connection to the same database still sees the row.
    again = TestClient(create_app(_db(make_db, "runs")))
    assert again.get(f"/runs/{run_id}", headers=HEADERS).status_code == 200

    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS)
    assert trace.status_code == 200
    body = trace.json()
    assert body["steps"] == []
    assert body["model_calls"] == []
    assert body["tool_calls"] == []
    assert body["checkpoints"] == []
    assert [e["sequence"] for e in body["events"]] == [1]
def test_finalize_run_leaves_the_parent_alone_when_the_lease_does_not_match(make_db):
    db = _db(make_db, "finalize-run-lease")
    parent, _created = db.create_agent_run(
        "org-1", "user-1", "chat", "finalize-run-parent", {"message": "hi"},
    )
    child, _created = db.create_agent_run(
        "org-1", "user-1", "tool", "finalize-run-child",
        {"task_id": "task-1", "session_id": "s1", "wave_id": "w1"},
        owner_run_id=parent["id"],
    )
    assert db.claim_chat_turn(child["id"], "claim-a", utcnow() + timedelta(minutes=5)) is not None
    assert finalize_run(
        db, child["id"],
        Outcome(kind="failed", error={"message": "no"}, reason="permanent", dead_letter=True),
        lease_owner="claim-b",
    ) is False
    assert db.find_agent_run_by_id(child["id"])["status"] == "running"
    assert db.latest_checkpoint(parent["id"]) is None


def test_same_process_reclaim_keeps_the_first_lease_from_finishing(make_db):
    import os

    from backend.celery.tasks import claim_owner
    from backend.services.run_worker import sweep_expired_leases

    db = _db(make_db, "same-pid-lease")
    first = claim_owner()
    second = claim_owner()
    assert first != second
    assert first.startswith(f"celery:{os.getpid()}:")
    assert second.startswith(f"celery:{os.getpid()}:")
    run, _created = db.create_agent_run("org-1", "user-1", "po", "same-pid", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], first, utcnow() - timedelta(seconds=5)) is not None
    sweep_expired_leases(db)
    assert db.find_agent_run_by_id(run["id"])["status"] == "queued"
    assert db.claim_chat_turn(run["id"], second, utcnow() + timedelta(minutes=5)) is not None
    assert finalize_run(
        db, run["id"], Outcome(kind="succeeded", output={"ok": True}), lease_owner=first,
    ) is False
    held = db.find_agent_run_by_id(run["id"])
    assert held["status"] == "running"
    assert held["lease_owner"] == second
    assert finalize_run(
        db, run["id"], Outcome(kind="succeeded", output={"ok": True}), lease_owner=second,
    ) is True
    assert db.find_agent_run_by_id(run["id"])["status"] == "succeeded"


def test_finalize_run_row_rejects_a_different_lease_owner(make_db):
    db = _db(make_db, "finalize-lease")
    run, _created = db.create_agent_run("org-1", "user-1", "chat", "finalize-lease", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], "claim-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(
        run["id"], Outcome(kind="succeeded", output={"ok": True}), lease_owner="claim-b",
    ) is None
    assert db.find_agent_run_by_id(run["id"])["status"] == "running"
    finished = db.finalize_run_row(
        run["id"], Outcome(kind="succeeded", output={"ok": True}), lease_owner="claim-a",
    )
    assert finished is not None and finished["status"] == "succeeded"


def test_cancel_and_resume_append_the_next_sequence(make_db):
    db = _db(make_db, "run-status")
    client = TestClient(create_app(db))
    created = client.post("/runs", json=BODY, headers=HEADERS).json()
    run_id = created["id"]

    too_early = client.post(f"/runs/{run_id}/resume", headers=HEADERS)
    assert too_early.status_code == 409
    assert too_early.json()["error"]["code"] == "RUN_NOT_WAITING_FOR_USER"

    claimed = db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(seconds=10))
    assert claimed is not None
    paused = db.transition_run("org-1", run_id, RunStatus.WAITING_FOR_USER, {})
    assert paused is not None
    assert paused["status"] == "waiting_for_user"
    assert paused["lease_owner"] is None

    resumed = client.post(f"/runs/{run_id}/resume", json={"input": {"message": "继续"}}, headers=HEADERS)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"

    cancelled = client.post(f"/runs/{run_id}/cancel", json={"reason": "客户改量"}, headers=HEADERS)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["ended_at"] is not None
    assert cancelled.json()["updated_at"] >= created["updated_at"]

    again = client.post(f"/runs/{run_id}/cancel", headers=HEADERS)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "RUN_ALREADY_TERMINAL"

    events = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["events"]
    assert [e["sequence"] for e in events] == [1, 2, 3, 4, 5, 6, 7]
    assert [e["event_type"] for e in events] == [
        "run.created",
        "run.started",
        "run.waiting_for_user",
        "run.resumed",
        "run.enqueued",
        "run.enqueued",
        "run.cancelled",
    ]
def test_create_run_writes_one_outbox_row_and_dispatch_enqueues_it(make_db):
    from backend.services.dispatcher import dispatch_pending

    db = _db(make_db, "outbox")
    client = TestClient(create_app(db))
    created = client.post("/runs", json=BODY, headers=HEADERS)
    assert created.status_code == 201
    run_id = created.json()["id"]

    replay = client.post("/runs", json=BODY, headers=HEADERS)
    assert replay.status_code == 200
    pending = db.find_pending_outbox_jobs()
    assert len(pending) == 1
    assert pending[0]["job_type"] == "execute_run"
    assert pending[0]["payload_json"]["run_id"] == run_id
    assert pending[0]["payload_json"]["trace_id"]

    assert dispatch_pending(db) == 1
    assert db.find_pending_outbox_jobs() == []
    events = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["events"]
    assert [e["event_type"] for e in events] == ["run.created", "run.enqueued"]
def test_permanent_error_fails_the_run_without_a_retry(make_db):
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import PermanentRunError, work_once

    db = _db(make_db, "permanent")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise PermanentRunError("参数不合法")

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "failed"
    events = client.get(f"/runs/{run_id}/events", headers=HEADERS).json()
    assert events[-1]["event_type"] == "run.failed"
    assert events[-1]["payload_json"]["reason"] == "permanent"
    assert db.find_pending_outbox_jobs() == []


def test_exhausted_json_repair_fails_once_and_dead_letters(make_db):
    from backend.llm.calls import JsonRepairExhausted
    from backend.llm.json_check import JSON_FAILURE_MESSAGE
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "json-repair")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise JsonRepairExhausted(JSON_FAILURE_MESSAGE)

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "failed"
    assert body["error_json"]["code"] == "JSON_REPAIR_EXHAUSTED"
    assert body["error_json"]["message"] == JSON_FAILURE_MESSAGE
    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()
    assert trace["events"][-1]["event_type"] == "run.failed"
    assert trace["dead_letters"][0]["error_json"]["code"] == "JSON_REPAIR_EXHAUSTED"
    assert db.find_pending_outbox_jobs() == []


def test_json_repair_failure_on_a_child_is_a_result_for_the_answer(make_db, monkeypatch):
    from datetime import timedelta

    from backend.llm.calls import JsonRepairExhausted
    from backend.llm.json_check import JSON_FAILURE_MESSAGE
    from backend.services.task_run import execute_task
    from backend.services.tool_registry import Tool, ToolRegistry

    db = _db(make_db, "json-repair-child")
    db.upsert_partnership(Partnership(
        id="ps-1", brand_id="brand-1", distributor_id="org-1", ship_to_country="US",
        currency="USD", brand_name="Brand", distributor_name="Org 1",
    ))
    db.upsert_user("u-1", "org-1", "User")
    session = db.create_session("json-repair-session", "ps-1", "u-1")
    parent, _ = db.create_agent_run(
        "org-1", "u-1", "chat", "chat:json-repair:parent",
        {"session_id": session["id"], "message": "两箱"},
    )
    parent_id = parent["id"]
    assert db.claim_chat_turn(parent_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent_id)["status"] == "waiting_for_tasks"
    payload = {
        "session_id": session["id"], "owner_run_id": parent_id, "task_id": "order-1",
        "kind": "tool", "name": "order", "tool_name": "order", "input": {},
        "wave_id": "wave-1",
    }
    child, _ = db.create_agent_run(
        "org-1", "u-1", "tool", "chat:json-repair:child", payload, owner_run_id=parent_id,
    )
    assert db.claim_chat_turn(child["id"], "worker-b", utcnow() + timedelta(minutes=5)) is not None

    def broken(_args, **_kwargs):
        raise JsonRepairExhausted(JSON_FAILURE_MESSAGE)

    monkeypatch.setattr(
        "backend.services.task_run.turn_tools",
        lambda *args, **kwargs: ToolRegistry([Tool(name="order", description="Order", fn=broken)]),
    )
    result = asyncio.run(execute_task(db, None, child))
    assert result["ok"] is False
    assert result["error"] == JSON_FAILURE_MESSAGE
    assert db.find_agent_run_by_id(parent_id)["status"] != "failed"
    assert db.run_trace("org-1", parent_id)["dead_letters"] == []
    failed = next(
        event for event in db.list_run_events("org-1", parent_id) if event["event_type"] == "tool.failed"
    )
    assert failed["payload_json"]["display"] == JSON_FAILURE_MESSAGE


def test_a_model_budget_failure_does_not_retry(make_db):
    from backend.llm.calls import ModelBudgetExceeded
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "budget-stop")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise ModelBudgetExceeded("org")

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "failed"
    assert body["error_json"]["code"] == "QUOTA_EXCEEDED"
    assert body["error_json"]["scope"] == "org"
    assert db.find_pending_outbox_jobs() == []


def test_transient_error_retries_then_dead_letters(make_db):
    from datetime import timedelta

    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "retry")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    immediate = (timedelta(0), timedelta(0), timedelta(0))

    def execute(_run):
        raise TimeoutError("订单系统超时")

    for _ in range(3):
        assert work_once(db, "worker-a", execute, immediate, run_id=run_id) is True
        body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
        assert body["status"] == "queued"
    assert work_once(db, "worker-a", execute, immediate, run_id=run_id) is True
    failed = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert failed["status"] == "failed"
    assert failed["error_json"]["code"] == "TIMEOUT"
    events = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["events"]
    assert [e["event_type"] for e in events if e["event_type"] == "run.retry_scheduled"] == [
        "run.retry_scheduled", "run.retry_scheduled", "run.retry_scheduled",
    ]
    assert events[-1]["payload_json"]["reason"] == "dead_letter"
def test_worker_loss_redeliveries_eventually_dead_letter(make_db):
    from backend.services.dispatcher import dispatch_pending

    db = _db(make_db, "worker-loss-dead-letter")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    for _ in range(3):
        assert db.record_worker_loss_redelivery(run_id) == ("continue", [])
    status, terminal = db.record_worker_loss_redelivery(run_id)
    assert status == "dead_letter" and terminal == [run_id]
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "failed"
    letters = db.list_dead_letter_jobs(run_id)
    assert len(letters) == 1
    assert letters[0]["error_json"]["code"] == "WORKER_LOST_REDELIVERY"
def test_model_capacity_requeues_without_spending_execution_attempt(make_db):
    from backend.llm.errors import ModelCapacityExceeded
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "model-capacity-requeue")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise ModelCapacityExceeded("qwen", "qwen3.7-flash")

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    row = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert row["status"] == "queued"
    pending = [
        item for item in db.run_trace("org-1", run_id)["outbox"]
        if item["job_type"] == "execute_run" and item["status"] == "pending"
    ]
    assert len(pending) == 1
    assert pending[0]["attempts"] == 0
    assert pending[0]["last_error_json"]["kind"] == "infrastructure"
    assert pending[0]["last_error_json"]["code"] == "MODEL_CAPACITY_EXCEEDED"
    assert pending[0]["last_error_json"]["infrastructure_attempts"] == 1
def test_provider_rate_limit_requeues_with_retry_after(make_db, monkeypatch):
    from backend.llm.errors import ProviderError
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    monkeypatch.setattr("backend.services.run_worker.random.uniform", lambda _a, _b: 1.0)
    db = _db(make_db, "provider-rate-limit-requeue")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise ProviderError(
            "qwen", "qwen3.7-flash", "rate_limited",
            status_code=429, retry_after_seconds=12, retryable=True,
        )

    before = utcnow()
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    row = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert row["status"] == "queued"
    pending = [
        item for item in db.run_trace("org-1", run_id)["outbox"]
        if item["job_type"] == "execute_run" and item["status"] == "pending"
    ]
    assert len(pending) == 1
    assert pending[0]["attempts"] == 0
    assert pending[0]["last_error_json"]["code"] == "PROVIDER_RATE_LIMITED"
    assert pending[0]["last_error_json"]["infrastructure_attempts"] == 1
    wait = (pending[0]["next_attempt_at"] - before).total_seconds()
    assert 11 <= wait <= 13
def test_provider_timeout_requeues_without_spending_an_attempt(make_db, monkeypatch):
    from backend.llm.errors import ProviderError
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    monkeypatch.setattr("backend.db.database.queue.random.uniform", lambda _a, _b: 1.0)
    db = _db(make_db, "provider-timeout-requeue")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise ProviderError("deepseek", "deepseek-flash", "timeout", retryable=True)

    before = utcnow()
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    row = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert row["status"] == "queued"
    pending = [
        item for item in db.run_trace("org-1", run_id)["outbox"]
        if item["job_type"] == "execute_run" and item["status"] == "pending"
    ]
    assert len(pending) == 1
    assert pending[0]["attempts"] == 0
    assert pending[0]["last_error_json"]["code"] == "PROVIDER_TIMEOUT"
    assert pending[0]["last_error_json"]["infrastructure_attempts"] == 1
    wait = (pending[0]["next_attempt_at"] - before).total_seconds()
    assert 0.9 <= wait <= 1.2
def test_circuit_open_fails_the_run_instead_of_waiting_out_the_cooldown(make_db):
    from backend.llm.errors import ProviderError
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "circuit-open-fail-fast")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise ProviderError(
            "qwen", "qwen3.7-flash", "circuit_open",
            retry_after_seconds=30, retryable=True,
        )

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    failed = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert failed["status"] == "failed"
    assert failed["error_json"]["code"] == "PROVIDER_CIRCUIT_OPEN"
    pending = [
        item for item in db.run_trace("org-1", run_id)["outbox"]
        if item["job_type"] == "execute_run" and item["status"] == "pending"
    ]
    assert pending == []
def test_provider_authentication_fails_the_run(make_db):
    from backend.llm.errors import ProviderError
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "provider-auth-fail")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)

    def execute(_run):
        raise ProviderError("qwen", "qwen3.7-flash", "authentication", status_code=401, retryable=False)

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    failed = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert failed["status"] == "failed"
    assert failed["error_json"]["code"] == "PROVIDER_AUTHENTICATION"
def test_forbidden_run_transitions_do_not_commit(make_db):
    db = _db(make_db, "transition")
    client = TestClient(create_app(db))

    def open_run(index: int) -> str:
        created = client.post(
            "/runs",
            json={**BODY, "idempotency_key": f"transition:{index}"},
            headers=HEADERS,
        )
        assert created.status_code == 201
        return created.json()["id"]

    def reject(run_id: str, to_status: str, source: str, events: list[str]) -> None:
        with pytest.raises(InvalidRunTransition) as caught:
            db.transition_run("org-1", run_id, to_status, {})
        assert (caught.value.from_status, caught.value.to_status) == (source, to_status)
        assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == source
        listed = client.get(f"/runs/{run_id}/events", headers=HEADERS).json()
        assert [event["event_type"] for event in listed] == events

    queued_id = open_run(1)
    reject(queued_id, RunStatus.SUCCEEDED, RunStatus.QUEUED, ["run.created"])

    waiting_id = open_run(2)
    assert db.claim_chat_turn(waiting_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    paused = db.transition_run("org-1", waiting_id, RunStatus.WAITING_FOR_USER, {})
    assert paused is not None and paused["lease_owner"] is None
    reject(
        waiting_id,
        RunStatus.SUCCEEDED,
        RunStatus.WAITING_FOR_USER,
        ["run.created", "run.started", "run.waiting_for_user"],
    )

    succeeded_id = open_run(3)
    assert db.claim_chat_turn(succeeded_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(succeeded_id, Outcome(kind="succeeded", output={"answer": "ok"}), allowed_from=frozenset({RunStatus.RUNNING}))["status"] == "succeeded"
    reject(
        succeeded_id,
        RunStatus.RUNNING,
        RunStatus.SUCCEEDED,
        ["run.created", "run.started", "run.completed"],
    )

    failed_id = open_run(4)
    assert db.claim_chat_turn(failed_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.transition_run("org-1", failed_id, RunStatus.FAILED, {"reason": "permanent"})["status"] == "failed"
    reject(
        failed_id,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        ["run.created", "run.started", "run.failed"],
    )

    cancelled_id = open_run(5)
    cancelled = client.post(f"/runs/{cancelled_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200
    reject(cancelled_id, RunStatus.RUNNING, RunStatus.CANCELLED, ["run.created", "run.cancelled"])
def test_create_run_accepts_only_the_five_types(make_db):
    db = _db(make_db, "run-types")
    client = TestClient(create_app(db))
    for run_type in ("po", "contract", "report", "data", "code", "brand", "fix", "reflect", "review", "revise", "tool", "chat"):
        created = client.post(
            "/runs",
            json={**BODY, "run_type": run_type, "idempotency_key": f"{run_type}:once"},
            headers=HEADERS,
        )
        assert created.status_code == 201
        assert created.json()["run_type"] == run_type

    rejected = client.post(
        "/runs",
        json={**BODY, "run_type": "purchase_order", "idempotency_key": "old"},
        headers=HEADERS,
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "INVALID_RUN_TYPE"
def test_an_expired_lease_is_swept_without_spending_a_retry(make_db):
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import sweep_expired_leases

    db = _db(make_db, "lease-sweep")
    client = TestClient(create_app(db))

    def claim(key: str, lease_for: timedelta) -> str:
        created = client.post(
            "/runs",
            json={**BODY, "idempotency_key": key},
            headers=HEADERS,
        )
        run_id = created.json()["id"]
        dispatch_pending(db)
        assert db.claim_chat_turn(run_id, "worker-a", utcnow() + lease_for) is not None
        return run_id

    expired_id = claim("lease:expired", timedelta(seconds=-5))
    live_id = claim("lease:live", timedelta(minutes=5))
    assert sweep_expired_leases(db) == 1

    released = client.get(f"/runs/{expired_id}", headers=HEADERS).json()
    assert released["status"] == "queued"
    assert released["lease_owner"] is None
    assert released["attempts"] == 1
    events = client.get(f"/runs/{expired_id}/trace", headers=HEADERS).json()["events"]
    assert events[-2]["event_type"] == "run.retry_scheduled"
    assert events[-1]["event_type"] == "run.requeued_by_sweeper"
    assert events[-1]["payload_json"]["reason"] == "lease_expired"
    pending = [
        row for row in db.find_pending_outbox_jobs()
        if row["payload_json"].get("run_id") == expired_id
    ]
    assert len(pending) == 1
    assert pending[0]["attempts"] == 0
    assert pending[0]["payload_json"]["trace_id"]
    assert dispatch_pending(db) == 1

    assert client.get(f"/runs/{live_id}", headers=HEADERS).json()["status"] == "running"
    assert sweep_expired_leases(db) == 0

    acked_id = claim("lease:acked", timedelta(seconds=-5))
    assert sweep_expired_leases(db) == 1
    assert client.get(f"/runs/{acked_id}", headers=HEADERS).json()["status"] == "queued"
    assert dispatch_pending(db) == 1
    assert db.claim_chat_turn(acked_id, "worker-b", utcnow() + timedelta(minutes=5)) is not None


def test_renew_extends_only_the_live_owner(make_db):
    from sqlalchemy import text

    from backend.services.dispatcher import dispatch_pending

    db = _db(make_db, "lease-renew")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:renew"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    before = db.find_agent_run_by_id(run_id)["lease_expires_at"]

    later = utcnow() + timedelta(minutes=30)
    assert db.renew_run_lease(run_id, "worker-a", later) == "renewed"
    renewed = db.find_agent_run_by_id(run_id)
    assert renewed["lease_owner"] == "worker-a"
    assert renewed["status"] == "running"
    assert renewed["lease_expires_at"] > before

    frozen = renewed["lease_expires_at"]
    assert db.renew_run_lease(run_id, "worker-b", utcnow() + timedelta(hours=1)) == "lost"
    assert db.find_agent_run_by_id(run_id)["lease_expires_at"] == frozen
    assert db.find_agent_run_by_id(run_id)["lease_owner"] == "worker-a"

    with db.engine.begin() as conn:
        conn.execute(text(
            "UPDATE agent_runs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"
        ), {"id": run_id})
    assert db.renew_run_lease(run_id, "worker-a", utcnow() + timedelta(minutes=5)) == "lost"
    expired = db.find_agent_run_by_id(run_id)
    assert expired["status"] == "running"
    assert expired["lease_expires_at"] < utcnow()


def test_renew_stops_quietly_once_the_run_is_parked(make_db):
    from backend.services.dispatcher import dispatch_pending

    db = _db(make_db, "lease-settled")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:settled"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(run_id)["status"] == "waiting_for_tasks"
    assert db.renew_run_lease(run_id, "worker-a", utcnow() + timedelta(minutes=5)) == "settled"
    parked = db.find_agent_run_by_id(run_id)
    assert parked["lease_owner"] is None
    assert parked["lease_expires_at"] is None


def test_a_live_worker_keeps_renewing_until_the_lease_is_stolen(make_db):
    from sqlalchemy import text

    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import RunLease

    db = _db(make_db, "lease-beat")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:beat"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(seconds=30)) is not None
    before = db.find_agent_run_by_id(run_id)["lease_expires_at"]
    hold = RunLease(db, run_id, "worker-a", beat=0.05)
    try:
        deadline = time.time() + 2
        while time.time() < deadline:
            row = db.find_agent_run_by_id(run_id)
            if row["lease_expires_at"] > before and row["lease_owner"] == "worker-a":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("lease was not renewed")
        assert hold.lost is False
        with db.engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_runs SET lease_owner = 'worker-b', "
                "lease_expires_at = now() + interval '5 minutes' WHERE id = :id"
            ), {"id": run_id})
        deadline = time.time() + 2
        while time.time() < deadline and not hold.lost:
            time.sleep(0.05)
        assert hold.lost is True
    finally:
        hold.stop()
    stolen = db.find_agent_run_by_id(run_id)
    assert stolen["lease_owner"] == "worker-b"
    assert stolen["status"] == "running"


def test_a_worker_that_lost_its_lease_does_not_finish_the_run(make_db):
    from sqlalchemy import text

    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "lease-lost-finish")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:lost-finish"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)

    def execute(run):
        with db.engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_runs SET lease_owner = 'worker-b' WHERE id = :id"
            ), {"id": run["id"]})
        return {"text": "should not land"}

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "running"
    assert body["lease_owner"] == "worker-b"
    assert body["output_json"] is None


def test_an_expired_lease_is_not_extended_when_the_worker_returns(make_db):
    from sqlalchemy import text

    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "lease-expired-finish")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:expired-finish"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)

    def execute(run):
        with db.engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_runs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"
            ), {"id": run["id"]})
        return {"text": "too late"}

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "running"
    assert body["lease_owner"] == "worker-a"
    assert body["output_json"] is None
    assert db.find_agent_run_by_id(run_id)["lease_expires_at"] < utcnow()


def test_parking_for_tasks_still_releases_the_lease(make_db):
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import work_once

    db = _db(make_db, "lease-park")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:park"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)

    def execute(run):
        assert db.mark_waiting_for_tasks(run["id"])["status"] == "waiting_for_tasks"
        return {"text": "parked"}

    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "waiting_for_tasks"
    assert body["lease_owner"] is None
    assert body["lease_expires_at"] is None


def test_an_expired_lease_cancels_a_run_that_already_asked(make_db):
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import sweep_expired_leases

    db = _db(make_db, "lease-cancel")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:cancel"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() - timedelta(seconds=5)) is not None
    cancelled = client.post(f"/runs/{run_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "running"

    assert sweep_expired_leases(db) == 1
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "cancelled"
    assert body["lease_owner"] is None
    assert db.find_pending_outbox_jobs() == []
    events = client.get(f"/runs/{run_id}/events", headers=HEADERS).json()
    assert events[-1]["event_type"] == "run.cancelled"
    assert events[-1]["payload_json"]["reason"] == "lease_expired"
def test_rerun_starts_a_new_run_from_the_parent(make_db):
    db = _db(make_db, "rerun")
    client = TestClient(create_app(db))
    parent = client.post("/runs", json=BODY, headers=HEADERS)
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    created = client.post(
        f"/runs/{parent_id}/rerun",
        json={"idempotency_key": "po:rerun:1"},
        headers=HEADERS,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["id"] != parent_id
    assert body["parent_run_id"] == parent_id
    assert body["status"] == "queued"
    assert body["run_type"] == "po"
    assert body["input_json"] == BODY["input"]
    assert client.get(f"/runs/{parent_id}", headers=HEADERS).json()["status"] == "queued"
    events = client.get(f"/runs/{body['id']}/events", headers=HEADERS).json()
    assert events[0]["payload_json"]["parent_run_id"] == parent_id

    replay = client.post(
        f"/runs/{parent_id}/rerun",
        json={"idempotency_key": "po:rerun:1"},
        headers=HEADERS,
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == body["id"]

    overridden = client.post(
        f"/runs/{parent_id}/rerun",
        json={"idempotency_key": "po:rerun:2", "input": {"message": "改一箱"}},
        headers=HEADERS,
    )
    assert overridden.status_code == 201
    assert overridden.json()["input_json"] == {"message": "改一箱"}
    assert overridden.json()["run_type"] == "po"
    assert overridden.json()["parent_run_id"] == parent_id

    missing = client.post(
        "/runs/run_missing/rerun",
        json={"idempotency_key": "po:rerun:missing"},
        headers=HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"

    hidden = client.post(
        f"/runs/{parent_id}/rerun",
        json={"idempotency_key": "po:rerun:other"},
        headers={"X-Org-Id": "org-2", "X-User-Id": "user-2"},
    )
    assert hidden.status_code == 404
def test_an_outbox_past_max_attempts_is_dead_lettered(make_db):
    db = _db(make_db, "outbox-dead")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    pending = db.find_pending_outbox_jobs()
    assert len(pending) == 1
    outbox_id = pending[0]["id"]
    assert pending[0]["max_attempts"] == 10

    for _ in range(10):
        updated, terminal = db.mark_outbox_retry(outbox_id, "publish failed")
        assert updated is not None and terminal == []
        assert updated["status"] == "pending"

    _updated, terminal = db.mark_outbox_retry(outbox_id, "publish failed")
    assert terminal == [run_id]
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "failed"
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["attempts"] == 1
    events = client.get(f"/runs/{run_id}/events", headers=HEADERS).json()
    assert [event["event_type"] for event in events] == ["run.created", "run.started", "run.failed"]
    assert events[-1]["payload_json"]["reason"] == "dead_letter"

    letters = db.list_dead_letter_jobs(run_id)
    assert len(letters) == 1
    assert letters[0]["job_type"] == "execute_run"
    assert letters[0]["payload_json"]["run_id"] == run_id
    assert letters[0]["payload_json"]["trace_id"]
    assert letters[0]["attempts"] == 11
    assert letters[0]["error_json"] == {"kind": "dead_letter", "message": "publish failed"}
    assert db.mark_outbox_retry(outbox_id, "publish failed") == (None, [])
def test_public_events_skip_the_cursor_and_drop_private_fields(make_db):
    db = _db(make_db, "public-events")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    db.append_run_event("org-1", run_id, "model.completed", {
        "purpose": "probe",
        "prompt": "secret",
        "prompt_tokens": 9,
        "token": "nope",
        "status": "succeeded",
    })
    db.append_run_event("org-1", run_id, "run.retry_scheduled", {"reason": "later"})

    listed = client.get(f"/runs/{run_id}/events", headers={**HEADERS, "Last-Event-ID": "1"})
    assert listed.status_code == 200
    body = listed.json()
    assert [row["event_type"] for row in body] == ["model.completed"]
    assert body[0]["sequence"] == 2
    assert body[0]["payload_json"]["status"] == "succeeded"
    assert "purpose" not in body[0]["payload_json"]
    assert "prompt" not in body[0]["payload_json"]
    assert "prompt_tokens" not in body[0]["payload_json"]
    assert "token" not in body[0]["payload_json"]

    hidden = client.get(f"/runs/{run_id}/events", headers={"X-Org-Id": "org-1", "X-User-Id": "user-2"})
    assert hidden.status_code == 404
    ignored = client.get(f"/runs/{run_id}/events", headers={"X-Org-Id": "org-2", "X-User-Id": "user-1"})
    assert ignored.status_code == 200
    opened = client.get(f"/runs/{run_id}/events", headers=HEADERS).json()
    assert opened[0]["event_type"] == "run.created"
    assert "display" not in opened[0]["payload_json"]
    stored = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["events"]
    assert [row["event_type"] for row in stored] == ["run.created", "model.completed", "run.retry_scheduled"]
    assert stored[1]["payload_json"]["purpose"] == "probe"
def test_event_stream_closes_after_the_terminal_event(make_db):
    db = _db(make_db, "event-stream")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    cancelled = client.post(f"/runs/{run_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200

    with client.stream("GET", f"/runs/{run_id}/events/stream", headers=HEADERS) as response:
        assert response.status_code == 200
        text = response.read().decode()
    assert "id: 1\nevent: run.created\n" in text
    assert "event: run.cancelled\n" in text
    assert "已停止" in text
    assert "purpose" not in text
    assert "run.enqueued" not in text

    missing = client.get("/runs/run_missing/events/stream", headers=HEADERS)
    assert missing.status_code == 404
def test_event_stream_wakes_when_the_run_is_notified(make_db, monkeypatch):
    import threading
    import time

    monkeypatch.setattr("backend.services.run_stream.HEARTBEAT_SECONDS", 0.4)
    db = _db(make_db, "event-notify")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    from backend.services.dispatcher import dispatch_pending

    dispatch_pending(db)
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(seconds=30)) is not None
    held: list[bytes] = []

    def read() -> None:
        with client.stream("GET", f"/runs/{run_id}/events/stream", headers=HEADERS) as response:
            held.append(response.read())

    thread = threading.Thread(target=read)
    thread.start()
    time.sleep(0.3)
    assert db.finalize_run_row(run_id, Outcome(kind="succeeded", output={"text": "ok"}), allowed_from=frozenset({RunStatus.RUNNING}))["status"] == "succeeded"
    thread.join(timeout=5)
    assert thread.is_alive() is False
    text = held[0].decode()
    assert "event: run.completed\n" in text
    assert "回复结束" in text
def test_tool_error_is_code_message_and_retryable():
    from backend.services.run_names import ToolErrorCode, tool_error

    assert tool_error(ToolErrorCode.VALIDATION, "地址不合法") == {
        "code": "TOOL_VALIDATION_ERROR",
        "message": "地址不合法",
        "retryable": False,
    }
    assert tool_error(ToolErrorCode.UPSTREAM, "timeout", retryable=True)["retryable"] is True
def test_tools_register_native_and_domain(db):
    from backend.llm import FakeLLM
    from backend.service import new_session
    from backend.services.session_tools import tools
    from backend.tools import web_search

    db.upsert_user("u-a", "dist-1", "Ada")
    session = new_session(db, "ps-1", "u-a")
    registry = tools(db, FakeLLM(), session, "hello")
    names = [schema["function"]["name"] for schema in registry.schemas()]
    assert names == [
        "web_search", "translate", "exchange_rate", "send_email",
        "po.draft", "contract.read", "report.draft", "data.query", "brand.search",
    ]
    assert registry.get("send_email").requires_approval is True
    assert registry.get("web_search").requires_approval is False
    assert registry.get("po.draft").requires_approval is False
    assert registry.get("web_search").fn is web_search.run
    mail = registry.get("send_email")
    assert mail.side_effect is True and mail.idempotency_required is True
    assert mail.timeout_seconds == 30 and mail.retryable_errors == frozenset()
    assert mail.display == "正在发信"
    search = registry.get("web_search")
    assert search.side_effect is False and search.timeout_seconds == 30
    assert search.retryable_errors == frozenset({
        "TOOL_TIMEOUT", "TOOL_RATE_LIMITED", "TOOL_UPSTREAM_ERROR",
    })
    assert registry.get("po.draft").side_effect is False
    assert registry.get("po.draft").timeout_seconds == 300
    assert registry.get("po.draft").retryable_errors == frozenset()
    assert registry.get("data.query").timeout_seconds == 120
    assert "TOOL_RATE_LIMITED" not in registry.get("data.query").retryable_errors
    assert "parameters" in registry.get("send_email").schema()["function"]
    assert "side_effect" not in registry.get("send_email").schema()["function"]
def test_mcp_tools_join_when_enabled_and_wait_for_approval(db, monkeypatch):
    from backend.llm import FakeLLM
    from backend.service import new_session
    from backend.services import session_tools
    from backend.tools import web_search

    class Hub:
        schemas: ClassVar[list] = [
            {"type": "function", "function": {
                "name": "socialcrawl_request",
                "description": "Call SocialCrawl",
                "parameters": {"type": "object", "properties": {}},
            }},
            {"type": "function", "function": {
                "name": "web_search",
                "description": "should not replace the native tool",
                "parameters": {"type": "object", "properties": {}},
            }},
            {"type": "function", "function": {
                "name": "get-user",
                "description": "hyphenated names are skipped",
                "parameters": {"type": "object", "properties": {}},
            }},
        ]

    monkeypatch.setattr(session_tools, "mcp_enabled", lambda: True)
    monkeypatch.setattr(session_tools, "get_hub", lambda: Hub())
    monkeypatch.setattr(session_tools, "make_mcp_tool_fn", lambda hub, name: (lambda arguments, **kwargs: {"ok": True, "name": name}))
    db.upsert_user("u-a", "dist-1", "Ada")
    session = new_session(db, "ps-1", "u-a")
    registry = session_tools.tools(db, FakeLLM(), session, "hello")
    names = [schema["function"]["name"] for schema in registry.schemas()]
    assert names == [
        "web_search", "translate", "exchange_rate", "send_email",
        "po.draft", "contract.read", "report.draft", "data.query", "brand.search",
        "socialcrawl_request",
    ]
    assert registry.get("web_search").fn is web_search.run
    crawled = registry.get("socialcrawl_request")
    assert crawled.requires_approval is True
    assert crawled.side_effect is True and crawled.idempotency_required is True
    assert crawled.timeout_seconds == 60 and crawled.retryable_errors == frozenset()
    assert crawled.display == ""
def _leased_run(db, client, key: str):
    from datetime import timedelta

    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": key}, headers=HEADERS,
    ).json()["id"]
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(seconds=60)) is not None
    run = db.find_agent_run_by_id(run_id)
    assert run is not None
    return run
def test_planned_side_effect_is_claimed_before_the_handler_and_reused(make_db, monkeypatch):
    from backend.services.task_run import execute_task
    from backend.services.tool_registry import Tool, ToolRegistry

    db = _db(make_db, "planned-side-effect")
    db.upsert_partnership(Partnership(
        id="ps-1", brand_id="brand-1", distributor_id="org-1", ship_to_country="US",
        currency="USD", brand_name="Brand", distributor_name="Org 1",
    ))
    db.upsert_user("u-1", "org-1", "User")
    session = db.create_session("planned-side-session", "ps-1", "u-1")
    parent, _ = db.create_agent_run(
        "org-1", "u-1", "chat", "chat:planned-side-session:parent",
        {"session_id": session["id"], "message": "send quote"},
    )
    parent_id = parent["id"]
    calls: list[dict] = []

    def send(args, **_kwargs):
        calls.append(dict(args))
        return {"ok": True, "message_id": "m-1"}

    registry = ToolRegistry([Tool(
        name="send_email", description="Send", fn=send, side_effect=True,
        idempotency_required=True,
    )])
    monkeypatch.setattr("backend.services.task_run.turn_tools", lambda *args, **kwargs: registry)
    payload = {
        "session_id": session["id"], "owner_run_id": parent_id, "task_id": "mail-1",
        "kind": "tool", "name": "send_email", "tool_name": "send_email",
        "input": {"to": "buyer@example.com", "subject": "Quote", "body": "10 cases"},
        "wave_id": "wave-1",
    }
    from backend.redis_store import begin_turn

    assert db.claim_chat_turn(parent_id, "test-worker", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent_id, {"wave_id": "wave-1"}) is not None
    begin_turn(session["id"], "wave-1", ["mail-1"])
    run, _ = db.create_agent_run(
        "org-1", "u-1", "tool", f"chat:planned-side-session:{parent_id}:mail-1", payload,
        owner_run_id=parent_id,
    )
    assert db.claim_chat_turn(run["id"], "test-worker", utcnow() + timedelta(minutes=5)) is not None

    first = asyncio.run(execute_task(db, None, run))
    second = asyncio.run(execute_task(db, None, run))
    replay, _ = db.create_agent_run(
        "org-1", "u-1", "tool", f"chat:planned-side-session:{parent_id}:mail-1:replay", payload,
        owner_run_id=parent_id,
    )
    assert db.claim_chat_turn(replay["id"], "test-worker", utcnow() + timedelta(minutes=5)) is not None
    third = asyncio.run(execute_task(db, None, replay))

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is True
    assert calls == [payload["input"]]
    rows = db.list_tool_calls(run["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == ToolCallStatus.SUCCEEDED
    assert rows[0]["idempotency_key"] == f"send_email:{parent_id}:mail-1"
def test_planned_side_effect_with_lost_response_is_not_retried(make_db, monkeypatch):
    from backend.services.task_run import execute_task
    from backend.services.tool_registry import Tool, ToolRegistry

    db = _db(make_db, "planned-lost-response")
    db.upsert_partnership(Partnership(
        id="ps-1", brand_id="brand-1", distributor_id="org-1", ship_to_country="US",
        currency="USD", brand_name="Brand", distributor_name="Org 1",
    ))
    db.upsert_user("u-1", "org-1", "User")
    session = db.create_session("lost-response-session", "ps-1", "u-1")
    parent, _ = db.create_agent_run(
        "org-1", "u-1", "chat", "chat:lost-response-session:parent",
        {"session_id": session["id"], "message": "send quote"},
    )
    parent_id = parent["id"]
    calls: list[dict] = []

    def send(args, **_kwargs):
        calls.append(dict(args))
        raise KeyboardInterrupt("crash after the external call")

    registry = ToolRegistry([Tool(
        name="send_email", description="Send", fn=send, side_effect=True,
        idempotency_required=True,
    )])
    monkeypatch.setattr("backend.services.task_run.turn_tools", lambda *args, **kwargs: registry)
    payload = {
        "session_id": session["id"], "owner_run_id": parent_id, "task_id": "mail-2",
        "kind": "tool", "name": "send_email", "tool_name": "send_email",
        "input": {"to": "buyer@example.com", "subject": "Quote", "body": "10 cases"},
        "wave_id": "wave-2",
    }
    from backend.redis_store import begin_turn

    assert db.claim_chat_turn(parent_id, "test-worker", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent_id, {"wave_id": "wave-2"}) is not None
    begin_turn(session["id"], "wave-2", ["mail-2"])
    run, _ = db.create_agent_run(
        "org-1", "u-1", "tool", f"chat:lost-response-session:{parent_id}:mail-2", payload,
        owner_run_id=parent_id,
    )
    assert db.claim_chat_turn(run["id"], "test-worker", utcnow() + timedelta(minutes=5)) is not None

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(execute_task(db, None, run))
    result = asyncio.run(execute_task(db, None, run))

    assert result["ok"] is False
    assert "manual review" in result["error"]
    assert len(calls) == 1
    rows = db.list_tool_calls(run["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == ToolCallStatus.RESPONSE_LOST
    events = db.list_run_events("org-1", run["id"])
    assert any(event["event_type"] == "tool.manual_review" for event in events)
def test_three_expired_leases_fail_the_run(make_db):
    from backend.services.run_worker import sweep_expired_leases

    db = _db(make_db, "lease-budget")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:budget"}, headers=HEADERS,
    ).json()["id"]
    for _ in range(3):
        assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(seconds=-5)) is not None
        sweep_expired_leases(db)
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "failed"
    assert body["attempts"] == 3
def test_a_terminal_run_is_not_executed_again(make_db):
    from backend.services.run_worker import work_once

    def execute(_run):
        raise AssertionError("execute must not run")

    db = _db(make_db, "terminal-skip")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(run_id, Outcome(kind="succeeded", output={"text": "done"}), allowed_from=frozenset({RunStatus.RUNNING})) is not None
    assert work_once(db, "worker-b", execute, run_id=run_id) is True
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "succeeded"
def test_sweep_terminal_child_reaches_the_parent_checkpoint(make_db):
    from backend.services.run_worker import sweep_expired_leases
    from backend.services.sweeper import close_attention

    db = _db(make_db, "sweep-parent")
    lease_parent, _created = db.create_agent_run(
        "org-1", "user-1", "chat", "sweep-lease-parent", {"message": "hi"},
    )
    lease_child, _created = db.create_agent_run(
        "org-1", "user-1", "tool", "sweep-lease-child",
        {"task_id": "lease-task", "session_id": "s-lease", "wave_id": "w-lease"},
        owner_run_id=lease_parent["id"],
    )
    for _ in range(3):
        assert db.claim_chat_turn(
            lease_child["id"], "worker-a", utcnow() - timedelta(seconds=5),
        ) is not None
        sweep_expired_leases(db)
    lease_saved = db.latest_checkpoint(lease_parent["id"]) or {}
    lease_results = ((lease_saved.get("state_json") or {}).get("orchestrator") or {}).get("results") or {}
    assert lease_results["lease-task"]["ok"] is False

    duration_parent, _created = db.create_agent_run(
        "org-1", "user-1", "chat", "sweep-duration-parent", {"message": "hi"},
    )
    duration_child, _created = db.create_agent_run(
        "org-1", "user-1", "tool", "sweep-duration-child",
        {"task_id": "duration-task", "session_id": "s-duration", "wave_id": "w-duration"},
        owner_run_id=duration_parent["id"], max_duration_seconds=0,
    )
    assert db.claim_chat_turn(
        duration_child["id"], "worker-a", utcnow() + timedelta(minutes=5),
    ) is not None
    assert duration_child["id"] in close_attention(db)
    duration_saved = db.latest_checkpoint(duration_parent["id"]) or {}
    duration_results = ((duration_saved.get("state_json") or {}).get("orchestrator") or {}).get("results") or {}
    assert duration_results["duration-task"]["ok"] is False


def test_dead_letter_insert_is_ignored_the_second_time(make_db):
    from backend.db.orm import AgentRun

    db = _db(make_db, "dead-letter-once")
    run, _created = db.create_agent_run("org-1", "user-1", "chat", "dead-letter-once", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(
        run["id"], Outcome(kind="failed", error={"message": "no"}, reason="permanent", dead_letter=True),
    ) is not None
    with db.tx() as s:
        row = s.get(AgentRun, run["id"])
        db._dead_letter(s, row, {"message": "again"}, utcnow(), attempts=2)
    assert len(db.list_dead_letter_jobs(run["id"])) == 1


def test_over_duration_is_the_same_outcome_for_worker_and_sweeper(make_db):
    from backend.db.outcome import over_duration_outcome
    from backend.services.run_worker import work_once
    from backend.services.sweeper import close_attention

    failed = over_duration_outcome(False)
    cancelled = over_duration_outcome(True)
    assert failed == Outcome(
        kind="failed", reason="over_duration", dead_letter=True,
        error={"kind": "permanent", "code": "COST_BUDGET_EXCEEDED", "message": "run duration exceeded"},
    )
    assert cancelled.kind == "cancelled" and cancelled.dead_letter is False

    db = _db(make_db, "over-duration-same")

    def execute(_run):
        raise AssertionError("execute must not run")

    worker, _created = db.create_agent_run(
        "org-1", "user-1", "po", "over-duration-worker", {"message": "hi"}, max_duration_seconds=0,
    )
    assert work_once(db, "worker-a", execute, run_id=worker["id"]) is True
    swept, _created = db.create_agent_run(
        "org-1", "user-1", "po", "over-duration-sweep", {"message": "hi"}, max_duration_seconds=0,
    )
    assert db.claim_chat_turn(swept["id"], "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert swept["id"] in close_attention(db)
    worker_row = db.find_agent_run_by_id(worker["id"])
    swept_row = db.find_agent_run_by_id(swept["id"])
    assert worker_row["error_json"] == swept_row["error_json"] == failed.error
    assert len(db.list_dead_letter_jobs(worker["id"])) == 1
    assert len(db.list_dead_letter_jobs(swept["id"])) == 1

    asked, _created = db.create_agent_run(
        "org-1", "user-1", "po", "over-duration-cancel", {"message": "hi"}, max_duration_seconds=0,
    )
    assert db.claim_chat_turn(asked["id"], "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.request_run_cancel("org-1", asked["id"], "stop") is not None
    assert asked["id"] in close_attention(db)
    assert db.find_agent_run_by_id(asked["id"])["status"] == "cancelled"
    assert db.list_dead_letter_jobs(asked["id"]) == []


def test_redispatch_keeps_stale_wakes_until_a_new_result(make_db):
    from backend.orchestrator.gather import checkpoint_state

    db = _db(make_db, "stale-wakes")
    owner, _created = db.create_agent_run(
        "org-1", "user-1", "chat", "stale-owner", {"message": "hi"},
    )
    previous = checkpoint_state([], [], {}, wave_id="wave-old", wave_ids=["old"])
    previous["orchestrator"]["stale_wakes"] = 2
    db.append_checkpoint(owner["id"], previous)
    assert db.claim_chat_turn(owner["id"], "worker-a", utcnow() + timedelta(minutes=5)) is not None
    parked = db.commit_task_wave(
        owner["id"],
        [{
            "run_type": "tool",
            "idempotency_key": "stale-child",
            "input": {"task_id": "task-1"},
            "started": {"tool_name": "tool", "task_id": "task-1", "display": "tool"},
        }],
        checkpoint_state([], [], {}, wave_id="wave-new", wave_ids=["task-1"]),
    )
    assert parked is not None
    carried = db.latest_checkpoint(owner["id"])["state_json"]["orchestrator"]
    assert carried["wave_id"] == "wave-new"
    assert carried["stale_wakes"] == 2
    result = {"id": "task-1", "ok": False, "status": "failed", "error": "no"}
    assert db.record_child_wave_result(
        "org-1", owner["id"], "task-1", result, event_type=None, event_payload=None,
    ) is True
    assert db.latest_checkpoint(owner["id"])["state_json"]["orchestrator"]["stale_wakes"] == 0
    db.note_stale_wake(owner["id"], progressed=False)
    db.note_stale_wake(owner["id"], progressed=False)
    assert db.record_child_wave_result(
        "org-1", owner["id"], "task-1", result, event_type=None, event_payload=None,
    ) is True
    assert db.latest_checkpoint(owner["id"])["state_json"]["orchestrator"]["stale_wakes"] == 2


def test_a_run_past_its_duration_fails_without_the_model(make_db):
    from backend.services.run_worker import work_once

    def execute(_run):
        raise AssertionError("execute must not run")

    db = _db(make_db, "duration")
    run, _created = db.create_agent_run(
        "org-1", "user-1", "po", "duration", {"message": "hi"}, max_duration_seconds=0,
    )
    assert work_once(db, "worker-a", execute, run_id=run["id"]) is True
    saved = db.find_agent_run_by_id(run["id"])
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["error_json"]["code"] == "COST_BUDGET_EXCEEDED"
def test_the_queue_switch_does_not_claim(make_db, monkeypatch):
    import pytest

    from backend.services.run_worker import QueueDisabled, work_once

    monkeypatch.setenv("QUEUE_ENABLED", "0")

    def execute(run):
        raise AssertionError("execute must not run")

    db = _db(make_db, "queue-off")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    with pytest.raises(QueueDisabled):
        work_once(db, "worker-a", execute, run_id=run_id)
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "queued"
def test_a_full_run_gate_returns_the_run_to_postgres(make_db, monkeypatch):
    from backend.services.run_worker import work_once

    monkeypatch.setattr(
        "backend.services.run_worker.acquire_run",
        lambda org_id, user_id: None,
    )

    def execute(run):
        raise AssertionError("execute must not run")

    db = _db(make_db, "gate-full")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    saved = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert saved["status"] == "queued"
    assert saved["lease_owner"] is None
    assert db.find_agent_run_by_id(run_id)["capacity_wait_since"] is not None
    pending = [row for row in db.run_trace("org-1", run_id)["outbox"] if row["status"] == "pending"]
    assert pending
def test_create_rate_limit_uses_the_user_not_the_header(make_db, monkeypatch):
    monkeypatch.setenv("KA_ACCOUNT_RATE_CREATE_ORG", "1")
    db = _db(make_db, "rate")
    db.upsert_distributor("org-rate", "Rate")
    db.upsert_user("user-rate", "org-rate", "Rate")
    client = TestClient(create_app(db))
    headers = {"X-Org-Id": "org-1", "X-User-Id": "user-rate"}
    first = client.post("/runs", json={**BODY, "idempotency_key": "rate:1"}, headers=headers)
    assert first.status_code == 201
    assert first.json()["org_id"] == "org-rate"
    second = client.post("/runs", json={**BODY, "idempotency_key": "rate:2"}, headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"
def test_an_unknown_user_is_refused(make_db):
    db = _db(make_db, "unknown-user")
    client = TestClient(create_app(db))
    refused = client.post("/runs", json=BODY, headers={"X-User-Id": "missing"})
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "UNAUTHENTICATED"
def test_a_repair_writes_an_audit_row_and_leaves_the_status(make_db):
    db = _db(make_db, "repair")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(run_id, Outcome(kind="failed", error={"kind": "permanent", "message": "no"}, reason="permanent", dead_letter=True), allowed_from=frozenset({RunStatus.RUNNING})) is not None
    repaired = client.post(
        f"/runs/{run_id}/repair",
        json={"reason": "更正数量", "before": {"qty": 1}, "after": {"qty": 2}},
        headers=HEADERS,
    )
    assert repaired.status_code == 200
    assert repaired.json()["status"] == "failed"
    audits = [row for row in db.list_audit_logs(run_id) if row["action"] == "data.repair"]
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["reason"] == "更正数量"
    child = client.post(
        f"/runs/{run_id}/rerun",
        json={"idempotency_key": "repair:child"},
        headers=HEADERS,
    )
    assert child.status_code == 201
    assert child.json()["parent_run_id"] == run_id
    assert child.json()["status"] == "queued"
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "failed"
def test_a_stale_side_effect_becomes_response_lost(make_db):
    db = _db(make_db, "stale-side")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "stale-side")
    step = db.insert_step(run["id"], "tool", "send_email", {})
    claimed = db.claim_tool_call(
        run["id"], step["id"], tool_name="send_email", args={"to": "a@b.c"},
        idempotency_key="send_email:org-1:a@b.c:abc",
    )
    assert claimed is not None
    db.sweep_attention(tool_after=timedelta(seconds=-1))
    row = db.list_tool_calls(run["id"])[0]
    assert row["status"] == "response_lost"
    events = db.list_run_events("org-1", run["id"])
    assert any(event["event_type"] == "tool.manual_review" for event in events)
    assert db.list_tool_calls(run["id"])[0]["status"] == "response_lost"
_METRIC_NAMES = (
    "agent_run_created_total",
    "agent_run_completed_total",
    "agent_run_failed_total",
    "agent_run_duration_seconds",
    "agent_queue_pending_jobs",
    "agent_queue_lag_seconds",
    "agent_worker_active_runs",
    "agent_model_call_latency_seconds",
    "agent_model_error_total",
    "agent_model_internal_error_total",
    "agent_model_route_selected_total",
    "agent_model_route_fallback_total",
    "agent_tool_call_total",
    "agent_tool_error_total",
    "agent_side_effect_duplicate_prevented_total",
    "agent_sse_connection_count",
    "agent_stuck_runs",
    "agent_oldest_queued_age_seconds",
    "agent_estimated_cost_cents_total",
)
def test_the_monitor_lists_one_org_and_reads_the_ledger(make_db):
    db = _db(make_db, "monitor")
    client = TestClient(create_app(db))
    created = client.post("/runs", json=BODY, headers=HEADERS)
    assert created.status_code == 201
    run_id = created.json()["id"]

    listed = client.get("/runs", headers=HEADERS)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [run_id]
    assert client.get("/runs", params={"status": "succeeded"}, headers=HEADERS).json() == []
    assert client.get("/runs", params={"user_id": "user-1"}, headers=HEADERS).json()[0]["id"] == run_id
    assert client.get("/runs", headers={"X-User-Id": "user-2"}).json() == []
    unknown = client.get("/runs", params={"status": "nope"}, headers=HEADERS)
    assert unknown.status_code == 422

    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS)
    assert trace.status_code == 200
    assert trace.json()["outbox"][0]["job_type"] == "execute_run"
    assert trace.json()["outbox"][0]["payload_json"]["run_id"] == run_id
    assert trace.json()["dead_letters"] == []

    text = client.get("/metrics", headers=HEADERS)
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    for name in _METRIC_NAMES:
        assert f"# TYPE {name} " in text.text
    assert "agent_run_created_total 1" in text.text

    scrape = client.get("/metrics/scrape")
    assert scrape.status_code == 200
    assert 'agent_run_created_total{org_id="org-1"} 1' in scrape.text
    assert 'agent_run_created_total{org_id="org-2"} 0' in scrape.text
    assert "agent_queue_pending_jobs{" not in scrape.text
    assert "\nagent_queue_pending_jobs " in scrape.text

    board = client.get("/monitor", headers=HEADERS)
    assert board.status_code == 200
    assert board.json()["switches"]["queue"] is True
    assert board.json()["metrics"]["created"] == 1
    assert len(board.json()["charts"]["hours"]) == 24
    assert sum(point["created"] for point in board.json()["charts"]["hours"]) == 1
    assert board.json()["metrics"]["failed"] == 0
    assert all(item["signal"] != "run.failed 比例升高" for item in board.json()["alerts"])

    assert db.claim_chat_turn(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(run_id, Outcome(kind="failed", error={"kind": "permanent", "code": "COST_BUDGET_EXCEEDED", "message": "no"}, reason="permanent", dead_letter=True), allowed_from=frozenset({RunStatus.RUNNING})) is not None
    failed = client.get("/monitor", headers=HEADERS).json()
    assert failed["metrics"]["failed"] == 1
    assert any(item["signal"] == "run.failed 比例升高" for item in failed["alerts"])
    assert {item["id"] for item in failed["runbooks"]} == {
        "queue", "stuck", "side_effect", "model", "migration",
    }
