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


def test_load_test_metrics_separates_observed_and_completed_latency(tmp_path):
    report = {
        "summary": {
            "submitted": 4,
            "completed": 3,
            "timeouts": 1,
            "completion_rate": 75.0,
        },
        "results": [
            {"completed": True, "total_ms": 1000},
            {"completed": True, "total_ms": 2000},
            {"completed": True, "total_ms": 3000},
            {"completed": False, "total_ms": 180000},
        ],
    }
    path = tmp_path / "load.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    metrics = load_test_metrics(path)

    assert metrics["submitted"] == 4
    assert metrics["completed"] == 3
    assert metrics["timeouts"] == 1
    assert metrics["completion_rate"] == 75.0
    assert metrics["observed_p95_seconds"] == 180.0
    assert metrics["completed_p95_seconds"] == 3.0


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
