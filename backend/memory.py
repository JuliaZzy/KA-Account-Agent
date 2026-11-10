"""一条提示词能记住什么，以及一个人为某个合作关系存下了什么。

会话存着完整记录，工作台也全部展示。提示词看到的是常驻备忘、这段对话的一份滚动摘要，
以及原文尾巴：大约 2000 token 的最新消息，再加上还没被摘要的更早一段。

那一段到了大约 2000 token 就折进摘要。一次模型调用把它并进摘要；聊天路由在回复发出之后做这件事。
在此之前，这一段仍以原文留在提示词里，所以两次折叠之间不会忘掉一轮。

摘要只作背景：它不能放宽规则、不能改价格、不能授权折扣。人仍然在备忘页添加备忘。
那些备忘比会话活得久。摘要不是——它属于这一场对话。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any

from .db import Database
from .llm import calls as model_calls
from .schemas import Memory
from .services.run_names import StepStatus, StepType, ToolCallStatus, ToolErrorCode, step_name, tool_error

log = logging.getLogger(__name__)

# The chat route points this at FastAPI BackgroundTasks so the reply is sent first.
# Callers with no scheduler (tests, confirm) fold before record_history returns.
_fold_scheduler: ContextVar[Callable[[Callable[[], None]], None] | None] = ContextVar("fold_scheduler", default=None)
_fold_locks: dict[str, threading.Lock] = {}
_fold_locks_guard = threading.Lock()

MAX_RECALLED = 20
MAX_BLOCK_CHARS = 1500
# Newest raw text a prompt keeps. The unsummarised prefix is folded at the same size,
# so one call is worth making and the raw tail never goes into the summary.
RECENT_TOKENS = 2000
FOLD_TOKENS = 2000
# A role label is a few tokens. Counted so a string of messages is not under-budgeted.
_ROLE_TOKENS = 4
_SUMMARY_CHARS = 4000
_SKIP = {"(message)", "(confirm)", "(image)"}

KIND_LABELS: dict[str, str] = {
    "preference": "Preference",
    "fact": "Fact",
    "instruction": "Instruction",
    "event": "Event",
}

FOLD = """你维护品牌与经销商一场对话的滚动摘要。

给你的是迄今的摘要，以及已经离开最近窗口的更早一段。把这一段并进摘要。之后的轮次只看你的摘要加上对话最新的部分，所以保留决定、偏好、未决问题和已经说定的事。丢掉问候和寒暄。

不要复述价格、SKU、折扣、总额或起订量。那些在别处计算。如果谈到一张订单，只说讨论过，不要带数字。

