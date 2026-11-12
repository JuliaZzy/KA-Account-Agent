#!/usr/bin/env python3
"""Build golden_hard_v2.jsonl from golden_hard_v1.template.jsonl and the seed_v2 CSV export (v1: build_golden_hard.py).

The template holds the questions, traps and checks; every number below is computed from the data
(gold queries are sqlite dialect and only define the expected answer), and the assertions guard the
facts a case depends on (no ties, the product exists / does not exist, results are non-zero).
A few template cases are rewritten here because the seed cannot trigger the trap as drafted:
  t2s-17  every partnership has exactly one order per month, so "most orders" is a 12-way tie;
          it now asks for the month with the highest purchase amount (a unique max).
  t2s-23  the catalog name is 'Energy Drink' (the '250 ml' is unit_size), and the gold name was wrong.
  t2s-27  no partnership has more than 99 order lines and a brand has only 8 products, so
          "list all products" never exceeds what the prompt shows; listing the order lines (99 rows,
          more than the 50 the prompt carries) is the case where the answer must not claim a full list.
  t2s-28/30  a brand has 8 products in one category, so nothing is cut: kept as controls.
Definitions fixed for all cases (also written into the column comments in table_metadata.py):
  已完成 = purchase_orders.status = 'completed'; 下单月 = submitted_at; 预计到货月 = eta_date.
Usage: python build_golden_hard.py   (from docs/experiments/golden_set)
"""
import csv, json, pathlib, sqlite3

HERE = pathlib.Path(__file__).parent
DATA = HERE.parent / "raw_data_v2"
con = sqlite3.connect(":memory:")
for t in ["purchase_orders", "purchase_order_items", "products", "report_facts", "sales_reports", "partnerships", "brands"]:
    rows = list(csv.DictReader(open(DATA / f"{t}.csv", encoding="utf-8")))
    cols = list(rows[0])
    con.execute(f"create table {t} ({','.join(cols)})")
    con.executemany(f"insert into {t} values ({','.join('?' * len(cols))})",
                    [[r[c] if r[c] != "" else None for c in cols] for r in rows])
for t, cs in {"purchase_order_items": ["quantity", "case_price", "line_total"], "report_facts": ["numeric_value"]}.items():
    for c in cs:
        con.execute(f"update {t} set {c}=cast({c} as real) where {c} is not null")


def q(sql):
    return con.execute(sql).fetchall()


PO = ("FROM purchase_orders o JOIN purchase_order_items i ON i.po_id=o.id "
      "JOIN products p ON p.id=i.product_id")
cases = {}
order = []
for line in open(HERE / "golden_hard_v1.template.jsonl", encoding="utf-8"):
    c = json.loads(line)
    cases[c["id"]] = c
    order.append(c["id"])


def check(c, kind):
    return next(k for k in c["checks"] if k["type"] == kind)


def scalar(c):
    return q(c["gold_sql"])[0][0]


def done(c):
    c["notes"] = c["notes"].split("TODO(build_golden.py)")[0].strip()


# t2s-13/14/16 were filled in the template: recompute and make sure they still agree.
# seed_v2: ps-liancai-02 has no 2026 orders, so t2s-13/14 move to ps-liancai-15 (orders in every month 04..09);
# all three values are recomputed instead of trusting the template.
for cid, key in (("t2s-13", "avg_month"), ("t2s-14", "pct"), ("t2s-16", "pct")):
    c = cases[cid]
    if c["scope"]["partnership_id"] == "ps-liancai-02":
        c["scope"]["partnership_id"] = "ps-liancai-15"
        c["gold_sql"] = c["gold_sql"].replace("ps-liancai-02", "ps-liancai-15")
    v = scalar(c)
    assert v, cid
    c["expected"] = {key: v}  # the template's helper fields (monthly sums etc.) are seed_v1 numbers
    check(c, "numbers_present")["values"] = [v]
    if cid == "t2s-16":
        # the wrong answer is the percentage over all metric_keys; its total must not appear
        bad = q("SELECT ROUND(SUM(f.numeric_value),2) FROM report_facts f JOIN sales_reports r ON r.id=f.report_id "
                "WHERE r.partnership_id='ps-liancai-01' AND r.reporting_period='2026-09'")[0][0]
        check(c, "numbers_absent")["values"] = [bad]

