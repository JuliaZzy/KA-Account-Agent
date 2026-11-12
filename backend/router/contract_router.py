"""List, read, and import contracts for a partnership."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ..contract import contract_discount_for_next_order, ingest_extraction, read_and_ingest
from ..contract.chunks import nest_chunk_rows
from ..db import Database
from ..deps import get_db
from ..llm import deepseek_client
from ..schemas import Partnership
from ._common import dump, partnership_or_404, read_upload, user_on_partnership

router = APIRouter()


@router.get("/api/partnerships/{pid}/contracts")
def contracts(pid: str, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    partnership_or_404(db, pid)
    return [dump(c) for c in db.contracts(pid)]


@router.get("/api/contracts/{cid}")
def contract_detail(cid: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    found = db.contract(cid)
    if not found:
        raise HTTPException(404, "contract not found")
    discount = None
    if found.status == "active":
        nxt = contract_discount_for_next_order(db, found.partnership_id)
        if nxt:
            discount = {
                "percent": nxt.percent, "note": nxt.note, "container_index": nxt.container_index,
                "rule_title": nxt.rule_title,
            }
    flat = [c.model_dump() for c in db.contract_chunks(cid)]
    return {
        "contract": dump(found),
        "terms": dump(db.contract_terms(cid)),
        "rules": [dump(r) for r in db.derived_rules(cid)],
        "extractions": db.contract_extractions(cid),
        "outline": nest_chunk_rows(flat) if flat else [],
        "next_discount": discount,
    }


@router.post("/api/partnerships/{pid}/contracts")
async def import_contract(
    pid: str,
    file: UploadFile = File(...),
    title: str = Form(""),
    user_id: str = Form(""),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    p: Partnership = partnership_or_404(db, pid)
    user_on_partnership(db, p, user_id or None)
    name, data = await read_upload(file)
    label = title.strip() or name
    try:
        if name.lower().endswith(".json"):
            parsed = json.loads(data.decode("utf-8"))
            extraction = parsed.get("extraction", parsed) if isinstance(parsed, dict) else None
            if not isinstance(extraction, dict):
                raise ValueError("the file is not a JSON object")
            rec = ingest_extraction(
                db, extraction, brand_id=p.brand_id, partnership_id=p.id, title=label, file_name=name,
                model=parsed.get("model") if isinstance(parsed, dict) else None,
            )
        else:
            rec = read_and_ingest(
                db, deepseek_client(),
                brand_id=p.brand_id, partnership_id=p.id, file_name=name, data=data, title=label,
                user_id=user_id or None,
            )
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(400, f"Could not import: {err}") from err
    return {
        "id": rec.contract.id,
        "run_id": rec.ingestion_run_id,
        "title": rec.contract.title or rec.contract.id,
        "discount_rules": len(rec.terms.discount_rules),
        "po_rules": len(rec.derived_rules),
        "skipped": rec.skipped,
    }
