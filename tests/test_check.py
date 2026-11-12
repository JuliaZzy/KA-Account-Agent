"""The deterministic gate. `ready` is the only thing standing between an LLM turn and a
real purchase order, so the cases that matter most are the ones where it must be False.
Ported from the first version's poAgent.codeCheck.test.ts (docs/porting/01-test-spec.md)."""

from __future__ import annotations

import pytest

from backend.po.check import MAX_CASES_PER_LINE, run_code_check
from backend.po.slots import Discount, LineItemSlot, PoSlots

NO_DISCOUNT = Discount(kind="none")


def slots(*lines: tuple[str, int, str | None], discount: Discount | None = NO_DISCOUNT) -> PoSlots:
    return PoSlots(line_items=[LineItemSlot(reference=r, cases=c, sku=s) for r, c, s in lines], discount=discount)


def check(catalog, s: PoSlots, resolve=None, rules=(), user_text: str = ""):
    return run_code_check(
        slots=s, catalog=catalog, rules=list(rules), partnership_id="ps-1", ship_to_country="US",
        resolve=resolve, user_text=user_text,
    )


def kinds(r) -> list[str]:
    return [g.kind for g in r.gaps]


# --- the gate must BLOCK -------------------------------------------------------

def test_no_items_is_never_ready(catalog):
    r = check(catalog, slots())
    assert r.ready is False and "no_items" in kinds(r)


def test_unknown_product_blocks(catalog):
    r = check(catalog, slots(("unicorn tears", 10, None)))
    assert r.ready is False and "unresolved_reference" in kinds(r)
    assert r.lines[0].unresolved and r.lines[0].product_id is None


def test_ambiguous_product_blocks_and_offers_both(catalog):
    r = check(catalog, slots(("the serum", 10, None)))
    assert r.ready is False and "ambiguous_product" in kinds(r)
    assert len(r.lines[0].candidates) == 2 and r.lines[0].unresolved is False


def test_below_moq_blocks_and_rounds_up(catalog):
    r = check(catalog, slots(("FO-100", 8, None)))
    assert r.ready is False and "below_moq" in kinds(r)
    assert r.lines[0].moq_cases == 9  # 100 / 12 → 9


def test_missing_price_blocks_on_a_regular_product(catalog):
    r = check(catalog, slots(("NP-300", 10, None)))
    assert r.ready is False and "missing_price" in kinds(r)


def test_an_unpriced_gift_is_not_a_gap(catalog):
    """GU-020 is a gift with no case price — priced lines carry the order, the gift rides along."""
    r = check(catalog, slots(("FO-100", 10, None), ("GU-020", 1, None)))
    assert "missing_price" not in kinds(r) and r.ready is True
    assert r.lines[1].missing_price is False and r.lines[1].line_total is None
    assert r.subtotal == 1000.0  # the unpriced line adds nothing


def test_an_order_of_only_unpriced_gifts_has_no_total(catalog):
    r = check(catalog, slots(("GU-020", 5, None)))
    assert r.ready is False and kinds(r) == ["zero_total"]


def test_zero_quantity_blocks(catalog):
    r = check(catalog, slots(("FO-100", 0, None)))
    assert r.ready is False and "zero_total" in kinds(r)


def test_unaddressed_discount_blocks_a_clean_order(catalog):
    r = check(catalog, slots(("FO-100", 10, None), discount=None))
    assert r.ready is False and "discount_unaddressed" in kinds(r)


def test_one_bad_line_poisons_the_order(catalog):
    r = check(catalog, slots(("FO-100", 10, None), ("unicorn tears", 1, None)))
    assert r.ready is False


def test_implausible_quantity_is_questioned_not_priced(catalog):
    r = check(catalog, slots(("FO-100", 999_999_999, None)))
    assert r.ready is False and "implausible_quantity" in kinds(r)
    assert check(catalog, slots(("FO-100", MAX_CASES_PER_LINE, None))).ready is True


# --- the gate must PASS --------------------------------------------------------

def test_clean_order_is_ready(catalog):
    r = check(catalog, slots(("fish oil", 10, None)))
    assert r.ready is True and r.lines[0].sku == "FO-100" and r.subtotal == 1000 and r.gaps == []


def test_partial_layer_is_a_nudge_not_a_block(catalog):
    r = check(catalog, slots(("FO-100", 12, None)))
    assert r.ready is True and r.lines[0].partial_layer is True and r.lines[0].cases_to_full_layer == 3


# --- exact SKU / name handling -------------------------------------------------

def test_exact_sku_is_case_insensitive(catalog):
    assert check(catalog, slots(("fo-100", 10, None))).lines[0].product_id == "p-fo"


def test_persisted_sku_skips_search(catalog):
    assert check(catalog, slots(("whatever the user called it", 10, "FO-100"))).lines[0].product_id == "p-fo"


