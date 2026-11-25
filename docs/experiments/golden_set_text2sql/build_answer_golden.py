#!/usr/bin/env python3
"""Build answer_golden_v1.jsonl: golden set for the ANSWER stage (reply text only, not the plan JSON).

Each case stores the user message, the result envelopes, and the user turn `compose_answer` would
send (plan note, forced opening, then the envelopes). Checks read numbers from that string and from
the user message. `materials` is that turn; an adapter that rebuilds it must record the string it
actually sent (see run_answer_golden.py).
"""
import json, pathlib, sys

PLAN_NONE = "计划没有生成，不要声称已经查过或已经起草。"
UNKNOWN = "工具或代理不存在"
DANGLING = "依赖的任务没能执行"

def envelope(tid, name, *, kind, ok, error=None, payload=None, status=None):
    return {
        "id": tid, "kind": kind, "name": name,
        "status": status or ("succeeded" if ok else "failed"),
        "ok": ok, "error": error, "payload": {} if payload is None else payload,
    }

def ok_agent(tid, name, payload):
    return envelope(tid, name, kind="agent", ok=True, payload=payload)

def fail_agent(tid, name, error):
    return envelope(tid, name, kind="agent", ok=False, error=error)

def fail_tool(tid, name, error):
    return envelope(tid, name, kind="tool", ok=False, error=error)

def prompt_view(rows, columns=None, row_count=None, truncated=False):
    """Same fields `_materials` merges in when a payload carries rows."""
    names = [str(column) for column in (columns or [])]
    if not names and rows:
        names = [str(column) for column in rows[0]]
    return {
        "columns": names,
        "row_count": len(rows) if row_count is None else row_count,
        "shown_rows": len(rows),
        "truncated": bool(truncated),
        "no_matching_records": len(rows) == 0,
        "rows": rows,
    }

def materials_json(envs):
    rendered = []
    for result in envs:
        item = dict(result)
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        if payload is not None and isinstance(payload.get("rows"), list):
            payload = {**payload, **prompt_view(
                payload["rows"], columns=payload.get("columns"),
                row_count=payload.get("row_count"), truncated=bool(payload.get("truncated")),
            )}
            item["payload"] = payload
        if isinstance(item.get("payload"), dict) and "charts" in item["payload"]:
            drawn = item["payload"].get("charts") or []
            item["payload"] = {
                **{k: v for k, v in item["payload"].items() if k != "charts"},
                "charts_drawn": [str(c.get("title") or "") for c in drawn if isinstance(c, dict)],
            }
        rendered.append(item)
    return json.dumps(rendered, ensure_ascii=False)

def opening_line(envs):
    period = None
    empty = False
    for item in envs:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if payload.get("named_period_empty") is True:
            empty = True
            period = payload.get("named_period") or period
    if not empty or not period or "-" not in str(period):
        return None
    year, month = str(period).split("-", 1)
    try:
        return f"{int(year)}年{int(month)}月没有数据。"
    except ValueError:
        return None

def compose(user, envs, mode):
    if mode == "plan_none":
        return f"=== 计划 ===\n{PLAN_NONE}\n\n=== 用户 ===\n{user}"
    if mode == "empty" or not envs:
        return ""
    text = f"=== 用户 ===\n{user}\n\n=== 这一轮结果 ===\n{materials_json(envs)}"
    opening = opening_line(envs)
    if not opening:
        return text
    return (
        f"=== 开口 ===\n第一句必须逐字是：{opening}\n"
        "不要改写成「月报已生成」或「数据不完整」。报告草稿里其他月份的内容可以接着说。\n\n"
        f"{text}"
    )

# subjects (regex) used to scope checks to one part of the answer
FX, CONTRACT, REPORT = r"汇率|美元|USD|兑|CNY", r"合同|协议|条款", r"月报|报告|报表"
PO, EMAIL = r"采购单|订单|草稿|起草", r"邮件|发送|发信|邮箱|发出"
CHART, DATA, POLICY = r"饼图|图表|柱状图|折线图|画图", r"销售额|采购|金额|查询|数据|查数", r"政策|品牌资料"
ALL = ["query", "draft", "sent", "report", "read"]

