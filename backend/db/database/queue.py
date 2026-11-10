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

from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import Session

from ...services.run_names import OutboxStatus, RunStatus, StepEvent
from .. import orm
from .support import _open_scope, _queue_last_seen, _row_dict, utcnow

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

    def fail_unclaimed_exhausted(
        self,
        older_than: datetime,
        limit: int = 100,
        *,
        max_redispatches: int | None = 3,
        max_age: timedelta | None = None,
        open_only: bool | None = None,
        not_before: datetime | None = None,
    ) -> int:
        """Fail queued runs that sweeper redispatch can no longer help.

        Two cases, both terminal because re-running would only produce a stale reply:

        - exhausted: still unclaimed after `max_redispatches` redispatches. Pass `None` to
          switch this case off, and `open_only` to restrict it to one queue. The caller
          must only enable it for a queue it has seen empty: a run waiting behind a deep
          backlog is slow, not lost.
        - expired: no sign of life for `max_age`. Sign of life is the later of `queued_at`
          (entry or redispatch) and `queue_heartbeat_at` (a worker retrying a SoftBusy run),
          so a run that is merely waiting for capacity never expires, while one orphaned
          before the redispatch columns existed, or during a sweeper outage, does. It is
          never restricted by queue: Redis cannot say anything about a message that was lost.
        """
        now = utcnow()
        dispatched = select(orm.OutboxJob.id).where(
            orm.OutboxJob.job_type == "execute_run",
            orm.OutboxJob.status == OutboxStatus.DISPATCHED,
            orm.OutboxJob.payload_json["run_id"].as_string() == orm.AgentRun.id,
            orm.OutboxJob.dispatch_at < older_than,
        ).exists()
        exhausted = (
            and_(orm.AgentRun.redispatch_count >= max_redispatches, _open_scope(open_only), dispatched)
            if max_redispatches is not None
            else false()
        )
        expired = _queue_last_seen() < now - max_age if max_age is not None else false()
        with self.tx() as s:
            runs = list(s.scalars(
                select(orm.AgentRun)
                .where(
                    orm.AgentRun.status == RunStatus.QUEUED,
                    orm.AgentRun.queued_at < older_than,
                    _queue_last_seen() < older_than,
                    _queue_last_seen() >= not_before if not_before is not None else True,
                    or_(exhausted, expired),
                )
                .order_by(orm.AgentRun.queued_at, orm.AgentRun.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ))
            for run in runs:
                last_seen = max(t for t in (run.queued_at, run.queue_heartbeat_at) if t is not None)
                is_expired = max_age is not None and last_seen < now - max_age
                code = "STUCK_QUEUE_EXPIRED" if is_expired else "STUCK_REDISPATCH_EXHAUSTED"
                reason = code.lower()
                error = {
                    "kind": "permanent",
                    "code": code,
                    "message": (
                        "run stayed queued past the maximum queue age"
                        if is_expired
                        else "run stayed queued after the maximum sweeper redispatches"
                    ),
                }
                # QUEUED has no edge to FAILED in the state machine; the run passes through
                # RUNNING (which bumps `attempts`) so the transition table stays unchanged.
                self._transition(
                    s, run, RunStatus.RUNNING, {"reason": reason}, now,
                    actor_type="sweeper", actor_id="queue-sweep",
                )
                self._fail_locked_run(s, run, error, reason, now)
                self._dead_letter(s, run, error, now, attempts=int(run.attempts))
            s.flush()
            return len(runs)

    def record_worker_loss_redelivery(
        self, run_id: str, *, max_attempts: int = WORKER_LOST_MAX_REDELIVERIES,
    ) -> str:
        """Count a broker redelivery and dead-letter a repeatedly lost run."""
        now = utcnow()
        with self.tx() as s:
            run = s.scalars(
                select(orm.AgentRun).where(orm.AgentRun.id == run_id).with_for_update()
            ).first()
            if run is None or run.status in (
                RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.WAITING_FOR_USER,
            ):
                return "ack"
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
                return "continue"
            if run.status == RunStatus.QUEUED:
                self._transition(
                    s, run, RunStatus.RUNNING, {"reason": "worker_lost_dead_letter"}, now,
                    actor_type="sweeper", actor_id="worker-loss",
                )
            if run.status in (RunStatus.RUNNING, RunStatus.WAITING_FOR_TASKS):
                self._fail_locked_run(s, run, error, "dead_letter", now)
            self._dead_letter(s, run, error, now, attempts=count)
            s.flush()
            return "dead_letter"

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

    def mark_outbox_retry(self, outbox_id: str, error: str) -> dict[str, Any] | None:
        """Push a pending outbox row forward after a failed publish.

        Past `max_attempts` the outbox row is `failed` and one `dead_letter_jobs` row is written,
        and a still-queued run is moved running then failed in this transaction.
        """
        now = utcnow()
        cap_seconds = timedelta(minutes=15).total_seconds()
        with self.tx() as s:
            job = s.scalars(
                select(orm.OutboxJob)
                .where(orm.OutboxJob.id == outbox_id)
                .with_for_update()
            ).first()
            if job is None or job.status != OutboxStatus.PENDING:
                return None
            attempts = int(job.attempts) + 1
            job.attempts = attempts
            job.last_error_json = {"message": error}
            if attempts > int(job.max_attempts):
                job.status = OutboxStatus.FAILED
                payload = job.payload_json if isinstance(job.payload_json, dict) else {}
                run_id = payload.get("run_id")
                run = s.scalars(
                    select(orm.AgentRun)
                    .where(orm.AgentRun.id == run_id)
                    .with_for_update()
                ).first() if isinstance(run_id, str) else None
                failure = {"kind": "dead_letter", "message": error}
                if run is not None and run.status == RunStatus.QUEUED:
                    self._transition(
                        s, run, RunStatus.RUNNING, {"reason": "outbox_dead_letter"}, now,
                        actor_type="dispatcher", actor_id="outbox",
                    )
                    self._transition(
                        s, run, RunStatus.FAILED, {"reason": "dead_letter"}, now, error=failure,
                        actor_type="dispatcher", actor_id="outbox",
                    )
                s.add(orm.DeadLetterJob(
                    id=f"dlj_{uuid.uuid4().hex}",
                    original_job_id=job.id,
                    job_type=job.job_type,
                    payload_json=job.payload_json or {},
                    attempts=attempts,
                    error_json=failure,
                    created_at=now,
                ))
                s.flush()
                return _row_dict(job)
            delay_seconds = min(cap_seconds, 2 ** (attempts - 1))
            job.next_attempt_at = now + timedelta(seconds=delay_seconds)
            s.flush()
            return _row_dict(job)

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

    def fail_run(self, run_id: str, error: dict[str, Any]) -> dict[str, Any] | None:
        """Permanent failure: fail the run, fail its outbox, and keep one dead letter."""
        now = utcnow()
        with self.tx() as s:
            run = self._lock_running_run(s, run_id)
            if run is None:
                return None
            self._fail_locked_run(s, run, error, "permanent", now)
            self._dead_letter(s, run, error, now, attempts=int(run.attempts))
            s.flush()
            return _row_dict(run)

    def schedule_retry(
        self,
        run_id: str,
        error: dict[str, Any],
        delays: tuple[timedelta, ...],
        *,
        spend_attempt: bool = True,
        delay: timedelta | None = None,
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
            if attempts > EXECUTION_MAX_ATTEMPTS:
                if box is not None:
                    box.attempts = attempts
                self._fail_locked_run(s, run, error, "dead_letter", now)
                self._dead_letter(s, run, error, now, attempts=attempts)
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

    def _dead_letter(
        self, s: Session, run: orm.AgentRun, error: dict[str, Any], now: datetime, *, attempts: int,
    ) -> None:
        box = self._latest_execute_outbox(s, run.id)
        if box is not None and isinstance(box.payload_json, dict):
            payload = dict(box.payload_json)
            original = box.id
            box.status = OutboxStatus.FAILED
            box.last_error_json = error
        else:
            payload = self._execute_payload(run)
            original = run.id
        s.add(orm.DeadLetterJob(
            id=f"dlj_{uuid.uuid4().hex}",
            original_job_id=original,
            job_type="execute_run",
            payload_json=payload,
            attempts=attempts,
            error_json=error,
            created_at=now,
        ))

    def _fail_locked_run(
        self, s: Session, run: orm.AgentRun, error: dict[str, Any], reason: str, now: datetime,
    ) -> None:
        self._transition(
            s, run, RunStatus.FAILED, {"status": RunStatus.FAILED, "reason": reason}, now, error=error,
        )
