"""One place that picks a model and calls it.

Campus `deepseek-chat` is the small text model. Campus `deepseek-reasoner` is the
same model with thinking on, for report planning and prose. Campus `qwen` reads
an order photo. Official `qwen3.7-flash` plans a turn. Official `deepseek-flash`
writes strict JSON, Python, and tool calls, with thinking off. None of these
calls set `max_tokens`.

`call_model` is the only entry. It opens the vendor client from `brand` when the
caller did not pass a test double. The four wrappers keep the shapes callers
already expect. Vendors usually send token counts on the final response. A stream,
or a gateway that drops usage, leaves both counts at 0. Those calls are estimated
and labeled `estimated`. OCR stays in `documents`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from ..services.entry_guard import acquire_model, org_budget
from ..services.run_names import EntryErrorCode, StepEvent, StepStatus
from . import breaker
from .client import VENDORS, client_for
from .errors import BREAKER_KINDS, ModelCapacityExceeded, ProviderError, translate_provider_error
from .parse import parse_json_loose
from .types import AssistantTurn, ToolCall

log = logging.getLogger(__name__)

SCHOOL_CHAT = "deepseek-chat"
SCHOOL_REASONER = "deepseek-reasoner"
SCHOOL_PLAN = "minimax"
SCHOOL_VISION = "qwen"
DEEPSEEK_FLASH = "deepseek-flash"
QWEN_PLAN = "qwen3.7-flash"

Kind = Literal["chat_completion", "response"]
Expect = Literal["json", "text", "tools"]
Brand = Literal["sjtu", "deepseek", "glm", "qwen", "fake"]
# Official OpenAI-compatible APIs reject dots. Internal names keep them (`po.draft`).
_VENDOR_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


class ModelBudgetExceeded(Exception):
    """This run is already at its model or token cap. The vendor was not called."""

    def __init__(self) -> None:
        super().__init__(EntryErrorCode.COST_BUDGET_EXCEEDED)
        self.code = EntryErrorCode.COST_BUDGET_EXCEEDED


@dataclass
class ModelResult:
    brand: str
    model: str
    body: Any
    prompt_tokens: int
    completion_tokens: int
    usage_source: str
    error: str | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning: str | None = None


@dataclass
class _Transport:
    response: Any
    client: Any
    tool_alias: dict[str, str] = field(default_factory=dict)


@dataclass
class _Ledger:
    db: Any
    run_id: str
    step_id: str | None
    org_id: str
    trace_id: str | None
    purpose: str | None


def _estimate_tokens(text: str) -> int:
    """Stand-in count when the vendor omits usage. A CJK character is about one token."""
    cjk = other = 0
    for ch in text or "":
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
        else:
            other += 1
    return cjk + other // 4


def _prompt_text(system: str, messages: list[dict[str, Any]]) -> str:
    parts = [system]
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


def _ledger_brand(brand: Brand, client: Any) -> Brand:
    """FakeLLM is always recorded as brand fake, regardless of the wrapper's vendor."""
    from .fake import FakeLLM

    if isinstance(client, FakeLLM):
        return "fake"
    return brand


def _vendor_usage(client: Any) -> dict[str, int | None] | None:
    """返回模型实际使用的 token 数量，没有就返回 None"""
    usage = getattr(client, "last_usage", None)
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" not in usage and "completion_tokens" not in usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt == 0 and completion == 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": _optional_usage_int(usage.get("cache_hit_tokens")),
        "cache_miss_tokens": _optional_usage_int(usage.get("cache_miss_tokens")),
        "cache_creation_tokens": _optional_usage_int(usage.get("cache_creation_tokens")),
    }


def _optional_usage_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _tokens(
    client: Any, system: str, messages: list[dict[str, Any]], completion: str,
) -> tuple[int, int, str, dict[str, int | None]]:
    taken = _vendor_usage(client)
    if taken is not None:
        cache = {key: taken[key] for key in (
            "cache_hit_tokens", "cache_miss_tokens", "cache_creation_tokens",
        )}
        return int(taken["prompt_tokens"] or 0), int(taken["completion_tokens"] or 0), "actual", cache
    return (
        _estimate_tokens(_prompt_text(system, messages)), _estimate_tokens(completion), "estimated",
        {"cache_hit_tokens": None, "cache_miss_tokens": None, "cache_creation_tokens": None},
    )