for cid in ("t2s-15", "t2s-19", "t2s-22", "t2s-21"):
    c = cases[cid]
    v = scalar(c)
    assert v, cid
    c["expected"] = {"value": v}
    check(c, "numbers_present")["values"] = [v]
    done(c)

# t2s-18: orders placed in 2026-08 with an eta in 2026-09
c = cases["t2s-18"]
v = scalar(c)
assert v and v > 0
c["expected"] = {"count": v}
check(c, "numbers_present")["values"] = [float(v)]
done(c)

# t2s-20: the average is the same whether or not the model fans out the join, rounded to 2 places
c = cases["t2s-20"]
v = scalar(c)
c["expected"] = {"value": v}
check(c, "numbers_present")["values"] = [v]
done(c)

# t2s-17 rewritten: highest purchase amount by month
c = cases["t2s-17"]
c["input"] = "2025年哪个月的采购金额最高？是多少？"
c["gold_sql"] = ("SELECT substr(o.submitted_at,1,7) AS ym, ROUND(SUM(i.line_total),2) "
                 f"{PO} WHERE o.partnership_id='ps-liancai-01' AND substr(o.submitted_at,1,4)='2025' "
                 "GROUP BY 1 ORDER BY 2 DESC LIMIT 2")
(m1, a1), (m2, a2) = q(c["gold_sql"])
assert a1 != a2, "top two months tie"
mm = int(m1[5:])
c["expected"] = {"month": m1, "amount": a1}
c["checks"] = [
    {"type": "text_contains_any", "values": [m1, f"{m1[:4]}年{mm}月", f"{mm}月"], "hard": True},
    {"type": "numbers_present", "values": [a1], "hard": True},
    {"type": "read_only_sql", "hard": True},
]
c["tags"] = ["hard", "type-error", "text-date", "dev"]
c["notes"] = ("陷阱：submitted_at 在库里是 timestamptz，自己拼字符串（substr、to_char 当文本）或把 eta_date 当时间戳都会报错。"
              "原题问“下单最多的月份”，但种子里每个合作关系每月恰好 1 张单，会 12 个月并列，所以改问金额最高的月份。")

# t2s-31: "which month had the most orders" -- the original t2s-17 question, kept on purpose.
# In seed_v1 every partnership places exactly one order a month, so the honest answer is a tie.
# The case is built from the data: with a tie the answer must say so (and must not name one month as the
# winner); if the data is later changed so one month wins, it becomes a plain unique-answer question.
GOLD31 = ("SELECT substr(o.submitted_at,1,7) AS ym, COUNT(*) FROM purchase_orders o "
          "WHERE o.partnership_id='ps-liancai-01' AND substr(o.submitted_at,1,4)='2025' "
          "GROUP BY 1 ORDER BY 2 DESC, 1")
rows31 = q(GOLD31)
top31 = rows31[0][1]
winners = [ym for ym, n in rows31 if n == top31]
base31 = {"id": "t2s-31", "category": "text2sql", "phase": "any", "fixture": "seed_v2", "fixture_file": None,
          "scope": {"partnership_id": "ps-liancai-01", "user_id": None},
          "input": "2025年哪个月下的采购单最多？一共多少张？", "gold_sql": GOLD31}
if len(winners) > 1:
    base31.update({
        "tags": ["hard", "ambiguity", "tie", "dev"],
        "checks": [
            {"type": "text_contains_any", "values": ["并列", "相同", "一样", "均为", "都是", "每个月", "每月", "各有", "各下了", "各为", "各自", "分别", "都下了", "同样"],
             "hard": True},
            {"type": "numbers_present", "values": [float(top31)], "hard": True},
            {"type": "read_only_sql", "hard": True},
            {"type": "judge", "rubric": ["没有把某一个月当成唯一的最多", "说明了有多个月并列以及各自的张数"], "hard": False},
        ],
        "expected": {"tied_months": winners, "orders_each": top31},
        "notes": f"歧义题：{len(winners)} 个月并列（各 {top31} 张），诚实的回答要说并列，不能挑一个月当答案。"
                 "ORDER BY 2 DESC LIMIT 1 会悄悄挑出第一行。",
    })
else:
    ym = winners[0]
    mm31 = int(ym[5:])
    base31.update({
        "tags": ["hard", "tie-check", "dev"],
        "checks": [
            {"type": "text_contains_any", "values": [ym, f"{ym[:4]}年{mm31}月", f"{mm31}月"], "hard": True},
            {"type": "numbers_present", "values": [float(top31)], "hard": True},
            {"type": "read_only_sql", "hard": True},
        ],
        "expected": {"month": ym, "orders": top31},
        "notes": "数据里已有唯一的最多月份；要再测并列，把种子改回并列。",
    })
