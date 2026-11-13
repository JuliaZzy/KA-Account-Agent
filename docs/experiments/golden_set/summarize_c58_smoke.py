"""Summarize C knives 5–8 smoke: charts∥insights, parallel writes, templated metrics, purpose grades."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path("docs/experiments/exports")
PREFIX = sys.argv[1] if len(sys.argv) > 1 else "c58-smoke"
CASES = ["rpt-01", "rpt-07", "plan-02"]
METRICS_HINTS = ("月采购", "月销售", "采购总额", "销售总额", "采购金额", "销售金额", "metrics")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def overlap(a0, a1, b0, b1) -> float:
    if None in (a0, a1, b0, b1):
        return 0.0
    start, end = max(a0, b0), min(a1, b1)
    return max(0.0, (end - start).total_seconds())


def call_span(call: dict) -> tuple[datetime | None, datetime | None]:
    started = parse_ts(call.get("started_at"))
    ended = parse_ts(call.get("ended_at") or call.get("updated_at"))
    ms = call.get("latency_ms")
    if started and ended is None and isinstance(ms, (int, float)):
        from datetime import timedelta
        ended = started + timedelta(milliseconds=ms)
    return started, ended


def report_child(raw: dict) -> tuple[str, dict]:
    for rid, item in (raw.get("children") or {}).items():
        if item.get("run_type") == "report":
            return rid, item
    raise SystemExit("no report child")


def purpose_from_call(call: dict) -> str:
    req = call.get("request_json") or {}
    return str(req.get("purpose") or call.get("purpose") or "")


def schema_name(call: dict) -> str:
    req = call.get("request_json") or {}
    return str(((req.get("schema") or {}).get("name")) or "")


def main() -> None:
    rows = [json.loads(line) for line in (ROOT / f"{PREFIX}.jsonl").read_text().splitlines() if line]
    by_id = {row["id"]: row for row in rows}
    for case in CASES:
        row = by_id[case]
        raw = json.loads((ROOT / f"{PREFIX}-raw" / f"{case}-1.json").read_text())
        report_id, report = report_child(raw)
        trace = report["trace"]
        planned = []
        for step in trace.get("steps") or []:
            out = step.get("output_json") or {}
            if isinstance(out, dict) and out.get("planned"):
                planned = out["planned"]
                break
        kinds = [(t.get("id"), t.get("kind"), t.get("name"), t.get("depends_on")) for t in planned]
        q = [t for t in planned if str(t.get("id") or "").startswith("q")]
        s = [t for t in planned if str(t.get("id") or "").startswith("s")]
        metrics_q = [
            t for t in q
            if any(h in str(t.get("name") or t.get("question") or "") for h in METRICS_HINTS)
        ]

        children = []
        for rid, item in (raw.get("children") or {}).items():
            child_run = (item.get("trace") or {}).get("run") or {}
            rtype = item.get("run_type") or child_run.get("run_type")
            if rtype not in {"data", "tool"}:
                continue
            inp = child_run.get("input_json") or {}
            children.append({
                "id": rid,
                "type": rtype,
                "name": inp.get("name") or inp.get("tool_name"),
                "task_id": inp.get("task_id"),
                "status": item.get("status") or child_run.get("status"),
                "started": parse_ts(child_run.get("started_at")),
                "ended": parse_ts(child_run.get("ended_at") or child_run.get("updated_at")),
            })

        data = [c for c in children if c["type"] == "data"]
        search = [c for c in children if c["type"] == "tool" or c["name"] == "web_search"]
        data_span = (
            min((c["started"] for c in data if c["started"]), default=None),
            max((c["ended"] for c in data if c["ended"]), default=None),
        )
        search_span = (
            min((c["started"] for c in search if c["started"]), default=None),
            max((c["ended"] for c in search if c["ended"]), default=None),
        )

        schemas: Counter[str] = Counter()
        purposes: Counter[str] = Counter()
        review_scores = []
        writes = []
        wizard_latency = []
        chart_calls = []
        wizard_calls = []
        section_calls = []
        for call in trace.get("model_calls") or []:
            schema = schema_name(call)
            schemas[schema or "(none)"] += 1
            purpose = purpose_from_call(call)
            if purpose:
                purposes[purpose] += 1
            started, ended = call_span(call)
            pack = {"schema": schema, "purpose": purpose, "started": started, "ended": ended, "ms": call.get("latency_ms")}
            if schema == "code_wizards":
                wizard_latency.append(call.get("latency_ms"))
                wizard_calls.append(pack)
            if schema == "code_wizard":
                wizard_latency.append(call.get("latency_ms"))
                wizard_calls.append(pack)
            if schema == "report_charts":
                chart_calls.append(pack)
            if schema == "report_review":
                resp = call.get("response_json") or {}
                inner = resp.get("json") if isinstance(resp, dict) else None
                if isinstance(inner, dict) and "score" in inner:
                    review_scores.append(inner.get("score"))
                elif isinstance(resp, dict) and "score" in resp:
                    review_scores.append(resp.get("score"))
            if schema == "report_section":
                resp = call.get("response_json") or {}
                inner = resp.get("json") if isinstance(resp, dict) else resp
                title = ""
                if isinstance(inner, dict):
                    title = str(inner.get("title") or inner.get("id") or "")
                writes.append(title or "(section)")
                section_calls.append({**pack, "title": title})

        led = row.get("ledger") or {}
        by_purpose = led.get("by_purpose") or {}
        events = {(e.get("event_type") or "") for e in (trace.get("events") or [])}
        gate = sorted(e for e in events if "gate" in e.lower() or "busy" in e.lower() or "requeue" in e.lower())

        chart_span = (
            min((c["started"] for c in chart_calls if c["started"]), default=None),
            max((c["ended"] for c in chart_calls if c["ended"]), default=None),
        )
        wizard_span = (
            min((c["started"] for c in wizard_calls if c["started"]), default=None),
            max((c["ended"] for c in wizard_calls if c["ended"]), default=None),
        )
        first_writes = [c for c in section_calls if c["title"] not in {"摘要", "风险提示", "summary", "risks", ""}][:4]
        if len(section_calls) >= 4:
            first_writes = section_calls[:4]
        write_overlap = 0.0
        if len(first_writes) >= 2:
            spans = [(c["started"], c["ended"]) for c in first_writes]
            for i, a in enumerate(spans):
                for b in spans[i + 1 :]:
                    write_overlap = max(write_overlap, overlap(*a, *b))

        print(f"== {case} ==")
        print(f"  wall {row.get('seconds')}s  server {led.get('server_seconds')}s  cost ¥{led.get('cost_cny')}  calls {led.get('model_calls')}  fail {led.get('model_failures')}")
        print(f"  planned q={len(q)} s={len(s)} metrics_named_q={len(metrics_q)} ids={[t[0] for t in kinds]}")
        print(f"  children data={len(data)} search={len(search)} search_names={[c['name'] for c in search]}")
        print(f"  data span {data_span[0]} .. {data_span[1]}")
        print(f"  search span {search_span[0]} .. {search_span[1]}")
        print(f"  overlap_s {overlap(*data_span, *search_span)}")
        if data_span[0] and search_span[0]:
            print(f"  search_started_after_data_s {seconds(data_span[0], search_span[0])}")
        print(f"  schemas {dict(schemas)}")
        print(f"  purposes {dict(purposes)}")
        print(f"  ledger_by_purpose {by_purpose}")
        print(f"  wizard_ms {wizard_latency}")
        print(f"  charts∥insights overlap_s {overlap(*chart_span, *wizard_span)}")
        print(f"  first4_write_pairwise_max_overlap_s {write_overlap}")
        print(f"  section_writes {writes}")
        print(f"  review_scores {review_scores}")
        print(f"  gate-ish events {gate}")
        print()


if __name__ == "__main__":
    main()
