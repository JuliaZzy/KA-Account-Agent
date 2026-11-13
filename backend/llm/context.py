"""Bound ledger fields for the current task.

Callers that already pass ``db`` / ``run_id`` / ``step_id`` / ``purpose`` into
``call_model`` keep winning. The bound values fill in what they omit, so a
report chapter write that forgets ``trace`` still hits the run ledger.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallContext:
    db: Any = None
    run_id: str | None = None
    step_id: str | None = None
    purpose: str | None = None


_CALL_CONTEXT: ContextVar[CallContext | None] = ContextVar("llm_call_context", default=None)


def current_call_context() -> CallContext:
    return _CALL_CONTEXT.get() or CallContext()


def set_call_context(
    *,
    db: Any = None,
    run_id: str | None = None,
    step_id: str | None = None,
    purpose: str | None = None,
) -> Token[CallContext]:
    return _CALL_CONTEXT.set(CallContext(db=db, run_id=run_id, step_id=step_id, purpose=purpose))


def reset_call_context(token: Token[CallContext]) -> None:
    _CALL_CONTEXT.reset(token)


@contextmanager
def bind_call_context(
    *,
    db: Any = None,
    run_id: str | None = None,
    step_id: str | None = None,
    purpose: str | None = None,
) -> Iterator[CallContext]:
    token = set_call_context(db=db, run_id=run_id, step_id=step_id, purpose=purpose)
    try:
        yield current_call_context()
    finally:
        reset_call_context(token)


def fill_call_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Copy kwargs and fill missing ledger fields from the bound context."""
    ctx = current_call_context()
    filled = dict(kwargs)
    if not filled.get("db"):
        filled["db"] = ctx.db
    if not filled.get("run_id"):
        filled["run_id"] = ctx.run_id
    if not filled.get("step_id"):
        filled["step_id"] = ctx.step_id
    if not filled.get("purpose"):
        filled["purpose"] = ctx.purpose
    return filled
