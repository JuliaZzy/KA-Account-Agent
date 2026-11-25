"""Contract extraction → database → PO gate. Everything the reader found is stored (raw +
normalized), the derived rules trace back to the contract, and the discount schedule is
what pre-fills the next order."""

from __future__ import annotations

from datetime import date

from backend.contract import contract_discount_for_next_order, ingest_extraction, record_from_extraction
from backend.llm import FakeLLM
from backend.po.catalog import Catalog
from backend.po.check import run_code_check
from backend.po.slots import Discount, LineItemSlot, PoSlots
from backend.po.submit import create_and_submit_po
from backend.po.turn import load_bundle
from tests.support.po_draft import draft_order

EXTRACTION = {
    "contract_type": "distribution_agreement",
    "effective_date": "2026-03-15",
    "term_end_date": "2028-03-14",
    "exclusivity_type": "exclusive",
    "allowed_countries": ["US", "CA"],
    "excluded_countries": ["kp", "IR"],
    "minimum_order_quantities": [
        {"scope": "per_order", "quantity": 2000, "currency": "USD"},
        {"sku": "FO-100", "quantity": 240, "unit": "units"},
        {"scope": "per_shipment", "quantity": 1, "unit": "container"},  # ambiguous → skipped
    ],
    "commercial_discount_rules": [{
        "title": "Container sequence discount", "section_reference": "5.01(b)",
        "applies_per": "each_contract_year", "basis": "shipping_container_sequence",
        "bands": [{"from_container": 1, "to_container": 2, "discount_pct": 5}, {"from_container": 3, "discount_pct": 8}],
        "source_clause_text": "1st–2nd containers 5%, 3rd and further 8%.",
    }],
    "product_price_list": [{"product_name": "Deep Sea Fish Oil", "product_sku": "FO-100", "unit_price": 8.5, "currency": "USD", "unit": "per piece"}],
    "field_evidence": {"commercial_discount_rules": [{"page": 7, "quote": "3rd and further 8%", "section_hint": "5.01(b)"}]},
    "confidence_score": 0.85,
}


def test_mapping_produces_rules_moqs_and_reports_ambiguity():
    rec = record_from_extraction(EXTRACTION, brand_id="brand-1", partnership_id="ps-1", title="agreement")
    by_type = {x.rule_type: x for x in rec.derived_rules}
    assert by_type["min_order_value"].rule_config == {"min": 2000.0} and by_type["min_order_value"].severity == "block"
    assert by_type["territory_restriction"].rule_config == {"territories": ["KP", "IR"]}
    assert by_type["discount_approval"].rule_config["threshold_pct"] == 8.01 and by_type["discount_approval"].severity == "approval_required"
    assert all(x.partner_scope == "specific" and x.partner_ids == ["ps-1"] and "(from agreement)" in x.name for x in rec.derived_rules)
    assert rec.product_moq_units == {"FO-100": 240}
    assert len(rec.skipped) == 1 and "ambiguous" in rec.skipped[0]
    # The schedule itself survives — it is not collapsed into the approval threshold.
    (rule,) = rec.terms.discount_rules
    assert [(t.from_container, t.to_container, t.discount_percent) for t in rule.tiers] == [(1, 2, 5.0), (3, None, 8.0)]
    assert rec.contract.effective_from == "2026-03-15" and rec.contract.exclusivity_type == "exclusive"


def test_everything_is_stored_raw_and_normalized(db):
    rec = ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1", title="agreement", file_name="a.pdf", model="test-model")
    c = db.active_contract("ps-1")
    assert c and c.id == rec.contract.id and c.title == "agreement" and c.status == "active"
    (ex,) = db.contract_extractions(c.id)
    assert ex["raw"] == EXTRACTION and ex["model"] == "test-model" and ex["confidence_score"] == 0.85 and len(ex["skipped"]) == 1
    terms = db.contract_terms(c.id)
    assert [t.discount_percent for t in terms.discount_rules[0].tiers] == [5.0, 8.0]
    assert {(t.country_code, t.kind) for t in terms.territories} == {("US", "allowed"), ("CA", "allowed"), ("KP", "excluded"), ("IR", "excluded")}
    assert terms.price_list[0].product_sku == "FO-100" and terms.price_list[0].unit_price == 8.5
    assert terms.evidence[0].field == "commercial_discount_rules" and terms.evidence[0].page == 7
    assert len(terms.moqs) == 3
    # Derived rules point back at the contract they came from.
    rules = db.rules("brand-1")
    assert {r.rule_type for r in rules} == {"min_order_value", "territory_restriction", "discount_approval"}


def test_reingesting_replaces_terms_and_supersedes_the_old_contract(db):
    first = ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1", title="v1")
    second = ingest_extraction(db, {**EXTRACTION, "excluded_countries": []}, brand_id="brand-1", partnership_id="ps-1", title="v2")
    assert db.contract(first.contract.id).status == "superseded" and db.active_contract("ps-1").id == second.contract.id
    # Old contract's derived rules and the new one's coexist by contract; the gate reads all brand rules.
    assert len([r for r in db.rules("brand-1") if r.rule_type == "territory_restriction"]) == 1
    # Re-ingesting the SAME contract id replaces its terms and rules rather than accumulating them.
    ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1", contract_id=second.contract.id, title="v2 again")
    assert len(db.contract_terms(second.contract.id).discount_rules) == 1
    assert len([r for r in db.rules("brand-1") if r.rule_type == "min_order_value"]) == 2  # one per contract


