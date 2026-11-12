"""Run the archived multi-agent text-to-SQL workflow for controlled experiments.

This module is intentionally outside the production text2sql package. It preserves
the historical pick, write, repair, and reflect orchestration while using the current
query safety, result shape, logging, and prompt rules.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..codefix import format_failures
from ..db import Database, utcnow
from ..llm import calls as model_calls
from ..llm import deepseek_client
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..memory import actor_id, attach, prompt_context, prompt_turns, standing_block, summary_block
from ..text2sql.sql_validator import system_row_cap, unlimited_statement, validate
from ..text2sql.table_metadata import catalog_text, describe_tables, expand_tables, scope_views
from ..text2sql.text2sql import _AGGREGATION_HINT, _needs_scalar_aggregate
from .prompt import PICK, REFLECT, SQL

_MAX_QUESTION = 2_000
_MAX_CELL = 400
_MAX_ROWS = 50
_SQL_ATTEMPTS = 3
_MAX_STEPS = 10
REFLECT_PASS = 80
MAX_REFLECTIONS = 3
_OVERSIZED_HINT = (
    "结果触到了行数上限，返回的行不是完整答案。需要汇总时改用 GROUP BY / SUM / COUNT 在 SQL 里聚合，"
    "或加筛选条件缩小范围；只需要前几名时写明确的 LIMIT。不要把这批行当成全部记录。"
)
logger = logging.getLogger(__name__)
_FENCE = re.compile(r"^```(?:sql)?\s*([\s\S]*?)\s*```$", re.IGNORECASE)


@dataclass
class QueryResult:
    ok: bool
    sql: str | None
    rows: list[dict[str, Any]]
    row_count: int
    error: str | None
    error_type: str | None
    tables: list[str]
    log_id: int | None = None
    handoff: str = ""
    score: int | None = None
    truncated: bool = False
    columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    full_rows: list[dict[str, Any]] | None = field(default=None, repr=False, compare=False)
    stop_reason: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _RunTrace:
    db: Database | None = None
    run_id: str | None = None
    step_id: str | None = None
    model_calls: int = 0


class _LegacyStepLimit(Exception):
    """Stop the archived workflow when it reaches the same model-call budget as ReAct."""


def _ledger(trace: _RunTrace | None, purpose: str) -> dict[str, Any]:
    if trace is None or trace.db is None or not trace.run_id or not trace.step_id:
        return {}
    return {"db": trace.db, "run_id": trace.run_id, "step_id": trace.step_id, "purpose": purpose}


def _ds_client():
    return deepseek_client()


def _ask_ds(
    *, system: str, user: str, schema: dict[str, Any], trace: _RunTrace | None = None,
    purpose: str = "data.sql",
) -> dict[str, Any]:
    """One JSON object from deepseek-flash, thinking off."""
    _charge_model_call(trace)
    return model_calls.flash_json(
        system=system, messages=[{"role": "user", "content": user}], schema=schema,
        temperature=0, client=_ds_client(), **_ledger(trace, purpose),
    )


def _strip_fence(sql: str) -> str:
    text = (sql or "").strip()
    match = _FENCE.match(text)
    return (match.group(1) if match else text).strip()


def _json_value(value: Any) -> Any:
    """One cell, safe to store as JSON, with nothing removed.

    Dates and amounts are not JSON types. Nested values are walked in full.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        # Valid text stays text. Bytes that are not UTF-8 stay in the string via surrogates.
        return bytes(value).decode("utf-8", errors="surrogateescape")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _json_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _json_value(value) for key, value in row.items()} for row in rows]


def _clip(value: Any, depth: int = 0) -> Any:
    """A short copy for a model prompt. The returned rows do not use this."""
    if depth > 4:
        return None
    if isinstance(value, str) and len(value) > _MAX_CELL:
        return value[:_MAX_CELL] + "…"
    if isinstance(value, dict):
        return {str(key): _clip(item, depth + 1) for key, item in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [_clip(item, depth + 1) for item in list(value)[:30]]
    return value


def _rows_for_prompt(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _clip(value) for key, value in row.items()} for row in rows[:_MAX_ROWS]]


