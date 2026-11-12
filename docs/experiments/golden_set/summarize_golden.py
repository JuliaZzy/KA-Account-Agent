#!/usr/bin/env python3
"""Per-path latency, cost and success for experiment B, from run_golden.py results.

  python3 docs/experiments/golden_set/summarize_golden.py \\
      --cases docs/experiments/golden_set/golden_v1.jsonl \\
      --results docs/experiments/exports/golden-pre.jsonl docs/experiments/exports/golden-ingest.jsonl \\
                docs/experiments/exports/golden-post.jsonl \\
      --out docs/experiments/exports/golden-b-summary.json

Same conventions as scripts/load_test_chat.py: nearest-rank percentiles over successful runs only,
P95 only from 20 samples, P99 only from 300. Chat latency is submit -> first assistant reply;
contract imports are reported separately and use upload_seconds.
"""
from __future__ import annotations

import argparse
import collections
import json
import math

P95_MIN_SAMPLES = 20
P99_MIN_SAMPLES = 300


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))]


def latency(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": pct(values, 0.50),
        "p95": pct(values, 0.95) if len(values) >= P95_MIN_SAMPLES else None,
        "p99": pct(values, 0.99) if len(values) >= P99_MIN_SAMPLES else None,
        "max": max(values) if values else None,
    }


def group_stats(rows: list[dict], *, ingest: bool = False) -> dict:
    ok = [r for r in rows if not r.get("error") and (
        bool(r.get("contract_id")) if ingest else r.get("answer") is not None
    )]
    ledgers = [r["ledger"] for r in ok if r.get("ledger")]
    all_ledgers = [r["ledger"] for r in rows if r.get("ledger")]
    secs = [r["upload_seconds" if ingest else "seconds"] for r in ok
            if r.get("upload_seconds" if ingest else "seconds") is not None]
    legacy = sum("unknown_usage_calls" not in led for led in all_ledgers)
    unknown = sum(led.get("unknown_usage_calls", 0) for led in all_ledgers)
    by_purpose: dict[str, list[float]] = collections.defaultdict(list)
    for led in ledgers:
        for call in led["calls"]:
            if call["status"] == "succeeded" and call["latency_ms"] is not None:
                by_purpose[call["purpose"] or "(unlabelled)"].append(call["latency_ms"] / 1000)
    n_led = len(ledgers)
    average = lambda key, digits: round(sum(led[key] for led in ledgers) / n_led, digits) if n_led else None  # noqa: E731
    return {
        "runs": len(rows),
        "succeeded": len(ok),
        "errors": [r.get("error") for r in rows if r.get("error")],
        "latency_seconds": latency(secs),
        "ledger_samples": len(all_ledgers),
        "successful_ledger_samples": n_led,
        "missing_ledger_samples": len(rows) - len(all_ledgers),
        "legacy_ledger_samples": legacy,
        "unknown_usage_calls": unknown,
        "cost_complete": bool(rows) and len(all_ledgers) == len(rows) and not legacy and not unknown,
        "known_cost_cny_all_runs": round(sum(led["cost_cny"] for led in all_ledgers), 6) if all_ledgers else None,
        "model_calls_per_run": average("model_calls", 2),
        "model_seconds_sum_per_run": average("model_seconds_sum", 2),
        "prompt_tokens_per_run": average("prompt_tokens", 0),
        "completion_tokens_per_run": average("completion_tokens", 0),
        "cost_cny_per_run": average("cost_cny", 6),
        "cost_cny_total": round(sum(led["cost_cny"] for led in ledgers), 6) if ledgers else None,
        "model_failures": sum(led["model_failures"] for led in ledgers),
        "model_latency_by_purpose": {
            k: {"calls": len(v), "p50": pct(v, 0.5), "max": max(v)} for k, v in sorted(by_purpose.items())
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    cases = {json.loads(line)["id"]: json.loads(line) for line in open(a.cases, encoding="utf-8")}
    rows = [json.loads(line) for path in a.results for line in open(path, encoding="utf-8") if line.strip()]
    rows = [r for r in rows if r["id"] in cases]
    imports = [r for r in rows if cases[r["id"]].get("phase") == "ingest" and not r.get("upload_skipped")
               and "upload_seconds" in r]
    rows = [r for r in rows if "seconds" in r and cases[r["id"]].get("phase") != "ingest"]
    by_cat: dict[str, list[dict]] = collections.defaultdict(list)
    by_case: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_cat[cases[r["id"]]["category"]].append(r)
        by_case[r["id"]].append(r)
    summary = {
        "overall": group_stats(rows),
        "by_category": {k: group_stats(v) for k, v in by_cat.items()},
        "by_case": {k: group_stats(v) for k, v in sorted(by_case.items())},
        "contract_import": {
            "overall": group_stats(imports, ingest=True),
            "by_case": {case_id: group_stats([r for r in imports if r["id"] == case_id], ingest=True)
                        for case_id in sorted({r["id"] for r in imports})},
        },
    }
    print(f"{'category':10s} {'runs':>4s} {'ok':>3s} {'P50 s':>7s} {'P95 s':>7s} {'max s':>7s} {'calls':>6s} {'¥/run':>9s}")
    for name, st in summary["by_category"].items():
        lat = st["latency_seconds"]
        fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
        cost = "-" if st["cost_cny_per_run"] is None else f"{st['cost_cny_per_run']:.6f}"
        print(f"{name:10s} {st['runs']:4d} {st['succeeded']:3d} {fmt(lat['p50']):>7s} {fmt(lat['p95']):>7s} "
              f"{fmt(lat['max']):>7s} {fmt(st['model_calls_per_run']):>6s} {cost:>9s}")
    chat = summary["overall"]
    print(f"chat known cost ¥{chat['cost_cny_total'] if chat['cost_cny_total'] is not None else 'unknown'}; "
          f"missing ledgers {chat['missing_ledger_samples']}, legacy ledgers {chat['legacy_ledger_samples']}, "
          f"unknown usage {chat['unknown_usage_calls']}")
    imp = summary["contract_import"]["overall"]
    print(f"contract imports: {imp['runs']} runs, {imp['ledger_samples']} with ledger, "
          f"known cost ¥{imp['known_cost_cny_all_runs'] if imp['known_cost_cny_all_runs'] is not None else 'unknown'}; "
          f"unknown usage {imp['unknown_usage_calls']}, legacy ledgers {imp['legacy_ledger_samples']}")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
