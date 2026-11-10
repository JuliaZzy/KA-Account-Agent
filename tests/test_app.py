"""End-to-end through HTTP with a scripted model: open → plan → PO turn (SSE) → confirm → claim."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.llm import FakeLLM
from backend.services.chat_run import work_chat


def open_meta(tags=("po",), title="10 cases fish oil"):
    return {"tags": list(tags), "title": title}


def events(resp) -> list[dict]:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return [{"type": "run", "run_id": resp.json()["run_id"]}]
    return [json.loads(l[6:]) for l in resp.text.split("\n\n") if l.startswith("data: ")]


def _of(ev: list[dict], kind: str) -> dict:
    return next(event for event in ev if event["type"] == kind)


def test_full_flow(db):
    llm = FakeLLM(json_replies=[open_meta()], text_reply="All set — press Confirm.")
    c = TestClient(create_app(db=db, llm=llm))

    assert c.get("/").json()["service"] == "ka-account-agent"
    sid = c.post("/api/sessions", json={"partnership_id": "ps-1", "user_id": None}).json()["id"]
    db.upsert_user("u-a", "dist-1", "Ada")
    db.update_session(sid, user_id="u-a")

    ev = events(c.post(f"/api/sessions/{sid}/message", json={"message": "10 cases of fish oil, no discount"}))
    assert [event["type"] for event in ev if event["type"] == "session"] == []
    run_id = _of(ev, "run")["run_id"]
    assert db.find_agent_run("dist-1", run_id)["status"] == "queued"
    assert db.session(sid)["title"] == "10 cases of fish oil, no discount"
    assert [row for row in db.find_runs_by_owner(run_id) if row["run_type"] == "open"] == []
    assert work_chat(db, llm, run_id) is True
    assert db.session(sid)["title"] == "10 cases fish oil"
    assert db.session(sid)["tags"] == ["po"]
    opened = next(row for row in db.find_runs_by_owner(run_id) if row["run_type"] == "open")
    assert opened["status"] == "succeeded"
    trace = db.run_trace("dist-1", opened["id"])
    named = next(
        row for row in trace["model_calls"]
        if (row.get("request_json") or {}).get("schema", {}).get("name") == "session_open"
    )
    assert named["model"] == "qwen3.7-flash" and named["status"] == "succeeded"
    step = next(step for step in trace["steps"] if step["id"] == named["step_id"])
    assert (step["step_type"], step["name"], step["status"]) == ("model", "model", "succeeded")
    parent_trace = db.run_trace("dist-1", run_id)
    assert not any(
        (row.get("request_json") or {}).get("schema", {}).get("name") == "session_open"
        for row in parent_trace["model_calls"]
    )
    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "succeeded"
    assert run["output_json"]["text"] == "All set — press Confirm."
    assert db.session(sid)["history"][-1]["content"] == "All set — press Confirm."
    assert db.session(sid)["history"][-1]["run_id"] == run_id


def test_empty_plan_answers_directly(db):
    llm = FakeLLM(json_replies=[open_meta(tags=(), title="hello")], text_reply="Happy to help — what do you need?")
    db.upsert_user("u-a", "dist-1", "Ada")
    c = TestClient(create_app(db=db, llm=llm))
    sid = c.post("/api/sessions", json={"user_id": "u-a"}).json()["id"]
    ev = events(c.post(f"/api/sessions/{sid}/message", json={"message": "happy new year!"}))
    assert [event["type"] for event in ev if event["type"] == "session"] == []
    run_id = _of(ev, "run")["run_id"]
    assert db.session(sid)["title"] == "happy new year!"
    assert [row for row in db.find_runs_by_owner(run_id) if row["run_type"] == "open"] == []
    assert work_chat(db, llm, run_id) is True
    assert db.session(sid)["title"] == "hello"
    assert any(row["run_type"] == "open" for row in db.find_runs_by_owner(run_id))
    assert "Happy to help" in db.session(sid)["history"][-1]["content"]


def test_contract_plan_without_bytes_still_answers(db):
    llm = FakeLLM(json_replies=[
        open_meta(tags=("contract",), title="agreement"),
    ], text_reply="Please attach the agreement PDF.")
    db.upsert_user("u-a", "dist-1", "Ada")
    c = TestClient(create_app(db=db, llm=llm))
    sid = c.post("/api/sessions", json={"user_id": "u-a"}).json()["id"]
    ev = events(c.post(f"/api/sessions/{sid}/message", json={"message": "new agreement attached", "attachments": ["agreement.pdf"]}))
    assert work_chat(db, llm, _of(ev, "run")["run_id"]) is True
    assert "attach" in db.session(sid)["history"][-1]["content"].lower()


def test_model_failure_leaves_the_stream_open_and_fails_on_the_worker(db):
    llm = FakeLLM(fail_json=True)
    db.upsert_user("u-a", "dist-1", "Ada")
    c = TestClient(create_app(db=db, llm=llm))
    sid = c.post("/api/sessions", json={"user_id": "u-a"}).json()["id"]
    db.update_session(sid, title="Hello", tags=[])
    resp = c.post(f"/api/sessions/{sid}/message", json={"message": "hello"})
    assert resp.status_code == 202
    ev = events(resp)
    run_id = _of(ev, "run")["run_id"]
    assert all(e.get("type") != "error" for e in ev)
    assert work_chat(db, llm, run_id) is True
    run = db.find_agent_run("dist-1", run_id)
    assert run["status"] == "queued"
    assert run["error_json"]["kind"] == "transient"


def test_a_long_chat_folds_after_the_worker_saves(db, monkeypatch):
    from backend import memory

    ran: list = []

    def spy_fold(*args, **kwargs):
        ran.append(1)
        return memory_fold(*args, **kwargs)

    memory_fold = memory.fold_session
    monkeypatch.setattr(memory, "fold_session", spy_fold)

    llm = FakeLLM(
        json_replies=[{"tasks": []}, {"summary": "They ship on Tuesdays"}],
        text_reply="Noted.",
    )
    db.upsert_user("u-a", "dist-1", "Ada")
    c = TestClient(create_app(db=db, llm=llm))
    sid = c.post("/api/sessions", json={"partnership_id": "ps-1", "user_id": "u-a"}).json()["id"]
    db.update_session(sid, history=[{"role": "user", "content": "Q" * 8000}], title="already open", tags=["chat"])

    resp = c.post(f"/api/sessions/{sid}/message", json={"message": "and one more"})
    assert resp.status_code == 202
    run_id = _of(events(resp), "run")["run_id"]
    assert work_chat(db, llm, run_id) is True
    assert ran == [1]
    saved = db.session(sid)
    assert len(saved["history"]) == 3 and saved["folded_through"] == 1
    assert saved["summary"] == "They ship on Tuesdays"
    assert [m for m in db.memories("ps-1") if m.source == "chat"] == []


def test_empty_message_is_rejected(db):
    c = TestClient(create_app(db=db, llm=FakeLLM()))
    sid = c.post("/api/sessions", json={}).json()["id"]
    assert c.post(f"/api/sessions/{sid}/message", json={"message": "  "}).status_code == 400


def _po(id: str, number: str):
    from backend.schemas import PurchaseOrder

    return PurchaseOrder(
        id=id, po_number=number, brand_id="brand-1", distributor_id="dist-1", partnership_id="ps-1",
        status="submitted", currency="USD", payment_terms="net_30", incoterms="FOB", shipping_method="sea",
        eta_date=None, ship_to_country="US", notes=None, discount_amount=None, discount_percentage=None,
        subtotal_amount=1000, total_amount=1000,
    )


def _file_contract(db, title: str):
    from backend.contract.ingest import ingest_extraction

    return ingest_extraction(
        db, {"minimum_order_quantities": [{"scope": "per_order", "quantity": 500, "currency": "USD"}]},
        brand_id="brand-1", partnership_id="ps-1", title=title,
    )


def test_archive_session_drops_the_chat_and_keeps_its_records(db):
    from backend.schemas import Memory, NarrativeReport

    c = TestClient(create_app(db=db, llm=FakeLLM()))
    sid = c.post("/api/sessions", json={"partnership_id": "ps-1"}).json()["id"]
    keep = c.post("/api/sessions", json={"partnership_id": "ps-1"}).json()["id"]
    db.insert_po(_po("po-arch", "PO-ARCH"), [], [])
    filed = _file_contract(db, "From this chat")
    db.update_session(sid, po_id="po-arch", history=[{"role": "assistant", "contract_id": filed.contract.id}])
    db.save_narrative_report(NarrativeReport(
        id="nr-arch", brand_id="brand-1", partnership_id="ps-1", session_id=sid, title="August", body="Sold well.",
    ))
    db.add_memory(Memory(
        partnership_id="ps-1", kind="fact", content="They ship on Tuesdays", source="chat", session_id=sid,
    ))

    assert c.post(f"/api/sessions/{sid}/archive").status_code == 204
    assert c.get(f"/api/sessions/{sid}").status_code == 404
    assert [row["id"] for row in c.get("/api/sessions?partnership_id=ps-1").json()] == [keep]
    assert db.po("po-arch") is not None
    assert db.contract(filed.contract.id) is not None
    assert db.narrative_report("nr-arch") is not None
    left = db.memories("ps-1")
    assert [(m.content, m.session_id) for m in left] == [("They ship on Tuesdays", None)]
    assert c.post(f"/api/sessions/{sid}/archive").status_code == 404


def test_delete_session_drops_the_chat_and_the_records_it_filed(db):
    from backend.schemas import Memory, NarrativeReport

    c = TestClient(create_app(db=db, llm=FakeLLM()))
    sid = c.post("/api/sessions", json={"partnership_id": "ps-1"}).json()["id"]
    keep = c.post("/api/sessions", json={"partnership_id": "ps-1"}).json()["id"]
    db.insert_po(_po("po-gone", "PO-GONE"), [{
        "product_id": "p-fo", "sku": "FO-100", "product_name": "Fish Oil",
        "quantity": 10, "case_price": 100, "line_total": 1000, "case_pack": 12,
    }], [])
    db.insert_po(_po("po-keep", "PO-KEEP"), [], [])
    db.add_po_event("po-gone", "submitted", {})
    filed = _file_contract(db, "From this chat")
    other = _file_contract(db, "From the other chat")
    db.update_session(sid, po_id="po-gone", history=[{"role": "assistant", "contract_id": filed.contract.id}])
    db.update_session(keep, po_id="po-keep", history=[{"role": "assistant", "contract_id": other.contract.id}])
    db.save_narrative_report(NarrativeReport(
        id="nr-gone", brand_id="brand-1", partnership_id="ps-1", session_id=sid, title="August", body="Sold well.",
    ))
    db.save_narrative_report(NarrativeReport(
        id="nr-keep", brand_id="brand-1", partnership_id="ps-1", session_id=keep, title="Other", body="Stays.",
    ))
    db.add_memory(Memory(
        partnership_id="ps-1", kind="fact", content="They ship on Tuesdays", source="chat", session_id=sid,
    ))

    assert c.delete(f"/api/sessions/{sid}").status_code == 204
    assert c.get(f"/api/sessions/{sid}").status_code == 404
    assert [row["id"] for row in c.get("/api/sessions?partnership_id=ps-1").json()] == [keep]
    assert db.po("po-gone") is None and db.po_items("po-gone") == []
    assert db.po("po-keep") is not None
    assert db.contract(filed.contract.id) is None
    assert db.contract(other.contract.id) is not None
    kept_rules = db.derived_rules(other.contract.id)
    assert kept_rules and db.derived_rules(filed.contract.id) == []
    assert len(db.rules("brand-1")) == len(kept_rules)
    assert db.narrative_report("nr-gone") is None
    assert db.narrative_report("nr-keep") is not None and db.narrative_report("nr-keep").session_id == keep
    left = db.memories("ps-1")
    assert [(m.content, m.session_id) for m in left] == [("They ship on Tuesdays", None)]
    assert c.delete(f"/api/sessions/{sid}").status_code == 404
