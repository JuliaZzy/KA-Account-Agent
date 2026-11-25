"""In-process tools= loop for tool unit tests. Production chat uses PLAN + child runs."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from backend.llm import calls as model_calls
from backend.llm.errors import ModelCapacityExceeded, ProviderError
from backend.llm.types import AssistantTurn
from backend.memory import attach, prompt_turns
from backend.tools import all_schemas, all_tools

MAX_TOOL_ROUNDS = 4

# tools= loop only; chat parent runs use plan_system() instead.
REACT = """你是一个带工具的工作台助手。请根据用户的提问，选择适合的工具来回答问题，如果没有适合的工具，就直接回答用户的问题。

## 可用工具：
- web_search / translate / exchange_rate / send_email（以及提示里出现的 MCP）
- po_draft — 起草或改采购单。价格、折扣、MOQ 由代码算，你不要算。
- contract_read — 读上传的协议。没有文件时会要求文件。
- report_draft — 起草业绩报告。数字来自查询，不是你编的。
- data_query — 查这个合作关系表里的行。
- brand_search — 检索品牌资料段落。

## 回答要求：
1. 不要编造 SKU、价格、总额、汇率或合同折扣。只有用户明确要求发邮件时才调用 send_email。
2. 彼此独立的查询可以一次并行调用多个工具。有先后依赖时先调一个，看到结果再调下一个。
"""


def _run_one_tool(
    call: Any, runners: dict[str, Any], llm: Any, context: dict[str, Any],
) -> dict[str, Any]:
    fn = runners.get(call.name)
    if fn is None:
        result: dict[str, Any] = {"ok": False, "data": None, "error": f"unknown tool {call.name}"}
    else:
        try:
            result = fn(call.arguments, llm=llm, context=context)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
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
        last = model_calls.deepseek_tools(
            client=llm, system=attach(REACT, memory or ""), messages=messages, tools=schemas,
            purpose="session.tools",
        )
        if not last.tool_calls:
            if used:
                return (last.content or "").strip() or "Done."
            return None
        used = True
        messages.append(last.as_message())
        for call in last.tool_calls:
            messages.append(_run_one_tool(call, runners, llm, context))
    return (last.content if last else None) or "Done."