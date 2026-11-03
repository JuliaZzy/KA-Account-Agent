"""One planned task, one child run. The handler returns; the parent answers."""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..db import Database
from ..orchestrator.gather import finish_child
from ..orchestrator.plan import PlanTask, tool_name_for
from ..services.run_names import StepStatus, StepType, ToolCallStatus, step_name
from ..services.session_tools import tools as turn_tools

log = logging.getLogger(__name__)


def execute_task(db: Database, llm: Any, run: dict[str, Any]) -> dict[str, Any]:
    """Run the planned tool on this child. Results go to the wave box, not history."""
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    session = db.session(session_id)
    if not session:
        result = _failed(payload, "chat run has no session")
        finish_child(db, run, result)
        return result
    text = str(payload.get("message") or "")
    images = [url for url in (payload.get("images") or []) if isinstance(url, str)]
    documents = _documents(payload.get("documents") or [])
    registry = turn_tools(db, llm, session, text, images=images, documents=documents)
    name = str(payload.get("tool_name") or "")
    if not name:
        task = PlanTask(
            id=str(payload.get("task_id") or ""),
            kind=str(payload.get("kind") or "tool"),
            name=str(payload.get("name") or ""),
            input=payload.get("input") if isinstance(payload.get("input"), dict) else {},
        )
        name = tool_name_for(task)
    if name == "report.draft":
        from ..orchestrator.report import execute_report

        try:
            return execute_report(db, llm, run, session)
        except Exception as exc:
            log.exception("report child failed")
            result = _failed(payload, str(exc), name=name)
            finish_child(db, run, result)
            return result
    tool = registry.find(name)
    args = _arguments_for(payload, list(session.get("history") or []))
    step = db.insert_step(run["id"], StepType.TOOL, step_name(StepType.TOOL, name), args)
    step_id = None if step is None else step["id"]
    if tool is None:
        result = _failed(payload, f"unknown tool {name}", name=name)
        if step_id:
            db.finish_step(step_id, StepStatus.FAILED, None, {"message": result["error"]})
        finish_child(db, run, result)
        return result
    try:
        raw = tool.fn(args, context={
            "run_id": run["id"], "step_id": step_id,
            "history": session.get("history") or [],
        })
        if not isinstance(raw, dict):
            raw = {"ok": True, "data": raw}
        ok = raw.get("ok", True) is not False and not raw.get("error")
        result = {
            "id": payload.get("task_id"),
            "kind": payload.get("kind"),
            "name": name,
            "ok": bool(ok),
            "error": None if ok else str(raw.get("error") or "tool failed"),
            "payload": raw,
        }
        _record_tool(db, run, step_id, name, args, result, raw if ok else None)
    except Exception as exc:
        log.exception("child tool %s failed", name)
        result = _failed(payload, str(exc), name=name)
        _record_tool(db, run, step_id, name, args, result, None)
    finish_child(db, run, result)
    return result


def _record_tool(
    db: Database, run: dict[str, Any], step_id: str | None, name: str,
    args: dict[str, Any], result: dict[str, Any], output: dict[str, Any] | None,
) -> None:
    """Keep the tool_calls row and close the step. Redis is not the record."""
    if not step_id:
        return
    ok = bool(result.get("ok"))
    status = StepStatus.SUCCEEDED if ok else StepStatus.FAILED
    error = None if ok else {"message": result.get("error") or "tool failed"}
    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    saved = db.insert_tool_call(
        run["id"], step_id,
        tool_name=name, args=args, result=output or {},
        status=ToolCallStatus.SUCCEEDED if ok else ToolCallStatus.FAILED,
        idempotency_key=f"{name}:{run['id']}:{payload.get('task_id') or step_id}",
        latency_ms=0, error=error,
        step_status=status, step_output=output, step_error=error,
    )
    if saved is None:
        db.finish_step(step_id, status, output, error)


def _documents(items: Any) -> list[tuple[str, bytes]]:
    documents: list[tuple[str, bytes]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        raw = item.get("data")
        if not name or not isinstance(raw, str):
            continue
        documents.append((name, base64.b64decode(raw)))
    return documents


def _arguments_for(payload: dict[str, Any], history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    args = dict(payload.get("input") or {}) if isinstance(payload.get("input"), dict) else {}
    prior = [item for item in (payload.get("prior") or []) if isinstance(item, dict)]
    name = str(payload.get("name") or payload.get("tool_name") or "")
    if name == "translate":
        text = _upstream_text(prior)
        if text and not str(args.get("text") or "").strip():
            args["text"] = text
    elif name == "send_email":
        translated = _upstream_translation(prior)
        if translated and not str(args.get("body") or "").strip():
            args["body"] = translated
    elif name == "web_search":
        args["query"] = _web_query(args, str(payload.get("message") or ""), history or [])
    if "message" not in args and payload.get("message"):
        asked = args.get("question") or args.get("text")
        if not asked:
            args["message"] = payload.get("message")
    return args


_TOPIC_FILLER = (
    "web_search", "搜索一下", "搜一下", "查一下", "检索",
    "可以", "好的", "好啊", "帮我", "请你", "请", "继续", "直接", "一下", "吧", "了", "用",
)


def _names_a_topic(text: str) -> bool:
    """False when the line only agrees to search and does not say what to search."""
    left = text.casefold()
    for token in _TOPIC_FILLER:
        left = left.replace(token, "")
    kept = "".join(ch for ch in left if not ch.isspace() and ch not in "，。！？、,.!：:")
    return len(kept) >= 2


def _web_query(args: dict[str, Any], current: str, history: list[dict[str, Any]]) -> str:
    """Use the planner's query when it names a subject. Otherwise the previous turn does."""
    query = " ".join(str(args.get("query") or "").split())
    spoken = " ".join(current.split())
    if query and _names_a_topic(query):
        return query[:500]
    if _names_a_topic(spoken):
        return spoken[:500]
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = " ".join(str(message.get("content") or "").split())
        if content in {"(message)", "(confirm)", "(image)"}:
            continue
        if content and _names_a_topic(content):
            return content[:500]
    return (query or spoken)[:500]


def _inner(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def _upstream_translation(prior: list[dict[str, Any]]) -> str:
    for result in reversed(prior):
        if result.get("name") != "translate" or not result.get("ok"):
            continue
        data = _inner(result).get("data")
        if isinstance(data, dict) and data.get("text"):
            return str(data["text"])
        if _inner(result).get("text"):
            return str(_inner(result)["text"])
    return ""


def _upstream_text(prior: list[dict[str, Any]]) -> str:
    translated = _upstream_translation(prior)
    if translated:
        return translated
    parts: list[str] = []
    for result in prior:
        if not result.get("ok"):
            continue
        inner = _inner(result)
        data = inner.get("data")
        if isinstance(data, dict) and data.get("text"):
            parts.append(str(data["text"]))
        elif inner.get("text"):
            parts.append(str(inner["text"]))
        elif isinstance(data, str):
            parts.append(data)
    return "\n\n".join(parts)


def _failed(payload: dict[str, Any], error: str, *, name: str | None = None) -> dict[str, Any]:
    return {
        "id": payload.get("task_id"),
        "kind": payload.get("kind"),
        "name": name or payload.get("tool_name") or payload.get("name"),
        "ok": False,
        "error": error,
        "payload": {},
    }
