"""Errors raised by model transport, capacity guards, and the provider adapter.

Three different "slow down" conditions must not share one code:

- HTTP 429 at our own API door is `AppError` / `EntryErrorCode.RATE_LIMITED`.
- A full local `acquire_model` seat is `ModelCapacityExceeded`.
- A vendor HTTP 429 / 5xx / timeout is `ProviderError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

RETRYABLE_KINDS = frozenset({
    "rate_limited", "server_error", "timeout", "network", "circuit_open",
})
BREAKER_KINDS = frozenset({"rate_limited", "server_error", "timeout", "network"})

_PROVIDER_CODES = {
    "rate_limited": "PROVIDER_RATE_LIMITED",
    "server_error": "PROVIDER_SERVER_ERROR",
    "timeout": "PROVIDER_TIMEOUT",
    "network": "PROVIDER_NETWORK",
    "bad_request": "PROVIDER_BAD_REQUEST",
    "authentication": "PROVIDER_AUTHENTICATION",
    "bad_response": "PROVIDER_BAD_RESPONSE",
    "circuit_open": "PROVIDER_CIRCUIT_OPEN",
}

_TIMEOUT_NAMES = frozenset({
    "Timeout", "ReadTimeout", "ConnectTimeout", "APITimeoutError", "TimeoutException",
})
_NETWORK_NAMES = frozenset({
    "ConnectionError", "ConnectError", "APIConnectionError", "ConnectTimeoutError",
})


class ModelCapacityExceeded(Exception):
    """No local concurrency seat was available for this model provider."""

    code = "MODEL_CAPACITY_EXCEEDED"
    retryable = True

    def __init__(self, brand: str, model: str, message: str | None = None) -> None:
        self.brand = brand
        self.model = model
        super().__init__(message or f"model capacity is full for {brand}")


@dataclass
class ProviderError(Exception):
    """One vendor HTTP / SDK failure. The adapter fills this; callers do not sleep."""

    provider: str
    model: str
    kind: str
    status_code: int | None = None
    retry_after_seconds: float | None = None
    request_id: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.kind in RETRYABLE_KINDS:
            self.retryable = True
        Exception.__init__(self, self._message())

    @property
    def code(self) -> str:
        return _PROVIDER_CODES.get(self.kind, "PROVIDER_ERROR")

    def _message(self) -> str:
        status = f" HTTP {self.status_code}" if self.status_code else ""
        return f"{self.kind}{status} from {self.provider} {self.model}".strip()


def parse_retry_after(value: str | None) -> float | None:
    """Seconds until the vendor wants another request. None when the header is missing or junk."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def classify_status(status: int) -> tuple[str, bool]:
    if status == 429:
        return "rate_limited", True
    if status == 408:
        return "timeout", True
    if status in {500, 502, 503, 504} or status >= 500:
        return "server_error", True
    if status in {401, 403}:
        return "authentication", False
    if 400 <= status < 500:
        return "bad_request", False
    return "server_error", True


def provider_error_from_http(response: Any, *, provider: str, model: str) -> ProviderError:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", None) or {}
    kind, retryable = classify_status(status or 500)
    return ProviderError(
        provider=provider,
        model=model,
        kind=kind,
        status_code=status or None,
        retry_after_seconds=parse_retry_after(_header(headers, "Retry-After")),
        request_id=_header(headers, "X-Request-Id") or _header(headers, "x-request-id"),
        retryable=retryable,
    )


def translate_provider_error(exc: BaseException, *, provider: str, model: str) -> ProviderError:
    """Turn an SDK / HTTP exception into `ProviderError`. Already-wrapped errors pass through."""
    if isinstance(exc, ProviderError):
        return exc
    headers = _headers_of(exc)
    status = _status_of(exc)
    retry_after = parse_retry_after(_header(headers, "Retry-After"))
    request_id = _header(headers, "X-Request-Id") or _header(headers, "x-request-id") or _request_id_of(exc)
    name = type(exc).__name__
    if status is not None:
        kind, retryable = classify_status(status)
        return ProviderError(
            provider, model, kind,
            status_code=status, retry_after_seconds=retry_after,
            request_id=request_id, retryable=retryable,
        )
    if name in _TIMEOUT_NAMES or isinstance(exc, TimeoutError):
        return ProviderError(
            provider, model, "timeout",
            retry_after_seconds=retry_after, request_id=request_id, retryable=True,
        )
    if name in _NETWORK_NAMES:
        return ProviderError(
            provider, model, "network",
            retry_after_seconds=retry_after, request_id=request_id, retryable=True,
        )
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return ProviderError(
            provider, model, "authentication",
            status_code=status, request_id=request_id, retryable=False,
        )
    if name in {"BadRequestError", "UnprocessableEntityError"}:
        return ProviderError(
            provider, model, "bad_request",
            status_code=status, request_id=request_id, retryable=False,
        )
    if name in {"RateLimitError"}:
        return ProviderError(
            provider, model, "rate_limited",
            status_code=429, retry_after_seconds=retry_after, request_id=request_id, retryable=True,
        )
    return ProviderError(
        provider, model, "network",
        retry_after_seconds=retry_after, request_id=request_id, retryable=True,
    )


def _header(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status > 0:
        return status
    response = getattr(exc, "response", None)
    if response is None:
        return None
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status > 0:
        return status
    return None


def _headers_of(exc: BaseException) -> Any:
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            return headers
    return getattr(exc, "headers", None)


def _request_id_of(exc: BaseException) -> str | None:
    value = getattr(exc, "request_id", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None