def test_contract_terms_gate_the_next_order(db):
    ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1")
    catalog, rules = Catalog(db.products("brand-1")), db.rules("brand-1")
    # 10 cases × $100 = $1000 < $2000 contract minimum, and 10 cases < 240/12 = 20 MOQ.
    slots = PoSlots(line_items=[LineItemSlot(reference="fish oil", cases=10, sku=None)], discount=Discount(kind="none"))
    c = run_code_check(slots=slots, catalog=catalog, rules=rules, partnership_id="ps-1", ship_to_country="US")
    assert c.ready is False and c.lines[0].moq_cases == 20 and any(g.kind == "below_moq" for g in c.gaps)
    slots = PoSlots(line_items=[LineItemSlot(reference="fish oil", cases=20, sku=None)], discount=Discount(kind="none"))
    c = run_code_check(slots=slots, catalog=catalog, rules=rules, partnership_id="ps-1", ship_to_country="US")
    assert c.ready is True  # 20 × 100 = 2000 meets the minimum
    c = run_code_check(slots=slots, catalog=catalog, rules=rules, partnership_id="ps-1", ship_to_country="IR")
    assert c.ready is False and c.blocking[0].rule_type == "territory_restriction"


def _submit(db, cases: int = 20) -> None:
    bundle = load_bundle(db, "ps-1")
    slots = PoSlots(line_items=[LineItemSlot(reference="FO-100", cases=cases)], discount=Discount(kind="none"))
    check = run_code_check(slots=slots, catalog=bundle.catalog, rules=bundle.rules, partnership_id="ps-1", ship_to_country="US")
    assert check.ready
    create_and_submit_po(db, partnership=bundle.partnership, settings=bundle.settings, slots=slots, check=check)


def test_discount_schedule_picks_the_tier_by_container_sequence(db):
    ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1")
    today = date(2026, 9, 20)
    d = contract_discount_for_next_order(db, "ps-1", today)
    assert d and d.percent == 5.0 and d.container_index == 1 and d.period_start == date(2026, 3, 15)
    assert d.note.startswith("5% off — 1st container")
    _submit(db)
    _submit(db)
    d = contract_discount_for_next_order(db, "ps-1", today)
    assert d and d.percent == 8.0 and d.container_index == 3  # 3rd container this contract year
    # Before the anniversary the count resets to the previous contract year.
    assert contract_discount_for_next_order(db, "ps-1", date(2027, 3, 14)).container_index == 3
    assert contract_discount_for_next_order(db, "ps-1", date(2027, 3, 15)).container_index == 1


def test_no_contract_or_no_sequence_schedule_means_nothing_is_prefilled(db):
    assert contract_discount_for_next_order(db, "ps-1") is None
    volume = {**EXTRACTION, "commercial_discount_rules": [{"basis": "purchase_volume", "tiers": [{"discount_percent": 10}]}]}
    ingest_extraction(db, volume, brand_id="brand-1", partnership_id="ps-1")
    assert contract_discount_for_next_order(db, "ps-1") is None


EXTRACT_NO_DISCOUNT_MENTIONED = {
    "line_items": [{"reference": "fish oil", "cases": 20, "sku": None}], "discount_kind": None, "reply_language": "English",
}


def test_contract_rate_is_known_on_the_first_order_turn_before_any_product(db):
    ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1")
    llm = FakeLLM(json_replies=[{
        "line_items": [], "discount_kind": None, "reply_language": "English",
    }], text_reply="ok")
    s = db.create_session("s1", "ps-1")
    done = draft_order(db, llm, load_bundle(db, "ps-1"), s, "I want to place an order")[-1]
    assert done["slots"]["line_items"] == []
    assert done["slots"]["discount"]["source"] == "contract" and done["slots"]["discount"]["value"] == 5.0
    extract = llm.calls[0]["system"]
    assert "1st container" in extract and "discount_kind 保持 null" in extract


def test_turn_prefills_the_contract_rate_and_reply_is_told(db):
    ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1")
    llm = FakeLLM(json_replies=[dict(EXTRACT_NO_DISCOUNT_MENTIONED)], text_reply="ok")
    s = db.create_session("s1", "ps-1")
    done = draft_order(db, llm, load_bundle(db, "ps-1"), s, "20 cases of fish oil")[-1]
    assert done["slots"]["discount"] == {"kind": "percent", "value": 5.0, "source": "contract", "note": done["slots"]["discount"]["note"]}
    assert done["draft"]["discount_amount"] == 100.0 and done["draft"]["total"] == 1900.0
    assert not any("discount" in g for g in done["gaps"]) and done["stage"] == "confirm"
    assert "1st container" in llm.calls[0]["system"]


def test_customer_statement_overrides_the_prefill_and_over_contract_needs_approval(db):
    ingest_extraction(db, EXTRACTION, brand_id="brand-1", partnership_id="ps-1")
    llm = FakeLLM(json_replies=[
        dict(EXTRACT_NO_DISCOUNT_MENTIONED),
        {**EXTRACT_NO_DISCOUNT_MENTIONED, "discount_kind": "percent", "discount_value": 12},
    ], text_reply="ok")
    s = db.create_session("s1", "ps-1")
    bundle = load_bundle(db, "ps-1")
    draft_order(db, llm, bundle, s, "20 cases of fish oil")
    done = draft_order(db, llm, bundle, db.session("s1"), "make it 12%")[-1]
    assert done["slots"]["discount"]["source"] == "customer" and done["slots"]["discount"]["value"] == 12
    assert any("exceeds the 8.01% threshold" in a for a in done["advisories"])
