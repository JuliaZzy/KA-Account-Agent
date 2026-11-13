"""The parent/child plan contract: bad graphs fail closed, results are first-write, ANSWER keeps all of them."""

from __future__ import annotations

from sqlalchemy import text

from backend.llm import FakeLLM
from backend.orchestrator.gather import load_state
from backend.orchestrator.parent import (
    _materials,
    empty_named_period_line,
    ensure_empty_period_opening,
)
from backend.orchestrator.plan import (
    _accept,
    _normalize_data_contract,
    graph_error,
    parse_tasks,
    plan_schema,
    scheduled,
)
from backend.repositories.run_repository import latest_checkpoint
from backend.service import handle_message, new_session
from backend.services.chat_run import execute_chat, work_chat
from backend.services.run_worker import work_once


def test_unknown_agent_is_dropped_and_a_dangling_dependency_is_rejected():
    kept = parse_tasks({"tasks": [
        {"id": "nope", "kind": "agent", "name": "missing", "input": {}, "depends_on": []},
        {"id": "p", "kind": "agent", "name": "po", "input": {}, "depends_on": []},
    ]})
    assert [task.name for task in kept] == ["po"]
    assert graph_error(kept) is None

    dangling = parse_tasks({"tasks": [
        {"id": "nope", "kind": "agent", "name": "missing", "input": {}, "depends_on": []},
        {"id": "p", "kind": "agent", "name": "po", "input": {}, "depends_on": ["nope"]},
    ]})
    assert graph_error(dangling) is not None


def test_a_report_drops_its_data_task_and_the_dependency():
    tasks = scheduled(parse_tasks({"tasks": [
        {"id": "d", "kind": "agent", "name": "data", "input": {"question": "total"}, "depends_on": []},
        {"id": "r", "kind": "agent", "name": "report", "input": {}, "depends_on": ["d"]},
    ]}))
    assert [task.id for task in tasks] == ["r"]
    assert tasks[0].depends_on == []
    assert graph_error(tasks) is None


def test_data_delivery_mode_is_derived_from_the_plan_shape():
    only = _normalize_data_contract(parse_tasks({"tasks": [
        {"id": "d", "kind": "agent", "name": "data", "input": {"question": "total"}, "depends_on": []},
    ]}))
    assert only[0].input["deliver"] == "answer"

    mixed = _normalize_data_contract(parse_tasks({"tasks": [
        {"id": "d", "kind": "agent", "name": "data", "input": {"question": "total"}, "depends_on": []},
        {"id": "b", "kind": "agent", "name": "brand", "input": {}, "depends_on": []},
    ]}))
    assert mixed[0].input["deliver"] == "data"


def test_chart_flag_on_a_data_task_is_kept_as_a_bool_and_dropped_when_falsy():
    tasks = _normalize_data_contract(parse_tasks({"tasks": [
        {"id": "a", "kind": "agent", "name": "data", "input": {"question": "q", "chart": "pie"}, "depends_on": []},
        {"id": "b", "kind": "agent", "name": "data", "input": {"question": "q", "chart": False}, "depends_on": []},
    ]}))
    assert tasks[0].input["chart"] is True
    assert "chart" not in tasks[1].input


def test_a_chart_request_in_the_message_sets_chart_even_if_the_planner_forgot_it():
    def plan():
        return parse_tasks({"tasks": [
            {"id": "d", "kind": "agent", "name": "data", "input": {"question": "q"}, "depends_on": []},
        ]})

    assert _normalize_data_contract(plan(), "对比2026年9月的采购金额和销售额，用柱状图。")[0].input["chart"] is True
    assert "chart" not in _normalize_data_contract(plan(), "2026年9月采购总金额是多少？")[0].input
    with_report = _normalize_data_contract(parse_tasks({"tasks": [
        {"id": "d", "kind": "agent", "name": "data", "input": {"question": "q"}, "depends_on": []},
        {"id": "r", "kind": "agent", "name": "report", "input": {}, "depends_on": []},
    ]}), "出一份月报，带饼图")
    assert "chart" not in with_report[0].input


def test_answer_materials_replace_chart_options_with_what_was_drawn():
    text = _materials([{
        "id": "d", "status": "succeeded", "ok": True, "error": None,
        "payload": {"chart": {"requested": True, "produced": True},
                    "charts": [{"title": "渠道占比", "echarts_option": {"series": [1] * 500}}],
                    "chart_status": [{"role": "pie_category", "status": "skipped", "reason": "no_data"}]},
    }])
    assert "渠道占比" in text and "charts_drawn" in text
    assert "echarts_option" not in text
    assert "pie_category" in text and "skipped" in text


