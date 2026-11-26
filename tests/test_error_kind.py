"""One row per exception `classify` knows. A new exception type is a new row."""

from __future__ import annotations

import asyncio

import pytest

from backend.llm.calls import JsonRepairExhausted, ModelBudgetExceeded, ModelOutputError
from backend.llm.errors import (
    INTERNAL_ERROR_CODE,
    ModelCapacityExceeded,
    ProviderError,
    classify,
    hands_off_to_worker,
)
from backend.services.run_worker import PermanentRunError


def _rows():
    return [
        (ModelCapacityExceeded("qwen", "qwen3.7-flash"), "capacity", True, "MODEL_CAPACITY_EXCEEDED", True),
        (ProviderError("qwen", "qwen3.7-flash", "rate_limited"), "provider", True, "PROVIDER_RATE_LIMITED", True),
        (ProviderError("qwen", "qwen3.7-flash", "authentication"), "provider", False, "PROVIDER_AUTHENTICATION", False),
        (ProviderError("qwen", "qwen3.7-flash", "internal"), "internal", True, INTERNAL_ERROR_CODE, False),
        (ProviderError("deepseek", "deepseek-flash", "event_loop"), "internal", True, INTERNAL_ERROR_CODE, False),
        (ModelBudgetExceeded("org"), "budget", False, "QUOTA_EXCEEDED", False),
        (PermanentRunError("参数不合法"), "permanent", False, "PERMANENT", False),
        (PermanentRunError("停", code="STOPPED"), "permanent", False, "STOPPED", False),
        (JsonRepairExhausted("修不好"), "invalid_json", False, "JSON_REPAIR_EXHAUSTED", False),
        (ModelOutputError("not json"), "invalid_json", False, "INVALID_JSON", False),
        (RuntimeError("This event loop is already running"), "internal", True, INTERNAL_ERROR_CODE, False),
        (TimeoutError("订单系统超时"), "timeout", True, "TIMEOUT", False),
        (ProviderError("qwen", "qwen3.7-flash", "timeout"), "provider", True, "PROVIDER_TIMEOUT", True),
        (ValueError("nope"), "internal", True, INTERNAL_ERROR_CODE, False),
    ]


@pytest.mark.parametrize(
    ("exc", "name", "retryable", "code", "hands_off"),
    _rows(),
    ids=[type(row[0]).__name__ + "-" + row[1] for row in _rows()],
)
def test_classify_maps_each_exception(exc, name, retryable, code, hands_off):
    kind = classify(exc)
    assert kind.name == name
    assert kind.retryable is retryable
    assert kind.code == code
    assert kind.code != "PROVIDER_INTERNAL"
    assert hands_off_to_worker(kind) is hands_off


def test_event_loop_reentry_does_not_fall_back_or_open_the_breaker(monkeypatch):
    from backend.llm import breaker, calls

    seen: list[str] = []
    recorded: list[str] = []

    def fake_once(model_name: str, **_kwargs):
        seen.append(model_name)
        raise RuntimeError("This event loop is already running")

    monkeypatch.setattr(calls, "_call_model_once", fake_once)
    monkeypatch.setattr(breaker, "record_failure", lambda brand: recorded.append(brand))
    calls._record_breaker("qwen", RuntimeError("This event loop is already running"))
    with pytest.raises(RuntimeError, match="already running"):
        asyncio.run(calls.call_model_async(
            "qwen3.7-flash", brand="qwen", kind="chat_completion", expect="json",
            purpose="orchestrator.plan",
        ))
    assert seen == ["qwen3.7-flash"]
    assert recorded == []
