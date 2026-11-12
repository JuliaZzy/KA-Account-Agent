"""Reject anything that is not one read-only SELECT, then cap its LIMIT.

Keyword searches are the wrong tool here: `END` sits inside `term_end_date`, and the
earlier allow-list also permitted INSERT. The statement is parsed, and only a single
SELECT / UNION tree over the known tables is returned. Writes never reach the database.
The caller still runs that statement inside a transaction that always rolls back.
"""

from __future__ import annotations

import os
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .table_metadata import TABLES

MAX_SQL_CHARS = 8_000
MAX_SELECTS = 8


def _limit_from_env(name: str, default: int) -> int:
    """An experiment can lower the caps (the seed has too few rows to ever reach 100/200).
    Anything unparsable or non-positive falls back to the default."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_LIMIT = _limit_from_env("KA_SQL_DEFAULT_LIMIT", 100)
MAX_LIMIT = max(DEFAULT_LIMIT, _limit_from_env("KA_SQL_MAX_LIMIT", 200))
MAX_OFFSET = 5_000

# Functions sqlglot does not model as their own class. Anything else anonymous is rejected.
_ALLOWED_ANONYMOUS = frozenset({
    "coalesce", "nullif", "lower", "upper", "btrim", "trim", "ltrim", "rtrim",
    "length", "char_length", "character_length", "octet_length",
    "substr", "substring", "left", "right", "replace", "concat", "concat_ws",
    "round", "abs", "ceil", "ceiling", "floor", "trunc", "mod", "greatest", "least",
    "date_trunc", "extract", "date_part", "to_char", "to_date", "to_timestamp", "to_number",
    "age", "make_date", "make_timestamp", "make_timestamptz",
    "now", "current_date", "current_timestamp", "localtimestamp", "localtime",
    "string_agg", "array_agg", "json_agg", "jsonb_agg", "jsonb_object_agg",
    "jsonb_array_elements", "jsonb_array_elements_text", "jsonb_each", "jsonb_each_text",
    "jsonb_typeof", "jsonb_array_length", "jsonb_build_object", "jsonb_build_array",
    "json_build_object", "to_jsonb", "to_json",
    "bool_and", "bool_or", "every",
    "percentile_cont", "percentile_disc",
    "row_number", "rank", "dense_rank", "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
    "count", "sum", "avg", "min", "max",
    "regexp_replace", "split_part", "strpos", "initcap", "reverse",
    "cast",
})

_FORBIDDEN_NAMES = frozenset({
    "set_config", "current_setting", "setval", "nextval", "currval",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "pg_sleep",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_from_bytea",
    "dblink", "dblink_exec", "dblink_connect",
    "query_to_xml", "current_user", "session_user", "current_database", "current_schema",
    "inet_server_addr", "inet_client_addr", "version", "user",
})

_BANNED_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Transaction, exp.Set, exp.Into, exp.Copy, exp.Lock,
)

_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_OFFSET_RE = re.compile(r"\bOFFSET\s+(\d+)\b", re.IGNORECASE)


def _fail(message: str) -> tuple[bool, str, str]:
    return False, message, ""


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        raw = node.name or getattr(node.this, "name", "") or node.this
        return str(raw or "").lower()
    try:
        return node.sql_name().lower()
    except Exception:
        return node.key.lower()


def _schema_qualified(node: exp.Func) -> bool:
    """A schema prefix names one function in one schema, past the locked search path.

    sqlglot hangs `pg_catalog.date_trunc(...)` on a parent `Dot` and renders the
    function itself without the schema, so the function text alone misses that shape.
    A quoted name (`"pg_catalog.date_trunc"(...)`) keeps the dot inside the function.

    The rendered text is the wrong place to look for that dot. `AND` and `OR` are
    functions here, so `rf.status = 'confirmed' AND sr.status = 'confirmed'` would
    count as a schema-qualified call.
    """
    parent = node.parent
    if isinstance(parent, exp.Dot) and parent.expression is node:
        return True
    return isinstance(node, exp.Anonymous) and "." in _func_name(node)


def _const_int(node: exp.Expression) -> int | None:
    inner = node
    if isinstance(node, (exp.Limit, exp.Offset)):
        inner = node.args.get("expression") or node.this
    if isinstance(inner, exp.Literal) and not inner.is_string:
        try:
            return int(inner.this)
        except (TypeError, ValueError):
            return None
    return None


def _cap_numbers(sql: str) -> str:
    def limit(match: re.Match[str]) -> str:
        return f"LIMIT {min(int(match.group(1)), MAX_LIMIT)}"

    def offset(match: re.Match[str]) -> str:
        return f"OFFSET {min(int(match.group(1)), MAX_OFFSET)}"

    sql = _LIMIT_RE.sub(limit, sql)
    return _OFFSET_RE.sub(offset, sql)


def system_row_cap(raw: str, sanitized: str) -> int | None:
    """The row cap the system imposed on this statement, or None when the author's LIMIT stood.

    `LIMIT 5` written by the model is the question's answer, not a truncation. A LIMIT the
    validator appended (none written) or lowered (above MAX_LIMIT) is: hitting it means the
    result may be cut short, and callers should say so instead of reporting a row count.
    """
    final = _LIMIT_RE.findall(sanitized)
    if not final:
        return None
    written = _LIMIT_RE.findall(raw or "")
    if not written or int(written[-1]) > MAX_LIMIT:
        return int(final[-1])
    return None


def unlimited_statement(raw: str, sanitized: str) -> str | None:
    """Remove only a validator-added top-level LIMIT for an exact truncation count.

    An explicit LIMIT is part of the user's request and is therefore retained. The caller uses
    the returned statement only inside a bounded COUNT wrapper after the original query hit the
    system-added cap.
    """
    if system_row_cap(raw, sanitized) is None or _LIMIT_RE.search(raw or ""):
        return None
    try:
        tree = sqlglot.parse_one(sanitized, read="postgres")
    except ParseError:
        return None
    if tree.args.get("limit") is None:
        return None
    tree.set("limit", None)
    return tree.sql(dialect="postgres").strip().rstrip(";")


def validate(sql: str) -> tuple[bool, str, str]:
    """Return (ok, error, sanitized_sql). On success the SQL is one capped SELECT."""
    raw = (sql or "").strip()
    if not raw:
        return _fail("empty SQL")
    if len(raw) > MAX_SQL_CHARS:
        return _fail("query is too long")
    if raw.endswith(";"):
        raw = raw[:-1].strip()
    if ";" in raw:
        return _fail("multiple statements")

    try:
        parsed = [tree for tree in sqlglot.parse(raw, read="postgres") if tree is not None]
    except ParseError:
        return _fail("could not parse SQL")
    if len(parsed) != 1:
        return _fail("only one statement is allowed")
    expr = parsed[0]
    nodes = [item[0] if isinstance(item, tuple) else item for item in expr.walk()]
    if not isinstance(expr, exp.Query) or any(isinstance(node, _BANNED_NODES) for node in nodes):
        return _fail("only SELECT is allowed")
    if len(list(expr.find_all(exp.Select))) > MAX_SELECTS:
        return _fail("query is too deeply nested")

    cte_names = {cte.alias.lower() for cte in expr.find_all(exp.CTE) if cte.alias}
    for table in expr.find_all(exp.Table):
        if table.args.get("db") or table.args.get("catalog"):
            return _fail("schema-qualified names are not allowed")
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        if name not in TABLES:
            return _fail(f"table is not allowed: {table.name}")

    for func in expr.find_all(exp.Func):
        if _schema_qualified(func):
            return _fail("schema-qualified functions are not allowed")
        name = _func_name(func)
        if name in _FORBIDDEN_NAMES or name.startswith(("pg_", "lo_", "dblink")):
            return _fail(f"function is not allowed: {name}")
        if isinstance(func, exp.Anonymous) and name not in _ALLOWED_ANONYMOUS:
            return _fail(f"function is not allowed: {name}")

    for node in list(expr.find_all(exp.Limit)) + list(expr.find_all(exp.Offset)):
        if _const_int(node) is None:
            return _fail("LIMIT and OFFSET must be integer constants")

    rendered = expr.sql(dialect="postgres").strip().rstrip(";")
    if _LIMIT_RE.search(rendered) is None:
        rendered = f"{rendered} LIMIT {DEFAULT_LIMIT}"
    return True, "", _cap_numbers(rendered)
