#!/usr/bin/env python3
"""Summarize the C knives 5–8 mixed-load run (c58-gate-w5) vs record 29.

  ./dev py docs/experiments/golden_set/summarize_c58_gate.py
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

E = Path("docs/experiments/exports")
PREFIX = "c58-gate-w5"
HEAVY = {"rpt-01", "rpt-07", "plan-02"}


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    return round(v[max(0, min(len(v) - 1, math.ceil(p * len(v)) - 1))], 2)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def queue_wait(raw: Path):
    try:
        events = json.loads(raw.read_text())["trace"]["events"]
    except Exception:
        return None
    enq = sorted(ts(e["created_at"]) for e in events if e["event_type"] == "run.enqueued")
    sta = sorted(ts(e["created_at"]) for e in events if e["event_type"] == "run.started")
    if not enq or not sta:
        return None
    return sum(max(0.0, s - q) for q, s in zip(enq, sta))


def extra_enqueues(raw: Path):
    try:
        events = json.loads(raw.read_text())["trace"]["events"]
    except Exception:
        return None
    return sum(1 for e in events if e["event_type"] == "run.enqueued")


def child_wave(raw: Path):
    try:
        blob = json.loads(raw.read_text())
    except Exception:
        return None
    items = []
    for rec in (blob.get("children") or {}).values():
        if not isinstance(rec, dict):
            continue
        child_run = (rec.get("trace") or {}).get("run") or {}
        rtype = rec.get("run_type") or child_run.get("run_type") or ""
        inp = child_run.get("input_json") or {}
        started = child_run.get("started_at")
        if not started:
            continue
        tid = str(inp.get("task_id") or "")
        name = inp.get("name") or inp.get("tool_name")
        if rtype == "data" or tid.startswith("q"):
            kind = "q"
        elif rtype == "tool" or name == "web_search" or tid.startswith("s"):
            kind = "s"
        else:
            continue
        items.append((kind, ts(started), tid or name or rtype))
    if not items:
        return None
    q = [t for kind, t, _ in items if kind == "q"]
    s = [t for kind, t, _ in items if kind == "s"]
    if not q or not s:
        return {"n_q": len(q), "n_s": len(s)}
    return {
        "n_q": len(q),
        "n_s": len(s),
        "search_lag_s": round(min(s) - min(q), 2),
        "overlap_s": round(max(0.0, max(q) - min(s)), 2) if max(q) > min(s) else 0.0,
    }


def load_part(prefix, part):
    rows = []
    f = E / f"{prefix}-{part}.jsonl"
    if not f.exists():
        return rows
    for line in f.read_text().splitlines():
        r = json.loads(line)
        raw = E / f"{prefix}-{part}-raw" / f"{r['id']}-{r['repeat']}.json"
        r["_queue"] = queue_wait(raw)
        r["_enqueues"] = extra_enqueues(raw)
        if r["id"] in HEAVY:
            r["_wave"] = child_wave(raw)
        rows.append(r)
    return rows


def summarize(prefix):
    rows = load_part(prefix, "pre") + load_part(prefix, "post")
    wall = 0
    wf = E / f"{prefix}-wall.txt"
    if wf.exists():
        wall = sum(int(x.split()[2]) - int(x.split()[1]) for x in wf.read_text().splitlines() if x.strip())
    ok = [r for r in rows if not r.get("error")]
    light = [r["seconds"] for r in ok if r["id"] not in HEAVY]
    heavy = [r["seconds"] for r in ok if r["id"] in HEAVY]
    q_light = [r["_queue"] for r in rows if r["id"] not in HEAVY and r.get("_queue") is not None]
    q_heavy = [r["_queue"] for r in rows if r["id"] in HEAVY and r.get("_queue") is not None]
    cost = sum((r.get("ledger") or {}).get("cost_cny", 0) or 0 for r in rows)
    waves = [r["_wave"] for r in rows if r.get("_wave")]
    lags = [w["search_lag_s"] for w in waves if isinstance(w, dict) and "search_lag_s" in w]
    nq = [w["n_q"] for w in waves if isinstance(w, dict) and "n_q" in w]
    return {
        "runs": len(rows),
        "errors": len(rows) - len(ok),
        "timeouts": sum(1 for r in rows if r.get("error") == "client_timeout"),
        "error_ids": [f"{r['id']}-{r['repeat']}:{r.get('error')}" for r in rows if r.get("error")],
        "wall_s": wall,
        "runs_per_min": round(len(rows) / wall * 60, 2) if wall else None,
        "light_latency": {"n": len(light), "p50": pct(light, 0.5), "p95": pct(light, 0.95), "max": max(light, default=None)},
        "heavy_latency": {"n": len(heavy), "p50": pct(heavy, 0.5), "max": max(heavy, default=None),
                          "values": sorted(round(x, 1) for x in heavy)},
        "queue_wait_light": {"n": len(q_light), "p50": pct(q_light, 0.5), "p95": pct(q_light, 0.95)},
        "queue_wait_heavy": {"n": len(q_heavy), "p50": pct(q_heavy, 0.5)},
        "parent_enqueues_gt1": sum(1 for r in rows if (r.get("_enqueues") or 0) > 1),
        "cost_cny": round(cost, 3),
        "search_lag_s": {"n": len(lags), "p50": pct(lags, 0.5), "max": max(lags, default=None)},
        "first_wave_q": {"n": len(nq), "values": nq},
        "waves": waves,
    }


def gate_peaks(path: Path):
    peaks: dict[str, int] = {}
    samples = 0
    if not path.exists():
        return {"samples": 0, "peaks": {}}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        key, n = parts[1], int(parts[2])
        peaks[key] = max(peaks.get(key, 0), n)
        samples += 1
    org = {k: v for k, v in peaks.items() if k.startswith("gate:running:org:")}
    user = {k: v for k, v in peaks.items() if k.startswith("gate:running:user:")}
    model = {k: v for k, v in peaks.items() if k.startswith("gate:model:")}
    return {
        "samples": samples,
        "global": peaks.get("gate:running:global", 0),
        "org_peaks": org,
        "user_peaks": user,
        "model_peaks": model,
        "hit_org_10": any(v >= 10 for v in org.values()),
        "hit_user_5": any(v >= 5 for v in user.values()),
        "hit_model_32": any(v >= 32 for v in model.values()),
    }


def main():
    out = {
        "c58_gate_w5": summarize(PREFIX),
        "c_gate_w5": summarize("c-gate-w5"),
        "d_w5": summarize("d-w5"),
        "gates": gate_peaks(E / f"{PREFIX}-gates.tsv"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    (E / "c58-gate-w5-summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
