"""对话里的销售报告，一次写成草稿。

规划给关键指标、业绩、库存和下一周期计划安排问题和检索。问题与风险先留空。
text2sql 执行取数，并对返回的行打分。80 分及以上就用这些行；低于 80 分换问题再查，最多 3 次。
业绩和库存两节画图。下一周期计划检索公开网页。
查回的行交给一次模型调用写出四个独立的 analyze 函数，失败只修那一个，再交给撰写。四节正文在报表 worker 里同时写，摘要和问题与风险仍等正文。
出图按查询绑定：规划之后由代码给问题打 pie_category / trend / bar，一组结果最多一张图。空结果跳过模型；画出来对不上绑定行的丢掉，不走 code.fix。
出图和洞察在报表 worker 里同时打模型，不另开子 Run。
下一周期计划的网页检索和取数同一波派发。
代码根据已经查出的行标风险；有标记才写问题与风险，没有就整节不写，不再补查。
这一节写完再写摘要。
整份写完后先跑代码硬检查，再打分。85 分及以上过关。低于 85 分只重写扣分的那几节，默认两轮。
草稿留在会话里。用户确认后写入 narrative_reports，不写入 sales_reports。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, TypeVar

from ..codefix import format_failures, with_reflection
from ..db import Database, utcnow
from ..llm import calls as model_calls
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..memory import actor_id, attach, prompt_context, prompt_turns, standing_block, summary_block
from ..text2sql import QueryResult
from ..text2sql.impl import answer_question_impl
from ..text2sql.table_metadata import catalog_text, scope_views
from ..text2sql.text2sql import _json_rows, llm_result_view, prompt_view
from ..tools.web_search import search
from .analyze import risk_flags
from .charts import (
    ChartDecision,
    chart_role,
    chart_user_hint,
    decision_period,
    pie_category_question,
    skip_note,
    skip_reason,
    validate_chart,
    wants_category_pie,
)
from .check import apply_hard_fixes, hard_issues
from .prompt import (
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
from .wizard import execute

MAX_SECTION_QUESTIONS = 3
MAX_SECTION_SEARCHES = 5
PASS_SCORE = 85
MAX_REVIEW_ROUNDS = 2
_CHAPTER_POINTS = 20
_REQUIRED_CHAPTERS = ("关键指标", "摘要", "业绩表现", "库存情况", "下一周期计划")
_QUESTION = 2_000
_SEARCH = 200
_SNIPPET = 300
_FALLBACK = "这个合作关系最近的订单和动销"
_T = TypeVar("_T")
_SCHEMA_PURPOSE = {
    "report_plan": "report.optional",
    "report_charts": "report.optional",
    "code_wizards": "report.optional",
    "code_wizard": "report.optional",
    "report_section": "report.write",
    "report_review": "report.review",
    "report_revise": "report.review",
}


def purpose_for_schema(schema: dict[str, Any] | None) -> str:
    """Split the old catch-all `report` budget: writes, review, and the rest."""
    name = (schema or {}).get("name") if isinstance(schema, dict) else None
    return _SCHEMA_PURPOSE.get(str(name or ""), "report")


def _overlap(*fns: Callable[[], _T]) -> list[_T]:
    """Run independent callables that only wait on model HTTP.

    Each thread rebinds the ledger ContextVar. One worker seat, extra GATE_MODEL
    seats. Not a second outbox task — both callables belong to this report step.
    """
    if len(fns) <= 1:
        return [fn() for fn in fns]
    from ..llm.context import current_call_context, reset_call_context, set_call_context

    ledger = current_call_context()
    boxed: list[list[_T]] = [[] for _ in fns]
    errors: list[BaseException | None] = [None] * len(fns)

    def run(index: int, fn: Callable[[], _T]) -> None:
        token = set_call_context(
            db=ledger.db, run_id=ledger.run_id, step_id=ledger.step_id, purpose=ledger.purpose,
        )
        try:
            boxed[index].append(fn())
        except BaseException as exc:
            errors[index] = exc
        finally:
            reset_call_context(token)

    threads = [threading.Thread(target=run, args=(index, fn)) for index, fn in enumerate(fns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for exc in errors:
        if exc is not None:
            raise exc
    return [items[0] for items in boxed]


@dataclass(frozen=True)
class _RunTrace:
    db: Database | None = None
    run_id: str | None = None
    step_id: str | None = None

def _call_json(
    llm: Any,
    *, 
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any] | None = None,
    temperature: float = 0.0,
    flash: bool = False,
    trace: _RunTrace | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    purpose = purpose or purpose_for_schema(schema)
    ledger: dict[str, Any] = {"purpose": purpose}
    if trace is not None and trace.db is not None and trace.run_id and trace.step_id:
        ledger.update(db=trace.db, run_id=trace.run_id, step_id=trace.step_id)
    # Review/revise take cheap Qwen JSON. The rest of the report still uses official flash.
    if purpose == "report.review":
        return model_calls.qwen_json(
            system=system, messages=messages, schema=schema, temperature=temperature,
            client=llm, **ledger,
        )
    if flash or model in {model_calls.DEEPSEEK_FLASH, model_calls.SCHOOL_REASONER}:
        return model_calls.flash_json(
            system=system, messages=messages, schema=schema, temperature=temperature,
            client=llm, **ledger,
        )
    return model_calls.school_json(
        system=system, messages=messages, schema=schema, temperature=temperature,
        client=llm, **ledger,
    )

@dataclass(frozen=True)
class ReportDraft:
    text: str
    charts: list[dict[str, Any]]
    web_notes: list[str]
    warnings: list[str]
    data_query: dict[str, Any] | None
    series: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    chart_status: list[dict[str, Any]] = field(default_factory=list)
    named_period: str | None = None
    named_period_empty: bool = False


@dataclass
class _Ask:
    id: str
    question: str
    result: QueryResult


@dataclass
class _Search:
    query: str
    ok: bool
    results: list[dict[str, str]]
    error: str | None


def plan_schema() -> dict[str, Any]:
    return {
        "name": "report_plan", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "sections": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "enum": [section["id"] for section in SECTIONS]},
                        "questions": {"type": "array", "items": {"type": "string"}},
                        "searches": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "questions", "searches"],
                }},
            },
            "required": ["title", "sections"],
        },
    }


def code_wizard_schema() -> dict[str, Any]:
    return {
        "name": "code_wizard", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    }


_INSIGHT_SECTION_IDS = tuple(
    str(section["id"]) for section in SECTIONS if section["id"] not in {"summary", "risks"}
)


def code_wizards_schema() -> dict[str, Any]:
    properties = {sid: {"type": "string"} for sid in _INSIGHT_SECTION_IDS}
    return {
        "name": "code_wizards", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": properties,
            "required": list(_INSIGHT_SECTION_IDS),
        },
    }


def section_schema() -> dict[str, Any]:
    return {
        "name": "report_section", "strict": False,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "content": {"type": "string"},
                "key_points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content", "key_points"],
        },
    }


def chart_schema() -> dict[str, Any]:
    """The chart object the prompt asks for. `echarts_option` stays open: a closed schema would drop axes and series."""
    return {
        "name": "report_charts", "strict": False,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "charts": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "subtitle": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["line", "bar", "pie", "horizontal_bar", "scatter", "radar"],
                        },
                        "echarts_option": {"type": "object"},
                    },
                    "required": ["id", "title", "subtitle", "type", "echarts_option"],
                }},
            },
            "required": ["charts"],
        },
    }


@dataclass(frozen=True)
class SectionPlan:
    id: str
    title: str
    brief: str
    sql: bool
    chart: bool
    search: bool
    require_search: bool
    questions: list[str]
    searches: list[str]


def default_searches(today: date) -> dict[str, list[str]]:
    """Public lookups used when a section that must search comes back empty."""
    return {
        "next": [
            f"{today.year}年{today.month}月 零售档期 促销热点",
            "即将到来的节假日 零售促销",
        ],
    }


def _clean(items: Any, cap: int, limit: int) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return kept
    for item in items:
        if len(kept) >= cap or not isinstance(item, str):
            continue
        text = " ".join(item.split())[:limit]
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        kept.append(text)
    return kept


@dataclass(frozen=True)
class RiskPlan:
    write: bool
    questions: list[str]
    searches: list[str]


def planned_questions(planned: list[SectionPlan]) -> list[str]:
    """Distinct questions in plan order. Risks stays empty until after the chapters."""
    seen: set[str] = set()
    asked: list[str] = []
    for section in planned:
        for question in section.questions:
            key = question.casefold()
            if key in seen:
                continue
            seen.add(key)
            asked.append(question)
    return asked


def asks_from_payloads(
    planned: list[SectionPlan],
    results: dict[str, dict[str, Any]],
    questions: list[tuple[str, str]],
) -> tuple[list[_Ask], dict[str, list[_Ask]], list[str]]:
    """Rebuild the first query wave from child `data.query` payloads."""
    asked: list[_Ask] = []
    by_question: dict[str, _Ask] = {}
    failures: list[str] = []
    for task_id, question in questions:
        item = _ask_from_result(task_id, question, results.get(task_id) or {})
        asked.append(item)
        by_question[question.casefold()] = item
        if item.result.handoff:
            failures.append(item.result.handoff)
    by_section: dict[str, list[_Ask]] = {section.id: [] for section in planned}
    for section in planned:
        for question in section.questions:
            item = by_question.get(question.casefold())
            if item is not None:
                by_section[section.id].append(item)
    return asked, by_section, failures


def planned_searches(planned: list[SectionPlan]) -> list[str]:
    """Distinct web queries in plan order."""
    return _search_queries(planned)


def searches_from_payloads(
    queries: list[tuple[str, str]],
    results: dict[str, dict[str, Any]],
) -> list[_Search]:
    """Rebuild the first-wave web_search children."""
    found: list[_Search] = []
    for task_id, query in queries:
        found.append(_search_from_result(query, results.get(task_id) or {}))
    return found


def merge_extra_asks(
    asked: list[_Ask],
    by_section: dict[str, list[_Ask]],
    questions: list[tuple[str, str]],
    results: dict[str, dict[str, Any]],
    *,
    section_id: str = "risks",
) -> list[str]:
    """Append extra data-query results. Kept for reconstructing older traces."""
    notes: list[str] = []
    seen = {item.question.casefold(): item for item in asked}
    bucket = by_section.setdefault(section_id, [])
    have = {item.id for item in bucket}
    for task_id, question in questions:
        item = seen.get(question.casefold())
        if item is None:
            item = _ask_from_result(task_id, question, results.get(task_id) or {})
            seen[question.casefold()] = item
            asked.append(item)
            if item.result.handoff:
                notes.append(item.result.handoff)
        if item.id not in have:
            have.add(item.id)
            bucket.append(item)
    return notes


def _ask_from_result(task_id: str, question: str, result: dict[str, Any]) -> _Ask:
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else result
    if not isinstance(payload, dict):
        payload = {}
    return _Ask(task_id, question, _query_result(payload))


def _query_result(raw: dict[str, Any]) -> QueryResult:
    return QueryResult(
        ok=bool(raw.get("ok")),
        sql=raw.get("sql") if isinstance(raw.get("sql"), str) else None,
        rows=list(raw.get("rows") or []) if isinstance(raw.get("rows"), list) else [],
        row_count=int(raw.get("row_count") or 0),
        columns=[str(column) for column in raw.get("columns") or [] if column],
        error=raw.get("error") if isinstance(raw.get("error"), str) else None,
        error_type=raw.get("error_type") if isinstance(raw.get("error_type"), str) else None,
        tables=[str(name) for name in raw.get("tables") or [] if name],
        log_id=raw.get("log_id") if isinstance(raw.get("log_id"), int) else None,
        handoff=str(raw.get("handoff") or ""),
        score=raw.get("score") if isinstance(raw.get("score"), int) else None,
        truncated=bool(raw.get("truncated")),
        missing_columns=[str(column) for column in raw.get("missing_columns") or [] if column],
    )


def parse_risk_plan(raw: Any) -> RiskPlan:
    """A false or unreadable decision writes nothing. Questions on that decision are dropped."""
    if not isinstance(raw, dict) or raw.get("write") is not True:
        return RiskPlan(False, [], [])
    return RiskPlan(
        True,
        _clean(raw.get("questions"), MAX_SECTION_QUESTIONS, _QUESTION),
        _clean(raw.get("searches"), MAX_SECTION_SEARCHES, _SEARCH),
    )


def parse_report_title(raw: Any) -> str:
    """The document title from the plan. A missing or blank one stays empty."""
    if not isinstance(raw, dict):
        return ""
    return " ".join(str(raw.get("title") or "").split())[:80]


def parse_plan(raw: Any, *, today: date | None = None) -> list[SectionPlan]:
    """Keep the six sections. Questions are only the ones the model wrote.

    Risks stays empty. Flags for that section come from the already-fetched rows.
    Metrics questions are dropped: that chapter runs templated SQL at resume.
    """
    today = today or utcnow().date()
    supplied: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for item in raw.get("sections") or []:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "")
            if sid and sid not in supplied:
                supplied[sid] = item
    defaults = default_searches(today)
    planned: list[SectionPlan] = []
    for section in SECTIONS:
        item = supplied.get(str(section["id"]))
        questions: list[str] = []
        searches: list[str] = []
        if section["sql"] and section["id"] not in {"risks", "metrics"}:
            questions = _clean((item or {}).get("questions"), MAX_SECTION_QUESTIONS, _QUESTION) if item else []
        if section["search"] and section["id"] != "risks":
            if item is not None and "searches" in item:
                searches = _clean(item.get("searches"), MAX_SECTION_SEARCHES, _SEARCH)
            if section["require_search"] and not searches:
                searches = list(defaults.get(str(section["id"]), []))
        planned.append(SectionPlan(
            id=str(section["id"]),
            title=str(section["title"]),
            brief=str(section["brief"]),
            sql=bool(section["sql"]),
            chart=bool(section["chart"]),
            search=bool(section["search"]),
            require_search=bool(section["require_search"]),
            questions=questions,
            searches=searches,
        ))
    return planned


def ensure_chart_questions(planned: list[SectionPlan], message: str) -> list[SectionPlan]:
    """If the user asked for a category pie, make sure performance has a query that can draw it."""
    if not wants_category_pie(message):
        return planned
    filled: list[SectionPlan] = []
    for section in planned:
        if section.id != "performance":
            filled.append(section)
            continue
        if any(chart_role(question, message) == "pie_category" for question in section.questions):
            filled.append(section)
            continue
        question = pie_category_question(message)
        questions = list(section.questions)
        if len(questions) < MAX_SECTION_QUESTIONS:
            questions.append(question)
        else:
            replaced = False
            for index, existing in enumerate(questions):
                if chart_role(existing, message) != "trend":
                    questions[index] = question
                    replaced = True
                    break
            if not replaced:
                questions[-1] = question
        filled.append(replace(section, questions=questions))
    return filled


def iter_draft(
    db: Database, llm: Any, *, message: str, history: list[dict[str, Any]],
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    hint: str | None = None, search_fn: Any = None, folded_through: int = 0, 
    summary: str = "", run_id: str | None = None, step_id: str | None = None,
) -> Iterator[dict[str, Any] | ReportDraft]:
    """Yield a status around each slow step, then the draft."""
    # Importing orchestrator.status at module level loads orchestrator.turn, which imports this module.
    from ..llm.context import bind_call_context
    from ..orchestrator.status import status

    with bind_call_context(db=db, run_id=run_id, step_id=step_id, purpose="report"):
        yield from _iter_draft(
            db, llm, message=message, history=history, partnership_id=partnership_id,
            brand_id=brand_id, distributor_id=distributor_id, session_id=session_id,
            hint=hint, search_fn=search_fn, folded_through=folded_through, summary=summary,
            run_id=run_id, step_id=step_id, status=status,
        )


def _iter_draft(
    db: Database, llm: Any, *, message: str, history: list[dict[str, Any]],
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    hint: str | None, search_fn: Any, folded_through: int, summary: str,
    run_id: str | None, step_id: str | None, status: Any,
) -> Iterator[dict[str, Any] | ReportDraft]:
    trace = _RunTrace(db=db, run_id=run_id, step_id=step_id)
    remembered = prompt_context(standing_block(db, partnership_id, actor_id(db, session_id)), summary_block(summary))
    yield status("report-plan", "规划", "正在按大纲安排每一节要查什么", state="running")
    planned, report_title = _plan(
        llm, message=message, history=history, hint=hint, remembered=remembered, folded_through=folded_through, trace=trace
    )
    question_count = len({question.casefold() for section in planned for question in section.questions})
    search_count = len({query.casefold() for section in planned for query in section.searches})
    yield status("report-plan", "规划", f"{question_count} 个问题，{search_count} 次检索", state="done")

    yield status("report-query", "查数", "正在查询目标、业绩和库存", state="running")
    asked, by_section, failures = _query(
        db, llm, planned, partnership_id=partnership_id, brand_id=brand_id, distributor_id=distributor_id,
        session_id=session_id, history=history, folded_through=folded_through, summary=summary,
        trace=trace,
    )
    yield status("report-query", "查数", f"查完 {len(asked)} 个问题", state="done")
    yield from iter_after_queries(
        db, llm, planned=planned, report_title=report_title, asked=asked, by_section=by_section,
        failures=failures, message=message, history=history, remembered=remembered,
        folded_through=folded_through, partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session_id,
        search_fn=search_fn, trace=trace,
    )


def iter_after_queries(
    db: Database, llm: Any, *, planned: list[SectionPlan], report_title: str,
    asked: list[_Ask], by_section: dict[str, list[_Ask]], failures: list[str],
    message: str, history: list[dict[str, Any]], remembered: str, folded_through: int,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    search_fn: Any = None, trace: _RunTrace | None = None,
    found: list[_Search] | None = None,
) -> Iterator[dict[str, Any] | ReportDraft]:
    """Charts through review. Risks are flags on the already-fetched rows, not a second query wave.

    `found` from the first wave skips the in-process search. None means look them up here.
    """
    from ..orchestrator.status import status

    asked, by_section = _attach_metrics(
        db, message, planned, asked, by_section,
        partnership_id=partnership_id, brand_id=brand_id, distributor_id=distributor_id,
        session_id=session_id, trace=trace,
    )
    _hydrate_full_rows(
        db, asked, partnership_id=partnership_id, distributor_id=distributor_id,
        brand_id=brand_id, session_id=session_id,
    )
    if found is None:
        yield status("report-news", "新闻", "正在看热点和节假日", state="running")
        found = [_lookup(query, search_fn or search) for query in _search_queries(planned)]
    yield status("report-news", "新闻", f"看了 {len(found)} 条检索", state="done")

    yield status("report-chart", "图表", "正在给业绩和库存配图", state="running")
    yield status("report-insight", "洞察", "正在按章节计算", state="running")
    query_results = _query_results(asked, include_full=True)
    charts, charts_for, chart_failures, insights_by_section, insight_errors, insight_failures, chart_decisions = (
        _charts_and_insights(llm, message, planned, by_section, query_results, trace)
    )
    yield status("report-chart", "图表", f"{len(charts)} 张图" if charts else "这组数不画图", state="done")
    insight_count = sum(len(items) for items in insights_by_section.values())
    yield status("report-insight", "洞察", f"{insight_count} 条" if insight_count else "没有算出洞察", state="done")

    yield status("report-write", "正文", "正在写关键指标、业绩、库存和下一周期计划", state="running")
    texts = _write_body_sections(
        llm, message, history, planned, by_section, found, charts_for, insights_by_section,
        remembered=remembered, folded_through=folded_through, trace=trace,
        chart_decisions=chart_decisions,
    )
    written = sum(
        1 for section in planned
        if section.id not in {"summary", "risks"} and texts.get(section.id, "").strip()
    )
    yield status("report-write", "正文", f"{written} 节已写完", state="done")

    yield status("report-risks", "问题与风险", "正在根据查询结果标风险", state="running")
    flags = risk_flags(query_results, today=utcnow().date())
    if flags:
        insights_by_section["risks"] = flags
    yield from iter_from_risks(
        db, llm, planned=planned, report_title=report_title, asked=asked, by_section=by_section,
        failures=failures, charts=charts, charts_for=charts_for, chart_failures=chart_failures,
        found=found, insights_by_section=insights_by_section, insight_failures=insight_failures,
        insight_errors=insight_errors, texts=texts,
        message=message, history=history, remembered=remembered, folded_through=folded_through,
        trace=trace, write=bool(flags), chart_decisions=chart_decisions,
    )


def iter_from_risks(
    db: Database, llm: Any, *, planned: list[SectionPlan], report_title: str,
    asked: list[_Ask], by_section: dict[str, list[_Ask]], failures: list[str],
    charts: list[dict[str, Any]], charts_for: dict[str, list[dict[str, Any]]],
    chart_failures: list[str], found: list[_Search],
    insights_by_section: dict[str, list[dict[str, Any]]], insight_failures: list[str],
    insight_errors: list[str], texts: dict[str, str],
    message: str, history: list[dict[str, Any]], remembered: str, folded_through: int,
    trace: _RunTrace | None = None, write: bool,
    chart_decisions: list[ChartDecision] | None = None,
) -> Iterator[dict[str, Any] | ReportDraft]:
    """Write risks from flags (if any), then summary and review."""
    from ..orchestrator.status import status

    if write:
        risks_section = next(section for section in planned if section.id == "risks")
        yield status("report-risks", "问题与风险", "正在写这一节", state="running")
        content, _points = _write_section(
            llm, message, history, risks_section, _asks_for(risks_section, by_section), found, [],
            _insights_for(risks_section, insights_by_section),
            remembered=remembered, folded_through=folded_through, prior=_body_for_summary(planned, texts),
            trace=trace,
        )
        if content.strip():
            texts["risks"] = finish_section(content, [])
            yield status("report-risks", "问题与风险", "已写完", state="done")
        else:
            insights_by_section.pop("risks", None)
            yield status("report-risks", "问题与风险", "看过之后这一节不写", state="done")
    else:
        yield status("report-risks", "问题与风险", "查询结果里没有要单写的问题", state="done")

    yield status("report-summary", "摘要", "正在根据已写章节写摘要", state="running")
    summary_text, _points = _write_summary(
        llm, message, history, _body_for_summary(planned, texts),
        remembered=remembered, folded_through=folded_through,
        trace=trace,
    )
    texts["summary"] = finish_section(strip_chart_refs(summary_text), []) or "摘要没有写成。"
    yield status("report-summary", "摘要", "摘要已写完", state="done")

    report = _assemble(planned, texts) or "The report draft is empty."
    evidence = _report_evidence(asked, insights_by_section, found, charts, chart_decisions or [])
    yield status("report-review", "审核", "正在给整份报告打分", state="running")
    report, score, review_error = _review_rounds(
        llm, message=message, report=report, evidence=evidence, remembered=remembered,
        planned=planned, charts_for=charts_for, trace=trace,
    )
    if review_error or score is None:
        review_note = "审核没有完成"
    elif score >= PASS_SCORE:
        review_note = f"{score} 分，过关"
    else:
        review_note = f"{score} 分，未过关"
    yield status("report-review", "审核", review_note, state="done")
    report = finish_report(report, planned, charts_for)

    warnings = _warnings(asked, found)
    warnings.extend(failures)
    warnings.extend(chart_failures)
    warnings.extend(insight_failures)
    if insight_errors and not insight_failures:
        warnings.append("Insights were not computed: " + "; ".join(insight_errors)[:160])
    if review_error:
        warnings.append(f"报告审核没有完成: {review_error[:160]}")
    drawable = any(
        item.result.ok and skip_reason(
            chart_role(item.question, message),
            item.result.full_rows if item.result.full_rows is not None else item.result.rows,
            item.result.columns or _column_names(
                item.result.full_rows if item.result.full_rows is not None else item.result.rows
            ),
        ) is None
        for section in planned if section.chart
        for item in by_section[section.id]
    )
    if drawable and not charts:
        warnings.append("业绩表现和库存情况没有画成图")
    from .metrics import named_period_absent, period_from_message

    named_period = period_from_message(message)
    m2 = next((item for item in asked if item.id == "m2"), None)
    named_empty = bool(
        named_period
        and m2 is not None
        and m2.result.ok
        and named_period_absent(named_period, m2.result.rows)
    )
    yield ReportDraft(
        text=report,
        charts=charts,
        web_notes=_notes(found),
        warnings=warnings,
        data_query=_mark(asked),
        series=series_payload(asked),
        title=report_title,
        chart_status=[item.as_dict() for item in chart_decisions or []],
        named_period=named_period,
        named_period_empty=named_empty,
    )


def _plan(
    llm: Any, *, message: str, history: list[dict[str, Any]], hint: str | None,
    remembered: str = "", folded_through: int = 0, trace: _RunTrace | None = None,
) -> tuple[list[SectionPlan], str]:
    recent = "\n".join(
        f"{turn['role']}: {str(turn.get('content') or '')[:500]}"
        for turn in prompt_turns(history, folded_through)
        if turn.get("role") in ("user", "assistant") and str(turn.get("content") or "").strip()
    )
    user = (
        f"=== 今天 ===\n{utcnow().date().isoformat()}\n\n"
        f"=== 最近对话 ===\n{recent or '（无）'}\n\n"
        f"=== 规划问题 ===\n{(hint or '').strip() or '（无）'}"
    )
    try:
        raw = _call_json(
            llm,
            model=model_calls.SCHOOL_REASONER,
            system=attach(REPORT_QUESTIONS_PROMPT.format(
                max_questions=MAX_SECTION_QUESTIONS,
                max_searches=MAX_SECTION_SEARCHES,
                message=(message or "").strip()[:4000] or "（无文字）",
                database_structure=catalog_text(),
                outline=outline_text(),
            ), remembered),
            messages=[{"role": "user", "content": user}],
            schema=plan_schema(), 
            temperature=0,
            trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception:
        raw = {}
    planned = ensure_chart_questions(parse_plan(raw, today=utcnow().date()), message)
    return planned, parse_report_title(raw)


def _fill_risks(planned: list[SectionPlan], questions: list[str], searches: list[str]) -> list[SectionPlan]:
    """Replace the empty risks slot with the questions and searches chosen after the chapters."""
    filled: list[SectionPlan] = []
    for section in planned:
        if section.id != "risks":
            filled.append(section)
            continue
        filled.append(SectionPlan(
            id=section.id, title=section.title, brief=section.brief,
            sql=section.sql, chart=section.chart, search=section.search,
            require_search=section.require_search, questions=questions, searches=searches,
        ))
    return filled


def _query(
    db: Database, llm: Any, planned: list[SectionPlan], *,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    history: list[dict[str, Any]], folded_through: int, summary: str,
    trace: _RunTrace | None = None,
) -> tuple[list[_Ask], dict[str, list[_Ask]], list[str]]:
    """Run each distinct planned question once. Repair and the reflection score live in text2sql."""
    seen: dict[str, _Ask] = {}
    asked: list[_Ask] = []
    failures: list[str] = []
    by_section: dict[str, list[_Ask]] = {section.id: [] for section in planned}
    for section in planned:
        for question in section.questions:
            key = question.casefold()
            item = seen.get(key)
            if item is None:
                item, note = _ask(
                    db, llm, question=question, qid=f"q{len(asked) + 1}",
                    partnership_id=partnership_id, brand_id=brand_id, distributor_id=distributor_id,
                    session_id=session_id, history=history, folded_through=folded_through, summary=summary,
                    trace=trace,
                )
                seen[key] = item
                asked.append(item)
                if note:
                    failures.append(note)
            by_section[section.id].append(item)
    return asked, by_section, failures


def _ask(
    db: Database, llm: Any, *, question: str, qid: str,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    history: list[dict[str, Any]], folded_through: int, summary: str,
    trace: _RunTrace | None = None,
) -> tuple[_Ask, str]:
    """One planned question. Statement repair and the reflection score live in text2sql."""
    result = answer_question_impl()(
        db, question=question, partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session_id, history=history,
        folded_through=folded_through, summary=summary,
        run_id=None if trace is None else trace.run_id, 
        step_id=None if trace is None else trace.step_id,
    )
    return _Ask(qid, question, result), result.handoff


def _asks_for(section: SectionPlan, by_section: dict[str, list[_Ask]]) -> list[_Ask]:
    """Next period and risks read the performance and inventory rows. They do not query again."""
    asked = list(by_section[section.id])
    if section.id not in {"next", "risks"}:
        return asked
    seen = {item.id for item in asked}
    for source in ("performance", "inventory"):
        for item in by_section.get(source, []):
            if item.id in seen:
                continue
            seen.add(item.id)
            asked.append(item)
    return asked


def _search_queries(planned: list[SectionPlan]) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for section in planned:
        for query in section.searches:
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            queries.append(query)
    return queries


def _lookup(query: str, search_fn: Any) -> _Search:
    try:
        rows = search_fn(query) or []
    except Exception as err:
        return _Search(query=query, ok=False, results=[], error=str(err).split("\n", 1)[0][:200])
    return _Search(query=query, ok=True, results=_search_hits(rows), error=None)


def _search_from_result(query: str, result: dict[str, Any]) -> _Search:
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else result
    if not isinstance(payload, dict):
        payload = {}
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    error = result.get("error") or payload.get("error")
    ok = result.get("ok", payload.get("ok"))
    if ok is False or error:
        return _Search(
            query=query, ok=False, results=[],
            error=str(error or "search failed").split("\n", 1)[0][:200],
        )
    rows = nested.get("results") if isinstance(nested, dict) else None
    if rows is None and not result:
        return _Search(query=query, ok=False, results=[], error="search result missing")
    return _Search(query=query, ok=True, results=_search_hits(rows), error=None)


def _search_hits(rows: Any) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return hits
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        hits.append({
            "title": str(row.get("title") or "").strip()[:160],
            "snippet": str(row.get("snippet") or "").strip()[:_SNIPPET],
            "url": str(row.get("url") or "").strip()[:300],
        })
    return hits


def _echarts_charts(specs: Any) -> list[dict[str, Any]]:
    """Keep charts that already carry an ECharts option."""
    if not isinstance(specs, list):
        return []
    kept: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        option = spec.get("echarts_option")
        title = str(spec.get("title") or "").strip()
        if not title or not isinstance(option, dict) or not option:
            continue
        kept.append({
            "id": str(spec.get("id") or f"chart_{len(kept) + 1:03d}"),
            "title": title[:120],
            "subtitle": str(spec.get("subtitle") or "").strip()[:120],
            "type": str(spec.get("type") or ""),
            "echarts_option": option,
        })
    return kept


def _attach_metrics(
    db: Database, message: str, planned: list[SectionPlan],
    asked: list[_Ask], by_section: dict[str, list[_Ask]], *,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    trace: _RunTrace | None = None,
) -> tuple[list[_Ask], dict[str, list[_Ask]]]:
    """Fill the metrics chapter from templated SQL. Skipped when that chapter is absent."""
    if not any(section.id == "metrics" for section in planned):
        return asked, by_section
    from .metrics import fetch_metric_results

    extras = fetch_metric_results(
        db, message=message, partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session_id,
        run_id=None if trace is None else trace.run_id,
    )
    bucket = by_section.setdefault("metrics", [])
    have = {item.id for item in asked}
    for qid, question, result in extras:
        if qid in have:
            continue
        item = _Ask(qid, question, result)
        asked.append(item)
        bucket.append(item)
        have.add(qid)
    return asked, by_section


def _hydrate_full_rows(
    db: Database, asked: list[_Ask], *, partnership_id: str, brand_id: str,
    distributor_id: str, session_id: str,
) -> None:
    """Load complete rows for code/chart consumers; prose paths keep the bounded view."""
    from ..text2sql.text2sql import _scope_params

    params = _scope_params(partnership_id, brand_id, distributor_id, actor_id(db, session_id))
    for item in asked:
        result = item.result
        if not result.ok or not result.log_id or result.full_rows is not None:
            continue
        try:
            rows = db.load_text2sql_rows(result.log_id, partnership_id, scope_views(), params)
        except Exception:
            continue
        result.full_rows = _json_rows(rows)


def _draw_charts(
    llm: Any, message: str, planned: list[SectionPlan], by_section: dict[str, list[_Ask]],
    trace: _RunTrace | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str], list[ChartDecision]]:
    charts: list[dict[str, Any]] = []
    charts_for: dict[str, list[dict[str, Any]]] = {}
    used_chart_ids: set[str] = set()
    chart_failures: list[str] = []
    decisions: list[ChartDecision] = []
    for section in planned:
        if not section.chart:
            continue
        made: list[dict[str, Any]] = []
        for item in by_section.get(section.id) or []:
            drawn, fails, decision = _chart_one(llm, message, section.id, item, trace=trace)
            chart_failures.extend(fails)
            if drawn:
                fitted = fit_chart_axes(_assign_chart_ids(drawn[:1], used_chart_ids))
                made.extend(fitted)
                first = fitted[0]
                decisions.append(replace(
                    decision, status="drawn",
                    chart_id=str(first.get("id") or ""),
                    title=str(first.get("title") or ""),
                    reason="", note="",
                ))
            else:
                decisions.append(decision)
        charts_for[section.id] = made
        charts.extend(made)
    return charts, charts_for, chart_failures, decisions


def _charts_and_insights(
    llm: Any, message: str, planned: list[SectionPlan], by_section: dict[str, list[_Ask]],
    query_results: list[dict[str, Any]], trace: _RunTrace | None = None,
) -> tuple[
    list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str],
    dict[str, list[dict[str, Any]]], list[str], list[str], list[ChartDecision],
]:
    """Charts and the four insight functions wait on the model at the same time."""
    insight_sections = [section for section in planned if section.id in _INSIGHT_SECTION_IDS]
    chart_pack, insight_pack = _overlap(
        lambda: _draw_charts(llm, message, planned, by_section, trace),
        lambda: _code_wizards(llm, insight_sections, query_results, trace=trace),
    )
    charts, charts_for, chart_failures, decisions = chart_pack
    insights_by_section, insight_errors, insight_failures = insight_pack
    return charts, charts_for, chart_failures, insights_by_section, insight_errors, insight_failures, decisions


def _chart_one(
    llm: Any, message: str, section_id: str, item: _Ask, trace: _RunTrace | None = None,
) -> tuple[list[dict[str, Any]], list[str], ChartDecision]:
    """One query, one chart attempt. Empty rows skip the model; a bad drawing is dropped."""
    role = chart_role(item.question, message)
    period = decision_period(item.question, message)
    pending = ChartDecision(
        dataset_id=item.id, section_id=section_id, question=item.question,
        role=role, status="skipped", period=period,
    )
    rows = item.result.full_rows if item.result.full_rows is not None else item.result.rows
    columns = item.result.columns or _column_names(rows)
    if not item.result.ok:
        reason = "no_data"
        return [], [], replace(pending, reason=reason, note=skip_note(role, reason, period))
    reason = skip_reason(role, rows, columns)
    if reason:
        return [], [], replace(pending, reason=reason, note=skip_note(role, reason, period))
    query_results = [{
        "id": item.id,
        "question": item.question,
        **prompt_view(
            rows, columns=columns, row_count=item.result.row_count, truncated=item.result.truncated,
        ),
    }]
    try:
        raw = _call_json(
            llm,
            model=model_calls.DEEPSEEK_FLASH,
            system=CHART_GENERATION_PROMPT.format(
                message=(message or "").strip()[:4000] or "（无文字）",
                query_results=json.dumps(query_results, ensure_ascii=False),
            ),
            messages=[{"role": "user", "content": chart_user_hint(role, message)}],
            schema=chart_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as err:
        raw = {"error": str(err).split("\n", 1)[0][:300]}

    def run(text: str) -> tuple[bool, list[dict[str, Any]], str | None, str]:
        try:
            parsed = json.loads(text) if text.strip().startswith("{") or text.strip().startswith("[") else {}
        except json.JSONDecodeError as err:
            return False, [], f"图表 JSON 无法解析: {err.msg}", ""
        specs = parsed.get("charts") if isinstance(parsed, dict) else parsed
        charts = _echarts_charts(specs)
        kept = [
            chart for chart in charts
            if validate_chart(chart, role=role, message=message, rows=rows, columns=columns) is None
        ]
        if kept:
            return True, kept[:1], None, ""
        if charts:
            return True, [], None, ""
        return False, [], "图表 JSON 没有可用的 echarts_option", ""

    charts, attempts = with_reflection(
        llm, kind="chart", first=json.dumps(raw, ensure_ascii=False), run=run, trace=trace,
    )
    if charts:
        return charts[:1], [], pending
    dropped = "bad_dimension" if not attempts else "no_chart"
    fails = [format_failures(attempts)] if attempts else []
    return [], fails, replace(pending, reason=dropped, note=skip_note(role, dropped, period))


def chart_for_query(
    db: Database, llm: Any, message: str, question: str, result: QueryResult, *,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str,
    run_id: str | None = None, step_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One chart for a single chat question, from rows text2sql already fetched.

    The report draft draws charts per section; a lone "draw me a pie" never reaches it, so the data
    path calls this instead. Same chart prompt, schema, id and axis fitting as the draft.
    Returns ``(charts, failures)``; an empty ``charts`` means nothing was drawn and the caller must say so.
    """
    asked = [_Ask("chart", question, result)]
    _hydrate_full_rows(
        db, asked, partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session_id,
    )
    made, failures, _decision = _chart_one(
        llm, message, "chat", asked[0], trace=_RunTrace(db=db, run_id=run_id, step_id=step_id),
    )
    return fit_chart_axes(_assign_chart_ids(made, set())), failures


