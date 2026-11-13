"""Chat report draft: questions and web lookups are slots; charts are the model's ECharts options."""

from __future__ import annotations

from backend.report.analyze import column_kinds, cover_days, findings, risk_flags
from backend.report.prompt import (
    CHART_GENERATION_PROMPT,
    CODE_WIZARD_PROMPT,
    CODE_WIZARDS_PROMPT,
    REPORT_QUESTIONS_PROMPT,
    REPORT_REVIEW_PROMPT,
    REPORT_REVISE_PROMPT,
    SECTIONS,
    SUMMARY_PROMPT,
    WRITING_PROMPT,
    outline_text,
)
from backend.report.turn import (
    ensure_chart_questions,
    finish_report,
    fit_chart_axes,
    parse_plan,
    parse_report_title,
    parse_risk_plan,
    place_charts_in_sections,
    purpose_for_schema,
    shape_prose,
)
from backend.report.wizard import execute


def test_cover_days_divides_stock_by_period_units():
    notes = cover_days([
        [{"sku": "FO-100", "stock_on_hand": 90}],
        [{"sku": "FO-100", "units_sold": 30}, {"sku": "FO-200", "units_sold": 10}],
    ])
    assert notes == [{"sku": "FO-100", "stock": 90.0, "units": 30.0, "cover_days": 90.0}]


def test_report_schema_names_pick_a_graded_purpose():
    assert purpose_for_schema({"name": "report_section"}) == "report.write"
    assert purpose_for_schema({"name": "report_review"}) == "report.review"
    assert purpose_for_schema({"name": "report_revise"}) == "report.review"
    assert purpose_for_schema({"name": "report_charts"}) == "report.optional"
    assert purpose_for_schema({"name": "code_wizards"}) == "report.optional"
    assert purpose_for_schema({"name": "report_plan"}) == "report.optional"
    assert purpose_for_schema({}) == "report"


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


def test_parse_plan_drops_metrics_questions_the_model_wrote():
    planned = parse_plan({
        "sections": [
            {"id": "metrics", "questions": ["合同目标是多少？", "上月销售额"], "searches": ["不要搜"]},
        ],
    })
    metrics = next(section for section in planned if section.id == "metrics")
    assert metrics.questions == [] and metrics.searches == []


def test_metrics_templates_pass_the_sql_validator():
    from backend.report.metrics import (
        assert_templates_validate,
        period_from_message,
        previous_month,
        uses_purchase_orders,
    )

    assert_templates_validate()
    assert period_from_message("出一份2026年9月的采购月报") == "2026-09"
    assert period_from_message("2026-09 动销") == "2026-09"
    assert period_from_message("写一份月报") is None
    assert previous_month("2026-01") == "2025-12"
    assert uses_purchase_orders("出一份采购月报") is True
    assert uses_purchase_orders("出一份动销月报") is False


def test_named_period_absent_when_metrics_skip_that_month():
    from backend.report.metrics import named_period_absent

    september = [{"period": "2026-09", "total_amount": 100}]
    assert named_period_absent("2026-10", september) is True
    assert named_period_absent("2026-09", september) is False
    assert named_period_absent("2026-10", []) is True
    assert named_period_absent(None, september) is False


def test_metrics_sql_runs_without_a_model(db):
    from backend.report.metrics import fetch_metric_results
    from backend.text2sql.text2sql import QueryResult

    rows = fetch_metric_results(
        db, message="出一份2026年9月的采购月报",
        partnership_id="ps-x", brand_id="brand-x", distributor_id="dist-1", session_id="s-x",
    )
    assert [qid for qid, _question, _result in rows] == ["m1", "m2"]
    assert all(isinstance(result, QueryResult) for _qid, _question, result in rows)
    m2 = next(result for qid, _question, result in rows if qid == "m2")
    assert m2.ok, m2.error


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


