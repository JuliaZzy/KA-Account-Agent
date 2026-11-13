"""Fixed SQL for the metrics chapter. The planner no longer writes those questions.

Contract targets always come from `contracts`. Period totals are purchase orders when
the user asked for 采购, otherwise confirmed `report_facts` revenue. Period labels are
YYYY-MM inlined after a format check — `run_scoped_read` cannot bind user parameters.
"""

from __future__ import annotations

import re
from typing import Any

from ..db import Database
from ..text2sql.sql_validator import validate
from ..text2sql.text2sql import QueryResult, no_matching_records, run_statement

_PERIOD_CN = re.compile(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月")
_PERIOD_ISO = re.compile(r"(20\d{2})-(1[0-2]|0[1-9])")
_YYYY_MM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

CONTRACT_SQL = """
SELECT title, annual_sales_target, annual_sales_target_currency,
       term_start_date, term_end_date, effective_from, status
FROM contracts
WHERE status = 'active'
ORDER BY created_at DESC
LIMIT 5
""".strip()

CONTRACT_QUESTION = "正在生效的合同的年度销售目标、货币和期限起止日是什么？"


def period_from_message(message: str) -> str | None:
    """First YYYY-MM mentioned in the user text, or None."""
    text = message or ""
    found = _PERIOD_CN.search(text)
    if found:
        return f"{int(found.group(1)):04d}-{int(found.group(2)):02d}"
    found = _PERIOD_ISO.search(text)
    if found:
        return f"{found.group(1)}-{int(found.group(2)):02d}"
    return None


def named_period_absent(period: str | None, rows: list[dict[str, Any]] | None) -> bool:
    """True when the named YYYY-MM is missing from the metrics totals query."""
    if not period:
        return False
    data = [row for row in (rows or []) if isinstance(row, dict)]
    if no_matching_records(data):
        return True
    return period not in {str(row.get("period") or "") for row in data}


def previous_month(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    month -= 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def uses_purchase_orders(message: str) -> bool:
    """Purchase monthly reports read purchase_orders; sales/动销 reports read report_facts."""
    text = message or ""
    return "采购" in text


def _literal(period: str) -> str:
    if not _YYYY_MM.fullmatch(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    return f"'{period}'"


def totals_sql(message: str, period: str | None) -> tuple[str, str, list[str]]:
    """SQL, the question shown to the writer, and table names for the log."""
    if uses_purchase_orders(message):
        question = "指定期间和上一期间的采购金额合计各是多少？"
        tables = ["purchase_orders", "purchase_order_items"]
        if period:
            prev = previous_month(period)
            sql = f"""
SELECT to_char(o.submitted_at, 'YYYY-MM') AS period,
       ROUND(CAST(SUM(i.line_total) AS numeric), 2) AS total_amount
FROM purchase_orders o
JOIN purchase_order_items i ON i.po_id = o.id
WHERE to_char(o.submitted_at, 'YYYY-MM') IN ({_literal(period)}, {_literal(prev)})
GROUP BY 1
ORDER BY 1
""".strip()
        else:
            sql = """
WITH months AS (
  SELECT to_char(submitted_at, 'YYYY-MM') AS period
  FROM purchase_orders
  WHERE submitted_at IS NOT NULL
  GROUP BY 1
  ORDER BY 1 DESC
  LIMIT 2
)
SELECT m.period, ROUND(CAST(SUM(i.line_total) AS numeric), 2) AS total_amount
FROM months m
JOIN purchase_orders o ON to_char(o.submitted_at, 'YYYY-MM') = m.period
JOIN purchase_order_items i ON i.po_id = o.id
GROUP BY m.period
ORDER BY m.period
""".strip()
        return sql, question, tables
    question = "指定期间和上一期间已确认的销售额合计各是多少？"
    tables = ["sales_reports", "report_facts"]
    if period:
        prev = previous_month(period)
        sql = f"""
SELECT r.reporting_period AS period,
       ROUND(CAST(SUM(f.numeric_value) AS numeric), 2) AS confirmed_sales
FROM report_facts f
JOIN sales_reports r ON r.id = f.report_id
WHERE f.status = 'confirmed'
  AND f.metric_key = 'revenue'
  AND r.reporting_period IN ({_literal(period)}, {_literal(prev)})
GROUP BY 1
ORDER BY 1
""".strip()
    else:
        sql = """
WITH periods AS (
  SELECT reporting_period AS period
  FROM sales_reports
  WHERE status = 'confirmed' AND reporting_period IS NOT NULL
  GROUP BY 1
  ORDER BY 1 DESC
  LIMIT 2
)
SELECT p.period, ROUND(CAST(SUM(f.numeric_value) AS numeric), 2) AS confirmed_sales
FROM periods p
JOIN sales_reports r ON r.reporting_period = p.period
JOIN report_facts f ON f.report_id = r.id
WHERE f.status = 'confirmed' AND f.metric_key = 'revenue'
GROUP BY p.period
ORDER BY p.period
""".strip()
    return sql, question, tables


def fetch_metric_results(
    db: Database, *, message: str, partnership_id: str, brand_id: str,
    distributor_id: str, session_id: str, run_id: str | None = None,
) -> list[tuple[str, str, QueryResult]]:
    """Two scoped SELECTs. Failures stay as QueryResult; they do not raise."""
    period = period_from_message(message)
    rows: list[tuple[str, str, QueryResult]] = [
        ("m1", CONTRACT_QUESTION, run_statement(
            db, CONTRACT_SQL, question=CONTRACT_QUESTION,
            partnership_id=partnership_id, brand_id=brand_id, distributor_id=distributor_id,
            session_id=session_id, tables=["contracts"], run_id=run_id,
        )),
    ]
    sql, question, tables = totals_sql(message, period)
    ok, err, _clean = validate(sql)
    if not ok:
        rows.append(("m2", question, QueryResult(
            False, sql, [], 0, err or "metrics SQL rejected", "validation", tables,
        )))
        return rows
    rows.append(("m2", question, run_statement(
        db, sql, question=question,
        partnership_id=partnership_id, brand_id=brand_id, distributor_id=distributor_id,
        session_id=session_id, tables=tables, run_id=run_id,
    )))
    return rows


def assert_templates_validate() -> None:
    """Used by tests. Every variant must pass the same validator as model SQL."""
    for sql, _question, _tables in (
        (CONTRACT_SQL, CONTRACT_QUESTION, ["contracts"]),
        totals_sql("出一份2026年9月的采购月报", "2026-09"),
        totals_sql("出一份2026年9月的采购月报", None),
        totals_sql("出一份2026年9月的动销月报", "2026-09"),
        totals_sql("出一份2026年9月的动销月报", None),
    ):
        ok, err, _clean = validate(sql)
        if not ok:
            raise AssertionError(f"{err}: {sql}")