def _school_text(resp: Any) -> str:
    try:
        if isinstance(resp, dict):
            return str(resp["choices"][0]["message"]["content"] or "")
        return str(resp.choices[0].message.content or "")
    except (KeyError, IndexError, TypeError, AttributeError) as err:
        raise ValueError("Campus model did not return a message.") from err


def _flash_input(messages: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if len(messages) == 1 and isinstance(messages[0].get("content"), str):
        return messages[0]["content"]
    return messages


def _tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_format(schema: dict[str, Any] | None) -> dict[str, Any]:
    if schema:
        return {"format": {"type": "json_schema", "name": schema["name"], "schema": schema["schema"]}}
    return {"format": {"type": "json_object"}}


def _await(result: Any) -> Any:
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _call_sjtu(
    client: Any, *, model: str, messages: list[dict[str, Any]], stream: bool,
    temperature: float | None, top_p: float | None,
    tools: list[dict[str, Any]] | None, tool_choice: str | None,
) -> Any:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if top_p is not None:
        kwargs["top_p"] = top_p
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    return client.chat_completion(**kwargs)


def _call_deepseek(
    client: Any, *, kind: Kind, model: str, system: str, messages: list[dict[str, Any]],
    stream: bool, temperature: float | None, top_p: float | None,
    schema: dict[str, Any] | None, tools: list[dict[str, Any]] | None,
    tool_choice: str | None, reasoning_effort: str | None,
) -> Any:
    if kind == "response":
        return client.response(
            model=model, instructions=system, input=_flash_input(messages),
            text=_json_format(schema), reasoning_effort=reasoning_effort,
            temperature=temperature, **({} if top_p is None else {"top_p": top_p}),
        )
    kwargs: dict[str, Any] = {"model": model, "messages": [{"role": "system", "content": system}, *messages]}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if top_p is not None:
        kwargs["top_p"] = top_p
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if stream:
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
    return client.chat_completion(**kwargs)


def _call_glm(
    client: Any, *, kind: Kind, model: str, system: str, messages: list[dict[str, Any]],
    stream: bool, temperature: float | None, schema: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None, tool_choice: str | None, reasoning_effort: str | None,
) -> Any:
    if kind != "response":
        raise ValueError("GLM chat goes through the Responses API")
    del schema
    kwargs: dict[str, Any] = {"model": model, "input": [{"role": "system", "content": system}, *messages]}
    if stream:
        kwargs["stream"] = True
    if temperature is not None:
        kwargs["temperature"] = temperature
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return client.response(**kwargs)


def _qwen_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not schema:
        return None
    body = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
    return {
        "type": "json_schema",
        "json_schema": {
            "name": str(schema.get("name") or "response"),
            "strict": bool(schema.get("strict", True)),
            "schema": body,
        },
    }


def _call_qwen(
    client: Any, *, kind: Kind, model: str, system: str, messages: list[dict[str, Any]],
    stream: bool, temperature: float | None, tools: list[dict[str, Any]] | None,
    tool_choice: str | None, reasoning_effort: str | None,
    schema: dict[str, Any] | None = None,
) -> Any:
    thinking = None
    if reasoning_effort == "none":
        thinking = {"enable_thinking": False}
    elif reasoning_effort:
        thinking = {"enable_thinking": True}
    if kind == "response":
        return _await(client.response(
            model=model, input=[{"role": "system", "content": system}, *messages],
            stream=stream, tools=tools, tool_choice=tool_choice,
            reasoning_effort=reasoning_effort, extra_body=thinking,
        ))
    kwargs: dict[str, Any] = {
        "model": model, "messages": [{"role": "system", "content": system}, *messages],
    }
    if stream:
        kwargs["stream"] = True
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    fmt = _qwen_schema(schema)
    if fmt is not None:
        kwargs["response_format"] = fmt
    if thinking:
        kwargs["extra_body"] = thinking
    del temperature
    return _await(client.chat_completion(**kwargs))


def _call_double(
    client: Any, *, kind: Kind, model: str, system: str, messages: list[dict[str, Any]],
    stream: bool, temperature: float | None, top_p: float | None,
    schema: dict[str, Any] | None, tools: list[dict[str, Any]] | None,
    tool_choice: str | None, reasoning_effort: str | None,
) -> Any:
    """FakeLLM and the other test doubles. They are not one of the four clients."""
    # 区分 chat completion & response 的传参
    if kind == "response":
        response = getattr(client, "response", None)
        if response is None:
            raise TypeError(f"{type(client).__name__} cannot answer a Responses call")
        return _await(response(
            model=model, instructions=system, input=_flash_input(messages),
            text=_json_format(schema), reasoning_effort=reasoning_effort,
            temperature=temperature, **({} if top_p is None else {"top_p": top_p}),
        ))
    chat = getattr(client, "chat_completion", None)
    if chat is None:
        raise TypeError(f"{type(client).__name__} cannot answer a run")

    # 其他形式传参
    kwargs: dict[str, Any] = {
        "model": model, "messages": [{"role": "system", "content": system}, *messages],
    }
    extras = {
        "temperature": temperature, "top_p": top_p, "tools": tools,
        "tool_choice": (tool_choice or "auto") if tools else None,
        "reasoning_effort": reasoning_effort, "stream": True if stream else None,
    }
    params = inspect.signature(chat).parameters
    accept_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    for key, value in extras.items():
        if value is None:
            continue
        if accept_any or key in params:
            kwargs[key] = value
    return _await(chat(**kwargs))


def _assemble_stream(chunks: Any, on_text: Callable[..., None]) -> Any:
    parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    for chunk in chunks:
        delta = _choice_delta(chunk)
        piece = _delta_text(delta)
        thought = _delta_reasoning(delta)
        if thought:
            reasoning_parts.append(thought)
        if piece:
            parts.append(piece)
        if piece or thought:
            on_text("".join(parts), reasoning="".join(reasoning_parts))
        _take_tool_delta(calls, delta)
    message = SimpleNamespace(
        content="".join(parts) or None,
        reasoning_content="".join(reasoning_parts) or None,
        tool_calls=[_tool_namespace(calls[index]) for index in sorted(calls)] or None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _choice_delta(chunk: Any) -> Any:
    if isinstance(chunk, dict):
        choices = chunk.get("choices") or []
        if not choices:
            return None
        return (choices[0] or {}).get("delta")
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "delta", None)


def _delta_text(delta: Any) -> str:
    if delta is None:
        return ""
    content = delta.get("content") if isinstance(delta, dict) else getattr(delta, "content", None)
    return content if isinstance(content, str) else ""


def _delta_reasoning(delta: Any) -> str:
    if delta is None:
        return ""
    content = (
        delta.get("reasoning_content")
        if isinstance(delta, dict)
        else getattr(delta, "reasoning_content", None)
    )
    return content if isinstance(content, str) else ""


def _extract_reasoning(raw: Any) -> str | None:
    if isinstance(raw, AssistantTurn):
        value = getattr(raw, "reasoning_content", None)
        return value.strip() if isinstance(value, str) and value.strip() else None
    message: Any = None
    if isinstance(raw, dict):
        choices = raw.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
    else:
        choices = getattr(raw, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
    value = (
        message.get("reasoning_content")
        if isinstance(message, dict)
        else getattr(message, "reasoning_content", None)
    )
    return value.strip() if isinstance(value, str) and value.strip() else None


def _take_tool_delta(calls: dict[int, dict[str, str]], delta: Any) -> None:
    if delta is None:
        return
    raw = delta.get("tool_calls") if isinstance(delta, dict) else getattr(delta, "tool_calls", None)
    for call in raw or []:
        if isinstance(call, dict):
            index = int(call.get("index") or 0)
            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call.get("id"):
                slot["id"] = str(call["id"])
            function = call.get("function") or {}
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])
            continue
        index = int(getattr(call, "index", 0) or 0)
        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if getattr(call, "id", None):
            slot["id"] = str(call.id)
        function = getattr(call, "function", None)
        name = getattr(function, "name", None) if function is not None else None
        arguments = getattr(function, "arguments", None) if function is not None else None
        if name:
            slot["name"] = str(name)
        if arguments:
            slot["arguments"] += str(arguments)


