"""How a model failure is classified, retried, switched, and broken.

Three modules, one decision:

- `errors` names the failure and reads the kind a boundary already declared.
- `policy` says whether that kind waits, spends an attempt, switches model, or opens the breaker.
- `breaker` keeps the per-brand circuit in Redis.

The worker still writes the outbox and the run status. `call_model_async` still
picks the next candidate. Those steps apply this package; they do not carry a
second copy of the rules.
"""

from . import breaker
from .errors import (
    KIND_CAPACITY,
    KIND_INVALID_JSON,
    KIND_PROVIDER,
    UNKNOWN_CODE,
    ClassifiedError,
    ErrorKind,
    ModelCapacityExceeded,
    ProviderError,
    classify,
    classify_status,
    hands_off_to_worker,
    parse_retry_after,
    provider_error_from_http,
    translate_provider_error,
)
from .policy import fallback_group, kind_policy

__all__ = [
    "KIND_CAPACITY",
    "KIND_INVALID_JSON",
    "KIND_PROVIDER",
    "UNKNOWN_CODE",
    "ClassifiedError",
    "ErrorKind",
    "ModelCapacityExceeded",
    "ProviderError",
    "breaker",
    "classify",
    "classify_status",
    "fallback_group",
    "hands_off_to_worker",
    "kind_policy",
    "parse_retry_after",
    "provider_error_from_http",
    "translate_provider_error",
]
