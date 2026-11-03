"""Several tasks in one turn share one reply. Redis holds the lock and the results."""

from __future__ import annotations

import threading

from backend import service
from backend.llm import FakeLLM
from backend.redis_store import begin_turn, drop_turn, fx_drop, fx_put, load_results, store_result
from backend.schemas import PurchaseOrder
from backend.services.chat_run import work_chat
from backend.tools.exchange_rate import quote


def test_results_arrive_in_any_order_and_read_back_together():
    begin_turn("sess-redis", "turn-1", ["a", "b"])
    try:
        assert store_result("sess-redis", "turn-1", "b", {"id": "b", "ok": True, "payload": {"n": 2}}) == 1
        assert store_result("sess-redis", "turn-1", "a", {"id": "a", "ok": True, "payload": {"n": 1}}) == 0
        loaded = load_results("sess-redis", "turn-1")
        assert loaded["a"]["payload"]["n"] == 1 and loaded["b"]["payload"]["n"] == 2
    finally:
        drop_turn("sess-redis", "turn-1")


def test_cached_exchange_rate_skips_the_network(monkeypatch):
    fx_drop("USD", "EUR")
    fx_put("USD", "EUR", rate=2, date="2026-01-01", source="test")

    def boom(*_a, **_k):
        raise AssertionError("network")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    try:
        quoted = quote("USD", "EUR", amount=3)
        assert quoted["rate"] == 2 and quoted["converted"] == 6
    finally:
        fx_drop("USD", "EUR")


def test_one_reply_covers_contract_and_data(db, monkeypatch):
    db.insert_po(PurchaseOrder(**dict(
        id="po-fan", po_number="PO-FAN", brand_id="brand-1", distributor_id="dist-1", partnership_id="ps-1",
        status="submitted", currency="USD", payment_terms="net_30", incoterms="FOB", shipping_method="sea",
        eta_date=None, ship_to_country="US", notes=None, discount_amount=None, discount_percentage=None,
        subtotal_amount=1000, total_amount=1000,
    )), [], [])
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-fan", "ps-1", "u-a")["id"]
    from tests.test_text2sql import _models

    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
    ])
    llm = FakeLLM(json_replies=[
        {"tags": ["data", "contract"], "title": "一起"},
        {"tasks": [
            {"id": "c", "kind": "agent", "name": "contract", "input": {}, "depends_on": []},
            {"id": "d", "kind": "agent", "name": "data", "input": {"question": "order total"}, "depends_on": []},
        ]},
    ], text_reply="合同还没有文件。总额 1000。")
    events = list(service.handle_message(db, llm, sid, "查一下订单总额，顺便看看合同"))
    assert work_chat(db, llm, events[0]["run_id"]) is True
    history = db.session(sid)["history"]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[-1]["content"] == "合同还没有文件。总额 1000。"
    parent = db.find_agent_run_by_id(events[0]["run_id"])
    assert parent["parent_run_id"] is None
    children = db.find_runs_by_owner(parent["id"])
    assert {child["run_type"] for child in children} == {"contract", "data", "open"}
    assert all(child["owner_run_id"] == parent["id"] for child in children)
    assert all(child["parent_run_id"] is None for child in children)


