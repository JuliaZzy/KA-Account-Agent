"""Monitor percentiles and stage timings come from the ledger. An empty org stays null."""

from __future__ import annotations

from datetime import timedelta

from backend.db import utcnow
from backend.schemas.run import MonitorResponse
from backend.services.monitor import metrics_text, monitor

_STAGES = {"queue", "claim", "plan", "child", "model", "tool", "answer", "history"}


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
    assert db.complete_run(run["id"], {"text": "ok"}) is not None

    facts = db.monitor_facts("dist-1")
    assert facts["duration"]["count"] == 1
    assert facts["duration"]["p50"] is not None
    assert facts["duration"]["p50"] <= facts["duration"]["p95"] <= facts["duration"]["p99"]
    assert facts["stages"]["queue"]["count"] >= 1

    failed, _created = db.create_agent_run("dist-1", "u-a", "chat", "metric-failed", {"message": "no"})
    assert db.claim_chat_turn(failed["id"], "worker", utcnow() + timedelta(minutes=5)) is not None
    assert db.fail_run(failed["id"], {"message": "no"}) is not None
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