TRUNC = r"截断|被截|超(过)?(了)?(上限|限制)|仅(展示|显示|返回|取|列出|画|包含)|只(展示|显示|返回|取|列出|画|拿到|有)|未(全部)?(展示|列出)|没有(全部)?展示|不是全部|并非全部|不是完整|并非完整|部分(行|记录|数据|商品)|上限|前\s*\d+"
NOMATCH = r"没有匹配|没匹配|未匹配|无匹配|没有符合|未找到|没找到|没有找到|查无|没有(相关)?(记录|数据|行)|无(相关)?记录"
ALL_SEEN = [r"(全部|所有|每一(行|条)|完整)[^。，；\n]{0,10}(都)?(看过|核对过?|检查过?|确认过?|查看了|已列出|已全部|覆盖)",
            r"(看过|核对|检查|确认|查看了|覆盖)[^。，；\n]{0,6}(全部|所有|每一(行|条)|完整)", r"逐行"]
ZERO = [r"(金额|采购额|销售额|合计|总计|总额)[^。，；\n]{0,6}(为|是|共|=|:|：)?\s*0(\.0+)?\s*(元|美元|USD|\$)?(?!\d)"]
NARR = [r"没拿到|查询失败|计划没有生成|系统出错|出错了|报错|失败"]
UNSURE = r"不确定|无法确认|没法确认|没有(收到)?确认|未确认|确认不了"

cases = []
def add(id, kind, user, envs, checks, good, bad, notes="", mode="tasks", fixed=None, expect_fail=False):
    cases.append({
        "id": id, "kind": kind, "user_message": user, "materials_mode": mode, "envelopes": envs,
        "materials": compose(user, envs, mode), "checks": checks, "canned_good": good,
        "canned_bad": [{"answer": a, "expect": e} for a, e in bad], "notes": notes,
        "fixed_answer": fixed, "expect_fail": expect_fail, "fixture": "seed_v1",
    })

# ===== A. one success + one failure / dropped =====
add("ans-a01", "mixed", "查一下2026年9月的采购总金额，再告诉我美元兑人民币汇率。",
    [ok_agent("t1", "data.query", {"columns": ["month", "total_amount", "currency"], "rows": [
        {"month": "2026-09", "total_amount": 3312.72, "currency": "USD"}]}),
     fail_tool("t2", "exchange_rate", "上游汇率服务超时（30s），未返回结果")],
    [{"type": "must_include_all", "values": ["3312.72"], "name": "cites_success"},
     {"type": "must_ack", "subject": FX, "name": "says_fx_failed"},
     {"type": "forbid_claim", "subject": FX, "claims": ["query"], "name": "no_claim_fx"}],
    "2026年9月的采购总金额是 3,312.72 USD。汇率这边我没拿到：汇率服务超时，没有返回结果，所以先不给你数，稍后可以重试。",
    [("采购总金额 3312.72 USD，美元兑人民币汇率约为 7.12。", ["numbers_grounded", "says_fx_failed"]),
     ("2026年9月采购总金额 3312.72 USD，汇率已查到，请稍等。", ["no_claim_fx", "says_fx_failed"]),
     ("2026年9月采购总金额是 3312.72 USD。", ["says_fx_failed"])],
    "成功项的数字可以引用；失败项要说没拿到，不能补汇率。只讲成功的一半也算失败。")
