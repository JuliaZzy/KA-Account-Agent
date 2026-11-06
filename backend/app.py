"""HTTP entry. Routers live in `router/`; this module only builds the app and hands it a
database and a model for this process (tests pass their own).

The exercise run routes are mounted here so the desk can read a trace and cancel a run.
This process still does not start the dispatcher or the worker.

    ./dev api                            # http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import Database
from .router import include_routers
from .routes.runs import router as runs_router
from .services.run_service import AppError


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid4().hex[:12]}"
    request.state.request_id = request_id
    return request_id

def create_app(db: Database | None = None, llm: Any | None = None) -> FastAPI:
    app = FastAPI(title="KA Account Agent")
    app.state.db = db
    app.state.llm = llm
    include_routers(app)
    app.include_router(runs_router, prefix="/api")

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": _request_id(request),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "request_id": _request_id(request),
                }
            },
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="0.0.0.0", port=8000, reload=False)
