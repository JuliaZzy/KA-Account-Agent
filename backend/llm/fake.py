"""Scripted stand-in for tests. No network.

`json_replies` are consumed in order. `text_reply` streams word by word.
`tool_replies` are consumed by `chat_completion` when tools are offered.
Every call is appended to `calls`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .types import AssistantTurn


def _chat_response(turn: AssistantTurn) -> SimpleNamespace:
    calls = []
    for call in turn.tool_calls:
        calls.append(SimpleNamespace(
            id=call.id,
            function=SimpleNamespace(
                name=call.name,
                arguments=json.dumps(call.arguments, ensure_ascii=False),
            ),
        ))
    message = SimpleNamespace(content=turn.content, tool_calls=calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@dataclass
class FakeLLM:
    """Scripted model for tests. `json_replies` are consumed in order; `text_reply` streams
    word by word. Every call is recorded so a test can assert on the prompt it received."""

    json_replies: list[dict[str, Any]] = field(default_factory=list)
    tool_replies: list[AssistantTurn] = field(default_factory=list)
    text_reply: str = "Got it."
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_json: bool = False
    model: str = "fake"
    brand: str = "fake"
    last_usage: dict[str, int] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def complete_json(self, *, system, messages, schema=None, temperature=0.0, max_tokens=900):
        with self._lock:
            self.calls.append({"kind": "json", "system": system, "messages": messages, "schema": schema})
            self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
            if self.fail_json:
                raise RuntimeError("model unavailable")
            name = (schema or {}).get("name") if isinstance(schema, dict) else None
            if name == "orchestrator_plan":
                return _take_reply(self.json_replies, "tasks") or {"tasks": []}
            if name == "session_open":
                return _take_open(self.json_replies) or {"tags": [], "title": "New chat"}
            tagged = _take_schema(self.json_replies, name)
            if tagged is not None:
                return tagged
            matched = _take_match(self.json_replies, system, messages)
            if matched is not None:
                return matched
            if not self.json_replies:
                raise RuntimeError("FakeLLM: no scripted JSON reply left")
            return self.json_replies.pop(0)

    def stream_text(self, *, system, messages, temperature=0.7, max_tokens=300):
        self.calls.append({"kind": "text", "system": system, "messages": messages})
        for i, word in enumerate(self.text_reply.split(" ")):
            yield (" " if i else "") + word

    def complete(self, *, system, messages, tools=None, temperature=0.0, max_tokens=900):
        self.calls.append({"kind": "complete", "system": system, "messages": messages, "tools": tools})
        if self.tool_replies:
            return self.tool_replies.pop(0)
        return AssistantTurn()

    def chat_completion(self, *, model, messages, tools=None, tool_choice=None, **kwargs):
        del tool_choice
        system = ""
        chat: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "system":
                system = str(message.get("content") or "")
            else:
                chat.append(message)
        self.calls.append({"kind": "complete", "system": system, "messages": chat, "tools": tools, "model": model})
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        if self.fail_json:
            raise RuntimeError("model unavailable")
        if tools is not None and self.tool_replies:
            turn = self.tool_replies.pop(0)
        else:
            turn = AssistantTurn(content=self.text_reply)
        if kwargs.get("stream"):
            return _stream_turn(turn)
        return _chat_response(turn)


def _take_reply(replies: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for index, reply in enumerate(replies):
        if isinstance(reply, dict) and key in reply:
            return replies.pop(index)
    return None


def _take_open(replies: list[dict[str, Any]]) -> dict[str, Any] | None:
    for index, reply in enumerate(replies):
        if isinstance(reply, dict) and "title" in reply and "tasks" not in reply:
            return replies.pop(index)
    return None


def _take_schema(replies: list[dict[str, Any]], name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    for index, reply in enumerate(replies):
        if isinstance(reply, dict) and reply.get("_schema") == name:
            replies.pop(index)
            return {key: value for key, value in reply.items() if key != "_schema"}
    return None


def _take_match(replies: list[dict[str, Any]], system: str, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    blob = system + json.dumps(messages, ensure_ascii=False)
    for index, reply in enumerate(replies):
        needle = reply.get("_match") if isinstance(reply, dict) else None
        if needle and str(needle) in blob:
            replies.pop(index)
            return {key: value for key, value in reply.items() if key != "_match"}
    return None


def _stream_turn(turn: AssistantTurn):
    if turn.content:
        for i, word in enumerate(turn.content.split(" ")):
            yield _stream_chunk(content=(" " if i else "") + word)
    for index, call in enumerate(turn.tool_calls):
        yield _stream_chunk(tool_call={
            "index": index,
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            },
        })
    yield _stream_chunk(usage={"prompt_tokens": 1, "completion_tokens": 1})


def _stream_chunk(*, content: str | None = None, tool_call: dict[str, Any] | None = None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_call is not None:
        delta["tool_calls"] = [tool_call]
    chunk: dict[str, Any] = {"choices": [{"delta": delta}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk

    def response(self, *, model, input, instructions=None, text=None, **kwargs):
        del model, text, kwargs
        messages = input if isinstance(input, list) else [{"role": "user", "content": input}]
        raw = self.complete_json(system=instructions or "", messages=messages)
        return SimpleNamespace(output_text=json.dumps(raw, ensure_ascii=False))