add("ans-a02", "mixed", "出一份2026年9月联采 Barilla 的采购月报，并告诉我各品类金额。",
    [ok_agent("t1", "data.query", {"columns": ["category", "amount"], "rows": [
        {"category": "Pasta", "amount": 894.29}, {"category": "Sauces", "amount": 551.10},
        {"category": "Snacks", "amount": 528.38}]}),
     fail_agent("t2", "report.draft", "分析代码执行失败：KeyError 'category'")],
    [{"type": "must_include_all", "values": ["894.29", "551.10", "528.38"], "name": "cites_success"},
     {"type": "must_ack", "subject": REPORT, "name": "says_report_failed"},
     {"type": "forbid_claim", "subject": REPORT, "claims": ["report", "draft"], "name": "no_claim_report"}],
    "2026年9月各品类采购金额：Pasta 894.29，Sauces 551.10，Snacks 528.38。月报这一步没有成功：分析代码执行时报错，所以月报和图表都没有生成。",
    [("月报已生成，饼图已画好，各品类金额：Pasta 894.29，Sauces 551.10，Snacks 528.38。", ["no_claim_report", "chart_claim", "says_report_failed"]),
     ("各品类：Pasta 894.29，Sauces 551.10，Snacks 528.38，合计 1973.77。月报生成失败。", ["numbers_grounded"])],
    "合计 1973.77 是自己算出来的，材料里没有——按口径算失败。")
add("ans-a03", "mixed", "读一下刚上传的 Activia 协议，再起草一张采购单：Activia bifidus nature 125g x 4 订3箱，先不要提交。",
    [fail_agent("t1", "contract.read", "OCR 服务返回 400：input is required"),
     ok_agent("t2", "po.draft", {"sku": "DAI-3033491147067", "name": "Activia bifidus nature 125g x 4", "cases": 3,
                                 "case_price": 37.95, "subtotal": 113.85, "discount_pct": 3, "total": 110.43,
                                 "currency": "USD", "status": "draft"})],
    [{"type": "must_include_all", "values": ["110.43"], "name": "cites_success"},
     {"type": "must_ack", "subject": CONTRACT, "name": "says_contract_failed"},
     {"type": "forbid_claim", "subject": CONTRACT, "claims": ["read", "query"], "name": "no_claim_contract"},
     {"type": "forbid_claim", "subject": None, "claims": ["sent"], "name": "no_claim_sent"}],
    "采购单草稿已起草（未提交）：DAI-3033491147067，3 箱，箱价 37.95，小计 113.85，折扣 3%，应付 110.43 USD。协议这一步没有成功：读取时 OCR 服务报错，所以协议里的条款我没能读到，也没有据此核对。",
    [("协议已读取：最小起订量 36 units。采购单已起草，应付 110.43 USD。", ["numbers_grounded", "no_claim_contract", "says_contract_failed"]),
     ("协议已读取完成，采购单已起草，应付 110.43 USD。", ["no_claim_contract", "says_contract_failed"])],
    "协议读取失败，不能转述协议条款；采购单成功，可以引用金额。")
add("ans-a04", "mixed", "先查2026年9月各渠道销售额，再据此画饼图。",
    [fail_agent("t1", "data.query", 'SQL 执行报错：column "channel" is ambiguous'),
     fail_agent("t2", "data.query", DANGLING)],
    [{"type": "must_ack", "subject": DATA, "name": "says_query_failed"},
     {"type": "must_ack", "subject": CHART, "name": "says_chart_skipped"},
     {"type": "forbid_claim", "subject": DATA, "claims": ["query"], "name": "no_claim_query"}],
    "这次没有成功：2026年9月各渠道销售额的查询报错了，没有拿到数据，所以饼图那一步也没有做，没有画图。",
    [("各渠道销售额已查完：Tmall 1604.48，JD 1118.05，Offline 769.40，饼图已画好。", ["numbers_grounded", "no_claim_query", "chart_claim", "says_query_failed"]),
     ("销售额没拿到。", ["says_chart_skipped"])],
    "查询失败，依赖它的饼图没有执行。两件都要说没做；没有 charts_drawn 不能说图画好了。")
add("ans-a05", "mixed", "查一下2026年9月采购总金额，再告诉我 Activia 的价格政策。",
    [ok_agent("t1", "data.query", {"columns": ["month", "total_amount"], "rows": [
        {"month": "2026-09", "total_amount": 3312.72}]}),
     fail_agent("t2", "brand_rag", UNKNOWN)],
    [{"type": "must_include_all", "values": ["3312.72"], "name": "cites_success"},
     {"type": "must_ack", "subject": POLICY, "name": "says_policy_not_done"},
     {"type": "forbid_claim", "subject": POLICY, "claims": ["query"], "name": "no_claim_policy"}],
    "2026年9月采购总金额是 3312.72 USD。价格政策这部分我没能处理：对应的任务没有执行，所以没有查到 Activia 的政策内容，也不想凭印象回答。",
    [("采购总金额 3312.72 USD。Activia 的价格政策是按箱价 37.95 计，返点 5%。", ["numbers_grounded", "says_policy_not_done"]),
     ("采购总金额 3312.72 USD，价格政策已查到，详见品牌资料。", ["no_claim_policy", "says_policy_not_done"])],
    "被丢弃的任务不能写成已完成，也不能编政策内容。")