def test_exact_name_beats_its_longer_sibling(catalog):
    assert check(catalog, slots(("Radiance Serum", 10, None))).lines[0].sku == "SR-200"
    assert check(catalog, slots(("Radiance Serum Plus", 10, None))).lines[0].sku == "SR-201"


def test_a_sku_the_customer_typed_settles_a_tie_in_the_words(catalog):
    # "the serum" ties SR-200 and SR-201. The customer also wrote SR-200 themselves.
    r = check(catalog, slots(("the serum", 10, "SR-200")), user_text="Order the serum (SR-200), 10 cases")
    assert r.lines[0].sku == "SR-200" and not r.lines[0].ambiguous and r.lines[0].line_total == 500


def test_a_sku_only_the_model_supplied_still_cannot_settle_the_tie(catalog):
    r = check(catalog, slots(("the serum", 10, "SR-200")), user_text="Order the serum, 10 cases")
    assert r.lines[0].ambiguous


def test_a_typed_sku_must_be_a_whole_token(catalog):
    # SR-2000 is not SR-200: a prefix match would silently pick a different product.
    r = check(catalog, slots(("the serum", 10, "SR-200")), user_text="the serum SR-2000")
    assert r.lines[0].ambiguous


def test_guessed_sku_on_a_vague_reference_stays_ambiguous(catalog):
    # Measured: asked for "radiance serum", gpt-4.1 volunteered SR-200 and llama3.2:3b
    # volunteered SR-201 — same words, different price, user never asked.
    r = check(catalog, slots(("the serum", 10, "SR-200")))
    assert r.ready is False and r.lines[0].ambiguous and len(r.lines[0].candidates) == 2


def test_customer_words_beat_a_conflicting_model_sku(catalog):
    assert check(catalog, slots(("Radiance Serum", 10, "SR-201"))).lines[0].sku == "SR-200"
    assert check(catalog, slots(("FO-100", 10, "SR-200"))).lines[0].product_id == "p-fo"


def test_settled_line_is_not_relitigated(catalog):
    r = check(catalog, slots(("fish oil", 10, "FO-100")))
    assert r.lines[0].product_id == "p-fo" and r.ready is True


# --- LLM fallback resolver: the catalog is the authority -----------------------

def test_hallucinated_sku_is_dropped(catalog):
    r = check(catalog, slots(("unicorn tears", 10, None)), resolve=lambda refs: {"unicorn tears": ["SKU-THAT-DOES-NOT-EXIST"]})
    assert r.ready is False and r.lines[0].unresolved


def test_one_valid_sku_rescues_the_line(catalog):
    r = check(catalog, slots(("the omega three stuff", 10, None)), resolve=lambda refs: {"the omega three stuff": ["FO-100"]})
    assert r.ready is True and r.lines[0].sku == "FO-100"


def test_two_candidates_ask_rather_than_guess(catalog):
    r = check(catalog, slots(("the skin thing", 10, None)), resolve=lambda refs: {"the skin thing": ["SR-200", "SR-201"]})
    assert r.ready is False and r.lines[0].ambiguous


def test_resolver_outage_never_auto_readies(catalog):
    def boom(refs):
        raise RuntimeError("model unavailable")
    r = check(catalog, slots(("unicorn tears", 10, None)), resolve=boom)
    assert r.ready is False and r.lines[0].unresolved


def test_resolver_is_called_once_with_only_unresolved_refs(catalog):
    calls = []
    def rec(refs):
        calls.append(refs)
        return {}
    check(catalog, slots(("fish oil", 10, None), ("unicorn tears", 1, None), ("moon dust", 2, None)), resolve=rec)
    assert calls == [["unicorn tears", "moon dust"]] or sorted(calls[0]) == ["moon dust", "unicorn tears"]


# --- rules only run on clean lines --------------------------------------------

def test_rules_are_not_evaluated_on_a_dirty_order(catalog):
    from tests.conftest import rule
    r = check(catalog, slots(("unicorn tears", 10, None)), rules=[rule(rule_config={"min": 99999})])
    assert r.rule_results == [] and r.blocking == []


def test_blocking_rule_stops_a_clean_order(catalog):
    from tests.conftest import rule
    r = check(catalog, slots(("fish oil", 10, None)), rules=[rule(rule_config={"min": 5000})])
    assert r.ready is False and r.gaps == [] and len(r.blocking) == 1


def test_advisory_rule_does_not_stop_the_order(catalog):
    from tests.conftest import rule
    r = check(catalog, slots(("fish oil", 10, None), discount=Discount(kind="percent", value=20)),
              rules=[rule(rule_type="discount_approval", rule_config={"threshold_pct": 15}, severity="approval_required")])
    assert r.ready is True and len(r.advisory) == 1


@pytest.mark.parametrize("ref", ["鱼油", "omega-3"])
def test_cjk_and_substring_references_resolve(catalog, ref):
    assert check(catalog, slots((ref, 10, None))).lines[0].sku == "FO-100"
