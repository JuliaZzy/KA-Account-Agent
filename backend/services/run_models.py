"""Callbacks for the tool loop, not a replacement for the desk agents.

`decide` sends the tool schemas to the injected model and gets back a tool
call or some text. `answer` asks that same model for a final reply when
decide returned neither. The tools themselves are registered in `session_tools.tools`.

Purchase orders, contracts, reports, data queries, and code fixes keep their
own modules. This file does not price an order, read a contract, or write a
session.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..llm.calls import DEEPSEEK_FLASH, SCHOOL_CHAT, Brand
from ..llm.fake import FakeLLM
from ..llm.types import AssistantTurn
from ..memory import attach, prompt_context, standing_block, summary_block
from ..orchestrator.prompt import ANSWER, REACT
from ..repositories.run_repository import note_running_reply
from .run_loop import ToolRegistry, execute_run
from .session_tools import tools as turn_tools


def bind(
    llm: Any, *, decide_system: str = REACT, answer_system: str = ANSWER,
    on_text: Callable[..., None] | None = None,
) -> tuple[Callable[..., AssistantTurn], Callable[..., str]]:
    """Turn one injected client into the harness callbacks.

    Tool choice is official `deepseek-flash`. The final reply is campus
    `deepseek-chat`. `call_model` opens the matching vendor. `on_text`
    receives the prose as it grows.
    """

    decide_brand: Brand = "fake" if isinstance(llm, FakeLLM) else "deepseek"
    answer_brand: Brand = "fake" if isinstance(llm, FakeLLM) else "sjtu"

    def decide(context: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> AssistantTurn:
        called = model_calls.call_model(
            DEEPSEEK_FLASH, brand=decide_brand, kind="chat_completion", expect="tools",
            client=llm, system=decide_system, messages=_tool_messages(context),
            tools=schemas, tool_choice="auto", reasoning_effort="none", on_text=on_text,
        )
        turn = called.body if isinstance(called.body, AssistantTurn) else AssistantTurn()
        return _with_usage(turn, (
            called.prompt_tokens, called.completion_tokens,
            called.cache_hit_tokens, called.cache_miss_tokens, called.cache_creation_tokens,
        ))

    def answer(context: list[dict[str, Any]]) -> str:
        called = model_calls.call_model(
            SCHOOL_CHAT, brand=answer_brand, kind="chat_completion", expect="text",
            client=llm, system=answer_system, messages=_answer_messages(context),
            on_text=on_text,
        )
        text = str(called.body or "").strip()
        answer.prompt_tokens = called.prompt_tokens
        answer.completion_tokens = called.completion_tokens
        answer.cache_hit_tokens = called.cache_hit_tokens
        answer.cache_miss_tokens = called.cache_miss_tokens
        answer.cache_creation_tokens = called.cache_creation_tokens
        return text

    decide.brand, decide.model = decide_brand, DEEPSEEK_FLASH
    answer.brand, answer.model = answer_brand, SCHOOL_CHAT
    return decide, answer


def execute_for(
    db: Database,
    llm: Any,
    tools: ToolRegistry | None = None,
    *,
    session: dict[str, Any] | None = None,
    text: str = "",
    images: list[str] | None = None,
    documents:list[tuple[str, bytes]] | None = None,
    decide_system: str = REACT,
    answer_system: str = ANSWER,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The callable `work_once` takes for the tool loop."""
    if session is not None:
        memory = prompt_context(
            standing_block(db, session["partnership_id"], session.get("user_id")),
            summary_block(session.get("summary")),
        )
        decide_system = attach(decide_system, memory)
        answer_system = attach(answer_system, memory)
    if tools is None:
        if session is None:
            raise ValueError("session is required when tools is None")
        tools = turn_tools(db, llm, session, text, images=images, documents=documents)

    def execute(run: dict[str, Any]) -> dict[str, Any]:
        def on_text(
            growing: str,
            generation_id: str | None = None,
            reasoning: str | None = None,
        ) -> None:
            kwargs: dict[str, Any] = {"generation_id": generation_id}
            if reasoning:
                kwargs["reasoning"] = reasoning
            note_running_reply(db, run["id"], growing, **kwargs)
            
        decide, answer = bind(llm, decide_system=decide_system, answer_system=answer_system, on_text=on_text)
        return execute_run(db, tools, decide=decide, answer=answer)(run)

    return execute


def _with_usage(
    turn: AssistantTurn,
    usage: tuple[int, int, int | None, int | None, int | None],
) -> AssistantTurn:
    prompt_tokens, completion_tokens, cache_hit, cache_miss, cache_creation = usage
    if not isinstance(turn, AssistantTurn):
        return turn
    if turn.prompt_tokens or turn.completion_tokens or (prompt_tokens == 0 and completion_tokens == 0):
        return turn
    return AssistantTurn(
        content=turn.content,
        tool_calls=list(turn.tool_calls),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        cache_creation_tokens=cache_creation,
    )

def _tool_messages(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Harness blocks use tool_use / tool_result. Chat clients speak tool_calls."""
    messages: list[dict[str, Any]] = []
    for message in context:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            messages.append(_assistant_chat(content))
            continue
        if role != "user":
            continue
        if isinstance(content, str):
            if content.strip():
                messages.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                if texts:
                    messages.append({"role": "user", "content": "\n".join(texts)})
                    texts = []
                messages.append(_tool_message(block))
                continue
            text = str(block.get("text") or "").strip() if block.get("type") == "text" else ""
            if text:
                texts.append(text)
        if texts:
            messages.append({"role": "user", "content": "\n".join(texts)})
    return messages


def _assistant_chat(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                text_parts.append(str(block["text"]).strip())
            elif block.get("type") == "tool_use":
                raw = block.get("input")
                arguments = raw if isinstance(raw, dict) else {}
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                })
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tool_message(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": block.get("tool_use_id"),
        "content": _json_text(block.get("content")),
    }


def _answer_messages(context: list[dict[str, Any]]) -> list[dict[str, str]]:
    """A text call has no tool role. Tool results become plain user text."""
    messages: list[dict[str, str]] = []
    for message in context:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            text = _plain_text(content)
            if text:
                messages.append({"role": "assistant", "content": text})
            continue
        if role != "user":
            continue
        if isinstance(content, str):
            if content.strip():
                messages.append({"role": "user", "content": content})
            continue
        text = _tool_plain(content)
        if text:
            messages.append({"role": "user", "content": text})
    return messages


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip()
    ]
    return "\n".join(parts)


def _tool_plain(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text") or "").strip():
            parts.append(str(block["text"]).strip())
        elif block.get("type") == "tool_result":
            parts.append(_json_text(block.get("content")))
    return "\n".join(parts)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps({} if value is None else value, ensure_ascii=False)