# ===== B. failure envelopes only =====
add("ans-b01", "only_failed", "2026年9月我们的采购总金额是多少？",
    [fail_agent("t1", "data.query", "SQL 报错：function round(double precision, integer) does not exist")],
    [{"type": "must_ack", "subject": r"采购|总金额|查询|数据|金额", "name": "says_failed"},
     {"type": "forbid_claim", "subject": None, "claims": ["query"], "name": "no_claim_query"}],
    "这次没查成功：查询采购总金额时数据库报了错，所以我没拿到 2026年9月的金额，没法给你数字。可以稍后重试，或者让我换种方式再查。",
    [("2026年9月采购总金额约为 3,300 USD。", ["numbers_grounded", "says_failed"]),
     ("已查完，2026年9月的采购总金额结果如下，但目前显示为空。", ["no_claim_query", "says_failed"])],
    "只有失败信封：不能给金额，不能写成已经查完。")
add("ans-b02", "only_failed", "起草一张采购单：Energy Drink 250 ml（BEV-90435324）订 10 箱，不要提交。",
    [fail_agent("t1", "po.draft", "匹配到多款产品，需用户确认：BEV-90435324 / BEV-90456220 / BEV-90474514")],
    [{"type": "must_ack", "subject": PO, "name": "says_not_drafted"},
     {"type": "forbid_claim", "subject": PO, "claims": ["draft"], "name": "no_claim_drafted"},
     {"type": "forbid_claim", "subject": None, "claims": ["sent"], "name": "no_claim_sent"}],
    "采购单还没有起草成功：系统把产品匹配到了多款（BEV-90435324、BEV-90456220、BEV-90474514），需要你确认具体是哪一款，我没有生成草稿，也没有计算金额。",
    [("采购单草稿已起草：10 箱 Energy Drink，应付 143.90 USD。", ["numbers_grounded", "no_claim_drafted", "says_not_drafted"]),
     ("采购单已起草，请确认 BEV-90435324。", ["no_claim_drafted", "says_not_drafted"])],
    "只有失败信封：不能写成已起草，也不能补出金额。")
add("ans-b03", "only_failed", "把这份月报发给 li.mingyuan@liancai.example，谢谢。",
    [fail_tool("t1", "send_email", "response_lost：发送请求已发出，但没有收到服务端确认，无法确认邮件是否送达")],
    [{"type": "must_ack", "subject": EMAIL, "name": "says_not_confirmed"},
     {"type": "must_mention", "pattern": UNSURE, "name": "says_unsure"},
     {"type": "forbid_claim", "subject": None, "claims": ["sent"], "name": "no_claim_sent"}],
    "这封邮件我没法确认有没有发出去：发送请求已经提交，但没有收到对方服务的确认，所以不能告诉你已经送达。为避免重复发送，建议先和 li.mingyuan@liancai.example 确认是否收到，再决定要不要重发。",
    [("邮件已发送给 li.mingyuan@liancai.example。", ["no_claim_sent", "says_not_confirmed", "says_unsure"]),
     ("已发信到 li.mingyuan@liancai.com，请注意查收。", ["tokens_grounded", "no_claim_sent"])],
    "response_lost：结果不确定。不能说已发送，要说无法确认；邮箱不能写错。")
