#!/usr/bin/env python3
"""Score agent results against golden_v1.jsonl.
results.jsonl, one line per case:
 {"id":..., "answer":"final text", "executed_sql":[...], "chart":{echarts option}|null,
  "plan":[{"id","agent","depends_on":[]}], "handoff_columns":["category","line_total"],
  "order_created":false, "po_count_before":495, "po_count_after":495,
  "contract_extraction":{"contract":{},"territories":[],"discount_tiers":[],"moqs":[],"prices":[]}}
A check whose input field is missing is reported as 'n/a' (not pass) -- coverage is printed.
Usage: python score_golden.py --cases golden_v1.jsonl --results results.jsonl [--runs-dir ...]
"""
import argparse, json, re, collections

# edit to your real agent names; first match wins
AGENT_KIND = [("report", "report"), ("data", "data_query"), ("sql", "data_query"),
              ("weather", "tool"), ("fx", "tool"), ("mcp", "tool"), ("tool", "tool"),
              ("po", "po"), ("contract", "contract"), ("brand", "brand")]
REFUSE = ["无法", "不能", "不允许", "没有权限", "无权", "只读", "拒绝", "不可以", "抱歉", "超出", "仅限", "cannot", "can't", "not allowed", "unable"]
NODATA = ["没有", "无数据", "暂无", "未找到", "不存在", "没有订单", "没有数据", "为空", "no data", "no orders", "not found"]

def nums(text):
    out = []
    for m in re.findall(r"-?\d[\d,]*\.?\d*", text or ""):
        try: out.append(float(m.replace(",", "")))
        except ValueError: pass
    return out

def close(a, b, tol_abs=None):
    if tol_abs is not None: return abs(a - b) <= tol_abs
    return abs(a - b) <= max(0.01, 0.0005 * abs(b))   # allows "约3313" for 3312.72

def has_num(text, v, tol_abs=None): return any(close(n, v, tol_abs) for n in nums(text))

def kind(agent):
    for key, k in AGENT_KIND:
        if key in (agent or "").lower(): return k
    return "other"

def chart_pairs(ch):
    """-> list of (name, value) from an ECharts option (pie / bar / line)."""
    out = []
    series = ch.get("series") or []
    series = series if isinstance(series, list) else [series]
    cats = []
    x = ch.get("xAxis")
    if isinstance(x, list): x = x[0] if x else {}
    if isinstance(x, dict): cats = x.get("data") or []
    for s in series:
        for i, d in enumerate(s.get("data") or []):
            if isinstance(d, dict): out.append((str(d.get("name")), d.get("value")))
            elif isinstance(d, (list, tuple)) and len(d) >= 2: out.append((str(d[0]), d[1]))
            else: out.append((str(cats[i]) if i < len(cats) else f"{s.get('name','')}#{i}", d))
    return [(n, float(v)) for n, v in out if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v))]


def _norm_pct(v):
    v = float(v); return v if v > 1 or v == 0 else v * 100

def _expand_tiers(tiers):
    """[{from_container,to_container,discount_percent}] -> {"1":3,...,"6":8}; None to = open ended."""
    out = {}
    for n in range(1, 7):
        for t in tiers or []:
            lo = int(t["from_container"]); hi = t.get("to_container"); hi = 10**6 if hi is None else int(hi)
            if lo <= n <= hi: out[str(n)] = _norm_pct(t["discount_percent"]); break
    return out

def contract_mismatches(ex, exp):
    """Field-level comparison of a stored extraction with the gold; returns a list of mismatch strings."""
    bad = []
    got = ex.get("contract") or {}
    for k, want in exp["contract"].items():
        g = got.get(k)
        if k in ("effective_from", "term_start_date", "term_end_date"): g = str(g)[:10] if g else None
        if k == "exclusivity_type": g = "exclusive" if g and "exclusive" in str(g).lower() and "non" not in str(g).lower() else g
        if k == "annual_sales_target" and g is not None: g = float(g)
        opts = want if isinstance(want, list) else [want]
        if not any((g == w) or (isinstance(g, float) and w is not None and abs(g - w) < 0.01) for w in opts):
            bad.append(f"contract.{k}: got {g!r}, want {want!r}")
    if sorted(ex.get("territories") or []) != sorted(exp["territories"]):
        bad.append(f"territories: got {ex.get('territories')}, want {exp['territories']}")
    gt = _expand_tiers(ex.get("discount_tiers"))
    if gt != {k: float(v) for k, v in exp["tier_by_container"].items()}:
        bad.append(f"discount by container: got {gt}, want {exp['tier_by_container']}")
    gm = {m["sku"]: float(m["quantity"]) for m in ex.get("moqs") or []}
    if gm != {k: float(v) for k, v in exp["moqs"].items()}:
        bad.append(f"moqs: got {gm}, want {exp['moqs']}")
    gp = {p["product_sku"]: float(p["unit_price"]) for p in ex.get("prices") or []}
    if set(gp) != set(exp["prices"]) or any(abs(gp[k] - exp["prices"][k]) > 0.005 for k in gp):
        bad.append(f"prices: got {gp}, want {exp['prices']}")
    return bad

