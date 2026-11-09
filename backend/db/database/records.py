"""Steps, checkpoints, model calls, and tool calls for one harness iteration.

Most methods are their own transaction. Closing a tool step is the exception:
the tool row, the step, the checkpoint, the events, and a side-effect audit
row commit together. The run row is locked while the next step or checkpoint
index is chosen. Event names match `StepEvent`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ...services.run_names import RunStatus, StepEvent, StepStatus, StepType, ToolCallStatus
from .. import orm
from .support import _row_dict, utcnow

_STEP_TYPES = frozenset({StepType.MODEL, StepType.TOOL})
_STEP_DONE = frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED})
_TOOL_DONE = frozenset({ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED})
_TOOL_SETTLE = _TOOL_DONE | {ToolCallStatus.RESPONSE_LOST}
_CHECKPOINT_KEYS = frozenset({"context", "completed_tool_calls", "last"})


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


class RecordMixin:
    def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.tx() as s:
            row = s.scalars(
                select(orm.Checkpoint)
                .where(orm.Checkpoint.run_id == run_id)
                .order_by(orm.Checkpoint.checkpoint_index.desc())
                .limit(1)
            ).first()
            return _row_dict(row) if row else None

    def append_checkpoint(self, run_id: str, state: dict[str, Any]) -> int:
        self._require_checkpoint_state(state)
        with self.tx() as s:
            row = self._write_checkpoint(s, run_id, state, utcnow())
            return _row_dict(row) if row is not None else None

    def record_child_wave_result(
        self,
        org_id: str,
        owner_run_id: str,
        task_id: str,
        result: dict[str, Any],
        *,
        event_type: str | None,
        event_payload: dict[str, Any] | None,
    ) -> bool | None:
        """Merge one child result on the owner checkpoint and optionally append a tool event.

        True commits both. None when the owner is missing. False when the org does not match.
        """
        now = utcnow()
        with self.tx() as s:
            run = self._lock_run(s, owner_run_id)
            if run is None:
                return None
            if org_id and run.org_id != org_id:
                return False
            previous = s.scalars(
                select(orm.Checkpoint)
                .where(orm.Checkpoint.run_id == owner_run_id)
                .order_by(orm.Checkpoint.checkpoint_index.desc())
                .limit(1)
            ).first()
            state = dict(previous.state_json or {}) if previous is not None else {}
            results = dict(state.get("orchestrator", {}).get("results") or {})
            orchestrator = dict(state.get("orchestrator") or {})
            results[task_id] = result
            finished = [str(item) for item in (orchestrator.get("finished") or []) if item]
            if task_id not in finished:
                finished.append(task_id)
            orchestrator["results"] = results
            orchestrator["finished"] = finished
            state["orchestrator"] = orchestrator
            if self._write_checkpoint(s, owner_run_id, state, now) is None:
                return None
            if event_type and event_payload is not None:
                self._append_event(s, owner_run_id, event_type, event_payload, now)
            s.flush()
            return True

    def insert_step(
        self, 
        run_id: str, 
        step_type: str, 
        name: str, 
        step_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Open one iteration as `running` and append `step_started`.

        None when the run is missing or is not `running`. The caller picks
        `name` with `step_name` before this write. 
        """
        if step_type not in _STEP_TYPES:
            raise ValueError(f"Invalid step type: {step_type}")
        if not isinstance(name, str) or not name:
            raise ValueError("`name` is required")
        now = utcnow()
        with self.tx() as s:
            run = self._lock_run(s, run_id)
            if run is None or run.status != RunStatus.RUNNING:
                return None
            current = s.scalar(
                select(func.max(orm.AgentStep.step_index))
                .where(orm.AgentStep.run_id == run_id)
            )
            row = orm.AgentStep(
                id=f"step_{uuid.uuid4().hex}",
                run_id=run_id,
                status=StepStatus.RUNNING,
                step_index=int(current or 0) + 1,
                step_type=step_type,
                name=name,
                input_json=step_input,
                started_at=now,
            )
            s.add(row)
            s.flush()
            self._append_event(s, run_id, StepEvent.STEP_STARTED, {
                "step_id": row.id, "step_index": row.step_index, "step_type": row.step_type, "name": name
            }, now)
            s.flush()
            return _row_dict(row)

    def finish_step(
        self, 
        step_id: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        extra_event: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Close a running step. A second call returns the row and does not append again.

        `checkpoint` and `extra_event` are written in this same transaction.
        A call that does not call a tool (unknown tool, or over the tool budget)
        uses them so the step, the checkpoint, and `tool.failed` commit together.
        """
        if status not in _STEP_DONE:
            raise ValueError(f"Invalid step status: {status}")
        if checkpoint is not None:
            self._require_checkpoint_state(checkpoint)
        now = utcnow()
        with self.tx() as s:
            step = s.scalars(
                select(orm.AgentStep)
                .where(orm.AgentStep.id == step_id)
                .with_for_update()
            ).first()
            if step is None:
                return None
            if step.status == StepStatus.RUNNING:
                self._close_step(s, step, status, output, error, now)
            if extra_event:
                self._append_event(s, step.run_id, extra_event, extra_payload or {}, now)
            if checkpoint is not None and self._write_checkpoint(s, step.run_id, checkpoint, now) is None:
                raise RuntimeError("run missing while closing a step")
            s.flush()
            return _row_dict(step)

    def insert_model_call(
        self,
        run_id: str,
        step_id: str,
        *, 
        provider: str,
        model: str,
        request: dict[str, Any],
        response: dict[str, Any],
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        status: str,
        cache_hit_tokens: int | None = None,
        cache_miss_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        error: dict[str, Any] | None = None,
        usage_source: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        """Record one finished model call. None when the step is not on this run. 
        
        The row is written after the call returns, so `created_at` is the write
        time and `latency_ms` is the measured duration.
        """
        if status not in _STEP_DONE:
            raise ValueError("model call status must be succeeded or failed")
        now = utcnow()
        prompt_tokens = max(0, int(prompt_tokens))
        completion_tokens = max(0, int(completion_tokens))
        cache_hit_tokens = _optional_nonnegative_int(cache_hit_tokens)
        cache_miss_tokens = _optional_nonnegative_int(cache_miss_tokens)
        cache_creation_tokens = _optional_nonnegative_int(cache_creation_tokens)
        with self.tx() as s:
            step = self._lock_step(s, run_id, step_id)
            if step is None:
                return None
            run = s.get(orm.AgentRun, run_id)
            if run is None:
                return None
            row = orm.ModelCall(
                id = f"mcall_{uuid.uuid4().hex}",
                run_id = run_id,
                step_id = step_id,
                provider = provider,
                model = model,
                request_json = request,
                response_json = response or {},
                prompt_tokens = prompt_tokens,
                completion_tokens = completion_tokens,
                cache_hit_tokens = cache_hit_tokens,
                cache_miss_tokens = cache_miss_tokens,
                cache_creation_tokens = cache_creation_tokens,
                usage_source = usage_source,
                latency_ms = latency_ms,
                status = status,
                error_json = error or {},
                created_at = now,
            )
            s.add(orm.UsageRecord(
                id=f"usage_{uuid.uuid4().hex}",
                run_id=run_id,
                org_id=run.org_id,
                user_id=run.user_id,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                cache_creation_tokens=cache_creation_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                estimated_cost_cents=0,
                usage_source=usage_source,
                created_at=now,
            ))
            s.add(row)
            s.flush()
            if emit_event:
                event = StepEvent.MODEL_FAILED if status == StepStatus.FAILED else StepEvent.MODEL_COMPLETED
                self._append_event(s, run_id, event, {
                    "step_id": step_id, "model_call_id": row.id, "status": status
                }, now)
            s.flush()
            return _row_dict(row)

    def model_budget(self, run_id: str) -> dict[str, Any] | None:
        """Caps and spend already stored for this run. None when the run is missing.

        A null cap is not a limit. A refused call is not in these totals.
        """
        with self.tx() as s:
            run = s.get(orm.AgentRun, run_id)
            if run is None:
                return None
            calls = s.scalar(
                select(func.count()).select_from(orm.ModelCall).where(orm.ModelCall.run_id == run_id)
            )
            tokens = s.scalar(
                select(func.coalesce(func.sum(orm.UsageRecord.total_tokens), 0)).where(
                    orm.UsageRecord.run_id == run_id
                )
            )
            return {
                "org_id": run.org_id,
                "trace_id": run.trace_id,
                "max_model_calls": run.max_model_calls,
                "max_tokens": run.max_tokens,
                "model_calls": int(calls or 0),
                "total_tokens": int(tokens or 0),
            }

    def insert_tool_call(
        self,
        run_id: str,
        step_id: str,
        *, 
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        status: str,
        idempotency_key: str, 
        latency_ms: int,
        error: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        step_status: str | None = None,
        step_output: dict[str, Any] | None = None,
        step_error: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record one finished tool call.

        A replay does not append another tool event. None when the step is not on this run.
        When `checkpoint` or `step_status` is set, that close happens in this same transaction.
        """
        if status not in _TOOL_DONE:
            raise ValueError("tool call status must be succeeded or failed")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("`idempotency_key` is required")
        if checkpoint is not None:
            self._require_checkpoint_state(checkpoint)
        if step_status is not None and step_status not in _STEP_DONE:
            raise ValueError(f"Invalid step status: {step_status}")
        now = utcnow()
        try:
            with self.tx() as s:
                step = self._lock_step(s, run_id, step_id)
                if step is None:
                    return None
                existing = s.scalars(
                    select(orm.ToolCall)
                    .where(
                        orm.ToolCall.tool_name == tool_name, 
                        orm.ToolCall.idempotency_key == idempotency_key
                    )
                ).first()
                if existing is not None:
                    self._attach_close(
                        s, step, now, step_status=step_status, step_output=step_output,
                        step_error=step_error, checkpoint=checkpoint,
                    )
                    s.flush()
                    return _row_dict(existing)
                run = s.get(orm.AgentRun, run_id)
                row = orm.ToolCall(
                    id = f"tcall_{uuid.uuid4().hex}",
                    run_id = run_id,
                    org_id = None if run is None else run.org_id,
                    user_id = None if run is None else run.user_id,
                    step_id = step_id,
                    tool_name = tool_name,
                    args_json = args,
                    result_json = result or {},
                    status = status,
                    idempotency_key = idempotency_key,
                    latency_ms = latency_ms,
                    error_json = error or {},
                    created_at = now,
                    started_at = now,
                    ended_at = now,
                )
                s.add(row)
                s.flush()
                event = StepEvent.TOOL_FAILED if status == ToolCallStatus.FAILED else StepEvent.TOOL_COMPLETED
                self._append_event(s, run_id, event, {
                    "step_id": step_id, "tool_call_id": row.id, "tool_name": tool_name, "status": status
                }, now)
                self._attach_close(
                    s, step, now, step_status=step_status, step_output=step_output,
                    step_error=step_error, checkpoint=checkpoint,
                )
                s.flush()
                return _row_dict(row)
        except IntegrityError:
            with self.tx() as s:
                step = self._lock_step(s, run_id, step_id)
                existing = s.scalars(
                    select(orm.ToolCall)
                    .where(
                        orm.ToolCall.tool_name == tool_name, 
                        orm.ToolCall.idempotency_key == idempotency_key
                    )
                ).first()
                if existing is None or step is None:
                    raise
                self._attach_close(
                    s, step, utcnow(), step_status=step_status, step_output=step_output,
                    step_error=step_error, checkpoint=checkpoint,
                )
                s.flush()
                return _row_dict(existing)

    def claim_tool_call(
        self,
        run_id: str,
        step_id: str,
        *, 
        tool_name: str,
        args: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool] | None:
        """Reserve one side effect before it runs.

        The bool is True when this call inserted the row.
        False means the key already exists, so the caller must not run the tool again. 
        None when the step is not on this run.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("`idempotency_key` is required")
        now = utcnow()
        try:
            with self.tx() as s:
                step = self._lock_step(s, run_id, step_id)
                if step is None:
                    return None
                existing = s.scalars(
                    select(orm.ToolCall)
                    .where(orm.ToolCall.tool_name == tool_name, orm.ToolCall.idempotency_key == idempotency_key)
                ).first()
                if existing is not None:
                    return _row_dict(existing), False
                run = s.get(orm.AgentRun, run_id)
                row = orm.ToolCall(
                    id = f"tcall_{uuid.uuid4().hex}",
                    run_id = run_id,
                    org_id = None if run is None else run.org_id,
                    user_id = None if run is None else run.user_id,
                    step_id = step_id,
                    tool_name = tool_name,
                    args_json = args,
                    result_json = {},
                    status = ToolCallStatus.RUNNING, 
                    idempotency_key = idempotency_key,
                    latency_ms = 0,
                    created_at = now,
                    started_at = now,
                )
                s.add(row)
                s.flush()
                return _row_dict(row), True
        except IntegrityError:
            with self.tx() as s:
                existing = s.scalars(
                    select(orm.ToolCall)
                    .where(orm.ToolCall.tool_name == tool_name, orm.ToolCall.idempotency_key == idempotency_key)
                ).first()
                if existing is None:
                    raise
                return _row_dict(existing), False

    def complete_tool_call(
        self, 
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
        """Fill a claimed tool call. A second call returns the row and does not append again.

        A side-effect result, its checkpoint, the step, the tool event, and the audit
        row commit in this transaction. The handler itself runs before this is called.
        """
        if status not in _TOOL_SETTLE:
            raise ValueError("tool call status must be succeeded, failed, or response_lost")
        if checkpoint is not None:
            self._require_checkpoint_state(checkpoint)
        if step_status is not None and step_status not in _STEP_DONE:
            raise ValueError(f"Invalid step status: {step_status}")
        now = utcnow()
        with self.tx() as s:
            row = s.scalars(
                select(orm.ToolCall)
                .where(orm.ToolCall.id == tool_call_id)
                .with_for_update()
            ).first()
            if row is None:
                return None
            if row.status != ToolCallStatus.RUNNING:
                if checkpoint is None and step_status is None:
                    return _row_dict(row)
            else:
                row.result_json = result or {}
                row.status = status
                row.latency_ms = latency_ms
                row.error_json = error or {}
                row.ended_at = now
                event = StepEvent.TOOL_COMPLETED if status == ToolCallStatus.SUCCEEDED else StepEvent.TOOL_FAILED
                self._append_event(s, row.run_id, event, {
                    "step_id": row.step_id, "tool_call_id": row.id, "tool_name": row.tool_name, "status": status
                }, now)
                if manual_review:
                    self._append_event(s, row.run_id, StepEvent.TOOL_MANUAL_REVIEW, {
                        "step_id": row.step_id, "tool_call_id": row.id, "tool_name": row.tool_name,
                        "status": status,
                    }, now)
                if audit:
                    self._audit_tool(s, row, status, error, now)
            step = self._lock_step(s, row.run_id, row.step_id)
            if step is None:
                raise RuntimeError("tool call step is missing")
            self._attach_close(
                s, step, now, step_status=step_status, step_output=step_output,
                step_error=step_error, checkpoint=checkpoint,
            )
            s.flush()
            return _row_dict(row)

    def count_tool_calls(self, run_id: str) -> int:
        """Rows already stored for this run, including ones that did not succeed."""
        with self.tx() as s:
            count = s.scalar(
                select(func.count()).select_from(orm.ToolCall).where(orm.ToolCall.run_id == run_id)
            )
            return int(count or 0)

    def list_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self.tx() as s:
            rows = s.scalars(
                select(orm.ToolCall)
                .where(orm.ToolCall.run_id == run_id)
                .order_by(orm.ToolCall.created_at, orm.ToolCall.id)
            )
            return [_row_dict(row) for row in rows]

    def list_audit_logs(self, run_id: str) -> list[dict[str, Any]]:
        with self.tx() as s:
            rows = s.scalars(
                select(orm.AuditLog)
                .where(orm.AuditLog.run_id == run_id)
                .order_by(orm.AuditLog.created_at, orm.AuditLog.id)
            )
            return [_row_dict(row) for row in rows]

    def _close_step(self, s, step, status: str, output, error, now) -> None:
        step.status = status
        step.output_json = output
        step.error_json = error
        step.ended_at = now
        event = StepEvent.STEP_FAILED if status == StepStatus.FAILED else StepEvent.STEP_COMPLETED
        self._append_event(s, step.run_id, event, {
            "step_id": step.id, "step_index": step.step_index, "status": status,
        }, now)

    def _attach_close(
        self, s, step, now, *, step_status, step_output, step_error, checkpoint,
    ) -> None:
        if step_status is not None and step.status == StepStatus.RUNNING:
            self._close_step(s, step, step_status, step_output, step_error, now)
        if checkpoint is not None and self._write_checkpoint(s, step.run_id, checkpoint, now) is None:
            raise RuntimeError("run missing while closing a tool step")

    def _write_checkpoint(self, s, run_id: str, state: dict[str, Any], now):
        run = self._lock_run(s, run_id)
        if run is None:
            return None
        current = s.scalar(
            select(func.max(orm.Checkpoint.checkpoint_index)).where(orm.Checkpoint.run_id == run_id)
        )
        row = orm.Checkpoint(
            id=f"ckpt_{uuid.uuid4().hex}",
            run_id=run_id,
            checkpoint_index=int(current or 0) + 1,
            state_json=state,
            created_at=now,
        )
        s.add(row)
        s.flush()
        return row

    def _audit_tool(self, s, row, status: str, error: dict[str, Any] | None, now) -> None:
        """One audit row for a side effect. The body and the arguments stay out."""
        if not row.org_id or not row.user_id:
            return
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        s.add(orm.AuditLog(
            id=f"aud_{uuid.uuid4().hex}",
            user_id=row.user_id,
            org_id=row.org_id,
            run_id=row.run_id,
            action=str(row.tool_name)[:64],
            target_type="tool_call",
            target_id=row.id,
            metadata_json={"status": str(status), "error_code": code},
            created_at=now,
        ))

    @staticmethod
    def _require_checkpoint_state(state: dict[str, Any]) -> None:
        missing = [key for key in _CHECKPOINT_KEYS if key not in state]
        if missing:
            raise ValueError("checkpoint state needs context, completed_tool_calls, and last")
        if not isinstance(state["context"], list) or not isinstance(state["completed_tool_calls"], list):
            raise ValueError("checkpoint context and completed_tool_calls must be lists")
        if not isinstance(state["last"], str):
            raise ValueError("checkpoint last must be a string")
        if "last_report" in state and not isinstance(state["last_report"], str):
            raise ValueError("checkpoint last_report must be a string")

    @staticmethod
    def _lock_run(s, run_id: str) -> orm.AgentRun | None:
        return s.scalars(
            select(orm.AgentRun)
            .where(orm.AgentRun.id == run_id)
            .with_for_update()
        ).first()

    def list_usage_records(self, run_id: str) -> list[dict[str, Any]]:
        """Usage rows for one run, oldest first."""
        with self.tx() as s:
            rows = s.scalars(
                select(orm.UsageRecord)
                .where(orm.UsageRecord.run_id == run_id)
                .order_by(orm.UsageRecord.created_at, orm.UsageRecord.id)
            )
            return [_row_dict(row) for row in rows]

    @staticmethod
    def _lock_step(s, run_id: str, step_id: str) -> orm.AgentStep | None:
        return s.scalars(
            select(orm.AgentStep)
            .where(orm.AgentStep.id == step_id, orm.AgentStep.run_id == run_id)
            .with_for_update()
        ).first()