def _safe_tool_name(name: str) -> str | None:
    safe = name.replace(".", "_")
    return safe if _VENDOR_TOOL_NAME.fullmatch(safe) else None


def _tools_for_vendor(tools: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]] | None, dict[str, str]]:
    """Post names the vendor accepts. The alias maps posted name → internal name."""
    if not tools:
        return tools, {}
    alias: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function")
        if not isinstance(func, dict):
            out.append(tool)
            continue
        name = func.get("name")
        if not isinstance(name, str):
            continue
        safe = _safe_tool_name(name)
        if safe is None:
            continue
        if safe != name:
            alias[safe] = name
        out.append({**tool, "function": {**func, "name": safe}})
    return out, alias


def _messages_for_vendor(messages: list[dict[str, Any]], alias: dict[str, str]) -> list[dict[str, Any]]:
    posted = {original: safe for safe, original in alias.items()}
    rewritten: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or not msg.get("tool_calls"):
            rewritten.append(msg)
            continue
        calls = []
        for call in msg["tool_calls"]:
            if not isinstance(call, dict):
                calls.append(call)
                continue
            func = dict(call.get("function") or {})
            name = func.get("name")
            if isinstance(name, str):
                safe = posted.get(name) or _safe_tool_name(name)
                if safe:
                    func = {**func, "name": safe}
                    if safe != name:
                        alias.setdefault(safe, name)
            calls.append({**call, "function": func})
        rewritten.append({**msg, "tool_calls": calls})
    return rewritten