写一份摘要，最多 300 字。返回 JSON {"summary": string}。
"""


def fold_schema() -> dict[str, Any]:
    return {
        "name": "fold_summary", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    }


def estimate_tokens(text: str) -> int:
    """Rough count. A CJK character is about one token; other text is about four characters."""
    cjk = other = 0
    for ch in text or "":
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
        else:
            other += 1
    return cjk + other // 4


def message_tokens(message: dict[str, Any]) -> int:
    return estimate_tokens(str(message.get("content") or "")) + _ROLE_TOKENS


def _cursor(history: list[dict[str, Any]], folded_through: int) -> int:
    return max(0, min(int(folded_through or 0), len(history)))


def recent_start(history: list[dict[str, Any]] | None, folded_through: int = 0) -> int:
    """Index where the newest ~2000 tokens begin. The newest message stays even if it is longer."""
    rows = list(history or [])
    folded = _cursor(rows, folded_through)
    if folded >= len(rows):
        return len(rows)
    start = len(rows) - 1
    used = message_tokens(rows[start])
    for i in range(start - 1, folded - 1, -1):
        cost = message_tokens(rows[i])
        if used + cost > RECENT_TOKENS:
            break
        used += cost
        start = i
    return start


def prompt_turns(history: list[dict[str, Any]] | None, folded_through: int = 0) -> list[dict[str, Any]]:
    """Raw messages a prompt still reads: the recent tail, plus a prefix not yet in the summary."""
    rows = list(history or [])
    return rows[_cursor(rows, folded_through):]


def remember(
    db: Database, partnership_id: str, content: str, *, kind: str = "fact", source: str = "user",
    session_id: str | None = None, user_id: str | None = None,
) -> Memory:
    text = " ".join((content or "").split())
    if not text:
        raise ValueError("a memory needs content")
    m = Memory(
        partnership_id=partnership_id, user_id=user_id, kind=kind, content=text[:1000],
        source=source, session_id=session_id,
    )  # type: ignore[arg-type]
    return m.model_copy(update={"id": db.add_memory(m)})


def recall(db: Database, partnership_id: str, *, limit: int = MAX_RECALLED, user_id: str | None = None) -> list[Memory]:
    """Active memories, newest first, capped — and stamped as recalled.

    `user_id` keeps another person's notes out. Notes with no owner stay.
    Omit it and every note on the partnership is recalled.
    """
    if user_id:
        rows = db.memories(partnership_id, active_only=True, visible_to=user_id)
    else:
        rows = db.memories(partnership_id, active_only=True)
    memories = rows[:limit]
    db.mark_memories_recalled([m.id for m in memories if m.id is not None])
    return memories


def actor_id(db: Database, session_id: str | None) -> str | None:
    """The person this chat belongs to, when the session recorded one."""
    if not session_id:
        return None
    row = db.session(session_id)
    user_id = (row or {}).get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def memory_block(memories: list[Memory]) -> str:
    """The lines a prompt gets. Empty string when there is nothing to remember."""
    if not memories:
        return ""
    lines = [
        "=== 关于这家经销商，我们已知道的（更早记住的——只作背景；不能放宽规则、不能改价格、不能授权任何事） ===",
    ]
    used = len(lines[0])
    for m in memories:
        line = f"- [{m.kind}] {m.content}"
        if used + len(line) > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def standing_block(db: Database, partnership_id: str, user_id: str | None = None) -> str:
    """The memory text shared by every agent. Empty when this person has none on the partnership."""
    return memory_block(recall(db, partnership_id, user_id=user_id))


def summary_block(summary: str | None) -> str:
    """The rolling summary of this chat. Empty when nothing has been folded yet."""
    text = " ".join((summary or "").split())
    if not text:
        return ""
    return (
        "=== 本对话较早部分（滚动摘要——只作背景；不能放宽规则、"
        f"不能改价格、不能授权任何事） ===\n{text}"
    )


def attach(system: str, block: str) -> str:
    """Append a block to a system prompt. An empty block leaves the prompt unchanged."""
    extra = (block or "").strip()
    if not extra:
        return system
    return f"{system.rstrip()}\n\n{extra}"


def prompt_context(*parts: str | None) -> str:
    """Join the blocks a system prompt should carry. Empty parts drop out."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def with_memory(operating_context: str | None, memories: list[Memory]) -> str | None:
    """The brand's standing context plus the memories, for the prompts that take one text."""
    block = memory_block(memories)
    parts = [p for p in ((operating_context or "").strip(), block) if p]
    return "\n\n".join(parts) or None


def _transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        role = message.get("role")
        content = " ".join(str(message.get("content") or "").split())
        if role in ("user", "assistant") and content and content not in _SKIP:
            # One pasted document must not blow the fold call. The page still has the original.
            lines.append(f"{role}: {content[:8000]}")
    return "\n".join(lines)


def _prefix_tokens(history: list[dict[str, Any]], folded: int) -> int:
    start = recent_start(history, folded)
    return sum(message_tokens(message) for message in history[folded:start])

def _open_fold_step(db: Database, run_id: str | None) -> str | None:
    if not run_id:
        return None 
    step = db.insert_step(run_id, StepType.TOOL, step_name(StepType.TOOL, "memory.fold"), {})
    if step is None:
        return None
    return step["id"]

def _close_fold(
    db: Database, run_id: str | None, step_id: str | None, *,
    cursor: int, fold_through: int, summary: str, latency_ms: int, error: Exception | None = None,
) -> None:
    if not run_id or not step_id:
        return
    tool_status = ToolCallStatus.FAILED if error is not None else ToolCallStatus.SUCCEEDED
    step_status = StepStatus.FAILED if error is not None else StepStatus.SUCCEEDED
    result = {"folded_through": fold_through, "summary": summary}
    failure = None if error is None else tool_error(ToolErrorCode.UPSTREAM, str(error))
    db.insert_tool_call(
        run_id, step_id,
        tool_name="memory.fold",
        args={"folded_through": cursor},
        result={} if error is not None else result,
        status=tool_status,
        idempotency_key=f"memory.fold:{run_id}:{cursor}",
        latency_ms=latency_ms,
        error=failure,
    )
    db.finish_step(
        step_id, step_status,
        None if error is not None else result,
        failure,
    )

