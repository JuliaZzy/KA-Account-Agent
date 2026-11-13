"""Tests for the versioned model-route policy and fallback selection."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from backend.db import utcnow
from backend.llm import calls
from backend.llm.calls import ModelResult, qwen_json
from backend.llm.errors import ProviderError
from backend.llm.routing import (
    ROUTING_POLICY_VERSION,
    RouteCandidate,
    RoutePolicy,
    policy_for,
    quality_check,
    run_budget_for,
)


class _TraceDb:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def model_budget(self, _run_id: str) -> dict[str, str]:
        return {"org_id": "org-1", "trace_id": "trace-1"}

    def append_run_event(self, _org_id: str, _run_id: str, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def run_trace(self, _org_id: str, _run_id: str) -> dict:
        return {
            "events": [
                {"event_type": event_type, "payload_json": payload}
                for event_type, payload in self.events
            ]
        }


class _ScriptedChat:
    """Duck-typed vendor stand-in. No complete_json, so the real ledger still runs."""

    def __init__(self, bodies: list[dict]) -> None:
        self.bodies = list(bodies)
        self.last_usage = {"prompt_tokens": 4, "completion_tokens": 2}

    def chat_completion(self, **_kwargs):
        body = self.bodies.pop(0)
        return {"choices": [{"message": {"content": json.dumps(body)}}]}


def test_plan_policy_has_primary_fallback_and_budget_metadata():
    policy = policy_for("orchestrator.plan")

    assert policy is not None
    assert ROUTING_POLICY_VERSION == "model-routing-v1"
    assert policy.primary.model == "qwen3.7-flash"
    assert policy.fallbacks[0].model == "deepseek-flash"
    assert policy.max_tokens == 1200
    assert policy.max_model_calls == 2


def test_chat_and_open_runs_have_safety_net_budgets():
    chat = run_budget_for("chat")
    opened = run_budget_for("open")

    assert chat.max_model_calls == 8
    assert chat.max_tokens == 32_000
    assert opened.max_model_calls == 2
    assert opened.max_tokens == 8_000
    assert run_budget_for("po").max_model_calls is None


def test_unknown_quality_gate_fails_closed():
    policy = RoutePolicy(
        task="test",
        primary=RouteCandidate("fake", "fake-model"),
        quality_gate="valid_task_plans",
    )

    passed, reason = quality_check(policy, {"tasks": []})

    assert passed is False
    assert reason == "unknown_quality_gate:valid_task_plans"


def test_task_plan_quality_check_handles_a_deep_dependency_chain_iteratively():
    tasks = [
        {
            "id": f"task-{index}",
            "kind": "tool",
            "name": "echo",
            "depends_on": [] if index == 0 else [f"task-{index - 1}"],
        }
        for index in range(1200)
    ]

    passed, reason = quality_check(
        RoutePolicy("test", RouteCandidate("fake", "fake"), quality_gate="valid_task_plan"),
        {"tasks": tasks},
    )

    assert passed is True
    assert reason == "valid_task_plan"


def test_unknown_purpose_keeps_the_requested_model(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_once(model_name: str, **kwargs):
        seen.append((model_name, kwargs["brand"]))
        return ModelResult("fake", model_name, {"ok": True}, 1, 1, "estimated")

    monkeypatch.setattr(calls, "_call_model_once", fake_once)
    result = calls.call_model(
        "existing-model", brand="deepseek", kind="response", expect="json", purpose="other.task",
    )

    assert result.body == {"ok": True}
    assert seen == [("existing-model", "deepseek")]


def test_blank_purpose_does_not_select_a_route(monkeypatch):
    seen: list[str] = []

    def fake_once(model_name: str, **kwargs):
        seen.append(kwargs["brand"])
        return ModelResult("fake", model_name, {"ok": True}, 1, 1, "estimated")

    monkeypatch.setattr(calls, "_call_model_once", fake_once)
    result = calls.call_model(
        "existing-model", brand="deepseek", kind="response", expect="json",
    )

    assert result.body == {"ok": True}
    assert seen == ["deepseek"]


def test_named_purposes_receive_a_compatible_route():
    for purpose, primary in (
        ("session.title", "qwen3.7-flash"),
        ("memory.fold", "qwen3.7-flash"),
        ("po.claim", "qwen3.7-flash"),
        ("po.resolve", "qwen3.7-flash"),
        ("po.reply", "qwen3.7-flash"),
        ("brand.query", "qwen3.7-flash"),
        ("brand.answer", "qwen3.7-flash"),
        ("translate", "qwen3.7-flash"),
        ("contract.select", "qwen3.7-flash"),
        ("report", "deepseek-flash"),
        ("report.write", "deepseek-flash"),
        ("report.review", "qwen3.7-flash"),
        ("report.optional", "deepseek-flash"),
        ("contract.extract", "deepseek-flash"),
        ("code.fix", "deepseek-flash"),
    ):
        policy = policy_for(purpose)
        assert policy is not None
        assert policy.primary.model == primary
        assert policy.fallbacks[0].model in {"deepseek-flash", "qwen3.7-flash"}


def test_answer_policy_prefers_cheap_qwen():
    policy = policy_for("orchestrator.answer")
    assert policy is not None
    assert policy.primary.model == "qwen3.7-flash"
    assert policy.fallbacks[0].model == "deepseek-flash"


def test_plan_provider_error_uses_the_configured_fallback(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_once(model_name: str, **kwargs):
        seen.append((model_name, kwargs["brand"]))
        if len(seen) == 1:
            raise ProviderError("qwen", model_name, "rate_limited")
        return ModelResult("fake", model_name, {"tasks": []}, 1, 1, "estimated")

    monkeypatch.setattr(calls, "_call_model_once", fake_once)
    result = calls.call_model(
        "qwen3.7-flash", brand="qwen", kind="chat_completion", expect="json",
        purpose="orchestrator.plan",
    )

    assert result.body == {"tasks": []}
    assert seen == [("qwen3.7-flash", "qwen"), ("deepseek-flash", "deepseek")]


def test_route_events_are_written_to_the_existing_trace(monkeypatch):
    db = _TraceDb()

    monkeypatch.setattr(
        calls,
        "_call_model_once",
        lambda model_name, **kwargs: ModelResult("fake", model_name, {"tasks": []}, 1, 1, "estimated"),
    )
    calls.call_model(
        "qwen3.7-flash", brand="qwen", kind="chat_completion", expect="json",
        purpose="orchestrator.plan", db=db, run_id="run-1", step_id="step-1",
    )

    assert [event[0] for event in db.events] == ["model.route_selected"]
    assert db.events[0][1]["selected_model"] == "qwen3.7-flash"
    assert db.events[0][1]["trace_id"] == "trace-1"


def test_quality_gate_failure_falls_back_and_is_visible_in_trace(monkeypatch):
    db = _TraceDb()
    calls_seen: list[str] = []

    def fake_once(model_name: str, **kwargs):
        calls_seen.append(model_name)
        body = (
            {"tasks": [{"id": "bad", "kind": "unknown", "name": "x"}]}
            if len(calls_seen) == 1
            else {"tasks": []}
        )
        return ModelResult("fake", model_name, body, 1, 1, "estimated")

    monkeypatch.setattr(calls, "_call_model_once", fake_once)
    result = calls.call_model(
        "qwen3.7-flash", brand="qwen", kind="chat_completion", expect="json",
        purpose="orchestrator.plan", db=db, run_id="run-1", step_id="step-1",
    )

    assert result.body == {"tasks": []}
    assert calls_seen == ["qwen3.7-flash", "deepseek-flash"]
    assert any(event[0] == "model.quality_gate_failed" for event in db.events)
    assert any(event[0] == "model.route_fallback" for event in db.events)


def test_quality_gate_failure_falls_back_after_model_completed(db, monkeypatch):
    policy = policy_for("orchestrator.plan")
    assert policy is not None
    tight = replace(policy, max_model_calls=1)
    monkeypatch.setattr(calls, "policy_for", lambda *_args, **_kwargs: tight)
    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "route-quality", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], "worker-test", utcnow() + timedelta(minutes=5)) is not None
    step = db.insert_step(run["id"], "model", "model", {})
    parsed = qwen_json(
        system="plan",
        messages=[{"role": "user", "content": "hi"}],
        client=_ScriptedChat([
            {"tasks": [{"id": "bad", "kind": "unknown", "name": "x"}]},
            {"tasks": []},
        ]),
        db=db, run_id=run["id"], step_id=step["id"], purpose="orchestrator.plan",
    )

    assert parsed == {"tasks": []}
    types = [event["event_type"] for event in db.run_trace("dist-1", run["id"])["events"]]
    assert types.count("model.completed") == 2
    assert "model.quality_gate_failed" in types
    assert "model.route_fallback" in types


def test_report_route_allows_a_full_report_worth_of_calls():
    # Catch-all still covers a full report if a call forgets a graded purpose.
    policy = policy_for("report")
    assert policy is not None
    assert policy.max_model_calls >= 12
    assert policy.max_tokens >= 16_000
    write = policy_for("report.write")
    review = policy_for("report.review")
    optional = policy_for("report.optional")
    assert write is not None and write.max_model_calls == 12
    assert review is not None and review.max_model_calls == 8
    assert review.primary.model == "qwen3.7-flash"
    assert review.fallbacks[0].model == "deepseek-flash"
    assert optional is not None and optional.max_model_calls == 12
