"""Chat report draft: questions and web lookups are slots; charts are the model's ECharts options."""

from __future__ import annotations

from backend.report.analyze import column_kinds, cover_days, findings
from backend.report.prompt import (
    CHART_GENERATION_PROMPT,
    CODE_WIZARD_PROMPT,
    REPORT_QUESTIONS_PROMPT,
    REPORT_REVIEW_PROMPT,
    REPORT_REVISE_PROMPT,
    RISK_PLAN_PROMPT,
    SECTIONS,
    SUMMARY_PROMPT,
    WRITING_PROMPT,
    outline_text,
)
from backend.report.turn import (
    finish_report,
    fit_chart_axes,
    parse_plan,
    parse_report_title,
    parse_risk_plan,
    place_charts_in_sections,
    shape_prose,
)
from backend.report.wizard import execute


def test_cover_days_divides_stock_by_period_units():
    notes = cover_days([
        [{"sku": "FO-100", "stock_on_hand": 90}],
        [{"sku": "FO-100", "units_sold": 30}, {"sku": "FO-200", "units_sold": 10}],
    ])
    assert notes == [{"sku": "FO-100", "stock": 90.0, "units": 30.0, "cover_days": 90.0}]


def test_parse_report_title_keeps_what_the_plan_named():
    assert parse_report_title({"title": " 8月月报 "}) == "8月月报"
    assert parse_report_title({}) == ""
    assert parse_report_title("8月月报") == ""
    assert len(parse_report_title({"title": "月" * 100})) == 80


def test_parse_plan_keeps_six_sections_and_drops_a_search_on_a_data_section():
    from datetime import date

    planned = parse_plan({
        "sections": [
            {"id": "performance", "questions": [" A ", "a", "b", "c", "d"], "searches": ["should not search"]},
            {"id": "nope", "questions": ["x"]},
            {"id": "next", "questions": ["sql?"], "searches": [" 双十一 ", "black friday", "618"]},
            {"id": "risks", "questions": ["amount"], "searches": ["市场下行"]},
            {"id": "summary", "questions": ["skip"], "searches": ["skip"]},
        ],
    }, today=date(2026, 10, 19))
    assert [section.id for section in planned] == [section["id"] for section in SECTIONS]
    performance = next(section for section in planned if section.id == "performance")
    assert performance.questions == ["A", "b", "c"]
    assert performance.searches == []
    nxt = next(section for section in planned if section.id == "next")
    assert nxt.questions == []
    assert nxt.searches == ["双十一", "black friday", "618"]
    risks = next(section for section in planned if section.id == "risks")
    assert risks.questions == [] and risks.searches == []
    summary = next(section for section in planned if section.id == "summary")
    assert summary.questions == [] and summary.searches == []


def test_parse_plan_does_not_invent_questions_when_the_model_returns_nothing():
    from datetime import date

    planned = parse_plan({}, today=date(2026, 10, 19))
    performance = next(section for section in planned if section.id == "performance")
    assert performance.questions == []
    metrics = next(section for section in planned if section.id == "metrics")
    assert metrics.questions == []
    nxt = next(section for section in planned if section.id == "next")
    assert nxt.searches == ["2026年10月 零售档期 促销热点", "即将到来的节假日 零售促销"]
    summary = next(section for section in planned if section.id == "summary")
    assert summary.questions == [] and summary.searches == []
    risks = next(section for section in planned if section.id == "risks")
    assert risks.questions == [] and risks.searches == []


def test_parse_risk_plan_waits_for_a_real_problem():
    skipped = parse_risk_plan({"write": False, "questions": ["库存"], "searches": ["新闻"]})
    assert skipped.write is False and skipped.questions == [] and skipped.searches == []
    assert parse_risk_plan({}).write is False
    chosen = parse_risk_plan({
        "write": True,
        "questions": [" 未完成采购单 ", "未完成采购单", "a", "b", "c"],
        "searches": [" 档期 ", "档期", "节假日"],
    })
    assert chosen.write is True
    assert chosen.questions == ["未完成采购单", "a", "b"]
    assert chosen.searches == ["档期", "节假日"]


