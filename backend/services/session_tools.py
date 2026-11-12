"""Every tool the loop can call.

Pricing, contract tables, and report numbers stay in the existing modules.
This file only binds this turn onto a `Tool` and returns a dict. It does not 
write thte chat transcript. `send_email` and every MCP tool wait for approval.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..brand_knowledge.answer import _search_query, _with_sources
from ..brand_knowledge.rag import RagUnavailable, retrieve
from ..contract.turn import file_contract
from ..db import Database
from ..mcp.runtime import get_hub, make_mcp_tool_fn, mcp_enabled
from ..memory import prompt_context, standing_block, summary_block
from ..po.agent import collect_order
from ..po.turn import load_bundle
from ..report.turn import ReportDraft, chart_for_query, iter_draft
from ..text2sql.impl import answer_question_impl
from ..tools import exchange_rate, lang_translate, send_email, web_search
from .run_loop import Tool, ToolRegistry
from .run_names import TOOL_STARTED_DISPLAY, StepType, ToolErrorCode, step_name

# Read tools may be retried by the executor. Side effects are not.
_RETRY_FAST = frozenset({ToolErrorCode.TIMEOUT, ToolErrorCode.RATE_LIMITED, ToolErrorCode.UPSTREAM})
_RETRY_QUERY = frozenset({ToolErrorCode.TIMEOUT, ToolErrorCode.UPSTREAM})

_QUESTION = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The question or order text for this tool. Empty means this turn's message.",
        },
    },
}


def tools(
    db: Database,
    llm: Any,
    session: dict[str, Any],
    text: str,
    *,
    images: list[str] | None = None,
    documents:list[tuple[str, bytes]] | None = None,
) -> ToolRegistry:
    """Native tools, MCP and agents."""
    images = images or []
    documents = documents or []
    bundle = load_bundle(db, session["partnership_id"], session.get("user_id"))
    listed = [
        Tool(
            name="web_search",
            description=web_search.SCHEMA["function"]["description"],
            fn=web_search.run,
            parameters=web_search.SCHEMA["function"]["parameters"],
            timeout_seconds=30,
            retryable_errors=_RETRY_FAST,
            display=TOOL_STARTED_DISPLAY["web_search"],
        ),
        Tool(
            name="translate",
            description=lang_translate.SCHEMA["function"]["description"],
            fn=lang_translate.run,
            parameters=lang_translate.SCHEMA["function"]["parameters"],
            timeout_seconds=30,
            retryable_errors=_RETRY_FAST,
            display=TOOL_STARTED_DISPLAY["translate"],
        ),
        Tool(
            name="exchange_rate",
            description=exchange_rate.SCHEMA["function"]["description"],
            fn=exchange_rate.run,
            parameters=exchange_rate.SCHEMA["function"]["parameters"],
            timeout_seconds=30,
            retryable_errors=_RETRY_FAST,
            display=TOOL_STARTED_DISPLAY["exchange_rate"],
        ),
        Tool(
            name="send_email",
            description=send_email.SCHEMA["function"]["description"],
            fn=send_email.run,
            parameters=send_email.SCHEMA["function"]["parameters"],
            requires_approval=True,
            side_effect=True,
            idempotency_required=True,
            timeout_seconds=30,
            display=TOOL_STARTED_DISPLAY["send_email"],
        ),
        Tool(
            name="po.draft",
            description="Draft or update a purchase order. Prices, discount rules in the contract and MOQ come from code.",
            fn=_po_draft(db, llm, session, text, images, bundle),
            parameters=_QUESTION,
            timeout_seconds=300,
            display=TOOL_STARTED_DISPLAY["po.draft"],
        ),
        Tool(
            name="contract.read",
            description="Read an uploaded agreement into a draft table. Nothing is written until confirm.",
            fn=_contract_read(db, session, documents),
            parameters={"type": "object", "properties": {}},
            timeout_seconds=300,
            display=TOOL_STARTED_DISPLAY["contract.read"],
        ),
        Tool(
            name="report.draft",
            description="Draft a performance report. Numbers come from queries, not the model.",
            fn=_report_draft(db, llm, session, text, bundle),
            parameters=_QUESTION,
            timeout_seconds=300,
            display=TOOL_STARTED_DISPLAY["report.draft"],
        ),
        Tool(
            name="data.query",
            description="Answer a question with rows from this partnership's tables through creating a SQL query.",
            fn=_data_query(db, llm, session, text, bundle),
            parameters=_QUESTION,
            timeout_seconds=120,
            retryable_errors=_RETRY_QUERY,
            display=TOOL_STARTED_DISPLAY["data.query"],
        ),
        Tool(
            name="brand.search",
            description="Retrieve brand-knowledge passages for this question.",
            fn=_brand_search(db, llm, session, text, bundle),
            parameters=_QUESTION,
            timeout_seconds=120,
            retryable_errors=_RETRY_QUERY,
            display=TOOL_STARTED_DISPLAY["brand.search"],
        ),
    ]
    taken = {tool.name for tool in listed}
    return ToolRegistry([*listed, *_mcp_tools(taken)])

def _asked(args: dict[str, Any], fallback: str) -> str:
    asked = str(args.get("message") or "").strip()
    return asked or fallback 

def _po_draft(db, llm, session, text, images, bundle):
    def run(args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = context or {}
        run_id = ctx.get("run_id") if isinstance(ctx.get("run_id"), str) else None
        step_id = ctx.get("step_id") if isinstance(ctx.get("step_id"), str) else None
        return collect_order(
            db, llm, session, _asked(args, text), images, bundle, 
            emit=lambda _event:None, run_id=run_id, step_id=step_id
        )
    return run

def _contract_read(db, session, documents):
    def run(args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        del args
        ctx = context or {}
        run_id = ctx.get("run_id") if isinstance(ctx.get("run_id"), str) else None
        filed = file_contract(db, session, documents, run_id=run_id)
        return{
            "status": filed.get("status"),
            "contract_id": filed.get("contract_id"),
            "table": filed.get("table"),
            "pending": filed.get("pending"),
        }
    return run

def _report_draft(db, llm, session, text, bundle):
    def run(args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = context or {}
        run_id = ctx.get("run_id") if isinstance(ctx.get("run_id"), str) else None
        step_id = ctx.get("step_id") if isinstance(ctx.get("step_id"), str) else None
        partnership = bundle.partnership
        draft: ReportDraft | None = None
        for item in iter_draft(
            db, llm, message=_asked(args, text), 
            history=list(session.get("history", [])),
            partnership_id=partnership.id, brand_id=partnership.brand_id,
            distributor_id=partnership.distributor_id, session_id=session["id"],
            folded_through=int(session.get("folded_through") or 0),
            summary=str(session.get("summary") or ""),
            run_id=run_id, step_id=step_id,
        ):
            if isinstance(item, ReportDraft):
                draft = item 
        if draft is None:
            return {"ok": False, "error": "No draft found"}
        return {
            "ok": True,
            "text": draft.text,
            "title": draft.title,
            "charts": draft.charts,
            "web_notes": draft.web_notes,
            "warnings": draft.warnings,
            "report_pending_id": f"rp_{uuid.uuid4().hex[:12]}",
        }
    return run

def _scope(db, partnership, text: str) -> dict[str, Any]:
    """Whose numbers a query returns, plus any other party the user named.

    The model is told to open with "I can only see X" when the user names someone else, but it
    skips that when nothing looks wrong (the numbers come back fine, just for the wrong party).
    Matching names against the other partnerships is deterministic, so the answer gets a field to
    act on instead of having to notice the mismatch itself."""
    scope: dict[str, Any] = {"brand": partnership.brand_name, "distributor": partnership.distributor_name}
    own = {partnership.brand_name, partnership.distributor_name}
    named: list[str] = []
    try:
        others = db.partnerships()
    except Exception:  # scope is advisory; never fail a query over it
        others = []
    for other in others:
        for name in (other.distributor_name, other.brand_name):
            if name and name not in own and name in text and name not in named:
                named.append(name)
    if named:
        scope["user_named_other"] = named
    return scope


def _data_query(db, llm, session, text, bundle):
    def run(args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = context or {}
        run_id = ctx.get("run_id") if isinstance(ctx.get("run_id"), str) else None
        step_id = ctx.get("step_id") if isinstance(ctx.get("step_id"), str) else None
        partnership = bundle.partnership
        result = answer_question_impl()(
            db, question=str(args.get("question") or "").strip() or _asked(args, text),
            required_columns=_required_columns(args),
            run_id=run_id, step_id=step_id, 
            partnership_id=partnership.id, brand_id=partnership.brand_id,
            distributor_id=partnership.distributor_id, session_id=session["id"],
            history=list(session.get("history") or []),
            folded_through=int(session.get("folded_through") or 0),
            summary=str(session.get("summary") or ""),
        )
        contract_error = (
            "query result is missing required columns: " + ", ".join(result.missing_columns)
            if result.missing_columns else result.error
        )
        asked_chart = bool(args.get("chart"))
        charts: list[dict[str, Any]] = []
        chart_error: str | None = None
        if asked_chart and result.ok and result.rows:
            try:
                charts, failures = chart_for_query(
                    db, llm, text, str(args.get("question") or "").strip() or _asked(args, text),
                    result, partnership_id=partnership.id, brand_id=partnership.brand_id,
                    distributor_id=partnership.distributor_id, session_id=session["id"],
                    run_id=run_id, step_id=step_id,
                )
                chart_error = failures[0] if failures and not charts else None
            except Exception as err:  # a failed chart must not turn a good answer into a failed query
                chart_error = str(err).split("\n", 1)[0][:200]
        return {
            "ok": result.ok and not result.missing_columns,
            "complete": result.ok and not result.missing_columns,
            "deliver": str(args.get("deliver") or "data"),
            # Whose numbers these are: the answer must not present them as another party's.
            "scope": _scope(db, partnership, text),
            "chart": {"requested": asked_chart, "produced": bool(charts), "error": chart_error} if asked_chart else None,
            "charts": charts,
            "row_count": result.row_count,
            "columns": result.columns,
            "missing_columns": result.missing_columns,
            "truncated": result.truncated,
            "rows_handle": result.log_id,
            "rows": result.rows,
            "error": contract_error,
            "error_type": result.error_type,
            "sql": result.sql,
            "tables": result.tables,
            "log_id": result.log_id,
            "handoff": result.handoff,
            "score": result.score,
        }
    return run


def _required_columns(args: dict[str, Any]) -> list[str]:
    # `answer` is a direct conversational result. Its columns are not a downstream handoff
    # contract, and forcing planner-selected aliases here can reject a correct aggregate.
    if str(args.get("deliver") or "data").strip().lower() == "answer":
        return []
    raw = args.get("columns", args.get("required_columns", []))
    if not isinstance(raw, list):
        return []
    return [str(column).strip() for column in raw if str(column).strip()]


def _brand_search(db, llm, session, text, bundle):
    def run(args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        del context 
        asked = _asked(args, text)
        folded = int(session.get("folded_through") or 0)
        remembered = prompt_context(
            standing_block(db, session["partnership_id"], session.get("user_id")),
            summary_block(session.get("summary")),
        )
        query = _search_query(llm, asked, list(session.get("history") or []), remembered, folded)
        try:
            hits = retrieve(query, bundle.partnership.brand_id)
        except RagUnavailable as e:
            return {"hits": [], "query": query, "error": str(e)}
        return {"hits": _with_sources(db, hits), "query": query, "error": None}
    return run

def _mcp_tools(taken: set[str]) -> list[Tool]:
    if not mcp_enabled():
        return []
    try:
        hub = get_hub()
        schemas = list(hub.schemas)
    except Exception:
        return []
    registered: list[Tool] = []
    for schema in schemas:
        func = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(func, dict):
            continue 
        name = func.get("name")
        if not isinstance(name, str) or name in taken:
            continue 
        try:
            step_name(StepType.TOOL, name)
        except ValueError:
            continue 
        parameters = func.get("parameters")
        registered.append(Tool(
            name=name,
            description=str(func.get("description") or name),
            fn=make_mcp_tool_fn(hub, name),
            parameters=parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}},
            requires_approval=True,
            side_effect=True,
            idempotency_required=True,
            timeout_seconds=60,
        ))
    return registered
