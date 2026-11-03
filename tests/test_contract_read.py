"""An agreement file is read into the extraction the contract tables already store.
The model fills the form; a quote that is not in the text is dropped; filing writes
the contract. A chat attachment that is only a name does not pretend to have the file.
"""

from __future__ import annotations

import json

import pytest

from backend import service
from backend.contract import read_agreement, read_and_ingest, sanitize_extraction
from backend.llm import FakeLLM
from backend.services.chat_run import work_chat


class _Output:
    def __init__(self, text: str):
        self.output_text = text


class _Readers:
    """One queue for both clients. School selections and extractions are consumed in call order."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat_completion(self, model, messages, stream=False, max_tokens=None, temperature=None):
        self.calls.append({"kind": "school", "model": model, "messages": messages})
        raw = self.replies.pop(0)
        return {"choices": [{"message": {"content": json.dumps(raw, ensure_ascii=False)}}]}

    def response(self, **kwargs):
        self.calls.append({
            "kind": "extract", "model": kwargs.get("model"), "input": kwargs.get("input"), "text": kwargs.get("text"),
        })
        raw = self.replies.pop(0)
        return _Output(json.dumps(raw, ensure_ascii=False))


def _user(call: dict) -> str:
    return call["messages"][1]["content"]

AGREEMENT = (
    "Distribution agreement.\n\n"
    "Term 2026-01-01 to 2027-12-31. Territory: Sweden.\n\n"
    "Each purchase order shall have a minimum value of USD 500.\n\n"
    "In each contract year the first and second containers are invoiced at list price less 3%."
)

EXTRACTION = {
    "contract_type": "distribution_agreement",
    "effective_date": "2026-01-01",
    "term_start_date": "2026-01-01",
    "term_end_date": "2027-12-31",
    "auto_renewal": False,
    "exclusivity_type": None,
    "territory_text": "Sweden.",
    "allowed_countries": ["se", "Sweden"],
    "excluded_countries": [],
    "minimum_order_quantities": [
        {"quantity": 500, "unit": "USD", "applies_per": "per_order", "source_clause_text": "minimum value of USD 500"},
    ],
    "commercial_discount_rules": [{
        "title": "Container sequence",
        "section_reference": "5",
        "applies_per": "each_contract_year",
        "basis": "shipping_container_sequence",
        "source_clause_text": "the first and second containers are invoiced at list price less 3%",
        "tiers": [{"from_container": 1, "to_container": 2, "discount_percent": 3, "notes": None}],
    }],
    "product_price_list": [],
    "field_evidence": {
        "minimum_order_quantities": [
            {"page": 1, "quote": "minimum value of USD 500", "section_hint": "4"},
            {"page": 1, "quote": "the model invented this sentence", "section_hint": None},
        ],
    },
    "confidence_score": 0.8,
}


def test_sanitize_keeps_only_quotes_that_are_in_the_document():
    pages = [(1, AGREEMENT)]
    out = sanitize_extraction(EXTRACTION, pages, set())
    assert out["allowed_countries"] == ["SE"]
    assert out["term_start_date"] == "2026-01-01"
    quotes = [e["quote"] for e in out["field_evidence"]["minimum_order_quantities"]]
    assert quotes == ["minimum value of USD 500"]


def test_read_and_file_an_agreement(db):
    readers = _Readers([dict(EXTRACTION)])
    rec = read_and_ingest(
        db, readers, readers, brand_id="brand-1", partnership_id="ps-1",
        file_name="agreement.txt", data=AGREEMENT.encode(), title="Nordic 2026",
    )
    assert rec.contract.title == "Nordic 2026" and rec.terms.discount_rules[0].tiers[0].discount_percent == 3
    assert len(rec.terms.evidence) == 1
    stored = db.active_contract("ps-1")
    assert stored and stored.id == rec.contract.id and stored.file_name == "agreement.txt"
    assert readers.calls[0]["model"] == "deepseek-flash"
    content = readers.calls[0]["input"]
    assert "Section path: preamble" in content or "Section id: c1" in content
    assert db.contract_chunks(rec.contract.id)


_EMPTY = {
    "commercial_discount_rules": [],
    "minimum_order_quantities": [],
    "product_price_list": [],
    "allowed_countries": [],
    "excluded_countries": [],
    "field_evidence": {},
}


def test_the_model_picks_chunks_from_the_full_outline():
    text = "第一条 定义\n术语以本合同为准。\n第二条 订货\n最低订单金额为 USD 500。\n"
    readers = _Readers([{"selected_ids": ["c2"]}, dict(EXTRACTION), {"selected_ids": []}])
    read_agreement(readers, readers, "agreement.txt", text.encode())
    select, extract, reflect = readers.calls
    assert select["model"] == "deepseek-chat"
    outline = _user(select)
    assert "第一条" in outline and "定义" in outline and "第二条" in outline and "订货" in outline
    assert "术语以本合同为准" not in outline
    body = extract["input"]
    assert "USD 500" in body and "术语以本合同为准" not in body
    retry = _user(reflect)
    assert "missing_fields" in retry and "定义" in retry and "订货" not in retry


def test_high_confidence_skips_another_pass():
    text = "第一条 定义\n术语以本合同为准。\n第二条 订货\n最低订单金额为 USD 500。\n"
    high = {**EXTRACTION, "confidence_score": 0.9}
    readers = _Readers([{"selected_ids": ["c2"]}, high, {"selected_ids": ["c1"]}])
    extraction, _tree = read_agreement(readers, readers, "agreement.txt", text.encode())
    assert extraction["confidence_score"] == 0.9
    assert len(readers.calls) == 2
    assert readers.replies == [{"selected_ids": ["c1"]}]


def test_a_blank_field_is_read_from_an_unread_chunk():
    text = "第一条 定义\n术语以本合同为准。\n第二条 订货\n最低订单金额为 USD 500。\n"
    readers = _Readers([{"selected_ids": ["c1"]}, dict(_EMPTY), {"selected_ids": ["c2"]}, dict(EXTRACTION)])
    extraction, _tree = read_agreement(readers, readers, "agreement.txt", text.encode())
    assert extraction["minimum_order_quantities"][0]["quantity"] == 500
    assert extraction["territory_text"] == "Sweden."
    reflect = _user(readers.calls[2])
    assert "minimum_order_quantities" in reflect and "订货" in reflect and "术语以本合同为准" not in reflect
    assert len(readers.calls) == 4


def test_reflection_stops_after_three_passes():
    text = "第一条 定义\n甲。\n第二条 期限\n自 2026-01-01 起。\n第三条 区域\n瑞典。\n第四条 订货\n最低订单金额为 USD 500。\n"
    readers = _Readers([
        {"selected_ids": ["c1"]}, dict(_EMPTY),
        {"selected_ids": ["c2"]}, dict(_EMPTY),
        {"selected_ids": ["c3"]}, dict(_EMPTY),
        {"selected_ids": ["c4"]}, dict(EXTRACTION),
    ])
    with pytest.raises(ValueError, match="No enforceable terms"):
        read_agreement(readers, readers, "agreement.txt", text.encode())
    extracts = [call for call in readers.calls if call["kind"] == "extract"]
    assert len(readers.calls) == 6 and len(extracts) == 3
    assert all("最低订单金额" not in call["input"] for call in extracts)
    assert len(readers.replies) == 2


def test_outline_shows_a_heading_sentence_when_there_is_no_short_title():
    text = '1.1 "Container" means one shipping container.\n第二条 订货\n最低订单金额为 USD 500。\n'
    readers = _Readers([{"selected_ids": ["c2"]}, dict(EXTRACTION), {"selected_ids": []}])
    read_agreement(readers, readers, "agreement.txt", text.encode())
    outline = _user(readers.calls[0])
    assert "Container" in outline and "订货" in outline and "USD 500" not in outline


def test_a_file_with_no_terms_is_not_written(db):
    readers = _Readers([{
        "commercial_discount_rules": [], "minimum_order_quantities": [],
        "allowed_countries": [], "excluded_countries": [], "field_evidence": {},
    }])
    with pytest.raises(ValueError, match="No enforceable terms"):
        read_agreement(readers, readers, "empty.txt", b"Hello, this message has no commercial terms at all, just a greeting.")
    assert db.active_contract("ps-1") is None


def test_chat_holds_the_agreement_until_confirm(db, monkeypatch):
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    named = FakeLLM(
        json_replies=[{"tags": ["contract"], "title": "agreement"}],
        text_reply="Please attach the agreement file.",
    )
    ev = list(service.handle_message(db, named, sid, "see the attached agreement", attachments=["agreement.pdf"]))
    assert work_chat(db, named, ev[0]["run_id"]) is True
    assert "attach" in db.session(sid)["history"][-1]["content"].lower()
    assert db.active_contract("ps-1") is None
    assert not any((c.get("schema") or {}).get("name") == "contract_extraction" for c in named.calls)

    readers = _Readers([dict(EXTRACTION)])
    monkeypatch.setattr("backend.contract.turn.deepseek_client", lambda: readers)
    monkeypatch.setattr("backend.contract.turn.campus_client", lambda: readers)
    llm = FakeLLM(
        json_replies=[{"tasks": [{"id": "c", "kind": "agent", "name": "contract", "input": {}, "depends_on": []}]}],
        text_reply="Filed the agreement.",
    )
    ev = list(service.handle_message(db, llm, sid, "please file this", documents=[("agreement.txt", AGREEMENT.encode())]))
    assert work_chat(db, llm, ev[0]["run_id"]) is True
    saved = db.session(sid)["history"][-1]
    assert saved["content"] == "Filed the agreement."
    assert "contract_id" not in saved
    table = saved["contract"]
    assert table["pending_id"] and table["confirmed"] is False
    assert table["file_name"] == "agreement.txt" and table["term_start_date"] == "2026-01-01"
    assert table["discount_rules"][0]["tiers"][0]["discount_percent"] == 3
    assert db.active_contract("ps-1") is None
    assert saved["contract_pending"]["extraction"]["term_start_date"] == "2026-01-01"
    assert "activity" not in saved and "model_json" not in saved

    filing = dict(table)
    filing["title"] = "Nordic edited"
    filing["discount_rules"] = [{
        **filing["discount_rules"][0],
        "tiers": [{**filing["discount_rules"][0]["tiers"][0], "discount_percent": 8}],
    }]
    filed = service.confirm_contract(db, sid, table["pending_id"], filing)
    stored = db.active_contract("ps-1")
    assert stored and stored.id == filed["contract_id"] and stored.title == "Nordic edited"
    assert stored.term_start_date == "2026-01-01"
    terms = db.contract_terms(stored.id)
    assert terms.discount_rules[0].tiers[0].discount_percent == 8
    assert db.contract_chunks(stored.id)
    again = service.confirm_contract(db, sid, table["pending_id"], filing)
    assert again["contract_id"] == stored.id
    assert len(db.contracts("ps-1")) == 1


def test_contract_read_hands_the_run_to_ocr(db, monkeypatch):
    seen: dict = {}

    def fake_pages(file_name, data, *, db=None, run_id=None):
        seen["file_name"] = file_name
        seen["run_id"] = run_id
        seen["db"] = db
        raise ValueError("stop after the handoff")

    monkeypatch.setattr("backend.contract.read.load_pages", fake_pages)
    monkeypatch.setattr("backend.contract.turn.deepseek_client", lambda: object())
    monkeypatch.setattr("backend.contract.turn.campus_client", lambda: object())
    from backend.services.session_tools import _contract_read

    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "ocr-handoff", {"message": "file"})
    filed = _contract_read(db, {"id": "s", "partnership_id": "ps-1"}, [("agreement.pdf", b"%PDF")])(
        {}, context={"run_id": run["id"]},
    )
    assert filed["status"] == "failed"
    assert seen["file_name"] == "agreement.pdf"
    assert seen["run_id"] == run["id"]
    assert seen["db"] is db
