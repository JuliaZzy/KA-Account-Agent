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

# Vendor failures only. `circuit_open` is not here: the cooldown gates the next
# probe, and this request falls back or fails immediately. `internal` is a local
# bug, so it is not retried and does not open the vendor breaker.
RETRYABLE_KINDS = frozenset({
    "rate_limited", "server_error", "timeout", "network",
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
    "internal": "PROVIDER_INTERNAL",
    # Local asyncio misuse. Not a vendor failure, so it stays out of the breaker.
    "event_loop": "EVENT_LOOP_REENTERED",
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
    # Original SDK/HTTP exception, kept so logs and model_calls.error_json can show
    # more than the flattened "network from qwen …" message.
    cause_type: str | None = None
    cause: str | None = None

    def __post_init__(self) -> None:
        # The kind is the policy. A caller cannot mark an internal error retryable
        # or a vendor 429 permanent by passing the other flag.
        self.retryable = self.kind in RETRYABLE_KINDS
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


def _cause_fields(exc: BaseException) -> dict[str, str]:
    message = str(exc).strip() or repr(exc)
    return {"cause_type": type(exc).__name__, "cause": message[:500]}


def translate_provider_error(exc: BaseException, *, provider: str, model: str) -> ProviderError:
    """Turn an SDK / HTTP exception into `ProviderError`. Already-wrapped errors pass through."""
    if isinstance(exc, ProviderError):
        return exc
    headers = _headers_of(exc)
    status = _status_of(exc)
    retry_after = parse_retry_after(_header(headers, "Retry-After"))
    request_id = _header(headers, "X-Request-Id") or _header(headers, "x-request-id") or _request_id_of(exc)
    name = type(exc).__name__
    cause = _cause_fields(exc)
    if status is not None:
        kind, retryable = classify_status(status)
        return ProviderError(
            provider, model, kind,
            status_code=status, retry_after_seconds=retry_after,
            request_id=request_id, retryable=retryable, **cause,
        )
    if name in _TIMEOUT_NAMES or isinstance(exc, TimeoutError):
        return ProviderError(
            provider, model, "timeout",
            retry_after_seconds=retry_after, request_id=request_id, retryable=True, **cause,
        )
    if name in _NETWORK_NAMES:
        return ProviderError(
            provider, model, "network",
            retry_after_seconds=retry_after, request_id=request_id, retryable=True, **cause,
        )
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return ProviderError(
            provider, model, "authentication",
            status_code=status, request_id=request_id, retryable=False, **cause,
        )
    if name in {"BadRequestError", "UnprocessableEntityError"}:
        return ProviderError(
            provider, model, "bad_request",
            status_code=status, request_id=request_id, retryable=False, **cause,
        )
    if name in {"RateLimitError"}:
        return ProviderError(
            provider, model, "rate_limited",
            status_code=429, retry_after_seconds=retry_after, request_id=request_id, **cause,
        )
    # `asyncio.run` on a thread that already has a loop. A local bug, not the vendor.
    if isinstance(exc, RuntimeError) and "already running" in str(exc).lower():
        return ProviderError(provider, model, "event_loop", request_id=request_id, **cause)
    # Anything we do not recognize is a local bug. Retrying it, counting it toward
    # the vendor breaker, or falling back to another model hides the fault.
    return ProviderError(
        provider, model, "internal",
        request_id=request_id, **cause,
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


# Route groups. `fallback_on` still lists these names, not each fine kind.
KIND_CAPACITY = "capacity"
KIND_PROVIDER = "provider"
KIND_INVALID_JSON = "invalid_json"
UNKNOWN_CODE = "UNKNOWN"


class ClassifiedError(Exception):
    """A failure whose kind was declared where it was raised.

    `classify` reads `kind`. It does not look at the exception type to guess.
    """

    def __init__(self, kind: str, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


@dataclass(frozen=True)
class ErrorKind:
    """The declared kind and the code written on the run. Policy lives in `policy`."""

    name: str
    code: str

    @property
    def retryable(self) -> bool:
        from .policy import kind_policy

        return kind_policy(self.name).mode != "none"


def classify(exc: BaseException) -> ErrorKind:
    """Read the kind a boundary already declared. Undeclared exceptions are `unknown`.

    `ProviderError.kind` of `internal` or `event_loop` is a local fault the
    transport already wrapped. Those names are not in the policy table, so
    they are `unknown`: no retry, no model switch, no breaker.
    """
    from .policy import POLICIES

    if isinstance(exc, ModelCapacityExceeded):
        return ErrorKind(KIND_CAPACITY, str(exc.code))
    if isinstance(exc, ProviderError):
        if exc.kind in POLICIES:
            return ErrorKind(exc.kind, exc.code)
        return ErrorKind("unknown", UNKNOWN_CODE)
    if isinstance(exc, ClassifiedError):
        if exc.kind not in POLICIES:
            return ErrorKind("unknown", UNKNOWN_CODE)
        return ErrorKind(exc.kind, str(exc.code or _code_for(exc.kind)))

    from ..llm.calls import JsonRepairExhausted, ModelBudgetExceeded, ModelOutputError
    from ..services.run_worker import PermanentRunError

    if isinstance(exc, ModelBudgetExceeded):
        # The model row uses `exc.code` (`COST_BUDGET_EXCEEDED`). The run row
        # keeps the older quota code so a spent cap is distinct in `error_json`.
        return ErrorKind("budget", "QUOTA_EXCEEDED")
    if isinstance(exc, (JsonRepairExhausted, ModelOutputError)):
        code = getattr(exc, "code", None) or "INVALID_JSON"
        return ErrorKind(KIND_INVALID_JSON, str(code))
    if isinstance(exc, PermanentRunError):
        code = getattr(exc, "code", None) or "PERMANENT"
        return ErrorKind("permanent", str(code))
    return ErrorKind("unknown", UNKNOWN_CODE)


def _code_for(kind: str) -> str:
    if kind == "invalid_json":
        return "INVALID_JSON"
    if kind == "capacity":
        return "MODEL_CAPACITY_EXCEEDED"
    if kind == "budget":
        return "QUOTA_EXCEEDED"
    if kind == "permanent":
        return "PERMANENT"
    if kind == "unknown":
        return UNKNOWN_CODE
    return kind.upper()


def hands_off_to_worker(kind: ErrorKind) -> bool:
    """Infrastructure failures leave the handler and requeue. Business retries do not.

    A child tool turns `invalid_json` into `{ok: false}` for the parent. The
    same exception fails and business-retries only when nothing caught it.
    """
    from .policy import kind_policy

    return kind_policy(kind.name).mode == "infrastructure"


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
