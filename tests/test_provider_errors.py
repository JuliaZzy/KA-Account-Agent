"""Vendor 429 / 5xx mapping, client retries off, and the Redis circuit breaker."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.llm import calls
from backend.llm.calls import ModelCapacityExceeded
from backend.llm.client import DeepSeekClient, GLMClient, QwenClient
from backend.redis_store import client
from backend.resilience import (
    ProviderError,
    parse_retry_after,
    provider_error_from_http,
    translate_provider_error,
)
from backend.resilience.breaker import CircuitTicket, acquire, record_failure, record_success, release


def test_parse_retry_after_seconds():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(" 0 ") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("nope") is None


def test_parse_retry_after_http_date():
    when = datetime.now(UTC) + timedelta(seconds=30)
    header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    seconds = parse_retry_after(header)
    assert seconds is not None
    assert 20 <= seconds <= 35


def test_http_429_maps_retry_after():
    err = provider_error_from_http(
        SimpleNamespace(status_code=429, headers={"Retry-After": "12", "X-Request-Id": "req-1"}),
        provider="qwen",
        model="qwen3.7-flash",
    )
    assert err.kind == "rate_limited"
    assert err.retryable is True
    assert err.status_code == 429
    assert err.retry_after_seconds == 12
    assert err.request_id == "req-1"
    assert err.code == "PROVIDER_RATE_LIMITED"


def test_http_401_is_not_retryable():
    err = provider_error_from_http(
        SimpleNamespace(status_code=401, headers={}, text="no"),
        provider="deepseek",
        model="deepseek-flash",
    )
    assert err.kind == "authentication"
    assert err.retryable is False


def test_timeout_exception_is_retryable():
    err = translate_provider_error(
        TimeoutError("read timed out"),
        provider="qwen",
        model="qwen3.7-flash",
    )
    assert err.kind == "timeout"
    assert err.retryable is True
    assert err.cause_type == "TimeoutError"
    assert "read timed out" in (err.cause or "")


def test_unknown_sdk_error_is_internal_and_not_retried():
    err = translate_provider_error(
        RuntimeError("stream closed mid-chunk"),
        provider="qwen",
        model="qwen3.7-flash",
    )
    assert err.kind == "internal"
    assert err.retryable is False
    assert err.code == "PROVIDER_INTERNAL"
    assert err.cause_type == "RuntimeError"
    assert "stream closed mid-chunk" in (err.cause or "")


def test_async_generator_typeerror_is_internal_not_network():
    err = translate_provider_error(
        TypeError("'async_generator' object is not iterable"),
        provider="qwen",
        model="qwen3.7-flash",
    )
    assert err.kind == "internal"
    assert err.retryable is False
    assert err.code == "PROVIDER_INTERNAL"
    assert err.cause_type == "TypeError"
    assert "async_generator" in (err.cause or "")


def test_running_event_loop_is_not_a_provider_failure():
    err = translate_provider_error(
        RuntimeError("This event loop is already running"),
        provider="deepseek",
        model="deepseek-flash",
    )
    assert err.kind == "event_loop"
    assert err.retryable is False
    assert err.code == "EVENT_LOOP_REENTERED"
    assert err.cause_type == "RuntimeError"


def test_closed_event_loop_is_internal_not_network():
    err = translate_provider_error(
        RuntimeError("Event loop is closed"),
        provider="qwen",
        model="qwen3.7-flash",
    )
    assert err.kind == "internal"
    assert err.retryable is False
    assert err.cause_type == "RuntimeError"


def test_kind_decides_retry_even_when_the_caller_passes_the_other_flag():
    opened = ProviderError(
        "qwen", "qwen3.7-flash", "circuit_open", retry_after_seconds=30, retryable=True,
    )
    assert opened.retryable is False
    assert opened.code == "PROVIDER_CIRCUIT_OPEN"
    limited = ProviderError("qwen", "qwen3.7-flash", "rate_limited", retryable=False)
    assert limited.retryable is True


def _sdk_timeout_seconds(client) -> float:
    value = client._client.timeout
    if isinstance(value, (int, float)):
        return float(value)
    return float(value.read)


def test_openai_compatible_clients_disable_sdk_retries(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm.example/v1")
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.example/v1")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    assert DeepSeekClient()._client.max_retries == 0
    assert GLMClient()._client.max_retries == 0
    assert QwenClient()._client.max_retries == 0


def test_deepseek_and_glm_sdk_timeout_is_thirty_seconds_not_two_hours(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm.example/v1")
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.example/v1")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    assert _sdk_timeout_seconds(DeepSeekClient()) == 30
    assert _sdk_timeout_seconds(GLMClient()) == 30
    # Qwen's stream is bounded in `_call_qwen`; the SDK stays at LLMSettings.timeout.
    assert _sdk_timeout_seconds(QwenClient()) == 7200


def test_translate_maps_status_codes():
    class _HTTP(Exception):
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.response = type("R", (), {"headers": {"Retry-After": "8"}, "status_code": status})()

    limited = translate_provider_error(_HTTP(429), provider="qwen", model="qwen3.7-flash")
    assert limited.kind == "rate_limited"
    assert limited.retry_after_seconds == 8
    server = translate_provider_error(_HTTP(503), provider="qwen", model="qwen3.7-flash")
    assert server.kind == "server_error"
    assert server.retryable is True


def test_circuit_opens_after_consecutive_vendor_failures_and_allows_one_probe(monkeypatch):
    brand = f"test-{uuid.uuid4().hex}"
    monkeypatch.setenv("KA_ACCOUNT_CIRCUIT_FAILURES", "5")
    monkeypatch.setenv("KA_ACCOUNT_CIRCUIT_COOLDOWN_SECONDS", "30")
    try:
        for _ in range(5):
            record_failure(brand)
        closed = acquire(brand)
        assert closed.allowed is False
        assert closed.retry_after_seconds is not None
        record_success(brand)
        opened = acquire(brand)
        assert opened.allowed is True
        for _ in range(5):
            record_failure(brand)
        from backend.redis_store import client
        client().delete(f"circuit:{brand}:open")
        first = acquire(brand)
        second = acquire(brand)
        assert first.allowed is True and first.probing is True
        assert second.allowed is False
        release(first)
    finally:
        record_success(brand)


def _arm_half_open(brand: str, monkeypatch) -> None:
    monkeypatch.setenv("KA_ACCOUNT_CIRCUIT_FAILURES", "5")
    monkeypatch.setenv("KA_ACCOUNT_CIRCUIT_COOLDOWN_SECONDS", "30")
    for _ in range(5):
        record_failure(brand)
    client().delete(f"circuit:{brand}:open")


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("local fault"),
        ProviderError("qwen", "qwen3.7-flash", "bad_request"),
        ProviderError("qwen", "qwen3.7-flash", "authentication"),
    ],
    ids=["unknown", "bad_request", "authentication"],
)
def test_probe_outside_the_breaker_lets_the_next_request_probe(monkeypatch, error):
    brand = f"test-{uuid.uuid4().hex}"
    try:
        _arm_half_open(brand, monkeypatch)
        ticket = acquire(brand)
        assert ticket.probing is True
        calls._record_breaker(brand, error, ticket)
        nxt = acquire(brand)
        assert nxt.allowed is True and nxt.probing is True
        assert int(client().get(f"circuit:{brand}:failures") or 0) == 5
        release(nxt)
    finally:
        record_success(brand)


def test_non_probe_failure_does_not_drop_another_holders_probe(monkeypatch):
    brand = f"test-{uuid.uuid4().hex}"
    try:
        _arm_half_open(brand, monkeypatch)
        holder = acquire(brand)
        assert holder.probing is True
        for error in (
            RuntimeError("local fault"),
            ProviderError(brand, "m", "bad_request"),
            ProviderError(brand, "m", "authentication"),
        ):
            bystander = CircuitTicket(brand=brand, allowed=True, probing=False)
            calls._record_breaker(brand, error, bystander)
        blocked = acquire(brand)
        assert blocked.allowed is False
        assert client().get(f"circuit:{brand}:probe") is not None
        release(holder)
    finally:
        record_success(brand)


def test_lost_model_lease_releases_the_probe(monkeypatch):
    brand = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(calls, "client_for", lambda _brand, injected: injected or SimpleNamespace())
    monkeypatch.setattr(
        calls, "acquire_model", lambda _brand: SimpleNamespace(lost=True, release=lambda: None),
    )

    async def ok(**_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(calls, "_invoke", ok)
    try:
        _arm_half_open(brand, monkeypatch)
        with pytest.raises(ModelCapacityExceeded):
            asyncio.run(calls._call_model_once(
                "qwen3.7-flash", brand=brand, kind="chat_completion", client=SimpleNamespace(),
            ))
        nxt = acquire(brand)
        assert nxt.allowed is True and nxt.probing is True
        assert int(client().get(f"circuit:{brand}:failures") or 0) == 5
        release(nxt)
    finally:
        record_success(brand)
