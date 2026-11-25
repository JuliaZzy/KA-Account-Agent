"""把追问改写成一条可以单独检索的查询，并给检索结果补上来源名。

回答由编排器写。这里不调用模型回复，也不自己开事件循环。
"""

from __future__ import annotations

from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_turns
from ..orchestrator.prompt import FOLLOWUP
from .rag import source_label

_SKIP_HISTORY = {"(message)", "(confirm)"}


def _followup_schema() -> dict[str, Any]:
    return {
        "name": "brand_search_query", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }


def _prior_turns(history: list[dict[str, Any]], folded_through: int = 0) -> list[dict[str, str]]:
    turns = []
    for message in prompt_turns(history, folded_through):
        role = message.get("role")
        content = str(message.get("content") or "").strip()
        if role in ("user", "assistant") and content and content not in _SKIP_HISTORY:
            turns.append({"role": role, "content": content[:500]})
    return turns


async def _search_query(
    llm: Any, text: str, history: list[dict[str, Any]], remembered: str = "", folded_through: int = 0,
) -> str:
    """Standalone query for retrieval. The first turn is searched as typed.

    A later turn is rewritten with the recent conversation. A failed or empty rewrite
    falls back to the user's words so the answer still runs.
    """
    prior = _prior_turns(history, folded_through)
    if not prior:
        return text
    recent = "\n".join(f"{turn['role']}: {turn['content']}" for turn in prior)
    try:
        raw = await model_calls.qwen_json_async(
            client=llm,
            system=attach(FOLLOWUP, remembered),
            messages=[{"role": "user", "content": (
                f"=== 最近对话 ===\n{recent}\n\n=== 最新消息 ===\n{text[:4000]}"
            )}],
            schema=_followup_schema(),
            temperature=0,
            purpose="brand.query",
        )
    except Exception:
        return text
    query = str(raw.get("query") or "").strip()
    return query[:500] if query else text


def _with_sources(db: Database, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number retrieved passages and name the file. The model cites [1]; the client shows `source`."""
    named = []
    for n, hit in enumerate(hits, start=1):
        doc_id = str(hit.get("doc") or "")
        row = db.document(doc_id) if doc_id else None
        file_name = row.file_name if row is not None else None
        title = ((row.title or "").strip() if row is not None else "") or file_name
        named.append({
            **hit,
            "n": n,
            "file_name": file_name,
            "source": source_label(title, hit.get("page"), doc=doc_id or None),
        })
    return named
