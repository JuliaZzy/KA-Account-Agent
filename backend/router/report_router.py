"""Preview, save, confirm, and delete sales reports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from ..db import Database
from ..deps import get_db
from ..report import spreadsheet as reports
from ..schemas import Partnership
from ._common import dump, partnership_for_user, read_upload

router = APIRouter()

_MATRIX = {"units", "revenue", "stock"}


def _matrix(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value or value.startswith("("):
        return None
    if value not in _MATRIX:
        raise HTTPException(400, "matrix must be units, revenue or stock")
    return value


def _analyze(db: Database, p: Partnership, name: str, data: bytes, period: str, matrix: str | None) -> reports.Analysis:
    try:
        return reports.analyze(
            name, data, reporting_period=period.strip() or None, currency=p.currency,
            products=db.products(p.brand_id), matrix_metric=_matrix(matrix),
        )
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(400, f"Could not read the file: {err}") from err


def _analysis_body(analysis: reports.Analysis) -> dict[str, Any]:
    return {
        "file_name": analysis.file_name,
        "fact_count": len(analysis.facts),
        "facts": [dump(f) for f in analysis.facts[:60]],
        "skipped": analysis.skipped,
        "mappings": [
            {"sheet": m.sheet, "header_row": m.header_row, "currency": m.currency, "columns": m.describe()}
            for m in analysis.mappings
        ],
    }


def _report_for_user(db: Database, rid: str, user_id: str) -> Any:
    found = db.report(rid)
    if not found:
        raise HTTPException(404, "report not found")
    partnership_for_user(db, found.partnership_id, user_id)
    return found


@router.get("/api/partnerships/{pid}/reports")
def list_reports(pid: str, user_id: str, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    partnership_for_user(db, pid, user_id)
    return [dump(r) for r in db.reports(pid)]


@router.get("/api/reports/{rid}")
def report_detail(rid: str, user_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    found = _report_for_user(db, rid, user_id)
    return {
        "report": dump(found),
        "facts": [dump(f) for f in db.report_facts(rid)],
        "extractions": db.report_extractions(rid),
    }


@router.post("/api/partnerships/{pid}/reports/preview")
async def preview_report(
    pid: str, user_id: str = Form(""), file: UploadFile = File(...), period: str = Form(""), matrix: str = Form(""),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    p = partnership_for_user(db, pid, user_id)
    name, data = await read_upload(file)
    return _analysis_body(_analyze(db, p, name, data, period, matrix))


@router.post("/api/partnerships/{pid}/reports")
async def save_report(
    pid: str, user_id: str = Form(""), file: UploadFile = File(...), period: str = Form(""), title: str = Form(""),
    matrix: str = Form(""), db: Database = Depends(get_db),
) -> dict[str, Any]:
    p = partnership_for_user(db, pid, user_id)
    name, data = await read_upload(file)
    analysis = _analyze(db, p, name, data, period, matrix)
    if not analysis.facts:
        raise HTTPException(400, "No facts could be read from this file.")
    saved = reports.ingest(db, p, analysis, title=title.strip() or None)
    return dump(saved)


@router.post("/api/reports/{rid}/confirm")
def confirm_report(rid: str, user_id: str, db: Database = Depends(get_db)) -> dict[str, int]:
    _report_for_user(db, rid, user_id)
    try:
        return db.confirm_report(rid)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err


@router.delete("/api/reports/{rid}", status_code=204)
def delete_report(rid: str, user_id: str, db: Database = Depends(get_db)) -> Response:
    _report_for_user(db, rid, user_id)
    db.delete_report(rid)
    return Response(status_code=204)


@router.get("/api/partnerships/{pid}/narratives")
def narratives(pid: str, user_id: str, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    partnership_for_user(db, pid, user_id)
    return [dump(row) for row in db.narrative_reports(pid)]


@router.get("/api/partnerships/{pid}/facts")
def confirmed_facts(pid: str, user_id: str, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    partnership_for_user(db, pid, user_id)
    return [dump(f) for f in db.confirmed_facts(pid)]
