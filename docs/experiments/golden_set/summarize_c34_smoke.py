"""Summarize C knives 3+4 smoke: batched insights + first-wave search."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path("docs/experiments/exports")
CASES = ["rpt-01", "rpt-07", "plan-02"]


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


def report_child(raw: dict) -> tuple[str, dict]:
    for rid, item in (raw.get("children") or {}).items():
        if item.get("run_type") == "report":
            return rid, item
    raise SystemExit("no report child")


def main() -> None:
    rows = [json.loads(line) for line in (ROOT / "c34-smoke.jsonl").read_text().splitlines() if line]
    by_id = {row["id"]: row for row in rows}
    for case in CASES:
        row = by_id[case]
        raw = json.loads((ROOT / "c34-smoke-raw" / f"{case}-1.json").read_text())
        report_id, report = report_child(raw)
        trace = report["trace"]
        run = trace["run"]
        planned = []
        for step in trace.get("steps") or []:
            out = step.get("output_json") or {}
            if isinstance(out, dict) and out.get("planned"):
                planned = out["planned"]
                break
        kinds = [(t.get("id"), t.get("kind"), t.get("name"), t.get("depends_on")) for t in planned]
        q = [t for t in planned if str(t.get("id") or "").startswith("q")]
        s = [t for t in planned if str(t.get("id") or "").startswith("s")]

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
        review_scores = []
        writes = []
        wizard_latency = []
        for call in trace.get("model_calls") or []:
            req = call.get("request_json") or {}
            schema = ((req.get("schema") or {}).get("name")) or ""
            schemas[schema or "(none)"] += 1
            if schema == "code_wizards":
                wizard_latency.append(call.get("latency_ms"))
            if schema == "code_wizard":
                wizard_latency.append(call.get("latency_ms"))
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

        led = row.get("ledger") or {}
        report_calls = [c for c in led.get("calls") or [] if c.get("run_type") == "report"]
        write_calls = [c for c in report_calls if c.get("purpose") == "report"]
        events = {(e.get("event_type") or "") for e in (trace.get("events") or [])}
        gate = sorted(e for e in events if "gate" in e.lower() or "busy" in e.lower() or "requeue" in e.lower())

        print(f"== {case} ==")
        print(f"  wall {row.get('seconds')}s  server {led.get('server_seconds')}s  cost ¥{led.get('cost_cny')}  calls {led.get('model_calls')}  fail {led.get('model_failures')}")
        print(f"  planned q={len(q)} s={len(s)}  ids={[t[0] for t in kinds]}")
        print(f"  children data={len(data)} search={len(search)} search_names={[c['name'] for c in search]}")
        print(f"  data span {data_span[0]} .. {data_span[1]}")
        print(f"  search span {search_span[0]} .. {search_span[1]}")
        print(f"  overlap_s {overlap(*data_span, *search_span)}")
        if data_span[0] and search_span[0]:
            print(f"  search_started_after_data_s {seconds(data_span[0], search_span[0])}")
            print(f"  search_ended_vs_data_end_s {seconds(data_span[1], search_span[1])}")
        print(f"  schemas {dict(schemas)}")
        print(f"  wizard_ms {wizard_latency}")
        print(f"  section_writes {writes}")
        print(f"  review_scores {review_scores}")
        print(f"  report_purpose_calls {len(write_calls)}")
        print(f"  gate-ish events {gate}")
        print()


if __name__ == "__main__":
    main()