def prompt_view(
    rows: list[dict[str, Any]], *, columns: list[str] | None = None,
    row_count: int | None = None, truncated: bool = False, max_rows: int = _MAX_ROWS,
) -> dict[str, Any]:
    """The one view every prose-writing model gets of a result set.

    Two different cuts hide rows, and the model must be able to tell them apart:
    `shown_rows < row_count` means this prompt carries only the first rows of what the
    query returned; `truncated` means the statement itself hit the system row cap.
    Code consumers do not use this: they load the complete rows through the log handle.
    """
    shown = _rows_for_prompt(rows)[:max_rows]
    names = [str(column) for column in columns or []]
    if not names and rows:
        names = [str(column) for column in rows[0]]
    return {
        "columns": names,
        "row_count": len(rows) if row_count is None else row_count,
        "shown_rows": len(shown),
        "truncated": truncated,
        "no_matching_records": no_matching_records(rows),
        "rows": shown,
    }


def _is_count_column(name: str) -> bool:
    lowered = str(name).lower()
    return "count" in lowered or "cnt" in lowered


def no_matching_records(rows: list[dict[str, Any]]) -> bool:
    """True when the query found nothing, decided by code rather than by a prompt rule.

    A bare `SUM(x)` over no rows comes back as one row of NULL, and `COUNT(*)` as one row of 0.
    Models read those as "the amount is 0". No rows, or one row whose values are all NULL (or a
    zero in a count column), means there were no records to total.
    """
    if not rows:
        return True
    if len(rows) != 1:
        return False
    for key, value in rows[0].items():
        if value is None:
            continue
        zero = isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
        if zero and _is_count_column(key):
            continue
        return False
    return True


def llm_result_view(result: QueryResult, *, max_rows: int = _MAX_ROWS) -> dict[str, Any]:
    return prompt_view(
        result.rows, columns=result.columns, row_count=result.row_count,
        truncated=result.truncated, max_rows=max_rows,
    )


def _question_text(question: str, history: list[dict[str, Any]] | None, folded_through: int = 0) -> str:
    """The question, with the raw tail of this chat when it already has some."""
    recent = "\n".join(
        f"{turn.get('role')}: {str(turn.get('content') or '')[:500]}"
        for turn in prompt_turns(history, folded_through)
        if turn.get("role") in ("user", "assistant") and str(turn.get("content") or "").strip()
    )
    if not recent:
        return question
    return f"=== 最近对话 ===\n{recent}\n\n=== 问题 ===\n{question}"


def _ask_school(
    *, system: str, user: str, trace: _RunTrace | None = None, purpose: str = "data.pick",
) -> dict[str, Any]:
    """Run the historical planning role through the same DeepSeek JSON route as ReAct."""
    _charge_model_call(trace)
    schema = table_schema() if purpose == "data.pick" else reflection_schema()
    return model_calls.flash_json(
        system=system, messages=[{"role": "user", "content": user}], schema=schema,
        temperature=0, client=_ds_client(), **_ledger(trace, purpose),
    )


def sql_schema() -> dict[str, Any]:
    return {
        "name": "select_sql", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    }


def table_schema() -> dict[str, Any]:
    return {
        "name": "select_tables", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"tables": {"type": "array", "items": {"type": "string"}}},
            "required": ["tables"],
        },
    }


def reflection_schema() -> dict[str, Any]:
    return {
        "name": "reflect_result", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "score": {"type": "integer"},
                "retry_question": {"type": "string"},
            },
            "required": ["score", "retry_question"],
        },
    }


def _charge_model_call(trace: _RunTrace | None) -> None:
    if trace is None:
        return
    if trace.model_calls >= _MAX_STEPS:
        raise _LegacyStepLimit
    trace.model_calls += 1


def _pick_tables(question: str, trace: _RunTrace | None = None) -> list[str]:
    raw = _ask_school(
        system=f"{PICK}\n\n=== 表目录 ===\n{catalog_text()}",
        user=question,
        trace=trace,
    )
    names = raw.get("tables")
    if not isinstance(names, list):
        return []
    return expand_tables([str(name) for name in names])


def _write_sql(
    question: str, schema_text: str, *, context: str = "",
    history: list[dict[str, Any]] | None = None, folded_through: int = 0,
    trace: _RunTrace | None = None, required: list[str] | None = None,
) -> str:
    asked = _question_text(question, history, folded_through)
    if required:
        # ReAct sees `required_columns` on every turn; the writer must not learn about them only
        # after its first statement has already failed the check.
        asked += "\n\n=== required_columns ===\n" + json.dumps(required, ensure_ascii=False)
    system = attach(f"{SQL}\n\n=== 结构 ===\n{schema_text}", context)
    raw = _ask_ds(
        system=system, user=asked, schema=sql_schema(), trace=trace, purpose="data.sql",
    )
    return _strip_fence(str(raw.get("sql") or ""))


