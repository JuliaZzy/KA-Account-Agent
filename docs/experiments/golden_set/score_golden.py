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
# The data agent aliases SUM(line_total) however it likes; any of these names carries the same column.
COLUMN_ALIASES = {"line_total": {"line_total", "total_amount", "amount", "total", "purchase_amount", "sum_line_total"},
                  "category": {"category", "product_category", "category_name"},
                  "quantity": {"quantity", "cases", "case_count", "purchase_cases", "purchase_boxes", "total_boxes",
                               "box_count", "boxes", "total_quantity", "total_cases", "total_quantity_boxes"}}
REFUSE = ["无法", "不能", "不允许", "没有权限", "无权", "只读", "拒绝", "不可以", "抱歉", "超出", "仅限", "看不到", "只能查看", "cannot", "can't", "not allowed", "unable"]
NODATA = ["没有", "无数据", "暂无", "未找到", "不存在", "没有订单", "没有数据", "为空", "no data", "no orders", "not found",
          # the agent's own phrasings ("没拿到 / 查不到 / 未查到") and "please attach the file" for a missing agreement
          "没拿到", "未拿到", "没查到", "查不到", "未查到", "查无", "请附上", "请上传", "未提供",
          # "无匹配记录 / 未返回…数据" is how the report path words an empty month
          "无匹配", "没有匹配", "未返回"]

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

def _charts(r):
    """All charts of a result, or None when the collector did not look (old `chart` field still accepted)."""
    if "charts" in r: return r["charts"] or []
    if "chart" in r: return [r["chart"]] if r["chart"] else []
    return None

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

def _target_amount(v):
    """The stored target is a small object: {"amount": N, ...} or {"years": [{"target": N, ...}, ...]}.
    A comparable number is the amount, or the first contract year's target."""
    if isinstance(v, dict):
        if v.get("amount") is not None: return float(v["amount"])
        years = v.get("years") or []
        if years and years[0].get("target") is not None: return float(years[0]["target"])
        return None
    return float(v) if v is not None else None

def contract_mismatches(ex, exp):
    """Field-level comparison of a stored extraction with the gold; returns a list of mismatch strings."""
    bad = []
    got = ex.get("contract") or {}
    for k, want in exp["contract"].items():
        g = got.get(k)
        if k in ("effective_from", "term_start_date", "term_end_date"): g = str(g)[:10] if g else None
        if k == "exclusivity_type": g = "exclusive" if g and "exclusive" in str(g).lower() and "non" not in str(g).lower() else g
        if k == "annual_sales_target": g = _target_amount(g)
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
    # Bold/code marks are presentation, not content: "**不会** 自动续约" must read as "不会 自动续约".
    # The mark often leaves a stray space next to Chinese characters ("2031 年 9 月"), which is not content either.
    t, ans = c["type"], re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+|[ \t]+(?=[\u4e00-\u9fff])", "", re.sub(r"[*`]", "", r.get("answer") or ""))
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
        charts = _charts(r) or []
        # allow_charts: a report may still show other months; the case then pairs this check with
        # no_pie_with_values so the requested chart cannot be fabricated.
        drawn = False if c.get("allow_charts") else any(any(v for _, v in chart_pairs(ch)) for ch in charts)
        return "pass" if any(k in ans.lower() for k in NODATA) and not drawn else "fail"
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
    if t == "no_pie_with_values":
        # A month with no data may still carry trend charts built from other months; what it must not
        # do is draw the pie that was asked for out of nothing.
        charts = _charts(r)
        if charts is None: return "n/a"
        def pie_with_values(ch):
            series = ch.get("series") or []
            series = series if isinstance(series, list) else [series]
            if not any(s.get("type") == "pie" for s in series): return False
            # A pie split by month (names like 2026-05) is a trend of other months, not the requested split.
            return any(v and not re.fullmatch(r"\d{4}-\d{2}", n) for n, v in chart_pairs(ch))
        return "fail" if any(pie_with_values(ch) for ch in charts) else "pass"
    if t == "no_chart_or_empty":
        charts = _charts(r)
        if charts is None: return "n/a"
        return "pass" if not any(any(v for _, v in chart_pairs(ch)) for ch in charts) else "fail"
    if t == "chart_values":
        charts = _charts(r)
        if charts is None: return "n/a"
        exp = c["expected"]
        def matches(ch):
            got = chart_pairs(ch)
            if c.get("mode") == "pairs":
                gd = dict(got); return all(k in gd and close(gd[k], v) for k, v in exp.items()) and len(gd) == len(exp)
            g = sorted(v for _, v in got); e = sorted(exp.values())
            return len(g) == len(e) and all(close(a, b) for a, b in zip(g, e))
        # A report may carry several charts (and an empty placeholder): one matching chart is enough.
        return "pass" if any(matches(ch) for ch in charts) else "fail"
    if t == "handoff_columns":
        cols = r.get("handoff_columns")
        if cols is None: return "n/a"
        return "pass" if all(any(a in cols for a in COLUMN_ALIASES.get(x, {x})) for x in c["required"]) else "fail"
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
    ap = argparse.ArgumentParser(); ap.add_argument("--cases"); ap.add_argument("--results"); ap.add_argument("--phase", help="only score cases of this phase (plus phase=any)"); ap.add_argument("--split", help="golden_all_v2 only: comma-separated splits to score (default: all that have results)")
    a = ap.parse_args()
    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8")]
    res = collections.defaultdict(list)
    for l in open(a.results, encoding="utf-8"):
        r = json.loads(l); res[r["id"]].append(r)
    details = []
    rows, bycat = [], collections.defaultdict(lambda: [0, 0, 0])  # runs, pass, na
    for c in cases:
        if a.phase and c.get("phase", "any") not in (a.phase, "any"): continue
        if a.split and c.get("split", "core") not in a.split.split(","): continue
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
