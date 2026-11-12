#!/usr/bin/env python3
"""Collect agent results for the golden set through the real HTTP API (same endpoints as
scripts/load_test_chat.py) and write results.jsonl for score_golden.py / summarize_golden.py.

  # 1) before any agreement is uploaded
  ./dev py docs/experiments/golden_set/run_golden.py --phase pre --out docs/experiments/exports/golden-pre.jsonl
  # 2) upload the two agreements through the same API the desk uses, then read the extraction back
  ./dev py docs/experiments/golden_set/run_golden.py --phase ingest --out docs/experiments/exports/golden-ingest.jsonl
  # 3) cases that need the agreements
  ./dev py docs/experiments/golden_set/run_golden.py --phase post --out docs/experiments/exports/golden-post.jsonl

Besides the fields the scorer needs, every row carries the run ledger (model calls, tokens, cost,
per-purpose latency, over the parent run and all child runs), so experiment B can report latency and
cost per path from the same runs that are scored. Fields that cannot be found stay absent and the
scorer reports 'n/a' (not pass). Raw history, trace and child traces are kept under --raw-dir.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

SQL_KEYS = ("sql", "query", "statement")
PLAN_PURPOSE = "orchestrator.plan"
# Agreement PDFs ship with the demo fixtures; the partnership decides which brand they belong to.
AGREEMENTS = {
    "ps-liancai-01": "fixtures/demo/联采贸易-经销协议.pdf",
    "ps-liancai-13": "fixtures/demo/联采贸易-红牛-经销协议.pdf",
}


def walk(obj):
    """Yield (key, value) for every dict entry at any depth; strings holding JSON are parsed too."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    elif isinstance(obj, str) and obj[:1] in "{[" and len(obj) > 2:
        try:
            yield from walk(json.loads(obj))
        except ValueError:
            pass


_SQL_START = r"\s*(select|with|insert|update|delete|drop|alter|create|truncate|explain)\b"


def find_sql(*objs) -> list[str] | None:
    out: list[str] = []
    for o in objs:
        for k, v in walk(o):
            if isinstance(v, str) and any(s in str(k).lower() for s in SQL_KEYS) and re.match(_SQL_START, v, re.I):
                if v not in out:
                    out.append(v)
    return out or None


def _options_with_series(o) -> list[dict]:
    """Every ECharts option (a dict with a non-empty `series` list) under `o`, in document order."""
    found: list[dict] = []
    stack = [o]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            if isinstance(cur.get("series"), list) and cur["series"]:
                found.append(cur)
            else:
                stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and cur[:1] in "{[":
            try:
                stack.append(json.loads(cur))
            except ValueError:
                pass
    return found


def find_charts(*objs) -> list[dict]:
    """All distinct chart options. A report can carry several, and the agent may also leave an
    empty placeholder option; the scorer decides which one answers the case."""
    out: list[dict] = []
    seen: set[str] = set()
    for o in objs:
        for option in _options_with_series(o):
            key = json.dumps(option, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen:
                seen.add(key)
                out.append(option)
    return out


def find_plan(trace) -> list[dict] | None:
    """The planner's task list: the output of the `orchestrator.plan` model step."""
    for step in (trace or {}).get("steps") or []:
        if (step.get("input_json") or {}).get("purpose") != PLAN_PURPOSE:
            continue
        tasks = (step.get("output_json") or {}).get("tasks")
        if isinstance(tasks, list):
            return [
                {
                    "id": t.get("id"),
                    # Tools are labelled so the scorer can tell `data` (an agent) from `exchange_rate` (a tool).
                    "agent": t.get("name") if t.get("kind") == "agent" else f"tool:{t.get('name')}",
                    "depends_on": t.get("depends_on") or [],
                }
                for t in tasks if isinstance(t, dict) and t.get("name")
            ]
    return None


def find_columns(*objs) -> list[str] | None:
    cols: list[str] = []
    for o in objs:
        for k, v in walk(o):
            if str(k).lower() == "columns" and isinstance(v, list):
                cols += [c if isinstance(c, str) else str(c.get("name")) for c in v]
            if str(k).lower() == "rows" and isinstance(v, list) and v and isinstance(v[0], dict):
                cols += list(v[0])
    return sorted(set(cols)) or None


def last_assistant(history: list) -> str:
    for item in reversed(history):
        if isinstance(item, dict) and item.get("role") == "assistant":
            c = item.get("content") or item.get("text") or item.get("message") or ""
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return ""


def default_user(ps: str) -> str:
    return "user-haina-wu" if ps.startswith("ps-haina") else "user-liancai-chen"


def po_count(db) -> int | None:
    if db is None:
        return None
    try:
        from sqlalchemy import text
        with db.engine.connect() as c:
            return int(c.execute(text("select count(*) from purchase_orders")).scalar_one())
    except Exception:
        return None


TERMINAL = ("succeeded", "failed", "cancelled")


def run_tree(db, run_id: str) -> list[dict]:
    """The run plus every run it scheduled (data / report / po children), breadth first."""
    seen: list[dict] = []
    queue = [run_id]
    while queue:
        row = db.find_agent_run_by_id(queue.pop(0))
        if row is None:
            continue
        seen.append(row)
        queue += [child["id"] for child in db.find_runs_by_owner(row["id"])]
    return seen


def ledger(db, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-run ledger summary and the raw child traces. Cost uses the same price table as the load test."""
    from backend.llm.pricing import call_cost_cny

    runs = run_tree(db, run_id)
    if not runs:
        raise ValueError(f"run {run_id} was not found")
    calls: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}
    outputs: list[Any] = []
    for run in runs:
        trace = db.run_trace(run["org_id"], run["id"])
        if trace is None:
            raise ValueError(f"trace for run {run['id']} was not found")
        raw[run["id"]] = {"run_type": run["run_type"], "status": run["status"], "trace": trace}
        outputs.append(run.get("output_json"))
        # Orchestrator steps carry the purpose on the step; child runs only on the model events.
        purpose_by_call = {
            (e.get("payload_json") or {}).get("model_call_id"): (e.get("payload_json") or {}).get("purpose")
            for e in trace.get("events") or [] if e.get("event_type") == "model.completed"
        }
        purpose_by_step = {s["id"]: (s.get("input_json") or {}).get("purpose") for s in trace.get("steps") or []}
        for m in trace.get("model_calls") or []:
            prompt = int(m.get("prompt_tokens") or 0)
            completion = int(m.get("completion_tokens") or 0)
            cost = call_cost_cny(
                str(m.get("provider") or ""), str(m.get("model") or ""),
                prompt_tokens=prompt, completion_tokens=completion,
                cache_hit_tokens=m.get("cache_hit_tokens"), cache_miss_tokens=m.get("cache_miss_tokens"),
                cache_creation_tokens=m.get("cache_creation_tokens"), created_at=m.get("created_at"),
            )
            calls.append({
                "run_type": run["run_type"],
                "purpose": (purpose_by_call.get(m.get("id")) or purpose_by_step.get(m.get("step_id"))
                            or (m.get("request_json") or {}).get("purpose")),
                "provider": m.get("provider"), "model": m.get("model"), "status": m.get("status"),
                "latency_ms": m.get("latency_ms"), "prompt_tokens": prompt, "completion_tokens": completion,
                "cache_hit_tokens": m.get("cache_hit_tokens"), "cost_cny": cost,
                "usage_source": m.get("usage_source"),
            })
    # The open (session title) run is background work; keep it out of the path's latency and cost.
    path = [c for c in calls if c["run_type"] != "open"]
    path_runs = [r for r in runs if r["run_type"] != "open"]
    started = [r["started_at"] for r in path_runs if r.get("started_at")]
    ended = [r["ended_at"] for r in path_runs if r.get("ended_at")]
    parent = runs[0]
    summary = {
        "run_status": parent["status"],
        "queue_wait_seconds": (parent["started_at"] - parent["created_at"]).total_seconds() if parent.get("started_at") else None,
        "server_seconds": (max(ended) - min(started)).total_seconds() if started and ended else None,
        "child_runs": [{"type": r["run_type"], "status": r["status"]} for r in runs[1:]],
        "model_calls": len(path),
        "model_failures": sum(1 for c in path if c["status"] != "succeeded"),
        "model_seconds_sum": round(sum((c["latency_ms"] or 0) for c in path) / 1000, 2),
        "prompt_tokens": sum(c["prompt_tokens"] for c in path),
        "completion_tokens": sum(c["completion_tokens"] for c in path),
        "cost_cny": round(sum(c["cost_cny"] for c in path), 6),
        "unknown_usage_calls": sum(c["usage_source"] == "unknown" for c in path),
        "calls": path,
    }
    return summary, {"runs": raw, "outputs": outputs}


async def wait_for_reply(client, base, sid, uid, t0, timeout) -> list | None:
    while time.monotonic() - t0 < timeout:
        h = await client.get(f"{base}/api/sessions/{sid}")
        h.raise_for_status()
        history = h.json().get("history") or []
        if any(isinstance(x, dict) and x.get("role") == "assistant" for x in history):
            return history
        await asyncio.sleep(0.5)
    return None


async def run_one(client, base, case, repeat, args, db) -> dict[str, Any]:
    ps = case["scope"]["partnership_id"]
    uid = case["scope"].get("user_id") or default_user(ps)
    res: dict[str, Any] = {"id": case["id"], "category": case["category"], "repeat": repeat,
                           "partnership_id": ps, "user_id": uid}
    before = po_count(db)
    t0 = time.monotonic()
    try:
        r = await client.post(f"{base}/api/sessions", json={"partnership_id": ps, "user_id": uid})
        r.raise_for_status()
        sid = r.json()["id"]
        r = await client.post(f"{base}/api/sessions/{sid}/message", json={"message": case["input"]})
        r.raise_for_status()
        run_id = r.json().get("run_id")
        res.update(session_id=sid, run_id=run_id)
        history = await wait_for_reply(client, base, sid, uid, t0, args.timeout)
        if history is None:
            res["error"] = "client_timeout"
            res["seconds"] = round(time.monotonic() - t0, 2)
            return res
        res["seconds"] = round(time.monotonic() - t0, 2)  # submit -> first assistant reply, as in the load test
        res["answer"] = last_assistant(history)
        trace: Any = None
        try:
            t = await client.get(f"{base}/api/runs/{run_id}/trace", headers={"X-User-Id": uid})
            t.raise_for_status()
            trace = t.json()
        except Exception as exc:
            res["trace_error"] = type(exc).__name__
        extra: Any = {}
        outputs: list[Any] = []
        if db is not None:
            for _ in range(20):  # the reply is written one step before the run row is closed
                row = db.find_agent_run_by_id(run_id)
                if row and row["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.5)
            res["ledger"], extra = ledger(db, run_id)
            outputs = extra["outputs"]
        raw = Path(args.raw_dir)
        raw.mkdir(parents=True, exist_ok=True)
        (raw / f"{case['id']}-{repeat}.json").write_text(
            json.dumps({"history": history, "trace": trace, "children": extra.get("runs")},
                       ensure_ascii=False, indent=1, default=str))
        if trace is not None:
            # With the ledger in hand, "no SQL found" means none ran (a refusal), not "unknown".
            res["executed_sql"] = find_sql(trace, outputs) or ([] if db is not None else None)
            res["plan"] = find_plan(trace)
            if case["category"] == "report":
                res["charts"] = find_charts(trace, history, outputs)  # [] = no chart produced
                res["handoff_columns"] = find_columns(trace, outputs)
        elif case["category"] == "report":
            res["charts"] = find_charts(history, outputs)
        after = po_count(db)
        if before is not None and after is not None:
            res.update(po_count_before=before, po_count_after=after, order_created=after > before)
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:300]
        res.setdefault("seconds", round(time.monotonic() - t0, 2))
    return res


def read_contract(db, ps: str) -> dict | None:
    from sqlalchemy import text
    with db.engine.connect() as c:
        row = c.execute(text("select * from contracts where partnership_id=:p order by created_at desc limit 1"),
                        {"p": ps}).mappings().first()
        if not row:
            return None
        cid = row["id"]

        def f(v):
            return float(v) if hasattr(v, "as_tuple") else (v.isoformat() if hasattr(v, "isoformat") else v)

        contract = {k: f(row[k]) for k in ("effective_from", "term_start_date", "term_end_date", "auto_renewal",
                                           "exclusivity_type", "annual_sales_target",
                                           "annual_sales_target_currency", "status") if k in row}

        def q(s):
            return [{k: f(v) for k, v in m.items()} for m in c.execute(text(s), {"c": cid}).mappings()]

        return {"contract": contract,
                "territories": [t["country_code"] for t in q("select country_code, kind from contract_territories where contract_id=:c")
                                if not str(t.get("kind") or "").lower().startswith("exclu")],
                "discount_tiers": q("select t.from_container, t.to_container, t.discount_percent from contract_discount_tiers t "
                                    "join contract_discount_rules r on r.id=t.rule_id where r.contract_id=:c order by r.position, t.position"),
                "moqs": q("select sku, quantity from contract_moqs where contract_id=:c"),
                "prices": q("select product_sku, unit_price from contract_price_list where contract_id=:c")}


async def upload_agreement(client, base, ps: str) -> dict[str, Any]:
    """Import one agreement through the same endpoint the desk uses (PDF -> model extraction -> tables)."""
    path = Path(AGREEMENTS[ps])
    t0 = time.monotonic()
    try:
        r = await client.post(f"{base}/api/partnerships/{ps}/contracts",
                              files={"file": (path.name, path.read_bytes(), "application/pdf")},
                              data={"title": path.stem}, timeout=httpx.Timeout(600))
    except httpx.RequestError as exc:
        return {"upload_seconds": round(time.monotonic() - t0, 2), "error": type(exc).__name__}
    out = {"upload_status": r.status_code, "upload_seconds": round(time.monotonic() - t0, 2)}
    if r.status_code == 200:
        out.update(contract_id=r.json().get("id"), discount_rules=r.json().get("discount_rules"),
                   run_id=r.json().get("run_id"))
    else:
        out["error"] = r.text[:300]
    return out


def upload_agreement_inproc(db, ps: str) -> dict[str, Any]:
    """Same call the import route makes, without HTTP: for when the running API's OCR URL is wrong
    and the corrected one is passed only to this process."""
    from sqlalchemy import text

    from backend.contract import read_and_ingest
    from backend.llm import deepseek_client

    path = Path(AGREEMENTS[ps])
    with db.engine.connect() as c:
        brand_id = c.execute(text("select brand_id from partnerships where id=:p"), {"p": ps}).scalar_one()
    t0 = time.monotonic()
    try:
        rec = read_and_ingest(db, deepseek_client(), brand_id=brand_id, partnership_id=ps,
                              file_name=path.name, data=path.read_bytes(), title=path.stem)
    except Exception as exc:
        return {"upload_mode": "inproc", "upload_seconds": round(time.monotonic() - t0, 2),
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"upload_mode": "inproc", "upload_seconds": round(time.monotonic() - t0, 2),
            "contract_id": rec.contract.id, "discount_rules": len(rec.terms.discount_rules),
            "run_id": rec.ingestion_run_id}


async def main_async(args, client=None, db=None) -> list[dict]:
    cases = [json.loads(line) for line in open(args.cases, encoding="utf-8")]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    out: list[dict] = []
    if args.phase in ("ingest", "ingest-check"):
        own = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(30))
        try:
            for c in [c for c in cases if c["phase"] == "ingest"]:
                ps = c["scope"]["partnership_id"]
                row: dict[str, Any] = {"id": c["id"], "category": c["category"], "partnership_id": ps}
                if args.phase == "ingest":
                    if db is not None and read_contract(db, ps) is not None:
                        row["upload_skipped"] = "partnership already has a contract; clear it first to re-measure"
                    elif args.inproc:
                        row.update(upload_agreement_inproc(db, ps))
                    else:
                        row.update(await upload_agreement(client, args.base_url.rstrip("/"), ps))
                ex = read_contract(db, ps) if db else None
                if db is not None and row.get("run_id"):
                    try:
                        row["ledger"], _ = ledger(db, row["run_id"])
                    except Exception as exc:
                        row["ledger_error"] = f"{type(exc).__name__}: {exc}"
                row.update({"contract_extraction": ex} if ex else {"error": row.get("error") or "no contract row found"})
                out.append(row)
        finally:
            if own:
                await client.aclose()
        return out
    want = {"pre": ("any", "pre_contract"), "post": ("post_contract",)}[args.phase]
    todo = [c for c in cases if c["phase"] in want and c.get("input")]
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30))
    sem = asyncio.Semaphore(args.concurrency)

    async def go(c, i):
        async with sem:
            return await run_one(client, args.base_url.rstrip("/"), c, i, args, db)

    try:
        out = await asyncio.gather(*(go(c, i) for c in todo for i in range(1, args.repeats + 1)))
    finally:
        if own:
            await client.aclose()
    return list(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default="docs/experiments/golden_set/golden_v1.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", default="docs/experiments/exports/golden-raw")
    ap.add_argument("--base-url", default="http://api:8000")
    ap.add_argument("--phase", required=True, choices=["pre", "ingest", "ingest-check", "post"])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="keep 1: purchase_orders before/after counts assume no other writers, and B is a no-load baseline")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--inproc", action="store_true", help="ingest: call the import code directly instead of the HTTP route")
    args = ap.parse_args()
    db = None
    try:
        from backend.db import Database
        db = Database()
    except Exception as exc:
        print(f"[warn] no DB access ({type(exc).__name__}); ledger, po-count checks and contract read-back will be missing")
    rows = asyncio.run(main_async(args, db=db))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows), encoding="utf-8")
    errs = sum(1 for r in rows if r.get("error"))
    print(f"{len(rows)} runs written to {args.out}; {errs} with errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