def _revise_sql(
    *, schema_text: str, statement: str, error: str, trace: _RunTrace | None = None,
    question: str = "", required: list[str] | None = None,
) -> str:
    """Rewrite a failed or incomplete SELECT against the same schema."""
    system = (
        f"{SQL}\n\n=== 结构 ===\n{schema_text}\n\n"
        "上一版 SELECT 没有得到可用结果。按报错或缺列说明改这一条，"
        "不要改成另一个问题，也不要退回已经失败的版本。"
    )
    raw = _ask_ds(
        system=system,
        user=json.dumps({
            "original_question": question, "required_columns": required or [],
            "failed_sql": statement, "error": error or "（无）",
        }, ensure_ascii=False),
        schema=sql_schema(), trace=trace, purpose="data.sql.revise",
    )
    return _strip_fence(str(raw.get("sql") or ""))


def _reflect(question: str, result: QueryResult, trace: _RunTrace | None = None) -> tuple[int, str]:
    raw = _ask_school(
        system=REFLECT,
        user=json.dumps({
            "question": question,
            "tables": result.tables,
            "result": llm_result_view(result),
            "sql": result.sql,
            "error": result.error,
        }, ensure_ascii=False),
        trace=trace,
        purpose="data.score",
    )
    retry = str(raw.get("retry_question") or "").strip()[:_MAX_QUESTION]
    return _clamp_score(raw.get("score")), retry


def _same_question(left: str, right: str) -> bool:
    return " ".join(left.lower().split()) == " ".join(right.lower().split())


def _done(result: QueryResult, reason: str, observations: list[dict[str, Any]]) -> QueryResult:
    result.stop_reason = reason
    result.trace = [{k: v for k, v in o.items() if k not in {"rows", "schema"}} for o in observations]
    return result


def _observation(result: QueryResult) -> dict[str, Any]:
    return {
        "action": "run_sql", "ok": result.ok, "sql": result.sql,
        "error": result.error, "error_type": result.error_type,
        **llm_result_view(result),
        "missing_columns": result.missing_columns,
    }


_RunOutcome = tuple[bool, str | None, str, list[dict[str, Any]] | None]


def _run(db: Database, sql: str, params: dict[str, str]) -> _RunOutcome:
    ok, message, clean = validate(sql)
    if not ok:
        # validate() returns no statement on failure. Repair and the log need the text the model wrote.
        return False, message, sql, None
    try:
        unlimited = unlimited_statement(sql, clean)
        count_sql = f"SELECT COUNT(*) AS _text2sql_total FROM ({unlimited}) AS _text2sql_rows" if unlimited else None
        rows = db.run_scoped_read(clean, scope_views(), params, count_sql=count_sql)
    except Exception as err:
        return False, str(err).split("\n", 1)[0][:500], clean, None
    return True, None, clean, rows