def _query_results(asked: list[_Ask], *, include_full: bool = False) -> list[dict[str, Any]]:
    """Every successful text2sql result on this draft. Later chapters can read earlier rows."""
    results = []
    for item in asked:
        if not item.result.ok or not item.result.rows:
            continue
        source_rows = item.result.full_rows if include_full and item.result.full_rows is not None else item.result.rows
        columns = item.result.columns or _column_names(source_rows)
        if not include_full:
            results.append({
                "id": item.id, "question": item.question,
                **prompt_view(
                    source_rows, columns=columns, row_count=item.result.row_count,
                    truncated=item.result.truncated,
                ),
            })
            continue
        # Code consumers (analyze(datasets), charts) get every row the handle returns. The
        # `llm_*` fields keep what the query reported, for the prompt that describes the data.
        results.append({
            "id": item.id,
            "question": item.question,
            "row_count": len(source_rows),
            "truncated": False,
            "columns": columns,
            "rows": source_rows,
            "llm_row_count": item.result.row_count,
            "llm_truncated": item.result.truncated,
        })
    return results


def _column_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in names:
                names.append(text)
    return names


def _dataset_catalog(query_results: list[dict[str, Any]]) -> str:
    """Which columns each successful query actually has. The repair step reads this."""
    lines = ["各次查询的列："]
    for index, item in enumerate(query_results):
        columns = item.get("columns") or _column_names(item.get("rows") or [])
        question = str(item.get("question") or "")
        lines.append(f"datasets[{index}] question={question} columns={columns}")
    return "\n".join(lines)