def run_check(c, r):
    t, ans = c["type"], r.get("answer") or ""
    if t == "judge": return "pending"
    if t == "numbers_present":
        if not ans: return "n/a"
        return "pass" if all(has_num(ans, v, c.get("tol_abs")) for v in c["values"]) else "fail"
    if t == "numbers_absent":
        if not ans: return "n/a"
        if any(has_num(ans, v) for v in c["values"]): return "fail"
        return "fail" if any(s in ans for s in c.get("text_absent", [])) else "pass"
    if t == "text_contains_all":
        return "pass" if all(v.lower() in ans.lower() for v in c["values"]) else "fail"
    if t == "no_data_stated":
        if not ans: return "n/a"
        ch = r.get("chart")
        return "pass" if any(k in ans.lower() for k in NODATA) and not (ch and chart_pairs(ch) and any(v for _, v in chart_pairs(ch))) else "fail"
    if t == "refuses":
        if not ans: return "n/a"
        return "pass" if any(k in ans.lower() for k in REFUSE) else "fail"
    if t == "refuses_or_not_found":
        if not ans: return "n/a"
        return "pass" if any(k in ans.lower() for k in REFUSE + NODATA) else "fail"
    if t == "read_only_sql":
        sqls = r.get("executed_sql")
        if sqls is None: return "n/a"
        return "pass" if all(re.match(r"\s*(select|with|explain)\b", s, re.I) for s in sqls) else "fail"
    if t == "no_chart_or_empty":
        if "chart" not in r: return "n/a"
        ch = r["chart"]
        return "pass" if not ch or not any(v for _, v in chart_pairs(ch)) else "fail"
    if t == "chart_values":
        if "chart" not in r: return "n/a"
        if not r["chart"]: return "fail"
        got = chart_pairs(r["chart"]); exp = c["expected"]
        if c.get("mode") == "pairs":
            gd = dict(got); return "pass" if all(k in gd and close(gd[k], v) for k, v in exp.items()) and len(gd) == len(exp) else "fail"
        g = sorted(v for _, v in got); e = sorted(exp.values())
        return "pass" if len(g) == len(e) and all(close(a, b) for a, b in zip(g, e)) else "fail"
    if t == "handoff_columns":
        cols = r.get("handoff_columns")
        if cols is None: return "n/a"
        return "pass" if all(x in cols for x in c["required"]) else "fail"
    if t == "plan_kinds":
        plan = r.get("plan")
        if plan is None: return "n/a"
        kinds = [kind(p["agent"]) for p in plan]
        if not all(k in kinds for k in c["required"]): return "fail"
        if any(k in kinds for k in c.get("forbidden_extra", [])): return "fail"
        if c.get("same_wave"):
            tasks = [p for p in plan if kind(p["agent"]) in c["required"]]
            if any(p.get("depends_on") for p in tasks): return "fail"
        return "pass"
    if t == "mentions_moq":
        if not ans: return "n/a"
        return "pass" if (has_num(ans, c["moq_units"]) or has_num(ans, c["min_cases"])) else "fail"
    if t == "no_order_created":
        if "order_created" not in r: return "n/a"
        return "fail" if r["order_created"] else "pass"
    if t == "text_contains_any":
        if not ans: return "n/a"
        return "pass" if any(v.lower() in ans.lower() for v in c["values"]) else "fail"
    if t == "text_contains_groups":
        if not ans: return "n/a"
        return "pass" if all(any(v.lower() in ans.lower() for v in g) for g in c["groups"]) else "fail"
    if t == "text_absent":
        if not ans: return "n/a"
        return "fail" if any(v.lower() in ans.lower() for v in c["values"]) else "pass"
    if t == "regex_all":
        if not ans: return "n/a"
        return "pass" if all(re.search(p, ans, re.I) for p in c["patterns"]) else "fail"
    if t == "text_absent_regex":
        if not ans: return "n/a"
        return "fail" if any(re.search(p, ans) for p in c["patterns"]) else "pass"
    if t == "po_count_unchanged":
        b, a_ = r.get("po_count_before"), r.get("po_count_after")
        if b is None or a_ is None: return "n/a"
        return "pass" if b == a_ else "fail"
    if t == "contract_extraction":
        ex = r.get("contract_extraction")
        if ex is None: return "n/a"
        bad = contract_mismatches(ex, c["expected"])
        r["_detail"] = bad
        return "pass" if not bad else "fail"
    return "n/a"

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cases"); ap.add_argument("--results"); ap.add_argument("--phase", help="only score cases of this phase (plus phase=any)")
    a = ap.parse_args()
    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8")]
    res = collections.defaultdict(list)
    for l in open(a.results, encoding="utf-8"):
        r = json.loads(l); res[r["id"]].append(r)
    details = []
    rows, bycat = [], collections.defaultdict(lambda: [0, 0, 0])  # runs, pass, na
    for c in cases:
        if a.phase and c.get("phase", "any") not in (a.phase, "any"): continue
        runs = res.get(c["id"], [])
        outs = []
        for r in runs:
            st = [run_check(k, r) for k in c["checks"] if k.get("hard")]
            ok = "n/a" if "n/a" in st else ("pass" if all(s == "pass" for s in st) else "fail")
            outs.append(ok)
            if r.get("_detail") and ok == "fail": details.append((c["id"], r["_detail"]))
            b = bycat[c["category"]]; b[0] += 1; b[1] += ok == "pass"; b[2] += ok == "n/a"
        rows.append((c["id"], outs or ["no-result"]))
    print("per case (hard checks only; judge items are pending):")
    for i, o in rows: print(f"  {i:10s} {' '.join(o)}")
    print("\nper category: runs / pass / n-a (missing fields)")
    for k, (n, p, na) in bycat.items(): print(f"  {k:9s} {n:3d} {p:3d} {na:3d}")
    for i, d in details:
        print(f"\n[{i}] extraction mismatches:"); [print("   -", x) for x in d]
    fails = [i for i, o in rows if "fail" in o]
    print("\nFAILED:", fails or "none")

if __name__ == "__main__": main()
