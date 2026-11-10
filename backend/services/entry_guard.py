"""限流、并发闸门、组织日预算以及三个开关。

这些数值来自执行路线图的起点。每一项都是环境变量，在调用时读取，这样进程可在不改动调用点默认埋参的情况下调整配置。Redis 保存时间窗口与闸门；数据库只做账务账本。

闸门是一套「持有者」的有序集合，而不是单一共享计数器。每个持有者自行更新自己的 score。进程崩溃后会在与旧计数器键相同的 300s 窗口内失去席位——即便其他请求仍在维持该键存活，也不例外。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.exceptions import RedisError

from ..config import env
from ..redis_store import client
from .run_names import EntryErrorCode, StepEvent
from .run_service import AppError

# The old counter key expired after 300s. A dead holder is reclaimed on that
# same window. Renewing three times inside it means two missed beats are not
# treated as a crash. The key TTL only collects an idle key.
_SEAT_S = 300
_STALE_MS = _SEAT_S * 1000
_BEAT_S = _SEAT_S / 3

_LUA_HELP = """
local function key_kind(key)
  local t = redis.call('TYPE', key)
  if type(t) == 'table' then
    return t.ok
  end
  return t
end

local function ensure_zset(key)
  local kind = key_kind(key)
  if kind ~= 'none' and kind ~= 'zset' then
    redis.call('DEL', key)
  end
end

local function now_ms()
  local clock = redis.call('TIME')
  return tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
end
"""

# Sweep dead holders, then add this one only when the set is still under the cap.
# A full set is left untouched. A leftover string counter is deleted first.
_ACQUIRE = _LUA_HELP + """
local now = now_ms()
local stale = tonumber(ARGV[2])
for i, key in ipairs(KEYS) do
  ensure_zset(key)
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - stale)
  local limit = tonumber(ARGV[i + 2])
  if limit > 0 and redis.call('ZCARD', key) >= limit then
    return 0
  end
end
for i, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[i + 2])
  if limit > 0 then
    redis.call('ZADD', key, now, ARGV[1])
    redis.call('PEXPIRE', key, stale)
  end
end
return 1
"""

# ZADD XX counts inserts, so a score refresh would look like a lost seat.
# Presence is read first; XX then refuses to recreate a member already removed.
_RENEW = _LUA_HELP + """
for _, key in ipairs(KEYS) do
  if not redis.call('ZSCORE', key, ARGV[1]) then
    return 0
  end
end
local now = now_ms()
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, 'XX', now, ARGV[1])
  redis.call('PEXPIRE', key, ARGV[2])
end
return 1
"""

_RELEASE = """
local removed = 0
for _, key in ipairs(KEYS) do
  removed = removed + redis.call('ZREM', key, ARGV[1])
  if redis.call('ZCARD', key) == 0 then
    redis.call('DEL', key)
  end
