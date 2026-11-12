"""聊天里的查数。跑一个问题，然后引用返回的行。口头回答走学校的 deepseek-chat。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..db import Database
from ..orchestrator.speak import answer_directly
from ..orchestrator.status import status
from .prompt import ANSWER
from .text2sql import answer_question, llm_result_view


def _prompt(question: str, result: Any) -> str:
    payload = {"ok": result.ok, "error": result.error, **llm_result_view(result)}
    return (
        f"{question}\n\n"
        "=== 数据（从本合作关系的数据库查出；引用这些数字，不要重算，也不要编造） ===\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
    )


def _mark(result: Any) -> dict[str, Any]:
    error = result.error or (
        "query result is missing required columns: " + ", ".join(result.missing_columns)
        if result.missing_columns else None
    )
    return {
        "ok": result.ok and not result.missing_columns, "row_count": result.row_count,
        "columns": result.columns, "missing_columns": result.missing_columns,
        "truncated": result.truncated, "rows_handle": result.log_id,
        "error": error, "log_id": result.log_id,
    }


def run(
    db: Database, llm: Any, session: dict[str, Any], text: str, question: str, *,
    partnership_id: str, brand_id: str, distributor_id: str,
) -> Iterator[dict[str, Any]]:
    db.update_session(session["id"], specialist="data")
    yield status("data-query", "查数", "正在把问题写成查询并执行", state="running")
    result = answer_question(
        db, question=question,
        partnership_id=partnership_id, brand_id=brand_id,
        distributor_id=distributor_id, session_id=session["id"],
        history=list(session.get("history") or []),
        folded_through=int(session.get("folded_through") or 0),
        summary=str(session.get("summary") or ""),
    )
    detail = f"{result.row_count} 行" if result.ok else "没查到"
    yield status("data-query", "查数", detail, state="done")
    if result.handoff:
        yield {"type": "warning", "message": result.handoff}
    yield {"type": "data", "ok": result.ok, "row_count": result.row_count, "log_id": result.log_id}
    yield status("data-reply", "回答", "正在引用查询结果", state="running")
    done = yield from answer_directly(
        db, llm, session, text,
        prompt_text=_prompt(text, result),
        system=ANSWER,
        extra={"data_query": _mark(result)},
        done_step=("data-reply", "回答", "回复已写完"),
    )
    yield done