def test_risk_flags_from_cover_drop_overdue_and_missing_target():
    from datetime import date

    flags = risk_flags([
        {
            "id": "stock", "question": "库存",
            "columns": ["sku", "product_name", "stock_on_hand"],
            "rows": [{"sku": "FO-100", "product_name": "深海鱼油", "stock_on_hand": 12}],
        },
        {
            "id": "units", "question": "销量",
            "columns": ["sku", "units_sold"],
            "rows": [{"sku": "FO-100", "units_sold": 30}],
        },
        {
            "id": "sales", "question": "销售额",
            "columns": ["reporting_period", "total_sales"],
            "rows": [
                {"reporting_period": "2026-08", "total_sales": 100},
                {"reporting_period": "2026-09", "total_sales": 70},
            ],
        },
        {
            "id": "po", "question": "未完成采购单",
            "columns": ["po_number", "status", "eta_date", "total_amount"],
            "rows": [{
                "po_number": "PO-1", "status": "submitted",
                "eta_date": "2026-11-01", "total_amount": 500,
            }],
        },
        {
            "id": "contract", "question": "合同目标",
            "columns": ["title", "annual_sales_target"],
            "rows": [{"title": "经销协议", "annual_sales_target": None}],
        },
    ], today=date(2026, 11, 12))
    kinds = [item["kind"] for item in flags]
    assert kinds == ["missing_target", "overdue_po", "low_cover", "sales_drop"]
    cover = next(item for item in flags if item["kind"] == "low_cover")
    assert "深海鱼油（FO-100）" in cover["detail"]
    assert cover["evidence"]["cover_days"] == 12.0
    drop = next(item for item in flags if item["kind"] == "sales_drop")
    assert drop["evidence"]["change_percent"] == -30.0
    assert risk_flags([], today=date(2026, 11, 12)) == []
    healthy = risk_flags([{
        "id": "stock", "question": "库存",
        "columns": ["sku", "stock_on_hand", "units_sold"],
        "rows": [{"sku": "FO-100", "stock_on_hand": 90, "units_sold": 30}],
    }], today=date(2026, 11, 12))
    assert healthy == []


def test_prompts_use_the_sectioned_shape_and_format_cleanly():
    assert [section["id"] for section in SECTIONS] == [
        "metrics", "summary", "performance", "inventory", "next", "risks",
    ]
    outline = outline_text()
    assert "关键指标" in outline and "问题与风险" in outline and "不要原样照抄" in outline
    for prompt in (
        REPORT_QUESTIONS_PROMPT, CHART_GENERATION_PROMPT, WRITING_PROMPT, SUMMARY_PROMPT,
        CODE_WIZARD_PROMPT, CODE_WIZARDS_PROMPT, REPORT_REVIEW_PROMPT, REPORT_REVISE_PROMPT,
    ):
        assert "## 角色" in prompt and "## 输入定义" in prompt
    assert "问题与风险的 questions 和 searches 都是空数组" in REPORT_QUESTIONS_PROMPT
    assert "8月月报" in REPORT_QUESTIONS_PROMPT
    assert "不要写 SKU" in CHART_GENERATION_PROMPT
    assert "最多一张图" in CHART_GENERATION_PROMPT
    assert "不要改成「最近一个报告期」" in REPORT_QUESTIONS_PROMPT
    assert "没有画出的图" in REPORT_REVIEW_PROMPT
    assert "`drawn`" in WRITING_PROMPT
    assert "产品名称（SKU）" in WRITING_PROMPT
    assert "风险标记" in WRITING_PROMPT
    planned = REPORT_QUESTIONS_PROMPT.format(
        outline=outline_text(), message="写一份报告", database_structure="- contracts: 协议",
        max_questions=3, max_searches=2,
    )
    assert "写一份报告" in planned and "{outline}" not in planned
    chart = CHART_GENERATION_PROMPT.format(message="月报", query_results="[]")
    assert "{c}%" in chart and "860" in chart
    WRITING_PROMPT.format(message="月报", section_title="业绩表现", section_brief="销量")
    SUMMARY_PROMPT.format(message="月报")
    filled = CODE_WIZARD_PROMPT.format(
        section_title="库存情况",
        section_brief="采购与库存剩余",
        questions='["期末库存"]',
        query_results='[{"question": "期末库存", "rows": []}]',
    )
    assert "库存情况" in filled and "期末库存" in filled
    assert "按列名选查询" in filled and "不要默认只读 datasets[0]" in filled
    assert "{section_title}" not in filled and "{query_results}" not in filled
    batched = CODE_WIZARDS_PROMPT.format(
        sections="- `inventory` 「库存情况」：采购与库存剩余",
        query_results='[{"question": "期末库存", "rows": []}]',
    )
    assert "四个章节" in batched and "analyze" in batched
    assert "{sections}" not in batched and "{query_results}" not in batched
    reviewed = REPORT_REVIEW_PROMPT.format(
        message="月报", outline="关键指标", report="正文", evidence="{}", previous_issues="（无）",
    )
    assert "月报" in reviewed and "{report}" not in reviewed
    revised = REPORT_REVISE_PROMPT.format(
        message="月报", report="正文", score=70, chapters="[]", issues="[]", evidence="{}",
    )
    assert "正文" in revised and "{issues}" not in revised
    assert "只改" in REPORT_REVISE_PROMPT and "sections" in REPORT_REVISE_PROMPT
    assert "一次列全" in REPORT_REVIEW_PROMPT
    assert "不要只改一章" in REPORT_REVISE_PROMPT


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


