"""Tables for the run models in `schemas/run.py`.

The request and response classes in that file stay Pydantic. These are the rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint, desc, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONCol, TimestampCol


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key", name="uq_agent_run_idempotency"),
        # Org run list, newest first. DESC is part of the index, not just the query.
        Index("idx_agent_runs_org_created", "org_id", desc("created_at")),
        # Worker scan for queued / running runs.
        Index("idx_agent_runs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), ForeignKey("distributors.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TimestampCol)
    ended_at: Mapped[datetime | None] = mapped_column(TimestampCol)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(TimestampCol)
    updated_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TimestampCol)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parent_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(64))
    # Null means this run is not capped. The model entry reads these before it calls.
    max_model_calls: Mapped[int | None] = mapped_column(Integer)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    max_tool_calls: Mapped[int | None] = mapped_column(Integer)
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer)


class AgentStep(Base):
    """`id` is the primary key. `(run_id, step_index)` is unique, and `run_id` is an `agent_runs.id`."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_agent_step_index"),
        Index("idx_agent_steps_run_index", "run_id", "step_index"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONCol)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol)
    started_at: Mapped[datetime | None] = mapped_column(TimestampCol, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(TimestampCol)

class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        Index("idx_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "checkpoint_index", name="uq_checkpoint_index"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    checkpoint_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (Index("idx_usage_records_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    org_id: Mapped[str] = mapped_column(String(64), ForeignKey("distributors.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # actual when the vendor sent usage; estimated when this row was counted locally.
    usage_source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    step_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_steps.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # request_json & response_json 保持原有输入和响应，便于回放
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONCol)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    usage_source: Mapped[str | None] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("tool_name", "idempotency_key", name="uq_tool_call_idempotency"),
        Index("idx_tool_calls_run_created", "run_id", "created_at"),
        Index("idx_tool_calls_tool_status", "tool_name", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    org_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("distributors.id"), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_steps.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONCol)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    latency_ms: Mapped[int] = mapped_column(Integer)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TimestampCol, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TimestampCol)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("idx_audit_logs_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[str] = mapped_column(String(64), ForeignKey("distributors.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONCol)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())

class OutboxJob(Base):
    __tablename__ = "outbox_jobs"
    __table_args__ = (
        Index("idx_outbox_jobs_pending", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONCol)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    next_attempt_at: Mapped[datetime | None] = mapped_column(TimestampCol, nullable=False)
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())
    dispatch_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())

class DeadLetterJob(Base):
    __tablename__ = "dead_letter_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONCol, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCol, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, server_default=func.now())
