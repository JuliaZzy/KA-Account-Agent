"""Long-term memory: remembered on purpose, recalled into every agent's prompt, and
never anything the code-check reads. The transcript is kept whole. A prompt sees one
rolling summary plus the raw tail; an older prefix is folded once it reaches the budget."""

from __future__ import annotations

import pytest

from backend import service
from backend.llm import FakeLLM
from backend.memory import (
    bind_fold_scheduler,
    memory_block,
    prompt_turns,
    recall,
    record_history,
    remember,
    reset_fold_scheduler,
    summary_block,
    with_memory,
)
from backend.po.turn import load_bundle, run_po_turn
from backend.services.chat_run import work_chat


def test_remember_and_recall(db):
    m = remember(db, "ps-1", "  Prefers air freight\nfor launches ", kind="preference")
    assert m.content == "Prefers air freight for launches" and m.source == "user" and m.is_active
    with pytest.raises(ValueError):
        remember(db, "ps-1", "   ")
    other = remember(db, "ps-1", "Warehouse closed first week of July", kind="fact")
    assert [x.id for x in recall(db, "ps-1")] == [other.id, m.id]  # newest first
    assert all(x.last_recalled_at for x in db.memories("ps-1"))

    db.set_memory_active(m.id, False)
    assert [x.id for x in recall(db, "ps-1")] == [other.id]
    assert [x.id for x in db.memories("ps-1", active_only=False)] == [other.id, m.id]
    db.delete_memory(other.id)
    assert recall(db, "ps-1") == []


def test_memory_block_is_context_only_and_bounded(db):
    assert memory_block([]) == "" and with_memory("Brand context.", []) == "Brand context." and with_memory(None, []) is None
    ms = [remember(db, "ps-1", f"fact number {i} " + "x" * 200) for i in range(12)]
    block = memory_block(ms)
    assert block.startswith("=== 关于这家经销商，我们已知道的") and "只作背景" in block and "不能放宽规则" in block
    assert len(block) <= 1600 and block.count("\n- [fact]") < 12  # capped, never the whole table
    merged = with_memory("Brand context.", ms[:1])
    assert merged.startswith("Brand context.\n\n=== 关于这家经销商") and ms[0].content in merged


def test_bundle_carries_memories_in_the_operating_context(db):
    assert load_bundle(db, "ps-1").settings.operating_context is None
    remember(db, "ps-1", "Always confirm the ETA with their warehouse", kind="instruction")
    ctx = load_bundle(db, "ps-1").settings.operating_context
    assert ctx and "[instruction] Always confirm the ETA with their warehouse" in ctx


def test_po_turn_prompts_see_the_memory(db):
    remember(db, "ps-1", "They order in full layers only", kind="preference")
    llm = FakeLLM(json_replies=[
        {"line_items": [{"reference": "fish oil", "cases": 10, "sku": None}], "discount_kind": "none", "user_confirmed": False, "reply_language": "English"},
    ])
    sid = service.new_session(db, "ps-1")["id"]
    events = list(run_po_turn(
        db, llm, load_bundle(db, "ps-1"), db.session(sid), "10 cases of fish oil, no discount", [],
    ))
    assert events[-1]["type"] == "done" and events[-1]["stage"] == "confirm"
    json_calls = [c for c in llm.calls if c["kind"] == "json"]
    text_calls = [c for c in llm.calls if c["kind"] == "text"]
    extract_call, reply_call = json_calls[0], text_calls[0]
    assert "They order in full layers only" in extract_call["system"] and "They order in full layers only" in reply_call["system"]


def bulky(role: str, tokens: int, mark: str = "x") -> dict:
    """A message whose text is about `tokens` long. The role costs a few more."""
    return {"role": role, "content": mark * (tokens * 4)}