def test_four_insight_functions_are_generated_once_and_repaired_alone():
    from backend.llm import FakeLLM
    from backend.report.turn import SectionPlan, _code_wizards

    sections = [
        SectionPlan("metrics", "关键指标", "目标", True, False, False, False, ["目标"], []),
        SectionPlan("performance", "业绩表现", "畅销", True, True, False, False, ["销售额"], []),
        SectionPlan("inventory", "库存情况", "库存", True, True, False, False, ["库存"], []),
        SectionPlan("next", "下一周期计划", "计划", False, False, True, True, [], ["档期"]),
    ]
    datasets = [
        {
            "id": "q1", "question": "目标", "row_count": 1,
            "columns": ["annual_sales_target"],
            "rows": [{"annual_sales_target": 1200000}],
        },
        {
            "id": "q2", "question": "销售额", "row_count": 1,
            "columns": ["sku", "product_name", "total_revenue"],
            "rows": [{"sku": "DAI-1", "product_name": "Activia", "total_revenue": 1696.46}],
        },
    ]
    ok = (
        "def analyze(datasets):\n"
        "    return [{'title': 'ok', 'detail': '一行', 'evidence': {'n': 1}}]"
    )
    bad = (
        "def analyze(datasets):\n"
        "    return [{'title': 'x', 'detail': datasets[0]['rows'][0]['missing'], 'evidence': {}}]"
    )
    llm = FakeLLM(json_replies=[
        {"metrics": ok, "performance": bad, "inventory": ok, "next": ok},
        {"fixed_code": ok},
    ])
    by_section, errors, fails = _code_wizards(llm, sections, datasets)
    assert errors == [] and fails == []
    assert [item["title"] for item in by_section["metrics"]] == ["ok"]
    assert [item["title"] for item in by_section["performance"]] == ["ok"]
    assert [item["title"] for item in by_section["inventory"]] == ["ok"]
    assert [item["title"] for item in by_section["next"]] == ["ok"]
    assert len(llm.calls) == 2
    assert (llm.calls[0]["schema"] or {}).get("name") == "code_wizards"
    assert "四个章节" in llm.calls[0]["system"]
    assert "KeyError" in llm.calls[1]["system"]


def test_overlap_runs_both_callables_before_either_returns():
    import threading

    from backend.report.turn import _overlap

    barrier = threading.Barrier(2, timeout=2)

    def one():
        barrier.wait()
        return "a"

    def two():
        barrier.wait()
        return "b"

    assert _overlap(one, two) == ["a", "b"]
    assert _overlap(lambda: 1) == [1]


def _query_result(rows, columns=None, *, ok=True):
    from backend.text2sql.text2sql import QueryResult

    columns = columns or (list(rows[0]) if rows else [])
    return QueryResult(
        ok=ok, sql="select 1", rows=rows, row_count=len(rows), error=None, error_type=None,
        tables=["purchase_orders"], columns=columns,
    )


def _chart_sections():
    from backend.report.turn import SectionPlan

    return [
        SectionPlan("metrics", "关键指标", "目标", True, False, False, False, ["目标"], []),
        SectionPlan("performance", "业绩表现", "畅销", True, True, False, False, ["销售额"], []),
        SectionPlan("inventory", "库存情况", "库存", True, True, False, False, ["库存"], []),
        SectionPlan("next", "下一周期计划", "计划", False, False, True, True, [], ["档期"]),
    ]


def _wizard_ok():
    code = (
        "def analyze(datasets):\n"
        "    return [{'title': 'ok', 'detail': '一行', 'evidence': {'n': 1}}]"
    )
    return {"_schema": "code_wizards", "metrics": code, "performance": code, "inventory": code, "next": code}


