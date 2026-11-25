"""The confirm gate: token burned before create, hash re-checked at click time, and a
double-click producing exactly one order."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from backend.llm import FakeLLM
from backend.po.turn import confirm_order, load_bundle
from backend.schemas import CommercialRule
from tests.support.po_draft import draft_order

EXTRACT_OK = {
    "line_items": [{"reference": "fish oil", "cases": 10, "sku": None}],
    "discount_kind": "none", "reply_language": "English",
}


def drive_to_confirm(db, llm=None):
    llm = llm or FakeLLM(json_replies=[dict(EXTRACT_OK)], text_reply="Looks good — press Confirm.")
    s = db.create_session("s1", "ps-1")
    bundle = load_bundle(db, "ps-1")
    events = draft_order(db, llm, bundle, s, "10 cases of fish oil, no discount")
    done = events[-1]
    assert done["type"] == "done" and done["stage"] == "confirm" and done["confirm_token"]
    return bundle, done


def test_turn_reaches_confirm(db):
    bundle, done = drive_to_confirm(db)
    assert done["draft"]["total"] == 1000 and done["gaps"] == []
    s = db.session("s1")
    assert s["stage"] == "confirm" and s["confirm_hash"] and s["slots"]["line_items"]


def test_confirm_creates_one_order_and_burns_the_token(db):
    bundle, done = drive_to_confirm(db)
    r = confirm_order(db, bundle, db.session("s1"), done["confirm_token"])
    assert r["state"] == "submitted" and r["po_number"].startswith("PO-")
    po = db.po(r["po_id"])
    assert po and po.status == "submitted" and po.total_amount == 1000
    assert [i["sku"] for i in db.po_items(po.id)] == ["FO-100"]
    # Second click on the same token: "done", not a second order.
    r2 = confirm_order(db, bundle, db.session("s1"), done["confirm_token"])
    assert r2["state"] == "done" and len(db.list_pos()) == 1


def test_double_click_races_produce_exactly_one_order(db):
    bundle, done = drive_to_confirm(db)
    session = db.session("s1")
    results = []

    def click():
        results.append(confirm_order(db, bundle, session, done["confirm_token"])["state"])

    threads = [threading.Thread(target=click) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["submitted"] + ["used"] * 5
    assert len(db.list_pos()) == 1


def test_wrong_or_missing_token_is_refused(db):
    bundle, done = drive_to_confirm(db)
    assert confirm_order(db, bundle, db.session("s1"), "not-the-token")["state"] == "used"
    assert len(db.list_pos()) == 0


def test_expired_token_is_refused(db):
    bundle, done = drive_to_confirm(db)
    db.update_session("s1", confirm_expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert confirm_order(db, bundle, db.session("s1"), done["confirm_token"])["state"] == "expired"


def test_price_change_between_draft_and_click_is_refused(db):
    bundle, done = drive_to_confirm(db)
    # The world moved: the catalog price changed after the draft was shown.
    p = next(x for x in db.products("brand-1") if x.id == "p-fo")
    db.upsert_product("brand-1", p.model_copy(update={"case_price": 120}))
    bundle = load_bundle(db, "ps-1")
    r = confirm_order(db, bundle, db.session("s1"), done["confirm_token"])
    assert r["state"] == "changed" and r["draft"]["total"] == 1200
    assert len(db.list_pos()) == 0 and db.session("s1")["stage"] == "gathering"


def test_rule_added_between_draft_and_click_is_refused(db):
    bundle, done = drive_to_confirm(db)
    db.upsert_rule("brand-1", CommercialRule(id="r-min", name="min", rule_type="min_order_value", rule_config={"min": 5000}, severity="block"))
    r = confirm_order(db, load_bundle(db, "ps-1"), db.session("s1"), done["confirm_token"])
    assert r["state"] == "changed" and any("below" in g for g in r["gaps"])


def test_extraction_failure_keeps_previous_slots(db):
    llm = FakeLLM(json_replies=[dict(EXTRACT_OK)], text_reply="ok")
    s = db.create_session("s1", "ps-1")
    bundle = load_bundle(db, "ps-1")
    draft_order(db, llm, bundle, s, "10 cases of fish oil, no discount")
    llm.fail_json = True
    events = draft_order(db, llm, bundle, db.session("s1"), "and make it quick")
    assert any(event["type"] == "warning" for event in events)
    assert events[-1]["slots"]["line_items"][0]["sku"] == "FO-100"


