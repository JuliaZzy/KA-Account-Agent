"""Agent runs, the status machine, and the trace read.

Status is assigned only in `_transition`. `_append_event` and `_append_audit`
share the caller's transaction. A single leading underscore keeps those helpers
visible to the other mixins on `Database`; a double underscore would be renamed
per class and the queue mixin could not call them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...services.run_names import (
    PROJECTED_CHILD_EVENTS,
    RUN_EVENT_CHANNEL,
    OutboxStatus,
    RunStatus,
    RunType,
    StepEvent,
    ToolCallStatus,
    event_display,
)
from .. import orm
from .support import (
    ALLOWED_RUN_TRANSITIONS,
    RUN_TRANSITION_EVENTS,
    ChatTurnBusy,
    InvalidRunTransition,
    _row_dict,
    utcnow,
)

# Prometheus histogram edges, in seconds. The last bucket is +Inf and is the row count.
_DURATION_EDGES = (1, 5, 15, 30, 60, 120, 300)
_LAG_EDGES = (0.1, 0.3, 1, 5, 10, 30, 60)
_MODEL_EDGES = (0.5, 1, 2, 5, 10, 30, 60)
_DURATION_LABELS = ("≤1s", "≤5s", "≤15s", "≤30s", "≤60s", "≤2min", "≤5min", ">5min")
_LAG_LABELS = ("≤0.1s", "≤0.3s", "≤1s", "≤5s", "≤10s", "≤30s", "≤1min", ">1min")


def _hour_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _fill_hours(start: datetime, rows: list[Any]) -> list[dict[str, Any]]:
    found: dict[datetime, tuple[int, int]] = {}
    for stamp, created, failed in rows:
        found[_hour_key(stamp)] = (int(created or 0), int(failed or 0))
    points: list[dict[str, Any]] = []
    cursor = _hour_key(start)
    for _ in range(24):
        created, failed = found.get(cursor, (0, 0))
        points.append({"at": cursor.isoformat(), "created": created, "failed": failed})
        cursor += timedelta(hours=1)
    return points


def _bars(hist: dict[str, Any], labels: tuple[str, ...], *, tail_from: int) -> list[dict[str, Any]]:
    """Turn cumulative histogram buckets into one bar per range."""
    counts = [*hist["buckets"], hist["count"]]
    previous = 0
    bars: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        count = int(counts[index] or 0) - previous
        previous = int(counts[index] or 0)
        bars.append({"label": label, "count": count, "tail": index >= tail_from})
    return bars


def _histogram(
    s: Session, seconds: Any, edges: tuple[float, ...], *filters: Any, joined: type | None = None,
) -> dict[str, Any]:
    columns: list[Any] = [func.count().filter(seconds <= edge) for edge in edges]
    columns.append(func.count())
    columns.append(func.coalesce(func.sum(seconds), 0))
    columns.append(func.percentile_cont(0.95).within_group(seconds))
    query = select(*columns)
    if joined is orm.ModelCall:
        query = query.select_from(orm.ModelCall).join(
            orm.AgentRun, orm.ModelCall.run_id == orm.AgentRun.id,
        )
    row = s.execute(query.where(*filters)).one()
    width = len(edges)
    p95 = row[width + 2]
    return {
        "edges": list(edges),
        "buckets": [int(row[index] or 0) for index in range(width)],
        "count": int(row[width] or 0),
        "sum": float(row[width + 1] or 0),
        "p95": None if p95 is None else float(p95),
    }


def _wave_child(child: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """One child of `commit_task_wave`: key, run type, input, and the owner's started event."""
    if not isinstance(child, dict):
        raise ValueError("task wave child must be an object")
    key = child.get("idempotency_key")
    run_type = child.get("run_type")
    run_input = child.get("input")
    started = child.get("started")
    if not isinstance(key, str) or not key:
        raise ValueError("task wave child needs an idempotency key")
    if not isinstance(run_type, str) or not run_type:
        raise ValueError("task wave child needs a run type")
    if not isinstance(run_input, dict):
        raise ValueError("task wave child input must be an object")
    if not isinstance(started, dict):
        raise ValueError("task wave child needs a started event")
    return key, run_type, run_input, started