def _restore_tool_name(name: str, alias: dict[str, str]) -> str:
    return alias.get(name, name)


def _tool_namespace(slot: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=slot["id"],
        function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
    )


def _invoke(
    *,
    brand: str,
    model_name: str,
    kind: Kind,
    expect: Expect,
    temperature: float | None,
    top_p: float | None,
    system: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | None,
    reasoning_effort: str | None,
    client: Any,
    on_text: Callable[[str], None] | None = None,
) -> _Transport:
    """One vendor call. Tests replace this; the ledger in `call_model` still runs."""
    stream = on_text is not None and kind == "chat_completion"
    alias: dict[str, str] = {}
    if not isinstance(client, VENDORS):
        response = _call_double(
            client, kind=kind, model=model_name, system=system, messages=messages,
            stream=stream, temperature=temperature, top_p=top_p, schema=schema,
            tools=tools, tool_choice=tool_choice, reasoning_effort=reasoning_effort,
        )
    else:
        tools, alias = _tools_for_vendor(tools)
        messages = _messages_for_vendor(messages, alias)
        if brand == "sjtu":
            response = _call_sjtu(
                client, model=model_name,
                messages=[{"role": "system", "content": system}, *messages],
                stream=stream, temperature=temperature, top_p=top_p,
                tools=tools, tool_choice=tool_choice,
            )
        elif brand == "deepseek":
            response = _call_deepseek(
                client, kind=kind, model=model_name, system=system, messages=messages,
                stream=stream, temperature=temperature, top_p=top_p, schema=schema,
                tools=tools, tool_choice=tool_choice, reasoning_effort=reasoning_effort,
            )
        elif brand == "glm":
            response = _call_glm(
                client, kind=kind, model=model_name, system=system, messages=messages,
                stream=stream, temperature=temperature, schema=schema,
                tools=tools, tool_choice=tool_choice, reasoning_effort=reasoning_effort,
            )
        elif brand == "qwen":
            response = _call_qwen(
                client, kind=kind, model=model_name, system=system, messages=messages,
                stream=stream, temperature=temperature, tools=tools,
                tool_choice=tool_choice, reasoning_effort=reasoning_effort,
                schema=schema,
            )
        else:
            raise ValueError(f"unknown model brand {brand}")
    if stream:
        return _Transport(response=_assemble_stream(response, on_text), client=client, tool_alias=alias)
    return _Transport(response=response, client=client, tool_alias=alias)


