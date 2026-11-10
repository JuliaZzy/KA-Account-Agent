"""A desk turn enqueues a run. The worker writes the loop trace."""

from __future__ import annotations

from backend.llm import FakeLLM
from backend.llm.routing import run_budget_for
from backend.service import handle_message, new_session
from backend.services.chat_run import enqueue_chat_run, work_chat


def _chat(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = new_session(db, "ps-1", "u-a")["id"]
    db.update_session(sid, title="Hello", tags=[])
    return sid


def test_a_turn_enqueues_and_the_worker_records_the_loop(db):
    llm = FakeLLM(text_reply="Got it.")
    sid = _chat(db)
    events = list(handle_message(db, llm, sid, "hello"))
    assert events[0]["type"] == "run"
    run_id = events[0]["run_id"]
    assert all(event.get("type") != "token" for event in events)
    assert all(event.get("type") != "done" for event in events)

    queued = db.find_agent_run("dist-1", run_id)
    assert queued["status"] == "queued"
    assert queued["run_type"] == "chat"
    assert queued["user_id"] == "u-a"
    chat_budget = run_budget_for("chat")
    assert queued["max_model_calls"] == chat_budget.max_model_calls
    assert queued["max_tokens"] == chat_budget.max_tokens

    assert work_chat(db, llm, run_id) is True
    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "succeeded"
    assert run["output_json"]["text"] == "Got it."
    assert db.find_pending_outbox_jobs() == []

    trace = db.run_trace("dist-1", run_id)
    assert {step["name"] for step in trace["steps"]} == {"model"}
    assert {step["step_type"] for step in trace["steps"]} == {"model"}
    assert len(trace["steps"]) >= 2
    saved = db.session(sid)["history"][-1]
    assert saved["content"] == "Got it."
    assert "activity" not in saved and "model_json" not in saved


def test_closing_the_stream_leaves_the_run_queued(db):
    llm = FakeLLM(text_reply="Got it.")
    sid = _chat(db)
    turn = handle_message(db, llm, sid, "hello")
    opened = next(turn)
    turn.close()
    run = db.find_agent_run("dist-1", opened["run_id"])
    assert run["status"] == "queued"
    assert work_chat(db, llm, opened["run_id"]) is True
    run = db.find_agent_run("dist-1", opened["run_id"])
    assert run["status"] == "succeeded"


def test_enqueue_writes_open_run_budget(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    session = new_session(db, "ps-1", "u-a")
    run = enqueue_chat_run(db, session, "hello")
    opened = [row for row in db.find_runs_by_owner(run["id"]) if row["run_type"] == "open"]
    budget = run_budget_for("open")
    assert opened
    assert opened[0]["max_model_calls"] == budget.max_model_calls
    assert opened[0]["max_tokens"] == budget.max_tokens


def test_a_model_error_fails_the_run_on_the_worker(db):
    llm = FakeLLM(fail_json=True)
    sid = _chat(db)
    events = list(handle_message(db, llm, sid, "hello"))
    assert events[0]["type"] == "run"
    assert work_chat(db, llm, events[0]["run_id"]) is True
    run = db.find_agent_run("dist-1", events[0]["run_id"])
    assert run["status"] == "queued"
    assert run["error_json"]["kind"] == "transient"

def test_po_extract_is_recorded_on_the_chat_run(db):
    llm = FakeLLM(
        json_replies=[
            {"tasks": [{"id": "p", "kind": "agent", "name": "po", "input": {}, "depends_on": []}]},
            {
                "line_items": [{"reference": "fish oil", "cases": 10, "sku": "FO-100"}],
                "discount_kind": "none", "user_confirmed": False, "reply_language": "English",
            },
        ],
        text_reply="All set — press Confirm.",
    )
    sid = _chat(db)
    events = list(handle_message(db, llm, sid, "10 cases of fish oil, no discount"))
    run_id = events[0]["run_id"]
    assert work_chat(db, llm, run_id) is True

    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "succeeded"
    po = next(child for child in db.find_runs_by_owner(run_id) if child["run_type"] == "po")
    trace = db.run_trace("dist-1", po["id"])
    extract = next(
        row for row in trace["model_calls"]
        if row["model"] == "deepseek-flash"
        and (row.get("request_json") or {}).get("schema", {}).get("name") == "po_turn_extraction"
    )
    assert extract["status"] == "succeeded"
    assert extract["step_id"] == next(
        step["id"] for step in trace["steps"] if step["name"] == "po.draft"
    )
    assert extract["request_json"]["schema"]["name"] == "po_turn_extraction"
    assert extract["response_json"]["json"]["line_items"][0]["sku"] == "FO-100"

def test_po_claim_is_recorded_on_the_chat_run(db):
    from backend.po.check import CheckResult, ResolvedLine
    from backend.po.slots import Discount, LineItemSlot, PoSlots
    from backend.po.submit import create_and_submit_po

    line = ResolvedLine(
        reference="fish oil", sku="FO-100", cases=10, product_id="p-fo",
        product_name="Deep Sea Fish Oil", case_pack=12, case_price=100.0, line_total=1000.0,
    )
    check = CheckResult(
        lines=[line], subtotal=1000.0, gaps=[], blocking=[], advisory=[], rule_results=[], ready=True,
    )
    submitted = create_and_submit_po(
        db, partnership=db.partnership("ps-1"), settings=db.settings("brand-1"),
        slots=PoSlots(line_items=[LineItemSlot(reference="fish oil", cases=10, sku="FO-100")], discount=Discount(kind="none")),
        check=check,
    )
    db.update_po(submitted.po_id, status="shipped", prepaid_amount=500.0)

    llm = FakeLLM(
        json_replies=[
            {"tasks": [{"id": "p", "kind": "agent", "name": "po", "input": {}, "depends_on": []}]},
            {"verdict": "received_ok", "confidence": 0.95},
        ],
        text_reply="Marked received.",
    )
    sid = _chat(db)
    db.update_session(sid, stage="submitted", po_id=submitted.po_id)
    events = list(handle_message(db, llm, sid, "goods arrived, all good"))
    run_id = events[0]["run_id"]
    assert work_chat(db, llm, run_id) is True

    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "succeeded"
    po = next(child for child in db.find_runs_by_owner(run_id) if child["run_type"] == "po")
    trace = db.run_trace("dist-1", po["id"])
    claim = next(row for row in trace["model_calls"] if row["model"] == "qwen3.7-flash")
    assert claim["status"] == "succeeded"
    assert claim["step_id"] == next(step["id"] for step in trace["steps"] if step["name"] == "po.draft")
    assert claim["request_json"]["system"]
    assert claim["response_json"]["json"]["verdict"] == "received_ok"
    assert db.po(submitted.po_id).status == "awaiting_payment"

def test_report_plan_is_recorded_on_the_chat_run(db):
    llm = FakeLLM(
        json_replies=[{
            "tasks": [{"id": "r", "kind": "agent", "name": "report", "input": {}, "depends_on": []}],
        }, {
            "title": "月报",
            "sections": [
                {"id": "metrics", "questions": [], "searches": []},
                {"id": "summary", "questions": [], "searches": []},
                {"id": "performance", "questions": [], "searches": []},
                {"id": "inventory", "questions": [], "searches": []},
                {"id": "next", "questions": [], "searches": []},
                {"id": "risks", "questions": [], "searches": []},
            ],
        }],
        text_reply="Report draft ready.",
    )
    sid = _chat(db)
    events = list(handle_message(db, llm, sid, "write a short sales note"))
    run_id = events[0]["run_id"]
    assert work_chat(db, llm, run_id) is True

    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "succeeded"
    report = next(child for child in db.find_runs_by_owner(run_id) if child["run_type"] == "report")
    trace = db.run_trace("dist-1", report["id"])
    planned = next(
        row for row in trace["model_calls"]
        if row["model"] == "deepseek-flash"
        and (row.get("request_json") or {}).get("schema", {}).get("name") == "report_plan"
    )
    assert planned["status"] == "succeeded"
    assert planned["step_id"] == next(step["id"] for step in trace["steps"] if step["name"] == "report.draft")
    assert planned["response_json"]["json"]["title"] == "月报"


def test_a_running_reply_is_visible_on_the_trace(db):
    from datetime import timedelta

    from backend.db import utcnow

    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "partial-reply", {"message": "hello"})
    assert db.note_running_reply(run["id"], "Hel") is None
    claimed = db.claim_chat_turn(run["id"], "worker-test", utcnow() + timedelta(minutes=5))
    assert claimed is not None
    assert db.note_running_reply(run["id"], "Hello")["output_json"]["text"] == "Hello"
    trace = db.run_trace("dist-1", run["id"])
    assert trace["run"]["status"] == "running"
    assert trace["run"]["output_json"]["text"] == "Hello"


def test_a_prose_reply_is_published_while_the_model_is_still_writing(db):
    llm = FakeLLM(text_reply="Hello there friend")
    sid = _chat(db)
    events = list(handle_message(db, llm, sid, "hello"))
    run_id = events[0]["run_id"]
    seen: list[str] = []
    original = db.note_running_reply

    def spy(target_id: str, text: str, *, generation_id: str | None = None, reasoning: str | None = None):
        row = original(target_id, text, generation_id=generation_id, reasoning=reasoning)
        if row is not None:
            assert row["status"] == "running"
            assert generation_id
            seen.append(text)
        return row

    db.note_running_reply = spy
    assert work_chat(db, llm, run_id) is True
    assert seen[-1] == "Hello there friend"
    assert seen[0] != seen[-1]
    assert all(len(seen[i]) < len(seen[i + 1]) and seen[i + 1].startswith(seen[i]) for i in range(len(seen) - 1))
    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "succeeded"
    assert run["output_json"]["text"] == "Hello there friend"