class RunMixin:
    def find_agent_run(self, org_id: str, run_id: str) -> dict[str, Any] | None:
        """Load one run. `org_id` is part of the lookup, so another org's id looks missing."""
        with self.tx() as s:
            row = s.scalars(
                select(orm.AgentRun).where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
            ).first()
            return _row_dict(row) if row else None

    def find_agent_run_by_idempotency(self, org_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.tx() as s:
            row = s.scalars(
                select(orm.AgentRun).where(
                    orm.AgentRun.org_id == org_id, orm.AgentRun.idempotency_key == idempotency_key,
                )
            ).first()
            return _row_dict(row) if row else None

    def list_run_events(self, org_id: str, run_id: str, after: int = 0) -> list[dict[str, Any]] | None:
        """Events with `sequence` greater than `after`, in order. None when this org has no such run."""
        with self.tx() as s:
            owned = s.scalars(
                select(orm.AgentRun.id).where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
            ).first()
            if owned is None:
                return None
            rows = s.scalars(
                select(orm.RunEvent)
                .where(orm.RunEvent.run_id == run_id, orm.RunEvent.sequence > after)
                .order_by(orm.RunEvent.sequence)
            )
            return [_row_dict(row) for row in rows]

    def transition_run(
        self,
        org_id: str,
        run_id: str,
        to_status: str,
        payload: dict[str, Any] | None = None,
        *,
        from_status: frozenset[str] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Move one run along a legal edge and append that edge's event, in one transaction.
        None when this org has no such run. `from_statuses` narrows which current
        statuses the caller accepts; the whitelist still applies. An illegal edge
        raises InvalidRunTransition and the transaction rolls back.
        """
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
                .with_for_update()
            ).first()
            if run is None:
                return None
            if from_status is not None and run.status not in from_status:
                raise InvalidRunTransition(run.status, to_status)
            self._transition(
                s, run, to_status, payload or {}, now, error=error, actor_type="api", actor_id=run.user_id,
            )
            s.flush()
            return _row_dict(run)

    def _transition(
        self,
        s: Session,
        run: orm.AgentRun,
        to_status: str,
        payload: dict[str, Any],
        now: datetime,
        *,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        actor_type: str = "worker",
        actor_id: str | None = None,
    ) -> None:
        """The one place that assigns `agent_runs.status`. The row is already locked."""
        source = run.status
        if to_status not in ALLOWED_RUN_TRANSITIONS.get(source, frozenset()):
            raise InvalidRunTransition(source, to_status)
        who = actor_id or lease_owner or run.lease_owner or run.user_id
        self._apply_status_fields(run, to_status, now, lease_owner=lease_owner, lease_expires_at=lease_expires_at, output=output, error=error)
        run.status = to_status
        run.updated_at = now
        event_payload = {
            "from_status": source,
            "to_status": to_status,
            "actor_type": actor_type,
            "actor_id": who,
        }
        event_payload.update(payload)
        self._append_event(s, run.id, RUN_TRANSITION_EVENTS[(source, to_status)], event_payload, now)
        self._append_audit(s, run, event_payload, now, who)
        if source in (RunStatus.WAITING_FOR_USER, RunStatus.WAITING_FOR_TASKS) and to_status == RunStatus.QUEUED:
            self._add_execute_outbox(s, run, now)
            s.flush()

    def _apply_status_fields(
        self,
        run: orm.AgentRun,
        to_status: str,
        now: datetime,
        *,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Columns that ride along with the status. The event is written by `_transition`.
        `agent_runs.attempts` counts entries into `running`. The execution budget
        that dead-letters a run lives on the outbox row, counted in `schedule_retry`.
        """
        if to_status == RunStatus.RUNNING:
            if run.started_at is None:
                run.started_at = now
            run.attempts = int(run.attempts) + 1
            run.lease_owner = lease_owner
            run.lease_expires_at = lease_expires_at
        elif to_status in (RunStatus.WAITING_FOR_USER, RunStatus.WAITING_FOR_TASKS):
            run.lease_owner = None
            run.lease_expires_at = None
        elif to_status == RunStatus.QUEUED:
            run.lease_owner = None
            run.lease_expires_at = None
            if error is not None:
                run.error_json = error
        elif to_status == RunStatus.SUCCEEDED:
            run.output_json = output
            run.ended_at = now
        elif to_status == RunStatus.FAILED:
            run.error_json = error
            run.ended_at = now
            run.lease_owner = None
            run.lease_expires_at = None
        elif to_status == RunStatus.CANCELLED:
            run.ended_at = now
            run.lease_owner = None
            run.lease_expires_at = None

    def request_run_cancel(
        self,
        org_id: str,
        run_id: str,
        reason: str | None,
    ) -> dict[str, Any] | None:
        """Mark a running run for cancel. Status stays `running`. None when this org has no such run."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
                .with_for_update()
            ).first()
            if run is None:
                return None
            if run.status != RunStatus.RUNNING:
                return _row_dict(run)
            run.cancel_requested_at = now
            run.updated_at = now
            payload: dict[str, Any] = {"status": RunStatus.RUNNING}
            if reason:
                payload["reason"] = reason
            self._append_event(s, run_id, StepEvent.RUN_CANCEL_REQUESTED, payload, now)
            s.flush()
            return _row_dict(run)

    def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        """Worker path: running to cancelled, plus `run.cancelled`. None unless the flag is set."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.RUNNING)
                .with_for_update()
            ).first()
            if run is None or run.cancel_requested_at is None:
                return None
            self._transition(s, run, RunStatus.CANCELLED, {"status": RunStatus.CANCELLED}, now)
            s.flush()
            return _row_dict(run)

    @staticmethod
    def _execute_payload(run: orm.AgentRun) -> dict[str, Any]:
        payload: dict[str, Any] = {"run_id": run.id}
        if isinstance(run.trace_id, str) and run.trace_id:
            payload["trace_id"] = run.trace_id
        return payload

    @staticmethod
    def _write_event(
        s: Session,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> orm.RunEvent:
        s.scalars(
            select(orm.AgentRun).where(orm.AgentRun.id == run_id).with_for_update()
        ).first()
        current = s.scalar(
            select(func.max(orm.RunEvent.sequence)).where(orm.RunEvent.run_id == run_id)
        )
        body = dict(payload)
        if not (isinstance(body.get("display"), str) and body["display"].strip()):
            sentence = event_display(event_type, body)
            if sentence:
                body["display"] = sentence
        event = orm.RunEvent(
            id=f"evt_{uuid.uuid4().hex}",
            run_id=run_id,
            sequence=int(current or 0) + 1,
            event_type=event_type,
            payload_json=body,
            created_at=now,
        )
        s.add(event)
        try:
            with s.begin_nested():
                s.execute(
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": RUN_EVENT_CHANNEL, "payload": run_id},
                )
        except Exception:
            pass
        return event

    @staticmethod
    def _append_event(
        s: Session,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> orm.RunEvent:
        # Serialize sequence assignment on this run. A second lock in the same
        # transaction is the one `_transition` already holds.
        event = RunMixin._write_event(s, run_id, event_type, payload, now)
        if event_type not in PROJECTED_CHILD_EVENTS or payload.get("projected_from"):
            return event
        locked = s.get(orm.AgentRun, run_id)
        if locked is not None and locked.run_type == RunType.OPEN:
            return event
        owner_id = None if locked is None else locked.owner_run_id
        source_id = run_id
        while owner_id:
            copy = dict(payload)
            copy["projected_from"] = source_id
            copy["child_run_id"] = source_id
            RunMixin._write_event(s, owner_id, event_type, copy, now)
            owner = s.get(orm.AgentRun, owner_id)
            owner_id = None if owner is None else owner.owner_run_id
        return event

    def _append_audit(
        self, 
        s: Session,
        run: orm.AgentRun,
        metadata: dict[str, Any],
        now: datetime,
        actor_id: str 
    ) -> None:
        s.add(orm.AuditLog(
            id = f"aud_{uuid.uuid4().hex}",
            user_id = actor_id,
            org_id = run.org_id,
            run_id = run.id,
            action = "run.transition",
            target_type = "agent_run",
            target_id = run.id,
            metadata_json = metadata,
            created_at = now,
        ))

    def append_run_event(
        self, org_id: str, run_id: str, event_type: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Append the next `sequence` for this run. None when the org has no such run."""
        now = utcnow()
        with self.tx() as s:
            owned = s.scalars(
                select(orm.AgentRun.id).where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
            ).first()
            if owned is None:
                return None
            event = self._append_event(s, run_id, event_type, payload, now)
            s.flush()
            return _row_dict(event)

    def create_agent_run(
        self,
        org_id: str,
        user_id: str,
        run_type: str,
        idempotency_key: str,
        run_input: dict[str, Any],
        parent_run_id: str | None = None,
        owner_run_id: str | None = None,
        max_model_calls: int | None = None,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
        max_duration_seconds: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert the queued run, `run.created`, and a pending `execute_run` outbox row together.

        False means this org already has the idempotency key, and the returned row is that run. That replay does not add another outbox row.
        """
        now = utcnow()
        run_id = f"run_{uuid.uuid4().hex}"
        try:
            with self.tx() as s:
                run = self._insert_queued_run(
                    s,
                    run_id=run_id,
                    org_id=org_id,
                    user_id=user_id,
                    run_type=run_type,
                    idempotency_key=idempotency_key,
                    run_input=run_input,
                    now=now,
                    parent_run_id=parent_run_id,
                    owner_run_id=owner_run_id,
                    max_model_calls=max_model_calls,
                    max_tokens=max_tokens,
                    max_tool_calls=max_tool_calls,
                    max_duration_seconds=max_duration_seconds,
                )
                return _row_dict(run), True
        except IntegrityError:
            existing = self.find_agent_run_by_idempotency(org_id, idempotency_key)
            if existing is None:
                raise
            return existing, False

    def _insert_queued_run(
        self,
        s: Session,
        *,
        run_id: str,
        org_id: str,
        user_id: str,
        run_type: str,
        idempotency_key: str,
        run_input: dict[str, Any],
        now: datetime,
        parent_run_id: str | None = None,
        owner_run_id: str | None = None,
        max_model_calls: int | None = None,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
        max_duration_seconds: int | None = None,
    ) -> orm.AgentRun:
        """Insert a queued run, `run.created`, and its execute outbox. The caller owns the transaction."""
        run = orm.AgentRun(
            id=run_id,
            org_id=org_id,
            user_id=user_id,
            run_type=run_type,
            idempotency_key=idempotency_key,
            status=RunStatus.QUEUED,
            input_json=run_input,
            parent_run_id=parent_run_id,
            owner_run_id=owner_run_id,
            trace_id=uuid.uuid4().hex,
            max_model_calls=max_model_calls,
            max_tokens=max_tokens,
            max_tool_calls=max_tool_calls,
            max_duration_seconds=max_duration_seconds,
            created_at=now,
            updated_at=now,
        )
        s.add(run)
        s.flush()
        created: dict[str, Any] = {"status": RunStatus.QUEUED}
        if parent_run_id is not None:
            created["parent_run_id"] = parent_run_id
        if owner_run_id is not None:
            created["owner_run_id"] = owner_run_id
        self._append_event(s, run_id, StepEvent.RUN_CREATED, created, now)
        self._add_execute_outbox(s, run, now)
        s.flush()
        return run

    def find_agent_run_by_id(self, run_id: str) -> dict[str, Any] | None:
        """Load a run by primary key. The worker has noorg header; the HTTP API still filters by org."""
        with self.tx() as s:
            row = s.get(orm.AgentRun, run_id)
            return _row_dict(row) if row else None

    def claim_run(self, run_id: str, worker_id: str, lease_expires_at: datetime) -> dict[str, Any] | None:
        """Claim a queued run and append `run.started` together. None when it is no longer queued."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.QUEUED)
                .with_for_update()
            ).first()
            if run is None:
                return None
            self._transition(s, run, RunStatus.RUNNING, {"lease_owner": worker_id}, now, lease_owner=worker_id, lease_expires_at=lease_expires_at)
            s.flush()
            return _row_dict(run)

    def claim_chat_turn(self, run_id: str, worker_id: str, lease_expires_at: datetime) -> dict[str, Any] | None:
        """Claim a queued run. A chat turn also waits while that session is already running."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.QUEUED)
                .with_for_update()
            ).first()
            if run is None:
                return None
            payload = run.input_json if isinstance(run.input_json, dict) else {}
            session_id = payload.get("session_id")
            if run.run_type == "chat" and isinstance(session_id, str) and session_id:
                s.scalars(
                    select(orm.ChatSession)
                    .where(orm.ChatSession.id == session_id)
                    .with_for_update()
                ).first()
                other = s.scalars(
                    select(orm.AgentRun.id)
                    .where(
                        orm.AgentRun.id != run_id,
                        orm.AgentRun.status.in_((RunStatus.RUNNING, RunStatus.WAITING_FOR_TASKS)),
                        orm.AgentRun.run_type == "chat",
                        orm.AgentRun.input_json["session_id"].as_string() == session_id,
                    )
                    .limit(1)
                ).first()
                if other is not None:
                    raise ChatTurnBusy(run_id)
            self._transition(
                s, run, RunStatus.RUNNING, {"lease_owner": worker_id}, now,
                lease_owner=worker_id, lease_expires_at=lease_expires_at,
            )
            s.flush()
            return _row_dict(run)

    def sweep_expired_leases(self, limit: int = 100) -> int:
        """Return expired running leases to queued and append a new execute outbox.

        This does not spend an execution attempt. A run that already asked to cancel
        is cancelled and does not get another outbox row.
        """
        now = utcnow()
        with self.tx() as s:
            runs = list(s.scalars(
                select(orm.AgentRun)
                .where(
                    orm.AgentRun.status == RunStatus.RUNNING,
                    orm.AgentRun.lease_expires_at.is_not(None),
                    orm.AgentRun.lease_expires_at < now,
                )
                .order_by(orm.AgentRun.lease_expires_at)
                .limit(limit)
                .with_for_update()
            ))
            from .queue import EXECUTION_MAX_ATTEMPTS

            for run in runs:
                if run.cancel_requested_at is not None:
                    self._transition(
                        s, run, RunStatus.CANCELLED, {"reason": "lease_expired"}, now,
                        actor_type="sweeper", actor_id="lease-sweep",
                    )
                    continue
                if int(run.attempts) >= EXECUTION_MAX_ATTEMPTS:
                    self._transition(
                        s, run, RunStatus.FAILED, {"reason": "lease_expired"}, now,
                        error={"kind": "transient", "message": "execution attempts exhausted"},
                        actor_type="sweeper", actor_id="lease-sweep",
                    )
                    continue
                self._transition(
                    s, run, RunStatus.QUEUED, {"reason": "lease_expired"}, now,
                    actor_type="sweeper", actor_id="lease-sweep",
                )
                self._add_execute_outbox(s, run, now)
                self._append_event(
                    s, run.id, StepEvent.RUN_REQUEUED_BY_SWEEPER, {"reason": "lease_expired"}, now,
                )
            s.flush()
            return len(runs)

    def sweep_waiting_for_tasks(
        self, limit: int = 100, stale_after: timedelta = timedelta(minutes=15),
    ) -> int:
        """Wake owners whose children already finished. Lost Redis counts leave them parked.

        A run still has queued or running children is left waiting. This does not
        treat the row as `waiting_for_user`.
        """
        now = utcnow()
        cutoff = now - stale_after
        woke = 0
        with self.tx() as s:
            runs = list(s.scalars(
                select(orm.AgentRun)
                .where(
                    orm.AgentRun.status == RunStatus.WAITING_FOR_TASKS,
                    orm.AgentRun.updated_at < cutoff,
                )
                .order_by(orm.AgentRun.updated_at)
                .limit(limit)
                .with_for_update()
            ))
            busy = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_FOR_TASKS)
            for run in runs:
                children = list(s.scalars(
                    select(orm.AgentRun).where(orm.AgentRun.owner_run_id == run.id)
                ))
                if any(child.status in busy for child in children):
                    continue
                self._transition(s, run, RunStatus.QUEUED, {"reason": "waiting_tasks_stale"}, now)
                woke += 1
            s.flush()
            return woke

    def sweep_attention(
        self,
        *,
        queue_after: timedelta = timedelta(minutes=10),
        waiting_after: timedelta = timedelta(days=1),
        tool_after: timedelta = timedelta(seconds=90),
        limit: int = 100,
    ) -> None:
        """Note stale queue and confirmation rows, and close side effects left running.

        Run status changes go through `_transition`. A reminder does not change status
        and does not confirm or skip a tool. `waiting_for_user` is left waiting.
        """
        now = utcnow()
        self._note_stale_queued(now - queue_after, limit)
        self._note_waiting(now - waiting_after, limit)
        self._fail_over_duration(now, limit)
        self._lose_side_effects(now - tool_after, limit)

    def unclaimed_dispatches(self, older_than: datetime, limit: int = 100) -> list[dict[str, Any]]:
        """Dispatched execute rows whose run is still queued. The caller publishes again."""
        with self.tx() as s:
            rows = s.scalars(
                select(orm.OutboxJob)
                .where(
                    orm.OutboxJob.job_type == "execute_run",
                    orm.OutboxJob.status == OutboxStatus.DISPATCHED,
                    orm.OutboxJob.dispatch_at < older_than,
                )
                .order_by(orm.OutboxJob.dispatch_at)
                .limit(limit)
            )
            payloads: list[dict[str, Any]] = []
            for job in rows:
                payload = job.payload_json if isinstance(job.payload_json, dict) else {}
                run_id = payload.get("run_id")
                if not isinstance(run_id, str):
                    continue
                run = s.get(orm.AgentRun, run_id)
                if run is not None and run.status == RunStatus.QUEUED:
                    payloads.append(dict(payload))
            return payloads

    def _note_stale_queued(self, cutoff: datetime, limit: int) -> None:
        now = utcnow()
        with self.tx() as s:
            runs = list(s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.status == RunStatus.QUEUED, orm.AgentRun.updated_at < cutoff)
                .order_by(orm.AgentRun.updated_at)
                .limit(limit)
            ))
            for run in runs:
                dispatched = s.scalars(
                    select(orm.OutboxJob.id)
                    .where(
                        orm.OutboxJob.status == OutboxStatus.DISPATCHED,
                        orm.OutboxJob.payload_json["run_id"].as_string() == run.id,
                    )
                    .limit(1)
                ).first()
                if dispatched is None or self._has_event(s, run.id, StepEvent.RUN_QUEUE_STALE):
                    continue
                self._append_event(s, run.id, StepEvent.RUN_QUEUE_STALE, {"reason": "queue_unclaimed"}, now)
            s.flush()

    def _note_waiting(self, cutoff: datetime, limit: int) -> None:
        now = utcnow()
        with self.tx() as s:
            runs = list(s.scalars(
                select(orm.AgentRun)
                .where(
                    orm.AgentRun.status == RunStatus.WAITING_FOR_USER,
                    orm.AgentRun.updated_at < cutoff,
                )
                .order_by(orm.AgentRun.updated_at)
                .limit(limit)
            ))
            for run in runs:
                if self._has_event(s, run.id, StepEvent.RUN_WAITING_REMINDER):
                    continue
                self._append_event(
                    s, run.id, StepEvent.RUN_WAITING_REMINDER, {"reason": "waiting_for_user"}, now,
                )
            s.flush()

    def _fail_over_duration(self, now: datetime, limit: int) -> None:
        with self.tx() as s:
            runs = list(s.scalars(
                select(orm.AgentRun)
                .where(
                    orm.AgentRun.status == RunStatus.RUNNING,
                    orm.AgentRun.max_duration_seconds.is_not(None),
                    orm.AgentRun.started_at.is_not(None),
                )
                .limit(limit)
                .with_for_update()
            ))
            for run in runs:
                cap = int(run.max_duration_seconds or 0)
                started = run.started_at
                if started is None or now < started + timedelta(seconds=cap):
                    continue
                if run.cancel_requested_at is not None:
                    self._transition(
                        s, run, RunStatus.CANCELLED, {"reason": "duration"}, now,
                        actor_type="sweeper", actor_id="duration",
                    )
                    continue
                self._transition(
                    s, run, RunStatus.FAILED, {"reason": "duration"}, now,
                    error={
                        "kind": "permanent",
                        "code": "COST_BUDGET_EXCEEDED",
                        "message": "run duration exceeded",
                    },
                    actor_type="sweeper", actor_id="duration",
                )
            s.flush()

    def _lose_side_effects(self, cutoff: datetime, limit: int) -> None:
        with self.tx() as s:
            rows = list(s.scalars(
                select(orm.ToolCall)
                .where(
                    orm.ToolCall.status == "running",
                    orm.ToolCall.started_at.is_not(None),
                    orm.ToolCall.started_at < cutoff,
                    or_(
                        orm.ToolCall.tool_name == "send_email",
                        orm.ToolCall.idempotency_key.startswith("send_email:"),
                        orm.ToolCall.idempotency_key.startswith("mcp:"),
                    ),
                )
                .limit(limit)
            ))
            ids = [row.id for row in rows]
        for tool_call_id in ids:
            try:
                self.complete_tool_call(
                    tool_call_id, result={}, status="response_lost", latency_ms=0,
                    error={"code": "TOOL_TIMEOUT", "message": "side effect was left running", "retryable": False},
                    audit=True, manual_review=True,
                )
            except Exception:
                continue

    def _has_event(self, s: Session, run_id: str, event_type: str) -> bool:
        found = s.scalars(
            select(orm.RunEvent.id)
            .where(orm.RunEvent.run_id == run_id, orm.RunEvent.event_type == event_type)
            .limit(1)
        ).first()
        return found is not None

    def org_daily_usage(self, org_id: str) -> dict[str, int]:
        """Tokens and estimated cents stored for this distributor since UTC midnight."""
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with self.tx() as s:
            tokens = s.scalar(
                select(func.coalesce(func.sum(orm.UsageRecord.total_tokens), 0)).where(
                    orm.UsageRecord.org_id == org_id, orm.UsageRecord.created_at >= start,
                )
            )
            cost = s.scalar(
                select(func.coalesce(func.sum(orm.UsageRecord.estimated_cost_cents), 0)).where(
                    orm.UsageRecord.org_id == org_id, orm.UsageRecord.created_at >= start,
                )
            )
            return {"total_tokens": int(tokens or 0), "estimated_cost_cents": int(cost or 0)}

    def record_repair(
        self,
        org_id: str,
        run_id: str,
        user_id: str,
        *,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Write an audit row for a hand edit. The run status is not changed."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
                .with_for_update()
            ).first()
            if run is None:
                return None
            s.add(orm.AuditLog(
                id=f"aud_{uuid.uuid4().hex}",
                user_id=user_id,
                org_id=org_id,
                run_id=run.id,
                action="data.repair",
                target_type="agent_run",
                target_id=run.id,
                metadata_json={"reason": reason, "before": before, "after": after},
                created_at=now,
            ))
            s.flush()
            return _row_dict(run)

    def _add_execute_outbox(self, s: Session, run: orm.AgentRun, now: datetime) -> None:
        s.add(orm.OutboxJob(
            id=f"obx_{uuid.uuid4().hex}",
            job_type="execute_run",
            payload_json=self._execute_payload(run),
            status=OutboxStatus.PENDING,
            next_attempt_at=now,
            created_at=now,
            dispatch_at=now,
        ))

    def complete_run(self, run_id: str, output: dict[str, Any]) -> dict[str, Any] | None:
        """Write `succeeded`, `output_json`, and `run.completed` together. None when the run is not running."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.RUNNING)
                .with_for_update()
            ).first()
            if run is None:
                return None
            if run.cancel_requested_at is not None:
                return None
            self._transition(s, run, RunStatus.SUCCEEDED, {"status": RunStatus.SUCCEEDED}, now, output=output)
            s.flush()
            return _row_dict(run)

    def note_running_reply(
        self, run_id: str, text: str, *, generation_id: str | None = None,
        reasoning: str | None = None,
    ) -> dict[str, Any] | None:
        """Store reply text while the run is still running. The status stays running.

        `generation_id` labels this attempt so the desk can drop tokens from a
        failed stream when the worker retries. None when the run is missing or
        not running.
        """
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.RUNNING)
                .with_for_update()
            ).first()
            if run is None:
                return None
            payload: dict[str, Any] = {"text": text}
            if generation_id:
                payload["generation_id"] = generation_id
            if reasoning:
                payload["reasoning"] = reasoning
            run.output_json = payload
            if text:
                self._append_event(s, run_id, StepEvent.RUN_REPLY_UPDATED, payload, utcnow())
            s.flush()
            return _row_dict(run)

    def mark_waiting_for_user(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Park a running run and append `run.waiting_for_user`. The actor is the worker.
        
        None when the run is not running, or `cancel_requested_at` is already set. 
        """
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.RUNNING)
                .with_for_update()
            ).first()
            if run is None or run.cancel_requested_at is not None:
                return None
            self._transition(s, run, RunStatus.WAITING_FOR_USER, payload or {}, now)
            s.flush()
            return _row_dict(run)

    def mark_waiting_for_tasks(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Park a running run while child tasks execute. The actor is the worker.

        None when the run is not running, or `cancel_requested_at` is already set.
        """
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.RUNNING)
                .with_for_update()
            ).first()
            if run is None or run.cancel_requested_at is not None:
                return None
            self._transition(s, run, RunStatus.WAITING_FOR_TASKS, payload or {}, now)
            s.flush()
            return _row_dict(run)

    def commit_task_wave(
        self,
        owner_run_id: str,
        children: list[dict[str, Any]],
        checkpoint: dict[str, Any],
        wait_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Park one wave in a single commit.

        Each child is a queued run, `run.created`, and an execute outbox. The owner
        also gets one `tool.started` per child, the checkpoint, and the move from
        `running` to `waiting_for_tasks`. A child idempotency key that already exists
        is reused and does not get another outbox row.

        None when the owner is missing, not running, already asked to cancel, or
        `children` is empty. Redis and the outbox publish stay outside this commit.
        """
        now = utcnow()
        specs = [_wave_child(child) for child in children]
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == owner_run_id, orm.AgentRun.status == RunStatus.RUNNING)
                .with_for_update()
            ).first()
            if run is None or run.cancel_requested_at is not None or not specs:
                return None
            self._require_checkpoint_state(checkpoint)
            keys = [key for key, _run_type, _run_input, _started in specs]
            existing = s.scalars(
                select(orm.AgentRun).where(
                    orm.AgentRun.org_id == run.org_id,
                    orm.AgentRun.idempotency_key.in_(keys),
                )
            ).all()
            by_key = {row.idempotency_key: row for row in existing}
            for key, run_type, run_input, started in specs:
                if key not in by_key:
                    by_key[key] = self._insert_queued_run(
                        s,
                        run_id=f"run_{uuid.uuid4().hex}",
                        org_id=run.org_id,
                        user_id=run.user_id,
                        run_type=run_type,
                        idempotency_key=key,
                        run_input=run_input,
                        now=now,
                        owner_run_id=run.id,
                    )
                self._append_event(s, run.id, StepEvent.TOOL_STARTED, started, now)
            if self._write_checkpoint(s, run.id, checkpoint, now) is None:
                raise RuntimeError("owner missing while committing a task wave")
            self._transition(s, run, RunStatus.WAITING_FOR_TASKS, wait_payload or {}, now)
            s.flush()
            return _row_dict(run)

    def requeue_waiting_tasks(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Wake a run that was waiting for a wave. Adds an execute outbox."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.WAITING_FOR_TASKS)
                .with_for_update()
            ).first()
            if run is None or run.cancel_requested_at is not None:
                return None
            self._transition(s, run, RunStatus.QUEUED, payload or {"reason": "wave_ready"}, now)
            s.flush()
            return _row_dict(run)

    def find_runs_by_owner(self, owner_run_id: str) -> list[dict[str, Any]]:
        with self.tx() as s:
            rows = s.scalars(
                select(orm.AgentRun)
                .where(orm.AgentRun.owner_run_id == owner_run_id)
                .order_by(orm.AgentRun.created_at)
            ).all()
            return [_row_dict(row) for row in rows]

    def run_trace(self, org_id: str, run_id: str) -> dict[str, Any] | None:
        """Run plus its steps, events, model calls, tool calls and checkpoints.

        None when the run is missing or belongs to another org. The lists stay empty until a
        later chapter writes those rows; the final reply is only `output_json`.
        """
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun).where(orm.AgentRun.id == run_id, orm.AgentRun.org_id == org_id)
            ).first()
            if run is None:
                return None
            return {
                "run": _row_dict(run),
                "steps": self._run_rows(s, orm.AgentStep, orm.AgentStep.step_index, run_id),
                "events": self._run_rows(s, orm.RunEvent, orm.RunEvent.sequence, run_id),
                "model_calls": self._run_rows(s, orm.ModelCall, orm.ModelCall.created_at, run_id),
                "tool_calls": self._run_rows(s, orm.ToolCall, orm.ToolCall.created_at, run_id),
                "checkpoints": self._run_rows(s, orm.Checkpoint, orm.Checkpoint.checkpoint_index, run_id),
                "usage_records": self._run_rows(s, orm.UsageRecord, orm.UsageRecord.created_at, run_id),
                "audit_logs": self._run_rows(s, orm.AuditLog, orm.AuditLog.created_at, run_id),
                "outbox": self._payload_rows(s, orm.OutboxJob, run_id),
                "dead_letters": self._payload_rows(s, orm.DeadLetterJob, run_id),
            }

    def list_agent_runs(
        self, org_id: str, *, user_id: str | None, status: str | None, limit: int,
    ) -> list[dict[str, Any]]:
        """Newest runs for this distributor. `user_id` and `status` narrow the same org."""
        with self.tx() as s:
            query = select(orm.AgentRun).where(orm.AgentRun.org_id == org_id)
            if user_id:
                query = query.where(orm.AgentRun.user_id == user_id)
            if status:
                query = query.where(orm.AgentRun.status == status)
            query = query.order_by(orm.AgentRun.created_at.desc(), orm.AgentRun.id.desc()).limit(limit)
            return [_row_dict(row) for row in s.scalars(query)]

    def monitor_facts(self, org_id: str) -> dict[str, Any]:
        """Counts and latency distributions already stored for this distributor.

        Queue depth and the SSE gauge are not in these tables. The monitor reads those
        from Redis and leaves this method as the ledger.
        """
        now = utcnow()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stuck_queued = and_(
            orm.AgentRun.status == RunStatus.QUEUED,
            orm.AgentRun.created_at <= now - timedelta(minutes=10),
        )
        stuck_running = and_(
            orm.AgentRun.status == RunStatus.RUNNING,
            orm.AgentRun.lease_expires_at.is_not(None),
            orm.AgentRun.lease_expires_at < now,
        )
        with self.tx() as s:
            org = orm.AgentRun.org_id == org_id
            duration = func.extract("epoch", orm.AgentRun.ended_at - orm.AgentRun.started_at)
            lag = func.extract(
                "epoch", func.coalesce(orm.AgentRun.started_at, func.now()) - orm.AgentRun.created_at,
            )
            model_seconds = orm.ModelCall.latency_ms / 1000.0
            facts = {
                "created": self._count(s, orm.AgentRun, org),
                "succeeded": self._count(s, orm.AgentRun, org, orm.AgentRun.status == RunStatus.SUCCEEDED),
                "failed": self._count(s, orm.AgentRun, org, orm.AgentRun.status == RunStatus.FAILED),
                "duration": _histogram(
                    s, duration, _DURATION_EDGES,
                    org, orm.AgentRun.started_at.is_not(None), orm.AgentRun.ended_at.is_not(None),
                ),
                "queue_lag": _histogram(s, lag, _LAG_EDGES, org),
                "model_latency": _histogram(
                    s, model_seconds, _MODEL_EDGES,
                    org, orm.ModelCall.latency_ms.is_not(None),
                    joined=orm.ModelCall,
                ),
                "model_errors": self._count(
                    s, orm.ModelCall, org, orm.ModelCall.status == "failed", joined=orm.ModelCall,
                ),
                "model_rate_limited": self._count(
                    s, orm.ModelCall, org,
                    orm.ModelCall.error_json["code"].astext == "PROVIDER_RATE_LIMITED",
                    joined=orm.ModelCall,
                ),
                "tool_calls": self._count(s, orm.ToolCall, orm.ToolCall.org_id == org_id),
                "tool_errors": self._count(
                    s, orm.ToolCall, orm.ToolCall.org_id == org_id,
                    orm.ToolCall.status.in_((ToolCallStatus.FAILED, ToolCallStatus.RESPONSE_LOST)),
                ),
                "tool_timeouts": self._count(
                    s, orm.ToolCall, orm.ToolCall.org_id == org_id,
                    orm.ToolCall.error_json["code"].astext == "TOOL_TIMEOUT",
                ),
                "send_email_calls": self._count(
                    s, orm.ToolCall, orm.ToolCall.org_id == org_id, orm.ToolCall.tool_name == "send_email",
                ),
                "send_email_errors": self._count(
                    s, orm.ToolCall, orm.ToolCall.org_id == org_id, orm.ToolCall.tool_name == "send_email",
                    orm.ToolCall.status.in_((ToolCallStatus.FAILED, ToolCallStatus.RESPONSE_LOST)),
                ),
                "mcp_calls": self._count(
                    s, orm.ToolCall, orm.ToolCall.org_id == org_id,
                    orm.ToolCall.idempotency_key.startswith("mcp:"),
                ),
                "mcp_errors": self._count(
                    s, orm.ToolCall, orm.ToolCall.org_id == org_id,
                    orm.ToolCall.idempotency_key.startswith("mcp:"),
                    orm.ToolCall.status.in_((ToolCallStatus.FAILED, ToolCallStatus.RESPONSE_LOST)),
                ),
                "duplicates": self._count(
                    s, orm.RunEvent, org, orm.RunEvent.payload_json["skipped_duplicate"].astext == "true",
                    joined=orm.RunEvent,
                ),
                "cost_cents": int(s.scalar(
                    select(func.coalesce(func.sum(orm.UsageRecord.estimated_cost_cents), 0)).where(
                        orm.UsageRecord.org_id == org_id,
                    )
                ) or 0),
                "daily_cost_cents": int(s.scalar(
                    select(func.coalesce(func.sum(orm.UsageRecord.estimated_cost_cents), 0)).where(
                        orm.UsageRecord.org_id == org_id, orm.UsageRecord.created_at >= day,
                    )
                ) or 0),
                "daily_tokens": int(s.scalar(
                    select(func.coalesce(func.sum(orm.UsageRecord.total_tokens), 0)).where(
                        orm.UsageRecord.org_id == org_id, orm.UsageRecord.created_at >= day,
                    )
                ) or 0),
                "active_runs": self._count(
                    s, orm.AgentRun, org, orm.AgentRun.status == RunStatus.RUNNING,
                    or_(
                        orm.AgentRun.lease_expires_at.is_(None),
                        orm.AgentRun.lease_expires_at >= now,
                    ),
                ),
                "stuck_runs": self._count(s, orm.AgentRun, org, or_(stuck_queued, stuck_running)),
                "failed_codes": self._failed_codes(s, org_id),
            }
            return facts

    def monitor_charts(self, org_id: str) -> dict[str, Any]:
        """The last 24 hours, from rows that already exist.

        Gauges such as the queue depth are not here: nothing stores their history.
        """
        now = utcnow()
        start = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        with self.tx() as s:
            org = orm.AgentRun.org_id == org_id
            recent = orm.AgentRun.created_at >= start
            hour = func.date_trunc("hour", orm.AgentRun.created_at)
            rows = s.execute(
                select(
                    hour,
                    func.count(),
                    func.count().filter(orm.AgentRun.status == RunStatus.FAILED),
                )
                .where(org, recent)
                .group_by(hour)
            ).all()
            duration = func.extract("epoch", orm.AgentRun.ended_at - orm.AgentRun.started_at)
            lag = func.extract(
                "epoch", func.coalesce(orm.AgentRun.started_at, func.now()) - orm.AgentRun.created_at,
            )
            duration_hist = _histogram(
                s, duration, _DURATION_EDGES,
                org, recent, orm.AgentRun.started_at.is_not(None), orm.AgentRun.ended_at.is_not(None),
            )
            lag_hist = _histogram(s, lag, _LAG_EDGES, org, recent)
        return {
            "hours": _fill_hours(start, rows),
            "duration": _bars(duration_hist, _DURATION_LABELS, tail_from=5),
            "duration_p95": duration_hist["p95"],
            "queue": _bars(lag_hist, _LAG_LABELS, tail_from=5),
            "queue_p95": lag_hist["p95"],
        }

    @staticmethod
    def _count(s: Session, model: type, *filters: Any, joined: type | None = None) -> int:
        query = select(func.count()).select_from(model)
        if joined is orm.ModelCall:
            query = query.join(orm.AgentRun, orm.ModelCall.run_id == orm.AgentRun.id)
        elif joined is orm.RunEvent:
            query = query.join(orm.AgentRun, orm.RunEvent.run_id == orm.AgentRun.id)
        return int(s.scalar(query.where(*filters)) or 0)

    @staticmethod
    def _failed_codes(s: Session, org_id: str) -> list[dict[str, Any]]:
        code = func.coalesce(orm.AgentRun.error_json["code"].astext, "unknown")
        rows = s.execute(
            select(code, func.count())
            .where(orm.AgentRun.org_id == org_id, orm.AgentRun.status == RunStatus.FAILED)
            .group_by(code)
            .order_by(func.count().desc())
            .limit(5)
        )
        return [{"code": str(name), "count": int(count)} for name, count in rows]

    @staticmethod
    def _payload_rows(s: Session, model: type, run_id: str) -> list[dict[str, Any]]:
        rows = s.scalars(
            select(model)
            .where(model.payload_json["run_id"].astext == run_id)
            .order_by(model.created_at, model.id)
        )
        return [_row_dict(row) for row in rows]

    @staticmethod
    def _run_rows(s: Session, model: type, order: Any, run_id: str) -> list[dict[str, Any]]:
        rows = s.scalars(select(model).where(model.run_id == run_id).order_by(order))
        return [_row_dict(row) for row in rows]
