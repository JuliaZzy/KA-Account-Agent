"""Session table. `schemas/chat.py` is the HTTP body for those sessions, not a second table.

`Text2SQLLog` has no schema module. It is the row behind the data-query feedback on a session.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONCol, TimestampCol


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    partnership_id: Mapped[str] = mapped_column(ForeignKey("partnerships.id"), index=True)
    # Null on chats opened before people existed. The desk always sets it.
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    title: Mapped[str | None] = mapped_column(String(120))
    tags: Mapped[list[str]] = mapped_column(JSONCol, default=list)
    specialist: Mapped[str | None] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(16), default="gathering")
    slots: Mapped[dict[str, Any]] = mapped_column(JSONCol, default=dict)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONCol, default=list)
    # Messages before this index are already in `summary`. The transcript stays whole.
    folded_through: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    # The burn (`UPDATE … WHERE confirm_token = ?`) is the idempotency guarantee; index it.
    confirm_token: Mapped[str | None] = mapped_column(String(64), index=True)
    confirm_hash: Mapped[str | None] = mapped_column(String(64))
    confirm_expires_at: Mapped[datetime | None] = mapped_column(TimestampCol)
    confirmed_at: Mapped[datetime | None] = mapped_column(TimestampCol)
    po_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_orders.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TimestampCol)
    updated_at: Mapped[datetime] = mapped_column(TimestampCol)


class Text2SQLLog(Base):
    """One attempt to answer a partnership-scoped question with SQL.

    The rows themselves are not stored — bad-case review reads the question, the statement,
    and whether it ran. Not exposed to the text2sql role's views: a query must not read or
    rewrite its own log.
    """

    __tablename__ = "text2sql_logs"
    __table_args__ = (Index("ix_text2sql_logs_partnership_created", "partnership_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partnership_id: Mapped[str] = mapped_column(ForeignKey("partnerships.id"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text)
    selected_tables: Mapped[list[str]] = mapped_column(JSONCol, default=list)
    generated_sql: Mapped[str | None] = mapped_column(Text)
    execution_success: Mapped[bool] = mapped_column(Boolean, default=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    user_feedback: Mapped[str | None] = mapped_column(String(20))  # good | bad
    feedback_reason: Mapped[str | None] = mapped_column(Text)
    corrected_sql: Mapped[str | None] = mapped_column(Text)
    # Which agent run and loop turn wrote this statement, and how the model rated its result
    # (set one turn later, so it stays null for a statement that was never rated).
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True)
    turn: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TimestampCol)
