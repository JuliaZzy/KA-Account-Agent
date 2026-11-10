"""Unit tests for the repeatable performance-reporting helpers.

These tests do not start Compose, call a model, or create a database. They protect
the percentile and JSON-summary calculations used by the fixed performance workflow.
"""

from __future__ import annotations

import json

from scripts.load_test_chat import _percentile
from scripts.resume_metrics import load_test_metrics


def test_percentile_uses_the_upper_observation_for_tail_metrics():
    assert _percentile([], 0.95) is None
    assert _percentile([7.0], 0.95) == 7.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.50) == 2.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.99) == 4.0
    wider = [float(item) for item in range(1, 21)]
    assert _percentile(wider, 0.95) == 19.0
    assert _percentile(wider, 0.99) == 20.0


def test_a_small_sample_reports_p50_and_p95_and_leaves_p99_blank(tmp_path):
    """Twenty successes is one agent class inside a 100-request run. P99 would be the slowest row."""
    successes = [{"completed": True, "total_ms": index * 1000} for index in range(1, 21)]
    report = {
        "summary": {"submitted": 21, "completed": 20, "timeouts": 1, "completion_rate": 20 / 21 * 100},
        "results": [*successes, {"completed": False, "total_ms": 180000}],
    }
    path = tmp_path / "load.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    metrics = load_test_metrics(path)

    assert metrics["timeouts"] == 1
    assert metrics["completed_p50_seconds"] == 10.0
    assert metrics["completed_p95_seconds"] == 19.0
    assert metrics["completed_p99_seconds"] is None
    assert metrics["observed_p99_seconds"] is None
    assert metrics["completed_p95_seconds"] < 180.0


def test_a_pooled_sample_reports_p95_and_p99(tmp_path):
    """300 successes make P99 the fourth-slowest point, not the single maximum."""
    successes = [{"completed": True, "total_ms": index * 1000} for index in range(1, 301)]
    report = {
        "summary": {"submitted": 301, "completed": 300, "timeouts": 1},
        "results": [*successes, {"completed": False, "total_ms": 180000}],
    }
    path = tmp_path / "load.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    metrics = load_test_metrics(path)

    assert metrics["completed_p95_seconds"] == 285.0
    assert metrics["completed_p99_seconds"] == 297.0
    assert metrics["completed_p99_seconds"] < 300.0
    assert metrics["observed_p99_seconds"] == 297.0


def test_load_test_metrics_falls_back_to_result_durations(tmp_path):
    report = {
        "summary": {"submitted": 2, "completed": 2},
        "results": [
            {"completed": True, "total_ms": 500},
            {"completed": True, "total_ms": 1500},
        ],
    }
    path = tmp_path / "load.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    metrics = load_test_metrics(path)

    assert metrics["completion_rate"] is None
    assert metrics["observed_p50_seconds"] == 0.5
    assert metrics["observed_p95_seconds"] == 1.5
    assert metrics["completed_p50_seconds"] == 0.5
    assert metrics["completed_p95_seconds"] == 1.5
    assert metrics["observed_p99_seconds"] is None
    assert metrics["completed_p99_seconds"] is None