def test_chart_role_tags_pie_trend_and_bar():
    from backend.report.charts import chart_role, skip_reason, wants_category_pie

    assert wants_category_pie("出一份2026年10月采购月报，要品类饼图")
    assert chart_role("2026年10月各品类的采购金额和采购箱数分别是多少？") == "pie_category"
    assert chart_role("最近六个报告期已确认的销售额，按报告期各是多少？") == "trend"
    assert chart_role("最近一个报告期各产品已确认的期末库存是多少？") == "bar"
    assert skip_reason("pie_category", [], ["category", "line_total"]) == "no_data"
    assert skip_reason("pie_category", [{"category": None, "total": None}], ["category", "total"]) == "no_data"
    assert skip_reason(
        "pie_category",
        [{"product_name": "Cerise", "line_total": 10}],
        ["product_name", "line_total"],
    ) == "bad_dimension"
    assert skip_reason(
        "pie_category",
        [{"category": "Chocolate", "line_total": 10}],
        ["category", "line_total"],
    ) is None


def test_ensure_chart_questions_injects_a_named_month_category_query():
    from datetime import date

    planned = parse_plan({
        "sections": [
            {"id": "performance", "questions": ["最近一个报告期按产品的采购金额"], "searches": []},
        ],
    }, today=date(2026, 10, 19))
    filled = ensure_chart_questions(planned, "出一份2026年10月采购月报，要品类饼图")
    performance = next(section for section in filled if section.id == "performance")
    assert any("2026年10月" in question and "品类" in question for question in performance.questions)
    already = ensure_chart_questions(filled, "出一份2026年10月采购月报，要品类饼图")
    again = next(section for section in already if section.id == "performance")
    assert again.questions == performance.questions


def test_charts_and_insights_are_produced_together():
    from backend.llm import FakeLLM
    from backend.report.turn import _Ask, _charts_and_insights

    sections = _chart_sections()
    rows = [{"category": "Chocolate", "line_total": 10.0}]
    asked = [_Ask("q1", "各品类的采购金额是多少？", _query_result(rows, ["category", "line_total"]))]
    by_section = {
        "metrics": [], "performance": asked, "inventory": [], "next": [],
    }
    datasets = [{
        "id": "q1", "question": "各品类的采购金额是多少？", "row_count": 1,
        "columns": ["category", "line_total"], "rows": rows,
    }]
    llm = FakeLLM(json_replies=[
        {"_schema": "report_charts", "charts": [{
            "id": "c1", "title": "品类金额", "subtitle": "", "type": "pie",
            "echarts_option": {"series": [{"type": "pie", "data": [{"name": "Chocolate", "value": 10}]}]},
        }]},
        _wizard_ok(),
    ])
    charts, charts_for, failures, insights, errors, insight_fails, decisions = _charts_and_insights(
        llm, "出一份月报，要品类饼图", sections, by_section, datasets,
    )
    assert failures == [] and errors == [] and insight_fails == []
    assert charts and charts_for["performance"]
    assert [item["title"] for item in insights["metrics"]] == ["ok"]
    assert any(item.status == "drawn" and item.role == "pie_category" for item in decisions)
    names = [(call["schema"] or {}).get("name") for call in llm.calls]
    assert "report_charts" in names and "code_wizards" in names


def test_empty_category_query_skips_the_chart_model():
    from backend.llm import FakeLLM
    from backend.report.turn import _Ask, _charts_and_insights

    sections = _chart_sections()
    asked = [_Ask("q1", "2026年10月各品类的采购金额是多少？", _query_result([], ["category", "line_total"]))]
    by_section = {"metrics": [], "performance": asked, "inventory": [], "next": []}
    datasets = [{
        "id": "m1", "question": "合同目标", "row_count": 1,
        "columns": ["title"], "rows": [{"title": "协议"}],
    }]
    llm = FakeLLM(json_replies=[_wizard_ok()])
    charts, charts_for, failures, _insights, errors, insight_fails, decisions = _charts_and_insights(
        llm, "出一份2026年10月采购月报，要品类饼图", sections, by_section, datasets,
    )
    assert charts == [] and charts_for["performance"] == []
    assert failures == [] and errors == [] and insight_fails == []
    assert decisions[0].status == "skipped" and decisions[0].reason == "no_data"
    assert "2026年10月" in decisions[0].note
    names = [(call["schema"] or {}).get("name") for call in llm.calls]
    assert "report_charts" not in names and "code_wizards" in names