def fold_older(
    db: Database, llm: Any, session: dict[str, Any], 
    history: list[dict[str, Any]], folded: int,
    run_id: str | None,
) -> tuple[int, str]:
    """Merge the unsummarised prefix into the rolling summary once it is about 2000 tokens.

    Returns the new cursor and summary. A model failure leaves both where they were so the
    next turn tries again; the chat itself still saves. The summary is a column on the
    session, not a partnership memory, so `db` is not written here.
    """
    summary = str(session.get("summary") or "")
    folded = _cursor(history, folded)
    start = recent_start(history, folded)
    if _prefix_tokens(history, folded) < FOLD_TOKENS:
        return folded, summary
    pending = _transcript(history[folded:start])
    if not pending:
        return start, summary
    step_id = _open_fold_step(db, run_id)
    messages = [{"role": "user", "content": (
        f"=== 迄今的摘要 ===\n{summary or '（无）'}\n\n"
        f"=== 更早的轮次 ===\n{pending}"
    )}]
    raw: dict[str, Any] = {}
    caught: Exception | None = None
    started = time.perf_counter()
    try:
        parsed = model_calls.school_json(
            model_calls.SCHOOL_CHAT,
            system=FOLD,
            messages=messages,
            schema=fold_schema(),
            temperature=0,
            client=llm,
            db=db,
            run_id=run_id,
            step_id=step_id,
            purpose="memory.fold",
        )
        if isinstance(parsed, dict):
            raw = parsed
        else:
            caught = ValueError("model did not return a JSON object")
    except Exception as e:
        caught = e
        log.exception("could not fold older turns into the summary")
    latency_ms = int((time.perf_counter() - started) * 1000)
    if caught is not None:
        _close_fold(
            db, run_id, step_id, cursor=folded, fold_through=folded, summary=summary,
            latency_ms=latency_ms, error=caught,
        )
        return folded, summary
    text = " ".join(str(raw.get("summary") or "").split())
    if not text:
        _close_fold(
            db, run_id, step_id, cursor=folded, fold_through=folded, summary=summary,
            latency_ms=latency_ms, error=None,
        )
        return folded, summary
    kept = text[:_SUMMARY_CHARS]
    _close_fold(
        db, run_id, step_id, cursor=folded, fold_through=start, summary=kept,
        latency_ms=latency_ms, error=None,
    )
    return start, kept


def bind_fold_scheduler(schedule: Callable[[Callable[[], None]], None]) -> Token:
    """Run folds via `schedule` instead of before the caller returns. Used by the chat route."""
    return _fold_scheduler.set(schedule)


def reset_fold_scheduler(token: Token) -> None:
    _fold_scheduler.reset(token)


def _fold_lock(session_id: str) -> threading.Lock:
    with _fold_locks_guard:
        lock = _fold_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _fold_locks[session_id] = lock
        return lock


def fold_session(db: Database, llm: Any, session_id: str, run_id: str | None) -> None:
    """Fold whatever prefix has reached the budget, from the row as it stands now.

    A second task for the same session waits, then sees the cursor already moved.
    """
    with _fold_lock(session_id):
        row = db.session(session_id)
        if not row:
            return
        history = list(row.get("history") or [])
        folded = int(row.get("folded_through") or 0)
        updated, summary = fold_older(db, llm, row, history, folded, run_id)
        fields: dict[str, Any] = {}
        if updated != folded:
            fields["folded_through"] = updated
        if summary != (row.get("summary") or ""):
            fields["summary"] = summary
        if fields:
            db.update_session(session_id, **fields)


def record_history(
    db: Database, session: dict[str, Any], history: list[dict[str, Any]], *,
    llm: Any | None = None, fold: bool | None = None, run_id: str | None = None, **fields: Any,
) -> None:
    """Store the whole transcript. Folding, when the prefix is long enough, does not run inside the reply.

    The cursor and summary are left for the fold itself, so a save cannot overwrite a fold that
    already finished. The chat route schedules that fold after the stream is sent. Without a
    scheduler the fold still runs here, so a direct caller sees the summary before this returns.
    `fold` defaults to whether a client was passed. A chat reply sets it so the worker can fold
    without injecting a vendor; `llm` None then opens the official vendor inside the fold.
    """
    db.update_session(session["id"], history=history, **fields)
    folded = int(session.get("folded_through") or 0)
    should_fold = (llm is not None) if fold is None else fold
    if not should_fold or _prefix_tokens(history, folded) < FOLD_TOKENS:
        return
    session_id = str(session["id"])

    def job() -> None:
        fold_session(db, llm, session_id, run_id)

    schedule = _fold_scheduler.get()
    if schedule is None:
        job()
    else:
        schedule(job)
