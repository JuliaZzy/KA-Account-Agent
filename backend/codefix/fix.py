"""Run an artifact, and on failure ask for a fix. Three runs at most.

The last failed artifact and its error are what a person sees.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..llm import calls as model_calls
from .prompt import CODE_FIX_PROMPT

MAX_ATTEMPTS = 3


def fix_schema() -> dict[str, Any]:
    return {
        "name": "code_fix", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "error_analysis": {"type": "string"},
                "fix_description": {"type": "string"},
                "fixed_code": {"type": "string"},
            },
            "required": ["error_analysis", "fix_description", "fixed_code"],
        },
    }


def with_reflection(
    llm: Any, *, kind: str, first: str,
    run: Callable[[str], tuple[bool, Any, str | None, str]],
    trace: Any | None = None,
) -> tuple[Any, list[dict[str, str]]]:
    """Run first. On failure, revise and run again, at most three runs."""
    current = first
    attempts: list[dict[str, str]] = []
    for index in range(MAX_ATTEMPTS):
        ok, value, error, output = run(current)
        attempts.append({
            "kind": kind,
            "artifact": current,
            "error": error or "",
            "output": output or "",
        })
        if ok:
            return value, []
        if index == MAX_ATTEMPTS - 1:
            break
        revised = _revise(llm, kind=kind, artifact=current, error=error or "", output=output or "", trace=trace)
        if not revised:
            break
        current = revised
    return None, attempts


def format_failures(attempts: list[dict[str, str]]) -> str:
    """The last failed artifact and its error, for a person to take over."""
    if not attempts:
        return ""
    last = attempts[-1]
    kind = last.get("kind") or "output"
    lines = [f"{kind} 没有跑通，需要人工处理。", f"报错：{last.get('error') or '未知'}"]
    output = (last.get("output") or "").strip()
    if output:
        lines.append(f"输出：{output[:500]}")
    lines.append(f"最后一次内容：{(last.get('artifact') or '')[:800]}")
    return "\n".join(lines)

def _ledger(trace: Any | None) -> dict[str, Any]:
    db = getattr(trace, "db", None)
    run_id = getattr(trace, "run_id", None)
    step_id = getattr(trace, "step_id", None)
    if db is None or not run_id or not step_id:
        return {}
    return {"db": db, "run_id": run_id, "step_id": step_id, "purpose": "code.fix"}


def _revise(
    llm: Any, *, kind: str, artifact: str, error: str, output: str, trace: Any | None = None
) -> str:
    system = CODE_FIX_PROMPT.format(
        kind=kind,
        code=(artifact or "（无）")[:6000],
        error=(error or "（无）")[:1500],
        stdout=(output or "（无）")[:1500],
    )
    messages = [{"role": "user", "content": "按错误信息修复这一次失败的内容。"}]
    try:
        parsed = model_calls.flash_json(
            system=system,
            messages=messages,
            schema=fix_schema(),
            temperature=0,
            client=llm,
            **_ledger(trace),
        )
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("fixed_code") or "").strip()