def answer_question(
    db: Database, *, question: str,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str | None = None,
    reflect: bool = True, history: list[dict[str, Any]] | None = None,
    folded_through: int = 0, summary: str = "",
    run_id: str | None = None, step_id: str | None = None,
    required_columns: list[str] | None = None,
) -> QueryResult:
    """Run the historical pick, write, repair, and reflection workflow."""
    original = (question or "").strip()[:_MAX_QUESTION]
    required = [str(column).strip() for column in (required_columns or []) if str(column).strip()]
    observations: list[dict[str, Any]] = []
    trace = _RunTrace(db=db, run_id=run_id, step_id=step_id)
    if not original:
        result = QueryResult(False, None, [], 0, "empty question", "generation", [])
        return _done(result, "empty_question", observations)

    result = _legacy_attempt(
        db, question=original, partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session_id, history=history,
        folded_through=folded_through, summary=summary, required=required,
        run_id=run_id, trace=trace, observations=observations,
    )
    if not reflect or not result.ok:
        return _done(result, "first_success" if result.ok else "query_error", observations)

    chosen = result
    chosen_score = -1
    asked = original
    for reflection_turn in range(MAX_REFLECTIONS):
        try:
            score, retry = _reflect(asked, result, trace=trace)
        except _LegacyStepLimit:
            observations.append({
                "turn": len(observations) + 1,
                "action": "model_call",
                "error": f"model call limit {_MAX_STEPS} reached; loop stopped",
            })
            return _done(chosen, "steps_exhausted", observations)
        except (ModelCapacityExceeded, ProviderError, model_calls.ModelBudgetExceeded):
            raise
        except Exception as err:
            observations.append({
                "turn": len(observations) + 1, "action": "score", "error": str(err)[:300],
            })
            return _done(chosen, "reflection_error", observations)
        result.score = score
        if result.log_id is not None:
            _log_score(db, result.log_id, score)
        observations.append({
            "turn": len(observations) + 1, "action": "score", "score": score,
            "retry_question": retry,
        })
        if score >= chosen_score:
            chosen, chosen_score = result, score
        if (
            score >= REFLECT_PASS or reflection_turn == MAX_REFLECTIONS - 1
            or not retry or _same_question(retry, asked)
        ):
            return _done(chosen, "finished", observations)
        result = _legacy_attempt(
            db, question=retry, partnership_id=partnership_id, brand_id=brand_id,
            distributor_id=distributor_id, session_id=session_id, history=history,
            folded_through=folded_through, summary=summary, required=required,
            run_id=run_id, trace=trace, observations=observations,
        )
        if not result.ok:
            return _done(chosen, "retry_error", observations)
        asked = retry
    return _done(chosen, "reflection_limit", observations)


def _legacy_attempt(
    db: Database, *, question: str, partnership_id: str, brand_id: str,
    distributor_id: str, session_id: str | None, history: list[dict[str, Any]] | None,
    folded_through: int, summary: str, required: list[str], run_id: str | None,
    trace: _RunTrace, observations: list[dict[str, Any]],
) -> QueryResult:
    """Run one table-selection and SQL-repair attempt of the archived workflow."""
    try:
        result: QueryResult | None = None
        tables = _pick_tables(question, trace=trace)
        if not tables:
            return QueryResult(False, None, [], 0, "no relevant tables", "generation", [])
        schema_text = describe_tables(tables)
        person = actor_id(db, session_id)
        context = prompt_context(standing_block(db, partnership_id, person), summary_block(summary))
        current_sql = _write_sql(
            question, schema_text, context=context, history=history,
            folded_through=folded_through, trace=trace, required=required,
        )
        attempts: list[dict[str, str]] = []
        for repair_turn in range(_SQL_ATTEMPTS):
            result = run_statement(
                db, current_sql, question=question, partnership_id=partnership_id,
                brand_id=brand_id, distributor_id=distributor_id, session_id=session_id,
                tables=tables, run_id=run_id, turn=len(observations) + 1,
            )
            entry = {"turn": len(observations) + 1, **_observation(result)}
            observations.append(entry)
            attempts.append({
                "kind": "sql", "artifact": current_sql, "error": result.error or "",
                "output": json.dumps(result.rows, ensure_ascii=False) if result.ok else "",
            })
            result.missing_columns = _missing_columns(required, result.columns)
            if result.ok and _needs_scalar_aggregate(question, result.rows):
                result.ok = False
                result.error = _AGGREGATION_HINT
                result.error_type = "result_shape"
            if result.ok and not result.missing_columns and not result.truncated:
                return result
            if repair_turn == _SQL_ATTEMPTS - 1:
                break
            reason = result.error or ""
            if result.missing_columns:
                reason = "missing required columns: " + ", ".join(result.missing_columns)
            if result.truncated:
                reason = _OVERSIZED_HINT
            revised = _revise_sql(
                schema_text=schema_text, statement=current_sql, error=reason, trace=trace,
                question=question, required=required,
            )
            if not revised:
                break
            current_sql = revised
        if result is None:
            return QueryResult(False, None, [], 0, "no query completed", "generation", tables)
        if attempts and not result.ok:
            result.handoff = format_failures(attempts)
        return result
    except _LegacyStepLimit:
        observations.append({
            "turn": len(observations) + 1,
            "action": "model_call",
            "error": f"model call limit {_MAX_STEPS} reached; loop stopped",
        })
        if result is not None:
            return result
        return QueryResult(False, None, [], 0, "model call limit reached", "generation", [])
    except (ModelCapacityExceeded, ProviderError, model_calls.ModelBudgetExceeded):
        raise
    except Exception as err:
        return QueryResult(False, None, [], 0, str(err).split("\n", 1)[0][:500], "generation", [])