end
return removed
"""

_COUNT = _LUA_HELP + """
ensure_zset(KEYS[1])
local now = now_ms()
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[1]))
return redis.call('ZCARD', KEYS[1])
"""

_scripts: dict[str, object] | None = None
_scripts_lock = threading.Lock()

_READ_TOOLS = frozenset({"web_search", "data.query", "translate", "exchange_rate"})
_SIDE_TOOLS = frozenset({"send_email"})


@dataclass(frozen=True)
class Limits:
    create_org: int
    create_user: int
    read_org: int
    read_user: int
    global_running: int
    org_running: int
    user_running: int
    side_effect: int
    read_tool: int
    model: int
    daily_tokens: int
    daily_cost_cents: int


def limits() -> Limits:
    return Limits(
        create_org=_int("KA_ACCOUNT_RATE_CREATE_ORG", 1000),
        create_user=_int("KA_ACCOUNT_RATE_CREATE_USER", 200),
        read_org=_int("KA_ACCOUNT_RATE_READ_ORG", 10000),
        read_user=_int("KA_ACCOUNT_RATE_READ_USER", 2000),
        global_running=_int("KA_ACCOUNT_GATE_GLOBAL", 100),
        org_running=_int("KA_ACCOUNT_GATE_ORG", 10),
        user_running=_int("KA_ACCOUNT_GATE_USER", 5),
        side_effect=_int("KA_ACCOUNT_GATE_SIDE_EFFECT", 3),
        read_tool=_int("KA_ACCOUNT_GATE_READ_TOOL", 20),
        model=_int("KA_ACCOUNT_GATE_MODEL", 32),
        daily_tokens=_int("KA_ACCOUNT_ORG_DAILY_TOKENS", 0),
        daily_cost_cents=_int("KA_ACCOUNT_ORG_DAILY_COST_CENTS", 0),
    )


def send_email_enabled() -> bool:
    return _on("SEND_EMAIL_ENABLED")


def mcp_tools_enabled() -> bool:
    return _on("MCP_TOOLS_ENABLED")


def queue_enabled() -> bool:
    return _on("QUEUE_ENABLED")


def tool_switch_off(name: str, *, side_effect: bool, idempotency_required: bool) -> str | None:
    """The switch that is holding this tool, or None when the call may proceed."""
    if name == "send_email" and not send_email_enabled():
        return "send_email"
    if name != "send_email" and side_effect and idempotency_required and not mcp_tools_enabled():
        return "mcp"
    return None


def admit_create(org_id: str, user_id: str) -> None:
    """Refuse a new run when this minute's window or the running gates are full."""
    cap = limits()
    minute = _minute()
    _hit(f"rate_limit:{org_id}:runs.create:{minute}", cap.create_org)
    _hit(f"rate_limit:{org_id}:{user_id}:runs.create:{minute}", cap.create_user)
    if not _room(org_id, user_id, cap):
        raise AppError(429, EntryErrorCode.RATE_LIMITED, "Too many runs are already going")


def admit_read(org_id: str, user_id: str) -> None:
    cap = limits()
    minute = _minute()
    _hit(f"rate_limit:{org_id}:runs.read:{minute}", cap.read_org)
    _hit(f"rate_limit:{org_id}:{user_id}:runs.read:{minute}", cap.read_user)


def acquire_run(org_id: str, user_id: str) -> Lease | None:
    """Take the global, org, and user seats for this claim.

    None when any limited seat is full. A limit of 0 is not a cap. Release
    drops only the holders this call added.
    """
    cap = limits()
    if not _room(org_id, user_id, cap):
        return None
    return _acquire([
        ("gate:running:global", cap.global_running),
        (f"gate:running:org:{org_id}", cap.org_running),
        (f"gate:running:user:{org_id}:{user_id}", cap.user_running),
    ])


def tool_group(name: str) -> str | None:
    if name in _SIDE_TOOLS:
        return "side_effect"
    if name in _READ_TOOLS:
        return "read_tool"
    return None


def acquire_tool(org_id: str, group: str) -> Lease | None:
    cap = limits()
    limit = cap.side_effect if group == "side_effect" else cap.read_tool
    return _acquire([(f"gate:tool:{org_id}:{group}", limit)])


def acquire_model(brand: str) -> Lease | None:
    return _acquire([(f"gate:model:{brand or 'unknown'}", limits().model)])


def org_budget(db, org_id: str, run_id: str) -> str | None:
    """`over` blocks the vendor call. `near` records one internal event per day.

    A limit of 0 is not a cap.
    """
    cap = limits()
    if cap.daily_tokens <= 0 and cap.daily_cost_cents <= 0:
        return None
    usage = db.org_daily_usage(org_id)
    tokens = int(usage["total_tokens"])
    cost = int(usage["estimated_cost_cents"])
    over_tokens = cap.daily_tokens > 0 and tokens >= cap.daily_tokens
    over_cost = cap.daily_cost_cents > 0 and cost >= cap.daily_cost_cents
    if over_tokens or over_cost:
        return "over"
    near_tokens = cap.daily_tokens > 0 and tokens * 5 >= cap.daily_tokens * 4
    near_cost = cap.daily_cost_cents > 0 and cost * 5 >= cap.daily_cost_cents * 4
    if near_tokens or near_cost:
        _warn_once(db, org_id, run_id)
    return None


