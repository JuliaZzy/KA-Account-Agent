"""One place that picks a model and calls it.

Campus `deepseek-chat` is the small text model. Campus `deepseek-reasoner` is the
same model with thinking on, for report planning and prose. Campus `minimax` plans
a turn. Campus `qwen` reads an order photo. Official `deepseek-flash` writes strict
JSON, Python, and tool calls, with thinking off. None of these calls set `max_tokens`.

`call_model` is the only entry that calls a client and writes the run ledger.
The caller passes the client. The four wrappers keep the shapes callers already
expect. Vendors usually
send token counts on the final response. A stream, or a gateway that drops usage,
leaves both counts at 0. Those calls are estimated and labeled `estimated`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ..services.entry_guard import acquire_model, org_budget, release_model
from ..services.run_names import EntryErrorCode, StepEvent, StepStatus
from .parse import parse_json_loose
from .types import AssistantTurn, ToolCall

log = logging.getLogger(__name__)

SCHOOL_CHAT = "deepseek-chat"
SCHOOL_REASONER = "deepseek-reasoner"
SCHOOL_PLAN = "minimax"
SCHOOL_VISION = "qwen"
DEEPSEEK_FLASH = "deepseek-flash"

Kind = Literal["chat_completion", "response"]
Expect = Literal["json", "text", "tools"]


class ModelBudgetExceeded(Exception):
    """This run is already at its model or token cap. The vendor was not called."""

    def __init__(self) -> None:
        super().__init__(EntryErrorCode.COST_BUDGET_EXCEEDED)
        self.code = EntryErrorCode.COST_BUDGET_EXCEEDED


@dataclass
class ModelResult:
    provider: str
    model: str
    body: Any
    prompt_tokens: int
    completion_tokens: int
    usage_source: str
    error: str | None = None


@dataclass
class _Transport:
    response: Any
    client: Any


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


def _provider(model_name: str, client: Any) -> str:
    named = getattr(client, "provider", None)
    if isinstance(named, str) and named.strip():
        return named
    if model_name == DEEPSEEK_FLASH:
        return "ds"
    return "sjtu"


def _vendor_usage(client: Any) -> tuple[int, int] | None:
    """Counts the vendor actually sent. Both zeros means the usage object was empty."""
    usage = getattr(client, "last_usage", None)
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" not in usage and "completion_tokens" not in usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt == 0 and completion == 0:
        return None
    return prompt, completion


def _tokens(client: Any, system: str, messages: list[dict[str, Any]], completion: str) -> tuple[int, int, str]:
    taken = _vendor_usage(client)
    if taken is not None:
        return taken[0], taken[1], "actual"
    return _estimate_tokens(_prompt_text(system, messages)), _estimate_tokens(completion), "estimated"


def _school_text(resp: Any) -> str:
    try:
        if isinstance(resp, dict):
            return str(resp["choices"][0]["message"]["content"] or "")
        return str(resp.choice[0].message.content or "")
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


def _invoke(
    *,
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
) -> _Transport:
    """One vendor call. Tests replace this; the ledger in `call_model` still runs."""
    if kind == "response":
        response = client.response(
            model=model_name,
            instructions=system,
            input=_flash_input(messages),
            text=_json_format(schema),
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            **({} if top_p is None else {"top_p": top_p}),
        )
        return _Transport(response=response, client=client)
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": temperature,
    }
    if top_p is not None:
        payload["top_p"] = top_p
    if expect == "tools":
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
        payload["reasoning_effort"] = reasoning_effort
    return _Transport(response=client.chat_completion(**payload), client=client)


def _interpret(expect: Expect, raw: Any) -> tuple[Any, str]:
    if expect == "tools":
        if isinstance(raw, AssistantTurn):
            return raw, raw.content or ""
        message = raw.choices[0].message
        calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=_tool_arguments(call.function.arguments))
            for call in (message.tool_calls or [])
        ]
        turn = AssistantTurn(content=message.content, tool_calls=calls)
        return turn, message.content or ""
    if expect == "text":
        if isinstance(raw, str):
            return raw.strip(), raw.strip()
        if hasattr(raw, "__iter__") and not isinstance(raw, (dict, list, tuple)):
            text = "".join(str(part) for part in raw).strip()
            return text, text
        text = _school_text(raw).strip()
        return text, text
    if isinstance(raw, dict) and "choices" not in raw and not hasattr(raw, "output_text"):
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
            provider="", model="", body=None, prompt_tokens=0, completion_tokens=0,
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
    usage_source: str,
    latency_ms: int,
    status: str,
    error: Exception | None,
) -> str | None:
    if ledger is None or not ledger.step_id:
        return None
    row = ledger.db.insert_model_call(
        ledger.run_id, ledger.step_id,
        provider=provider,
        model=model_name,
        request=_request(model_name, system, messages, schema, tools),
        response=response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_source=usage_source,
        latency_ms=latency_ms,
        status=status,
        error={"message": str(error)} if error is not None else None,
        emit_event=False,
    )
    if row is None:
        return None
    return str(row.get("id") or "") or None


def _fail(
    ledger: _Ledger | None,
    *,
    provider: str,
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
    prompt, completion, source = _tokens(client, system, messages, "")
    call_id = _save(
        ledger, provider=provider, model_name=model_name, system=system, messages=messages,
        schema=schema, tools=tools, response={}, prompt_tokens=prompt, completion_tokens=completion,
        usage_source=source, latency_ms=int((time.perf_counter() - started) * 1000),
        status=StepStatus.FAILED, error=error,
    )
    payload: dict[str, Any] = {
        "purpose": purpose,
        "trace_id": ledger.trace_id,
        "model": model_name,
        "provider": provider,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "usage_source": source,
        "status": StepStatus.FAILED,
        "step_id": ledger.step_id,
    }
    if call_id:
        payload["model_call_id"] = call_id
    _note(ledger, StepEvent.MODEL_FAILED, payload)
    log.info(
        "model.failed trace_id=%s run_id=%s model=%s",
        ledger.trace_id, ledger.run_id, model_name,
    )


def call_model(
    model_name: str,
    *,
    kind: Kind,
    client: Any,
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
    db: Any | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
) -> ModelResult:
    """Call one model and, when a run is passed, write its row, usage, and events.

    Over `max_model_calls` or `max_tokens` the vendor is not called. The result's
    `error` is `COST_BUDGET_EXCEEDED`. A null cap does not block the call.
    """
    messages = list(messages or [])
    gate = _budget(db, run_id, step_id, purpose)
    if isinstance(gate, ModelResult):
        return gate
    ledger = gate
    provider = _provider(model_name, client)
    if ledger is not None:
        _note(ledger, StepEvent.MODEL_STARTED, {
            "display": "正在思考",
            "purpose": purpose,
            "trace_id": ledger.trace_id,
            "model": model_name,
            "provider": provider,
            "step_id": step_id,
        })
        log.info(
            "model.started trace_id=%s run_id=%s model=%s purpose=%s",
            ledger.trace_id, run_id, model_name, purpose,
        )
    started = time.perf_counter()
    held = acquire_model(provider)
    if not held:
        return ModelResult(
            provider=provider, model=model_name, body=None, prompt_tokens=0, completion_tokens=0,
            usage_source="estimated", error=EntryErrorCode.RATE_LIMITED,
        )
    try:
        carried = _invoke(
            model_name=model_name, kind=kind, expect=expect, temperature=temperature, top_p=top_p,
            system=system, messages=messages, schema=schema, tools=tools, tool_choice=tool_choice,
            reasoning_effort=reasoning_effort, client=client,
        )
    except Exception as exc:
        _fail(
            ledger, provider=provider, model_name=model_name, system=system, messages=messages,
            schema=schema, tools=tools, client=client, started=started, error=exc, purpose=purpose,
        )
        raise
    finally:
        release_model(provider)
    try:
        body, text = _interpret(expect, carried.response)
    except Exception as exc:
        _fail(
            ledger, provider=_provider(model_name, carried.client), model_name=model_name,
            system=system, messages=messages, schema=schema, tools=tools, client=carried.client,
            started=started, error=exc, purpose=purpose,
        )
        raise
    prompt, completion, source = _tokens(carried.client, system, messages, text)
    provider = _provider(model_name, carried.client)
    response: dict[str, Any]
    if expect == "json" and isinstance(body, dict):
        response = {"json": body}
    elif expect == "tools":
        response = {"content": body.content, "tool_calls": [asdict(call) for call in body.tool_calls]}
    else:
        response = {"text": text}
    call_id = _save(
        ledger, provider=provider, model_name=model_name, system=system, messages=messages,
        schema=schema, tools=tools, response=response, prompt_tokens=prompt, completion_tokens=completion,
        usage_source=source, latency_ms=int((time.perf_counter() - started) * 1000),
        status=StepStatus.SUCCEEDED, error=None,
    )
    if ledger is not None:
        completed: dict[str, Any] = {
            "purpose": purpose,
            "trace_id": ledger.trace_id,
            "model": model_name,
            "provider": provider,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
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
        provider=provider, model=model_name, body=body,
        prompt_tokens=prompt, completion_tokens=completion, usage_source=source,
    )


def _unwrap(result: ModelResult) -> Any:
    if result.error == EntryErrorCode.COST_BUDGET_EXCEEDED:
        raise ModelBudgetExceeded()
    return result.body


def school_json(
    model: str, *, client: Any, system: str, messages: list[dict[str, Any]],
    schema: dict[str, Any] | None = None, temperature: float = 0,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Campus chat. The gateway has no JSON schema; the reply text is the object."""
    # Thinking is on for this model name, so leave temperature at the gateway default.
    return _unwrap(call_model(
        model, kind="chat_completion", expect="json",
        temperature=None if model == SCHOOL_REASONER else temperature,
        system=system, messages=messages, schema=schema, client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))


