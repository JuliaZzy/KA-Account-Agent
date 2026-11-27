"""One row per exception `classify` knows. A new exception type is a new row."""

from __future__ import annotations

import asyncio

import pytest

from backend.llm.calls import JsonRepairExhausted, ModelBudgetExceeded, ModelOutputError
from backend.resilience import (
    UNKNOWN_CODE,
    ClassifiedError,
    ModelCapacityExceeded,
    ProviderError,
    classify,
    hands_off_to_worker,
)
from backend.services.run_worker import PermanentRunError


def _rows():
    return [
        (ModelCapacityExceeded("qwen", "qwen3.7-flash"), "capacity", True, "MODEL_CAPACITY_EXCEEDED", True),
        (ProviderError("qwen", "qwen3.7-flash", "rate_limited"), "rate_limited", True, "PROVIDER_RATE_LIMITED", True),
        (ProviderError("qwen", "qwen3.7-flash", "authentication"), "authentication", False, "PROVIDER_AUTHENTICATION", False),
        (ProviderError("qwen", "qwen3.7-flash", "bad_request"), "bad_request", False, "PROVIDER_BAD_REQUEST", False),
        (ProviderError("qwen", "qwen3.7-flash", "bad_response"), "bad_response", False, "PROVIDER_BAD_RESPONSE", False),
        (ProviderError("qwen", "qwen3.7-flash", "circuit_open"), "circuit_open", False, "PROVIDER_CIRCUIT_OPEN", False),
        (ProviderError("qwen", "qwen3.7-flash", "internal"), "unknown", False, UNKNOWN_CODE, False),
        (ProviderError("deepseek", "deepseek-flash", "event_loop"), "unknown", False, UNKNOWN_CODE, False),
        (ModelBudgetExceeded("org"), "budget", False, "QUOTA_EXCEEDED", False),
        (PermanentRunError("参数不合法"), "permanent", False, "PERMANENT", False),
        (PermanentRunError("停", code="STOPPED"), "permanent", False, "STOPPED", False),
        (JsonRepairExhausted("修不好"), "invalid_json", True, "JSON_REPAIR_EXHAUSTED", False),
        (ModelOutputError("not json"), "invalid_json", True, "INVALID_JSON", False),
        (ClassifiedError("quota", "额度用尽", code="PROVIDER_QUOTA"), "quota", False, "PROVIDER_QUOTA", False),
        (RuntimeError("This event loop is already running"), "unknown", False, UNKNOWN_CODE, False),
        (TimeoutError("订单系统超时"), "unknown", False, UNKNOWN_CODE, False),
        (ProviderError("qwen", "qwen3.7-flash", "timeout"), "timeout", True, "PROVIDER_TIMEOUT", True),
        (ValueError("nope"), "unknown", False, UNKNOWN_CODE, False),
    ]


@pytest.mark.parametrize(
    ("exc", "name", "retryable", "code", "hands_off"),
    _rows(),
    ids=[f"{type(row[0]).__name__}-{row[1]}-{index}" for index, row in enumerate(_rows())],
)
def test_classify_maps_each_exception(exc, name, retryable, code, hands_off):
    kind = classify(exc)
    assert kind.name == name
    assert kind.retryable is retryable
    assert kind.code == code
    assert kind.code != "PROVIDER_INTERNAL"
    assert hands_off_to_worker(kind) is hands_off


def test_event_loop_reentry_does_not_fall_back_or_open_the_breaker(monkeypatch):
    from backend.llm import calls
    from backend.resilience import breaker

    seen: list[str] = []
    recorded: list[str] = []

    def fake_once(model_name: str, **_kwargs):
        seen.append(model_name)
        raise RuntimeError("This event loop is already running")

    monkeypatch.setattr(calls, "_call_model_once", fake_once)
    monkeypatch.setattr(breaker, "record_failure", lambda brand: recorded.append(brand))
    calls._record_breaker("qwen", RuntimeError("This event loop is already running"), None)
    with pytest.raises(RuntimeError, match="already running"):
        asyncio.run(calls.call_model_async(
            "qwen3.7-flash", brand="qwen", kind="chat_completion", expect="json",
            purpose="orchestrator.plan",
        ))
    assert seen == ["qwen3.7-flash"]
    assert recorded == []
    assert classify(RuntimeError("This event loop is already running")).name == "unknown"