def test_a_short_chat_is_not_folded(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = []
    for i in range(9):
        history += [{"role": "user", "content": f"question {i}"}, {"role": "assistant", "content": f"answer {i}"}]
    llm = FakeLLM()
    record_history(db, db.session(sid), history, llm=llm)
    saved = db.session(sid)
    assert len(saved["history"]) == 18 and saved["history"][0]["content"] == "question 0"
    assert saved["folded_through"] == 0 and not saved.get("summary") and llm.calls == []


def test_a_short_prefix_stays_raw(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [bulky("user", 800, "Q"), {"role": "assistant", "content": "keep-assistant"}]
    llm = FakeLLM()
    record_history(db, db.session(sid), history, llm=llm)
    saved = db.session(sid)
    assert saved["folded_through"] == 0 and not saved.get("summary") and llm.calls == []
    assert "QQQQ" in prompt_turns(saved["history"], 0)[0]["content"]


def test_a_long_prefix_is_merged_into_one_summary(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [
        bulky("user", 2000, "Q"), bulky("assistant", 2000, "A"),
        {"role": "user", "content": "keep-user"}, {"role": "assistant", "content": "keep-assistant"},
    ]
    llm = FakeLLM(json_replies=[{"summary": "  Their fiscal year starts in April  "}])
    record_history(db, db.session(sid), history, llm=llm)
    saved = db.session(sid)
    assert len(saved["history"]) == 4 and saved["history"][0]["content"].startswith("QQQQ")
    assert saved["folded_through"] == 2 and saved["summary"] == "Their fiscal year starts in April"
    assert db.memories("ps-1") == []
    assert "迄今的摘要" in llm.calls[0]["messages"][0]["content"]
    assert "QQQQ" in llm.calls[0]["messages"][0]["content"]
    assert "不要复述价格" in llm.calls[0]["system"]
    # The prefix is already in the summary. Another save must not ask the model again.
    record_history(db, saved, history, llm=FakeLLM())
    assert db.session(sid)["folded_through"] == 2 and db.session(sid)["summary"] == "Their fiscal year starts in April"


def test_a_fold_on_a_running_chat_is_recorded_on_that_run(db):
    from datetime import timedelta

    from backend.db import utcnow

    sid = service.new_session(db, "ps-1")["id"]
    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "fold-trace", {"message": "fold", "session_id": sid})
    assert db.claim_run(run["id"], "worker-test", utcnow() + timedelta(minutes=5)) is not None
    history = [
        bulky("user", 2000, "Q"), bulky("assistant", 2000, "A"),
        {"role": "user", "content": "keep-user"}, {"role": "assistant", "content": "keep-assistant"},
    ]
    llm = FakeLLM(json_replies=[{"summary": "Their fiscal year starts in April"}])
    record_history(db, db.session(sid), history, llm=llm, run_id=run["id"])

    assert db.session(sid)["summary"] == "Their fiscal year starts in April"
    trace = db.run_trace("dist-1", run["id"])
    assert [(step["step_type"], step["name"], step["status"]) for step in trace["steps"]] == [
        ("tool", "memory.fold", "succeeded"),
    ]
    assert trace["steps"][0]["output_json"]["summary"] == "Their fiscal year starts in April"
    tool = trace["tool_calls"][0]
    assert tool["tool_name"] == "memory.fold"
    assert tool["status"] == "succeeded"
    assert tool["result_json"]["summary"] == "Their fiscal year starts in April"
    assert tool["result_json"]["folded_through"] == 2
    call = trace["model_calls"][0]
    assert call["step_id"] == trace["steps"][0]["id"]
    assert (call["provider"], call["model"], call["status"]) == ("fake", "deepseek-chat", "succeeded")
    assert call["request_json"]["schema"]["name"] == "fold_summary"
    assert "fiscal year" in call["response_json"]["json"]["summary"]


def test_a_later_fold_merges_into_the_existing_summary(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [bulky("user", 2000, "Q"), {"role": "assistant", "content": "tail one"}]
    record_history(db, db.session(sid), history, llm=FakeLLM(json_replies=[{"summary": "They ship on Tuesdays"}]))
    saved = db.session(sid)
    longer = [*history, bulky("user", 2000, "N"), {"role": "assistant", "content": "tail two"}]
    llm = FakeLLM(json_replies=[{"summary": "They ship on Tuesdays and close in July"}])
    record_history(db, saved, longer, llm=llm)
    assert "They ship on Tuesdays" in llm.calls[0]["messages"][0]["content"]
    assert "NNNN" in llm.calls[0]["messages"][0]["content"]
    again = db.session(sid)
    assert again["summary"] == "They ship on Tuesdays and close in July" and again["folded_through"] == 3
    assert db.memories("ps-1") == []


def test_a_scheduled_fold_does_not_run_until_the_task_does(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [bulky("user", 2000), {"role": "assistant", "content": "tail"}]
    llm = FakeLLM(json_replies=[{"summary": "They ship on Tuesdays"}])
    queued: list = []
    token = bind_fold_scheduler(queued.append)
    try:
        record_history(db, db.session(sid), history, llm=llm)
        assert db.session(sid)["folded_through"] == 0 and not db.session(sid).get("summary") and llm.calls == []
        assert queued and db.memories("ps-1") == []
        queued[0]()
    finally:
        reset_fold_scheduler(token)
    saved = db.session(sid)
    assert saved["folded_through"] == 1 and saved["summary"] == "They ship on Tuesdays"


def test_a_failed_fold_keeps_the_cursor_and_the_transcript(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [bulky("user", 2000), {"role": "assistant", "content": "tail"}]
    record_history(db, db.session(sid), history, llm=FakeLLM(fail_json=True))
    saved = db.session(sid)
    assert len(saved["history"]) == 2 and saved["folded_through"] == 0 and not saved.get("summary")
    assert db.memories("ps-1") == []


def test_a_plain_answer_and_the_tool_loop_see_the_memory(db):
    remember(db, "ps-1", "Their fiscal year starts in April", kind="fact")
    llm = FakeLLM()
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    db.update_session(sid, title="Hello", tags=[])
    events = list(service.handle_message(db, llm, sid, "hello"))
    assert work_chat(db, llm, events[0]["run_id"]) is True
    systems = [c["system"] for c in llm.calls if c["kind"] in ("json", "text")]
    assert systems and all("fiscal year" in system and "只作背景" in system for system in systems)


def test_the_next_prompt_keeps_the_summary_with_the_raw_tail(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [bulky("user", 2000, "Q"), {"role": "assistant", "content": "keep-assistant"}]
    record_history(db, db.session(sid), history, llm=FakeLLM(json_replies=[{
        "summary": "Warehouse closes the first week of July",
    }]))
    saved = db.session(sid)
    from backend.orchestrator.speak import _chat_messages

    block = summary_block(saved["summary"])
    assert "Warehouse closes the first week of July" in block and "只作背景" in block
    messages = _chat_messages(saved, "question 9")
    contents = [m["content"] for m in messages]
    assert all("QQQQ" not in content for content in contents)
    assert "keep-assistant" in contents and contents[-1] == "question 9"


def test_sql_prompt_includes_standing_memory(monkeypatch):
    from backend.text2sql.text2sql import _write_sql

    calls: list[dict] = []

    class _DeepSeek:
        def response(self, **kwargs):
            calls.append(kwargs)
            return type("Reply", (), {"output_text": '{"sql": "SELECT 1"}'})()

    monkeypatch.setattr("backend.text2sql.text2sql.deepseek_client", lambda: _DeepSeek())
    _write_sql("how many orders", "purchase_orders(id)", context="[fact] fiscal year starts in April")
    assert "fiscal year" in calls[0]["instructions"]
    calls.clear()
    _write_sql("how many orders", "purchase_orders(id)")
    assert "fiscal year" not in calls[0]["instructions"]


def test_an_unsummarised_prefix_stays_in_the_next_prompt(db):
    sid = service.new_session(db, "ps-1")["id"]
    history = [bulky("user", 800, "Q"), {"role": "assistant", "content": "keep-assistant"}]
    record_history(db, db.session(sid), history, llm=FakeLLM())
    from backend.orchestrator.speak import _chat_messages

    contents = [m["content"] for m in _chat_messages(db.session(sid), "next")]
    assert "QQQQ" in contents[0] and "keep-assistant" in contents[1] and contents[-1] == "next"


def test_standing_block_hides_another_persons_note(db):
    from backend.memory import standing_block

    db.upsert_user("u-a", "dist-1", "Ada")
    db.upsert_user("u-b", "dist-1", "Bea")
    remember(db, "ps-1", "Ada ships by air", user_id="u-a")
    remember(db, "ps-1", "Bea wants pallets", user_id="u-b")
    remember(db, "ps-1", "Warehouse closes in July")
    block = standing_block(db, "ps-1", "u-a")
    assert "Ada ships by air" in block and "Warehouse closes in July" in block
    assert "Bea wants pallets" not in block
    page = [m.content for m in db.memories("ps-1", user_id="u-a")]
    assert page == ["Ada ships by air"]


def test_plan_history_skips_the_folded_prefix_and_keeps_the_rest():
    from backend.orchestrator.plan import _history_block

    folded = [{"role": "user", "content": f"old-{i}"} for i in range(10)]
    fresh = [
        {"role": "user", "content": "那改成查上海的天气吧"},
        {"role": "assistant", "content": "需要我用 web_search 吗"},
    ]
    block = _history_block([*folded, *fresh], 10)
    assert "old-0" not in block and "old-9" not in block
    assert "那改成查上海的天气吧" in block
    longer = [{"role": "user", "content": f"turn-{i}"} for i in range(12)]
    shown = _history_block(longer, 0)
    assert "turn-0" in shown and "turn-11" in shown
