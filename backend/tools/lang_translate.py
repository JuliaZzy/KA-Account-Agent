"""翻译一段文字。模型只改语言，不增加事实，也不删事实。

「把刚刚的报告译成英语」这种理解属于调用方。这个模块只决定翻译哪一段，
然后用学校网关的 deepseek-chat 做一次调用。聊天模型不参与。
"""

from __future__ import annotations

from typing import Any, Literal

from ..llm import calls as model_calls

SYSTEM = """你是一名专业的翻译，请把用户给出的文字译成用户要求的语言。
如果用户没有提供目标语言，请及时提问，直到用户提供为止。
不要添加原文里没有的事实。名称、SKU 和数字保持原样，除非目标语言有约定的写法。"""

Source = Literal["message", "last_assistant", "report"]
SOURCES = ("message", "last_assistant", "report")

# Shown to the model (OpenAI tools=). `run` reads the same keys. `text` is optional
# because source=report | last_assistant takes the string from session context.
SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "translate",
        "description": "用户要求翻译时，把文字译成另一种语言。",
        "parameters": {
            "type": "object",
            "properties": {
                "target_language": {
                    "type": "string",
                    "description": "要译成的语言，例如英语、德语、中文",
                },
                "text": {
                    "type": "string",
                    "description": "要翻译的原话，当这些话就在这条用户消息里时填写",
                },
                "source": {
                    "type": "string",
                    "enum": list(SOURCES),
                    "description": "`text` 为空时，从哪里取文字",
                },
            },
            "required": ["target_language"],
        },
    },
}


def _source_key(raw: Any) -> Source:
    key = str(raw or "message").strip().lower().replace(" ", "_")
    if key not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    return key  # type: ignore[return-value]


def resolve_text(arguments: dict[str, Any], context: dict[str, Any] | None) -> str:
    """Pick the string to translate. Explicit `text` wins; otherwise `source` names a
    field on `context` that the orchestrator already filled from the session."""
    explicit = str(arguments.get("text") or "").strip()
    if explicit:
        return explicit
    source = _source_key(arguments.get("source"))
    ctx = context or {}
    if source == "message":
        raise ValueError("text required when source is message")
    if source == "last_assistant":
        history = ctx.get("history") or []
        for turn in reversed(history):
            if isinstance(turn, dict) and turn.get("role") == "assistant":
                body = str(turn.get("content") or "").strip()
                if body:
                    return body
        raise ValueError("no assistant message in this session to translate")
    report = str(ctx.get("last_report") or "").strip()
    if not report:
        raise ValueError("no report in this session to translate")
    return report


def translate(*, text: str, target_language: str) -> str:
    """One campus chat call. The reply text is the translation."""
    out = "".join(model_calls.school_text(
        model_calls.SCHOOL_CHAT,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"目标语言：{target_language}\n\n原文：\n{text}"}],
        temperature=0.3,
    )).strip()
    if not out:
        raise ValueError("empty translation")
    return out


def _target(arguments: dict[str, Any]) -> str:
    """The planner often writes `target` or `language` instead of the schema key."""
    for key in ("target_language", "target_lang", "target", "language", "lang"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""


def run(arguments: dict[str, Any], *, llm: Any = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """`llm` is the chat model. Translation does not use it."""
    del llm
    target = _target(arguments)
    if not target:
        return {"ok": False, "data": None, "error": "target_language required"}
    try:
        text = resolve_text(arguments, context)
        return {"ok": True, "data": {"translation": translate(text=text, target_language=target)}, "error": None}
    except Exception as err:
        return {"ok": False, "data": None, "error": str(err)}
