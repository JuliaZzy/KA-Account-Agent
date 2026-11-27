"""Parent reconciliation, the stale-wake cap, and the waiting-parent duration cap."""

from __future__ import annotations

import threading
from datetime import timedelta

from backend.db import utcnow
from backend.db.orm import AgentRun
from backend.db.outcome import Outcome
from backend.llm import FakeLLM
from backend.orchestrator.gather import checkpoint_state, finish_child, load_state
from backend.services.chat_run import execute_chat
from backend.services.run_finalize import notify_parent
from backend.services.run_names import RunStatus
from backend.services.run_worker import sweep_waiting_for_tasks, work_once
from backend.services.sweeper import close_attention


def _user(db) -> None:
    db.upsert_user("u-a", "dist-1", "Ada")


def _checkpoint(task_id: str = "1") -> dict:
    return {
        "context": [],
        "completed_tool_calls": [],
        "last": "user",
        "orchestrator": {
            "tasks": [{"id": task_id, "kind": "tool", "name": "report.draft", "input": {}, "depends_on": []}],
            "finished": [],
            "results": {},
            "wave_id": "wave-1",
            "wave_ids": [task_id],
            "stale_wakes": 0,
        },
    }


def _park(db, key: str, *, session_id: str, task_id: str = "1") -> dict:
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "chat", key, {"session_id": session_id, "message": "月报"},
    )
    assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "wave-1"}) is not None
    db.append_checkpoint(parent["id"], _checkpoint(task_id))
    return parent


def _child(db, parent: dict, key: str, *, session_id: str, task_id: str = "1") -> dict:
    child, _created = db.create_agent_run(
        "dist-1", "u-a", "report", key, {
            "session_id": session_id, "task_id": task_id, "tool_name": "report.draft", "wave_id": "wave-1",
        },
        owner_run_id=parent["id"],
    )
    return child


def _orch(db, run_id: str) -> dict:
    return load_state(db.latest_checkpoint(run_id)) or {}


