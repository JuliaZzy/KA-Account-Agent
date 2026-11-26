"""Outbox delivery, execution retries, and dead letters.

Publishing a message and marking the outbox row dispatched stay separate
transactions, so a crash between them leaves the outbox pending.

The execution budget is an outbox row whose `max_attempts` is 3, the old
queue default. Publish retries keep the row created with the run, whose
limit is 10. A lease sweep and an infrastructure requeue do not increment
the execution counter.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ...services.run_names import OutboxStatus, RunStatus, StepEvent
from .. import orm
from ..outcome import Outcome
from .support import _row_dict, utcnow

# How many times `work_once` may enter a run before a transient error is dead-lettered.
EXECUTION_MAX_ATTEMPTS = 3
# A redelivered Celery message may mean the worker process was lost. Keep a
# separate durable budget so a poison task cannot loop through the broker forever.
WORKER_LOST_MAX_REDELIVERIES = 3


class QueueMixin:
    def purge_dispatched_outbox(self, older_than: datetime, limit: int = 1000) -> int:
        """Delete old dispatched rows after their run reached a terminal state.

        The batch is selected with `SKIP LOCKED` so cleanup cannot block dispatch or
        accidentally remove the only evidence for a still-queued run.
        """
        terminal = {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        active_run = select(orm.AgentRun.id).where(
            orm.AgentRun.id == orm.OutboxJob.payload_json["run_id"].as_string(),
            orm.AgentRun.status.not_in(terminal),
        ).exists()
        with self.tx() as s:
            jobs = list(s.scalars(
                select(orm.OutboxJob)
                .where(
                    orm.OutboxJob.status == OutboxStatus.DISPATCHED,
                    orm.OutboxJob.dispatch_at < older_than,
                    ~active_run,
                )
                .order_by(orm.OutboxJob.dispatch_at, orm.OutboxJob.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ))
            removed = 0
            for job in jobs:
                if job.job_type == "execute_run":
                    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
                    run_id = payload.get("run_id")
                    run = s.get(orm.AgentRun, run_id) if isinstance(run_id, str) else None
                    if run is not None and run.status not in terminal:
                        continue
                s.delete(job)
                removed += 1
            s.flush()
            return removed

    def record_worker_loss_redelivery(
        self, run_id: str, *, max_attempts: int = WORKER_LOST_MAX_REDELIVERIES,
    ) -> tuple[str, list[str]]:
        """Count a broker redelivery and dead-letter a repeatedly lost run.

        The list is empty unless this call wrote the run to `failed`. The caller
        notifies after this commit.
        """
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun).where(orm.AgentRun.id == run_id).with_for_update()
            ).first()
            if run is None or run.status in (
                RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.WAITING_FOR_USER,
            ):
                return "ack", []
            boxes = list(s.scalars(
                select(orm.OutboxJob)
                .where(
                    orm.OutboxJob.job_type == "execute_run",
                    orm.OutboxJob.payload_json["run_id"].as_string() == run_id,
                )
                .order_by(orm.OutboxJob.created_at)
                .with_for_update()
            ))
            box = boxes[-1] if boxes else None
            count = 0
            for item in boxes:
                value = (
                    item.last_error_json.get("worker_loss_redeliveries", 0)
                    if isinstance(item.last_error_json, dict) else 0
                )
                try:
                    count = max(count, int(value))
                except (TypeError, ValueError):
                    continue
            try:
                count = int(count) + 1
            except (TypeError, ValueError):
                count = 1
            error = {
                "kind": "worker_lost",
                "code": "WORKER_LOST_REDELIVERY",
                "message": "Celery redelivered the task after worker loss",
                "worker_loss_redeliveries": count,
            }
            if box is not None:
                box.last_error_json = error
            else:
                run.error_json = error
            if count <= max_attempts:
                s.flush()
                return "continue", []
            if run.status == RunStatus.QUEUED:
                self._transition(
                    s, run, RunStatus.RUNNING, {"reason": "worker_lost_dead_letter"}, now,
                    actor_type="sweeper", actor_id="worker-loss",
                )
            terminal: list[str] = []
            if run.status in (RunStatus.RUNNING, RunStatus.WAITING_FOR_TASKS):
                self._fail_locked_run(
                    s, run, error, "dead_letter", now,
                    actor_type="sweeper", actor_id="worker-loss", attempts=count,
                )
                terminal.append(run.id)
            s.flush()
            return "dead_letter", terminal

    def defer_pending_outbox(self, outbox_id: str, seconds: int = 5) -> None:
        """Move one pending row out of the head of the scan without starting its wait over."""
        now = utcnow()
        with self.tx() as s:
            job = s.scalars(
                select(orm.OutboxJob)
                .where(orm.OutboxJob.id == outbox_id, orm.OutboxJob.status == OutboxStatus.PENDING)
                .with_for_update()
            ).first()
            if job is None:
                return
            job.next_attempt_at = now + timedelta(seconds=seconds)

    def find_pending_outbox_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Pending outbox rows whose `next_attempt_at` has arrived, oldest first."""
        now = utcnow()
        with self.tx() as s:
            rows = s.scalars(
                select(orm.OutboxJob)
                .where(orm.OutboxJob.status == OutboxStatus.PENDING, orm.OutboxJob.next_attempt_at <= now)
                .order_by(orm.OutboxJob.next_attempt_at)
                .limit(limit)
            )
            return [_row_dict(row) for row in rows]

    def mark_outbox_retry(self, outbox_id: str, error: str) -> tuple[dict[str, Any] | None, list[str]]:
        """Push a pending outbox row forward after a failed publish.

        Past `max_attempts` the outbox row is `failed`. A still-queued run moves
        `queued` to `running`, then `_write_terminal` writes `failed` and the dead
        letter. The list is those run ids. The caller notifies after this commit.
        """
        now = utcnow()
        cap_seconds = timedelta(minutes=15).total_seconds()
        with self.tx() as s:
            # Run, then outbox. `schedule_retry` and `mark_outbox_dispatched` use this order.
            peeked = s.get(orm.OutboxJob, outbox_id)
            if peeked is None or peeked.status != OutboxStatus.PENDING:
                return None, []
            payload = peeked.payload_json if isinstance(peeked.payload_json, dict) else {}
            run_id = payload.get("run_id")
            run = None
            if isinstance(run_id, str):
                run = s.scalars(
                    select(orm.AgentRun).where(orm.AgentRun.id == run_id).with_for_update()
                ).first()
            job = s.scalars(
                select(orm.OutboxJob).where(orm.OutboxJob.id == outbox_id).with_for_update()
            ).first()
            if job is None or job.status != OutboxStatus.PENDING:
                return None, []
            attempts = int(job.attempts) + 1
            job.attempts = attempts
            job.last_error_json = {"message": error}
            if attempts > int(job.max_attempts):
                job.status = OutboxStatus.FAILED
                terminal: list[str] = []
                # From QUEUED. There is no queued → failed edge, so the run passes through RUNNING.
                if run is not None and run.status == RunStatus.QUEUED:
                    failure = {"kind": "dead_letter", "message": error}
                    self._transition(
                        s, run, RunStatus.RUNNING, {"reason": "outbox_dead_letter"}, now,
                        actor_type="dispatcher", actor_id="outbox",
                    )
                    self._write_terminal(
                        s, run, Outcome(
                            kind="failed", error=failure, reason="dead_letter", dead_letter=True,
                        ), now, actor_type="dispatcher", actor_id="outbox", attempts=attempts,
                    )
                    terminal.append(run.id)
                s.flush()
                return _row_dict(job), terminal
            delay_seconds = min(cap_seconds, 2 ** (attempts - 1))
            job.next_attempt_at = now + timedelta(seconds=delay_seconds)
            s.flush()
            return _row_dict(job), []

    def list_dead_letter_jobs(self, original_job_id: str) -> list[dict[str, Any]]:
        """Dead-letter rows for one outbox id or run id, oldest first."""
        with self.tx() as s:
            rows = s.scalars(
                select(orm.DeadLetterJob)
                .where(orm.DeadLetterJob.original_job_id == original_job_id)
                .order_by(orm.DeadLetterJob.created_at, orm.DeadLetterJob.id)
            )
            return [_row_dict(row) for row in rows]

    def mark_outbox_dispatched(self, outbox_id: str) -> dict[str, Any] | None:
        """Mark one outbox row dispatched. This is its own transaction, after the publish.

        The run row is locked before the outbox row. `schedule_retry` uses that same
        order, so the two cannot deadlock while a worker is already running the run.
        """
        now = utcnow()
        with self.tx() as s:
            peeked = s.get(orm.OutboxJob, outbox_id)
            if peeked is None or peeked.status != OutboxStatus.PENDING:
                return None
            payload = peeked.payload_json if isinstance(peeked.payload_json, dict) else {}
            run_id = payload.get("run_id")
            if not isinstance(run_id, str):
                return None
            run = s.scalars(
                select(orm.AgentRun).where(orm.AgentRun.id == run_id).with_for_update()
            ).first()
            if run is None:
                return None
            job = s.scalars(
                select(orm.OutboxJob).where(orm.OutboxJob.id == outbox_id).with_for_update()
            ).first()
            if job is None or job.status != OutboxStatus.PENDING:
                return None
            job.status = OutboxStatus.DISPATCHED
            job.dispatch_at = now
            self._append_event(s, run_id, StepEvent.RUN_ENQUEUED, {"outbox_job_id": job.id}, now)
            s.flush()
            return _row_dict(job)

    def schedule_retry(
        self,
        run_id: str,
        error: dict[str, Any],
        delays: tuple[timedelta, ...],
        *,
        spend_attempt: bool = True,
        delay: timedelta | None = None,
        attempt_limit: int | None = None,
    ) -> str | None:
        """Count one transient failure. `retry` queues the run again; `dead_letter` stops it.

        `spend_attempt=False` is an infrastructure requeue: the run goes back to queued
        and an outbox is armed, but the execution counter stays where it was.
        `delay` overrides the computed wait, used when the vendor sent Retry-After.
        None when the run is no longer running, for example because it was cancelled.
        """
        now = utcnow()
        with self.tx() as s:
            run = self._lock_running_run(s, run_id)
            if run is None:
                return None
            box = self._execution_outbox(s, run_id)
            if not spend_attempt:
                previous = box.last_error_json if box is not None else None
                previous_count = (
                    previous.get("infrastructure_attempts", 0) if isinstance(previous, dict) else 0
                )
                try:
                    infrastructure_attempts = int(previous_count) + 1
                except (TypeError, ValueError):
                    infrastructure_attempts = 1
                error = {**error, "infrastructure_attempts": infrastructure_attempts}
                if delay is not None:
                    when = now + delay
                else:
                    base_seconds = max(1.0, delays[0].total_seconds()) if delays else 1.0
                    backoff = min(60.0, base_seconds * (2 ** min(infrastructure_attempts - 1, 6)))
                    jittered = max(0.05, random.uniform(0.0, backoff))
                    when = now + timedelta(seconds=jittered)
                self._arm_execution_outbox(
                    s, run, box, attempts=int(box.attempts) if box else 0, when=when, error=error, now=now,
                )
                self._transition(
                    s, run, RunStatus.QUEUED,
                    {"kind": "infrastructure", "available_at": when.isoformat()},
                    now, error=error,
                )
                s.flush()
                return "retry"
            attempts = (int(box.attempts) if box is not None else 0) + 1
            # From RUNNING. The outbox counter is already past this failure's cap.
            limit = EXECUTION_MAX_ATTEMPTS if attempt_limit is None else attempt_limit
            if attempts > limit:
                if box is not None:
                    box.attempts = attempts
                self._write_terminal(
                    s, run, Outcome(
                        kind="failed", error=error, reason="dead_letter", dead_letter=True,
                    ), now, attempts=attempts,
                )
                s.flush()
                return "dead_letter"
            when = now + delays[min(attempts, len(delays)) - 1]
            self._arm_execution_outbox(
                s, run, box, attempts=attempts, when=when, error=error, now=now,
            )
            self._transition(
                s, run, RunStatus.QUEUED,
                {"attempts": attempts, "available_at": when.isoformat()},
                now, error=error,
            )
            s.flush()
            return "retry"

    def _lock_running_run(self, s: Session, run_id: str) -> orm.AgentRun | None:
        return s.scalars(
            select(orm.AgentRun)
            .where(orm.AgentRun.id == run_id, orm.AgentRun.status == RunStatus.RUNNING)
            .with_for_update()
        ).first()

    def _execution_outbox(self, s: Session, run_id: str) -> orm.OutboxJob | None:
        return s.scalars(
            select(orm.OutboxJob)
            .where(
                orm.OutboxJob.job_type == "execute_run",
                orm.OutboxJob.max_attempts == EXECUTION_MAX_ATTEMPTS,
                orm.OutboxJob.payload_json["run_id"].as_string() == run_id,
            )
            .order_by(orm.OutboxJob.created_at.desc())
            .limit(1)
            .with_for_update()
        ).first()

    def _arm_execution_outbox(
        self,
        s: Session,
        run: orm.AgentRun,
        box: orm.OutboxJob | None,
        *,
        attempts: int,
        when: datetime,
        error: dict[str, Any],
        now: datetime,
    ) -> None:
        if box is None:
            s.add(orm.OutboxJob(
                id=f"obx_{uuid.uuid4().hex}",
                job_type="execute_run",
                payload_json=self._execute_payload(run),
                status=OutboxStatus.PENDING,
                attempts=attempts,
                max_attempts=EXECUTION_MAX_ATTEMPTS,
                next_attempt_at=when,
                last_error_json=error,
                created_at=now,
                dispatch_at=now,
            ))
            return
        box.attempts = attempts
        box.status = OutboxStatus.PENDING
        box.next_attempt_at = when
        box.last_error_json = error

    def _latest_execute_outbox(self, s: Session, run_id: str) -> orm.OutboxJob | None:
        return s.scalars(
            select(orm.OutboxJob)
            .where(
                orm.OutboxJob.job_type == "execute_run",
                orm.OutboxJob.payload_json["run_id"].as_string() == run_id,
            )
            .order_by(orm.OutboxJob.created_at.desc())
            .limit(1)
            .with_for_update()
        ).first()

    def record_plan_failure(self, run_id: str, error: dict[str, Any]) -> None:
        """记一条计划不可用的事件。对话还要继续回答，所以这不是死信。"""
        now = utcnow()
        with self.tx() as s:
            run = s.get(orm.AgentRun, run_id)
            if run is None:
                return
            self._append_event(s, run_id, "plan.unusable", error, now)

    def _dead_letter(
        self, s: Session, run: orm.AgentRun, error: dict[str, Any], now: datetime, *, attempts: int,
    ) -> None:
        error = dict(error)
        call_id = self._latest_failed_model_call(s, run.id)
        if call_id:
            error["failed_model_call_id"] = call_id
        box = self._latest_execute_outbox(s, run.id)
        if box is not None and isinstance(box.payload_json, dict):
            payload = dict(box.payload_json)
            box.status = OutboxStatus.FAILED
            box.last_error_json = error
        else:
            payload = self._execute_payload(run)
        # One letter per run. The key is the run id, not whichever outbox was latest.
        s.execute(pg_insert(orm.DeadLetterJob).values(
            id=f"dlj_{uuid.uuid4().hex}",
            original_job_id=run.id,
            job_type="execute_run",
            payload_json=payload,
            attempts=attempts,
            error_json=error,
            created_at=now,
        ).on_conflict_do_nothing(constraint="uq_dead_letter_original_job"))

    @staticmethod
    def _latest_failed_model_call(s: Session, run_id: str) -> str | None:
        """放弃这条 Run 时，指向最近一次失败的模型调用。没有就空着。"""
        row = s.scalars(
            select(orm.ModelCall)
            .where(orm.ModelCall.run_id == run_id, orm.ModelCall.status == "failed")
            .order_by(orm.ModelCall.created_at.desc(), orm.ModelCall.id.desc())
            .limit(1)
        ).first()
        return None if row is None else row.id

    def _fail_locked_run(
        self,
        s: Session,
        run: orm.AgentRun,
        error: dict[str, Any],
        reason: str,
        now: datetime,
        *,
        actor_type: str = "worker",
        actor_id: str | None = None,
        attempts: int | None = None,
    ) -> None:
        """Fail a row this transaction already locked. The dead letter is part of `_write_terminal`."""
        self._write_terminal(
            s, run, Outcome(
                kind="failed", error=error, reason=reason, dead_letter=True,
            ), now, actor_type=actor_type, actor_id=actor_id, attempts=attempts,
        )
