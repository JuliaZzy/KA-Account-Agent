"""Monitor percentiles and stage timings come from the ledger. An empty org stays null."""

from __future__ import annotations

from datetime import timedelta

from backend.db import utcnow
from backend.db.outcome import Outcome
from backend.schemas.run import MonitorResponse
from backend.services.monitor import metrics_text, monitor
from backend.services.run_names import RunStatus

_STAGES = {"queue", "claim", "plan", "child", "model", "tool", "answer", "history"}


def test_recent_fallback_ratio_and_internal_errors_raise_alerts():
    from backend.services.monitor import _alerts

    facts = {
        "failed": 0,
        "created": 20,
        "failed_codes": [],
        "queue_lag": {"p95": 1, "p99": 1},
        "duration": {"p99": 1},
        "pending_jobs": 0,
        "active_runs": 1,
        "stuck_runs": 0,
        "send_email_errors": 0,
        "send_email_calls": 0,
        "mcp_errors": 0,
        "mcp_calls": 0,
        "model_rate_limited": 0,
        "tool_timeouts": 0,
        "daily_tokens": 0,
        "daily_cost_cents": 0,
        "daily_unknown_usage_calls": 0,
        "route_selected_recent": 10,
        "route_fallback_recent": 2,
        "model_internal_recent": 1,
        "plan_truncated_recent": 1,
    }
    signals = {item["signal"] for item in _alerts(facts, 0, 0)}
    assert "降级调用占比升高" in signals
    assert "模型调用出现内部错误" in signals
    assert "计划输出被截断" in signals

    quiet = {**facts, "route_fallback_recent": 0, "model_internal_recent": 0, "plan_truncated_recent": 0}
    quiet_signals = {item["signal"] for item in _alerts(quiet, 0, 0)}
    assert "降级调用占比升高" not in quiet_signals
    assert "模型调用出现内部错误" not in quiet_signals
    assert "计划输出被截断" not in quiet_signals


def test_p99_is_null_without_samples_and_stays_with_p95(db):
    empty = db.monitor_facts("dist-1")
    assert empty["duration"]["p50"] is None
    assert empty["duration"]["p95"] is None
    assert empty["duration"]["p99"] is None
    assert empty["queue_lag"]["p99"] is None
    assert empty["duration"]["edges"][-1] == 300
    assert 180 in empty["duration"]["edges"]
    assert empty["model_latency"]["edges"][-1] == 30
    assert max(empty["model_latency"]["edges"]) < max(empty["duration"]["edges"])
    assert set(empty["stages"]) == _STAGES
    assert all(empty["stages"][name]["count"] == 0 for name in _STAGES)

    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "metric-1", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], "worker", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(run["id"], Outcome(kind="succeeded", output={"text": "ok"}), allowed_from=frozenset({RunStatus.RUNNING})) is not None

    facts = db.monitor_facts("dist-1")
    assert facts["duration"]["count"] == 1
    assert facts["duration"]["p50"] is not None
    assert facts["duration"]["p50"] <= facts["duration"]["p95"] <= facts["duration"]["p99"]
    assert facts["stages"]["queue"]["count"] >= 1

    failed, _created = db.create_agent_run("dist-1", "u-a", "chat", "metric-failed", {"message": "no"})
    assert db.claim_chat_turn(failed["id"], "worker", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(failed["id"], Outcome(kind="failed", error={"message": "no"}, reason="permanent", dead_letter=True), allowed_from=frozenset({RunStatus.RUNNING})) is not None
    waiting, _created = db.create_agent_run("dist-1", "u-a", "chat", "metric-queued", {"message": "later"})
    assert waiting["status"] == "queued"
    after = db.monitor_facts("dist-1")
    assert after["duration"]["count"] == 1
    assert after["failed"] == 1
    assert after["stages"]["queue"]["count"] == facts["stages"]["queue"]["count"] + 1
    other = db.monitor_facts("dist-missing")
    assert other["created"] == 0
    assert other["duration"]["p99"] is None

    board = monitor(db, "dist-1")
    MonitorResponse.model_validate(board)
    names = [row["name"] for row in board["slo"]]
    assert "普通 run 完成时间 P99" in names
    assert "排队等待 P99" in names
    marks = [bar["mark"] for bar in board["charts"]["duration"] if bar["mark"]]
    assert any("P95" in mark for mark in marks)
    assert any("P99" in mark for mark in marks)
    assert board["metrics"]["duration_p50"] == facts["duration"]["p50"]
    assert board["metrics"]["duration_p99"] == facts["duration"]["p99"]
    assert board["charts"]["duration_p99"] == facts["duration"]["p99"]
    assert len(board["charts"]["duration"]) == len(facts["duration"]["edges"]) + 1
    text = metrics_text(db, "dist-1")
    assert 'agent_run_duration_seconds_bucket{le="180"}' in text
    assert 'agent_run_duration_seconds_bucket{le="300"}' in text
    assert 'agent_model_call_latency_seconds_bucket{le="30"}' in text
    assert 'agent_model_call_latency_seconds_bucket{le="300"}' not in text
    assert "run_id=" not in text
    assert "session_id=" not in text
    assert "user_id=" not in text
    assert "agent_model_route_fallback_total 0" in text
    assert "agent_model_internal_error_total 0" in text
    assert all(item["signal"] != "降级调用占比升高" for item in board["alerts"])
    assert all(item["signal"] != "模型调用出现内部错误" for item in board["alerts"])
