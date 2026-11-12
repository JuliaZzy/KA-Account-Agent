"""Run a bounded observe-and-act loop over scoped business data.

The model can inspect the schema, submit a SELECT, and observe errors or rows before
deciding to retry or finish. Every submitted statement passes the SQL validator and
the database's isolated reader role. Attempts are logged for review.
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

from ..db import Database, utcnow
from ..llm import calls as model_calls
from ..llm import deepseek_client
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..memory import actor_id, attach, prompt_context, prompt_turns, standing_block, summary_block
from .prompt import REACT
from .sql_validator import system_row_cap, unlimited_statement, validate
from .table_metadata import catalog_text, describe_tables, expand_tables, scope_views

_MAX_QUESTION = 2_000
_MAX_CELL = 400
_MAX_ROWS = 50
_SQL_ATTEMPTS = 3
_MAX_INSPECTIONS = 2
_MAX_STEPS = 10  # model calls per question; repair retries and rejected actions each cost one
_MAX_REPAIRS = 2  # retries after the model's output could not be used
# A scored result at or above this is kept over a newer, never-scored one: the model was satisfied.
_ACCEPT_SCORE = 80
_REPAIR_HINT = (
    "上一步输出不是合法的动作 JSON。只输出一个 JSON 对象，"
    "包含 action、tables、sql、score 四个字段，不要附加说明。"
)
_OVERSIZED_HINT = (
    "结果触到了行数上限，返回的行不是完整答案。需要汇总时改用 GROUP BY / SUM / COUNT 在 SQL 里聚合，"
    "或加筛选条件缩小范围；只需要前几名时写明确的 LIMIT。不要把这批行当成全部记录。"
)
_AGGREGATION_HINT = (
    "问题要求一个平均值，但查询返回了多行分组明细。请在 SQL 中先按需要的维度聚合，"
    "再用 AVG/SUM/COUNT 算出最终指标，返回一行结果；不要让回答模型自己数或计算。"
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


def _ledger(trace: _RunTrace | None, purpose: str) -> dict[str, Any]:
    if trace is None or trace.db is None or not trace.run_id or not trace.step_id:
        return {}
    return {"db": trace.db, "run_id": trace.run_id, "step_id": trace.step_id, "purpose": purpose}


def _ds_client():
    return deepseek_client()


def _ask_ds(
    *, system: str, user: str, schema: dict[str, Any], trace: _RunTrace | None = None,
    purpose: str = "data.react",
) -> dict[str, Any]:
    """One JSON object from deepseek-flash, thinking off."""
    return model_calls.flash_json(
        system=system, messages=[{"role": "user", "content": user}], schema=schema,
        temperature=0, client=_ds_client(), **_ledger(trace, purpose),
    )


def action_schema() -> dict[str, Any]:
    return {
        "name": "data_action", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["inspect_schema", "run_sql", "finish"]},
                "tables": {"type": "array", "items": {"type": "string"}},
                "sql": {"type": "string"},
                "score": {"type": "integer"},
            },
            "required": ["action", "tables", "sql", "score"],
        },
    }


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


def _needs_scalar_aggregate(question: str, rows: list[dict[str, Any]]) -> bool:
    """Catch the common case where a monthly-average question returns only monthly detail rows."""
    if len(rows) <= 1:
        return False
    text = (question or "").replace(" ", "")
    return "平均" in text and ("每月" in text or "月均" in text or "平均每" in text)


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


def _next_action(
    *, question: str, context: str, history: list[dict[str, Any]] | None,
    folded_through: int, observations: list[dict[str, Any]], trace: _RunTrace,
    remaining: dict[str, int], required_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Choose one tool action from the original question and bounded observations."""
    system = attach(REACT, context)
    user = json.dumps({
        "original_question": _question_text(question, history, folded_through),
        "catalog": catalog_text(),
        "remaining": remaining,
        "required_columns": required_columns or [],
        "observations": observations,
    }, ensure_ascii=False)
    return _ask_ds(system=system, user=user, schema=action_schema(), trace=trace,
                   purpose="data.react")


