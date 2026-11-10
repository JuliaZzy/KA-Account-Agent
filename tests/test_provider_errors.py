"""Vendor 429 / 5xx mapping, client retries off, and the Redis circuit breaker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from backend.llm.breaker import acquire, record_failure, record_success, release
from backend.llm.client import DeepSeekClient, GLMClient, QwenClient
from backend.llm.errors import parse_retry_after, provider_error_from_http, translate_provider_error


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
