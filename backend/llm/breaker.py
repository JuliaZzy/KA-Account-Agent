"""Per-provider circuit breaker in Redis.

Counts vendor `rate_limited` / `server_error` / `timeout` / `network` only.
A full local `acquire_model` seat is not a vendor failure and must not open
this circuit. Redis errors fail open so a broken breaker does not block calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.exceptions import RedisError

from ..config import env
from ..redis_store import client

_FAILURES = "circuit:{brand}:failures"
_OPEN = "circuit:{brand}:open"
_PROBE = "circuit:{brand}:probe"


@dataclass(frozen=True)
class CircuitTicket:
    brand: str
    allowed: bool
    retry_after_seconds: float | None = None
    probing: bool = False


def failure_threshold() -> int:
    return _int("KA_ACCOUNT_CIRCUIT_FAILURES", 5)


def cooldown_seconds() -> int:
    return max(1, _int("KA_ACCOUNT_CIRCUIT_COOLDOWN_SECONDS", 30))


def acquire(brand: str) -> CircuitTicket:
    """May this process call the vendor? A denied ticket is `kind=circuit_open`."""
    name = brand or "unknown"
    threshold = failure_threshold()
    if threshold <= 0:
        return CircuitTicket(brand=name, allowed=True)
    try:
        redis = client()
        open_key = _OPEN.format(brand=name)
        ttl = int(redis.ttl(open_key))
        if ttl > 0:
            return CircuitTicket(brand=name, allowed=False, retry_after_seconds=float(ttl))
        failures = int(redis.get(_FAILURES.format(brand=name)) or 0)
        if failures < threshold:
            return CircuitTicket(brand=name, allowed=True)
        cooldown = cooldown_seconds()
        if redis.set(_PROBE.format(brand=name), "1", nx=True, ex=max(cooldown, 60)):
            return CircuitTicket(brand=name, allowed=True, probing=True)
        return CircuitTicket(brand=name, allowed=False, retry_after_seconds=float(cooldown))
    except RedisError:
        return CircuitTicket(brand=name, allowed=True)


def record_success(brand: str) -> None:
    _delete(brand, _FAILURES, _OPEN, _PROBE)


def record_failure(brand: str) -> None:
    name = brand or "unknown"
    threshold = failure_threshold()
    if threshold <= 0:
        return
    try:
        redis = client()
        key = _FAILURES.format(brand=name)
        count = int(redis.incr(key))
        redis.expire(key, max(cooldown_seconds() * 10, 300))
        redis.delete(_PROBE.format(brand=name))
        if count >= threshold:
            redis.set(_OPEN.format(brand=name), "1", ex=cooldown_seconds())
    except RedisError:
        return


def release(ticket: CircuitTicket | None) -> None:
    """Drop a half-open probe that never reached the vendor."""
    if ticket is None or not ticket.probing:
        return
    _delete(ticket.brand, _PROBE)


def release_probe(brand: str) -> None:
    _delete(brand, _PROBE)


def _delete(brand: str, *templates: str) -> None:
    name = brand or "unknown"
    try:
        client().delete(*[item.format(brand=name) for item in templates])
    except RedisError:
        return


def _int(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
