"""按供应商放在 Redis 里的熔断器。

只累计策略表里 `breaker` 为真的 kind（`rate_limited`、`server_error`、`timeout`、`network`）。
本地槽满、`unknown`、`circuit_open` 本身不是厂商故障，不能打开熔断。
打开期间每个请求立刻拒绝。冷却结束后允许一次探测打到厂商，其余请求仍然拒绝。
探测若以不计入熔断的错误结束，或调用期间租约丢失，只释放自己这张探测票。
Redis 出错时放行，避免熔断器自己坏了把所有调用挡住。
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
    """这一次能不能打到厂商。被拒绝的票对应 `kind=circuit_open`。"""
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
    """丢掉这一张半开探测票。票是空的，或者这次不是探测，就什么都不做。

    只删自己持有的钥匙，不会清掉别人正在探测的那把。
    """
    if ticket is None or not ticket.probing:
        return
    _delete(ticket.brand, _PROBE)


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