cases["t2s-31"] = base31
order.append("t2s-31")

# t2s-23: the catalog name is 'Energy Drink'; '250ml' is only in the question
c = cases["t2s-23"]
c["gold_sql"] = ("SELECT SUM(i.quantity) FROM purchase_orders o JOIN purchase_order_items i ON i.po_id=o.id "
                 "JOIN products p ON p.id=i.product_id WHERE o.partnership_id='ps-liancai-13' "
                 "AND substr(o.submitted_at,1,4)='2026' AND p.product_name_en='Energy Drink'")
v = scalar(c)
assert v
c["expected"] = {"value": v}
check(c, "numbers_present")["values"] = [v]
c["notes"] = ("陷阱：问题里是 'Energy Drink 250ml'，库里的名称是 'Energy Drink'（250 ml 在 unit_size），"
              "等值过滤为空，要放宽；但 '%Energy Drink%' 会把 'Kratingdaeng Energy Drink (Thai Style)' 也算进去。")

# t2s-22: only one product matches the short name, so drop the misleading "all pack sizes"
c = cases["t2s-22"]
c["input"] = "Activia bifidus nature 这款产品2025年采购了多少箱？"
assert q("SELECT COUNT(*) FROM products WHERE product_name_en LIKE '%bifidus nature%'")[0][0] >= 1
c["notes"] = "陷阱：product_name_en = 'Activia bifidus nature' 返回空，要改成模糊匹配。"

# t2s-21: the trap in the template (a B1 PO issue) does not exist on the query path; keep as an easy control
c = cases["t2s-21"]
c["tags"] = [t for t in c["tags"] if t != "hard"] + ["control"]
c["notes"] = "对照：显式给出 SKU 和名称。此前的“显式 SKU 被判歧义”是采购单路径的问题，查询路径没有这个陷阱。"

# t2s-25 is a distributor-grain control. Users belong to the desk/session layer; purchase orders
# belong to the partnership/distributor and have no order-owner field.
c = cases["t2s-25"]
c["scope"] = {"partnership_id": "ps-liancai-01", "user_id": None}
c["gold_sql"] = ("SELECT COUNT(*) FROM purchase_orders o WHERE o.partnership_id='ps-liancai-01'")
orders = scalar(c)
assert orders > 0
c["input"] = "这个经销商一共下了多少张采购单？"
c["checks"] = [
    {"type": "numbers_present", "values": [float(orders)], "hard": True},
    {"type": "read_only_sql", "hard": True},
]
c["expected"] = {"distributor_orders": orders}
c["tags"] = ["hard", "control", "distributor-grain", "dev"]
c["notes"] = "对照：采购单按当前合作关系/经销商归属统计；工作台用户不是采购单的归属字段。"
assert not q(cases["t2s-24"]["gold_sql"]) and not q(cases["t2s-26"]["gold_sql"])

# t2s-27 rewritten: list every order line (more rows than the prompt shows)
c = cases["t2s-27"]
c["input"] = "把我们所有采购单的明细行列出来：采购单号、产品名称、箱数、金额。"
c["gold_sql"] = ("SELECT COUNT(*) FROM purchase_orders o JOIN purchase_order_items i ON i.po_id=o.id "
                 "WHERE o.partnership_id='ps-liancai-01'")
n = scalar(c)
assert n > 50, "must exceed the 50 rows a prompt carries"
c["expected"] = {"line_count": n}
check(c, "numbers_present")["values"] = [float(n)]
c["tags"] = ["hard", "truncation", "prompt-view", "dev"]
c["notes"] = (f"陷阱：明细行共 {int(n)} 行，既超过提示里能放的 50 行，也超过系统默认 LIMIT 100 和上限 200；"
              "回答必须给出总行数，不能声称列出了全部（用 50/80 做触顶实验时同样成立）。"
              "一个品牌只有 8 个产品，所以不能用“列出所有产品”触发。")

# t2s-28 control: one category, eight products
c = cases["t2s-28"]
c["gold_sql"] = ("SELECT p.category, COUNT(*) FROM products p JOIN partnerships s ON s.brand_id=p.brand_id "
                 "WHERE s.id='ps-liancai-01' GROUP BY p.category")
