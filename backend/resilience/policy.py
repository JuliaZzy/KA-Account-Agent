"""One policy per declared error kind.

`classify` reads a kind that a boundary already put on the exception. It does
not invent one. A kind missing from this table is `unknown`: fail the run, do
not retry, do not switch models, do not open the breaker.

Infrastructure waits are not counted against the execution budget. The wait
cap is `min(60, base_seconds * 2^(n-1))`, then a uniform draw from 0 up to that
cap (`schedule_retry` with `spend_attempt=False`). A vendor `Retry-After` on
the four transport kinds replaces that wait, capped at 300 seconds.
Business retries spend an attempt, at most `EXECUTION_MAX_ATTEMPTS` (3), on
the 1 / 5 / 15 minute ladder.

Postgres `OperationalError` is retried inside the Celery task and is not a row
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["infrastructure", "business", "none"]

# Route `fallback_on` still names these groups. A kind switches models only when
# its group is listed on that route. `invalid_json` is listed on `data.react`
# and on unnamed purposes only.
GROUP_PROVIDER = "provider"
GROUP_CAPACITY = "capacity"
GROUP_INVALID_JSON = "invalid_json"


@dataclass(frozen=True)
class KindPolicy:
    mode: Mode
    fallback_group: str | None
    breaker: bool
    # Seconds. Infrastructure only. None when this kind does not wait.
    base_seconds: float | None = None
    honor_retry_after: bool = False


def _vendor_outage() -> KindPolicy:
    return KindPolicy(
        "infrastructure", GROUP_PROVIDER, True, base_seconds=1, honor_retry_after=True,
    )


# `authentication`, `quota`, and `bad_response` still switch models: today they
# are vendor errors whose route group is `provider`, and every route that falls
# back lists that group. `bad_request` does not switch.
POLICIES: dict[str, KindPolicy] = {
    "rate_limited": _vendor_outage(),
    "server_error": _vendor_outage(),
    "timeout": _vendor_outage(),
    "network": _vendor_outage(),
    "capacity": KindPolicy("infrastructure", GROUP_CAPACITY, False, base_seconds=5),
    "circuit_open": KindPolicy("none", GROUP_PROVIDER, False),
    "authentication": KindPolicy("none", GROUP_PROVIDER, False),
    "quota": KindPolicy("none", GROUP_PROVIDER, False),
    "bad_response": KindPolicy("none", GROUP_PROVIDER, False),
    "invalid_json": KindPolicy("business", GROUP_INVALID_JSON, False),
    "bad_request": KindPolicy("none", None, False),
    "content_filter": KindPolicy("none", None, False),
    "config": KindPolicy("none", None, False),
    "tool_error": KindPolicy("none", None, False),
    "budget": KindPolicy("none", None, False),
    "permanent": KindPolicy("none", None, False),
    "unknown": KindPolicy("none", None, False),
}


def kind_policy(name: str) -> KindPolicy:
    """Policy for a declared kind. Anything else is `unknown`."""
    return POLICIES.get(name, POLICIES["unknown"])


def fallback_group(name: str) -> str | None:
    """The `fallback_on` entry this kind matches, or None when it never switches."""
    return kind_policy(name).fallback_group