def _warn_once(db, org_id: str, run_id: str) -> None:
    day = datetime.now(UTC).strftime("%Y%m%d")
    key = f"budget_warn:{org_id}:{day}"
    try:
        fresh = client().set(key, "1", nx=True, ex=86_400)
    except Exception:
        return
    if not fresh:
        return
    db.append_run_event(org_id, run_id, StepEvent.RUN_BUDGET_WARNING, {"org_id": org_id})


def _room(org_id: str, user_id: str, cap: Limits) -> bool:
    return (
        _open("gate:running:global", cap.global_running)
        and _open(f"gate:running:org:{org_id}", cap.org_running)
        and _open(f"gate:running:user:{org_id}:{user_id}", cap.user_running)
    )


def _open(key: str, limit: int) -> bool:
    if limit <= 0:
        return True
    return _count(key) < limit


def _hit(key: str, limit: int) -> None:
    if limit <= 0:
        return
    count = int(client().incr(key))
    client().expire(key, 120)
    if count > limit:
        raise AppError(429, EntryErrorCode.RATE_LIMITED, "Too many requests")


class Lease:
    """One acquire. `release` drops only this holder and can be called twice."""

    def __init__(self, keys: tuple[str, ...], holder: str) -> None:
        self._keys = keys
        self._holder = holder
        self._stopped = threading.Event()
        self._lost = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if keys:
            self._thread = threading.Thread(
                target=self._pump, name=f"gate-{holder[:8]}", daemon=True,
            )
            self._thread.start()

    def _pump(self) -> None:
        failed_since: float | None = None
        while self._keys and not self._stopped.wait(_BEAT_S):
            try:
                alive = _renew(self._keys, self._holder)
            except RedisError:
                failed_since = failed_since or time.monotonic()
                if time.monotonic() - failed_since >= _SEAT_S:
                    self._lost.set()
                    return
                continue
            failed_since = None
            if not alive:
                self._lost.set()
                return

    @property
    def lost(self) -> bool:
        """Whether Redis no longer confirmed every key in this lease."""
        return self._lost.is_set()

    def release(self) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        _drop(self._keys, self._holder)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _acquire(slots: list[tuple[str, int]]) -> Lease | None:
    """Take every limited key atomically, or none of them."""
    holder = uuid.uuid4().hex
    limited = [(key, limit) for key, limit in slots if limit > 0]
    if not limited:
        return Lease((), "")
    keys = tuple(key for key, _limit in limited)
    limits = [limit for _key, limit in limited]
    granted = _script("acquire")(keys=list(keys), args=[holder, _STALE_MS, *limits])
    if not int(granted):
        return None
    return Lease(keys, holder)


def _script(name: str):
    global _scripts
    if _scripts is None:
        with _scripts_lock:
            if _scripts is None:
                conn = client()
                _scripts = {
                    "acquire": conn.register_script(_ACQUIRE),
                    "renew": conn.register_script(_RENEW),
                    "release": conn.register_script(_RELEASE),
                    "count": conn.register_script(_COUNT),
                }
    assert _scripts is not None
    return _scripts[name]


def _claim(key: str, holder: str, limit: int) -> bool:
    granted = _script("acquire")(keys=[key], args=[holder, _STALE_MS, limit])
    return bool(int(granted))


def _renew(keys: tuple[str, ...] | str, holder: str) -> bool:
    key_list = [keys] if isinstance(keys, str) else list(keys)
    alive = _script("renew")(keys=key_list, args=[holder, _STALE_MS])
    return bool(int(alive))


def _drop(keys: tuple[str, ...] | list[str], holder: str) -> None:
    if not holder:
        return
    try:
        _script("release")(keys=list(keys), args=[holder])
    except RedisError:
        return


def _count(key: str) -> int:
    return int(_script("count")(keys=[key], args=[_STALE_MS]))


def _minute() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M")


def _int(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _on(name: str) -> bool:
    return env(name, "1").lower() not in {"0", "false", "no", "off"}