def test_category_pie_of_product_names_is_dropped():
    from backend.llm import FakeLLM
    from backend.report.turn import _Ask, _charts_and_insights

    sections = _chart_sections()
    rows = [{"category": "Chocolate", "line_total": 10.0}]
    asked = [_Ask("q1", "各品类的采购金额是多少？", _query_result(rows, ["category", "line_total"]))]
    by_section = {"metrics": [], "performance": asked, "inventory": [], "next": []}
    llm = FakeLLM(json_replies=[
        {"_schema": "report_charts", "charts": [{
            "id": "c1", "title": "产品占比", "subtitle": "", "type": "pie",
            "echarts_option": {"series": [{"type": "pie", "data": [{"name": "Cerise", "value": 10}]}]},
        }]},
        _wizard_ok(),
    ])
    charts, _charts_for, failures, _insights, _errors, _insight_fails, decisions = _charts_and_insights(
        llm, "出一份月报，要品类饼图", sections, by_section, [],
    )
    assert charts == [] and failures == []
    assert decisions[0].status == "skipped" and decisions[0].reason == "bad_dimension"


def test_inventory_product_pie_is_dropped_when_the_user_asked_for_a_category_pie():
    from backend.llm import FakeLLM
    from backend.report.turn import _Ask, _charts_and_insights

    sections = _chart_sections()
    rows = [{"product_name": "Cerise", "stock_on_hand": 12}]
    asked = [_Ask("q1", "最近一个报告期各产品已确认的期末库存是多少？", _query_result(rows))]
    by_section = {"metrics": [], "performance": [], "inventory": asked, "next": []}
    llm = FakeLLM(json_replies=[
        {"_schema": "report_charts", "charts": [{
            "id": "c1", "title": "库存占比", "subtitle": "", "type": "pie",
            "echarts_option": {"series": [{"type": "pie", "data": [{"name": "Cerise", "value": 12}]}]},
        }]},
        _wizard_ok(),
    ])
    charts, charts_for, failures, _insights, _errors, _insight_fails, decisions = _charts_and_insights(
        llm, "出一份2026年10月采购月报，要品类饼图", sections, by_section, [],
    )
    assert charts == [] and charts_for["inventory"] == [] and failures == []
    assert decisions[0].role == "bar" and decisions[0].status == "skipped"


def test_trend_chart_is_kept_when_the_axis_is_periods():
    from backend.llm import FakeLLM
    from backend.report.turn import _Ask, _charts_and_insights

    sections = _chart_sections()
    rows = [
        {"reporting_period": "2026-05", "total_amount": 1},
        {"reporting_period": "2026-06", "total_amount": 2},
    ]
    asked = [_Ask("q1", "最近六个报告期的采购金额按报告期各是多少？", _query_result(rows))]
    by_section = {"metrics": [], "performance": asked, "inventory": [], "next": []}
    llm = FakeLLM(json_replies=[
        {"_schema": "report_charts", "charts": [{
            "id": "c1", "title": "近六月采购", "subtitle": "", "type": "line",
            "echarts_option": {
                "xAxis": {"type": "category", "data": ["2026-05", "2026-06"]},
                "series": [{"type": "line", "data": [1, 2]}],
            },
        }]},
        _wizard_ok(),
    ])
    charts, charts_for, failures, _insights, _errors, _insight_fails, decisions = _charts_and_insights(
        llm, "出一份2026年10月采购月报，要品类饼图", sections, by_section, [],
    )
    assert failures == [] and charts and charts_for["performance"]
    assert decisions[0].status == "drawn" and decisions[0].role == "trend"


