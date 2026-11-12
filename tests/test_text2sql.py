"""Text-to-SQL: the statement is checked, then run only against this partnership's views."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from backend import service
from backend.app import create_app
from backend.db.database.text2sql import _inline_scope
from backend.db.orm import Base
from backend.llm import FakeLLM
from backend.llm.types import AssistantTurn, ToolCall
from backend.schemas import Partnership, Product, PurchaseOrder
from backend.services.chat_run import work_chat
from backend.text2sql import answer_question, run_statement
from backend.text2sql.analyze_badcase import analyze, classify_error
from backend.text2sql.sql_validator import system_row_cap, validate
from backend.text2sql.table_metadata import EXCLUDED_TABLES, HIDDEN_COLUMNS, TABLES, scope_views
from backend.text2sql.text2sql import _json_rows, _rows_for_prompt, prompt_view


def _models(monkeypatch, replies: list[dict]):
    """Script data-agent actions while preserving compact test fixtures."""
    pending = list(replies)
    school: list[dict] = []

    def next_action():
        raw = pending.pop(0)
        if "tables" in raw:
            return {"action": "inspect_schema", "tables": raw["tables"], "sql": "", "score": 0}
        if "sql" in raw:
            return {"action": "run_sql", "tables": [], "sql": raw["sql"], "score": 0}
        if "score" in raw:
            if raw["score"] < 80 and raw.get("retry_question") and pending:
                action = next_action()
                action["score"] = raw["score"]
                return action
            return {"action": "finish", "tables": [], "sql": "", "score": raw["score"]}
        return raw

    class _School:
        def chat_completion(self, model, messages, stream=False, max_tokens=None, temperature=None):
            school.append({"model": model, "messages": messages})
            raw = next_action()
            return {"choices": [{"message": {"content": json.dumps(raw)}}]}

    class _DeepSeek:
        def response(self, **_kwargs):
            raw = next_action()
            return type("Reply", (), {"output_text": json.dumps(raw)})()

    monkeypatch.setattr("backend.llm.client.qwen_client", lambda: _School())
    monkeypatch.setattr("backend.text2sql.text2sql.deepseek_client", lambda: _DeepSeek())
    return pending, school


def _po(**over) -> PurchaseOrder:
    base = dict(
        id="po-1", po_number="PO-1", brand_id="brand-1", distributor_id="dist-1", partnership_id="ps-1",
        status="submitted", currency="USD", payment_terms="net_30", incoterms="FOB", shipping_method="sea",
        eta_date=None, ship_to_country="US", notes=None, discount_amount=None, discount_percentage=None,
        subtotal_amount=1000, total_amount=1000,
    )
    return PurchaseOrder(**{**base, **over})


def test_metadata_matches_the_tables_and_hides_bytes_and_sessions():
    names = {table.name for table in Base.metadata.sorted_tables}
    assert set(TABLES) | EXCLUDED_TABLES == names
    assert "sessions" in EXCLUDED_TABLES and "text2sql_logs" in EXCLUDED_TABLES
    for name, meta in TABLES.items():
        columns = {column.name for column in Base.metadata.tables[name].columns}
        assert set(meta["columns"]) == columns - HIDDEN_COLUMNS.get(name, frozenset())
    views = dict(scope_views())
    assert "file_bytes" not in views["brand_documents"]
    assert "sessions" not in views


def test_returned_rows_keep_every_cell_and_the_prompt_copy_is_short():
    note = "甲" * 500
    raw = [
        {
            "note": note,
            "when": datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            "amount": Decimal("1.25"),
            "blob": "café".encode(),
            "n": index,
        }
        for index in range(60)
    ]
    kept = _json_rows(raw)
    assert len(kept) == 60
    assert kept[0]["note"] == note
    assert kept[0]["when"] == "2026-01-02T03:04:00+00:00"
    assert kept[0]["amount"] == 1.25
    assert kept[0]["blob"] == "café"
    prompt = _rows_for_prompt(kept)
    assert len(prompt) == 50
    assert prompt[0]["note"].endswith("…")
    assert len(prompt[0]["note"]) < len(note)


def test_validator_accepts_a_read_and_caps_the_limit():
    ok, err, sql = validate("SELECT term_end_date FROM contracts WHERE term_end_date >= '2026-01-01'")
    assert ok and err == "" and "term_end_date" in sql and "LIMIT 100" in sql.upper()

    ok, _, sql = validate("SELECT id FROM products LIMIT 99999")
    assert ok and "LIMIT 200" in sql.upper()

    ok, _, sql = validate("SELECT product_name_en FROM products WHERE product_name_en ILIKE '%drop table%'")
    assert ok and "drop table" in sql.lower()


@pytest.mark.parametrize("sql, message", [
    ("", "empty"),
    ("DELETE FROM purchase_orders", "only SELECT"),
    ("INSERT INTO purchase_orders (id) VALUES ('x')", "only SELECT"),
    ("UPDATE purchase_orders SET notes = 'x'", "only SELECT"),
    ("DROP TABLE purchase_orders", "only SELECT"),
    ("SELECT id FROM purchase_orders; DELETE FROM purchase_orders", "multiple"),
    ("SELECT pg_read_file('/etc/passwd')", "function"),
    ("SELECT id FROM public.purchase_orders", "schema-qualified"),
    ("SELECT set_config('search_path', 'public', true)", "function"),
])
def test_validator_rejects_writes_and_escapes(sql, message):
    ok, err, cleaned = validate(sql)
    assert not ok and cleaned == "" and message in err


def test_validator_allows_a_qualified_column_inside_and_and_or():
    joined = (
        "SELECT coalesce(sum(rf.numeric_value), 0) FROM report_facts rf "
        "JOIN sales_reports sr ON rf.report_id = sr.id "
        "WHERE rf.status = 'confirmed' AND rf.metric_key = 'units_sold' "
        "AND sr.status = 'confirmed' AND sr.reporting_period = ("
        "SELECT max(reporting_period) FROM sales_reports WHERE status = 'confirmed')"
    )
    ok, err, sql = validate(joined)
    assert ok and err == "" and "numeric_value" in sql.lower()

    either = (
        "SELECT id FROM report_facts rf JOIN sales_reports sr ON rf.report_id = sr.id "
        "WHERE rf.status = 'confirmed' OR sr.status = 'confirmed'"
    )
    ok, err, sql = validate(either)
    assert ok and err == "" and " or " in sql.lower()


def test_validator_accepts_a_bare_function_and_rejects_a_schema_prefix():
    ok, err, sql = validate("SELECT date_trunc('month', created_at) FROM purchase_orders")
    assert ok and err == "" and "date_trunc" in sql.lower()

    for qualified in (
        "SELECT pg_catalog.date_trunc('month', created_at) FROM purchase_orders",
        "SELECT public.max(total_amount) FROM purchase_orders",
        'SELECT "pg_catalog.date_trunc"(\'month\', created_at) FROM purchase_orders',
    ):
        ok, err, cleaned = validate(qualified)
        assert not ok and cleaned == "" and "schema-qualified" in err


@pytest.mark.parametrize("sql", [
    "SELECT pg_catalog.pg_sleep(1)",
    "SELECT public.pg_read_file('/etc/passwd')",
    'SELECT "pg_catalog.pg_sleep"(1)',
])
def test_validator_rejects_a_forbidden_function_with_a_schema(sql):
    ok, err, cleaned = validate(sql)
    assert not ok and cleaned == "" and "function" in err


def test_a_rejected_statement_is_kept_for_the_repair(db, monkeypatch):
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT pg_read_file('/etc/passwd')"},
        {"sql": "SELECT pg_sleep(1)"},
    ])
    result = answer_question(
        db,
        question="read a file", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok is False and result.error_type == "validation"
    logged = db.list_text2sql_logs("ps-1")[0]["generated_sql"]
    assert logged and "pg_sleep" in logged


def test_a_schema_qualified_function_is_rejected_then_repaired(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT pg_catalog.date_trunc('month', submitted_at) AS month FROM purchase_orders"},
        {"sql": "SELECT date_trunc('month', submitted_at) AS month FROM purchase_orders"},
    ])
    result = answer_question(
        db,
        question="orders by month", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok, result.error
    assert "pg_catalog" not in (result.sql or "").lower()
    assert "date_trunc" in (result.sql or "").lower()


def test_a_question_only_sees_this_partnership(db, monkeypatch):
    db.upsert_partnership(Partnership(
        id="ps-2", brand_id="brand-2", distributor_id="dist-2", brand_name="Other", distributor_name="Other",
        ship_to_country="DE", currency="EUR",
    ))
    db.upsert_product("brand-2", Product(id="p-secret", sku="XX-1", product_name_en="Secret Sauce"))
    db.insert_po(_po(), [], [])
    db.insert_po(_po(
        id="po-2", po_number="PO-2", brand_id="brand-2", distributor_id="dist-2", partnership_id="ps-2",
        total_amount=9999, subtotal_amount=9999, currency="EUR", ship_to_country="DE",
    ), [], [])

    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90},
    ])
    orders = answer_question(
        db,
        question="what did I order?", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert orders.ok
    assert {row["total_amount"] for row in orders.rows} == {1000}

    _models(monkeypatch, [
        {"tables": ["products"]},
        {"sql": "SELECT sku FROM products WHERE product_name_en ILIKE '%fish%'"},
    ])
    fish = answer_question(
        db,
        question="fish oil sku", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert fish.ok and [row["sku"] for row in fish.rows] == ["FO-100"]

    _models(monkeypatch, [
        {"tables": ["products"]},
        {"sql": "SELECT sku FROM products"},
    ])
    whole = answer_question(
        db,
        question="list skus", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert "XX-1" not in {row["sku"] for row in whole.rows}


def test_a_write_is_refused_and_the_order_stays(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "DELETE FROM purchase_orders"},
        {"sql": "DELETE FROM purchase_orders"},
    ])
    result = answer_question(
        db,
        question="delete my orders", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
        session_id=db.create_session("sess-1", "ps-1")["id"],
    )
    assert result.ok is False and result.error_type == "validation"
    assert db.po("po-1") is not None
    assert db.list_text2sql_logs("ps-1")[0]["execution_success"] is False


def test_a_bad_column_is_repaired_once(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT missing_column FROM purchase_orders"},
        {"sql": "SELECT total_amount FROM purchase_orders"},
    ])
    result = answer_question(
        db,
        question="order total", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok and result.rows == [{"total_amount": 1000}]
    assert "missing_column" in result.sql or "total_amount" in (result.sql or "")


def test_react_observes_failure_and_requeries_for_original_question(db, monkeypatch):
    db.insert_po(_po(), [], [])
    actions = iter([
        {"action": "inspect_schema", "tables": ["purchase_orders"], "score": 0},
        {"action": "run_sql", "sql": "SELECT missing_column FROM purchase_orders", "score": 0},
        {"action": "run_sql", "sql": "SELECT notes FROM purchase_orders", "score": 0},
        {"action": "run_sql", "sql": "SELECT total_amount FROM purchase_orders", "score": 20},
        {"action": "finish", "score": 95},
    ])
    seen = []

    def decide(*, question, observations, **_kwargs):
        seen.append((question, list(observations)))
        return next(actions)

    monkeypatch.setattr("backend.text2sql.text2sql._next_action", decide)
    result = answer_question(
        db, question="what is the order total?", partnership_id="ps-1",
        brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok and result.rows == [{"total_amount": 1000}] and result.score == 95
    assert all(question == "what is the order total?" for question, _ in seen)
    assert "missing_column" in (seen[2][1][-1]["error"] or "")
    assert seen[3][1][-1]["rows"] == [{"notes": None}]
    assert len(db.list_text2sql_logs("ps-1")) == 3


def test_react_caps_sql_attempts(db, monkeypatch):
    actions = iter([
        {"action": "inspect_schema", "tables": ["purchase_orders"], "score": 0},
        *({"action": "run_sql", "sql": "DELETE FROM purchase_orders", "score": 0} for _ in range(4)),
    ])
    monkeypatch.setattr("backend.text2sql.text2sql._next_action", lambda **_kwargs: next(actions))
    result = answer_question(
        db, question="delete orders", partnership_id="ps-1", brand_id="brand-1",
        distributor_id="dist-1",
    )
    assert not result.ok and result.error_type == "validation" and result.handoff
    assert len(db.list_text2sql_logs("ps-1")) == 3


def test_chat_quotes_the_rows_and_a_report_uses_them(db, monkeypatch):
    db.insert_po(_po(), [], [])
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-data", "ps-1", "u-a")["id"]
    db.update_session(sid, title="orders", tags=["data"])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
    ])
    llm = FakeLLM(
        json_replies=[{"tasks": [{"id": "d", "kind": "agent", "name": "data", "input": {"question": "order total"}, "depends_on": []}]}],
        text_reply="The order total is 1000.",
    )
    events = list(service.handle_message(db, llm, sid, "how much have I ordered?"))
    assert work_chat(db, llm, events[0]["run_id"]) is True
    saved = db.session(sid)["history"][-1]
    assert saved["content"] == "The order total is 1000."
    assert saved["data_query"]["log_id"]

    sid = db.create_session("sess-report", "ps-1", "u-a")["id"]
    db.update_session(sid, title="report", tags=["report"])
    report_llm = FakeLLM(json_replies=[
        {"tasks": [
            {"id": "d", "kind": "agent", "name": "data", "input": {"question": "order total"}, "depends_on": []},
            {"id": "r", "kind": "agent", "name": "report", "input": {}, "depends_on": ["d"]},
        ]},
        {"title": "订单月报", "sections": [
            {"id": "metrics", "questions": ["order total"], "searches": []},
            {"id": "summary", "questions": [], "searches": []},
            {"id": "performance", "questions": ["order total"], "searches": []},
            {"id": "inventory", "questions": ["order total"], "searches": []},
            {"id": "next", "questions": [], "searches": []},
            {"id": "risks", "questions": ["should not run yet"], "searches": ["should not search yet"]},
        ]},
        {"charts": [{
            "id": "chart_001",
            "title": "Order total",
            "subtitle": "",
            "type": "bar",
            "echarts_option": {"series": [{"type": "bar", "data": [1000]}]},
        }]},
        {"charts": [{
            "id": "chart_001",
            "title": "Order total",
            "subtitle": "",
            "type": "bar",
            "echarts_option": {"series": [{"type": "bar", "data": [1000]}]},
        }]},
        {"code": "rows = datasets[0]['rows']\ninsights = [{'title': '订单总额', 'detail': '总额 ' + str(rows[0]['total_amount']), 'evidence': {'total': rows[0]['total_amount']}}]"},
        {"code": "rows = datasets[0]['rows']\ninsights = [{'title': '订单总额', 'detail': '总额 ' + str(rows[0]['total_amount']), 'evidence': {'total': rows[0]['total_amount']}}]"},
        {"code": "rows = datasets[0]['rows']\ninsights = [{'title': '订单总额', 'detail': '总额 ' + str(rows[0]['total_amount']), 'evidence': {'total': rows[0]['total_amount']}}]"},
        {"code": "rows = datasets[0]['rows']\ninsights = [{'title': '订单总额', 'detail': '总额 ' + str(rows[0]['total_amount']), 'evidence': {'total': rows[0]['total_amount']}}]"},
        {"content": "Report: 1000.", "key_points": ["1000"]},
        {"content": "Report: 1000.", "key_points": ["1000"]},
        {"content": "Report: 1000.", "key_points": ["1000"]},
        {"content": "Report: 1000.", "key_points": ["1000"]},
        {"write": True, "questions": ["open purchase orders"], "searches": []},
        {"code": "rows = datasets[0]['rows']\ninsights = [{'title': '订单总额', 'detail': '总额 ' + str(rows[0]['total_amount']), 'evidence': {'total': rows[0]['total_amount']}}]"},
        {"content": "未结订单还在。", "key_points": ["未结"]},
        {"content": "Report: 1000.", "key_points": ["1000"]},
        {"score": 90, "ok": True, "issues": []},
    ], tool_replies=[AssistantTurn(tool_calls=[ToolCall(id="r1", name="report.draft", arguments={})])],
        text_reply="Report: 1000.")
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90, "retry_question": ""},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90, "retry_question": ""},
    ])
    events = list(service.handle_message(db, report_llm, sid, "write a short sales note"))
    parent_id = events[0]["run_id"]
    assert work_chat(db, report_llm, parent_id) is True
    saved = db.session(sid)["history"][-1]
    assert saved["content"] == "Report: 1000."
    report = next(row for row in db.find_runs_by_owner(parent_id) if row["run_type"] == "report")
    nested = [row for row in db.find_runs_by_owner(report["id"]) if row["run_type"] == "data"]
    assert nested and all(row["status"] == "succeeded" for row in nested)
    assert report["status"] == "succeeded"


def test_badcase_groups_failures_and_keeps_corrections_for_review():
    logs = [
        {"execution_success": False, "error_type": "validation", "error_message": "table is not allowed: sessions", "user_feedback": None},
        {"execution_success": False, "error_type": "execution", "error_message": 'column "nope" does not exist', "generated_sql": "SELECT nope FROM purchase_orders"},
        {"execution_success": True, "user_feedback": "bad", "corrected_sql": "SELECT count(*) FROM purchase_orders", "question": "how many", "generated_sql": "SELECT * FROM purchase_orders"},
        {"execution_success": True, "user_feedback": "good", "question": "fine"},
    ]
    assert classify_error(logs[0]) == "rejected"
    assert classify_error(logs[1]) == "schema_error"
    grouped, report, few = analyze(logs)
    assert len(grouped["rejected"]) == 1 and len(grouped["semantic_error"]) == 1
    assert "3 failed or rated bad" in report
    assert few == [{
        "question": "how many",
        "generated_sql": "SELECT * FROM purchase_orders",
        "corrected_sql": "SELECT count(*) FROM purchase_orders",
    }]


def test_feedback_is_stored_on_the_attempt(db, monkeypatch):
    _models(monkeypatch, [{"tables": ["purchase_orders"]}, {"sql": "DELETE FROM purchase_orders"}] * 2)
    result = answer_question(
        db,
        question="what is the weather", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok is False and result.log_id is not None
    db.set_text2sql_feedback(result.log_id, "bad", reason="wrong tables", corrected_sql="SELECT count(*) FROM purchase_orders")
    row = db.list_text2sql_logs("ps-1")[0]
    assert row["user_feedback"] == "bad" and row["corrected_sql"].startswith("SELECT count")
    with pytest.raises(ValueError):
        db.set_text2sql_feedback(result.log_id, "maybe")


def test_listing_all_text2sql_logs_requires_explicit_admin_mode(db):
    with pytest.raises(ValueError, match="partnership_id"):
        db.list_text2sql_logs()
    assert db.list_text2sql_logs(admin=True) == []


def test_queries_run_as_the_reader_and_cannot_write_or_read_files(db):
    db.insert_po(_po(), [], [])
    scope = {"partnership_id": "ps-1", "brand_id": "brand-1", "distributor_id": "dist-1", "user_id": "all"}
    rows = db.run_scoped_read("SELECT current_user AS role", scope_views(), scope)
    assert rows == [{"role": "text2sql_reader"}]
    with pytest.raises(Exception, match=r"(?i)permission denied"):
        db.run_scoped_read("SELECT pg_read_file('/etc/passwd')", scope_views(), scope)
    with pytest.raises(Exception, match=r"(?i)permission denied"):
        db.run_scoped_read(
            "WITH x AS (INSERT INTO purchase_orders (id) VALUES ('hacked') RETURNING id) SELECT id FROM x",
            scope_views(),
            scope,
        )
    assert db.po("po-1") is not None
    assert db.po("hacked") is None


def test_query_log_is_a_handle_for_reloading_complete_rows(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90},
    ])
    result = answer_question(
        db, question="order total", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    scope = {"partnership_id": "ps-1", "brand_id": "brand-1", "distributor_id": "dist-1", "user_id": "all"}
    rows = db.load_text2sql_rows(result.log_id, "ps-1", scope_views(), scope)
    assert rows == [{"total_amount": 1000}] and rows.truncated is False


def test_scope_inlining_respects_parameter_boundaries():
    sql = _inline_scope("WHERE id = :id AND id IN (:ids)", {"id": "one", "ids": "two"})
    assert sql == "WHERE id = 'one' AND id IN ('two')"


def test_bounded_query_reports_truncation(db):
    scope = {"partnership_id": "ps-1", "brand_id": "brand-1", "distributor_id": "dist-1", "user_id": "all"}
    rows = db.run_scoped_read("SELECT generate_series(1, 201) AS n", scope_views(), scope)
    assert len(rows) == 200
    assert rows.truncated is True
    complete = db.run_scoped_read("SELECT generate_series(1, 200) AS n", scope_views(), scope)
    assert len(complete) == 200
    assert complete.truncated is False


def test_truncated_query_can_report_the_exact_total(db):
    scope = {"partnership_id": "ps-1", "brand_id": "brand-1", "distributor_id": "dist-1", "user_id": "all"}
    rows = db.run_scoped_read(
        "SELECT generate_series(1, 201) AS n", scope_views(), scope,
        count_sql="SELECT COUNT(*) FROM (SELECT generate_series(1, 201) AS n) AS rows",
    )
    assert len(rows) == 200 and rows.truncated is True and rows.row_count == 201


def test_only_a_system_applied_limit_counts_as_a_cap():
    for sql, cap in [
        ("SELECT id FROM products", 100),
        ("SELECT id FROM products LIMIT 99999", 200),
        ("SELECT id FROM products LIMIT 5", None),
        ("SELECT id FROM products LIMIT 200", None),
    ]:
        ok, _, clean = validate(sql)
        assert ok and system_row_cap(sql, clean) == cap, sql


def test_a_statement_that_hits_the_system_cap_is_flagged_truncated(db):
    ask = dict(
        question="rows", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    capped = run_statement(db, "SELECT n FROM generate_series(1, 500) AS n", **ask)
    assert capped.ok and capped.row_count == 500 and capped.truncated is True
    # The author wrote this LIMIT, so 5 rows is the answer rather than a cut-off result.
    asked_for = run_statement(db, "SELECT n FROM generate_series(1, 500) AS n LIMIT 5", **ask)
    assert asked_for.ok and asked_for.row_count == 5 and asked_for.truncated is False


def test_code_marks_a_result_with_nothing_to_total():
    # SUM over no rows is one NULL row; COUNT over no rows is one zero. Neither is an amount of 0.
    assert prompt_view([], columns=["n"])["no_matching_records"] is True
    assert prompt_view([{"total": None}])["no_matching_records"] is True
    assert prompt_view([{"total": None, "order_count": 0}])["no_matching_records"] is True
    # Real data must never be flagged: a genuine zero amount, a NULL beside rows that exist.
    assert prompt_view([{"total": 0}])["no_matching_records"] is False
    assert prompt_view([{"total": None, "order_count": 4}])["no_matching_records"] is False
    assert prompt_view([{"total": 10.5, "order_count": 0}])["no_matching_records"] is False
    assert prompt_view([{"total": None}, {"total": 3}])["no_matching_records"] is False


def test_prompt_view_tells_a_cut_prompt_apart_from_a_cut_query():
    rows = [{"n": index} for index in range(120)]
    view = prompt_view(rows, columns=["n"], row_count=120, truncated=False)
    assert view["row_count"] == 120 and view["shown_rows"] == 50 and len(view["rows"]) == 50
    assert view["truncated"] is False and view["columns"] == ["n"]
    cut = prompt_view(rows[:3], row_count=3, truncated=True)
    assert cut["shown_rows"] == 3 and cut["truncated"] is True and cut["columns"] == ["n"]


def test_required_columns_are_checked_after_query(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT notes FROM purchase_orders"},
        {"score": 90},
    ])
    result = answer_question(
        db, question="order total", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
        required_columns=["total_amount"],
    )
    assert not result.ok and result.columns == ["notes"]
    assert result.missing_columns == ["total_amount"]
    assert "total_amount" in result.handoff


def test_a_capped_result_is_sent_back_for_aggregation(db, monkeypatch):
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT n FROM generate_series(1, 500) AS n"},
        {"sql": "SELECT count(*) AS total FROM generate_series(1, 500) AS n"},
        {"score": 95},
    ])
    result = answer_question(
        db, question="how many?", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok and result.rows == [{"total": 500}] and result.truncated is False
    first_query = next(step for step in result.trace if step.get("action") == "run_sql")
    assert first_query["too_many_rows"]


def test_a_capped_result_is_kept_and_flagged_when_the_model_cannot_aggregate(db, monkeypatch):
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT n FROM generate_series(1, 500) AS n"},
        {"sql": "SELECT n FROM generate_series(1, 400) AS n"},
        {"sql": "SELECT n FROM generate_series(1, 300) AS n"},
        {"score": 40},
    ])
    result = answer_question(
        db, question="list them", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok and result.truncated is True and result.row_count == 300


def test_a_capped_result_survives_a_later_failed_attempt(db, monkeypatch):
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT n FROM generate_series(1, 500) AS n"},
        {"sql": "SELECT nope FROM purchase_orders"},
        {"action": "finish", "tables": [], "sql": "", "score": 0},
    ])
    result = answer_question(
        db, question="list them", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok and result.truncated is True


def test_reflection_stops_at_80_and_retries_a_low_score(db, monkeypatch):
    db.insert_po(_po(), [], [])
    pending, _school = _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT notes FROM purchase_orders"},
        {"score": 80, "retry_question": "what is the order total"},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
    ])
    kept = answer_question(
        db, question="how much did I order?",
        partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert kept.ok and kept.score == 80 and "total_amount" not in kept.rows[0]
    assert len(pending) == 2

    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT notes FROM purchase_orders"},
        {"score": 40, "retry_question": "what is the order total"},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 91, "retry_question": ""},
    ])
    retried = answer_question(
        db, question="how much did I order?",
        partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert retried.ok and retried.score == 91 and retried.rows == [{"total_amount": 1000}]


def test_reflection_keeps_the_best_of_three_scores(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT notes FROM purchase_orders"},
        {"score": 20, "retry_question": "order total"},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 70, "retry_question": "how many orders"},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT count(*) AS n FROM purchase_orders"},
        {"score": 40, "retry_question": "unused"},
    ])
    result = answer_question(
        db, question="how much did I order?",
        partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    assert result.ok and result.score == 70 and result.rows == [{"total_amount": 1000}]


def test_data_feedback_endpoint_stamps_the_session_and_rejects_another(db, monkeypatch):
    db.insert_po(_po(), [], [])
    db.create_session("sess-rate", "ps-1")
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90},
    ])
    result = answer_question(
        db,
        question="order total",
        partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
        session_id="sess-rate",
    )
    assert result.ok and result.log_id
    db.update_session("sess-rate", history=[
        {"role": "user", "content": "order total"},
        {"role": "assistant", "content": "1000", "data_query": {"log_id": result.log_id, "ok": True}},
    ])
    other = db.create_session("sess-other", "ps-1")["id"]
    client = TestClient(create_app(db, FakeLLM()))
    denied = client.post(f"/api/sessions/{other}/data-feedback", json={
        "log_id": result.log_id, "feedback": "bad",
    })
    assert denied.status_code == 404
    saved = client.post("/api/sessions/sess-rate/data-feedback", json={
        "log_id": result.log_id,
        "feedback": "bad",
        "reason": "used the wrong column",
        "corrected_sql": "SELECT count(*) FROM purchase_orders",
    })
    assert saved.status_code == 200 and saved.json()["feedback"] == "bad"
    row = db.list_text2sql_logs("ps-1")[0]
    assert row["user_feedback"] == "bad" and row["corrected_sql"].startswith("SELECT count")
    history = db.session("sess-rate")["history"]
    assert history[1]["data_query"]["feedback"] == "bad"

def test_data_query_records_model_calls_on_the_chat_run(db, monkeypatch):
    db.insert_po(_po(), [], [])
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-data-trace", "ps-1", "u-a")["id"]
    db.update_session(sid, title="orders", tags=["data"])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 90, "retry_question": ""},
    ])
    llm = FakeLLM(
        json_replies=[{"tasks": [{"id": "d", "kind": "agent", "name": "data", "input": {"question": "order total"}, "depends_on": []}]}],
        text_reply="The order total is 1000.",
    )
    events = list(service.handle_message(db, llm, sid, "how much have I ordered?"))
    run_id = events[0]["run_id"]
    assert work_chat(db, llm, run_id) is True

    child = next(row for row in db.find_runs_by_owner(run_id) if row["run_type"] == "data")
    trace = db.run_trace("dist-1", child["id"])
    step_id = next(step["id"] for step in trace["steps"] if step["name"] == "data.query")
    models = [
        (row["provider"], row["model"])
        for row in trace["model_calls"]
        if row["step_id"] == step_id
    ]
    assert models == [("deepseek", "deepseek-flash")] * 3
    parent_events = db.list_run_events("dist-1", run_id) or []
    assert any(
        event["event_type"] == "model.started"
        and (event.get("payload_json") or {}).get("projected_from") == child["id"]
        for event in parent_events
    )


def test_data_query_records_a_low_score_retry(db, monkeypatch):
    db.insert_po(_po(), [], [])
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = db.create_session("sess-data-retry", "ps-1", "u-a")["id"]
    db.update_session(sid, title="orders", tags=["data"])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT notes FROM purchase_orders"},
        {"score": 40, "retry_question": "what is the order total"},
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT total_amount FROM purchase_orders"},
        {"score": 91, "retry_question": ""},
    ])
    llm = FakeLLM(
        json_replies=[{"tasks": [{"id": "d", "kind": "agent", "name": "data", "input": {"question": "order total"}, "depends_on": []}]}],
        text_reply="The order total is 1000.",
    )
    events = list(service.handle_message(db, llm, sid, "how much have I ordered?"))
    run_id = events[0]["run_id"]
    assert work_chat(db, llm, run_id) is True

    child = next(row for row in db.find_runs_by_owner(run_id) if row["run_type"] == "data")
    trace = db.run_trace("dist-1", child["id"])
    step_id = next(step["id"] for step in trace["steps"] if step["name"] == "data.query")
    models = [
        (row["provider"], row["model"])
        for row in trace["model_calls"]
        if row["step_id"] == step_id
    ]
    assert models == [("deepseek", "deepseek-flash")] * 5


def test_bad_model_output_is_fed_back_then_recovers(db, monkeypatch):
    from backend.text2sql import text2sql as t

    seen: list[list[dict]] = []
    replies = iter([ValueError("bad json"), {"action": "finish", "tables": [], "sql": "", "score": 0}])

    def decide(**kwargs):
        seen.append(list(kwargs["observations"]))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(t, "_next_action", decide)
    result = t.answer_question(db, question="q", partnership_id="p", brand_id="b", distributor_id="d")
    assert seen[1][0]["action"] == "model_output" and seen[1][0]["hint"]
    assert result.stop_reason == "finished"
    assert result.ok is False and result.error == "no query completed"
    assert [o["turn"] for o in result.trace] == [1]


def test_unusable_output_stops_after_the_repair_limit(db, monkeypatch):
    from backend.text2sql import text2sql as t

    calls = []

    def decide(**kwargs):
        calls.append(1)
        raise ValueError("bad json")

    monkeypatch.setattr(t, "_next_action", decide)
    result = t.answer_question(db, question="q", partnership_id="p", brand_id="b", distributor_id="d")
    assert len(calls) == t._MAX_REPAIRS + 1
    assert result.stop_reason == "model_error" and result.error_type == "generation"
    assert [o["action"] for o in result.trace] == ["model_output"] * (t._MAX_REPAIRS + 1)


def test_a_spent_model_budget_is_raised_not_swallowed(db, monkeypatch):
    from backend.llm.calls import ModelBudgetExceeded
    from backend.text2sql import text2sql as t

    def decide(**kwargs):
        raise ModelBudgetExceeded()

    monkeypatch.setattr(t, "_next_action", decide)
    with pytest.raises(ModelBudgetExceeded):
        t.answer_question(db, question="q", partnership_id="p", brand_id="b", distributor_id="d")


def test_each_attempt_log_keeps_its_turn_and_later_score(db, monkeypatch):
    db.insert_po(_po(), [], [])
    _models(monkeypatch, [
        {"tables": ["purchase_orders"]},
        {"sql": "SELECT count(*) AS n FROM purchase_orders"},
        {"score": 90},
    ])
    result = answer_question(
        db, question="how many orders?", partnership_id="ps-1", brand_id="brand-1", distributor_id="dist-1",
    )
    row = db.list_text2sql_logs("ps-1")[0]
    assert row["id"] == result.log_id and row["turn"] == 2 and row["score"] == 90


def test_row_caps_can_be_lowered_for_an_experiment_but_never_below_the_default(monkeypatch):
    import importlib

    from backend.text2sql import sql_validator

    try:
        monkeypatch.setenv("KA_SQL_DEFAULT_LIMIT", "50")
        monkeypatch.setenv("KA_SQL_MAX_LIMIT", "80")
        lowered = importlib.reload(sql_validator)
        assert (lowered.DEFAULT_LIMIT, lowered.MAX_LIMIT) == (50, 80)
        ok, _error, clean = lowered.validate("SELECT id FROM purchase_orders")
        assert ok and clean.endswith("LIMIT 50")
        ok, _error, clean = lowered.validate("SELECT id FROM purchase_orders LIMIT 150")
        assert ok and clean.endswith("LIMIT 80")
        assert lowered.system_row_cap("SELECT id FROM purchase_orders LIMIT 150", clean) == 80
        monkeypatch.setenv("KA_SQL_DEFAULT_LIMIT", "nonsense")
        monkeypatch.setenv("KA_SQL_MAX_LIMIT", "10")
        assert importlib.reload(sql_validator).DEFAULT_LIMIT == 100
        assert sql_validator.MAX_LIMIT == 100  # a max below the default would make LIMIT contradict itself
    finally:
        monkeypatch.delenv("KA_SQL_DEFAULT_LIMIT", raising=False)
        monkeypatch.delenv("KA_SQL_MAX_LIMIT", raising=False)
        importlib.reload(sql_validator)
