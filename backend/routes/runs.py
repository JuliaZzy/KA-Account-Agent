"""HTTP endpoints for the exercise Run API.

Each function reads the request, calls run_service, and returns the response.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import APIRouter, Body, Depends, Header, Query, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..db import Database
from ..deps import get_db
from ..schemas.run import (
    CancelRunRequest,
    CreateRunRequest,
    MonitorResponse,
    RepairRunRequest,
    RerunRunRequest,
    ResumeRunRequest,
    RunEventResponse,
    RunResponse,
    TraceResponse,
)
from ..services import monitor, run_service
from ..services.entry_guard import admit_create, admit_read
from ..services.run_names import RunStatus
from ..services.run_service import AppError
from ..services.run_stream import iter_event_stream

router = APIRouter()


@dataclass
class CurrentUser:
    org_id: str
    id: str


def get_current_user(
    x_user_id: str = Header(alias="X-User-Id"),
    db: Database = Depends(get_db),
) -> CurrentUser:
    """The user id selects the row. `org_id` is that row's distributor, never a header."""
    person = db.user(x_user_id)
    if person is None:
        raise AppError(401, "UNAUTHENTICATED", "Unknown user")
    return CurrentUser(org_id=person.distributor_id, id=person.id)


@router.post("/runs", response_model=RunResponse, status_code=201)
def create_run(
    request: CreateRunRequest,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict:
    admit_create(user.org_id, user.id)
    run, created = run_service.create_run(
        db,
        org_id=user.org_id,
        user_id=user.id,
        run_type=request.run_type,
        idempotency_key=request.idempotency_key,
        run_input=request.input,
    )
    if not created:
        response.status_code = 200
    return run


@router.post("/runs/{run_id}/rerun", response_model=RunResponse, status_code=201)
def rerun_run(
    run_id: str,
    request: RerunRunRequest,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict:
    admit_create(user.org_id, user.id)
    run, created = run_service.rerun(
        db,
        org_id=user.org_id,
        user_id=user.id,
        run_id=run_id,
        idempotency_key=request.idempotency_key,
        run_input=request.input,
    )
    if not created:
        response.status_code = 200
    return run


@router.get("/runs", response_model=list[RunResponse])
def list_runs(
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
    status: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> list:
    """Runs for the caller's distributor. A status or user id only narrows that org."""
    admit_read(user.org_id, user.id)
    if status is not None:
        try:
            RunStatus(status)
        except ValueError:
            raise AppError(422, "VALIDATION_ERROR", "Unknown run status") from None
    return run_service.list_runs(db, user.org_id, user_id=user_id, status=status, limit=limit)


@router.get("/metrics", response_class=PlainTextResponse)
def get_metrics(
    user: CurrentUser = Depends(get_current_user), db: Database = Depends(get_db),
) -> PlainTextResponse:
    admit_read(user.org_id, user.id)
    return PlainTextResponse(
        monitor.metrics_text(db, user.org_id),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/monitor", response_model=MonitorResponse)
def get_monitor(
    user: CurrentUser = Depends(get_current_user), db: Database = Depends(get_db),
) -> dict:
    admit_read(user.org_id, user.id)
    return monitor.monitor(db, user.org_id)


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, user: CurrentUser = Depends(get_current_user), db: Database = Depends(get_db)) -> dict:
    admit_read(user.org_id, user.id)
    return run_service.get_run(db, user.org_id, run_id)


@router.get("/runs/{run_id}/events", response_model=list[RunEventResponse])
def get_run_events(
    run_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
    after: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> list:
    admit_read(user.org_id, user.id)
    cursor = run_service.event_cursor(after, last_event_id)
    return run_service.list_events(db, user.org_id, run_id, cursor)


@router.get("/runs/{run_id}/events/stream")
def stream_run_events(
    run_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
    after: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    admit_read(user.org_id, user.id)
    cursor = run_service.event_cursor(after, last_event_id)
    # 404 before the first byte. The generator reads the same query again.
    run_service.list_events(db, user.org_id, run_id, cursor)

    def frames() -> Iterator[bytes]:
        yield from iter_event_stream(db, user.org_id, run_id, cursor)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
    request: CancelRunRequest | None = Body(default=None),
) -> dict:
    admit_create(user.org_id, user.id)
    reason = None if request is None else request.reason
    return run_service.cancel_run(db, user.org_id, run_id, reason)


@router.post("/runs/{run_id}/resume", response_model=RunResponse)
def resume_run(
    run_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
    request: ResumeRunRequest | None = Body(default=None),
) -> dict:
    admit_create(user.org_id, user.id)
    run_input = None if request is None else request.input
    return run_service.resume_run(db, user.org_id, run_id, run_input)


@router.get("/runs/{run_id}/trace", response_model=TraceResponse)
def get_run_trace(
    run_id: str, user: CurrentUser = Depends(get_current_user), db: Database = Depends(get_db),
) -> dict:
    admit_read(user.org_id, user.id)
    return run_service.get_trace(db, user.org_id, run_id)


@router.post("/runs/{run_id}/repair", response_model=RunResponse)
def repair_run(
    run_id: str,
    request: RepairRunRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict:
    """Record a hand edit. The run status stays where it is."""
    admit_create(user.org_id, user.id)
    return run_service.repair_run(
        db, user.org_id, user.id, run_id,
        reason=request.reason, before=request.before, after=request.after,
    )
