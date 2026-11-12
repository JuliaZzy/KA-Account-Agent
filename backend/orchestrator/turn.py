"""聊天入口：无标题会话另排一条 OPEN run，父 run 直接 PLAN。

OPEN 只写标题和标签，不进 tasks，也不向聊天 SSE 推事件。见 `docs/orchestrator.md`。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..llm.types import AssistantTurn
from ..memory import attach, prompt_turns
from ..services.chat_run import enqueue_chat_run
from ..services.run_names import StepStatus, StepType, step_name
from ..tools import all_schemas, all_tools
from .prompt import OPEN, REACT

_HANDOFF = {"po": "采购单", "contract": "合同", "report": "报表", "data": "查数", "brand": "品牌资料"}
log = logging.getLogger(__name__)
MAX_IMAGES = 5
MAX_TOOL_ROUNDS = 4

TOPIC_KEYS = ("po", "contract", "report", "brand", "data")


@dataclass(frozen=True)
class SessionOpen:
    title: str
    tags: list[str]

def session_open_schema() -> dict[str, Any]:
    return {
        "name": "session_open", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "tags": {"type": "array", "items": {"type": "string", "enum": list(TOPIC_KEYS)}},
                "title": {"type": "string"},
            },
            "required": ["tags", "title"],
        },
    }

def open_session(llm: Any, *, message: str, attachments: list[str], 
    db: Database | None = None, run_id: str | None = None
) -> SessionOpen:
    """First-turn only: tags + title. No history — the session does not have one yet."""
    user = (
        f"=== 附件 ===\n{', '.join(attachments) or '（无）'}\n\n"
        f"=== 消息 ===\n{message[:4000] or '（无文字）'}"
    )
    messages = [{"role": "user", "content": user}]
    step_id = None
    if db is not None and run_id:
        step = db.insert_step(run_id, StepType.MODEL, step_name(StepType.MODEL, None), {})
        step_id = None if step is None else step["id"]
    raw: dict[str, Any] = {}
    caught: Exception | None = None
    try:
        parsed = model_calls.school_json(
            model_calls.SCHOOL_CHAT, system=OPEN, messages=messages,
            schema=session_open_schema(), temperature=0, client=llm,
            db=db, run_id=run_id, step_id=step_id, purpose="session.title",
        )
        if isinstance(parsed, dict):
            raw = parsed
        else:
            caught = ValueError("Campus model did not return a JSON object")
    except Exception as err:
        caught = err

    if db is not None and run_id and step_id:
        db.finish_step(
            step_id,
            StepStatus.FAILED if caught is not None else StepStatus.SUCCEEDED,
            None if caught is not None else {"title": str(raw.get("title") or "")}, 
            {"message": str(caught)} if caught is not None else None,
        )
    if caught is not None:
        raise caught
    allowed = set(TOPIC_KEYS)
    tags = [t for t in (raw.get("tags") or []) if isinstance(t, str) and t in allowed]
    title = str(raw.get("title") or "").strip() or (attachments[0] if attachments else "New chat")
    return SessionOpen(title=title[:80], tags=tags)

def name_new_chat(db: Database, llm:Any, session: dict[str, Any], run: dict[str, Any]) -> None:
    """Name a chat that has no title yet. A model failure leaves the title empty."""
    execute_open(db, llm, run)


def execute_open(db: Database, llm: Any, run: dict[str, Any]) -> dict[str, Any]:
    """Background OPEN: refine tags and title. No wave box, no events on the chat run.

    The sidebar already shows a provisional title from the first message. If that
    title was replaced (user edit), skip overwrite and keep the new title.
    """
    from ..services.session_title import clear_provisional
    from ..turn_lock import hold_session

    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    session_id = str(payload.get("session_id") or "")
    session = db.session(session_id)
    if not session:
        return {"ok": False, "error": "chat run has no session"}
    provisional = str(payload.get("provisional_title") or "")
    current_title = str(session.get("title") or "")
    if provisional and current_title and current_title != provisional:
        clear_provisional(session_id)
        return {
            "ok": True, "title": current_title, "tags": list(session.get("tags") or []),
            "skipped": "user_title",
        }
    message = str(payload.get("message") or "")
    attachments = [name for name in (payload.get("attachments") or []) if isinstance(name, str)]
    try:
        opened = open_session(
            llm, message=message, attachments=attachments, db=db, run_id=str(run["id"]),
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as exc:
        log.exception("Failed to name new chat")
        return {"ok": False, "error": str(exc)}
    with hold_session(session_id):
        current = db.session(session_id) or session
        now_title = str(current.get("title") or "")
        if provisional and now_title and now_title != provisional:
            clear_provisional(session_id)
            return {
                "ok": True, "title": now_title, "tags": list(current.get("tags") or []),
                "skipped": "user_title",
            }
        fields: dict[str, Any] = {"tags": opened.tags}
        if not now_title or now_title == provisional:
            fields["title"] = opened.title
        db.update_session(session_id, **fields)
        clear_provisional(session_id)
    return {"ok": True, "title": fields.get("title", now_title), "tags": opened.tags}

def _run_one_tool(
    call: Any, runners: dict[str, Any], llm: Any, context: dict[str, Any],
) -> dict[str, Any]:
    fn = runners.get(call.name)
    if fn is None:
        result: dict[str, Any] = {"ok": False, "data": None, "error": f"unknown tool {call.name}"}
    else:
        try:
            result = fn(call.arguments, llm=llm, context=context)
        except (ModelCapacityExceeded, ProviderError):
            raise
        except Exception as err:
            result = {"ok": False, "data": None, "error": str(err)}
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps(result, ensure_ascii=False),
    }


def run_tool_calls(
    llm: Any, *, text: str, history: list[dict[str, Any]], extra: dict[str, Any] | None = None,
    include_mcp: bool | None = None, memory: str | None = None, folded_through: int = 0,
) -> str | None:
    """Native function-calling loop for tests and leftover callers.

    Chat turns do not use this. They go through PLAN and child runs.
    """
    schemas = all_schemas(include_mcp=include_mcp)
    runners = all_tools(include_mcp=include_mcp)
    if not schemas:
        return None
    messages: list[dict[str, Any]] = [
        {"role": m.get("role"), "content": str(m.get("content") or "")}
        for m in prompt_turns(history, folded_through)
        if m.get("role") in ("user", "assistant")
    ]
    messages.append({"role": "user", "content": text})
    context = {"history": history, **(extra or {})}
    used = False
    last: AssistantTurn | None = None
    for _ in range(MAX_TOOL_ROUNDS):
        last = model_calls.flash_tools(
            client=llm, system=attach(REACT, memory or ""), messages=messages, tools=schemas,
            purpose="session.tools",
        )
        if not last.tool_calls:
            if used:
                return (last.content or "").strip() or "Done."
            return None
        used = True
        messages.append(last.as_message())
        # Not the chat worker path. Chat parallelism is child runs, not a thread pool.
        for call in last.tool_calls:
            messages.append(_run_one_tool(call, runners, llm, context))
    return (last.content if last else None) or "Done."

def handle_message(
    db: Database, llm: Any, session_id: str, text: str, images: list[str] | None = None, attachments: list[str] | None = None,
    documents: list[tuple[str, bytes]] | None = None,
) -> Iterator[dict[str, Any]]:
    session = db.session(session_id)
    if not session:
        raise KeyError("session not found")
    text = (text or "").strip()
    images = [u for u in (images or []) if isinstance(u, str) and u.startswith("data:image/")][:MAX_IMAGES]
    documents = [(n, b) for n, b in (documents or []) if n and b]
    if not text and not images and not documents:
        raise ValueError("message required")
    names = [*(attachments or []), *[n for n, _ in documents], *[f"image-{i + 1}.jpg" for i in range(len(images))]]

    run = enqueue_chat_run(
        db, session, text, images=images, documents=documents, attachments=names
    )
    yield {"type": "run", "run_id": run["id"]}