def run_statement(
    db: Database, sql: str, *, question: str,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str | None = None,
    tables: list[str] | None = None, run_id: str | None = None, turn: int | None = None,
) -> QueryResult:
    """Run one agent-submitted statement without making another model call."""
    started = time.perf_counter()
    params = _scope_params(partnership_id, brand_id, distributor_id, actor_id(db, session_id))
    ok, error, clean, raw_rows = _run(db, sql, params)
    rows = _json_rows(raw_rows) if ok and raw_rows is not None else []
    truncated = bool(getattr(raw_rows, "truncated", False)) if ok else False
    cap = system_row_cap(sql, clean) if ok else None
    # The validator's LIMIT stops the reader one row short of seeing a 201st row, so reaching
    # a cap the system set is the only signal that the statement itself was cut.
    truncated = truncated or (cap is not None and len(rows) >= cap)
    columns = list(getattr(raw_rows, "columns", []) or []) if ok else []
    if not columns and rows:
        columns = [str(column) for column in rows[0]]
    error_type = None
    if not ok:
        error_type = "validation" if _is_validation_error(error) else "execution"
    elapsed = int((time.perf_counter() - started) * 1000)
    log_id = _log(
        db, partnership_id=partnership_id, session_id=session_id, question=question, tables=list(tables or []),
        sql=clean or sql, ok=ok, row_count=int(getattr(raw_rows, "row_count", len(rows))) if ok else len(rows),
        elapsed=elapsed, error_type=error_type, error=error,
        run_id=run_id, turn=turn,
    )
    row_count = int(getattr(raw_rows, "row_count", len(rows))) if ok else len(rows)
    return QueryResult(
        ok=ok, sql=clean or sql, rows=rows, row_count=row_count, error=error if not ok else None,
        error_type=error_type if not ok else None, tables=list(tables or []), log_id=log_id,
        truncated=truncated,
        columns=columns,
    )


def _clamp_score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _missing_columns(required: list[str], actual: list[str]) -> list[str]:
    present = {str(column).casefold() for column in actual}
    return [column for column in required if column.casefold() not in present]


def _is_validation_error(error: str | None) -> bool:
    if not error:
        return False
    if error in {
        "empty SQL", "multiple statements", "only SELECT is allowed", "could not parse SQL",
        "schema-qualified names are not allowed", "query is too deeply nested", "query is too long",
        "only one statement is allowed", "LIMIT and OFFSET must be integer constants",
    }:
        return True
    return (
        "not allowed" in error
        or error.startswith("table is not allowed")
        or error.startswith("function is not allowed")
    )


def _scope_params(
    partnership_id: str, brand_id: str, distributor_id: str, user_id: str | None = None,
) -> dict[str, str]:
    """Scope ids inlined into the views. `all` leaves memories unfiltered — a chat with no person."""
    person = user_id if user_id and re.fullmatch(r"[A-Za-z0-9_\-]+", user_id) else "all"
    return {
        "partnership_id": partnership_id,
        "brand_id": brand_id,
        "distributor_id": distributor_id,
        "user_id": person,
    }


def _log(
    db: Database, *, partnership_id: str, session_id: str | None, question: str, tables: list[str],
    sql: str | None, ok: bool, row_count: int, elapsed: int, error_type: str | None, error: str | None,
    run_id: str | None = None, turn: int | None = None,
) -> int | None:
    try:
        return db.add_text2sql_log({
            "partnership_id": partnership_id,
            "session_id": session_id,
            "question": question,
            "selected_tables": tables,
            "generated_sql": sql,
            "execution_success": ok,
            "row_count": row_count if ok else None,
            "execution_time_ms": elapsed,
            "error_type": error_type,
            "error_message": error[:500] if error else None,
            "run_id": run_id,
            "turn": turn,
            "created_at": utcnow(),
        })
    except Exception:
        return None


def _log_score(db: Database, log_id: int, score: int) -> None:
    try:
        db.set_text2sql_score(log_id, score)
    except Exception:
        logger.warning("could not record text2sql score for log %s", log_id, exc_info=True)