def test_empty_named_month_opens_the_chat_with_no_data():
    results = [{
        "id": "r", "status": "succeeded", "ok": True, "error": None,
        "payload": {"named_period": "2026-10", "named_period_empty": True, "text": "近半年趋势"},
    }]
    line = empty_named_period_line("出一份2026年10月的采购月报，带品类饼图。", results)
    assert line == "2026年10月没有数据。"
    assert ensure_empty_period_opening("月报已生成，请在右侧查看。", line).startswith(line)
    assert ensure_empty_period_opening("2026年10月没有数据。右侧是近半年。", line).startswith(line)
    assert empty_named_period_line("出一份2026年9月的采购月报", [{
        "id": "r", "payload": {"named_period": "2026-09", "named_period_empty": False},
    }]) is None


def test_kind_is_derived_from_the_name_and_never_asked_of_the_model():
    tasks = parse_tasks({"tasks": [
        {"id": "d", "name": "data_agent", "input": {"question": "total"}, "depends_on": []},
        {"id": "t", "name": "translate", "input": {}, "depends_on": ["d"]},
    ]})
    assert [(task.kind, task.name) for task in tasks] == [("agent", "data"), ("tool", "translate")]
    # A stale `kind` (older checkpoint, replayed plan) cannot turn an agent into a tool.
    stale = parse_tasks({"tasks": [
        {"id": "d", "kind": "tool", "name": "data", "input": {}, "depends_on": []},
        {"id": "p", "kind": "tool", "name": "po_agent", "input": {}, "depends_on": []},
    ]})
    assert [(task.kind, task.name) for task in stale] == [("agent", "data"), ("agent", "po")]
    assert "kind" not in plan_schema()["schema"]["properties"]["tasks"]["items"]["properties"]


def test_an_unknown_agent_name_is_dropped_not_dispatched_as_a_tool():
    tasks, dropped, unknown = _accept({"tasks": [
        {"id": "x", "name": "pricing_agent", "input": {}, "depends_on": []},
        {"id": "d", "name": "data_agent", "input": {}, "depends_on": ["x"]},
    ]})
    assert [task.name for task in tasks] == ["data"]
    assert dropped["unknown_agent"] == 1 and unknown == {"x"}


def test_a_cycle_is_rejected():
    tasks = parse_tasks({"tasks": [
        {"id": "a", "kind": "tool", "name": "translate", "input": {}, "depends_on": ["b"]},
        {"id": "b", "kind": "tool", "name": "translate", "input": {}, "depends_on": ["a"]},
    ]})
    assert graph_error(tasks) == "depends_on contains a cycle"


def test_answer_materials_keep_failed_and_skipped_results():
    text = _materials([
        {"id": "a", "status": "succeeded", "ok": True, "error": None, "payload": {"n": 1}},
        {"id": "b", "status": "failed", "ok": False, "error": "no", "payload": {}},
        {"id": "c", "status": "skipped", "ok": False, "error": "not needed", "payload": {}},
    ])
    assert '"status": "succeeded"' in text
    assert '"status": "failed"' in text
    assert '"status": "skipped"' in text


def test_an_unknown_agent_is_counted_when_the_plan_still_succeeds(db):
    from datetime import timedelta

    from backend.db import utcnow
    from backend.orchestrator.plan import make_plan

    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "plan-drop", {"message": "hi"})
    assert db.claim_chat_turn(run["id"], "worker", utcnow() + timedelta(minutes=5)) is not None
    llm = FakeLLM(json_replies=[{"tasks": [
        {"id": "nope", "kind": "agent", "name": "missing", "input": {}, "depends_on": []},
        {"id": "p", "kind": "agent", "name": "po", "input": {}, "depends_on": []},
    ]}])
    tasks = make_plan(
        llm, message="hi", attachments=[], history=[], open_po=False, db=db, run_id=run["id"],
    )
    assert [task.name for task in tasks] == ["po"]
    step = next(
        item for item in db.run_trace("dist-1", run["id"])["steps"]
        if (item.get("input_json") or {}).get("purpose") == "orchestrator.plan"
    )
    assert step["status"] == "succeeded"
    assert step["output_json"]["dropped"]["unknown_agent"] == 1
    quality = db.monitor_facts("dist-1")["plan"]
    assert quality["calls"] == 1
    assert quality["failed"] == 0
    assert quality["unknown_agent_calls"] == 1
    assert quality["unknown_agent_dropped"] == 1