def test_four_body_sections_are_written_together():
    from backend.llm import FakeLLM
    from backend.report.turn import SectionPlan, _write_body_sections

    sections = [
        SectionPlan("metrics", "关键指标", "目标", True, False, False, False, ["目标"], []),
        SectionPlan("performance", "业绩表现", "畅销", True, True, False, False, ["销售额"], []),
        SectionPlan("inventory", "库存情况", "库存", True, True, False, False, ["库存"], []),
        SectionPlan("next", "下一周期计划", "计划", False, False, True, True, [], ["档期"]),
        SectionPlan("summary", "摘要", "收拢", False, False, False, False, [], []),
        SectionPlan("risks", "问题与风险", "风险", False, False, False, False, [], []),
    ]
    llm = FakeLLM(json_replies=[
        {"_match": "当前章节：关键指标", "content": "指标正文", "key_points": []},
        {"_match": "当前章节：业绩表现", "content": "业绩正文", "key_points": []},
        {"_match": "当前章节：库存情况", "content": "库存正文", "key_points": []},
        {"_match": "当前章节：下一周期计划", "content": "计划正文", "key_points": []},
    ])
    texts = _write_body_sections(
        llm, "出一份月报", [], sections, {section.id: [] for section in sections},
        [], {}, {},
    )
    assert texts == {
        "metrics": "指标正文",
        "performance": "业绩正文",
        "inventory": "库存正文",
        "next": "计划正文",
    }
    assert "summary" not in texts and "risks" not in texts
    assert len(llm.calls) == 4


def test_first_wave_puts_searches_next_to_data_queries():
    from backend.orchestrator.report import _first_wave
    from backend.report.turn import searches_from_payloads

    tasks, questions, searches = _first_wave(["期末库存"], ["双十一 促销"])
    assert [(task.kind, task.name, task.id) for task in tasks] == [
        ("agent", "data", "q1"),
        ("tool", "web_search", "s1"),
    ]
    assert questions == [["q1", "期末库存"]]
    assert searches == [["s1", "双十一 促销"]]
    found = searches_from_payloads([("s1", "双十一 促销")], {
        "s1": {
            "ok": True,
            "payload": {"ok": True, "data": {"results": [
                {"title": "档期", "snippet": "双十一", "url": "https://example.com"},
            ]}, "error": None},
        },
    })
    assert found[0].ok and found[0].results[0]["title"] == "档期"
    missing = searches_from_payloads([("s1", "双十一 促销")], {})
    assert missing[0].ok is False and "missing" in (missing[0].error or "")


def test_first_wave_zips_search_into_the_front_of_data():
    from backend.orchestrator.report import _first_wave

    questions = [f"q{i}" for i in range(1, 7)]
    searches = ["促销", "库存新闻", "竞品"]
    tasks, question_pairs, search_pairs = _first_wave(questions, searches)
    assert [task.id for task in tasks] == ["q1", "s1", "q2", "s2", "q3", "s3", "q4", "q5", "q6"]
    assert all(task.depends_on == [] for task in tasks)
    assert [pair[0] for pair in question_pairs] == ["q1", "q2", "q3", "q4", "q5", "q6"]
    assert [pair[0] for pair in search_pairs] == ["s1", "s2", "s3"]
    extra_search, _, _ = _first_wave(["库存"], ["a", "b", "c"])
    assert [task.id for task in extra_search] == ["q1", "s1", "s2", "s3"]
    data_only, _, search_pairs = _first_wave(["库存", "销量"], [])
    assert [task.id for task in data_only] == ["q1", "q2"] and search_pairs == []


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