rows = q(c["gold_sql"])
c["expected"] = {"rows": rows}
check(c, "numbers_present")["values"] = [float(r[1]) for r in rows]
c["tags"] = ["control", "group-by", "heldout"]
c["notes"] = "对照：这个品牌只有一个品类、8 个产品，没有任何东西被截断，只检查聚合没有退步。"

# t2s-29
c = cases["t2s-29"]
scalar_lines = q("SELECT COUNT(*) FROM purchase_orders o JOIN purchase_order_items i ON i.po_id=o.id "
                 "WHERE o.partnership_id='ps-liancai-01'")[0][0]
total, avg = q(c["gold_sql"])[0]
c["expected"] = {"total": total, "avg_per_po": avg}
check(c, "numbers_present")["values"] = [total, avg]
assert scalar_lines > 200
c["notes"] = ("陷阱：这个范围的明细行有 201 行，提示只放得下 50 行；若选出明细行再让模型心算会算错，必须在 SQL 里聚合。")
assert q("SELECT COUNT(*) FROM purchase_orders o JOIN purchase_order_items i ON i.po_id=o.id "
         "WHERE o.partnership_id='ps-liancai-01'")[0][0] > 50

# t2s-30 control: the brand has few products, so a top-5 never depends on cut rows
c = cases["t2s-30"]
rows = q(c["gold_sql"])
assert rows[4][1] != rows[5][1], "5th and 6th tie"
top5 = rows[:5]
c["expected"] = {"rows": top5}
check(c, "numbers_present")["values"] = [r[1] for r in top5]
check(c, "text_contains_all")["values"] = [r[0] for r in top5]
c["tags"] = ["control", "top-n", "heldout"]
c["notes"] = "对照：这个品牌只有 8 个产品，前 5 名不依赖被截断的行；检查排序和 LIMIT 没有退步。"

# rpt-08 / rpt-09: category x quantity, and category x (amount, quantity)
def cat_measures(ps):
    return q(f"SELECT p.category, ROUND(SUM(i.line_total),2), CAST(SUM(i.quantity) AS REAL) {PO} "
             f"WHERE o.partnership_id='{ps}' AND substr(o.submitted_at,1,7)='2026-09' "
             "GROUP BY p.category ORDER BY 2 DESC")


rows = cat_measures("ps-liancai-10")
assert len(rows) == 3
c = cases["rpt-08"]
c["scope"]["partnership_id"] = "ps-liancai-10"
c["gold_sql"] = ("SELECT p.category, SUM(i.quantity) " + PO +
                 " WHERE o.partnership_id='ps-liancai-10' AND substr(o.submitted_at,1,7)='2026-09' GROUP BY p.category")
c["expected"] = {"rows": [[r[0], r[2]] for r in rows]}
check(c, "chart_values")["expected"] = {r[0]: r[2] for r in rows}
done(c)

rows = cat_measures("ps-liancai-05")
assert len(rows) == 4
c = cases["rpt-09"]
assert c["scope"]["partnership_id"] == "ps-liancai-05"
c["gold_sql"] = ("SELECT p.category, ROUND(SUM(i.line_total),2), SUM(i.quantity) " + PO +
                 " WHERE o.partnership_id='ps-liancai-05' AND substr(o.submitted_at,1,7)='2026-09' GROUP BY p.category")
c["expected"] = {"rows": [[r[0], r[1], r[2]] for r in rows]}
# mode "values" compares every plotted number, so both series go in (keys only need to be unique)
check(c, "chart_values")["expected"] = {**{r[0]: r[1] for r in rows}, **{f"{r[0]} (cases)": r[2] for r in rows}}
done(c)
c["notes"] = ("陷阱：两个度量要在同一次取数里一起交给下游，少一列图表就画不出来。"
              "chart_values 的 values 模式比较图里所有数值，所以期望值包含金额和箱数两个系列。")

# nothing may stay unfilled
for cid in order:
    c = cases[cid]
    for k in c["checks"]:
        for field in ("values", "expected"):
            if field in k:
                assert k[field] is not None, (cid, k["type"], field)
    assert "TODO" not in c["notes"], cid

with open(HERE / "golden_hard_v2.jsonl", "w", encoding="utf-8") as f:
    for cid in order:
        f.write(json.dumps(cases[cid], ensure_ascii=False) + "\n")
print(f"{len(order)} cases written")
