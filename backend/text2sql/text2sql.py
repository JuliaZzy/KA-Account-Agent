"""把一个自然语言问题变成这个合作关系表里的行。

学校的 deepseek-chat 选表，官方 deepseek-flash（思考关闭）写一条 SELECT。代码解析这条语句，在已经排除其他合作关系的视图上执行，并返回行。
行回来之后仍由学校的 deepseek-chat 打分。之后的聊天轮次引用这些行，它不重算它们。

失败后的改写最多三次：同一套 SQL 规则和列说明，加上失败的语句和报错。仍失败就把最后一次语句和报错交给人工。图表和报表代码仍走 `codefix`。
行回来之后打分，最多三次。80 分及以上就用这些行；低于 80 分可以换一个问题再查。
审过的改正不会自己喂回提示词——一次改错会让下一个问题学会同一个错误。
失败和改对的尝试记在 `text2sql_logs` 上，供 `analyze_badcase` 看。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..codefix import format_failures
from ..db import Database, utcnow
from ..llm import calls as model_calls
from ..llm import campus_client, deepseek_client
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..memory import actor_id, attach, prompt_context, prompt_turns, standing_block, summary_block
from .prompt import PICK, REFLECT, SQL
from .sql_validator import validate
from .table_metadata import catalog_text, describe_tables, expand_tables, scope_views

_MAX_QUESTION = 2_000
_MAX_CELL = 400
_MAX_ROWS = 50
# The reflect prompt states this same cutoff.
REFLECT_PASS = 80
MAX_REFLECTIONS = 3
_SQL_ATTEMPTS = 3
_SCHOOL = "deepseek-chat"
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


def _school_client():
    return campus_client()


def _ask_school(
    *, system: str, user: str, trace: _RunTrace | None = None, purpose: str = "data.pick",
) -> dict[str, Any]:
    """Campus deepseek-chat. The gateway has no JSON schema; the reply text is the object."""
    return model_calls.school_json(
        _SCHOOL, system=system, messages=[{"role": "user", "content": user}],
        temperature=0, client=_school_client(), **_ledger(trace, purpose),
    )


def _ask_ds(
    *, system: str, user: str, schema: dict[str, Any], trace: _RunTrace | None = None,
    purpose: str = "data.sql",
) -> dict[str, Any]:
    """One JSON object from deepseek-flash, thinking off."""
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


def _pick_tables(question: str, trace: _RunTrace | None = None) -> list[str]:
    raw = _ask_school(
        system=f"{PICK}\n\n=== 表 ===\n{catalog_text()}",
        user=question,
        trace=trace,
    )
    names = raw.get("tables")
    if not isinstance(names, list):
        return []
    return expand_tables([str(name) for name in names])


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


def _write_sql(
    question: str, schema_text: str, *,
    context: str = "", history: list[dict[str, Any]] | None = None, folded_through: int = 0,
    trace: _RunTrace | None = None,
) -> str:
    asked = _question_text(question, history, folded_through)
    system = attach(f"{SQL}\n\n=== 结构 ===\n{schema_text}", context)
    raw = _ask_ds(system=system, user=asked, schema=sql_schema(), trace=trace)
    return _strip_fence(str(raw.get("sql") or ""))


def _revise_sql(*, schema_text: str, statement: str, error: str, trace: _RunTrace | None = None) -> str:
    """Rewrite one failed SELECT against the same column schema and SQL rules."""
    system = (
        f"{SQL}\n\n=== 结构 ===\n{schema_text}\n\n"
        "上一版 SELECT 没有跑通。按报错改这一条，不要改成另一个问题，也不要退回已经失败的那一版。"
        "报错里没指出的数字不要新编。"
    )
    user = json.dumps({"failed_sql": statement, "error": error or "（无）"}, ensure_ascii=False)
    try:
        raw = _ask_ds(system=system, user=user, schema=sql_schema(), trace=trace, purpose="data.sql.revise")
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception:
        return ""
    return _strip_fence(str(raw.get("sql") or ""))


def _retry_sql(
    *, schema_text: str, first: str, trace: _RunTrace | None = None,
    run: Any,
) -> tuple[Any, list[dict[str, str]]]:
    """Run the statement. On failure, rewrite and run again, at most three runs."""
    current = first
    attempts: list[dict[str, str]] = []
    for index in range(_SQL_ATTEMPTS):
        ok, value, error, output = run(current)
        attempts.append({
            "kind": "sql",
            "artifact": current,
            "error": error or "",
            "output": output or "",
        })
        if ok:
            return value, []
        if index == _SQL_ATTEMPTS - 1:
            break
        revised = _revise_sql(schema_text=schema_text, statement=current, error=error or "", trace=trace)
        if not revised:
            break
        current = revised
    return None, attempts


def _run(db: Database, sql: str, params: dict[str, str]) -> tuple[bool, str | None, str, list[dict[str, Any]] | None]:
    ok, message, clean = validate(sql)
    if not ok:
        # validate() returns no statement on failure. Repair and the log need the text the model wrote.
        return False, message, sql, None
    try:
        rows = db.run_scoped_read(clean, scope_views(), params)
    except Exception as err:
        return False, str(err).split("\n", 1)[0][:500], clean, None
    return True, None, clean, rows


def answer_question(
    db: Database, *, question: str,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str | None = None,
    reflect: bool = True, history: list[dict[str, Any]] | None = None,
    folded_through: int = 0, summary: str = "",
    run_id: str | None = None, step_id: str | None = None,
) -> QueryResult:
    """Pick tables, generate one SELECT, run it, and log the attempt.

    A failed statement is rewritten against the same schema, up to three runs. When the
    rows come back, up to three reflections score how well they answer. A score of 80
    or more keeps them. Below that, the reflection may ask a different question and run
    again. A reflection error keeps the latest rows that did run.
    """
    trace = _RunTrace(db=db, run_id=run_id, step_id=step_id)
    result = _attempt(
        db, question=question, partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session_id, history=history,
        folded_through=folded_through, summary=summary,
        trace=trace,
    )
    if not reflect or not result.ok:
        return result
    chosen = result
    chosen_score = -1
    asked = question
    for turn in range(MAX_REFLECTIONS):
        try:
            score, retry = _reflect(asked, result, trace=trace)
        except (ModelCapacityExceeded, ProviderError):
            raise
        except Exception:
            return result
        result.score = score
        if score >= chosen_score:
            chosen, chosen_score = result, score
        if (
            score >= REFLECT_PASS or turn == MAX_REFLECTIONS - 1
            or not retry or _same_question(retry, asked)
        ):
            return chosen
        nxt = _attempt(
            db, question=retry, partnership_id=partnership_id, brand_id=brand_id,
            distributor_id=distributor_id, session_id=session_id, history=history,
            folded_through=folded_through, summary=summary, trace=trace,
        )
        if not nxt.ok:
            return chosen
        result = nxt
        asked = retry
    return chosen


def _attempt(
    db: Database, *, question: str,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str | None,
    history: list[dict[str, Any]] | None = None, folded_through: int = 0, summary: str = "",
    trace: _RunTrace | None = None,
) -> QueryResult:
    started = time.perf_counter()
    question = (question or "").strip()[:_MAX_QUESTION]
    tables: list[str] = []
    schema_text = ""
    sql: str | None = None
    ok = False
    error: str | None = None
    error_type: str | None = None
    rows: list[dict[str, Any]] = []

    try:
        if not question:
            ok, error, error_type = False, "empty question", "generation"
        else:
            tables = _pick_tables(question, trace=trace)
            if not tables:
                ok, error, error_type = False, "no relevant tables", "generation"
            else:
                schema_text = describe_tables(tables)
                person = actor_id(db, session_id)
                context = prompt_context(standing_block(db, partnership_id, person), summary_block(summary))
                sql = _write_sql(
                    question, schema_text, context=context, history=history, folded_through=folded_through,
                    trace=trace,
                )
                ok, error, sql, raw_rows = _run(
                    db, sql, _scope_params(partnership_id, brand_id, distributor_id, person),
                )
                if ok and raw_rows is not None:
                    rows = _json_rows(raw_rows)
                    error_type = None
                elif _is_validation_error(error):
                    error_type = "validation"
                else:
                    error_type = "execution"
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as err:
        ok, error, error_type = False, str(err).split("\n", 1)[0][:500], "generation"

    elapsed = int((time.perf_counter() - started) * 1000)
    log_id = _log(
        db, partnership_id=partnership_id, session_id=session_id, question=question, tables=tables,
        sql=sql, ok=ok, row_count=len(rows), elapsed=elapsed, error_type=error_type, error=error,
    )
    result = QueryResult(
        ok=ok, sql=sql, rows=rows, row_count=len(rows), error=error if not ok else None,
        error_type=error_type if not ok else None, tables=tables, log_id=log_id,
    )
    if result.ok or not (sql or "").strip():
        return result

    def run(statement: str) -> tuple[bool, QueryResult, str | None, str]:
        if not seen["done"]:
            seen["done"] = True
            return result.ok, result, result.error, ""
        nxt = run_statement(
            db, statement, question=question, partnership_id=partnership_id, brand_id=brand_id,
            distributor_id=distributor_id, session_id=session_id, tables=tables,
        )
        last["result"] = nxt
        return nxt.ok, nxt, nxt.error, ""

    seen = {"done": False}
    last = {"result": result}
    value, attempts = _retry_sql(schema_text=schema_text, first=sql or "", run=run, trace=trace)
    chosen = value if isinstance(value, QueryResult) else last["result"]
    if attempts:
        chosen.handoff = format_failures(attempts)
    return chosen


def run_statement(
    db: Database, sql: str, *, question: str,
    partnership_id: str, brand_id: str, distributor_id: str, session_id: str | None = None,
    tables: list[str] | None = None,
) -> QueryResult:
    """Run one statement the reflection step already wrote. No second model call."""
    started = time.perf_counter()
    params = _scope_params(partnership_id, brand_id, distributor_id, actor_id(db, session_id))
    ok, error, clean, raw_rows = _run(db, sql, params)
    rows = _json_rows(raw_rows) if ok and raw_rows is not None else []
    error_type = None
    if not ok:
        error_type = "validation" if _is_validation_error(error) else "execution"
    elapsed = int((time.perf_counter() - started) * 1000)
    log_id = _log(
        db, partnership_id=partnership_id, session_id=session_id, question=question, tables=list(tables or []),
        sql=clean or sql, ok=ok, row_count=len(rows), elapsed=elapsed, error_type=error_type, error=error,
    )
    return QueryResult(
        ok=ok, sql=clean or sql, rows=rows, row_count=len(rows), error=error if not ok else None,
        error_type=error_type if not ok else None, tables=list(tables or []), log_id=log_id,
    )


def _reflect(question: str, result: QueryResult, trace: _RunTrace | None = None) -> tuple[int, str]:
    raw = _ask_school(
        system=REFLECT,
        user=json.dumps({
            "question": question,
            "tables": result.tables,
            "row_count": result.row_count,
            "rows": _rows_for_prompt(result.rows)[:20],
        }, ensure_ascii=False),
        trace=trace,
        purpose="data.score",
    )
    retry = str(raw.get("retry_question") or "").strip()[:_MAX_QUESTION]
    return _clamp_score(raw.get("score")), retry


def _clamp_score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _same_question(left: str, right: str) -> bool:
    return " ".join(left.lower().split()) == " ".join(right.lower().split())


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
            "created_at": utcnow(),
        })
    except Exception:
        return None