def _insights_for(section: SectionPlan, by_section: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Next period also cites performance and inventory. Risks only cites its flags."""
    if section.id == "risks":
        return list(by_section.get("risks") or [])
    sources = (section.id,) if section.id != "next" else (section.id, "performance", "inventory")
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in sources:
        for item in by_section.get(sid, []):
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _prompt_query_results(query_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_results = []
    for item in query_results:
        prompt_results.append({
            "id": item.get("id"), "question": item.get("question"),
            **prompt_view(
                item.get("rows") or [], columns=item.get("columns"),
                row_count=item.get("llm_row_count", item.get("row_count")),
                truncated=bool(item.get("llm_truncated", item.get("truncated"))),
            ),
        })
    return prompt_results


def _insight_section_block(sections: list[SectionPlan]) -> str:
    lines: list[str] = []
    for section in sections:
        lines.append(
            f"- `{section.id}` 「{section.title}」：{section.brief}\n"
            f"  questions: {json.dumps(section.questions, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _run_analyze(
    llm: Any, code: str, query_results: list[dict[str, Any]],
    trace: _RunTrace | None = None,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Run one analyze(datasets). A failure is repaired on its own."""
    if not str(code or "").strip():
        return [], None, []

    def run(snippet: str) -> tuple[bool, list[dict[str, Any]], str | None, str]:
        result = execute(str(snippet or ""), query_results)
        if result["error"]:
            return False, [], result["error"], _dataset_catalog(query_results)
        return True, result["insights"], None, ""

    insights, attempts = with_reflection(llm, kind="code", first=str(code or ""), run=run, trace=trace)
    return insights or [], None, [format_failures(attempts)] if attempts else []


def _code_wizards(
    llm: Any, sections: list[SectionPlan], query_results: list[dict[str, Any]],
    trace: _RunTrace | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[str]]:
    """One prompt, four analyze(datasets) functions. A failure is repaired on its own."""
    by_section: dict[str, list[dict[str, Any]]] = {section.id: [] for section in sections}
    if not sections:
        return by_section, [], []
    if not query_results:
        return by_section, [], []
    try:
        raw = _call_json(
            llm,
            model=model_calls.DEEPSEEK_FLASH,
            system=CODE_WIZARDS_PROMPT.format(
                sections=_insight_section_block(sections),
                query_results=json.dumps(_prompt_query_results(query_results), ensure_ascii=False, default=str),
            ),
            messages=[{"role": "user", "content": "为四个章节各写一段独立的 analyze(datasets)。"}],
            schema=code_wizards_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as err:
        message = str(err).split("\n", 1)[0][:200]
        return by_section, [f"{section.title}: {message}" for section in sections], []
    if not isinstance(raw, dict):
        raw = {}
    errors: list[str] = []
    failures: list[str] = []
    for section in sections:
        insights, err, fails = _run_analyze(
            llm, str(raw.get(section.id) or ""), query_results, trace=trace,
        )
        by_section[section.id] = insights
        if err:
            errors.append(f"{section.title}: {err}")
        failures.extend(fails)
    return by_section, errors, failures


def _code_wizard(
    llm: Any, section: SectionPlan, query_results: list[dict[str, Any]],
    trace: _RunTrace | None = None,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Ask for analyze(datasets) for one chapter. datasets is every successful query."""
    if not query_results:
        return [], None, []
    try:
        raw = _call_json(
            llm,
            model=model_calls.DEEPSEEK_FLASH,
            system=CODE_WIZARD_PROMPT.format(
                section_title=section.title,
                section_brief=section.brief,
                questions=json.dumps(section.questions, ensure_ascii=False),
                query_results=json.dumps(_prompt_query_results(query_results), ensure_ascii=False, default=str),
            ),
            messages=[{"role": "user", "content": "只为当前章节写 analyze(datasets)。"}],
            schema=code_wizard_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as err:
        return [], str(err).split("\n", 1)[0][:200], []
    code = raw.get("code") if isinstance(raw, dict) else ""
    return _run_analyze(llm, str(code or ""), query_results, trace=trace)


def _history(history: list[dict[str, Any]], folded_through: int) -> list[dict[str, Any]]:
    return [
        {"role": turn.get("role"), "content": str(turn.get("content") or "")}
        for turn in prompt_turns(history, folded_through)
        if turn.get("role") in ("user", "assistant")
    ]


def _section_payload(
    section: SectionPlan, asked: list[_Ask], found: list[_Search], charts: list[dict[str, Any]],
    insights: list[dict[str, Any]], prior: str = "",
    chart_notes: list[dict[str, Any]] | None = None,
) -> str:
    wanted = {query.casefold() for query in section.searches}
    rows = []
    for item in asked:
        view = llm_result_view(item.result)
        rows.append({
            "id": item.id, "question": item.question, "ok": item.result.ok,
            **view, "error": item.result.error,
        })
    payload = {"rows": rows, "insights": insights}
    web = [
        {"query": item.query, "ok": item.ok, "results": item.results if item.ok else [], "error": item.error}
        for item in found
        if item.query.casefold() in wanted
    ]
    drawn = [
        {
            "id": chart.get("id") or "",
            "title": chart.get("title") or "",
            "type": chart.get("type") or "",
        }
        for chart in charts
    ] if section.chart else []
    skipped = [
        {
            "role": note.get("role") or "",
            "reason": note.get("reason") or "",
            "period": note.get("period"),
            "note": note.get("note") or "",
        }
        for note in (chart_notes or [])
        if note.get("status") == "skipped"
    ] if section.chart else []
    if section.chart:
        chart_block = json.dumps({"drawn": drawn, "skipped": skipped}, ensure_ascii=False)
    else:
        chart_block = "（本节不用）"
    blocks = [
        f"=== 查询结果 ===\n{json.dumps(payload, ensure_ascii=False)}",
        f"=== 新闻 ===\n{json.dumps(web, ensure_ascii=False) if web else '（本节不用）'}",
        f"=== 图表 ===\n{chart_block}",
    ]
    if prior.strip():
        blocks.insert(0, f"=== 已写章节 ===\n{prior.strip()}")
    return "\n\n".join(blocks)


def _read_section(raw: Any) -> tuple[str, list[str]]:
    if not isinstance(raw, dict):
        return "", []
    content = str(raw.get("content") or "").strip()
    points = [str(item).strip() for item in raw.get("key_points") or [] if str(item).strip()]
    return content, points


def _write_body_sections(
    llm: Any, message: str, history: list[dict[str, Any]], planned: list[SectionPlan],
    by_section: dict[str, list[_Ask]], found: list[_Search],
    charts_for: dict[str, list[dict[str, Any]]], insights_by_section: dict[str, list[dict[str, Any]]],
    *, remembered: str = "", folded_through: int = 0, trace: _RunTrace | None = None,
    chart_decisions: list[ChartDecision] | None = None,
) -> dict[str, str]:
    """Write the four independent chapters together. Summary and risks wait for these texts."""
    sections = [section for section in planned if section.id not in {"summary", "risks"}]
    if not sections:
        return {}

    def writer(section: SectionPlan) -> Callable[[], tuple[str, str]]:
        owned = charts_for.get(section.id, []) if section.chart else []
        notes = [
            item.as_dict() for item in chart_decisions or []
            if item.section_id == section.id
        ]

        def run() -> tuple[str, str]:
            content, _points = _write_section(
                llm, message, history, section, _asks_for(section, by_section), found, owned,
                _insights_for(section, insights_by_section),
                remembered=remembered, folded_through=folded_through, trace=trace,
                chart_notes=notes,
            )
            content = finish_section(content, owned if section.chart else [])
            return section.id, content or "这一节没有写成。"

        return run

    return dict(_overlap(*[writer(section) for section in sections]))


def _write_section(
    llm: Any, message: str, history: list[dict[str, Any]], section: SectionPlan,
    asked: list[_Ask], found: list[_Search], charts: list[dict[str, Any]], insights: list[dict[str, Any]],
    *, remembered: str = "", folded_through: int = 0, prior: str = "", trace: _RunTrace | None = None,
    chart_notes: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    messages = _history(history, folded_through)
    messages.append({
        "role": "user",
        "content": _section_payload(section, asked, found, charts, insights, prior, chart_notes),
    })
    try:
        raw = _call_json(
            llm,
            model=model_calls.SCHOOL_REASONER,
            system=attach(WRITING_PROMPT.format(
                message=(message or "").strip()[:4000] or _FALLBACK,
                section_title=section.title,
                section_brief=section.brief,
            ), remembered),
            messages=messages,
            schema=section_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception:
        return "", []
    return _read_section(raw)


def _write_summary(
    llm: Any, message: str, history: list[dict[str, Any]], body: str,
    *, remembered: str = "", folded_through: int = 0, trace: _RunTrace | None = None,
) -> tuple[str, list[str]]:
    messages = _history(history, folded_through)
    messages.append({"role": "user", "content": f"=== 已写章节 ===\n{body}"})
    try:
        raw = _call_json(
            llm,
            model=model_calls.SCHOOL_REASONER,
            system=attach(SUMMARY_PROMPT.format(
                message=(message or "").strip()[:4000] or _FALLBACK,
            ), remembered),
            messages=messages,
            schema=section_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception:
        return "", []
    return _read_section(raw)


def _body_for_summary(planned: list[SectionPlan], texts: dict[str, str]) -> str:
    parts: list[str] = []
    for index, section in enumerate(planned, start=1):
        if section.id == "summary":
            continue
        body = texts.get(section.id, "").strip()
        if not body:
            continue
        parts.append(f"## {index}. {section.title}\n\n{strip_chart_refs(body)}")
    return "\n\n".join(parts)


def _assemble(planned: list[SectionPlan], texts: dict[str, str]) -> str:
    parts: list[str] = []
    for index, section in enumerate(planned, start=1):
        body = texts.get(section.id, "").strip()
        if body:
            parts.append(f"## {index}. {section.title}\n\n{body}")
    return "\n\n".join(parts)


_CHART_REF = re.compile(r"\[([^\]\n]+)\]\((chart_[\w-]+)\)")
_SENTENCE = re.compile(r"[^。！？]+[。！？]?")
_TABLE_LINE = re.compile(r"^\s*\|")
_HEADING_LINE = re.compile(r"^#{1,6}\s+\S")
_BULLET_LINE = re.compile(r"^(?:[-*]|\d+[.)])\s+\S")
_LINK_LINE = re.compile(r"^\[[^\]\n]+\]\([^)\s]+\)\s*$")


def strip_chart_refs(text: str) -> str:
    """Drop chart placeholders, keeping the title words. Web links stay."""
    return _CHART_REF.sub(r"\1", text or "")


def shape_prose(text: str) -> str:
    """Break a wall of sentences into short paragraphs. Tables, lists, headings, and lone links stay put."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    prose: list[str] = []

    def flush() -> None:
        if not prose:
            return
        flat = "".join(part.strip() for part in prose)
        prose.clear()
        sentences = [item.strip() for item in _SENTENCE.findall(flat) if item.strip()]
        if not sentences:
            if flat:
                blocks.append(flat)
            return
        if len(sentences) <= 2:
            blocks.append("".join(sentences))
            return
        for index in range(0, len(sentences), 2):
            blocks.append("".join(sentences[index:index + 2]))

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush()
            index += 1
            continue
        if _TABLE_LINE.match(line):
            flush()
            table = [line.rstrip()]
            index += 1
            while index < len(lines) and lines[index].strip() and _TABLE_LINE.match(lines[index]):
                table.append(lines[index].rstrip())
                index += 1
            blocks.append("\n".join(table))
            continue
        if _HEADING_LINE.match(stripped) or _LINK_LINE.match(stripped):
            flush()
            blocks.append(stripped)
            index += 1
            continue
        if _BULLET_LINE.match(stripped):
            flush()
            items = [stripped]
            index += 1
            while index < len(lines):
                nxt = lines[index].strip()
                if not nxt:
                    look = index + 1
                    while look < len(lines) and not lines[look].strip():
                        look += 1
                    if look < len(lines) and _BULLET_LINE.match(lines[look].strip()):
                        index = look
                        continue
                    break
                if not _BULLET_LINE.match(nxt):
                    break
                items.append(nxt)
                index += 1
            blocks.append("\n".join(items))
            continue
        prose.append(stripped)
        index += 1
    flush()
    return "\n\n".join(blocks)


def ensure_chart_cites(text: str, charts: list[dict[str, Any]]) -> str:
    """Put a chart the section never mentioned at the end of that section."""
    missing = [
        f"[{str(chart.get('title') or '').strip()}]({chart.get('id')})"
        for chart in charts
        if chart.get("id") and str(chart.get("title") or "").strip() and str(chart.get("id")) not in (text or "")
    ]
    body = (text or "").rstrip()
    if not missing:
        return body
    extra = "\n\n".join(missing)
    return f"{body}\n\n{extra}" if body else extra


def finish_section(text: str, charts: list[dict[str, Any]]) -> str:
    """Paragraph breaks, then a citation for any chart this section still omitted."""
    return ensure_chart_cites(shape_prose(text), charts)


def _assign_chart_ids(charts: list[dict[str, Any]], used: set[str]) -> list[dict[str, Any]]:
    assigned: list[dict[str, Any]] = []
    for chart in charts:
        cid = str(chart.get("id") or "")
        if not cid or cid in used:
            number = 1
            while f"chart_{number:03d}" in used:
                number += 1
            cid = f"chart_{number:03d}"
        used.add(cid)
        assigned.append({**chart, "id": cid})
    return assigned


def _fit_category_axes(option: dict[str, Any]) -> None:
    """Long product names need room. Short category labels are left as the model drew them."""

    def axes(key: str) -> list[dict[str, Any]]:
        raw = option.get(key)
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

    long_x = False
    for axis in axes("xAxis"):
        data = axis.get("data")
        if axis.get("type") == "value" or not isinstance(data, list):
            continue
        if any(isinstance(item, str) and len(item) > 8 for item in data):
            long_x = True
            label = axis.get("axisLabel") if isinstance(axis.get("axisLabel"), dict) else {}
            label.setdefault("interval", 0)
            label.setdefault("rotate", 28)
            label.setdefault("hideOverlap", True)
            axis["axisLabel"] = label
    for axis in axes("yAxis"):
        data = axis.get("data")
        if not isinstance(data, list):
            continue
        if any(isinstance(item, str) and len(item) > 8 for item in data):
            label = axis.get("axisLabel") if isinstance(axis.get("axisLabel"), dict) else {}
            label.setdefault("interval", 0)
            label.setdefault("width", 140)
            label.setdefault("overflow", "break")
            axis["axisLabel"] = label
    if not long_x:
        return
    grid = option.get("grid") if isinstance(option.get("grid"), dict) else {}
    grid["containLabel"] = True
    if grid.get("bottom") in (None, "3%", "5%", 0):
        grid["bottom"] = "22%"
    option["grid"] = grid


def fit_chart_axes(charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Long category labels need room. The names stay whatever the query and the chart model wrote."""
    fitted: list[dict[str, Any]] = []
    for chart in charts:
        option = chart.get("echarts_option")
        copied = json.loads(json.dumps(option)) if isinstance(option, dict) else option
        if isinstance(copied, dict):
            _fit_category_axes(copied)
        fitted.append({**chart, "echarts_option": copied})
    return fitted


def _section_heading_id(heading: str, planned: list[SectionPlan]) -> str | None:
    title = re.sub(r"^##\s+", "", heading).strip()
    title = re.sub(r"^\d+\.\s*", "", title)
    for section in planned:
        if title == section.title or title.endswith(section.title):
            return section.id
    return None


def place_charts_in_sections(
    report: str, planned: list[SectionPlan], charts_for: dict[str, list[dict[str, Any]]],
) -> str:
    """A chart stays in the section that owns it. A citation in another section becomes plain title text."""
    if not any(charts_for.values()):
        return report
    owned = {
        str(chart.get("id")): sid
        for sid, charts in charts_for.items()
        for chart in charts
        if chart.get("id")
    }

    def keep_owned(text: str, sid: str) -> str:
        def repl(match: re.Match[str]) -> str:
            if owned.get(match.group(2)) == sid:
                return match.group(0)
            return match.group(1)
        return _CHART_REF.sub(repl, text)

    rebuilt: list[str] = []
    for part in re.split(r"(?=^##\s+)", report or "", flags=re.M):
        if not part.strip():
            continue
        lines = part.split("\n", 1)
        heading = lines[0]
        body = lines[1] if len(lines) > 1 else ""
        if heading.startswith("##"):
            sid = _section_heading_id(heading, planned) or ""
            body = keep_owned(body, sid)
            body = ensure_chart_cites(body, charts_for.get(sid, []))
            rebuilt.append(heading + ("\n\n" + body.strip() if body.strip() else ""))
        else:
            rebuilt.append(keep_owned(part, "").strip())
    return "\n\n".join(part for part in rebuilt if part.strip())


def finish_report(
    report: str, planned: list[SectionPlan], charts_for: dict[str, list[dict[str, Any]]],
) -> str:
    """The revise step can undo breaks and chart placement. Put them back on the final text."""
    placed = place_charts_in_sections(report, planned, charts_for)
    return shape_prose(placed) or report


def _report_evidence(
    asked: list[_Ask], insights_by_section: dict[str, list[dict[str, Any]]],
    found: list[_Search], charts: list[dict[str, Any]],
    chart_decisions: list[ChartDecision] | None = None,
) -> str:
    """The facts the reviewer and the revising writer are allowed to use."""
    payload = {
        "query_results": _query_results(asked),
        "insights": [
            {"section": sid, **item}
            for sid, items in insights_by_section.items()
            for item in items
        ],
        "news": [
            {"query": item.query, "results": item.results if item.ok else []}
            for item in found
        ],
        "charts": [
            {"id": chart.get("id") or "", "title": chart.get("title") or ""}
            for chart in charts
        ],
        "chart_status": [item.as_dict() for item in chart_decisions or []],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def review_schema() -> dict[str, Any]:
    return {
        "name": "report_review", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "chapters": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string", "enum": list(_REQUIRED_CHAPTERS)},
                        "score": {"type": "integer"},
                    },
                    "required": ["section", "score"],
                }},
                "risks_penalty": {"type": "integer"},
                "ok": {"type": "boolean"},
                "issues": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string"},
                        "issue_type": {
                            "type": "string",
                            "enum": ["hallucination", "logic", "exaggeration", "format", "incomplete"],
                        },
                        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                        "description": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["section", "issue_type", "severity", "description", "suggestion"],
                }},
            },
            "required": ["chapters", "risks_penalty", "ok", "issues"],
        },
    }


def revise_schema() -> dict[str, Any]:
    return {
        "name": "report_revise", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "sections": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["section", "content"],
                }},
            },
            "required": ["sections"],
        },
    }


def split_report_sections(report: str) -> list[tuple[str, str, str]]:
    """Each part is ``(heading_line, title, body)``. Text before the first heading is title ''."""
    parts: list[tuple[str, str, str]] = []
    for chunk in re.split(r"(?=^##\s+)", report or "", flags=re.M):
        if not chunk.strip():
            continue
        lines = chunk.split("\n", 1)
        heading = lines[0]
        body = lines[1] if len(lines) > 1 else ""
        if heading.startswith("##"):
            title = re.sub(r"^##\s+", "", heading).strip()
            title = re.sub(r"^\d+\.\s*", "", title)
            parts.append((heading, title, body.strip("\n")))
        else:
            parts.append(("", "", chunk.strip("\n")))
    return parts


def replace_report_sections(report: str, updates: dict[str, str]) -> str:
    """Replace bodies keyed by section title. A report with no headings takes the first update."""
    wanted = {title.strip(): body for title, body in updates.items() if title.strip()}
    if not wanted:
        return report
    parts = split_report_sections(report)
    headed = [item for item in parts if item[0].startswith("##")]
    if not headed:
        return next(iter(wanted.values())).strip()
    rebuilt: list[str] = []
    seen: set[str] = set()
    for heading, title, body in parts:
        if heading.startswith("##") and title in wanted:
            body = wanted[title].strip()
            seen.add(title)
        if heading.startswith("##"):
            rebuilt.append(heading + ("\n\n" + body.strip() if body.strip() else ""))
        elif body.strip():
            rebuilt.append(body.strip())
    for title, body in wanted.items():
        if title in seen:
            continue
        if not body.strip():
            continue
        rebuilt.append(f"## {title}\n\n{body.strip()}")
    # Drop a chapter the reviser emptied (风险).
    kept: list[str] = []
    for block in rebuilt:
        heading = block.split("\n", 1)[0]
        title = re.sub(r"^##\s+", "", heading).strip()
        title = re.sub(r"^\d+\.\s*", "", title)
        if heading.startswith("##") and title in wanted and not wanted[title].strip():
            continue
        kept.append(block)
    return "\n\n".join(kept)


def _review_issues(raw: Any) -> list[dict[str, str]]:
    """Keep critical and major issues. Minor wording notes do not send the draft back."""
    if not isinstance(raw, dict):
        return []
    issues: list[dict[str, str]] = []
    for item in raw.get("issues") or []:
        if not isinstance(item, dict):
            continue
        section = str(item.get("section") or item.get("target_section") or "").strip()
        description = str(item.get("description") or item.get("problem") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        severity = str(item.get("severity") or "major").strip().lower()
        issue_type = str(item.get("issue_type") or "logic").strip().lower()
        if severity == "minor" or not section or not description:
            continue
        issues.append({
            "section": section,
            "issue_type": issue_type,
            "severity": severity,
            "description": description,
            "suggestion": suggestion,
        })
    return issues


def _chapter_points(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(_CHAPTER_POINTS, score))


def _review_score(raw: Any) -> tuple[int | None, list[dict[str, int | str]]]:
    """Five required chapters, 20 points each. A bare score is only a fallback."""
    if not isinstance(raw, dict):
        return None, []
    found: dict[str, int] = {}
    for item in raw.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("section") or "").strip()
        if name in _REQUIRED_CHAPTERS:
            found[name] = _chapter_points(item.get("score"))
    if found:
        chapters = [{"section": name, "score": found.get(name, 0)} for name in _REQUIRED_CHAPTERS]
        try:
            penalty = int(raw.get("risks_penalty") or 0)
        except (TypeError, ValueError):
            penalty = 0
        penalty = max(0, min(_CHAPTER_POINTS, penalty))
        total = sum(int(item["score"]) for item in chapters) - penalty
        return max(0, min(100, total)), chapters
    if "score" not in raw:
        return None, []
    try:
        score = int(raw["score"])
    except (TypeError, ValueError):
        return None, []
    return max(0, min(100, score)), []


def _review_report(
    llm: Any, *, message: str, report: str, evidence: str, previous_issues: str = "（无）",
    remembered: str = "", trace: _RunTrace | None = None,
) -> tuple[int | None, list[dict[str, str]], list[dict[str, int | str]], str | None]:
    """Score the current draft from five chapter marks. Later rounds check the last issues."""
    try:
        raw = _call_json(
            llm,
            model=model_calls.DEEPSEEK_FLASH,
            system=attach(REPORT_REVIEW_PROMPT.format(
                message=(message or "").strip()[:4000] or _FALLBACK,
                outline=outline_text(),
                report=report[:12000],
                evidence=evidence[:12000],
                previous_issues=previous_issues or "（无）",
            ), remembered),
            messages=[{"role": "user", "content": "找出问题并给这整份报告打分。"}],
            schema=review_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as err:
        return None, [], [], str(err).split("\n", 1)[0][:200]
    score, chapters = _review_score(raw)
    if score is None:
        return None, [], [], "审核没有给出分数"
    return score, _review_issues(raw), chapters, None


def _review_rounds(
    llm: Any, *, message: str, report: str, evidence: str, remembered: str = "",
    planned: list[SectionPlan] | None = None, charts_for: dict[str, list[dict[str, Any]]] | None = None,
    trace: _RunTrace | None = None,
) -> tuple[str, int | None, str | None]:
    """Hard-check, score, and rewrite every named chapter in one call. Two revisions by default."""
    report = apply_hard_fixes(report)
    if planned is not None:
        report = place_charts_in_sections(report, planned, charts_for or {})
    gates = hard_issues(report, message=message, evidence=evidence)
    if gates:
        report = _revise_report(
            llm, message=message, report=report, score=0, chapters=[], issues=gates,
            evidence=evidence, remembered=remembered, trace=trace,
        ) or report
        report = apply_hard_fixes(report)
    score: int | None = None
    error: str | None = None
    previous = "（无）"
    for round_index in range(1, MAX_REVIEW_ROUNDS + 1):
        score, issues, chapters, error = _review_report(
            llm, message=message, report=report, evidence=evidence,
            previous_issues=previous, remembered=remembered,
            trace=trace,
        )
        if score is not None and issues and score >= PASS_SCORE:
            score = PASS_SCORE - 1
        if error or score is None or score >= PASS_SCORE or not issues:
            return report, score, error
        revised = _revise_report(
            llm, message=message, report=report, score=score, chapters=chapters, issues=issues,
            evidence=evidence, remembered=remembered,
            trace=trace,
        )
        if not revised.strip():
            return report, score, error
        report = apply_hard_fixes(revised.strip())
        previous = json.dumps(issues, ensure_ascii=False)
        if round_index == MAX_REVIEW_ROUNDS:
            break
    score, issues, _, error = _review_report(
        llm, message=message, report=report, evidence=evidence,
        previous_issues=previous, remembered=remembered,
        trace=trace,
    )
    if score is not None and issues and score >= PASS_SCORE:
        score = PASS_SCORE - 1
    return report, score, error


def _revise_instruction(issues: list[dict[str, str]]) -> str:
    titles: list[str] = []
    seen: set[str] = set()
    for item in issues:
        title = str(item.get("section") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
    if not titles:
        return "把审核意见点到的章节一次全部改完。"
    return f"一次改完这些章节，不要只改一节：{'、'.join(titles)}。"


def _revise_report(
    llm: Any, *, message: str, report: str, score: int, chapters: list[dict[str, int | str]],
    issues: list[dict[str, str]], evidence: str, remembered: str = "",
    trace: _RunTrace | None = None,
) -> str:
    """Rewrite every named chapter in one call. Empty means keep the original."""
    try:
        raw = _call_json(
            llm,
            model=model_calls.DEEPSEEK_FLASH,
            system=attach(REPORT_REVISE_PROMPT.format(
                message=(message or "").strip()[:4000] or _FALLBACK,
                report=report[:12000],
                score=score,
                chapters=json.dumps(chapters, ensure_ascii=False),
                issues=json.dumps(issues, ensure_ascii=False),
                evidence=evidence[:12000],
            ), remembered),
            messages=[{"role": "user", "content": _revise_instruction(issues)}],
            schema=revise_schema(), temperature=0, trace=trace,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception:
        return ""
    return _apply_revise(report, raw)


def _apply_revise(report: str, raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    updates: dict[str, str] = {}
    for item in raw.get("sections") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("section") or "").strip()
        if not title:
            continue
        updates[title] = str(item.get("content") or "")
    if updates:
        return replace_report_sections(report, updates)
    whole = str(raw.get("report") or "").strip()
    return whole


def _notes(found: list[_Search]) -> list[str]:
    notes: list[str] = []
    for item in found:
        if not item.ok:
            continue
        for row in item.results:
            title = row.get("title") or ""
            if title and title not in notes:
                notes.append(title)
            if len(notes) >= 4:
                return notes
    return notes


def _warnings(asked: list[_Ask], found: list[_Search]) -> list[str]:
    warnings: list[str] = []
    for item in asked:
        if item.result.ok:
            continue
        detail = item.result.error or "query failed"
        warnings.append(f'No figures for "{item.question[:80]}": {detail[:160]}')
    for item in found:
        if not item.ok:
            warnings.append(f'Web search failed ("{item.query[:80]}"): {item.error or "search failed"}')
    return warnings


def _mark(asked: list[_Ask]) -> dict[str, Any] | None:
    chosen = next((item for item in asked if item.result.ok and item.result.log_id), None)
    if chosen is None:
        chosen = next((item for item in asked if item.result.log_id), None)
    if chosen is None or not chosen.result.log_id:
        return None
    result = chosen.result
    error = result.error or (
        "query result is missing required columns: " + ", ".join(result.missing_columns)
        if result.missing_columns else None
    )
    return {
        "ok": result.ok and not result.missing_columns,
        "row_count": result.row_count, "columns": result.columns,
        "missing_columns": result.missing_columns, "truncated": result.truncated,
        "rows_handle": result.log_id, "error": error, "log_id": result.log_id,
    }


def series_payload(asked: list[_Ask]) -> list[dict[str, Any]]:
    """Rows from this draft, kept on the message."""
    return [
        {"id": item.id, "question": item.question, "rows": item.result.rows}
        for item in asked
        if item.result.ok and item.result.rows
    ]