def school_text(
    model: str, *, client: Any, system: str, messages: list[dict[str, Any]], temperature: float = 0,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> Iterator[str]:
    """Campus chat, one chunk. The desk still renders it as reply tokens."""
    text = _unwrap(call_model(
        model, kind="chat_completion", expect="text", temperature=temperature,
        system=system, messages=messages, client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))
    if text:
        yield text


def flash_json(
    *, client: Any, system: str, messages: list[dict[str, Any]],
    schema: dict[str, Any] | None = None, temperature: float = 0,
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Official deepseek-flash, thinking off. A schema is sent when the caller has one."""
    return _unwrap(call_model(
        DEEPSEEK_FLASH, kind="response", expect="json", temperature=temperature,
        system=system, messages=messages, schema=schema, reasoning_effort="none", client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))


def flash_tools(
    *, client: Any, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    db: Any | None = None, run_id: str | None = None, step_id: str | None = None,
    purpose: str | None = None,
) -> AssistantTurn:
    """Official deepseek-flash function calls. Thinking stays off."""
    return _unwrap(call_model(
        DEEPSEEK_FLASH, kind="chat_completion", expect="tools", temperature=0,
        system=system, messages=messages, tools=tools, tool_choice="auto",
        reasoning_effort="none", client=client,
        db=db, run_id=run_id, step_id=step_id, purpose=purpose,
    ))