def test_prompts_use_the_sectioned_shape_and_format_cleanly():
    assert [section["id"] for section in SECTIONS] == [
        "metrics", "summary", "performance", "inventory", "next", "risks",
    ]
    outline = outline_text()
    assert "关键指标" in outline and "问题与风险" in outline and "不要原样照抄" in outline
    for prompt in (
        REPORT_QUESTIONS_PROMPT, CHART_GENERATION_PROMPT, WRITING_PROMPT, SUMMARY_PROMPT,
        CODE_WIZARD_PROMPT, REPORT_REVIEW_PROMPT, REPORT_REVISE_PROMPT, RISK_PLAN_PROMPT,
    ):
        assert "## 角色" in prompt and "## 输入定义" in prompt
    assert "问题与风险的 questions 和 searches 都是空数组" in REPORT_QUESTIONS_PROMPT
    assert "8月月报" in REPORT_QUESTIONS_PROMPT
    assert "不要写 SKU" in CHART_GENERATION_PROMPT
    assert "产品名称（SKU）" in WRITING_PROMPT
    assert "要不要写" in RISK_PLAN_PROMPT
    planned = REPORT_QUESTIONS_PROMPT.format(
        outline=outline_text(), message="写一份报告", database_structure="- contracts: 协议",
        max_questions=3, max_searches=2,
    )
    assert "写一份报告" in planned and "{outline}" not in planned
    chart = CHART_GENERATION_PROMPT.format(message="月报", query_results="[]")
    assert "{c}%" in chart and "860" in chart
    WRITING_PROMPT.format(message="月报", section_title="业绩表现", section_brief="销量")
    SUMMARY_PROMPT.format(message="月报")
    RISK_PLAN_PROMPT.format(
        message="月报", database_structure="- contracts: 协议", max_questions=3, max_searches=2,
    )
    filled = CODE_WIZARD_PROMPT.format(
        section_title="库存情况",
        section_brief="采购与库存剩余",
        questions='["期末库存"]',
        query_results='[{"question": "期末库存", "rows": []}]',
    )
    assert "库存情况" in filled and "期末库存" in filled
    assert "按列名选查询" in filled and "不要默认只读 datasets[0]" in filled
    assert "{section_title}" not in filled and "{query_results}" not in filled
    reviewed = REPORT_REVIEW_PROMPT.format(
        message="月报", outline="关键指标", report="正文", evidence="{}", previous_issues="（无）",
    )
    assert "月报" in reviewed and "{report}" not in reviewed
    revised = REPORT_REVISE_PROMPT.format(
        message="月报", report="正文", score=70, chapters="[]", issues="[]", evidence="{}",
    )
    assert "正文" in revised and "{issues}" not in revised


def test_code_wizard_retries_against_the_query_that_has_the_column():
    from backend.llm import FakeLLM
    from backend.report.turn import SectionPlan, _code_wizard

    section = SectionPlan(
        id="performance", title="业绩表现", brief="畅销产品",
        sql=True, chart=True, search=False, require_search=False,
        questions=["各产品销售额"], searches=[],
    )
    datasets = [
        {
            "id": "q2", "question": "本期销售额", "row_count": 1,
            "columns": ["reporting_period", "total_sales"],
            "rows": [{"reporting_period": "2026-08", "total_sales": 2840.75}],
        },
        {
            "id": "q3", "question": "各产品销售额", "row_count": 1,
            "columns": ["sku", "product_name", "total_revenue"],
            "rows": [{"sku": "DAI-1", "product_name": "Activia", "total_revenue": 1696.46}],
        },
    ]
    bad = (
        "def analyze(datasets):\n"
        "    return [{'title': '畅销', 'detail': datasets[0]['rows'][0]['product_name'], 'evidence': {}}]"
    )
    good = (
        "def analyze(datasets):\n"
        "    chosen = next(item for item in datasets if 'product_name' in item.get('columns', []))\n"
        "    row = chosen['rows'][0]\n"
        "    return [{'title': '畅销', 'detail': row['product_name'] + '（' + row['sku'] + '）',"
        " 'evidence': {'revenue': row['total_revenue']}}]"
    )
    llm = FakeLLM(json_replies=[{"code": bad}, {"fixed_code": good}])
    insights, err, fails = _code_wizard(llm, section, datasets)
    assert err is None and fails == []
    assert insights[0]["detail"] == "Activia（DAI-1）"
    assert insights[0]["evidence"] == {"revenue": 1696.46}
    fix = llm.calls[1]["system"]
    assert "KeyError" in fix
    assert "datasets[0]" in fix and "total_sales" in fix
    assert "datasets[1]" in fix and "product_name" in fix
    assert "不要把别的列改名" in fix


