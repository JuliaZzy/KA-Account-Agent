"""Optional chart and insight failures leave a draft. The outline does not."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from backend.db import utcnow
from backend.llm import FakeLLM
from backend.llm.calls import ModelBudgetExceeded
from backend.orchestrator.gather import load_state
from backend.report.turn import (
    CHART_GAP,
    INSIGHT_GAP,
    SectionPlan,
    _Ask,
    _assemble,
    _charts_and_insights_async,
    finish_report,
)
from backend.services.chat_run import execute_chat
from backend.services.run_worker import work_once
from backend.services.task_run import execute_task
from backend.text2sql.text2sql import QueryResult


def _sections() -> list[SectionPlan]:
    return [
        SectionPlan("metrics", "关键指标", "目标", True, False, False, False, ["目标"], []),
        SectionPlan("performance", "业绩表现", "畅销", True, True, False, False, ["销售额"], []),
        SectionPlan("inventory", "库存情况", "库存", True, True, False, False, ["库存"], []),
        SectionPlan("next", "下一周期计划", "计划", False, False, True, True, [], ["档期"]),
    ]


def _rows():
    return [{"category": "Chocolate", "line_total": 10.0}]


def _ask(qid: str = "q1") -> _Ask:
    rows = _rows()
    return _Ask(qid, "各品类的采购金额是多少？", QueryResult(
        ok=True, sql="select 1", rows=rows, row_count=1, error=None, error_type=None,
        tables=["purchase_orders"], columns=["category", "line_total"],
    ))


def _chart_body() -> dict:
    return {"charts": [{
        "id": "c1", "title": "品类金额", "subtitle": "", "type": "pie",
        "echarts_option": {"series": [{"type": "pie", "data": [{"name": "Chocolate", "value": 10}]}]},
    }]}


def _wizard_body() -> dict:
    code = (
        "def analyze(datasets):\n"
        "    return [{'title': 'ok', 'detail': '一行', 'evidence': {'n': 1}}]"
    )
    return {"metrics": code, "performance": code, "inventory": code, "next": code}


def _draft(sections, texts, charts_for, degraded) -> str:
    return finish_report(_assemble(sections, texts), sections, charts_for, degraded)


def test_a_failed_insight_keeps_the_chart_and_marks_the_section(monkeypatch):
    from backend.report import turn

    seen: list[str] = []
    real = turn._call_json_async

    async def wrapped(llm, **kwargs):
        name = (kwargs.get("schema") or {}).get("name")
        seen.append(str(name))
        if name == "code_wizards":
            raise RuntimeError("This event loop is already running")
        return await real(llm, **kwargs)

    monkeypatch.setattr(turn, "_call_json_async", wrapped)
    sections = _sections()
    asked = [_ask()]
    by_section = {"metrics": [], "performance": [asked[0]], "inventory": [], "next": []}
    datasets = [{
        "id": "q1", "question": asked[0].question, "row_count": 1,
        "columns": ["category", "line_total"], "rows": _rows(),
    }]
    charts, charts_for, _failures, insights, _errors, _fails, _decisions, degraded = asyncio.run(
        _charts_and_insights_async(
            FakeLLM(json_replies=[_chart_body()]), "出一份月报，要品类饼图",
            sections, by_section, datasets,
        )
    )
    assert seen.count("report_charts") == 1
    assert charts and charts_for["performance"]
    assert any(row["step"] == "code_wizards" and row["code"] == "INTERNAL" for row in degraded)
    draft = _draft(
        sections,
        {"performance": "销售额 10。", "metrics": "目标 1。", "inventory": "库存 1。", "next": "下月备货。"},
        charts_for, degraded,
    )
    assert INSIGHT_GAP in draft
    assert "销售额 10" in draft
    assert insights["performance"] == []


def test_one_failed_chart_keeps_the_other_chart_and_the_insights(monkeypatch):
    from backend.report import turn

    chart_calls = 0
    real = turn._call_json_async

    async def wrapped(llm, **kwargs):
        nonlocal chart_calls
        name = (kwargs.get("schema") or {}).get("name")
        if name == "report_charts":
            chart_calls += 1
            if chart_calls == 1:
                raise RuntimeError("chart broke")
            return _chart_body()
        if name == "code_wizards":
            return _wizard_body()
        return await real(llm, **kwargs)

    monkeypatch.setattr(turn, "_call_json_async", wrapped)
    sections = _sections()
    asked = [_ask("q1"), _ask("q2")]
    by_section = {"metrics": [], "performance": asked, "inventory": [], "next": []}
    datasets = [{
        "id": "q1", "question": asked[0].question, "row_count": 1,
        "columns": ["category", "line_total"], "rows": _rows(),
    }]
    charts, charts_for, _failures, insights, _errors, _fails, _decisions, degraded = asyncio.run(
        _charts_and_insights_async(FakeLLM(), "出一份月报，要品类饼图", sections, by_section, datasets)
    )
    assert chart_calls == 2
    assert len(charts_for["performance"]) == 1
    assert [item["title"] for item in insights["performance"]] == ["ok"]
    assert any(row["section"] == "performance" and row["step"] == "report_charts" for row in degraded)
    draft = _draft(sections, {"performance": "销售额 10。"}, charts_for, degraded)
    assert CHART_GAP in draft
    assert "销售额 10" in draft


def test_a_spent_quota_is_not_degraded(monkeypatch):
    from backend.report import turn

    called: list[str] = []

    async def draw(*_args, **_kwargs):
        called.append("draw")
        await asyncio.sleep(0)
        return [], {}, [], []

    async def wizards(*_args, **_kwargs):
        called.append("wizards")
        raise ModelBudgetExceeded("org")

    monkeypatch.setattr(turn, "_draw_charts_async", draw)
    monkeypatch.setattr(turn, "_code_wizards_async", wizards)
    with pytest.raises(ModelBudgetExceeded):
        asyncio.run(_charts_and_insights_async(None, "", _sections(), {}, []))
    assert "draw" in called and "wizards" in called


def test_cancellation_is_not_degraded(monkeypatch):
    from backend.report import turn

    async def draw(*_args, **_kwargs):
        raise asyncio.CancelledError()

    async def wizards(*_args, **_kwargs):
        return {}, [], []

    monkeypatch.setattr(turn, "_draw_charts_async", draw)
    monkeypatch.setattr(turn, "_code_wizards_async", wizards)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_charts_and_insights_async(None, "", _sections(), {}, []))


def test_a_failed_outline_fails_the_report_and_the_parent_answers(db, monkeypatch):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-outline", "ps-1", "u-a")["id"]
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "chat", "outline-parent", {"session_id": sid, "message": "月报"},
    )
    assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "wave-1"}) is not None
    db.append_checkpoint(parent["id"], {
        "context": [], "completed_tool_calls": [], "last": "user",
        "orchestrator": {
            "tasks": [{"id": "1", "kind": "tool", "name": "report.draft", "input": {}, "depends_on": []}],
            "finished": [], "results": {}, "wave_id": "wave-1", "wave_ids": ["1"], "stale_wakes": 0,
        },
    })
    child, _created = db.create_agent_run(
        "dist-1", "u-a", "report", "outline-child", {
            "session_id": sid, "task_id": "1", "tool_name": "report.draft", "wave_id": "wave-1",
            "message": "月报",
        },
        owner_run_id=parent["id"],
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("outline broke")

    monkeypatch.setattr("backend.orchestrator.report._plan_async", boom)

    def run_child(run):
        return execute_task(db, None, run)

    immediate = (timedelta(0),)
    assert work_once(db, "w-child", run_child, immediate, run_id=child["id"]) is True
    assert db.find_agent_run_by_id(child["id"])["status"] == "queued"
    assert work_once(db, "w-child", run_child, immediate, run_id=child["id"]) is True
    failed = db.find_agent_run_by_id(child["id"])
    assert failed["status"] == "failed"
    assert failed["error_json"]["code"] == "INTERNAL"
    state = load_state(db.latest_checkpoint(parent["id"])) or {}
    assert state["results"]["1"]["ok"] is False
    if db.find_agent_run_by_id(parent["id"])["status"] == "waiting_for_tasks":
        from backend.services.run_worker import sweep_waiting_for_tasks
        sweep_waiting_for_tasks(db, stale_after=timedelta(0))
    assert work_once(
        db, "w", execute_chat(db, FakeLLM(text_reply="月报没有出来。")), run_id=parent["id"],
    ) is True
    assert db.find_agent_run_by_id(parent["id"])["status"] == "succeeded"


def test_a_spent_quota_fails_the_report_run(db, monkeypatch):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-quota", "ps-1", "u-a")["id"]
    child, _created = db.create_agent_run(
        "dist-1", "u-a", "report", "quota-child", {
            "session_id": sid, "task_id": "1", "tool_name": "report.draft", "message": "月报",
        },
    )

    async def boom(*_args, **_kwargs):
        raise ModelBudgetExceeded("org")

    monkeypatch.setattr("backend.orchestrator.report._plan_async", boom)
    assert work_once(db, "w", lambda run: execute_task(db, None, run), run_id=child["id"]) is True
    failed = db.find_agent_run_by_id(child["id"])
    assert failed["status"] == "failed"
    assert failed["error_json"]["code"] == "QUOTA_EXCEEDED"
    assert CHART_GAP not in str(failed.get("output_json") or "")
