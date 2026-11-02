"""Rate limits, concurrency gates, daily org budget, and the three switches.

The numbers are the starting points from the run roadmap. Each one is an
environment value, read on use, so a process can change them without a new
default buried in a call site. Redis holds the counters. The database stays
the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import env
from ..redis_store import client
from .run_names import EntryErrorCode, StepEvent
from .run_service import AppError

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
        user_running=_int("KA_ACCOUNT_GATE_USER", 3),
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


def acquire_run(org_id: str, user_id: str) -> bool:
    """Take one running slot for this claim. False when a gate is full."""
    cap = limits()
    if not _room(org_id, user_id, cap):
        return False
    held: list[str] = []
    for key, limit in (
        ("gate:running:global", cap.global_running),
        (f"gate:running:org:{org_id}", cap.org_running),
        (f"gate:running:user:{org_id}:{user_id}", cap.user_running),
    ):
        if not _take(key, limit):
            for taken in held:
                _give(taken)
            return False
        held.append(key)
    return True


def release_run(org_id: str, user_id: str) -> None:
    _give("gate:running:global")
    _give(f"gate:running:org:{org_id}")
    _give(f"gate:running:user:{org_id}:{user_id}")


def tool_group(name: str) -> str | None:
    if name in _SIDE_TOOLS:
        return "side_effect"
    if name in _READ_TOOLS:
        return "read_tool"
    return None


def acquire_tool(org_id: str, group: str) -> bool:
    cap = limits()
    limit = cap.side_effect if group == "side_effect" else cap.read_tool
    return _take(f"gate:tool:{org_id}:{group}", limit)


def release_tool(org_id: str, group: str) -> None:
    _give(f"gate:tool:{org_id}:{group}")


def acquire_model(provider: str) -> bool:
    return _take(f"gate:model:{provider or 'unknown'}", limits().model)


def release_model(provider: str) -> None:
    _give(f"gate:model:{provider or 'unknown'}")


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
        _count("gate:running:global") < cap.global_running
        and _count(f"gate:running:org:{org_id}") < cap.org_running
        and _count(f"gate:running:user:{org_id}:{user_id}") < cap.user_running
    )


def _hit(key: str, limit: int) -> None:
    if limit <= 0:
        return
    count = int(client().incr(key))
    client().expire(key, 120)
    if count > limit:
        raise AppError(429, EntryErrorCode.RATE_LIMITED, "Too many requests")


def _take(key: str, limit: int) -> bool:
    if limit <= 0:
        return True
    count = int(client().incr(key))
    client().expire(key, 300)
    if count > limit:
        _give(key)
        return False
    return True


def _give(key: str) -> None:
    count = int(client().decr(key))
    if count < 0:
        client().delete(key)


def _count(key: str) -> int:
    raw = client().get(key)
    return int(raw or 0)


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