def test_insight_code_reads_the_rows_and_rejects_an_import():
    ran = execute(
        "def analyze(datasets):\n"
        "    rows = datasets[0]['rows']\n"
        "    return [{'title': '订单', 'detail': '两行', 'evidence': {'n': len(rows), 'total': rows[0]['a'] + rows[1]['a']}}]",
        [{"id": "q1", "question": "合计", "rows": [{"a": 1}, {"a": 2}]}],
    )
    assert ran["error"] is None
    assert ran["insights"] == [{"title": "订单", "detail": "两行", "evidence": {"n": 2, "total": 3}}]
    blocked = execute("import os\ninsights = []", [])
    assert blocked["insights"] == [] and blocked["error"]


def test_five_chapters_add_up_and_a_missing_one_scores_zero():
    from backend.report.turn import _review_score

    raw = {"chapters": [
        {"section": "关键指标", "score": 20},
        {"section": "摘要", "score": 20},
        {"section": "业绩表现", "score": 20},
        {"section": "库存情况", "score": 20},
        {"section": "下一周期计划", "score": 5},
    ], "risks_penalty": 0, "score": 99}
    assert _review_score(raw)[0] == 85
    raw["risks_penalty"] = 10
    assert _review_score(raw)[0] == 75
    raw["chapters"] = [item for item in raw["chapters"] if item["section"] != "摘要"]
    raw["risks_penalty"] = 0
    assert _review_score(raw)[0] == 65
    assert _review_score({"score": 90})[0] == 90


def test_review_passes_at_85_and_revises_below_it_for_three_rounds():
    from backend.llm import FakeLLM
    from backend.report.turn import _review_rounds

    def run(replies: list[dict], report: str = "原文"):
        llm = FakeLLM(json_replies=replies)
        text, score, error = _review_rounds(llm, message="月报", report=report, evidence="{}", remembered="")
        return llm, text, score, error

    _, text, score, error = run([{"score": 85, "ok": True, "issues": []}])
    assert text == "原文" and score == 85 and error is None

    issue = {"section": "业绩表现", "problem": "没有提到图表"}
    llm, text, score, error = run([
        {"score": 84, "ok": False, "issues": [issue]},
        {"report": "改后的报告"},
        {"score": 90, "ok": True, "issues": []},
    ])
    assert text == "改后的报告" and score == 90 and error is None
    assert "没有提到图表" in llm.calls[1]["system"] and "84" in llm.calls[1]["system"]
    assert "没有提到图表" in llm.calls[2]["system"]

    replies: list[dict] = []
    for index in range(1, 4):
        replies.append({"score": 60, "ok": False, "issues": [issue]})
        replies.append({"report": f"第{index}稿"})
    replies.append({"score": 70, "ok": False, "issues": [issue]})
    llm, text, score, error = run(replies, report="原稿")
    assert text == "第3稿" and score == 70 and error is None
    assert len(llm.calls) == 7


def test_reflection_retries_a_failure_and_keeps_three_tries():
    from backend.llm import FakeLLM
    from backend.codefix import format_failures, with_reflection

    def fail_then_ok(artifact: str):
        if artifact == "ok":
            return True, "done", None, ""
        return False, None, f"bad {artifact}", "stdout"

    fixed = FakeLLM(json_replies=[{"fixed_code": "ok"}])
    value, failures = with_reflection(fixed, kind="code", first="bad", run=fail_then_ok)
    assert value == "done" and failures == []
    assert "bad bad" in fixed.calls[0]["system"]

    def always_fail(_artifact: str):
        return False, None, "boom", "out"

    stuck = FakeLLM(json_replies=[{"fixed_code": "still"}, {"fixed_code": "nope"}])
    value, failures = with_reflection(stuck, kind="sql", first="a", run=always_fail)
    assert value is None and len(failures) == 3
    assert [item["artifact"] for item in failures] == ["a", "still", "nope"]
    assert all(item["error"] == "boom" for item in failures)
    handed = format_failures(failures)
    assert "nope" in handed and "boom" in handed and "需要人工处理" in handed
    assert "still" not in handed and "第1次" not in handed


