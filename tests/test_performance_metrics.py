"""Unit tests for the repeatable performance-reporting helpers.

These tests do not start Compose, call a model, or create a database. They protect
the percentile and JSON-summary calculations used by the fixed performance workflow.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from backend.llm.pricing import call_cost_cny
from scripts.load_test_chat import _percentile, summarize_usage_rows
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


def test_zero_timeouts_are_not_replaced_by_other_failures(tmp_path):
    report = {
        "summary": {"submitted": 1, "completed": 0, "timeouts": 0},
        "results": [{"completed": False, "outcome": "enqueue_error", "total_ms": 50}],
    }
    path = tmp_path / "load.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    assert load_test_metrics(path)["timeouts"] == 0


def test_qwen_and_deepseek_pricing_and_usage_summary():
    # Weekday 02:30 UTC is 10:30 Beijing time, inside the vendor's peak window; pricing ignores it.
    peak_time = datetime(2026, 11, 10, 2, 30, tzinfo=UTC)
    offpeak_time = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)

    # qwen3.7-flash <=32K tier: miss ¥0.20/1M, implicit hit ¥0.04/1M (20%), output ¥0.80/1M
    qwen_short = call_cost_cny(
        "qwen",
        "qwen3.7-flash",
        prompt_tokens=10_000,
        completion_tokens=1_000,
        cache_hit_tokens=5_000,
        cache_miss_tokens=5_000,
    )
    assert round(qwen_short, 6) == round((5_000 * 0.20 + 5_000 * 0.04 + 1_000 * 0.80) / 1_000_000, 6)

    # qwen3.7-flash >256K tier: miss ¥1.20/1M, implicit hit ¥0.24/1M, output ¥4.80/1M
    qwen_long = call_cost_cny(
        "qwen",
        "qwen3.7-flash",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cache_hit_tokens=500_000,
        cache_miss_tokens=500_000,
    )
    assert round(qwen_long, 4) == round(0.5 * 1.20 + 0.5 * 0.24 + 4.80, 4)

    # deepseek-flash is always priced off-peak (¥1.00 miss / ¥0.02 hit / ¥4.00 out), at any hour
    ds_peak = call_cost_cny(
        "deepseek",
        "deepseek-flash",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cache_hit_tokens=500_000,
        cache_miss_tokens=500_000,
        created_at=peak_time,
    )
    ds_offpeak = call_cost_cny(
        "deepseek",
        "deepseek-flash",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cache_hit_tokens=500_000,
        cache_miss_tokens=500_000,
        created_at=offpeak_time,
    )
    assert round(ds_peak, 4) == round(0.5 * 1.00 + 0.5 * 0.02 + 4.00, 4)
    assert round(ds_offpeak, 4) == round(ds_peak, 4)

    usage = summarize_usage_rows(
        [
            {
                "run_id": "r1",
                "run_type": "chat",
                "provider": "qwen",
                "model": "qwen3.7-flash",
                "prompt_tokens": 1368,
                "completion_tokens": 10,
                "cache_hit_tokens": 1024,
                "cache_miss_tokens": 344,
                "created_at": peak_time,
            },
            {
                "run_id": "r2",
                "run_type": "open",
                "provider": "qwen",
                "model": "qwen3.7-flash",
                "prompt_tokens": 405,
                "completion_tokens": 18,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 405,
                "created_at": peak_time,
            },
        ],
        requests=1,
    )
    assert usage["prompt_tokens"] == 1773
    assert usage["completion_tokens"] == 28
    assert usage["cache_hit_tokens"] == 1024
    assert usage["cache_miss_tokens"] == 749
    assert usage["cache_hit_ratio"] == round(1024 / 1773 * 100, 2)
    assert usage["estimated_cost_cny"] > 0
    assert usage["uncached_cost_cny"] > usage["estimated_cost_cny"]
