"""The monitor reads the ledger. It does not keep a second metrics store.

`/metrics` is the Prometheus text for the fifteen names in the run roadmap.
`/monitor` is the same snapshot, plus the SLO checks and the five runbooks.
Queue depth is the Celery list that workers have not taken. The SSE gauge is
the number of open event streams in this process group.
"""

from __future__ import annotations

from typing import Any

from redis import Redis

from ..config import celery_broker_url
from ..db import Database
from ..redis_store import client
from .entry_guard import limits, mcp_tools_enabled, queue_enabled, send_email_enabled

_SSE_KEY = "pd:sse:open"
_QUEUE = "ka_account"

_METRIC_HELP = (
    ("agent_run_created_total", "counter", "Runs stored for this org"),
    ("agent_run_completed_total", "counter", "Runs that reached succeeded"),
    ("agent_run_failed_total", "counter", "Runs that reached failed"),
    ("agent_run_duration_seconds", "histogram", "Seconds from start to end"),
    ("agent_queue_pending_jobs", "gauge", "execute_run messages still in Redis"),
    ("agent_queue_lag_seconds", "histogram", "Seconds from create until the run starts"),
    ("agent_worker_active_runs", "gauge", "Running runs whose lease has not expired"),
    ("agent_model_call_latency_seconds", "histogram", "Model call latency"),
    ("agent_model_error_total", "counter", "Model calls stored as failed"),
    ("agent_tool_call_total", "counter", "Tool calls stored for this org"),
    ("agent_tool_error_total", "counter", "Tool calls that failed or lost the response"),
    ("agent_side_effect_duplicate_prevented_total", "counter", "Side effects blocked by an idempotency key"),
    ("agent_sse_connection_count", "gauge", "Open run event streams"),
    ("agent_stuck_runs", "gauge", "Queued over 10 minutes, or running with an expired lease"),
    ("agent_estimated_cost_cents_total", "counter", "Stored cost in cents, including estimated usage"),
)

RUNBOOKS: tuple[dict[str, str], ...] = (
    {
        "id": "queue",
        "title": "排队变长",
        "body": (
            "看 Redis 里还没取走的 execute_run、正在跑的 run，以及 worker 日志。"
            "worker 不够就加大并发。下游已经超时或返回 429 时，先把该工具或该模型的并发降下来。"
            "这时候再加 worker，只会把下游打得更满。"
        ),
    },
    {
        "id": "stuck",
        "title": "卡住的 run 变多",
        "body": (
            "看 lease_expires_at、最后一条 run_events、最后一份 checkpoint，然后跑 sweeper。"
            "租约还没过期的慢任务（report.draft、po.draft）不要收掉。"
        ),
    },
    {
        "id": "side_effect",
        "title": "发信或 MCP 报错变多",
        "body": (
            "看 tool_calls.error_json。确认下游异常后，关上对应的环境变量开关。"
            "之后的调用进入 waiting_for_user，不自动重发。"
        ),
    },
    {
        "id": "model",
        "title": "模型 429 变多",
        "body": (
            "降低该 provider 的并发，晚一点再试，并告诉正在用的人可能会慢。"
            "没有第二家已经接好的备用模型，就不切换模型。"
        ),
    },
    {
        "id": "migration",
        "title": "迁移失败",
        "body": (
            "停止继续发布。看 Alembic 是否写到一半，用准备好的回滚把 schema 退回去。"
            "API 和 worker 必须对着同一版 schema。一边新一边旧会读到不存在的列。"
        ),
    },
)


def note_sse(opened: bool) -> None:
    """Count one open event stream. A Redis miss leaves the gauge unchanged."""
    try:
        if opened:
            client().incr(_SSE_KEY)
            return
        count = int(client().decr(_SSE_KEY))
        if count < 0:
            client().set(_SSE_KEY, 0)
    except Exception:
        return


def snapshot(db: Database, org_id: str) -> dict[str, Any]:
    """Ledger facts plus the two Redis gauges."""
    facts = db.monitor_facts(org_id)
    facts["pending_jobs"] = _pending_jobs()
    facts["sse_connections"] = _sse_connections()
    return facts


def metrics_text(db: Database, org_id: str) -> str:
    facts = snapshot(db, org_id)
    lines: list[str] = []
    values = _series(facts)
    for name, kind, help_text in _METRIC_HELP:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        if kind == "histogram":
            lines.extend(_histogram_lines(name, values[name]))
        else:
            lines.append(f"{name} {_num(values[name])}")
    return "\n".join(lines) + "\n"


