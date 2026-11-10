#!/usr/bin/env python3
"""Collect agent results for the golden set through the real HTTP API (same endpoints as
scripts/load_test_chat.py), and write results.jsonl for score_golden.py.

  # 1) before uploading any agreement
  ./dev py scripts/run_golden.py --phase pre  --repeats 3 --out docs/experiments/exports/golden-pre.jsonl
  # 2) upload the two agreements in the UI, confirm them, then read the stored extraction back
  ./dev py scripts/run_golden.py --phase ingest-check --out docs/experiments/exports/golden-ingest.jsonl
  # 3) cases that need the agreements
  ./dev py scripts/run_golden.py --phase post --repeats 3 --out docs/experiments/exports/golden-post.jsonl

Fields it can't find stay absent, and the scorer then reports 'n/a' (not pass).
Every run's raw history + trace is saved under --raw-dir so extraction can be fixed after looking at a real trace.
"""
from __future__ import annotations
import argparse, asyncio, json, re, time
from pathlib import Path
from typing import Any
import httpx

SQL_KEYS = ("sql", "query", "statement")
PLAN_KEYS = ("plan", "tasks", "task_plan")


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


def find_sql(*objs) -> list[str] | None:
    out: list[str] = []
    for o in objs:
        for k, v in walk(o):
            if isinstance(v, str) and any(s in str(k).lower() for s in SQL_KEYS) and re.match(r"\s*(select|with|insert|update|delete|drop|alter|create|truncate|explain)\b", v, re.I):
                if v not in out: out.append(v)
    return out or None


def find_chart(*objs) -> dict | None:
    for o in objs:
        for k, v in walk(o):
            if k == "series" and isinstance(v, list) and v:
                # re-find the parent dict: walk() gave us the series list only, so rebuild minimal option
                return _parent_with_series(o)
    return None


def _parent_with_series(o):
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get("series"), list) and cur["series"]: return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and cur[:1] == "{":
            try: stack.append(json.loads(cur))
            except ValueError: pass
    return None


def find_plan(*objs) -> list[dict] | None:
    for o in objs:
        for k, v in walk(o):
            if str(k).lower() in PLAN_KEYS and isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                items = []
                for x in v:
                    agent = x.get("agent") or x.get("agent_name") or x.get("agent_type") or x.get("type")
                    if agent: items.append({"id": x.get("id") or x.get("task_id"), "agent": str(agent),
                                            "depends_on": x.get("depends_on") or x.get("dependencies") or []})
                if items: return items
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
    if db is None: return None
    try:
        from sqlalchemy import text
        with db.engine.connect() as c:
            return int(c.execute(text("select count(*) from purchase_orders")).scalar_one())
    except Exception:
        return None


async def run_one(client, base, case, repeat, args, db) -> dict[str, Any]:
    ps, uid = case["scope"]["partnership_id"], case["scope"].get("user_id") or default_user(case["scope"]["partnership_id"])
    res: dict[str, Any] = {"id": case["id"], "repeat": repeat, "partnership_id": ps, "user_id": uid}
    before = po_count(db)
    t0 = time.monotonic()
    try:
        r = await client.post(f"{base}/api/sessions", json={"partnership_id": ps, "user_id": uid}); r.raise_for_status()
        sid = r.json()["id"]
        r = await client.post(f"{base}/api/sessions/{sid}/message", json={"message": case["input"]}); r.raise_for_status()
        run_id = r.json().get("run_id"); res.update(session_id=sid, run_id=run_id)
        history: list = []
        while time.monotonic() - t0 < args.timeout:
            h = await client.get(f"{base}/api/sessions/{sid}"); h.raise_for_status()
            history = h.json().get("history") or []
            if any(isinstance(x, dict) and x.get("role") == "assistant" for x in history): break
            await asyncio.sleep(0.5)
        else:
            res["error"] = "client_timeout"; return res
        res["answer"] = last_assistant(history)
        trace: Any = None
        try:
            t = await client.get(f"{base}/api/runs/{run_id}/trace", headers={"X-User-Id": uid})
            t.raise_for_status(); trace = t.json()
        except Exception as exc:
            res["trace_error"] = type(exc).__name__
        raw = Path(args.raw_dir); raw.mkdir(parents=True, exist_ok=True)
        (raw / f"{case['id']}-{repeat}.json").write_text(json.dumps({"history": history, "trace": trace}, ensure_ascii=False, indent=1))
        if trace is not None:
            res["executed_sql"] = find_sql(trace)
            res["plan"] = find_plan(trace)
            if case["category"] == "report":
                res["chart"] = find_chart(trace, history)     # None = no chart produced
                res["handoff_columns"] = find_columns(trace)
        elif case["category"] == "report":
            res["chart"] = find_chart(history)
        after = po_count(db)
        if before is not None and after is not None:
            res.update(po_count_before=before, po_count_after=after, order_created=after > before)
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:300]
    res["seconds"] = round(time.monotonic() - t0, 2)
    return res


