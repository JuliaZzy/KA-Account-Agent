"""根据已经检索到的段落回答一个品牌问题。这不是代理。

后一轮会先改写成一条可以单独检索的查询。存下来的消息和回答提示词仍用用户的原话。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_context, prompt_turns, standing_block, summary_block
from ..orchestrator.prompt import FOLLOWUP
from ..orchestrator.speak import answer_directly, one_shot
from ..orchestrator.status import status
from .prompt.brand import ANSWER
from .rag import RagUnavailable, evidence_prompt, retrieve, source_label

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


def _search_query(
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
        raw = model_calls.school_json(
            model_calls.SCHOOL_CHAT,
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


def answer_in_chat(
    db: Database, llm: Any, session: dict[str, Any], text: str, history: list[dict[str, Any]], brand_id: str,
) -> Iterator[dict[str, Any]]:
    db.update_session(session["id"], specialist="brand")
    yield status("brand-search", "检索", "正在查找品牌资料", state="running")
    folded = int(session.get("folded_through") or 0)
    remembered = prompt_context(
        standing_block(db, session["partnership_id"], session.get("user_id")), summary_block(session.get("summary")),
    )
    query = _search_query(llm, text, history, remembered, folded)
    try:
        hits = retrieve(query, brand_id)
    except RagUnavailable as err:
        yield status("brand-search", "检索", "检索不可用", state="done")
        yield from one_shot(db, session, str(err), llm=llm)
        return
    named = _with_sources(db, hits)
    yield status("brand-search", "检索", f"找到 {len(named)} 段", state="done")
    yield status("brand-reply", "回答", "正在根据这些段落作答", state="running")
    done = yield from answer_directly(
        db, llm, session, text,
        prompt_text=evidence_prompt(text, named),
        system=ANSWER,
        purpose="brand.answer",
        extra={"brand_hits": [
            {
                "n": h["n"], "cid": h.get("cid"), "doc": h.get("doc"), "page": h.get("page"),
                "file_name": h.get("file_name"), "source": h["source"],
            }
            for h in named
        ]},
        done_step=("brand-reply", "回答", "回复已写完"),
    )
    yield done
