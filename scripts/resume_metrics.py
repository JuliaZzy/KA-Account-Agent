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
    }


def _format_percent(value: float | None) -> str:
    return "insufficient data" if value is None else f"{value:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path, help="pytest --junitxml output")
    parser.add_argument("--org-id", help="distributor id for Postgres run metrics")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    args = parser.parse_args()

    report: dict[str, Any] = {}
    if args.junitxml:
        report["regression"] = junit_metrics(args.junitxml)
    if args.org_id:
        report["ledger"] = ledger_metrics(Database(), args.org_id)
    if not report:
        parser.error("provide --junitxml and/or --org-id")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