def _note(observations: list[dict[str, Any]], turn: int, **fields: Any) -> dict[str, Any]:
    """Append one observation for this turn and return it so a score can be attached later."""
    entry = {"turn": turn, **fields}
    observations.append(entry)
    return entry


def _done(result: QueryResult, reason: str, observations: list[dict[str, Any]]) -> QueryResult:
    result.stop_reason = reason
    result.trace = [{k: v for k, v in o.items() if k not in {"rows", "schema"}} for o in observations]
    return result


def _pick_final(best: QueryResult | None, unrated: QueryResult | None) -> QueryResult | None:
    """The newest success may never have been scored (steps ran out, or the model call failed).

    Keep the older scored result only when the model was already satisfied with it;
    otherwise the newer one is more likely a correction of it.
    """
    if unrated is None:
        return best
    if best is not None and (best.score or 0) >= _ACCEPT_SCORE:
        return best
    return unrated


def _model_failure(err: Exception) -> str:
    """output: unusable JSON, feed the error back. fatal: not the model's doing, stop.

    Vendor timeouts and network faults never get here: the transport turns them into
    `ProviderError`, which is re-raised for the worker's retry policy. `call_model` has
    already tried the fallback routes by then, so a second attempt here would only repeat it.
    """
    return "output" if isinstance(err, ValueError) else "fatal"  # JSONDecodeError is a ValueError


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
    """Inspect schema, run scoped SELECTs, and observe outcomes within fixed limits.

    A successful query is retained if a later attempt fails. `reflect=False` returns
    after the first successful query for callers that do not need semantic review.
    """
    original = (question or "").strip()[:_MAX_QUESTION]
    required = [str(column).strip() for column in (required_columns or []) if str(column).strip()]
    if not original:
        return QueryResult(False, None, [], 0, "empty question", "generation", [])
    trace = _RunTrace(db=db, run_id=run_id, step_id=step_id)
    person = actor_id(db, session_id)
    context = prompt_context(standing_block(db, partnership_id, person), summary_block(summary))
    observations: list[dict[str, Any]] = []
    inspected: list[str] = []
    latest: QueryResult | None = None
    unrated: QueryResult | None = None
    unrated_entry: dict[str, Any] | None = None
    best: QueryResult | None = None
    oversized: QueryResult | None = None
    sql_attempts = inspections = repairs = 0
    stop_reason = "steps_exhausted"
    for turn in range(1, _MAX_STEPS + 1):
        # With an unscored success pending, one more call is allowed so the model can score or finish.
        if sql_attempts >= _SQL_ATTEMPTS and unrated is None:
            _note(observations, turn, action="run_sql",
                  error=f"sql attempt limit {_SQL_ATTEMPTS} reached; loop stopped")
            stop_reason = "sql_attempts_exhausted"
            break
        try:
            action = _next_action(
                question=original, context=context, history=history,
                folded_through=folded_through, observations=observations, trace=trace,
                remaining={
                    "steps": _MAX_STEPS - turn + 1,
                    "sql_attempts": _SQL_ATTEMPTS - sql_attempts,
                    "inspections": _MAX_INSPECTIONS - inspections,
                },
                required_columns=required,
            )
        except (ModelCapacityExceeded, ProviderError, model_calls.ModelBudgetExceeded):
            raise
        except Exception as err:
            kind = _model_failure(err)
            message = f"{type(err).__name__}: {str(err)[:300]}"
            entry = _note(observations, turn, action="model_output", error=message, kind=kind)
            if kind == "output" and repairs < _MAX_REPAIRS:
                repairs += 1
                entry["hint"] = _REPAIR_HINT
                continue
            logger.warning("text2sql model step failed: %s", message, exc_info=(kind == "fatal"))
            stop_reason = "model_error"
            if latest is None:
                latest = QueryResult(False, None, [], 0, message, "generation", inspected)
            break
        choice = action.get("action")
        if unrated is not None:
            unrated.score = _clamp_score(action.get("score"))
            if unrated_entry is not None:
                unrated_entry["score"] = unrated.score
            if unrated.log_id is not None:
                _log_score(db, unrated.log_id, unrated.score)
            if best is None or unrated.score >= (best.score or 0):
                best = unrated
            unrated = unrated_entry = None
        if choice == "finish":
            stop_reason = "finished"
            break
        if choice == "inspect_schema":
            if inspections >= _MAX_INSPECTIONS:
                _note(observations, turn, action="inspect_schema",
                      error="schema inspection limit reached; use run_sql or finish")
                continue
            names = action.get("tables")
            tables = expand_tables([str(name) for name in names] if isinstance(names, list) else [])
            if not tables:
                _note(observations, turn, action="inspect_schema",
                      error="no known tables selected; choose table names from the catalog")
                continue
            inspections += 1
            inspected = list(dict.fromkeys([*inspected, *tables]))
            _note(observations, turn, action="inspect_schema", tables=tables,
                  schema=describe_tables(tables))
            continue
        if choice != "run_sql":
            _note(observations, turn, error="unknown action")
            continue
        if not inspected:
            _note(observations, turn, action="run_sql", error="inspect schema first")
            continue
        if sql_attempts >= _SQL_ATTEMPTS:
            _note(observations, turn, action="run_sql",
                  error=f"sql attempt limit {_SQL_ATTEMPTS} reached; loop stopped")
            stop_reason = "sql_attempts_exhausted"
            break
        sql = _strip_fence(str(action.get("sql") or ""))
        sql_attempts += 1
        latest = run_statement(
            db, sql, question=original, partnership_id=partnership_id, brand_id=brand_id,
            distributor_id=distributor_id, session_id=session_id, tables=inspected,
            run_id=run_id, turn=turn,
        )
        entry = _note(observations, turn, **_observation(latest))
        if latest.ok:
            latest.missing_columns = _missing_columns(required, latest.columns)
            entry["missing_columns"] = latest.missing_columns
            if latest.missing_columns:
                continue
            if _needs_scalar_aggregate(original, latest.rows):
                latest.ok = False
                latest.error = _AGGREGATION_HINT
                latest.error_type = "result_shape"
                entry["aggregation_required"] = _AGGREGATION_HINT
                continue
            if latest.truncated:
                # The statement hit the system row cap, so these rows are not the whole answer.
                # Ask the model to aggregate or filter while it still has attempts; a cut-off
                # result is kept as a fallback and goes out flagged `truncated` if nothing better comes.
                oversized = latest
                if sql_attempts < _SQL_ATTEMPTS:
                    entry["too_many_rows"] = _OVERSIZED_HINT
                    continue
            unrated, unrated_entry = latest, entry
            if not reflect:
                return _done(latest, "first_success", observations)
    final = _pick_final(best, unrated)
    if final is None and oversized is not None:
        final = oversized
    if final is not None:
        final.missing_columns = _missing_columns(required, final.columns)
        if final.missing_columns:
            message = "查询结果缺少下游要求的列：" + ", ".join(final.missing_columns)
            final.ok, final.error, final.error_type, final.handoff = False, message, "columns", message
        return _done(final, stop_reason, observations)
    if latest is None:
        latest = QueryResult(False, None, [], 0, "no query completed", "generation", inspected)
    if latest.missing_columns:
        message = "查询结果缺少下游要求的列：" + ", ".join(latest.missing_columns)
        latest.ok, latest.error, latest.error_type, latest.handoff = False, message, "columns", message
    elif sql_attempts:
        latest.handoff = (
            "sql 没有跑通，需要人工处理。\n"
            f"报错：{latest.error or '未知'}\n"
            f"最后一次内容：{(latest.sql or '')[:800]}"
        )
    return _done(latest, stop_reason, observations)


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
    row_count = int(getattr(raw_rows, "row_count", len(rows))) if ok else len(rows)
    log_id = _log(
        db, partnership_id=partnership_id, session_id=session_id, question=question, tables=list(tables or []),
        sql=clean or sql, ok=ok, row_count=row_count, elapsed=elapsed, error_type=error_type, error=error,
        run_id=run_id, turn=turn,
    )
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