def read_contract(db, ps: str) -> dict | None:
    from sqlalchemy import text
    with db.engine.connect() as c:
        row = c.execute(text("select * from contracts where partnership_id=:p order by created_at desc limit 1"), {"p": ps}).mappings().first()
        if not row: return None
        cid = row["id"]
        f = lambda v: float(v) if hasattr(v, "as_tuple") else (v.isoformat() if hasattr(v, "isoformat") else v)
        contract = {k: f(row[k]) for k in ("effective_from", "term_start_date", "term_end_date", "auto_renewal", "exclusivity_type",
                                           "annual_sales_target", "annual_sales_target_currency", "status") if k in row}
        q = lambda s: [{k: f(v) for k, v in m.items()} for m in c.execute(text(s), {"c": cid}).mappings()]
        return {"contract": contract,
                "territories": [t["country_code"] for t in q("select country_code, kind from contract_territories where contract_id=:c")
                                if not str(t.get("kind") or "").lower().startswith("exclu")],
                "discount_tiers": q("select t.from_container, t.to_container, t.discount_percent from contract_discount_tiers t "
                                    "join contract_discount_rules r on r.id=t.rule_id where r.contract_id=:c order by r.position, t.position"),
                "moqs": q("select sku, quantity from contract_moqs where contract_id=:c"),
                "prices": q("select product_sku, unit_price from contract_price_list where contract_id=:c")}


async def main_async(args, client=None, db=None) -> list[dict]:
    cases = [json.loads(l) for l in open(args.cases, encoding="utf-8")]
    if args.only: cases = [c for c in cases if c["id"] in args.only.split(",")]
    out: list[dict] = []
    if args.phase == "ingest-check":
        for c in [c for c in cases if c["phase"] == "ingest"]:
            ex = read_contract(db, c["scope"]["partnership_id"]) if db else None
            out.append({"id": c["id"], **({"contract_extraction": ex} if ex else {"error": "no contract row found"})})
        return out
    want = {"pre": ("any", "pre_contract"), "post": ("post_contract",)}[args.phase]
    todo = [c for c in cases if c["phase"] in want and c.get("input")]
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30))
    sem = asyncio.Semaphore(args.concurrency)
    async def go(c, i):
        async with sem: return await run_one(client, args.base_url.rstrip("/"), c, i, args, db)
    try:
        out = await asyncio.gather(*(go(c, i) for c in todo for i in range(1, args.repeats + 1)))
    finally:
        if own: await client.aclose()
    return list(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default="eval/datasets/golden_v1.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", default="docs/experiments/exports/golden-raw")
    ap.add_argument("--base-url", default="http://api:8000")
    ap.add_argument("--phase", required=True, choices=["pre", "ingest-check", "post"])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=1, help="keep 1: purchase_orders before/after counts assume no other writers")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--only", help="comma-separated case ids")
    args = ap.parse_args()
    db = None
    try:
        from backend.db import Database
        db = Database()
    except Exception as exc:
        print(f"[warn] no DB access ({type(exc).__name__}); po-count checks and contract read-back will be n/a")
    rows = asyncio.run(main_async(args, db=db))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows), encoding="utf-8")
    errs = sum(1 for r in rows if r.get("error"))
    print(f"{len(rows)} runs written to {args.out}; {errs} with errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