def monitor(db: Database, org_id: str) -> dict[str, Any]:
    facts = snapshot(db, org_id)
    cap = limits()
    return {
        "switches": {
            "send_email": send_email_enabled(),
            "mcp": mcp_tools_enabled(),
            "queue": queue_enabled(),
        },
        "slo": _slo(facts, cap.daily_tokens, cap.daily_cost_cents),
        "alerts": _alerts(facts, cap.daily_tokens, cap.daily_cost_cents),
        "runbooks": [dict(item) for item in RUNBOOKS],
        "charts": _charts(db, org_id, facts),
        "metrics": {
            "created": facts["created"],
            "succeeded": facts["succeeded"],
            "failed": facts["failed"],
            "duration_p95": facts["duration"]["p95"],
            "queue_lag_p95": facts["queue_lag"]["p95"],
            "model_latency_p95": facts["model_latency"]["p95"],
            "pending_jobs": facts["pending_jobs"],
            "active_runs": facts["active_runs"],
            "sse_connections": facts["sse_connections"],
            "stuck_runs": facts["stuck_runs"],
            "model_errors": facts["model_errors"],
            "tool_calls": facts["tool_calls"],
            "tool_errors": facts["tool_errors"],
            "duplicates": facts["duplicates"],
            "cost_cents": facts["cost_cents"],
            "daily_cost_cents": facts["daily_cost_cents"],
            "daily_tokens": facts["daily_tokens"],
        },
    }


def _charts(db: Database, org_id: str, facts: dict[str, Any]) -> dict[str, Any]:
    window = db.monitor_charts(org_id)
    cap = limits()
    return {
        "hours": window["hours"],
        "duration": window["duration"],
        "duration_p95": window["duration_p95"],
        "queue": window["queue"],
        "queue_p95": window["queue_p95"],
        "levels": [
            {"label": "卡住", "value": facts["stuck_runs"], "limit": 10},
            {"label": "正在跑", "value": facts["active_runs"], "limit": cap.org_running or None},
            {"label": "待领", "value": facts["pending_jobs"], "limit": None},
        ],
    }


def _series(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_run_created_total": facts["created"],
        "agent_run_completed_total": facts["succeeded"],
        "agent_run_failed_total": facts["failed"],
        "agent_run_duration_seconds": facts["duration"],
        "agent_queue_pending_jobs": facts["pending_jobs"],
        "agent_queue_lag_seconds": facts["queue_lag"],
        "agent_worker_active_runs": facts["active_runs"],
        "agent_model_call_latency_seconds": facts["model_latency"],
        "agent_model_error_total": facts["model_errors"],
        "agent_tool_call_total": facts["tool_calls"],
        "agent_tool_error_total": facts["tool_errors"],
        "agent_side_effect_duplicate_prevented_total": facts["duplicates"],
        "agent_sse_connection_count": facts["sse_connections"],
        "agent_stuck_runs": facts["stuck_runs"],
        "agent_estimated_cost_cents_total": facts["cost_cents"],
    }


def _histogram_lines(name: str, hist: dict[str, Any]) -> list[str]:
    lines = [
        f'{name}_bucket{{le="{_edge(edge)}"}} {count}'
        for edge, count in zip(hist["edges"], hist["buckets"], strict=True)
    ]
    lines.append(f'{name}_bucket{{le="+Inf"}} {hist["count"]}')
    lines.append(f"{name}_sum {_num(hist['sum'])}")
    lines.append(f"{name}_count {hist['count']}")
    return lines


def _slo(facts: dict[str, Any], daily_tokens: int, daily_cost: int) -> list[dict[str, Any]]:
    failed_ratio = _ratio(facts["failed"], facts["created"])
    mail = _ratio(facts["send_email_errors"], facts["send_email_calls"])
    mcp = _ratio(facts["mcp_errors"], facts["mcp_calls"])
    rows = [
        ("发消息建 run 的延迟 P95", "< 300ms", "没有单独记这一跳", None),
        ("run.created 成功率", "> 99.9%", "拒绝发生在写入之前，没有分母", None),
        ("普通 run 完成时间 P95", "< 60s", _seconds(facts["duration"]["p95"]), _under(facts["duration"]["p95"], 60)),
        ("run.failed 比例", "< 1%", _percent(failed_ratio), failed_ratio is None or failed_ratio < 0.01),
        ("send_email 错误率", "< 0.1%", _percent(mail), mail is None or mail < 0.001),
        ("MCP 错误率", "< 0.1%", _percent(mcp), mcp is None or mcp < 0.001),
        ("排队等待 P95", "< 10s", _seconds(facts["queue_lag"]["p95"]), _under(facts["queue_lag"]["p95"], 10)),
        ("SSE 断线后用 after 补齐", "必须成立", "事件流按 after 再读", True),
        ("卡住的 run", "< 10", str(facts["stuck_runs"]), facts["stuck_runs"] < 10),
        ("单 org 每日费用", _budget_target(daily_tokens, daily_cost), _budget_value(facts, daily_tokens, daily_cost), _budget_ok(facts, daily_tokens, daily_cost)),
    ]
    return [{"name": name, "target": target, "value": value, "ok": ok} for name, target, value, ok in rows]