def test_empty_plan_answers_without_child_runs(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-hi", "ps-1", "u-a")["id"]
    db.update_session(sid, title="hello", tags=[])
    llm = FakeLLM(text_reply="你好，需要什么？")
    events = list(service.handle_message(db, llm, sid, "你好"))
    run_id = events[0]["run_id"]
    assert work_chat(db, llm, run_id) is True
    assert db.find_agent_run_by_id(run_id)["status"] == "succeeded"
    assert db.find_runs_by_owner(run_id) == []
    assert db.session(sid)["history"][-1]["content"] == "你好，需要什么？"


def test_same_wave_is_two_child_runs(db, monkeypatch):
    from backend.services.chat_run import execute_chat
    from backend.services.run_worker import work_once

    db.insert_po(PurchaseOrder(**dict(
        id="po-wave", po_number="PO-WAVE", brand_id="brand-1", distributor_id="dist-1", partnership_id="ps-1",
        status="submitted", currency="USD", payment_terms="net_30", incoterms="FOB", shipping_method="sea",
        eta_date=None, ship_to_country="US", notes=None, discount_amount=None, discount_percentage=None,
        subtotal_amount=1000, total_amount=1000,
    )), [], [])
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-wave", "ps-1", "u-a")["id"]
    db.update_session(sid, title="wave", tags=["data"])
    from tests.test_text2sql import _models

    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
    ])
    llm = FakeLLM(json_replies=[{
        "tasks": [
            {"id": "c", "kind": "agent", "name": "contract", "input": {}, "depends_on": []},
            {"id": "d", "kind": "agent", "name": "data", "input": {"question": "order total"}, "depends_on": []},
        ],
    }], text_reply="查完了。")
    events = list(service.handle_message(db, llm, sid, "总额和合同"))
    parent_id = events[0]["run_id"]
    execute = execute_chat(db, llm)
    assert work_once(db, "w1", execute, run_id=parent_id) is True
    assert db.find_agent_run_by_id(parent_id)["status"] == "waiting_for_tasks"
    children = db.find_runs_by_owner(parent_id)
    assert len(children) == 2
    assert {child["status"] for child in children} == {"queued"}
    for child in children:
        assert work_once(db, "w2", execute, run_id=child["id"]) is True
    assert db.find_agent_run_by_id(parent_id)["status"] == "queued"
    assert work_once(db, "w1", execute, run_id=parent_id) is True
    assert db.find_agent_run_by_id(parent_id)["status"] == "succeeded"
    assert db.session(sid)["history"][-1]["content"] == "查完了。"


def test_session_lock_is_exclusive():
    from backend.redis_store import hold_session

    order: list[str] = []
    holding = threading.Event()
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with hold_session("sess-lock-test"):
                order.append("a")
                holding.set()
                threading.Event().wait(0.1)
        except BaseException as err:
            errors.append(err)

    def second() -> None:
        try:
            assert holding.wait(2)
            with hold_session("sess-lock-test"):
                order.append("b")
        except BaseException as err:
            errors.append(err)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert errors == []
    assert order == ["a", "b"]


def test_sweep_wakes_a_parent_after_children_finish(db):
    from datetime import timedelta

    from backend.db import utcnow
    from backend.services.run_worker import sweep_waiting_for_tasks

    db.upsert_user("u-a", "dist-1", "Ada")
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "chat", "owner-wait", {"session_id": "sess-sweep", "message": "hi"},
    )
    assert db.claim_chat_turn(parent["id"], "w1", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "w1"}) is not None
    child, _created = db.create_agent_run(
        "dist-1", "u-a", "data", "child-done", {"session_id": "sess-sweep"},
        owner_run_id=parent["id"],
    )
    assert db.claim_chat_turn(child["id"], "w2", utcnow() + timedelta(minutes=5)) is not None
    assert db.complete_run(child["id"], {"ok": True}) is not None
    assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 1
    assert db.find_agent_run_by_id(parent["id"])["status"] == "queued"


def test_sweep_leaves_a_parent_while_a_child_is_still_running(db):
    from datetime import timedelta

    from backend.db import utcnow
    from backend.services.run_worker import sweep_waiting_for_tasks

    db.upsert_user("u-a", "dist-1", "Ada")
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "chat", "owner-busy", {"session_id": "sess-busy", "message": "hi"},
    )
    assert db.claim_chat_turn(parent["id"], "w1", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "w1"}) is not None
    child, _created = db.create_agent_run(
        "dist-1", "u-a", "data", "child-run", {"session_id": "sess-busy"},
        owner_run_id=parent["id"],
    )
    assert db.claim_chat_turn(child["id"], "w2", utcnow() + timedelta(minutes=5)) is not None
    assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 0
    assert db.find_agent_run_by_id(parent["id"])["status"] == "waiting_for_tasks"
