"""Request and response bodies for the exercise Run API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateRunRequest(BaseModel):
    run_type: str
    idempotency_key: str
    input: dict[str, Any]


class RerunRunRequest(BaseModel):
    idempotency_key: str
    input: dict[str, Any] | None = None

class RunResponse(BaseModel):
    id: str
    org_id: str
    user_id: str
    run_type: str
    idempotency_key: str
    status: str
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int
    redispatch_count: int
    parent_run_id: str | None = None
    owner_run_id: str | None = None
    trace_id: str | None = None
    max_model_calls: int | None = None
    max_tokens: int | None = None
    max_tool_calls: int | None = None
    max_duration_seconds: int | None = None


class RepairRunRequest(BaseModel):
    reason: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class RunEventResponse(BaseModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    payload_json: dict[str, Any]
    created_at: datetime


class CancelRunRequest(BaseModel):
    reason: str | None = None


class ResumeRunRequest(BaseModel):
    resume_token: str | None = None
    input: dict[str, Any] | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class AgentRun(BaseModel):
    id: str
    org_id: str
    user_id: str
    run_type: str
    idempotency_key: str
    status: str
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int
    redispatch_count: int
    parent_run_id: str | None = None
    owner_run_id: str | None = None
    trace_id: str | None = None
    max_model_calls: int | None = None
    max_tokens: int | None = None
    max_tool_calls: int | None = None
    max_duration_seconds: int | None = None


class AgentStep(BaseModel):
    id: str
    run_id: str
    step_index: int
    step_type: str
    name: str
    status: str
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class RunEvent(BaseModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    payload_json: dict[str, Any]
    created_at: datetime


class Checkpoint(BaseModel):
    id: str
    run_id: str
    checkpoint_index: int
    state_json: dict[str, Any]
    created_at: datetime


class UsageRecord(BaseModel):
    id: str
    run_id: str
    org_id: str
    user_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_cents: int
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_creation_tokens: int | None = None
    usage_source: str | None = None
    created_at: datetime


class ModelCall(BaseModel):
    id: str
    run_id: str
    step_id: str
    provider: str
    model: str
    request_json: dict[str, Any]
    response_json: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    status: str
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_creation_tokens: int | None = None
    usage_source: str | None = None
    error_json: dict[str, Any] | None = None
    created_at: datetime


class ToolCall(BaseModel):
    id: str
    run_id: str
    org_id: str | None = None
    user_id: str | None = None
    step_id: str
    tool_name: str
    args_json: dict[str, Any]
    result_json: dict[str, Any]
    status: str
    idempotency_key: str
    latency_ms: int
    error_json: dict[str, Any] | None = None
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


class AuditLog(BaseModel):
    id: str
    user_id: str
    org_id: str
    run_id: str
    action: str
    target_type: str
    target_id: str
    metadata_json: dict[str, Any]
    created_at: datetime


class OutboxJob(BaseModel):
    id: str
    job_type: str
    payload_json: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    last_error_json: dict[str, Any] | None = None
    created_at: datetime
    dispatch_at: datetime


class DeadLetterJob(BaseModel):
    id: str
    original_job_id: str
    job_type: str
    payload_json: dict[str, Any]
    attempts: int
    error_json: dict[str, Any]
    created_at: datetime


class TraceResponse(BaseModel):
    run: RunResponse
    steps: list[AgentStep] = Field(default_factory=list)
    events: list[RunEventResponse]
    model_calls: list[ModelCall] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    usage_records: list[UsageRecord] = Field(default_factory=list)
    audit_logs: list[AuditLog] = Field(default_factory=list)
    outbox: list[OutboxJob] = Field(default_factory=list)
    dead_letters: list[DeadLetterJob] = Field(default_factory=list)


class MonitorSwitches(BaseModel):
    send_email: bool
    mcp: bool
    queue: bool


class SloRow(BaseModel):
    name: str
    target: str
    value: str
    ok: bool | None = None


class MonitorAlert(BaseModel):
    signal: str
    action: str
    runbook: str | None = None


class RunbookResponse(BaseModel):
    id: str
    title: str
    body: str


class StageLatency(BaseModel):
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    count: int = 0


class PlanQuality(BaseModel):
    calls: int = 0
    failed: int = 0
    unknown_agent_calls: int = 0
    unknown_agent_dropped: int = 0
    cycle: int = 0
    unknown_dependency: int = 0
    depends_on_unknown_agent: int = 0


class MonitorMetrics(BaseModel):
    created: int
    succeeded: int
    failed: int
    duration_p50: float | None = None
    duration_p95: float | None = None
    duration_p99: float | None = None
    queue_lag_p50: float | None = None
    queue_lag_p95: float | None = None
    queue_lag_p99: float | None = None
    model_latency_p50: float | None = None
    model_latency_p95: float | None = None
    model_latency_p99: float | None = None
    tool_latency_p50: float | None = None
    tool_latency_p95: float | None = None
    tool_latency_p99: float | None = None
    stages: dict[str, StageLatency] = Field(default_factory=dict)
    plan: PlanQuality = Field(default_factory=PlanQuality)
    pending_jobs: int
    active_runs: int
    sse_connections: int
    stuck_runs: int
    oldest_queued_seconds: float = 0.0
    model_errors: int
    route_selected: int = 0
    route_fallback: int = 0
    route_quality_gate_failed: int = 0
    tool_calls: int
    tool_errors: int
    duplicates: int
    cost_cents: int
    daily_cost_cents: int
    daily_tokens: int


class MonitorHour(BaseModel):
    at: str
    created: int
    failed: int


class MonitorBar(BaseModel):
    label: str
    count: int
    tail: bool
    mark: str | None = None


class MonitorLevel(BaseModel):
    label: str
    value: int
    limit: int | None = None


class MonitorCharts(BaseModel):
    hours: list[MonitorHour]
    duration: list[MonitorBar]
    duration_p50: float | None = None
    duration_p95: float | None = None
    duration_p99: float | None = None
    queue: list[MonitorBar]
    queue_p50: float | None = None
    queue_p95: float | None = None
    queue_p99: float | None = None
    levels: list[MonitorLevel]


class MonitorResponse(BaseModel):
    switches: MonitorSwitches
    slo: list[SloRow]
    alerts: list[MonitorAlert]
    runbooks: list[RunbookResponse]
    charts: MonitorCharts
    metrics: MonitorMetrics