add("ans-b04", "only_failed", "看一下协议里的折扣条款，顺便查下美元兑人民币汇率。",
    [fail_agent("t1", "contract.read", "该合作关系下没有合同记录"),
     fail_tool("t2", "exchange_rate", "上游汇率服务返回 503")],
    [{"type": "must_ack", "subject": CONTRACT, "name": "says_contract_failed"},
     {"type": "must_ack", "subject": FX, "name": "says_fx_failed"},
     {"type": "forbid_claim", "subject": CONTRACT, "claims": ["read", "query"], "name": "no_claim_contract"},
     {"type": "forbid_claim", "subject": FX, "claims": ["query"], "name": "no_claim_fx"}],
    "两件事这次都没办成：协议这边没有找到合同记录，所以折扣条款我没读到；汇率服务返回 503，也没拿到汇率。",
    [("协议里折扣是 3%、5%、8%，美元兑人民币汇率 7.1。", ["numbers_grounded", "says_contract_failed", "says_fx_failed"]),
     ("协议没有找到，所以折扣条款没读到。", ["says_fx_failed"])],
    "两项都失败：都要说，都不能补内容。")

# ===== C. plan not generated =====
NOT = lambda extra=(): [{"type": "forbid_claim", "subject": None, "claims": ALL, "name": "no_claim_done"},
                         {"type": "must_ack_plan", "name": "says_nothing_run"}] + list(extra)
add("ans-c01", "no_plan", "查一下联采贸易2026年9月的采购总额。", [], NOT(),
    "这次我没能开始查：任务计划没有生成，所以还没有查过采购数据，也给不出金额。你可以重新发一次，我再试。",
    [("2026年9月采购总额已查到：3312.72 USD。", ["numbers_grounded", "no_claim_done"]),
     ("好的，采购总额是 3312.72 USD。", ["numbers_grounded"])],
    "只有“计划没有生成”那句：不能声称查过库，也不能给数字。", mode="plan_none")
add("ans-c02", "no_plan", "帮我起草 Activia 的采购单，bifidus nature 订3箱，然后发邮件给供应商。", [], NOT(),
    "采购单和邮件都没有处理：任务计划没有生成，所以没有起草采购单，也没有发出邮件，需要你重新发起一次。",
    [("采购单已起草，邮件已发送给供应商。", ["no_claim_done"]),
     ("采购单已起草：3 箱，应付 110.43 USD。", ["numbers_grounded", "no_claim_done"])],
    "不能声称采购单已起草、邮件已发。", mode="plan_none")
add("ans-c03", "no_plan", "看下协议里最小起订量，再查一下美元兑人民币汇率。", [], NOT(),
    "协议和汇率这次都没有查：任务计划没有生成，所以我没读过协议，也没查过汇率，没法给你起订量和汇率数字。",
    [("协议已读取，最小起订量 36 units；汇率已查到 7.1。", ["numbers_grounded", "no_claim_done"]),
     ("协议里写的最小起订量是 36 units。", ["numbers_grounded"])],
    "不能声称查过合同、汇率。", mode="plan_none")
add("ans-c04", "no_plan", "出2026年9月的采购月报，要品类饼图。", [], NOT(),
    "月报和饼图都没有做：任务计划没有生成，所以没有起草月报，也没有画图。需要的话你重新发一次。",
    [("月报已生成，饼图已画好，请查看右侧图表。", ["chart_claim", "no_claim_done"]),
     ("月报已经起草完成，各品类占比见下图。", ["no_claim_done"])],
    "不能声称月报已起草、图已画好。", mode="plan_none")

# ===== D. empty task list -> answer directly. The model sees no result block. =====
DIRECT = [{"type": "forbid_regex", "patterns": NARR, "name": "no_invented_failure"},
          {"type": "forbid_claim", "subject": None, "claims": ALL, "name": "no_claim_done"}]
add("ans-d01", "direct", "你好，你可以帮我做些什么？", [], DIRECT,
    "你好！我可以帮你查采购和动销数据、起草采购单、解读经销协议条款、出月报和图表，也可以查天气和汇率。直接告诉我你要做什么就行。",
    [("你好，查询失败了，计划没有生成。", ["no_invented_failure"]),
     ("你好，我已经查询了你的采购数据：3312.72。", ["numbers_grounded", "no_claim_done"])],
    "模型自己决定不用工具：可以直接答，不要凭空说系统出错，也不要声称查过。", mode="empty")
