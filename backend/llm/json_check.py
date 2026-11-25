"""输出被截断、解析不了或少了必填字段时，带着问题再问一次。

围栏在解析时已经去掉。期望的形状就是调用方本来要发给模型的 JSON schema。
查数 ReAct、品牌检索和规划器各自有自己的循环，不走这里。
次数用完还是不合格，这次模型调用标成失败，子任务把结果交给 answer，在那条子任务的事件上显示。
整条 Run 被放弃时才写一行死信，并带上导致放弃的那次 model_call。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# 先出一稿，再最多改这么多次；每一次都能看到上一次的输出。
JSON_REPAIRS = 2
# 次数用完后桌面显示的那句，和计划生成失败用的是同一句。
JSON_FAILURE_MESSAGE = "系统出错，请稍后再试。"
_ISSUE_CAP = 20
_SKIP_PURPOSES = frozenset({"orchestrator.plan", "brand.query", "brand.answer"})


@dataclass(frozen=True)
class JsonIssue:
    message: str


def repairs_enabled(purpose: str | None) -> bool:
    task = str(purpose or "")
    return not (task.startswith("data.") or task in _SKIP_PURPOSES)


def _node(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    inner = schema.get("schema")
    if isinstance(inner, dict) and "type" not in schema and "properties" not in schema:
        return inner
    return schema


def _types(node: dict[str, Any]) -> list[str]:
    raw = node.get("type")
    if isinstance(raw, list):
        return [item for item in raw if item != "null"]
    if isinstance(raw, str) and raw != "null":
        return [raw]
    return []


def _allows_null(node: dict[str, Any]) -> bool:
    raw = node.get("type")
    return raw == "null" or (isinstance(raw, list) and "null" in raw)


def _matches(value: Any, types: list[str]) -> bool:
    if not types:
        return True
    checks = {
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
    }
    return any(checks.get(kind, lambda _item: True)(value) for kind in types)


def _join(path: str, key: str) -> str:
    return key if not path else f"{path}.{key}"


def inspect_json(raw: Any, schema: dict[str, Any] | None) -> list[JsonIssue]:
    """核对必填字段、类型和枚举。多出来的字段不管。"""
    found: list[JsonIssue] = []

    def add(message: str) -> None:
        if len(found) < _ISSUE_CAP:
            found.append(JsonIssue(message))

    def walk(value: Any, node: dict[str, Any], path: str) -> None:
        if len(found) >= _ISSUE_CAP:
            return
        if value is None:
            if not _allows_null(node):
                add(f"缺少 {path or 'JSON 对象'}")
            return
        types = _types(node)
        if not _matches(value, types):
            add(f"{path or 'JSON 对象'} 的类型不对")
            return
        allowed = node.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            add(f"{path or 'JSON 对象'} 不在允许的取值里")
            return
        if isinstance(value, list):
            item_node = node.get("items") if isinstance(node.get("items"), dict) else None
            if item_node is None:
                return
            for index, item in enumerate(value):
                walk(item, item_node, f"{path}[{index}]" if path else f"[{index}]")
            return
        if not isinstance(value, dict):
            return
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        required = node.get("required") if isinstance(node.get("required"), list) else []
        for key in required:
            if isinstance(key, str) and key not in value:
                add(f"缺少 {_join(path, key)}")
        for key, child in properties.items():
            if key not in value or not isinstance(child, dict):
                continue
            walk(value.get(key), child, _join(path, key))

    if not isinstance(raw, dict):
        return [JsonIssue("输出不是 JSON 对象")]
    walk(raw, _node(schema), "")
    return found


def _exhausted(previous: str, *, finish_reason: str | None = None) -> None:
    """次数已经用完。调用方不能把坏对象留着继续用。"""
    from .calls import JsonRepairExhausted

    raise JsonRepairExhausted(
        JSON_FAILURE_MESSAGE, raw=(previous or "")[:6000], finish_reason=finish_reason,
    )


def repair_note(issues: list[JsonIssue], previous: str, *, truncated: bool) -> str:
    """下一轮发给模型的话。被截断时要求写短一点。"""
    if truncated:
        head = (
            "上一次输出被长度截断，没有写完。不要把同样长的 JSON 再写一遍。"
            "请精简后补上缺少的字段。只返回一个 JSON 对象，不要解释。"
        )
    else:
        head = "上一次输出不能用。下面是全部问题，请一次改完。只返回一个 JSON 对象，不要解释。"
    lines = "\n".join(f"- {issue.message}" for issue in issues)
    shown = previous.strip() or "（空）"
    return f"{head}\n{lines}\n\n上一次输出：\n{shown[:6000]}"


def repair_json(
    fetch: Callable[[list[dict[str, Any]]], Any],
    *,
    messages: list[dict[str, Any]],
    schema: dict[str, Any] | None,
    purpose: str | None,
) -> Any:
    """带着问题再调 `fetch`，直到对象合格，或次数用完。"""
    from .calls import ModelBudgetExceeded, ModelOutputError, last_finish_reason
    from .errors import ModelCapacityExceeded, ProviderError

    pending = list(messages)
    if not repairs_enabled(purpose):
        return fetch(pending)
    last_error: ModelOutputError | None = None
    for attempt in range(JSON_REPAIRS + 1):
        truncated = False
        try:
            raw = fetch(pending)
            truncated = last_finish_reason() == "length"
        except (ModelCapacityExceeded, ProviderError, ModelBudgetExceeded):
            raise
        except ModelOutputError as err:
            last_error = err
            truncated = err.finish_reason == "length" or last_finish_reason() == "length"
            if attempt == JSON_REPAIRS:
                _exhausted(err.raw or str(err), finish_reason=err.finish_reason)
            previous = err.raw or str(err)
            issue = JsonIssue("输出被长度截断" if truncated else "输出不是合法 JSON 对象")
            pending = [
                *pending,
                {"role": "assistant", "content": previous[:6000] or "（空）"},
                {"role": "user", "content": repair_note([issue], previous, truncated=truncated)},
            ]
            continue
        if not isinstance(raw, dict):
            if attempt == JSON_REPAIRS:
                _exhausted(str(raw), finish_reason="length" if truncated else None)
            previous = str(raw)
            pending = [
                *pending,
                {"role": "assistant", "content": previous[:6000] or "（空）"},
                {"role": "user", "content": repair_note(
                    [JsonIssue("输出不是 JSON 对象")], previous, truncated=truncated,
                )},
            ]
            continue
        issues = inspect_json(raw, schema)
        if truncated and issues:
            issues = [JsonIssue("输出被长度截断"), *issues]
        if not issues:
            return raw
        previous = json.dumps(raw, ensure_ascii=False)
        if attempt == JSON_REPAIRS:
            _exhausted(previous, finish_reason="length" if truncated else None)
        pending = [
            *pending,
            {"role": "assistant", "content": previous[:6000]},
            {"role": "user", "content": repair_note(issues, previous, truncated=truncated and bool(issues))},
        ]
    _exhausted(last_error.raw if last_error is not None else "", finish_reason=None)


async def repair_json_async(
    fetch: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    messages: list[dict[str, Any]],
    schema: dict[str, Any] | None,
    purpose: str | None,
) -> Any:
    """和 repair_json 一样：不合格就带着问题再问一次。这里的 fetch 是协程。"""
    from .calls import ModelBudgetExceeded, ModelOutputError, last_finish_reason
    from .errors import ModelCapacityExceeded, ProviderError

    pending = list(messages)
    if not repairs_enabled(purpose):
        return await fetch(pending)
    last_error: ModelOutputError | None = None
    for attempt in range(JSON_REPAIRS + 1):
        truncated = False
        try:
            raw = await fetch(pending)
            truncated = last_finish_reason() == "length"
        except (ModelCapacityExceeded, ProviderError, ModelBudgetExceeded):
            raise
        except ModelOutputError as err:
            last_error = err
            truncated = err.finish_reason == "length" or last_finish_reason() == "length"
            if attempt == JSON_REPAIRS:
                _exhausted(err.raw or str(err), finish_reason=err.finish_reason)
            previous = err.raw or str(err)
            issue = JsonIssue("输出被长度截断" if truncated else "输出不是合法 JSON 对象")
            pending = [
                *pending,
                {"role": "assistant", "content": previous[:6000] or "（空）"},
                {"role": "user", "content": repair_note([issue], previous, truncated=truncated)},
            ]
            continue
        if not isinstance(raw, dict):
            if attempt == JSON_REPAIRS:
                _exhausted(str(raw), finish_reason="length" if truncated else None)
            previous = str(raw)
            pending = [
                *pending,
                {"role": "assistant", "content": previous[:6000] or "（空）"},
                {"role": "user", "content": repair_note(
                    [JsonIssue("输出不是 JSON 对象")], previous, truncated=truncated,
                )},
            ]
            continue
        issues = inspect_json(raw, schema)
        if truncated and issues:
            issues = [JsonIssue("输出被长度截断"), *issues]
        if not issues:
            return raw
        previous = json.dumps(raw, ensure_ascii=False)
        if attempt == JSON_REPAIRS:
            _exhausted(previous, finish_reason="length" if truncated else None)
        pending = [
            *pending,
            {"role": "assistant", "content": previous[:6000]},
            {"role": "user", "content": repair_note(issues, previous, truncated=truncated and bool(issues))},
        ]
    _exhausted(last_error.raw if last_error is not None else "", finish_reason=None)
