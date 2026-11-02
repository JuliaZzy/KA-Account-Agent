"""The exercise Run API stores rows in PostgreSQL, not in process memory."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.db import InvalidRunTransition, utcnow
from backend.main import create_app
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


def test_cancel_and_resume_append_the_next_sequence(make_db):
    db = _db(make_db, "run-status")
    client = TestClient(create_app(db))
    created = client.post("/runs", json=BODY, headers=HEADERS).json()
    run_id = created["id"]

    too_early = client.post(f"/runs/{run_id}/resume", headers=HEADERS)
    assert too_early.status_code == 409
    assert too_early.json()["error"]["code"] == "RUN_NOT_WAITING_FOR_USER"

    claimed = db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=10))
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
    events = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["events"]
    assert [e["event_type"] for e in events if e["event_type"] == "run.retry_scheduled"] == [
        "run.retry_scheduled", "run.retry_scheduled", "run.retry_scheduled",
    ]
    assert events[-1]["payload_json"]["reason"] == "dead_letter"

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
    assert db.claim_run(waiting_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    paused = db.transition_run("org-1", waiting_id, RunStatus.WAITING_FOR_USER, {})
    assert paused is not None and paused["lease_owner"] is None
    reject(
        waiting_id,
        RunStatus.SUCCEEDED,
        RunStatus.WAITING_FOR_USER,
        ["run.created", "run.started", "run.waiting_for_user"],
    )

    succeeded_id = open_run(3)
    assert db.claim_run(succeeded_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.complete_run(succeeded_id, {"answer": "ok"})["status"] == "succeeded"
    reject(
        succeeded_id,
        RunStatus.RUNNING,
        RunStatus.SUCCEEDED,
        ["run.created", "run.started", "run.completed"],
    )

    failed_id = open_run(4)
    assert db.claim_run(failed_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
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

def test_loop_runs_a_tool_then_stores_the_answer(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    def echo(args):
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(
            content="先看一下", 
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})],
            prompt_tokens=100,
            completion_tokens=200,
        ),
        AssistantTurn(content="看到了 hi"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "succeeded"
    assert body["output_json"]["text"] == "看到了 hi"
    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()
    assert [(step["step_type"], step["name"]) for step in trace["steps"]] == [("tool", "echo"), ("model", "model")]
    assert trace["checkpoints"][-1]["state_json"]["last"] == "assistant:reply"
    assert [call["tool_name"] for call in trace["tool_calls"]] == ["echo"]
    usage = db.list_usage_records(run_id)
    assert [
        (
            row["provider"], row["model"], row["prompt_tokens"], row["completion_tokens"],
            row["total_tokens"], row["estimated_cost_cents"],
        )
        for row in usage
    ] == [
        ("fake", "deepseek-flash", 100, 200, 300, 0),
        ("fake", "deepseek-flash", 0, 0, 0, 0),
    ]
    assert usage[0]["org_id"] == "org-1"
    assert usage[0]["user_id"] == "user-1"


def test_loop_waits_for_approval_and_resumes_the_same_tool(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, requires_approval=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(content="先等确认", tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        AssistantTurn(content="确认后继续"),
    ]
    seen: list[list] = []

    def decide(context, schemas):
        seen.append(list(context))
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop-wait")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    execute = execute_run(db, tools, decide=decide, answer=answer)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    paused = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert paused["status"] == "waiting_for_user"
    assert paused["lease_owner"] is None
    assert called == []
    assert db.find_pending_outbox_jobs() == []
    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()
    assert trace["steps"][0]["status"] == "running"
    assert trace["checkpoints"][-1]["state_json"]["last"] == "assistant:tool_use:echo"
    assert trace["checkpoints"][-1]["state_json"]["open_step_id"] == trace["steps"][0]["id"]

    resumed = client.post(f"/runs/{run_id}/resume", json={"input": {"message": "同意"}}, headers=HEADERS)
    assert resumed.status_code == 200
    dispatch_pending(db)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    assert called == [{"text": "hi"}]
    finished = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert finished["status"] == "succeeded"
    assert finished["output_json"]["text"] == "确认后继续"
    assert any(message.get("content") == "同意" for message in seen[-1])

def test_skip_resumes_without_running_the_tool(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, requires_approval=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(content="先等确认", tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        AssistantTurn(content="已跳过"),
    ]

    def decide(context, schemas):
        del context, schemas
        return script.pop(0)

    def answer(context):
        del context
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop-skip")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    execute = execute_run(db, tools, decide=decide, answer=answer)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "waiting_for_user"

    resumed = client.post(
        f"/runs/{run_id}/resume",
        json={"input": {"message": "跳过", "decision": "skip"}},
        headers=HEADERS,
    )
    assert resumed.status_code == 200
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    assert called == []
    finished = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert finished["status"] == "succeeded"
    assert finished["output_json"]["text"] == "已跳过"
    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()
    assert trace["tool_calls"][0]["result_json"]["skipped"] is True

def test_an_approved_tool_replays_a_stored_result(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, requires_approval=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(content="先等确认", tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        AssistantTurn(content="确认后继续"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop-replay")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    execute = execute_run(db, tools, decide=decide, answer=answer)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    step_id = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["steps"][0]["id"]
    claimed = db.claim_tool_call(
        run_id, step_id, tool_name="echo", args={"text": "hi"}, idempotency_key="echo:call_1",
    )
    assert claimed is not None and claimed[1] is True
    assert claimed[0]["org_id"] == "org-1"
    assert claimed[0]["user_id"] == "user-1"
    assert claimed[0]["started_at"] is not None
    assert db.complete_tool_call(
        claimed[0]["id"], result={"echo": "stored"}, status=ToolCallStatus.SUCCEEDED, latency_ms=5,
    ) is not None

    resumed = client.post(f"/runs/{run_id}/resume", json={"input": {"message": "同意"}}, headers=HEADERS)
    assert resumed.status_code == 200
    dispatch_pending(db)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    assert called == []
    finished = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert finished["status"] == "succeeded"
    assert finished["output_json"]["text"] == "确认后继续"


def test_an_approved_tool_does_not_run_again_after_a_claim(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, requires_approval=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(content="先等确认", tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        AssistantTurn(content="不会走到这里"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop-claimed")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    execute = execute_run(db, tools, decide=decide, answer=answer)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    step_id = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["steps"][0]["id"]
    claimed = db.claim_tool_call(
        run_id, step_id, tool_name="echo", args={"text": "hi"}, idempotency_key="echo:call_1",
    )
    assert claimed is not None and claimed[1] is True

    resumed = client.post(f"/runs/{run_id}/resume", json={"input": {"message": "同意"}}, headers=HEADERS)
    assert resumed.status_code == 200
    dispatch_pending(db)
    assert work_once(db, "worker-a", execute, run_id=run_id) is True
    assert called == []
    finished = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert finished["status"] == "succeeded"
    assert finished["output_json"]["text"] == "不会走到这里"
    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()
    assert trace["tool_calls"][0]["status"] == "response_lost"
    assert trace["tool_calls"][0]["error_json"]["code"] == "TOOL_UPSTREAM_ERROR"

def test_a_tool_sees_the_assistant_text_from_this_run(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once
    from backend.tools.lang_translate import resolve_text

    seen: list[dict] = []

    def grab(args, context=None):
        seen.append(context or {})
        return {"ok": True}

    tools = ToolRegistry([Tool(name="grab", description="Read the context.", fn=grab)])
    script = [
        AssistantTurn(
            content="库存还有 12 箱",
            tool_calls=[ToolCall(id="call_1", name="grab", arguments={"source": "last_assistant"})],
        ),
        AssistantTurn(content="记下来了"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop-context")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert resolve_text({"source": "last_assistant"}, seen[0]) == "库存还有 12 箱"

def test_translate_reads_the_report_stored_on_the_checkpoint(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once
    from backend.tools.lang_translate import resolve_text

    seen: list[dict] = []

    def grab(args, context=None):
        seen.append(context or {})
        return {"ok": True}

    tools = ToolRegistry([Tool(name="grab", description="Read the context.", fn=grab)])
    script = [
        AssistantTurn(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="grab", arguments={"source": "report"})],
        ),
        AssistantTurn(content="译好了"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "loop-report")
    client = TestClient(create_app(db))
    created = client.post(
        "/runs",
        json={
            **BODY,
            "idempotency_key": "report:translate",
            "input": {**BODY["input"], "last_report": "本月销量持平。"},
        },
        headers=HEADERS,
    )
    run_id = created.json()["id"]
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert resolve_text({"source": "report"}, seen[0]) == "本月销量持平。"
    trace = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()
    assert trace["checkpoints"][-1]["state_json"]["last_report"] == "本月销量持平。"

def test_bind_uses_the_injected_client(monkeypatch):
    import json
    from types import SimpleNamespace

    from backend.llm.types import ToolCall
    from backend.orchestrator.prompt import ANSWER, REACT
    from backend.services.run_models import bind

    seen: dict[str, dict] = {}

    def _completion(content: str | None, tool_calls: list[ToolCall]):
        message = SimpleNamespace(
            content=content,
            tool_calls=[
                SimpleNamespace(
                    id=call.id,
                    function=SimpleNamespace(name=call.name, arguments=json.dumps(call.arguments)),
                )
                for call in tool_calls
            ],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class Client:
        provider = "openai"
        model = "gpt-4.1"

        def chat_completion(self, *, model, messages, tools=None, tool_choice=None):
            system = messages[0]["content"]
            body = messages[1:]
            if tools:
                seen["decide"] = {"system": system, "messages": body, "tools": tools, "model": model}
                self.last_usage = {"prompt_tokens": 8, "completion_tokens": 2}
                return _completion(None, [ToolCall(id="c1", name="echo", arguments={"text": "hi"})])
            seen["answer"] = {"system": system, "messages": body, "model": model}
            self.last_usage = {"prompt_tokens": 1, "completion_tokens": 3}
            return _completion("好的", [])

    context = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "先查"},
            {"type": "tool_use", "id": "call_1", "name": "echo", "input": {"text": "hi"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": {"echo": "hi"}},
        ]},
    ]
    schemas = [{"type": "function", "function": {"name": "echo"}}]
    decide, answer = bind(Client())
    turn = decide(context, schemas)
    assert turn.tool_calls[0].name == "echo"
    assert turn.prompt_tokens == 8
    assert turn.completion_tokens == 2
    assert seen["decide"]["system"] == REACT
    assert seen["decide"]["tools"] is schemas
    assert seen["decide"]["messages"][1]["tool_calls"][0]["function"] == {
        "name": "echo",
        "arguments": json.dumps({"text": "hi"}, ensure_ascii=False),
    }
    assert decide.provider == "openai"
    assert decide.model == "gpt-4.1"

    assert answer(context) == "好的"
    assert answer.prompt_tokens == 1
    assert answer.completion_tokens == 3
    assert seen["answer"]["system"] == ANSWER
    assert seen["answer"]["messages"] == [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "先查"},
        {"role": "user", "content": json.dumps({"echo": "hi"}, ensure_ascii=False)},
    ]
    assert answer.provider == "openai"
    assert answer.model == "gpt-4.1"

    qwen = Client()
    qwen.provider, qwen.model = "sjtu", "qwen"
    _, qwen_answer = bind(qwen, answer_system="用中文回答")
    assert qwen_answer.model == "qwen"
    assert qwen_answer([]) == "好的"
    assert seen["answer"]["system"] == "用中文回答"

def test_create_run_accepts_only_the_five_types(make_db):
    db = _db(make_db, "run-types")
    client = TestClient(create_app(db))
    for run_type in ("po", "contract", "report", "data", "code", "brand", "fix", "reflect", "review", "revise"):
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
        assert db.claim_run(run_id, "worker-a", utcnow() + lease_for) is not None
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
    assert db.claim_run(acked_id, "worker-b", utcnow() + timedelta(minutes=5)) is not None


def test_an_expired_lease_cancels_a_run_that_already_asked(make_db):
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_worker import sweep_expired_leases

    db = _db(make_db, "lease-cancel")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:cancel"}, headers=HEADERS,
    ).json()["id"]
    dispatch_pending(db)
    assert db.claim_run(run_id, "worker-a", utcnow() - timedelta(seconds=5)) is not None
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
        updated = db.mark_outbox_retry(outbox_id, "publish failed")
        assert updated is not None
        assert updated["status"] == "pending"

    dead = db.mark_outbox_retry(outbox_id, "publish failed")
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "failed"
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["attempts"] == 1
    events = client.get(f"/runs/{run_id}/events", headers=HEADERS).json()
    assert [event["event_type"] for event in events] == ["run.created", "run.started", "run.failed"]
    assert events[-1]["payload_json"]["reason"] == "dead_letter"

    letters = db.list_dead_letter_jobs(outbox_id)
    assert len(letters) == 1
    assert letters[0]["job_type"] == "execute_run"
    assert letters[0]["payload_json"]["run_id"] == run_id
    assert letters[0]["payload_json"]["trace_id"]
    assert letters[0]["attempts"] == 11
    assert letters[0]["error_json"] == {"kind": "dead_letter", "message": "publish failed"}
    assert db.mark_outbox_retry(outbox_id, "publish failed") is None

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
    assert opened[0]["payload_json"]["display"] == "已开始"
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
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=30)) is not None
    held: list[bytes] = []

    def read() -> None:
        with client.stream("GET", f"/runs/{run_id}/events/stream", headers=HEADERS) as response:
            held.append(response.read())

    thread = threading.Thread(target=read)
    thread.start()
    time.sleep(0.3)
    assert db.complete_run(run_id, {"text": "ok"})["status"] == "succeeded"
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
        schemas = [
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


def test_execute_tool_claims_before_the_handler_runs(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    db = _db(make_db, "tool-claim-first")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    seen: list[str] = []

    def echo(args):
        seen.append(db.list_tool_calls(run_id)[0]["status"])
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(content="先占座", tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        AssistantTurn(content="看到了"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert seen == ["running"]
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "succeeded"
    audits = [row for row in db.list_audit_logs(run_id) if row["target_type"] == "tool_call"]
    assert audits == []


def test_a_tool_error_stays_in_the_loop(make_db, caplog):
    import logging

    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    caplog.set_level(logging.INFO, logger="backend.services.run_loop")

    def echo(args):
        raise RuntimeError("smtp down")

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(
            content="试一下",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "secret-arg"})],
        ),
        AssistantTurn(content="没发出去"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "tool-error")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "succeeded"
    assert body["output_json"]["text"] == "没发出去"
    call = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["tool_calls"][0]
    assert call["status"] == "failed"
    assert call["error_json"]["code"] == "TOOL_UPSTREAM_ERROR"
    assert call["error_json"]["retryable"] is False
    assert "secret-arg" not in caplog.text
    assert "TOOL_UPSTREAM_ERROR" in caplog.text
    assert "executed=True" in caplog.text


def test_a_side_effect_audits_the_tool_call(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    def echo(args):
        return {"ok": True}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, side_effect=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])
    script = [
        AssistantTurn(content="发一下", tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        AssistantTurn(content="发过了"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "tool-audit")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    audits = [row for row in db.list_audit_logs(run_id) if row["target_type"] == "tool_call"]
    assert len(audits) == 1
    assert audits[0]["action"] == "echo"
    assert audits[0]["org_id"] == "org-1"
    assert audits[0]["user_id"] == "user-1"
    assert audits[0]["metadata_json"] == {"status": "succeeded", "error_code": ""}


def test_tool_budget_does_not_call_the_handler(make_db):
    from datetime import timedelta

    from backend.services.run_loop import Tool, ToolRegistry, execute_tool
    from backend.services.run_worker import PermanentRunError

    db = _db(make_db, "tool-budget")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=30)) is not None
    run = db.find_agent_run_by_id(run_id)
    assert run is not None
    run["max_tool_calls"] = 0
    step = db.insert_step(run_id, "tool", "echo", {})
    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"ok": True}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo,
        parameters={"type": "object", "properties": {}},
    )])
    with pytest.raises(PermanentRunError):
        execute_tool(
            db, run, tools, step_id=step["id"], tool_name="echo", args={"text": "secret-arg"},
            tool_use_id="call_1", context=[], completed=[],
        )
    assert called == []
    assert db.count_tool_calls(run_id) == 0


def test_invalid_arguments_do_not_call_or_claim(make_db):
    from backend.llm.types import AssistantTurn, ToolCall
    from backend.services.dispatcher import dispatch_pending
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"ok": True}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, side_effect=True, idempotency_required=True,
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )])
    script = [
        AssistantTurn(content="缺参数", tool_calls=[ToolCall(id="call_1", name="echo", arguments={})]),
        AssistantTurn(content="参数不对"),
    ]

    def decide(context, schemas):
        return script.pop(0)

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "tool-args")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    dispatch_pending(db)
    assert work_once(
        db, "worker-a", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert called == []
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "succeeded"
    assert body["output_json"]["text"] == "参数不对"
    call = client.get(f"/runs/{run_id}/trace", headers=HEADERS).json()["tool_calls"][0]
    assert call["status"] == "failed"
    assert call["idempotency_key"] == "echo:call_1"
    assert call["error_json"]["code"] == "TOOL_VALIDATION_ERROR"
    assert call["error_json"]["retryable"] is False
    events = db.list_run_events("org-1", run_id)
    assert events is not None
    assert "tool.started" not in [event["event_type"] for event in events]


def test_argument_schema_checks_type_enum_and_extra_fields(make_db):
    from datetime import timedelta

    from backend.services.run_loop import Tool, ToolRegistry, execute_tool

    db = _db(make_db, "tool-schema")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=30)) is not None
    run = db.find_agent_run_by_id(run_id)
    assert run is not None
    called: list[dict] = []

    def echo(args):
        called.append(dict(args))
        return {"ok": True}

    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["text"],
        "additionalProperties": False,
    }
    tools = ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo, side_effect=True,
        parameters=parameters,
    )])
    cases = [
        ("call_1", {}),
        ("call_2", {"text": 1}),
        ("call_3", {"text": "hi", "kind": "nope"}),
        ("call_4", {"text": "hi", "kind": "a", "extra": 1}),
        ("call_5", {"text": "hi", "kind": "a"}),
    ]
    for tool_use_id, args in cases:
        step = db.insert_step(run_id, "tool", "echo", {})
        assert execute_tool(
            db, run, tools, step_id=step["id"], tool_name="echo", args=args,
            tool_use_id=tool_use_id, context=[], completed=[],
        ) is not None
    assert called == [{"text": "hi", "kind": "a"}]
    rows = db.list_tool_calls(run_id)
    assert [row["status"] for row in rows] == ["failed", "failed", "failed", "failed", "succeeded"]
    assert [row["idempotency_key"] for row in rows] == [f"echo:{name}" for name, _args in cases]
    assert [row["error_json"]["code"] for row in rows[:4]] == ["TOOL_VALIDATION_ERROR"] * 4
    assert "nope" not in rows[2]["error_json"]["message"]
    events = db.list_run_events("org-1", run_id)
    assert events is not None
    assert sum(event["event_type"] == "tool.started" for event in events) == 1


def _leased_run(db, client, key: str):
    from datetime import timedelta

    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": key}, headers=HEADERS,
    ).json()["id"]
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=60)) is not None
    run = db.find_agent_run_by_id(run_id)
    assert run is not None
    return run


def test_send_email_reuses_the_business_key_across_runs(make_db, monkeypatch):
    from backend.services.run_loop import Tool, ToolRegistry, execute_tool
    from backend.tools.send_email import SCHEMA, run as email_run

    monkeypatch.setenv("EMAIL_DRY_RUN", "1")
    sent: list[dict] = []
    real_run = email_run

    def spy(arguments, **kwargs):
        sent.append(dict(arguments))
        return real_run(arguments, **kwargs)

    monkeypatch.setattr("backend.tools.send_email.run", spy)
    db = _db(make_db, "mail-key")
    client = TestClient(create_app(db))
    tools = ToolRegistry([Tool(
        name="send_email",
        description=SCHEMA["function"]["description"],
        fn=spy,
        parameters=SCHEMA["function"]["parameters"],
        requires_approval=True,
        side_effect=True,
        idempotency_required=True,
    )])
    message = {"to": "buyer@example.com", "subject": "Quote", "body": "10 cases", "cc": "ops@example.com"}

    def call(run, tool_use_id, args):
        step = db.insert_step(run["id"], "tool", "send_email", {})
        moved = execute_tool(
            db, run, tools, step_id=step["id"], tool_name="send_email", args=args,
            tool_use_id=tool_use_id, context=[], completed=[],
        )
        assert moved is not None
        return moved

    first = _leased_run(db, client, "mail:first")
    call(first, "call_1", message)
    assert len(sent) == 1
    row = db.list_tool_calls(first["id"])[0]
    assert row["status"] == "succeeded"
    assert row["idempotency_key"].startswith("send_email:org-1:buyer@example.com:")
    assert "call_1" not in row["idempotency_key"]
    assert "ops@example.com" not in row["idempotency_key"]

    second = _leased_run(db, client, "mail:second")
    moved = call(second, "call_2", {**message, "cc": "other@example.com"})
    assert len(sent) == 1
    visible = moved[0][-1]["content"][0]["content"]
    assert visible["skipped_duplicate"] is True
    assert visible["result"]["ok"] is True
    assert db.list_tool_calls(second["id"]) == []
    assert db.list_tool_calls(first["id"])[0]["status"] == "succeeded"

    call(second, "call_3", {**message, "body": "11 cases"})
    assert len(sent) == 2

    bad = call(first, "call_bad", {"to": "not-an-email", "subject": "Quote", "body": "10 cases"})
    assert len(sent) == 2
    refused = [row for row in db.list_tool_calls(first["id"]) if row["idempotency_key"] == "send_email:call_bad"]
    assert len(refused) == 1
    assert refused[0]["error_json"]["code"] == "TOOL_VALIDATION_ERROR"
    assert bad is not None


def test_smtp_failure_stays_on_the_business_key(make_db, monkeypatch):
    import smtplib

    from backend.services.run_loop import Tool, ToolRegistry, execute_tool
    from backend.tools.send_email import SCHEMA, run as email_run

    monkeypatch.delenv("EMAIL_DRY_RUN", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "desk@example.com")
    calls: list[int] = []

    def boom(**_kwargs):
        calls.append(1)
        raise smtplib.SMTPServerDisconnected("lost")

    monkeypatch.setattr("backend.tools.send_email.send", boom)
    db = _db(make_db, "mail-smtp")
    client = TestClient(create_app(db))
    tools = ToolRegistry([Tool(
        name="send_email",
        description=SCHEMA["function"]["description"],
        fn=email_run,
        parameters=SCHEMA["function"]["parameters"],
        side_effect=True,
        idempotency_required=True,
    )])
    args = {"to": "buyer@example.com", "subject": "Quote", "body": "10 cases"}
    first = _leased_run(db, client, "smtp:first")
    step = db.insert_step(first["id"], "tool", "send_email", {})
    assert execute_tool(
        db, first, tools, step_id=step["id"], tool_name="send_email", args=args,
        tool_use_id="call_1", context=[], completed=[],
    ) is not None
    row = db.list_tool_calls(first["id"])[0]
    assert row["status"] == "failed"
    assert row["error_json"]["code"] == "TOOL_UPSTREAM_ERROR"
    assert row["error_json"]["message"] == "lost"
    assert row["idempotency_key"].startswith("send_email:org-1:buyer@example.com:")

    second = _leased_run(db, client, "smtp:second")
    step = db.insert_step(second["id"], "tool", "send_email", {})
    moved = execute_tool(
        db, second, tools, step_id=step["id"], tool_name="send_email", args=args,
        tool_use_id="call_2", context=[], completed=[],
    )
    assert moved is not None
    assert moved[0][-1]["content"][0]["content"]["skipped_duplicate"] is True
    assert calls == [1]
    assert db.list_tool_calls(second["id"]) == []


def test_mcp_business_key_follows_the_arguments(make_db):
    from backend.services.run_loop import Tool, ToolRegistry, execute_tool

    calls: list[dict] = []

    def crawl(args):
        calls.append(dict(args))
        return {"ok": True}

    db = _db(make_db, "mcp-key")
    client = TestClient(create_app(db))
    tools = ToolRegistry([Tool(
        name="socialcrawl_request", description="Crawl.", fn=crawl,
        parameters={"type": "object", "properties": {}},
        side_effect=True, idempotency_required=True,
    )])
    first_args = {"b": 1, "a": "fish"}
    second_args = {"a": "fish", "b": 1}
    run = _leased_run(db, client, "mcp:first")
    step = db.insert_step(run["id"], "tool", "socialcrawl_request", {})
    assert execute_tool(
        db, run, tools, step_id=step["id"], tool_name="socialcrawl_request", args=first_args,
        tool_use_id="call_1", context=[], completed=[],
    ) is not None
    row = db.list_tool_calls(run["id"])[0]
    assert row["idempotency_key"].startswith("mcp:org-1:socialcrawl_request:")
    assert "call_1" not in row["idempotency_key"]

    other = _leased_run(db, client, "mcp:second")
    step = db.insert_step(other["id"], "tool", "socialcrawl_request", {})
    moved = execute_tool(
        db, other, tools, step_id=step["id"], tool_name="socialcrawl_request", args=second_args,
        tool_use_id="call_2", context=[], completed=[],
    )
    assert moved is not None
    assert moved[0][-1]["content"][0]["content"]["skipped_duplicate"] is True
    assert calls == [first_args]
    assert db.list_tool_calls(other["id"]) == []


def _slow_tool(fn, *, timeout_seconds, retryable_errors=frozenset(), side_effect=False, idempotency_required=False):
    from backend.services.run_loop import Tool, ToolRegistry

    return ToolRegistry([Tool(
        name="echo", description="Echo.", fn=fn,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        side_effect=side_effect, idempotency_required=idempotency_required,
        timeout_seconds=timeout_seconds, retryable_errors=retryable_errors,
    )])


def test_a_read_tool_retries_a_timeout_then_succeeds(make_db):
    import time

    from backend.services.run_loop import execute_tool
    from backend.services.run_names import ToolErrorCode

    calls: list[int] = []

    def echo(args):
        calls.append(1)
        if len(calls) < 2:
            time.sleep(1.2)
        return {"ok": True, "text": args["text"]}

    db = _db(make_db, "tool-timeout-retry")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "tool-timeout-retry")
    step = db.insert_step(run["id"], "tool", "echo", {})
    tools = _slow_tool(echo, timeout_seconds=1, retryable_errors=frozenset({ToolErrorCode.TIMEOUT}))
    done = execute_tool(
        db, run, tools, step_id=step["id"], tool_name="echo", args={"text": "hi"},
        tool_use_id="call_1", context=[], completed=[],
    )
    assert done is not None
    assert calls == [1, 1]
    row = db.list_tool_calls(run["id"])[0]
    assert row["status"] == "succeeded"
    assert row["result_json"]["ok"] is True
    events = db.list_run_events("org-1", run["id"])
    assert [event["event_type"] for event in events if event["event_type"] == "tool.manual_review"] == []


def test_a_read_tool_stops_after_two_retries(make_db):
    import time

    from backend.services.run_loop import execute_tool
    from backend.services.run_names import ToolErrorCode

    calls: list[int] = []

    def echo(args):
        calls.append(1)
        time.sleep(1.2)
        return {"ok": True}

    db = _db(make_db, "tool-timeout-give-up")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "tool-timeout-give-up")
    step = db.insert_step(run["id"], "tool", "echo", {})
    tools = _slow_tool(echo, timeout_seconds=1, retryable_errors=frozenset({ToolErrorCode.TIMEOUT}))
    done = execute_tool(
        db, run, tools, step_id=step["id"], tool_name="echo", args={"text": "hi"},
        tool_use_id="call_1", context=[], completed=[],
    )
    assert done is not None
    assert calls == [1, 1, 1]
    row = db.list_tool_calls(run["id"])[0]
    assert row["status"] == "failed"
    assert row["error_json"]["code"] == "TOOL_TIMEOUT"
    assert row["error_json"]["retryable"] is True
    assert db.count_tool_calls(run["id"]) == 1


def test_a_read_tool_without_a_retryable_timeout_fails_once(make_db):
    import time

    from backend.services.run_loop import execute_tool

    calls: list[int] = []

    def echo(args):
        calls.append(1)
        time.sleep(1.2)
        return {"ok": True}

    db = _db(make_db, "tool-timeout-once")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "tool-timeout-once")
    step = db.insert_step(run["id"], "tool", "echo", {})
    tools = _slow_tool(echo, timeout_seconds=1)
    done = execute_tool(
        db, run, tools, step_id=step["id"], tool_name="echo", args={"text": "hi"},
        tool_use_id="call_1", context=[], completed=[],
    )
    assert done is not None
    assert calls == [1]
    row = db.list_tool_calls(run["id"])[0]
    assert row["status"] == "failed"
    assert row["error_json"]["code"] == "TOOL_TIMEOUT"
    assert row["error_json"]["retryable"] is False
    events = db.list_run_events("org-1", run["id"])
    assert [event["event_type"] for event in events if event["event_type"] == "tool.manual_review"] == []


def test_an_upstream_error_is_retried_on_the_same_seat(make_db):
    from backend.services.run_loop import execute_tool
    from backend.services.run_names import ToolErrorCode

    calls: list[int] = []

    def echo(args):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("upstream down")
        return {"ok": True}

    db = _db(make_db, "tool-upstream-retry")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "tool-upstream-retry")
    step = db.insert_step(run["id"], "tool", "echo", {})
    tools = _slow_tool(echo, timeout_seconds=30, retryable_errors=frozenset({ToolErrorCode.UPSTREAM}))
    done = execute_tool(
        db, run, tools, step_id=step["id"], tool_name="echo", args={"text": "hi"},
        tool_use_id="call_1", context=[], completed=[],
    )
    assert done is not None
    assert calls == [1, 1, 1]
    row = db.list_tool_calls(run["id"])[0]
    assert row["status"] == "succeeded"
    assert db.count_tool_calls(run["id"]) == 1


def test_a_side_effect_timeout_is_response_lost(make_db):
    import time

    from backend.services.run_loop import execute_tool

    calls: list[int] = []

    def echo(args):
        calls.append(1)
        time.sleep(1.2)
        return {"ok": True}

    db = _db(make_db, "tool-side-timeout")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "tool-side-timeout")
    step = db.insert_step(run["id"], "tool", "echo", {})
    tools = _slow_tool(echo, timeout_seconds=1, side_effect=True, idempotency_required=True)
    done = execute_tool(
        db, run, tools, step_id=step["id"], tool_name="echo", args={"text": "hi"},
        tool_use_id="call_1", context=[], completed=[],
    )
    assert done is not None
    assert calls == [1]
    row = db.list_tool_calls(run["id"])[0]
    assert row["status"] == "response_lost"
    assert row["error_json"]["code"] == "TOOL_TIMEOUT"
    events = db.list_run_events("org-1", run["id"])
    review = [event for event in events if event["event_type"] == "tool.manual_review"]
    assert len(review) == 1
    assert review[0]["payload_json"]["display"] == "结果不确定，需要人确认"
    audits = [item for item in db.list_audit_logs(run["id"]) if item["target_type"] == "tool_call"]
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["status"] == "response_lost"

    other = _leased_run(db, client, "tool-side-timeout-again")
    step = db.insert_step(other["id"], "tool", "echo", {})
    again = execute_tool(
        db, other, tools, step_id=step["id"], tool_name="echo", args={"text": "hi"},
        tool_use_id="call_2", context=[], completed=[],
    )
    assert again is not None
    assert again[0][-1]["content"][0]["content"]["skipped_duplicate"] is True
    assert calls == [1]
    assert db.list_tool_calls(other["id"]) == []


def test_a_worker_continues_from_a_tool_result_without_calling_it(make_db):
    from backend.llm.types import AssistantTurn
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[int] = []

    def echo(args):
        called.append(1)
        return {"echo": args["text"]}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo.", fn=echo,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])

    def decide(context, schemas):
        return AssistantTurn(content="接着说")

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "recover-result")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "recover-result"}, headers=HEADERS,
    ).json()["id"]
    db.append_checkpoint(run_id, {
        "context": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "echo", "input": {"text": "hi"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": {"echo": "hi"}},
            ]},
        ],
        "completed_tool_calls": ["call_1"],
        "last": "tool_result:echo",
    })
    assert work_once(
        db, "worker-b", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert called == []
    finished = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert finished["output_json"]["text"] == "接着说"


def test_a_left_running_side_effect_is_not_called_again(make_db):
    from backend.llm.types import AssistantTurn
    from backend.services.run_loop import Tool, ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    called: list[int] = []

    def echo(args):
        called.append(1)
        return {"ok": True}

    tools = ToolRegistry([Tool(
        name="echo", description="Echo.", fn=echo, side_effect=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])

    def decide(context, schemas):
        return AssistantTurn(content="需要人看")

    def answer(context):
        raise AssertionError("answer must not run")

    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    db = _db(make_db, "recover-running")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "recover-running"}, headers=HEADERS,
    ).json()["id"]
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=-5)) is not None
    step = db.insert_step(run_id, "tool", "echo", {})
    claimed = db.claim_tool_call(
        run_id, step["id"], tool_name="echo", args={"text": "hi"}, idempotency_key="echo:call_1",
    )
    assert claimed is not None
    db.append_checkpoint(run_id, {
        "context": [
            {"role": "user", "content": "发一下"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "echo", "input": {"text": "hi"}},
            ]},
        ],
        "completed_tool_calls": [],
        "last": "assistant:tool_use:echo",
        "open_step_id": step["id"],
    })
    from backend.services.run_worker import sweep_expired_leases
    assert sweep_expired_leases(db) == 1
    assert work_once(
        db, "worker-b", execute_run(db, tools, decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert called == []
    row = db.list_tool_calls(run_id)[0]
    assert row["status"] == "response_lost"
    events = db.list_run_events("org-1", run_id)
    assert any(event["event_type"] == "tool.manual_review" for event in events)


def test_three_expired_leases_fail_the_run(make_db):
    from backend.services.run_worker import sweep_expired_leases

    db = _db(make_db, "lease-budget")
    client = TestClient(create_app(db))
    run_id = client.post(
        "/runs", json={**BODY, "idempotency_key": "lease:budget"}, headers=HEADERS,
    ).json()["id"]
    for _ in range(3):
        assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(seconds=-5)) is not None
        sweep_expired_leases(db)
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert body["status"] == "failed"
    assert body["attempts"] == 3


def test_a_terminal_run_is_not_executed_again(make_db):
    from backend.services.run_loop import ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    def decide(context, schemas):
        raise AssertionError("decide must not run")

    def answer(context):
        raise AssertionError("answer must not run")

    db = _db(make_db, "terminal-skip")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.complete_run(run_id, {"text": "done"}) is not None
    assert work_once(
        db, "worker-b", execute_run(db, ToolRegistry([]), decide=decide, answer=answer), run_id=run_id,
    ) is True
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "succeeded"


def test_a_run_past_its_duration_fails_without_the_model(make_db):
    from backend.services.run_loop import ToolRegistry, execute_run
    from backend.services.run_worker import work_once

    def decide(context, schemas):
        raise AssertionError("decide must not run")

    def answer(context):
        raise AssertionError("answer must not run")

    db = _db(make_db, "duration")
    run, _created = db.create_agent_run(
        "org-1", "user-1", "po", "duration", {"message": "hi"}, max_duration_seconds=0,
    )
    assert work_once(
        db, "worker-a", execute_run(db, ToolRegistry([]), decide=decide, answer=answer), run_id=run["id"],
    ) is True
    saved = db.find_agent_run_by_id(run["id"])
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["error_json"]["code"] == "COST_BUDGET_EXCEEDED"


def test_the_queue_switch_does_not_claim(make_db, monkeypatch):
    from backend.services.run_worker import work_once

    monkeypatch.setenv("QUEUE_ENABLED", "0")

    def execute(run):
        raise AssertionError("execute must not run")

    db = _db(make_db, "queue-off")
    client = TestClient(create_app(db))
    run_id = client.post("/runs", json=BODY, headers=HEADERS).json()["id"]
    assert work_once(db, "worker-a", execute, run_id=run_id) is False
    assert client.get(f"/runs/{run_id}", headers=HEADERS).json()["status"] == "queued"


def test_the_send_email_switch_waits_instead_of_sending(make_db, monkeypatch):
    from backend.services.run_loop import Tool, ToolRegistry, execute_tool

    monkeypatch.setenv("SEND_EMAIL_ENABLED", "0")
    called: list[int] = []

    def send(args):
        called.append(1)
        return {"ok": True}

    db = _db(make_db, "mail-switch")
    client = TestClient(create_app(db))
    run = _leased_run(db, client, "mail-switch")
    step = db.insert_step(run["id"], "tool", "send_email", {})
    tools = ToolRegistry([Tool(
        name="send_email", description="Send.", fn=send, side_effect=True, idempotency_required=True,
        parameters={"type": "object", "properties": {}},
    )])
    assert execute_tool(
        db, run, tools, step_id=step["id"], tool_name="send_email", args={},
        tool_use_id="call_1", context=[], completed=[],
    ) is None
    assert called == []
    assert db.find_agent_run_by_id(run["id"])["status"] == "waiting_for_user"


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
    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.fail_run(run_id, {"kind": "permanent", "message": "no"}) is not None
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
    "agent_tool_call_total",
    "agent_tool_error_total",
    "agent_side_effect_duplicate_prevented_total",
    "agent_sse_connection_count",
    "agent_stuck_runs",
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

    board = client.get("/monitor", headers=HEADERS)
    assert board.status_code == 200
    assert board.json()["switches"]["queue"] is True
    assert board.json()["metrics"]["created"] == 1
    assert board.json()["metrics"]["failed"] == 0
    assert all(item["signal"] != "run.failed 比例升高" for item in board.json()["alerts"])

    assert db.claim_run(run_id, "worker-a", utcnow() + timedelta(minutes=5)) is not None
    assert db.fail_run(run_id, {"kind": "permanent", "code": "COST_BUDGET_EXCEEDED", "message": "no"}) is not None
    failed = client.get("/monitor", headers=HEADERS).json()
    assert failed["metrics"]["failed"] == 1
    assert any(item["signal"] == "run.failed 比例升高" for item in failed["alerts"])
    assert {item["id"] for item in failed["runbooks"]} == {
        "queue", "stuck", "side_effect", "model", "migration",
    }