def _interpret(expect: Expect, raw: Any, alias: dict[str, str] | None = None) -> tuple[Any, str]:
    alias = alias or {}
    """解释三种不同格式的返回值，最终返回（结构化结果，原始文本）"""
    # 工具调用改写（po.draft -> po_draft)
    if expect == "tools":
        if isinstance(raw, AssistantTurn):
            if not alias:
                return raw, raw.content or ""
            restored = AssistantTurn(
                content=raw.content,
                tool_calls=[
                    ToolCall(id=c.id, name=_restore_tool_name(c.name, alias), arguments=c.arguments)
                    for c in raw.tool_calls
                ],
                prompt_tokens=raw.prompt_tokens,
                completion_tokens=raw.completion_tokens,
                cache_hit_tokens=raw.cache_hit_tokens,
                cache_miss_tokens=raw.cache_miss_tokens,
                cache_creation_tokens=raw.cache_creation_tokens,
            )
            return restored, raw.content or ""
        message = raw.choices[0].message
        calls = [
            ToolCall(
                id=call.id,
                name=_restore_tool_name(call.function.name, alias),
                arguments=_tool_arguments(call.function.arguments),
            )
            for call in (message.tool_calls or [])
        ]
        turn = AssistantTurn(content=message.content, tool_calls=calls)
        return turn, message.content or ""

    # 文本类型：非 string 返回 string
    if expect == "text":
        if isinstance(raw, str):
            return raw.strip(), raw.strip()
        if hasattr(raw, "__iter__") and not isinstance(raw, (dict, list, tuple)):
            text = "".join(str(part) for part in raw).strip()
            return text, text
        text = _school_text(raw).strip()
        return text, text

    # 结构化 JSON：直接返回
    if isinstance(raw, dict) and "choices" not in raw and not hasattr(raw, "output_text"): # 没有 `choices` 键、也没有 `output_text` 属性：说明调用方直接拿到了已解析的对象，不用再解析
        return raw, json.dumps(raw, ensure_ascii=False)
    text = str(raw.output_text or "") if hasattr(raw, "output_text") else _school_text(raw)
    parsed = parse_json_loose(text)
    if not isinstance(parsed, dict):
        raise ValueError("model did not return a JSON object")
    return parsed, text


def _budget(db: Any, run_id: str | None, step_id: str | None, purpose: str | None) -> _Ledger | ModelResult | None:
    if db is None or not run_id or not step_id:
        return None
    spend = db.model_budget(run_id)
    if spend is None:
        return None
    over_calls = spend["max_model_calls"] is not None and spend["model_calls"] >= spend["max_model_calls"]
    over_tokens = spend["max_tokens"] is not None and spend["total_tokens"] >= spend["max_tokens"]
    over_org = org_budget(db, spend["org_id"], run_id) == "over"
    if over_calls or over_tokens or over_org:
        db.append_run_event(spend["org_id"], run_id, StepEvent.MODEL_FAILED, {
            "purpose": purpose,
            "trace_id": spend["trace_id"],
            "error": EntryErrorCode.COST_BUDGET_EXCEEDED,
            "status": StepStatus.FAILED,
        })
        log.info(
            "model budget exceeded trace_id=%s run_id=%s",
            spend["trace_id"], run_id,
        )
        return ModelResult(
            brand="", model="", body=None, prompt_tokens=0, completion_tokens=0,
            usage_source="estimated", error=EntryErrorCode.COST_BUDGET_EXCEEDED,
        )
    return _Ledger(
        db=db, run_id=run_id, step_id=step_id, org_id=spend["org_id"],
        trace_id=spend["trace_id"], purpose=purpose,
    )


def _note(ledger: _Ledger, event_type: str, payload: dict[str, Any]) -> None:
    ledger.db.append_run_event(ledger.org_id, ledger.run_id, event_type, payload)