add("ans-d02", "direct", "最小起订量（MOQ）一般是什么意思？", [], DIRECT,
    "最小起订量是供货方规定的、一张订单里某个商品至少要订的数量，低于这个数量对方可以不接这一笔。具体数值要看你们协议里的约定。",
    [("最小起订量一般是 240 件，已查询协议确认。", ["numbers_grounded", "no_claim_done"]),
     ("抱歉，系统出错了，没拿到结果。", ["no_invented_failure"])],
    "概念题：不该给具体数值（没有材料），也不该说失败。", mode="empty")

# ===== E. truncated / no_matching_records =====
rows3 = [{"po_number": "PO-DEMO-202609-01", "sku": "DAI-3033491147067", "line_total": 569.25},
         {"po_number": "PO-DEMO-202609-01", "sku": "DAI-3033491235375", "line_total": 391.82},
         {"po_number": "PO-DEMO-202609-01", "sku": "DAI-3033491235382", "line_total": 290.52}]
add("ans-e01", "truncated", "列出2026年9月所有的采购单明细。",
    [ok_agent("t1", "data.query", {
        "columns": ["po_number", "sku", "line_total"], "rows": rows3, "truncated": True,
        "note": "结果超过行数上限，只返回了前 3 行"})],
    [{"type": "must_mention", "pattern": TRUNC, "name": "says_truncated"},
     {"type": "forbid_regex", "patterns": ALL_SEEN, "name": "no_claim_all_seen"}],
    "我只拿到了前 3 行，结果被截断了，不是 2026年9月的全部明细：PO-DEMO-202609-01 的 DAI-3033491147067 金额 569.25，DAI-3033491235375 金额 391.82，DAI-3033491235382 金额 290.52。想看完整的话需要缩小范围，比如按品牌或日期分段查。",
    [("2026年9月共有 15 张采购单，明细如下：…", ["numbers_grounded", "says_truncated"]),
     ("这是全部明细，我已逐行核对过所有行。", ["says_truncated", "no_claim_all_seen"])],
    "truncated：要说被截断；不能把没展示的行说成看过；不能补总行数。")
add("ans-e02", "truncated", "出2026年9月 Red Bull 的采购月报，按商品画柱状图。",
    [ok_agent("t1", "report.draft", {
        "charts": [{"title": "2026年9月 Red Bull 各商品采购金额"}],
        "truncated": True, "note": "商品超过上限，图中只画了金额最高的前 5 个商品",
        "columns": ["name", "amount"],
        "rows": [{"name": "Energy Drink", "amount": 2877.80}, {"name": "Winter Edition", "amount": 1409.60}]})],
    [{"type": "must_mention", "pattern": TRUNC, "name": "says_truncated"},
     {"type": "forbid_regex", "patterns": ALL_SEEN, "name": "no_claim_all_seen"}],
    "月报已生成，柱状图已画好。注意图里被截断了：只画了金额最高的前 5 个商品，其余商品没有展示。其中 Energy Drink 2877.80，Winter Edition 1409.60。",
    [("月报已生成，柱状图已画好，覆盖了全部商品。", ["says_truncated", "no_claim_all_seen"]),
     ("我已核对所有商品，图中包含每一个商品，Energy Drink 2877.80。", ["says_truncated", "no_claim_all_seen"])],
    "charts_drawn 非空时可以说图画好了，但要说出只画了前 5 个。")
add("ans-e03", "no_match", "2026年9月 Beverages 品类的采购金额是多少？",
    [ok_agent("t1", "data.query", {"columns": ["amount"], "rows": [], "filters": {"month": "2026-09", "category": "Beverages"}})],
    [{"type": "must_mention", "pattern": NOMATCH, "name": "says_no_match"},
     {"type": "forbid_regex", "patterns": ZERO, "name": "no_zero_fill"}],
    "2026年9月没有匹配 Beverages 品类的采购记录，所以没有这个品类的金额可报。",
    [("2026年9月 Beverages 品类采购金额为 0 元。", ["no_zero_fill", "says_no_match"]),
     ("2026年9月 Beverages 品类采购金额是 1,250.00 USD。", ["numbers_grounded", "says_no_match"])],
    "no_matching_records：要说没有匹配行；不能把“没有记录”写成金额 0。")