def test_finalize_all_paths_repairs_a_terminal_child_and_wakes_the_parent(db):
    _user(db)
    parent = _park(db, "fin-repair", session_id="sess-repair")
    child = _child(db, parent, "fin-repair-child", session_id="sess-repair")
    assert db.claim_chat_turn(child["id"], "w2", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(child["id"], Outcome(kind="failed", error={"code": "PROVIDER_INTERNAL", "message": "down"}, reason="permanent", dead_letter=True), allowed_from=frozenset({RunStatus.RUNNING})) is not None
    assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 1
    state = _orch(db, parent["id"])
    assert state["results"]["1"]["ok"] is False
    # The fill is progress, so the same sweep writes 0 and does not also increment.
    assert state["stale_wakes"] == 0
    assert db.find_agent_run_by_id(parent["id"])["status"] == "queued"
    events = db.list_run_events("dist-1", parent["id"])
    assert any(
        (event.get("payload_json") or {}).get("reason") == "waiting_tasks_stale" for event in events
    )


def test_finalize_all_paths_wakes_when_redis_has_no_counter(db, monkeypatch):
    _user(db)
    parent = _park(db, "fin-redis", session_id="sess-redis")
    child = _child(db, parent, "fin-redis-child", session_id="sess-redis")
    assert db.claim_chat_turn(child["id"], "w2", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(child["id"], Outcome(kind="failed", error={"code": "PROVIDER_INTERNAL", "message": "down"}, reason="permanent", dead_letter=True), allowed_from=frozenset({RunStatus.RUNNING})) is not None
    monkeypatch.setattr("backend.orchestrator.gather.store_result", lambda *_a, **_k: -1)
    finish_child(db, db.find_agent_run_by_id(child["id"]), {
        "id": "1", "kind": "tool", "name": "report.draft", "ok": False,
        "error": "PROVIDER_INTERNAL: down", "payload": {}, "status": "failed",
    })
    assert db.find_agent_run_by_id(parent["id"])["status"] == "waiting_for_tasks"
    assert _orch(db, parent["id"])["results"]["1"]["ok"] is False
    assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 1
    assert _orch(db, parent["id"])["stale_wakes"] == 0
    assert db.find_agent_run_by_id(parent["id"])["status"] == "queued"


def test_finalize_all_paths_retries_when_notify_parent_cannot_write(db, monkeypatch):
    _user(db)
    parent = _park(db, "fin-notify", session_id="sess-notify")
    child = _child(db, parent, "fin-notify-child", session_id="sess-notify")
    assert db.claim_chat_turn(child["id"], "w2", utcnow() + timedelta(minutes=5)) is not None
    assert db.finalize_run_row(child["id"], Outcome(kind="failed", error={"code": "PROVIDER_INTERNAL", "message": "down"}, reason="permanent", dead_letter=True), allowed_from=frozenset({RunStatus.RUNNING})) is not None

    def boom(*_args, **_kwargs):
        raise RuntimeError("checkpoint write failed")

    monkeypatch.setattr("backend.orchestrator.gather.finish_child", boom)
    notify_parent(db, child["id"])
    assert "1" not in (_orch(db, parent["id"]).get("results") or {})
    monkeypatch.undo()
    assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 1
    assert _orch(db, parent["id"])["results"]["1"]["ok"] is False
    assert db.find_agent_run_by_id(parent["id"])["status"] == "queued"


def test_finalize_all_paths_leaves_a_live_child_uncounted(db):
    _user(db)
    parent = _park(db, "fin-live", session_id="sess-live")
    child = _child(db, parent, "fin-live-child", session_id="sess-live")
    assert db.claim_chat_turn(child["id"], "w2", utcnow() + timedelta(minutes=5)) is not None
    before = db.latest_checkpoint(parent["id"])["checkpoint_index"]
    assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 0
    assert db.find_agent_run_by_id(parent["id"])["status"] == "waiting_for_tasks"
    assert db.latest_checkpoint(parent["id"])["checkpoint_index"] == before
    assert _orch(db, parent["id"]).get("stale_wakes") == 0


def test_finalize_all_paths_answers_after_three_empty_wakes(db):
    _user(db)
    sid = db.create_session("sess-stale-answer", "ps-1", "u-a")["id"]
    parent = _park(db, "fin-answer", session_id=sid)
    for _ in range(3):
        row = db.find_agent_run_by_id(parent["id"])
        if row["status"] != "waiting_for_tasks":
            assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
            assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "wave-1"}) is not None
        sweep_waiting_for_tasks(db, stale_after=timedelta(0))
    state = _orch(db, parent["id"])
    assert state["results"]["1"]["error"].startswith("WAVE_STALE:")
    assert "1" in state["finished"]
    assert db.find_agent_run_by_id(parent["id"])["status"] == "queued"
    assert work_once(db, "w", execute_chat(db, FakeLLM(text_reply="月报没有出来。")), run_id=parent["id"]) is True
    assert db.find_agent_run_by_id(parent["id"])["status"] == "succeeded"


def test_finalize_all_paths_keeps_stale_wakes_across_three_redispatches(db):
    _user(db)
    parent = _park(db, "fin-carry", session_id="sess-carry")
    for expected in (1, 2, 3):
        assert db.note_stale_wake(parent["id"], progressed=False) == expected
        assert db.requeue_waiting_tasks(parent["id"], {"reason": "waiting_tasks_stale"}) is not None
        assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
        parked = db.commit_task_wave(parent["id"], [{
            "run_type": "tool",
            "idempotency_key": "fin-carry-child",
            "input": {"task_id": "1", "wave_id": f"wave-{expected}"},
            "started": {"tool_name": "report.draft", "task_id": "1", "display": "report.draft"},
        }], checkpoint_state([], [], {}, wave_id=f"wave-{expected}", wave_ids=["1"]))
        assert parked is not None
        assert db.latest_checkpoint(parent["id"])["state_json"]["orchestrator"]["stale_wakes"] == expected