def _request(
    model_name: str, system: str, messages: list[dict[str, Any]],
    schema: dict[str, Any] | None, tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {"model": model_name, "system": system, "messages": messages}
    if schema is not None:
        request["schema"] = schema
    if tools is not None:
        request["tools"] = tools
    return request


def _save(
    ledger: _Ledger | None,
    *,
    provider: str,
    model_name: str,
    system: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None,
    response: dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int | None,
    cache_miss_tokens: int | None,
    cache_creation_tokens: int | None,
    usage_source: str,
    latency_ms: int,
    status: str,
    error: Exception | None,
) -> str | None:
    if ledger is None or not ledger.step_id:
        return None
    error_json = None
    if error is not None:
        error_json = {"message": str(error)}
        code = getattr(error, "code", None)
        if code is not None:
            error_json["code"] = str(code)
        if isinstance(error, ModelCapacityExceeded):
            error_json["retryable"] = True
        if isinstance(error, ProviderError):
            error_json["retryable"] = error.retryable
            error_json["kind"] = error.kind
            if error.status_code is not None:
                error_json["status_code"] = error.status_code
            if error.retry_after_seconds is not None:
                error_json["retry_after_seconds"] = error.retry_after_seconds
            if error.request_id is not None:
                error_json["request_id"] = error.request_id
    row = ledger.db.insert_model_call(
        ledger.run_id, ledger.step_id,
        provider=provider,
        model=model_name,
        request=_request(model_name, system, messages, schema, tools),
        response=response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        cache_creation_tokens=cache_creation_tokens,
        usage_source=usage_source,
        latency_ms=latency_ms,
        status=status,
        error=error_json,
        emit_event=False,
    )
    if row is None:
        return None
    return str(row.get("id") or "") or None


def _fail(
    ledger: _Ledger | None,
    *,
    brand: str,
    model_name: str,
    system: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None,
    client: Any,
    started: float,
    error: Exception,
    purpose: str | None,
) -> None:
    if ledger is None:
        return
    prompt, completion, source, cache = _tokens(client, system, messages, "")
    call_id = _save(
        ledger, provider=brand, model_name=model_name, system=system, messages=messages,
        schema=schema, tools=tools, response={}, prompt_tokens=prompt, completion_tokens=completion,
        cache_hit_tokens=cache["cache_hit_tokens"], cache_miss_tokens=cache["cache_miss_tokens"],
        cache_creation_tokens=cache["cache_creation_tokens"],
        usage_source=source, latency_ms=int((time.perf_counter() - started) * 1000),
        status=StepStatus.FAILED, error=error,
    )
    payload: dict[str, Any] = {
        "purpose": purpose,
        "trace_id": ledger.trace_id,
        "model": model_name,
        "brand": brand,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": cache["cache_hit_tokens"],
        "cache_miss_tokens": cache["cache_miss_tokens"],
        "cache_creation_tokens": cache["cache_creation_tokens"],
        "usage_source": source,
        "status": StepStatus.FAILED,
        "step_id": ledger.step_id,
    }
    error_code = getattr(error, "code", None)
    if error_code is not None:
        payload["error"] = str(error_code)
    if isinstance(error, ModelCapacityExceeded):
        payload["retryable"] = True
    if isinstance(error, ProviderError):
        payload["retryable"] = error.retryable
        payload["kind"] = error.kind
    if call_id:
        payload["model_call_id"] = call_id
    _note(ledger, StepEvent.MODEL_FAILED, payload)
    log.info(
        "model.failed trace_id=%s run_id=%s model=%s",
        ledger.trace_id, ledger.run_id, model_name,
    )


def _capacity_failed(
    ledger: _Ledger | None, *, brand: str, model_name: str, purpose: str | None,
    error: Exception,
) -> None:
    """Record a refused provider entry without charging the model budget."""
    if ledger is None:
        return
    code = str(getattr(error, "code", None) or EntryErrorCode.RATE_LIMITED)
    payload: dict[str, Any] = {
        "purpose": purpose,
        "trace_id": ledger.trace_id,
        "model": model_name,
        "brand": brand,
        "error": code,
        "retryable": bool(getattr(error, "retryable", True)),
        "status": StepStatus.FAILED,
        "step_id": ledger.step_id,
    }
    kind = getattr(error, "kind", None)
    if kind is not None:
        payload["kind"] = str(kind)
    _note(ledger, StepEvent.MODEL_FAILED, payload)


def call_model(
    model_name: str,
    *,
    brand: Brand,
    kind: Kind,
    client: Any | None = None,
    expect: Expect = "json",
    temperature: float | None = None,
    top_p: float | None = None,
    system: str = "",
    messages: list[dict[str, Any]] | None = None,
    schema: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    reasoning_effort: str | None = None,
    purpose: str | None = None,
    on_text: Callable[..., None] | None = None,
    db: Any | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
) -> ModelResult:
    """Call one model and, when a run is passed, write its row, usage, and events.

    `brand` selects the vendor client. A test double is used as given. Over
    `max_model_calls` or `max_tokens` the vendor is not called. The result's
    `error` is `COST_BUDGET_EXCEEDED`. A null cap does not block the call.
    A full provider gate raises `ModelCapacityExceeded` so the worker can
    requeue the run instead of turning an empty model result into an answer.
    """
    if not str(model_name or "").strip():
        raise ValueError("model is required")

    # 传入 message，model，是否 over budget or rare limit
    messages = list(messages or [])
    used = client_for(brand, client)
    brand = _ledger_brand(brand, used)
    gate = _budget(db, run_id, step_id, purpose)
    if isinstance(gate, ModelResult):
        return gate
    ledger = gate

    # 写入 model.started 事件
    if ledger is not None:
        _note(ledger, StepEvent.MODEL_STARTED, {
            "display": "正在思考...",
            "purpose": purpose,
            "trace_id": ledger.trace_id,
            "model": model_name,
            "brand": brand,
            "step_id": step_id,
        })
        log.info(
            "model.started trace_id=%s run_id=%s model=%s purpose=%s",
            ledger.trace_id, run_id, model_name, purpose,
        )

    started = time.perf_counter()
    ticket = None if brand == "fake" else breaker.acquire(brand)
    if ticket is not None and not ticket.allowed:
        closed = ProviderError(
            brand, model_name, "circuit_open",
            retry_after_seconds=ticket.retry_after_seconds, retryable=True,
        )
        _capacity_failed(
            ledger, brand=brand, model_name=model_name, purpose=purpose, error=closed,
        )
        raise closed

    held = acquire_model(brand)
    if held is None:
        breaker.release(ticket)
        busy = ModelCapacityExceeded(brand, model_name)
        _capacity_failed(
            ledger, brand=brand, model_name=model_name, purpose=purpose, error=busy,
        )
        raise busy

    generation_id = uuid.uuid4().hex

    def emit(text: str, *, reasoning: str | None = None) -> None:
        _emit_text(on_text, text, generation_id, reasoning)

    # 调用模型
    try:
        carried = _invoke(
            brand=brand, model_name=model_name, kind=kind, expect=expect,
            temperature=temperature, top_p=top_p, system=system, messages=messages,
            schema=schema, tools=tools, tool_choice=tool_choice,
            reasoning_effort=reasoning_effort, client=used,
            on_text=emit if on_text is not None else None,
        )
    except Exception as exc:
        error = _transport_error(exc, brand=brand, model_name=model_name, client=used)
        _record_breaker(brand, error)
        _fail(
            ledger, brand=brand, model_name=model_name, system=system, messages=messages,
            schema=schema, tools=tools, client=used, started=started, error=error, purpose=purpose,
        )
        raise error from exc
    finally:
        held.release()

    if held.lost:
        lost = ModelCapacityExceeded(brand, model_name, "model lease was lost while calling the provider")
        _fail(
            ledger, brand=brand, model_name=model_name, system=system, messages=messages,
            schema=schema, tools=tools, client=used, started=started, error=lost, purpose=purpose,
        )
        raise lost

    if brand != "fake":
        breaker.record_success(brand)

    # 解释返回值
    try:
        body, text = _interpret(expect, carried.response, getattr(carried, "tool_alias", None))
        reasoning = _extract_reasoning(carried.response)
    except Exception as exc:
        _fail(
            ledger, brand=_ledger_brand(brand, carried.client), model_name=model_name,
            system=system, messages=messages, schema=schema, tools=tools, client=carried.client,
            started=started, error=exc, purpose=purpose,
        )
        raise
    streamed = kind == "chat_completion" and on_text is not None

    # 非流式回调：发送文本
    if on_text is not None and not streamed:
        emit(text, reasoning=reasoning)
    prompt, completion, source, cache = _tokens(carried.client, system, messages, text)

    # 记录模型数据
    brand = _ledger_brand(brand, carried.client) # 看是不是 FakeLLM
    response: dict[str, Any]
    if expect == "json" and isinstance(body, dict):
        response = {"json": body}
    elif expect == "tools":
        response = {"content": body.content, "tool_calls": [asdict(call) for call in body.tool_calls]}
    else:
        response = {"text": text}
    if reasoning:
        response["reasoning"] = reasoning
    call_id = _save(
        ledger, provider=brand, model_name=model_name, system=system, messages=messages,
        schema=schema, tools=tools, response=response, prompt_tokens=prompt, completion_tokens=completion,
        cache_hit_tokens=cache["cache_hit_tokens"], cache_miss_tokens=cache["cache_miss_tokens"],
        cache_creation_tokens=cache["cache_creation_tokens"],
        usage_source=source, latency_ms=int((time.perf_counter() - started) * 1000),
        status=StepStatus.SUCCEEDED, error=None,
    )
    if ledger is not None:
        completed: dict[str, Any] = {
            "purpose": purpose,
            "trace_id": ledger.trace_id,
            "model": model_name,
            "brand": brand,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_hit_tokens": cache["cache_hit_tokens"],
            "cache_miss_tokens": cache["cache_miss_tokens"],
            "cache_creation_tokens": cache["cache_creation_tokens"],
            "usage_source": source,
            "status": StepStatus.SUCCEEDED,
            "step_id": step_id,
        }
        if call_id:
            completed["model_call_id"] = call_id
        _note(ledger, StepEvent.MODEL_COMPLETED, completed)
        log.info(
            "model.completed trace_id=%s run_id=%s model=%s usage_source=%s",
            ledger.trace_id, run_id, model_name, source,
        )
    return ModelResult(
        brand=brand, model=model_name, body=body,
        prompt_tokens=prompt, completion_tokens=completion, usage_source=source,
        cache_hit_tokens=cache["cache_hit_tokens"], cache_miss_tokens=cache["cache_miss_tokens"],
        cache_creation_tokens=cache["cache_creation_tokens"],
        reasoning=reasoning,
    )


def _unwrap(result: ModelResult) -> Any:
    if result.error == EntryErrorCode.COST_BUDGET_EXCEEDED:
        raise ModelBudgetExceeded()
    return result.body


def _emit_text(
    on_text: Callable[..., None] | None,
    text: str,
    generation_id: str,
    reasoning: str | None = None,
) -> None:
    if on_text is None or (not text and not reasoning):
        return
    try:
        on_text(text, generation_id=generation_id, reasoning=reasoning)
    except TypeError:
        try:
            on_text(text, generation_id=generation_id)
        except TypeError:
            on_text(text)


def _transport_error(exc: Exception, *, brand: str, model_name: str, client: Any) -> Exception:
    if isinstance(exc, (ProviderError, ModelCapacityExceeded, ModelBudgetExceeded)):
        return exc
    if isinstance(client, VENDORS):
        return translate_provider_error(exc, provider=brand, model=model_name)
    return exc


def _record_breaker(brand: str, error: Exception) -> None:
    if brand == "fake" or not isinstance(error, ProviderError):
        return
    if error.kind in BREAKER_KINDS:
        breaker.record_failure(brand)
    else:
        breaker.release_probe(brand)


def school_json(
    model: str, *, client: Any | None = None, system: str, messages: list[dict[str, Any]],
    schema: dict[str, Any] | None = None, temperature: float = 0,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Campus chat. The gateway has no JSON schema; the reply text is the object."""
    # Thinking is on for this model name, so leave temperature at the gateway default.
    return _unwrap(call_model(
        model, brand="sjtu", kind="chat_completion", expect="json",
        temperature=None if model == SCHOOL_REASONER else temperature,
        system=system, messages=messages, schema=schema, client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))


def school_text(
    model: str, *, client: Any | None = None, system: str, messages: list[dict[str, Any]],
    temperature: float = 0, on_text: Callable[..., None] | None = None,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> Iterator[str]:
    """Campus chat, one chunk. The desk still renders it as reply tokens."""
    text = _unwrap(call_model(
        model, brand="sjtu", kind="chat_completion", expect="text", temperature=temperature,
        system=system, messages=messages, client=client, on_text=on_text,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))
    if text:
        yield text


def qwen_json(
    *, client: Any | None = None, system: str, messages: list[dict[str, Any]],
    schema: dict[str, Any] | None = None, temperature: float = 0,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Official qwen3.7-flash. Thinking off. A schema is sent when the caller has one."""
    return _unwrap(call_model(
        QWEN_PLAN, brand="qwen", kind="chat_completion", expect="json", temperature=temperature,
        system=system, messages=messages, schema=schema, reasoning_effort="none", client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))


def flash_json(
    *, client: Any | None = None, system: str, messages: list[dict[str, Any]],
    schema: dict[str, Any] | None = None, temperature: float = 0,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Official deepseek-flash, thinking off. A schema is sent when the caller has one."""
    return _unwrap(call_model(
        DEEPSEEK_FLASH, brand="deepseek", kind="response", expect="json", temperature=temperature,
        system=system, messages=messages, schema=schema, reasoning_effort="none", client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))


def flash_tools(
    *, client: Any | None = None, system: str, messages: list[dict[str, Any]],
    tools: list[dict[str, Any]], on_text: Callable[..., None] | None = None,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> AssistantTurn:
    """Official deepseek-flash function calls. Thinking stays off."""
    return _unwrap(call_model(
        DEEPSEEK_FLASH, brand="deepseek", kind="chat_completion", expect="tools", temperature=0,
        system=system, messages=messages, tools=tools, tool_choice="auto",
        reasoning_effort="none", client=client, on_text=on_text,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))
