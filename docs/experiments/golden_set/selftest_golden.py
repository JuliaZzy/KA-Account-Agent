#!/usr/bin/env python3
"""Sanity-check the scorer itself: synthetic perfect answers must all pass; deliberately wrong ones must fail.
Usage: python selftest_golden.py docs/experiments/golden_set/golden_v1.jsonl"""
import copy, json, subprocess, sys, tempfile, os
cases = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]

def tiers_from(m):  # build discount_tiers rows from tier_by_container
    rows, last = [], None
    for n in range(1, 7):
        pct = m[str(n)]
        if rows and rows[-1]["discount_percent"] == pct: rows[-1]["to_container"] = n
        else: rows.append({"from_container": n, "to_container": n, "discount_percent": pct})
    rows[-1]["to_container"] = None
    return rows

def good(c):
    e = c["expected"] or {}; r = {"id": c["id"], "executed_sql": ["SELECT 1"], "po_count_before": 495, "po_count_after": 495}
    ans, chart = [], "chart" in json.dumps(c["checks"])
    for k in c["checks"]:
        t = k["type"]
        if t == "numbers_present": ans.append(" ".join(f"{v:,.2f}" for v in k["values"]))
        if t == "text_contains_all": ans.append(" ".join(k["values"]))
        if t == "text_contains_any": ans.append(k["values"][0])
        if t == "text_contains_groups": ans.append(" ".join(g[0] for g in k["groups"]))
        if t == "regex_all": ans.append("折扣 8%")
        if t == "refuses" or t == "refuses_or_not_found": ans.append("抱歉，我无法提供，没有权限，也没有找到相关资料")
        if t == "no_data_stated": ans.append("没有数据")
        if t == "mentions_moq": ans.append(f"最小起订量 {k['moq_units']} 件，至少 {k['min_cases']} 箱")
        if t == "chart_values":
            d = k["expected"]
            r["chart"] = ({"xAxis": {"data": list(d)}, "series": [{"data": list(d.values())}]} if k["mode"] == "pairs"
                          else {"series": [{"type": "pie", "data": [{"name": n, "value": v} for n, v in d.items()]}]})
        if t == "handoff_columns": r["handoff_columns"] = k["required"] + ["submitted_month"]
        if t in ("no_chart_or_empty", "no_pie_with_values"): r["chart"] = None
        if t == "plan_kinds":
            r["plan"] = [{"id": str(i), "agent": {"data_query": "data_query", "tool": "fx_tool", "report": "report"}[x], "depends_on": []} for i, x in enumerate(k["required"])]
        if t == "contract_extraction":
            g = k["expected"]
            r["contract_extraction"] = {"contract": {kk: (v[0] if isinstance(v, list) else v) for kk, v in g["contract"].items()},
                "territories": g["territories"], "discount_tiers": tiers_from(g["tier_by_container"]),
                "moqs": [{"sku": s, "quantity": q} for s, q in g["moqs"].items()],
                "prices": [{"product_sku": s, "unit_price": p} for s, p in g["prices"].items()]}
    r["answer"] = " ".join(ans) or "好的"
    return r

G = [good(c) for c in cases if c.get("input") or c["phase"] == "ingest"]
B = copy.deepcopy(G)
wrong = {"t2s-06": lambda r: r.update(answer="销售总额 4400.93"),
         "t2s-08": lambda r: r.update(answer="2026年10月采购金额为 1234.56"),
         "saf-01": lambda r: r.update(answer="已删除", executed_sql=["DELETE FROM purchase_orders"], po_count_after=494),
         "saf-02": lambda r: r.update(answer="Barilla 共 1973.77"),
         "rpt-01": lambda r: r.update(handoff_columns=["line_total"]),
         "rpt-07": lambda r: r.update(chart={"series": [{"data": [{"name": "x", "value": 100}]}]}),
         "po-02": lambda r: r.update(answer="应付 104.74 美元（8% 折扣）"),
         "po-03": lambda r: r.update(answer="应付 136.71 美元", po_count_after=496),
         "con-qa-06": lambda r: r.update(answer="Kratingdaeng 最小起订量 240"),
         "con-qa-02": lambda r: r.update(answer="2031-09-30 到期，将自动续展一年")}
def ext_bad(r):  # Red Bull read as auto-renewing, HK missed, wrong tier
    x = r["contract_extraction"]; x["contract"]["auto_renewal"] = True; x["territories"] = ["CN"]
    x["discount_tiers"] = tiers_from({"1": 3, "2": 5, "3": 8, "4": 8, "5": 8, "6": 8})
wrong["con-ext-02"] = ext_bad
for r in B:
    if r["id"] in wrong: wrong[r["id"]](r)
d = tempfile.mkdtemp()
for name, rows in (("good", G), ("bad", B)):
    open(f"{d}/{name}.jsonl", "w", encoding="utf-8").write("\n".join(json.dumps(x, ensure_ascii=False) for x in rows))
    out = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__) or ".", "score_golden.py"), "--cases", sys.argv[1],
                          "--results", f"{d}/{name}.jsonl"], capture_output=True, text=True).stdout
    print(f"=== {name} ===\n" + "\n".join(out.splitlines()[-12:] if name == "good" else [l for l in out.splitlines() if l.startswith("FAILED") or "mismatch" in l or l.startswith("   -")]))
exp_fail = sorted(wrong)
print("\nexpected to fail:", exp_fail)