def test_a_cycle_fails_the_run_without_children(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = new_session(db, "ps-1", "u-a")["id"]
    db.update_session(sid, title="Hello", tags=[])
    llm = FakeLLM(json_replies=[{"tasks": [
        {"id": "a", "kind": "tool", "name": "translate", "input": {}, "depends_on": ["b"]},
        {"id": "b", "kind": "tool", "name": "translate", "input": {}, "depends_on": ["a"]},
    ]}], text_reply="should not answer")
    run_id = next(iter(handle_message(db, llm, sid, "translate this")))["run_id"]
    assert work_chat(db, llm, run_id) is True
    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "failed"
    assert "cycle" in run["error_json"]["message"]
    plan_step = next(
        step for step in db.run_trace("dist-1", run_id)["steps"]
        if (step.get("input_json") or {}).get("purpose") == "orchestrator.plan"
    )
    assert plan_step["error_json"]["code"] == "plan_cycle"
    assert db.find_runs_by_owner(run_id) == []
    assert latest_checkpoint(db, run_id) is None


def test_a_second_result_keeps_the_first_and_a_lost_checkpoint_does_not_replan(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = new_session(db, "ps-1", "u-a")["id"]
    db.update_session(sid, title="Hello", tags=[])
    llm = FakeLLM(json_replies=[{"tasks": [
        {"id": "x", "kind": "tool", "name": "not_a_tool", "input": {}, "depends_on": []},
    ]}], text_reply="answered from the child")
    run_id = next(iter(handle_message(db, llm, sid, "do the thing")))["run_id"]
    execute = execute_chat(db, llm)
    assert work_once(db, "test-worker", execute, run_id=run_id) is True
    parent = db.find_agent_run("dist-1", run_id)
    assert parent["status"] == "waiting_for_tasks"
    child = db.find_runs_by_owner(run_id)[0]
    assert work_once(db, "test-worker", execute, run_id=child["id"]) is True

    state = load_state(latest_checkpoint(db, run_id))
    assert state is not None
    original = state["results"]["x"]
    assert original["status"] == "failed"
    wrote = db.record_child_wave_result(
        "dist-1", run_id, "x",
        {**original, "error": "a later writer", "payload": {"replaced": True}},
        event_type="tool.failed",
        event_payload={"task_id": "x", "ok": False},
    )
    assert wrote is True
    kept = load_state(latest_checkpoint(db, run_id))["results"]["x"]
    assert kept["error"] == original["error"]
    assert kept["payload"] == original["payload"]
    events = db.list_run_events("dist-1", run_id)
    assert any(event["event_type"] == "wave.result_ignored" for event in events)

    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM checkpoints WHERE run_id = :run_id"), {"run_id": run_id})
    assert latest_checkpoint(db, run_id) is None
    parent = db.find_agent_run("dist-1", run_id)
    assert parent["status"] == "queued"
    plan_steps = [
        step for step in db.run_trace("dist-1", run_id)["steps"]
        if (step.get("input_json") or {}).get("purpose") == "orchestrator.plan"
    ]
    assert work_once(db, "test-worker", execute, run_id=run_id) is True
    done = db.find_agent_run("dist-1", run_id)
    assert done["status"] == "succeeded"
    assert done["output_json"]["text"] == "answered from the child"
    again = [
        step for step in db.run_trace("dist-1", run_id)["steps"]
        if (step.get("input_json") or {}).get("purpose") == "orchestrator.plan"
    ]
    assert len(again) == len(plan_steps)
    answered = load_state(latest_checkpoint(db, run_id))
    assert answered["phase"] == "answer_done"
    assert answered["results"]["x"]["id"] == "x"


def test_scope_flags_another_party_the_user_named():
    from types import SimpleNamespace as NS

    from backend.services.session_tools import _scope

    def ps(brand, dist):
        return NS(brand_name=brand, distributor_name=dist)

    mine = ps("Activia", "海纳分销")
    db = NS(partnerships=lambda: [mine, ps("Activia", "联采贸易"), ps("Red Bull", "联采贸易")])
    assert _scope(db, mine, "联采贸易2026年9月的采购总金额")["user_named_other"] == ["联采贸易"]
    assert _scope(db, mine, "Red Bull 的订单")["user_named_other"] == ["Red Bull"]
    # Naming your own distributor or brand is not a mismatch.
    assert "user_named_other" not in _scope(db, mine, "海纳分销 Activia 9月采购额")