def _alerts(facts: dict[str, Any], daily_tokens: int, daily_cost: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    ratio = _ratio(facts["failed"], facts["created"])
    if ratio is not None and ratio >= 0.01:
        codes = "、".join(f"{item['code']} {item['count']}" for item in facts["failed_codes"]) or "没有 error_code"
        found.append({
            "signal": "run.failed 比例升高",
            "action": f"看 error_code 最高的几项：{codes}",
            "runbook": None,
        })
    if not _under(facts["queue_lag"]["p95"], 10):
        found.append({"signal": "排队变长", "action": "先看 worker 还是下游，再决定扩容。", "runbook": "queue"})
    if facts["pending_jobs"] > 0 and facts["active_runs"] == 0:
        found.append({
            "signal": "待领任务还在，没有正在跑的 run",
            "action": "先看 worker 还是下游，再决定扩容。",
            "runbook": "queue",
        })
    if facts["stuck_runs"] >= 10:
        found.append({"signal": "卡住的 run 变多", "action": "看 lease_expires_at，跑清扫。", "runbook": "stuck"})
    if not _under_rate(facts["send_email_errors"], facts["send_email_calls"]):
        found.append({"signal": "send_email 错误变多", "action": "暂停发信，转人工。", "runbook": "side_effect"})
    if not _under_rate(facts["mcp_errors"], facts["mcp_calls"]):
        found.append({"signal": "MCP 错误变多", "action": "暂停 MCP，转人工。", "runbook": "side_effect"})
    if facts["model_rate_limited"] > 0:
        found.append({"signal": "模型 429 变多", "action": "降该 provider 的并发，延迟再试。", "runbook": "model"})
    if facts["tool_timeouts"] > 0:
        found.append({
            "signal": "工具超时变多",
            "action": "降该工具并发。副作用超时走人工确认，不自动重试。",
            "runbook": None,
        })
    if not _budget_ok(facts, daily_tokens, daily_cost):
        found.append({
            "signal": "费用燃烧过快",
            "action": "看 max_model_calls，必要时拒绝新调用。",
            "runbook": None,
        })
    return found


def _pending_jobs() -> int:
    try:
        redis = Redis.from_url(celery_broker_url(), socket_connect_timeout=1, socket_timeout=1)
        try:
            return int(redis.llen(_QUEUE))
        finally:
            redis.close()
    except Exception:
        return 0


def _sse_connections() -> int:
    try:
        return max(int(client().get(_SSE_KEY) or 0), 0)
    except Exception:
        return 0


def _ratio(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return part / whole


def _under(p95: float | None, limit: float) -> bool:
    return p95 is None or p95 < limit


def _under_rate(errors: int, calls: int) -> bool:
    ratio = _ratio(errors, calls)
    return ratio is None or ratio < 0.001


def _seconds(p95: float | None) -> str:
    if p95 is None:
        return "没有样本"
    return f"{p95:.2f}s"


def _percent(ratio: float | None) -> str:
    if ratio is None:
        return "没有样本"
    return f"{ratio * 100:.2f}%"


def _budget_target(tokens: int, cost: int) -> str:
    if tokens <= 0 and cost <= 0:
        return "未设上限"
    parts = []
    if tokens > 0:
        parts.append(f"{tokens} token")
    if cost > 0:
        parts.append(f"{cost} 分")
    return "不超过 " + " / ".join(parts)


def _budget_value(facts: dict[str, Any], tokens: int, cost: int) -> str:
    if tokens <= 0 and cost <= 0:
        return f"今日 {facts['daily_tokens']} token，{facts['daily_cost_cents']} 分"
    return f"今日 {facts['daily_tokens']} token，{facts['daily_cost_cents']} 分"


def _budget_ok(facts: dict[str, Any], tokens: int, cost: int) -> bool:
    over_tokens = tokens > 0 and facts["daily_tokens"] >= tokens
    over_cost = cost > 0 and facts["daily_cost_cents"] >= cost
    return not over_tokens and not over_cost


def _edge(value: float) -> str:
    if value >= 1 and value == int(value):
        return str(int(value))
    return f"{value:g}"


def _num(value: float) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.6f}".rstrip("0")