def test_review_passes_at_85_and_revises_named_sections_together():
    from backend.llm import FakeLLM
    from backend.report.turn import _review_rounds

    draft = (
        "## 1. 关键指标\n\n目标缺失。\n\n"
        "## 2. 摘要\n\n摘要。\n\n"
        "## 3. 业绩表现\n\n没有图。\n\n"
        "## 4. 库存情况\n\n库存薄。\n\n"
        "## 5. 下一周期计划\n\n补货。"
    )

    def run(replies: list[dict], report: str = draft):
        llm = FakeLLM(json_replies=replies)
        text, score, error = _review_rounds(llm, message="月报", report=report, evidence="{}", remembered="")
        return llm, text, score, error

    _, text, score, error = run([{"chapters": [
        {"section": "关键指标", "score": 20},
        {"section": "摘要", "score": 20},
        {"section": "业绩表现", "score": 20},
        {"section": "库存情况", "score": 20},
        {"section": "下一周期计划", "score": 20},
    ], "risks_penalty": 0, "ok": True, "issues": []}])
    assert text == draft and score == 100 and error is None

    issues = [
        {
            "section": "业绩表现", "issue_type": "format", "severity": "major",
            "description": "没有提到图表", "suggestion": "补上图表引用",
        },
        {
            "section": "库存情况", "issue_type": "incomplete", "severity": "major",
            "description": "没有写补货", "suggestion": "补上还能卖多少天",
        },
    ]
    llm, text, score, error = run([
        {"score": 84, "ok": False, "issues": issues},
        {"sections": [
            {"section": "业绩表现", "content": "补上了图。"},
            {"section": "库存情况", "content": "还能卖 12 天。"},
        ]},
        {"score": 90, "ok": True, "issues": []},
    ])
    assert "补上了图。" in text and "还能卖 12 天。" in text
    assert "目标缺失。" in text and "库存薄。" not in text
    assert score == 90 and error is None
    assert "一次改完这些章节，不要只改一节：业绩表现、库存情况。" in llm.calls[1]["messages"][0]["content"]
    assert "没有提到图表" in llm.calls[1]["system"] and "84" in llm.calls[1]["system"]
    assert "没有提到图表" in llm.calls[2]["system"]

    llm, text, score, error = run([
        {"score": 60, "ok": False, "issues": issues},
        {"sections": [
            {"section": "业绩表现", "content": "第1稿"},
            {"section": "库存情况", "content": "库存第1稿"},
        ]},
        {"score": 70, "ok": False, "issues": issues},
        {"sections": [
            {"section": "业绩表现", "content": "第2稿"},
            {"section": "库存情况", "content": "库存第2稿"},
        ]},
        {"score": 75, "ok": False, "issues": issues},
    ], report=draft)
    assert "第2稿" in text and "库存第2稿" in text and "目标缺失。" in text
    assert score == 75 and error is None
    assert len(llm.calls) == 5


def test_reflection_retries_a_failure_and_keeps_three_tries():
    from backend.codefix import format_failures, with_reflection
    from backend.llm import FakeLLM

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
    claimed = db.claim_chat_turn(run["id"], "worker-test", utcnow() + timedelta(minutes=5))
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


def test_replace_report_sections_keeps_untouched_chapters():
    from backend.report.turn import replace_report_sections

    report = "## 1. 关键指标\n\n目标缺失。\n\n## 3. 业绩表现\n\n旧稿。\n\n## 6. 问题与风险\n\n有风险。"
    updated = replace_report_sections(report, {
        "业绩表现": "新稿。",
        "问题与风险": "",
    })
    assert "目标缺失。" in updated
    assert "新稿。" in updated
    assert "旧稿。" not in updated
    assert "问题与风险" not in updated


def test_hard_checks_catch_budget_code_wrong_period_and_invented_numbers():
    from backend.report.check import apply_hard_fixes, hard_issues

    dirty = "COST_BUDGET_EXCEEDED\n\n## 1. 关键指标\n\n2026年8月卖了 140,274.29。"
    cleaned = apply_hard_fixes(dirty)
    assert "COST_BUDGET_EXCEEDED" not in cleaned
    issues = hard_issues(
        cleaned, message="出一份2026年9月的采购月报",
        evidence='{"insights":[{"evidence":{"amount": 20092.83}}]}',
    )
    assert any(item["issue_type"] == "logic" and "2026年9月" in item["description"] for item in issues)
    assert any(item["issue_type"] == "hallucination" and "140,274.29" in item["description"] for item in issues)
    assert any(item["section"] == "摘要" for item in issues)


def test_bound_context_records_a_model_call_without_explicit_ledger(db):
    from datetime import timedelta

    from backend.db import utcnow
    from backend.llm import FakeLLM
    from backend.llm.calls import flash_json
    from backend.llm.context import bind_call_context

    run, _created = db.create_agent_run("dist-1", "u-a", "report", "ctx-trace", {"message": "月报"})
    claimed = db.claim_chat_turn(run["id"], "worker-test", utcnow() + timedelta(minutes=5))
    assert claimed is not None
    step = db.insert_step(run["id"], "tool", "report.draft", {})
    assert step is not None
    llm = FakeLLM(json_replies=[{"content": "指标", "key_points": []}])
    with bind_call_context(db=db, run_id=run["id"], step_id=step["id"], purpose="report"):
        body = flash_json(
            system="写", messages=[{"role": "user", "content": "关键指标"}],
            schema={"name": "report_section", "schema": {"type": "object"}},
            client=llm,
        )
    assert body["content"] == "指标"
    calls = [
        row for row in db.run_trace("dist-1", run["id"])["model_calls"]
        if row["step_id"] == step["id"]
    ]
    assert len(calls) == 1
    assert calls[0]["status"] == "succeeded"
