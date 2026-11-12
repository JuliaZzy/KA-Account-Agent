"""The archived pick/write/repair/reflect workflow, kept only for experiment comparisons."""

from __future__ import annotations

import json

from backend.schemas import PurchaseOrder
from backend.text2sql_legacy.text2sql import answer_question


def _po() -> PurchaseOrder:
    return PurchaseOrder(
        id="po-1", po_number="PO-1", brand_id="brand-1", distributor_id="dist-1", partnership_id="ps-1",
        status="submitted", currency="USD", payment_terms="net_30", incoterms="FOB", shipping_method="sea",
        eta_date=None, ship_to_country="US", notes=None, discount_amount=None, discount_percentage=None,
        subtotal_amount=1000, total_amount=1000,
    )


def _models(monkeypatch, replies: list[dict]) -> list[dict]:
    """Every role of the archived workflow goes through one scripted DeepSeek client."""
    pending = list(replies)

    class _DeepSeek:
        def response(self, **_kwargs):
            return type("Reply", (), {"output_text": json.dumps(pending.pop(0))})()

    monkeypatch.setattr("backend.text2sql_legacy.text2sql.deepseek_client", lambda: _DeepSeek())
    return pending


def _ask(db, **extra):
    return answer_question(
        db, question="how much did I order?",
        partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1", **extra,
    )


def test_legacy_picks_tables_writes_sql_and_scores(db, monkeypatch):
    db.insert_po(_po(), [], [])
    pending = _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90, "retry_question": ""},
    ])
    result = _ask(db)
    assert result.ok and result.score == 90 and result.rows[0]["total_amount"] == 1000
    assert result.columns == ["total_amount"] and not pending


def test_legacy_repairs_a_bad_column_once(db, monkeypatch):
    db.insert_po(_po(), [], [])
    pending = _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT no_such_column FROM purchase_orders"},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 95, "retry_question": ""},
    ])
    result = _ask(db)
    assert result.ok and result.rows[0]["total_amount"] == 1000 and not pending


def test_legacy_retries_a_low_score_with_a_new_question(db, monkeypatch):
    db.insert_po(_po(), [], [])
    pending = _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT notes FROM purchase_orders"},
        {"score": 40, "retry_question": "what is the order total"},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 91, "retry_question": ""},
    ])
    result = _ask(db)
    assert result.score == 91 and result.rows[0]["total_amount"] == 1000 and not pending


def test_legacy_stops_at_the_shared_model_call_budget(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [{"tables": ["purchase_orders"]}] + [{"sql": "SELECT nope FROM purchase_orders"}] * 20)
    result = _ask(db)
    assert not result.ok


def test_legacy_writer_and_reviser_see_the_required_columns(db, monkeypatch):
    """ReAct sees `required_columns` from its first turn, so the archived writer must too."""
    db.insert_po(_po(), [], [])
    seen: list[tuple[str, str]] = []
    statements = iter([
        "SELECT SUM(total_amount) AS total_purchase_amount FROM purchase_orders",
        "SELECT SUM(total_amount) AS total_amount FROM purchase_orders",
    ])

    def fake_ask(*, system, user, schema, trace=None, purpose="data.sql"):
        seen.append((purpose, user))
        return {"sql": next(statements)}

    monkeypatch.setattr("backend.text2sql_legacy.text2sql._pick_tables", lambda *_a, **_k: ["purchase_orders"])
    monkeypatch.setattr("backend.text2sql_legacy.text2sql._ask_ds", fake_ask)
    result = _ask(db, required_columns=["total_amount"], reflect=False)
    assert result.ok and not result.missing_columns
    assert [purpose for purpose, _ in seen] == ["data.sql", "data.sql.revise"]
    assert all("total_amount" in user for _, user in seen)
    assert "how much did I order?" in seen[1][1]
