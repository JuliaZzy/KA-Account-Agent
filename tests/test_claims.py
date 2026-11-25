from __future__ import annotations

import asyncio

from backend.llm import FakeLLM
from backend.po.claims import (
    handle_post_submit,
    inspection_ok_outcome,
    match_claim_lines,
    pick_claim_target,
    prepayment_due_now,
)
from backend.schemas import PurchaseOrder


def po(**over) -> PurchaseOrder:
    base = dict(id="po1", po_number="PO-1", brand_id="b", distributor_id="d", partnership_id="ps-1", status="shipped", currency="USD",
                payment_terms="50_50", incoterms="CIF", shipping_method="sea", eta_date=None, ship_to_country="US", notes=None,
                discount_amount=None, discount_percentage=None, subtotal_amount=1000.0, total_amount=1000.0, prepaid_amount=500.0)
    return PurchaseOrder(**{**base, **over})


# --- pure logic ------------------------------------------------------------------

def test_inspection_outcomes():
    assert inspection_ok_outcome(payment_terms="100_prepaid", total=1000, prepaid=1000, balance_paid=0).status == "completed"
    r = inspection_ok_outcome(payment_terms="50_50", total=1000, prepaid=500, balance_paid=0)
    assert (r.status, r.remaining, r.payment_stage) == ("awaiting_payment", 500.0, "final_payment")
    assert inspection_ok_outcome(payment_terms="net_30", total=1000, prepaid=0, balance_paid=0).payment_due_days == 30
    assert inspection_ok_outcome(payment_terms="net60", total=1000, prepaid=0, balance_paid=0).payment_due_days == 60
    r = inspection_ok_outcome(payment_terms="50_50", total=1000, prepaid=500, balance_paid=500, claim_credit=200)
    assert r.status == "awaiting_refund" and r.refund_amount == 200.0


def test_prepayment_due_now():
    assert prepayment_due_now("50_50", 1000) == 500 and prepayment_due_now("100_prepaid", 999.99) == 999.99 and prepayment_due_now("net_30", 1000) == 0


def test_pick_claim_target_never_guesses():
    a, b = po(id="a"), po(id="b")
    assert pick_claim_target([a, b], "b").id == "b"
    assert pick_claim_target([a], None).id == "a"
    assert pick_claim_target([a, b], None) is None
    assert pick_claim_target([], None) is None


def test_match_claim_lines():
    items = [{"sku": "FO-100", "product_name": "Deep Sea Fish Oil", "case_price": 100.0}, {"sku": "SR-200", "product_name": "Radiance Serum", "case_price": 50.0}]
    out = match_claim_lines(items, [
        {"reference": "fo-100", "qty": "2.4", "issue_type": "damaged", "description": " crushed "},
        {"reference": "serum", "qty": 1},
        {"reference": "something", "qty": 1},   # ambiguous across 2 lines → dropped
        {"reference": "FO-100"},                 # no qty → dropped
        "junk",
    ])
    assert [(l["sku"], l["qty"], l["amount"]) for l in out] == [("FO-100", 2, 200.0), ("SR-200", 1, 50.0)]
    assert out[0]["description"] == "crushed"
    single = [{"sku": "FO-100", "product_name": "Deep Sea Fish Oil", "case_price": 100.0}]
    assert match_claim_lines(single, [{"reference": "the broken ones", "qty": 3}])[0]["sku"] == "FO-100"


# --- apply through the classifiers ------------------------------------------------

def _po_in_db(db):
    from backend.po.check import CheckResult, ResolvedLine
    from backend.po.slots import Discount, LineItemSlot, PoSlots
    from backend.po.submit import create_and_submit_po
    line = ResolvedLine(reference="fish oil", sku="FO-100", cases=10, product_id="p-fo", product_name="Deep Sea Fish Oil", case_pack=12, case_price=100.0, line_total=1000.0)
    check = CheckResult(lines=[line], subtotal=1000.0, gaps=[], blocking=[], advisory=[], rule_results=[], ready=True)
    res = create_and_submit_po(db, partnership=db.partnership("ps-1"), settings=db.settings("brand-1"),
                               slots=PoSlots(line_items=[LineItemSlot(reference="fish oil", cases=10, sku="FO-100")], discount=Discount(kind="none")), check=check)
    db.update_po(res.po_id, status="shipped", prepaid_amount=500.0)
    return db.po(res.po_id)


def test_received_ok_marks_received_and_states_balance(db):
    p = _po_in_db(db)
    llm = FakeLLM(json_replies=[{"verdict": "received_ok", "confidence": 0.95}])
    out = asyncio.run(handle_post_submit(db, llm, p, "goods arrived, all good"))
    assert out.kind == "received" and "500.00 is still due" in out.message
    assert db.po(p.id).status == "awaiting_payment"


def test_low_confidence_receipt_is_left_for_a_human(db):
    p = _po_in_db(db)
    llm = FakeLLM(json_replies=[{"verdict": "received_ok", "confidence": 0.5}, {"paid": False, "confidence": 0.9}])
    out = asyncio.run(handle_post_submit(db, llm, p, "hmm arrived maybe"))
    assert out.kind == "none" and db.po(p.id).status == "shipped"


def test_payment_claim_records_without_verifying(db):
    p = _po_in_db(db)
    llm = FakeLLM(json_replies=[{"verdict": "none", "confidence": 0.9}, {"paid": True, "confidence": 0.9}])
    out = asyncio.run(handle_post_submit(db, llm, p, "wired the balance this morning"))
    assert out.kind == "payment" and db.po(p.id).status == "payment_claimed"


def test_damage_claim_opens_with_matched_lines(db):
    p = _po_in_db(db)
    llm = FakeLLM(json_replies=[{"verdict": "issues", "confidence": 0.9}, {"lines": [{"reference": "fish oil", "qty": 2, "issue_type": "damaged", "description": "crushed"}], "resolution": "credit", "confidence": 0.9}])
    out = asyncio.run(handle_post_submit(db, llm, p, "2 cases of the fish oil arrived crushed"))
    assert out.kind == "claim" and out.data["lines"][0]["amount"] == 200.0
    assert db.po(p.id).status == "claim_filed" and db.po_events(p.id)[-1]["kind"] == "claim_filed"