def test_code_fix_is_recorded_on_the_tool_step(db):
    from datetime import timedelta
    from types import SimpleNamespace

    from backend.codefix import with_reflection
    from backend.db import utcnow
    from backend.llm import FakeLLM

    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "codefix-trace", {"message": "fix"})
    claimed = db.claim_run(run["id"], "worker-test", utcnow() + timedelta(minutes=5))
    assert claimed is not None
    step = db.insert_step(run["id"], "tool", "report.draft", {})
    assert step is not None
    trace = SimpleNamespace(db=db, run_id=run["id"], step_id=step["id"])

    def always_fail(_artifact: str):
        return False, None, "boom", "out"

    llm = FakeLLM(json_replies=[{"fixed_code": "still"}, {"fixed_code": "nope"}])
    value, failures = with_reflection(llm, kind="code", first="a", run=always_fail, trace=trace)
    assert value is None and len(failures) == 3

    calls = [
        row for row in db.run_trace("dist-1", run["id"])["model_calls"]
        if row["step_id"] == step["id"]
    ]
    assert [(row["provider"], row["model"], row["status"]) for row in calls] == [
        ("fake", "deepseek-flash", "succeeded"),
        ("fake", "deepseek-flash", "succeeded"),
    ]
    assert [row["request_json"]["schema"]["name"] for row in calls] == ["code_fix", "code_fix"]
    assert [row["response_json"]["json"]["fixed_code"] for row in calls] == ["still", "nope"]


def test_findings_sum_a_mix_and_a_trend_and_skip_a_single_row():
    rows = [
        {"period": "2025-08", "channel": "Tmall", "units": 10},
        {"period": "2025-08", "channel": "JD", "units": 4},
        {"period": "2025-09", "channel": "Tmall", "units": 12},
        {"period": "2025-09", "channel": "JD", "units": 6},
    ]
    found = findings("q1", rows)
    trend = next(item for item in found if item["kind"] == "trend")
    mix = next(item for item in found if item["kind"] == "mix")
    assert trend["start"] == {"label": "2025-08", "value": 14}
    assert trend["end"] == {"label": "2025-09", "value": 18}
    assert trend["change"] == 4 and trend["change_percent"] == 28.6 and trend["direction"] == "up"
    assert mix["total"] == 32 and mix["group_count"] == 2
    assert mix["top"] == {"label": "Tmall", "value": 22, "share_percent": 68.8}
    assert mix["bottom"]["label"] == "JD"
    assert findings("q2", [{"total_amount": 1000}]) == []
    assert column_kinds([{"period": "2025-08", "units": 1}]) == [
        {"name": "period", "kind": "time"},
        {"name": "units", "kind": "number"},
    ]


def test_findings_range_when_there_is_no_category():
    assert findings("q1", [{"units": 2}, {"units": 4}]) == [{
        "id": "q1", "kind": "range", "y": "units", "count": 2, "min": 2, "max": 4, "mean": 3,
    }]


def test_long_category_labels_rotate_and_charts_stay_in_their_section():
    from backend.report.turn import SectionPlan

    assert shape_prose("销售额上升。库存偏紧。需要补货。还要看节假日。") == (
        "销售额上升。库存偏紧。\n\n需要补货。还要看节假日。"
    )

    chart = {
        "id": "chart_001", "title": "深海鱼油（FO-100）销量", "subtitle": "",
        "echarts_option": {"xAxis": {"type": "category", "data": ["深海鱼油（FO-100）", "精华（SR-200）"]}},
    }
    fitted = fit_chart_axes([chart])
    assert chart["title"] == "深海鱼油（FO-100）销量"
    assert fitted[0]["id"] == "chart_001"
    assert fitted[0]["echarts_option"]["xAxis"]["data"] == ["深海鱼油（FO-100）", "精华（SR-200）"]
    assert fitted[0]["echarts_option"]["xAxis"]["axisLabel"]["rotate"] == 28

    planned = [
        SectionPlan("summary", "摘要", "", False, False, False, False, [], []),
        SectionPlan("performance", "业绩表现", "", True, True, False, False, [], []),
    ]
    report = "## 2. 摘要\n\n见 [趋势](chart_001)。\n\n## 3. 业绩表现\n\n销量上升。"
    placed = place_charts_in_sections(report, planned, {"performance": fitted})
    summary, performance = placed.split("业绩表现", 1)
    assert "chart_001" not in summary
    assert "(chart_001)" in performance
    finished = finish_report(report, planned, {"performance": fitted})
    assert "chart_001" in finished.split("业绩表现", 1)[1]
