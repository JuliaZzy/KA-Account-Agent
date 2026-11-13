#!/usr/bin/env python3
"""Experiment D summary: latency, queue wait, throughput and failures per worker count.

  python3 summarize_d.py 3 5 10        # reads docs/experiments/exports/d-w<N>-{pre,post}.jsonl and -raw/

Queue wait is run.enqueued -> run.started on the parent run, summed over its (up to two) enqueues.
Heavy = full monthly reports (rpt-01, rpt-07, plan-02); everything else is light.
Latency is submit -> first reply from the client, so it already includes the queue wait.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

E = Path("docs/experiments/exports")
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


out = {}
for w in sys.argv[1:]:
    rows, waits = [], {"light": [], "heavy": []}
    wall = 0
    for part in ("pre", "post"):
        f = E / f"d-w{w}-{part}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            r = json.loads(line)
            rows.append(r)
            kind = "heavy" if r["id"] in HEAVY else "light"
            q = queue_wait(E / f"d-w{w}-{part}-raw" / f"{r['id']}-{r['repeat']}.json")
            if q is not None:
                waits[kind].append(q)
    wf = E / f"d-w{w}-wall.txt"
    if wf.exists():
        wall = sum(int(x.split()[2]) - int(x.split()[1]) for x in wf.read_text().splitlines() if x.strip())
    ok = [r for r in rows if not r.get("error")]
    light = [r["seconds"] for r in ok if r["id"] not in HEAVY]
    heavy = [r["seconds"] for r in ok if r["id"] in HEAVY]
    cost = sum((r.get("ledger") or {}).get("cost_cny", 0) or 0 for r in rows)
    out[w] = {
        "runs": len(rows), "errors": len(rows) - len(ok),
        "timeouts": sum(1 for r in rows if r.get("error") == "client_timeout"),
        "wall_s": wall, "runs_per_min": round(len(rows) / wall * 60, 2) if wall else None,
        "light_latency": {"n": len(light), "p50": pct(light, .5), "p95": pct(light, .95), "max": max(light, default=None)},
        "heavy_latency": {"n": len(heavy), "p50": pct(heavy, .5), "max": max(heavy, default=None)},
        "queue_wait_light": {"n": len(waits["light"]), "p50": pct(waits["light"], .5), "p95": pct(waits["light"], .95)},
        "queue_wait_heavy": {"n": len(waits["heavy"]), "p50": pct(waits["heavy"], .5)},
        "cost_cny": round(cost, 3),
    }
print(json.dumps(out, ensure_ascii=False, indent=1))
Path(E / "d-summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
