"""Turn a pytest JUnit report and the run ledger into resume-safe metrics.

The script deliberately reports sample sizes next to percentages. A test-suite pass rate is
not the same thing as a production task-success rate, and a P95 from a tiny sample should not
be presented as a stable SLA.
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from backend.db import Database
from scripts.load_test_chat import P99_MIN_SAMPLES, _percentile


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def junit_metrics(path: Path) -> dict[str, Any]:
    """Read pass/fail/error/skip counts from pytest's JUnit XML output."""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    passed = failed = errored = skipped = 0
    for case in cases:
        if case.find("failure") is not None:
            failed += 1
        elif case.find("error") is not None:
            errored += 1
        elif case.find("skipped") is not None:
            skipped += 1
        else:
            passed += 1
    executed = passed + failed + errored
    return {
        "total": len(cases),
        "passed": passed,
        "failed": failed,
        "errors": errored,
        "skipped": skipped,
        "executed": executed,
        "pass_rate": (passed / executed * 100) if executed else None,
    }


def ledger_metrics(db: Database, org_id: str) -> dict[str, Any]:
    """Read run and tool outcomes from the same Postgres ledger used by the monitor page."""
    facts = db.monitor_facts(org_id)
    created = int(facts.get("created") or 0)
    succeeded = int(facts.get("succeeded") or 0)
    failed = int(facts.get("failed") or 0)
    finished = succeeded + failed
    tool_calls = int(facts.get("tool_calls") or 0)
    tool_errors = int(facts.get("tool_errors") or 0)
    return {
        "org_id": org_id,
        "runs_created": created,
        "runs_finished": finished,
        "runs_succeeded": succeeded,
        "runs_failed": failed,
        "run_success_rate": (succeeded / finished * 100) if finished else None,
        "run_duration_p95_seconds": _number((facts.get("duration") or {}).get("p95")),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_success_rate": ((tool_calls - tool_errors) / tool_calls * 100) if tool_calls else None,
        "duplicate_side_effects_prevented": int(facts.get("duplicates") or 0),
        "daily_tokens": int(facts.get("daily_tokens") or 0),
        "daily_cost_cents": int(facts.get("daily_cost_cents") or 0),
        "total_cost_cents": int(facts.get("cost_cents") or 0),
    }


def _published_p99(stored: Any, values: list[float], sample_count: int | None = None) -> float | None:
    """P99 is omitted until the sample is large enough to be more than one point."""
    count = len(values) if values else int(sample_count or 0)
    if count < P99_MIN_SAMPLES:
        return None
    number = _number(stored)
    return _percentile(values, 0.99) if number is None else number


def load_test_metrics(path: Path) -> dict[str, Any]:
    """Read the completion summary emitted by ``load_test_chat.py``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        raise ValueError(f"{path} has no load-test summary")
    rows = payload.get("results") if isinstance(payload, dict) else None
    durations = [
        float(row["total_ms"]) / 1000
        for row in (rows or [])
        if isinstance(row, dict) and _number(row.get("total_ms")) is not None
    ]
    completed_durations = [
        float(row["total_ms"]) / 1000
        for row in (rows or [])
        if isinstance(row, dict) and row.get("completed") and _number(row.get("total_ms")) is not None
    ]
    observed_p50 = _number(summary.get("observed_p50_seconds"))
    observed_p95 = _number(summary.get("observed_p95_seconds"))
    observed_max = _number(summary.get("observed_max_seconds"))
    if observed_p50 is None:
        observed_p50 = _percentile(durations, 0.50)
    if observed_p95 is None:
        observed_p95 = _percentile(durations, 0.95)
    observed_p99 = _published_p99(
        summary.get("observed_p99_seconds"), durations, _number(summary.get("submitted")),
    )
    if observed_max is None:
        observed_max = max(durations, default=None)
    raw_timeouts = summary.get("timeouts")
    timeouts = int(raw_timeouts) if raw_timeouts is not None else sum(
        1 for row in (rows or []) if isinstance(row, dict) and not row.get("completed")
    )
    return {
        "submitted": int(summary.get("submitted") or 0),
        "completed": int(summary.get("completed") or 0),
        "timeouts": timeouts,
        "completion_rate": _number(summary.get("completion_rate")),
        "observed_p50_seconds": observed_p50,
        "observed_p95_seconds": observed_p95,
        "observed_p99_seconds": observed_p99,
        "observed_max_seconds": observed_max,
        "completed_p50_seconds": _number(
            summary.get("completed_p50_seconds", summary.get("completion_p50_seconds"))
        ) or _percentile(completed_durations, 0.50),
        "completed_p95_seconds": _number(
            summary.get("completed_p95_seconds", summary.get("completion_p95_seconds"))
        ) or _percentile(completed_durations, 0.95),
        "completed_p99_seconds": _published_p99(
            summary.get("completed_p99_seconds"), completed_durations, _number(summary.get("completed")),
        ),
        "completed_max_seconds": _number(
            summary.get("completed_max_seconds", summary.get("completion_max_seconds"))
        ) or max(completed_durations, default=None),
        "usage": summary.get("usage") if isinstance(summary.get("usage"), dict) else None,
    }


def _format_percent(value: float | None) -> str:
    return "insufficient data" if value is None else f"{value:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path, help="pytest --junitxml output")
    parser.add_argument("--load-json", type=Path, help="load_test_chat.py output")
    parser.add_argument("--org-id", help="distributor id for Postgres run metrics")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    parser.add_argument(
        "--output",
        type=Path,
        help="write the JSON report to this path (implies JSON; still prints the human summary unless --json)",
    )
    args = parser.parse_args()

    report: dict[str, Any] = {}
    if args.junitxml:
        report["regression"] = junit_metrics(args.junitxml)
    if args.load_json:
        report["load_test"] = load_test_metrics(args.load_json)
    if args.org_id:
        report["ledger"] = ledger_metrics(Database(), args.org_id)
    if not report:
        parser.error("provide --junitxml and/or --org-id")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if "regression" in report:
        metric = report["regression"]
        print(
            "关键回归用例："
            f"{metric['passed']}/{metric['executed']} 通过 "
            f"({_format_percent(metric['pass_rate'])})；"
            f"跳过 {metric['skipped']} 条。"
        )
    if "ledger" in report:
        metric = report["ledger"]
        print(
            f"Run：{metric['runs_succeeded']}/{metric['runs_finished']} 成功 "
            f"({_format_percent(metric['run_success_rate'])})；"
            f"完成时间 P95 {metric['run_duration_p95_seconds']!s} 秒；"
            f"工具成功率 {_format_percent(metric['tool_success_rate'])}。"
        )
    if "load_test" in report:
        metric = report["load_test"]
        print(
            f"合成压测：{metric['completed']}/{metric['submitted']} 完成 "
            f"({_format_percent(metric['completion_rate'])})；"
            f"超时 {metric['timeouts']} 条；"
            f"成功 P50 {metric['completed_p50_seconds']} 秒，"
            f"P95 {metric['completed_p95_seconds']} 秒，"
            f"P99 {metric['completed_p99_seconds']} 秒。"
        )
    if args.output is not None:
        print(f"结果已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
