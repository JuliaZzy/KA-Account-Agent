"""The desk no longer sends the dropdown id. Each step names its own model."""

from datetime import timedelta

import pytest

from backend.db import utcnow
from backend.llm.calls import (
    DEEPSEEK_FLASH,
    SCHOOL_CHAT,
    SCHOOL_PLAN,
    SCHOOL_REASONER,
    SCHOOL_VISION,
    ModelBudgetExceeded,
    school_json,
)
from backend.services.run_names import EntryErrorCode


def test_named_models():
    assert SCHOOL_CHAT == "deepseek-chat"
    assert SCHOOL_REASONER == "deepseek-reasoner"
    assert SCHOOL_PLAN == "minimax"
    assert SCHOOL_VISION == "qwen"
    assert DEEPSEEK_FLASH == "deepseek-flash"


class _Campus:
    """A campus-shaped client with no FakeLLM methods, so the real transport runs."""

    def __init__(self, usage: dict[str, int] | None = None) -> None:
        self.usage = usage
        self.calls = 0

    def chat_completion(self, model, messages, stream=False, max_tokens=None, temperature=None, **_kwargs):
        del model, messages, stream, max_tokens, temperature
        self.calls += 1
        if self.usage is not None:
            self.last_usage = self.usage
        return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}


def _open(db, key: str, **caps):
    run, _created = db.create_agent_run("dist-1", "u-a", "chat", key, {"message": "hi"}, **caps)
    assert db.claim_run(run["id"], "worker-test", utcnow() + timedelta(minutes=5)) is not None
    step = db.insert_step(run["id"], "model", "model", {})
    assert step is not None
    return run, step


def test_vendor_usage_is_actual_and_a_missing_usage_is_estimated(db):
    run, step = _open(db, "usage-source")
    metered = _Campus({"prompt_tokens": 11, "completion_tokens": 7})
    school_json(
        SCHOOL_CHAT, system="count these", messages=[{"role": "user", "content": "hi"}],
        client=metered, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    bare = _Campus()
    school_json(
        SCHOOL_CHAT, system="汉字汉字汉字汉字", messages=[{"role": "user", "content": "abcd"}],
        client=bare, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    calls = db.run_trace("dist-1", run["id"])["model_calls"]
    assert {
        (row["usage_source"], row["prompt_tokens"], row["completion_tokens"]) for row in calls
    } == {("actual", 11, 7), ("estimated", 9, 3)}
    usage = db.list_usage_records(run["id"])
    assert {row["usage_source"] for row in usage} == {"actual", "estimated"}
    actual = next(row for row in usage if row["usage_source"] == "actual")
    assert actual["provider"] == "sjtu" and actual["model"] == SCHOOL_CHAT
    events = db.run_trace("dist-1", run["id"])["events"]
    started = next(event for event in events if event["event_type"] == "model.started")
    completed = next(event for event in events if event["event_type"] == "model.completed")
    assert started["payload_json"]["display"] == "正在思考"
    assert started["payload_json"]["purpose"] == "probe"
    assert started["payload_json"]["trace_id"] == run["trace_id"]
    assert completed["payload_json"]["usage_source"] == "actual"
    assert completed["payload_json"]["trace_id"] == run["trace_id"]
    assert completed["payload_json"]["prompt_tokens"] == 11


def test_a_run_over_its_model_budget_does_not_call(db):
    run, step = _open(db, "over-calls", max_model_calls=1)
    client = _Campus({"prompt_tokens": 4, "completion_tokens": 1})
    school_json(
        SCHOOL_CHAT, system="once", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    with pytest.raises(ModelBudgetExceeded) as raised:
        school_json(
            SCHOOL_CHAT, system="twice", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert str(raised.value) == EntryErrorCode.COST_BUDGET_EXCEEDED
    assert client.calls == 1
    assert len(db.run_trace("dist-1", run["id"])["model_calls"]) == 1
    failed = [
        event for event in db.run_trace("dist-1", run["id"])["events"]
        if event["event_type"] == "model.failed"
    ]
    assert failed[-1]["payload_json"]["error"] == EntryErrorCode.COST_BUDGET_EXCEEDED
    assert failed[-1]["payload_json"]["trace_id"] == run["trace_id"]


def test_an_org_over_its_daily_tokens_does_not_call(db, monkeypatch):
    run, step = _open(db, "org-daily")
    client = _Campus({"prompt_tokens": 4, "completion_tokens": 1})
    school_json(
        SCHOOL_CHAT, system="once", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    monkeypatch.setenv("KA_ACCOUNT_ORG_DAILY_TOKENS", "1")
    with pytest.raises(ModelBudgetExceeded):
        school_json(
            SCHOOL_CHAT, system="twice", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert client.calls == 1


def test_a_run_over_its_token_budget_does_not_call(db):
    run, step = _open(db, "over-tokens", max_tokens=5)
    client = _Campus({"prompt_tokens": 4, "completion_tokens": 1})
    school_json(
        SCHOOL_CHAT, system="once", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    with pytest.raises(ModelBudgetExceeded):
        school_json(
            SCHOOL_CHAT, system="twice", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert client.calls == 1
