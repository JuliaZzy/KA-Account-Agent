"""Two chats at once, and one chat's turns in order.

The overlap is the model call: that is where a second turn would read a
history the first has not saved yet.
"""

from __future__ import annotations

import threading
import time

from backend import service
from backend.llm import FakeLLM
from backend.services.chat_run import work_chat


def test_two_sessions_overlap_on_the_model(db):
    state = {"inside": 0, "max": 0}
    mu = threading.Lock()
    gate = threading.Barrier(2)
    errors: list[BaseException] = []

    class Overlap(FakeLLM):
        def complete_json(self, **kwargs):
            with mu:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
            try:
                gate.wait(timeout=3)
                return super().complete_json(**kwargs)
            finally:
                with mu:
                    state["inside"] -= 1

    def run(sid: str, llm: FakeLLM) -> None:
        try:
            events = list(service.handle_message(db, llm, sid, "hello"))
            work_chat(db, llm, events[0]["run_id"])
        except BaseException as err:
            gate.abort()
            errors.append(err)

    db.upsert_user("u-a", "dist-1", "Ada")
    first = service.new_session(db, "ps-1", "u-a")["id"]
    second = service.new_session(db, "ps-1", "u-a")["id"]
    one = Overlap(json_replies=[{"tags": [], "title": "one"}, {"tasks": []}])
    two = Overlap(json_replies=[{"tags": [], "title": "two"}, {"tasks": []}])
    threads = [
        threading.Thread(target=run, args=(first, one)),
        threading.Thread(target=run, args=(second, two)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert state["max"] == 2
    assert [m["content"] for m in db.session(first)["history"] if m["role"] == "user"] == ["hello"]
    assert [m["content"] for m in db.session(second)["history"] if m["role"] == "user"] == ["hello"]


def test_enqueue_yields_the_run_before_the_title(db):
    """Enqueue writes a provisional title; OPEN refine replaces it after chat ends."""
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    llm = FakeLLM(json_replies=[{"tags": ["data"], "title": "订单总额"}])
    turn = service.handle_message(db, llm, sid, "how much did I order")
    first = next(turn)
    assert first["type"] == "run"
    assert db.session(sid).get("title") == "how much did I order"
    list(turn)
    assert work_chat(db, llm, first["run_id"]) is True
    assert db.session(sid)["title"] == "订单总额"
    assert db.session(sid)["tags"] == ["data"]


def test_one_session_finishes_a_turn_before_the_next_starts(db):
    from backend.services.run_worker import SessionBusy

    state = {"inside": 0, "max": 0, "two_running": False}
    mu = threading.Lock()
    run_ids: list[str] = []
    errors: list[BaseException] = []

    class Slow(FakeLLM):
        def complete_json(self, **kwargs):
            schema = kwargs.get("schema") or {}
            name = schema.get("name") if isinstance(schema, dict) else None
            if name == "session_open":
                return super().complete_json(**kwargs)
            with mu:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
                statuses = [
                    db.find_agent_run_by_id(run_id)["status"] for run_id in run_ids
                ]
                if statuses.count("running") > 1:
                    state["two_running"] = True
            try:
                threading.Event().wait(0.2)
                return super().complete_json(**kwargs)
            finally:
                with mu:
                    state["inside"] -= 1

    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    llm = Slow(json_replies=[
        {"tags": [], "title": "a"},
    ], text_reply="Hello")

    def run(text: str) -> None:
        try:
            events = list(service.handle_message(db, llm, sid, text))
            run_id = events[0]["run_id"]
            with mu:
                run_ids.append(run_id)
            for _ in range(40):
                try:
                    if work_chat(db, llm, run_id):
                        return
                except SessionBusy:
                    time.sleep(0.05)
            raise TimeoutError(text)
        except BaseException as err:
            errors.append(err)

    threads = [threading.Thread(target=run, args=(text,)) for text in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert state["max"] == 1
    assert state["two_running"] is False
    history = db.session(sid)["history"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    users = [m["content"] for m in history if m["role"] == "user"]
    assert sorted(users) == ["one", "two"]