def test_finalize_all_paths_counts_two_sweeps_once_each(db):
    _user(db)
    parent = _park(db, "fin-race", session_id="sess-race")
    counts: list[int] = []
    errors: list[BaseException] = []

    def once() -> None:
        try:
            counts.append(db.note_stale_wake(parent["id"], progressed=False))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=once), threading.Thread(target=once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert errors == []
    assert sorted(counts) == [1, 2]
    assert _orch(db, parent["id"])["stale_wakes"] == 2


def test_finalize_all_paths_skips_a_note_after_the_parent_is_awake(db):
    _user(db)
    parent = _park(db, "fin-awake", session_id="sess-awake")
    assert db.note_stale_wake(parent["id"], progressed=False) == 1
    assert db.requeue_waiting_tasks(parent["id"]) is not None
    before = db.latest_checkpoint(parent["id"])["checkpoint_index"]
    assert db.note_stale_wake(parent["id"], progressed=False) == 0
    saved = db.latest_checkpoint(parent["id"])
    assert saved["checkpoint_index"] == before
    assert saved["state_json"]["orchestrator"]["stale_wakes"] == 1


def test_finalize_all_paths_fails_a_waiting_parent_past_its_duration(db):
    _user(db)
    sid = db.create_session("sess-duration-wait", "ps-1", "u-a")["id"]
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "chat", "fin-duration",
        {"session_id": sid, "message": "月报"},
        max_duration_seconds=0,
    )
    assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "wave-1"}) is not None
    assert parent["id"] in close_attention(db)
    saved = db.find_agent_run_by_id(parent["id"])
    assert saved["status"] == "failed"
    assert saved["error_json"]["code"] == "COST_BUDGET_EXCEEDED"
    assert len(db.list_dead_letter_jobs(parent["id"])) == 1
    history = db.session(sid)["history"]
    assert history[-1]["role"] == "assistant" and history[-1]["failed"] is True


def test_finalize_all_paths_cancels_a_waiting_parent_that_asked_to_stop(db):
    _user(db)
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "chat", "fin-duration-cancel", {"message": "月报"}, max_duration_seconds=0,
    )
    assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
    assert db.mark_waiting_for_tasks(parent["id"], {"wave_id": "wave-1"}) is not None
    with db.tx() as s:
        row = s.get(AgentRun, parent["id"])
        row.cancel_requested_at = utcnow()
    assert parent["id"] in close_attention(db)
    assert db.find_agent_run_by_id(parent["id"])["status"] == "cancelled"
    assert db.list_dead_letter_jobs(parent["id"]) == []


def test_finalize_all_paths_retry_and_outbox_retry_do_not_deadlock(db):
    _user(db)
    parent, _created = db.create_agent_run(
        "dist-1", "u-a", "po", "fin-lock", {"message": "hi"},
    )
    pending = db.find_pending_outbox_jobs()
    outbox_id = next(job["id"] for job in pending if job["payload_json"].get("run_id") == parent["id"])
    assert db.claim_chat_turn(parent["id"], "w", utcnow() + timedelta(minutes=5)) is not None
    errors: list[BaseException] = []
    done: list[str] = []

    def retry() -> None:
        try:
            db.schedule_retry(parent["id"], {"kind": "transient", "message": "later"}, (timedelta(seconds=1),))
            done.append("retry")
        except BaseException as exc:
            errors.append(exc)

    def publish() -> None:
        try:
            db.mark_outbox_retry(outbox_id, "publish failed")
            done.append("publish")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=retry), threading.Thread(target=publish)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert errors == []
    assert sorted(done) == ["publish", "retry"]


def test_finalize_all_paths_unknown_error_fails_once_then_answers(db):
    _user(db)
    sid = db.create_session("sess-internal", "ps-1", "u-a")["id"]
    parent = _park(db, "fin-internal", session_id=sid)
    child = _child(db, parent, "fin-internal-child", session_id=sid)

    def boom(_run):
        raise RuntimeError("This event loop is already running")

    immediate = (timedelta(0),)
    assert work_once(db, "w-child", boom, immediate, run_id=child["id"]) is True
    failed = db.find_agent_run_by_id(child["id"])
    assert failed["status"] == "failed"
    assert failed["error_json"]["code"] == "UNKNOWN"
    assert failed["error_json"]["exc_type"] == "RuntimeError"
    assert failed["error_json"]["code"] != "PROVIDER_INTERNAL"
    state = _orch(db, parent["id"])
    assert state["results"]["1"]["ok"] is False
    if db.find_agent_run_by_id(parent["id"])["status"] == "waiting_for_tasks":
        assert sweep_waiting_for_tasks(db, stale_after=timedelta(0)) == 1
    assert db.find_agent_run_by_id(parent["id"])["status"] == "queued"
    assert work_once(
        db, "w", execute_chat(db, FakeLLM(text_reply="月报没有出来。")), run_id=parent["id"],
    ) is True
    assert db.find_agent_run_by_id(parent["id"])["status"] == "succeeded"
