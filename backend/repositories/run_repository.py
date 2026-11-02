"""SQL store for the exercise Run API.

The session and the queries live on `Database`. Each function is one read or one write.
Routes call the service, and the service decides which of these to call together.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..db import Database
from ..services.run_names import OutboxStatus as OutboxStatus
from ..services.run_names import RunStatus

RUN_TERMINAL_STATUSES = frozenset({
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
})

def find_run_by_id(db: Database, org_id: str, run_id: str) -> dict[str, Any] | None:
    return db.find_agent_run(org_id, run_id)


def find_run_by_idempotency_key(db: Database, org_id: str, idempotency_key: str) -> dict[str, Any] | None:
    return db.find_agent_run_by_idempotency(org_id, idempotency_key)

def transition_run(db: Database, org_id: str, run_id: str, to_status: str, payload: dict[str, Any] | None = None, from_status: frozenset[str] | None = None, lease_owner: str | None = None, lease_expires_at: datetime | None = None, output: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return db.transition_run(
        org_id, run_id, to_status, payload, from_status=from_status, error=error,
    )

def request_run_cancel(db: Database, org_id: str, run_id: str, reason: str | None) -> dict[str, Any] | None:
    return db.request_run_cancel(org_id, run_id, reason)

def cancel_run(db: Database, run_id: str) -> dict[str, Any] | None:
    return db.cancel_run(run_id)

def list_run_events(db: Database, org_id: str, run_id: str, after: int = 0) -> list[dict[str, Any]] | None:
    return db.list_run_events(org_id, run_id, after)


def append_run_event(
    db: Database, org_id: str, run_id: str, event_type: str, payload: dict[str, Any],
) -> dict[str, Any] | None:
    return db.append_run_event(org_id, run_id, event_type, payload)


def run_trace(db: Database, org_id: str, run_id: str) -> dict[str, Any] | None:
    return db.run_trace(org_id, run_id)


def list_agent_runs(
    db: Database, org_id: str, *, user_id: str | None, status: str | None, limit: int,
) -> list[dict[str, Any]]:
    return db.list_agent_runs(org_id, user_id=user_id, status=status, limit=limit)

def create_agent_run(
    db: Database,
    org_id: str,
    user_id: str,
    run_type: str,
    idempotency_key: str,
    run_input: dict[str, Any],
    parent_run_id: str | None = None,
    max_model_calls: int | None = None,
    max_tokens: int | None = None,
    max_tool_calls: int | None = None,
    max_duration_seconds: int | None = None,
) -> tuple[dict[str, Any], bool]:
    return db.create_agent_run(
        org_id, user_id, run_type, idempotency_key, run_input, parent_run_id,
        max_model_calls=max_model_calls, max_tokens=max_tokens,
        max_tool_calls=max_tool_calls, max_duration_seconds=max_duration_seconds,
    )


def find_pending_outbox_jobs(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return db.find_pending_outbox_jobs(limit)


def mark_outbox_dispatched(db: Database, outbox_id: str) -> dict[str, Any] | None:
    return db.mark_outbox_dispatched(outbox_id)

def mark_outbox_retry(db: Database, outbox_id: str, error: str) -> dict[str, Any] | None:
    return db.mark_outbox_retry(outbox_id, error)


def find_agent_run_by_id(db: Database, run_id: str) -> dict[str, Any] | None:
    return db.find_agent_run_by_id(run_id)


def claim_run(
    db: Database, run_id: str, worker_id: str, lease_expires_at: datetime,
) -> dict[str, Any] | None:
    return db.claim_run(run_id, worker_id, lease_expires_at)

def claim_chat_turn(
    db: Database, run_id: str, worker_id: str, lease_expires_at: datetime,
) -> dict[str, Any] | None:
    return db.claim_chat_turn(run_id, worker_id, lease_expires_at)

def sweep_expired_leases(db: Database, limit: int = 100) -> int:
    return db.sweep_expired_leases(limit)

def complete_run(db: Database, run_id: str, output: dict[str, Any]) -> dict[str, Any] | None:
    return db.complete_run(run_id, output)

def note_running_reply(db: Database, run_id: str, text: str) -> dict[str, Any] | None:
    return db.note_running_reply(run_id, text)

def fail_run(db: Database, run_id: str, error: dict[str, Any]) -> dict[str, Any] | None:
    return db.fail_run(run_id, error)

def schedule_retry(
    db: Database,
    run_id: str,
    error: dict[str, Any],
    delays: tuple[timedelta, ...],
    *,
    spend_attempt: bool = True,
) -> str | None:
    return db.schedule_retry(run_id, error, delays, spend_attempt=spend_attempt)

def latest_checkpoint(db: Database, run_id: str) -> dict[str, Any] | None:
    return db.latest_checkpoint(run_id)


def append_checkpoint(db: Database, run_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    return db.append_checkpoint(run_id, state)


def insert_step(
    db: Database, run_id: str, step_type: str, name: str, step_input: dict[str, Any],
) -> dict[str, Any] | None:
    return db.insert_step(run_id, step_type, name, step_input)


def finish_step(
    db: Database,
    step_id: str,
    status: str,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    extra_event: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return db.finish_step(
        step_id, status, output, error,
        checkpoint=checkpoint, extra_event=extra_event, extra_payload=extra_payload,
    )


def insert_model_call(db: Database, run_id: str, step_id: str, **fields: Any) -> dict[str, Any] | None:
    return db.insert_model_call(run_id, step_id, **fields)


def insert_tool_call(db: Database, run_id: str, step_id: str, **fields: Any) -> dict[str, Any] | None:
    return db.insert_tool_call(run_id, step_id, **fields)

def claim_tool_call(
    db: Database, 
    run_id: str, 
    step_id: str, 
    *, 
    tool_name: str, 
    args: dict[str, Any], 
    idempotency_key: str,
) -> tuple[dict[str, Any], bool] | None:
    return db.claim_tool_call(run_id, step_id, tool_name=tool_name, args=args, idempotency_key=idempotency_key)

def complete_tool_call(
    db: Database, 
    tool_call_id: str, 
    *, 
    result: dict[str, Any], 
    status: str, 
    latency_ms: int, 
    error: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    audit: bool = False,
    step_status: str | None = None,
    step_output: dict[str, Any] | None = None,
    step_error: dict[str, Any] | None = None,
    manual_review: bool = False,
) -> dict[str, Any] | None:
    return db.complete_tool_call(
        tool_call_id, result=result, status=status, latency_ms=latency_ms, error=error,
        checkpoint=checkpoint, audit=audit, step_status=step_status,
        step_output=step_output, step_error=step_error, manual_review=manual_review,
    )


def count_tool_calls(db: Database, run_id: str) -> int:
    return db.count_tool_calls(run_id)

def mark_waiting_for_user(
    db: Database, run_id: str, payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return db.mark_waiting_for_user(run_id, payload)