"""Create through the outbox, then run the loop in this process."""

from __future__ import annotations

from backend.llm.types import AssistantTurn, ToolCall
from backend.services.run_loop import Tool, ToolRegistry, run_in_this_process
from backend.services.run_names import RunType
from backend.services.run_worker import sweep_expired_leases


def _echo_tools():
    def echo(args):
        return {"echo": args["text"]}

    return ToolRegistry([Tool(
        name="echo", description="Echo the text.", fn=echo,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )])


def _callers(decide, answer):
    decide.provider, decide.model = "fake", "deepseek-flash"
    answer.provider, answer.model = "fake", "deepseek-chat"
    return decide, answer


def test_in_process_loop_consumes_the_outbox_and_writes_the_trace(make_db):
    db = make_db("in-process")
    db.upsert_distributor("org-1", "Org 1")
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

    row = run_in_this_process(
        db, "org-1", "user-1", RunType.CHAT, "chat:session-1:turn-1",
        {"message": "hello"}, _echo_tools(), *_callers(decide, answer),
    )
    assert row["status"] == "succeeded"
    assert row["output_json"]["text"] == "看到了 hi"
    assert db.find_pending_outbox_jobs() == []
    assert sweep_expired_leases(db) == 0

    trace = db.run_trace("org-1", row["id"])
    assert [(step["step_type"], step["name"]) for step in trace["steps"]] == [("tool", "echo"), ("model", "model")]
    assert trace["checkpoints"][-1]["state_json"]["last"] == "assistant:reply"
    assert [call["tool_name"] for call in trace["tool_calls"]] == ["echo"]
    usage = db.list_usage_records(row["id"])
    assert [
        (item["provider"], item["model"], item["prompt_tokens"], item["completion_tokens"])
        for item in usage
    ] == [
        ("fake", "deepseek-flash", 100, 200),
        ("fake", "deepseek-flash", 0, 0),
    ]


def test_in_process_permanent_error_fails_the_job(make_db):
    db = make_db("in-process-fail")
    db.upsert_distributor("org-1", "Org 1")

    def decide(context, schemas):
        return AssistantTurn(content="")

    def answer(context):
        return ""

    row = run_in_this_process(
        db, "org-1", "user-1", RunType.CHAT, "chat:session-1:fail-1",
        {"message": "hello"}, _echo_tools(), *_callers(decide, answer),
    )
    assert row["status"] == "failed"
    assert row["error_json"]["kind"] == "permanent"
    assert db.find_pending_outbox_jobs() == []