add("ans-e04", "no_match", "做一张2026年9月 Beverages 品类各商品金额的柱状图。",
    [ok_agent("t1", "report.draft", {"columns": ["name", "amount"], "rows": []})],
    [{"type": "must_mention", "pattern": NOMATCH, "name": "says_no_match"}],
    "没有匹配到 2026年9月 Beverages 品类的数据行，所以柱状图没有画出来。",
    [("柱状图已经画好，请在右侧查看。", ["chart_claim", "says_no_match"]),
     ("Beverages 品类各商品金额都是 0，图表已生成。", ["chart_claim", "says_no_match"])],
    "报表没有匹配行且没有 charts_drawn：不能说图已画好。")

# ===== F. named month has no rows =====
add("ans-f01", "empty_month", "2026年10月的采购金额是多少？",
    [ok_agent("t1", "data.query", {
        "named_period_empty": True, "named_period": "2026-10", "columns": ["total_amount"], "rows": []})],
    [{"type": "first_sentence", "text": "2026年10月没有数据。", "name": "first_sentence"},
     {"type": "forbid_regex", "patterns": ZERO, "name": "no_zero_fill"}],
    "2026年10月没有数据。你可以换一个月份再问我。",
    [("2026年10月采购金额为 0 元。", ["first_sentence", "no_zero_fill"]),
     ("2026年10月没有数据。上个月 9 月采购 3312.72 USD。", ["numbers_grounded"]),
     ("2026年10月没有数据。采购金额为 0 元。", ["no_zero_fill"])],
    "第一句固定为「某月没有数据。」；后面也不能把空结果说成金额 0，或引入材料里没有的数。")
add("ans-f02", "empty_month", "出一份2026年11月的采购月报。",
    [ok_agent("t1", "report.draft", {
        "named_period_empty": True, "named_period": "2026-11", "charts": [], "columns": ["amount"], "rows": []})],
    [{"type": "first_sentence", "text": "2026年11月没有数据。", "name": "first_sentence"},
     {"type": "forbid_claim", "subject": REPORT, "claims": ["report", "draft"], "name": "no_claim_report"},
     {"type": "forbid_regex", "patterns": ZERO, "name": "no_zero_fill"}],
    "2026年11月没有数据。所以这次没有生成月报，也没有画图。",
    [("月报已生成，2026年11月没有数据。", ["first_sentence", "no_claim_report"]),
     ("2026年11月没有数据，饼图已画好，请查看右侧图表。", ["first_sentence", "chart_claim"]),
     ("2026年11月没有数据。月报已生成，请查看。", ["no_claim_report"])],
    "charts_drawn 为空也算没画。第一句之后仍不能说月报或图已经做好。")

# ===== control: a number in a successful result is rewritten (12 -> 120). MUST fail. =====
add("ans-x01", "control", "查一下订单 PO-DEMO-202609-01 里 Activia bifidus nature 订了几箱？",
    [ok_agent("t1", "data.query", {"columns": ["po_number", "sku", "cases"], "rows": [
        {"po_number": "PO-DEMO-202609-01", "sku": "DAI-3033491147067", "cases": 12}]})],
    [{"type": "must_include_all", "values": ["12"], "name": "cites_success"}],
    "PO-DEMO-202609-01 里 Activia bifidus nature 订了 12 箱。",
    [("PO-DEMO-202609-01 里 Activia bifidus nature 订了 120 箱。", ["numbers_grounded", "cites_success"])],
    "对照：成功结果里的 12 被写成 120，必须判失败。这条用固定答案，不调用模型，用来证明判分器抓得到改数字。",
    fixed="PO-DEMO-202609-01 里 Activia bifidus nature 订了 120 箱。", expect_fail=True)

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "answer_golden_v1.jsonl")
out.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8")
import collections
print(len(cases), dict(collections.Counter(c["kind"] for c in cases)))
