"""从聊天里读一份协议，整理成待确认的表。

聊天模型不归档，也不编造条款。这个模块运行 `read`（它会切块），用 `record_from_extraction`
排出将要写入的行，但先不写库。用户在浮窗里改完，`confirm` 才调用 `ingest`。
`agent` 负责对经销商说话。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from ..db import Database
from ..documents import SUPPORTED_EXTS
from ..llm import campus_client, deepseek_client
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..schemas import ContractChunk
from .chunks import flatten_chunk_tree
from .ingest import record_from_extraction
from .read import read_agreement


def file_contract(
    db: Database, session: dict[str, Any], documents: list[tuple[str, bytes]], 
    *, run_id: str | None = None,
) -> dict[str, Any]:
    """Read the first agreement into a draft table. Nothing is written.

    `table` is what the panel shows. `pending` is what confirm needs and is not sent to
    the model. A filename without bytes never reaches here. The chat model does not
    read the file: the campus model picks sections, DeepSeek fills the form.
    """
    readable = [(name, data) for name, data in documents if name.lower().endswith(SUPPORTED_EXTS)]
    if not readable:
        return {"status": "need_file", "contract_id": None, "table": None, "pending": None, "error": None}
    name, data = readable[0]
    try:
        extraction, tree = read_agreement(
            deepseek_client(), campus_client(), name, data,
            db=db, run_id=run_id,
        )
        model = "deepseek-flash"
        digest = hashlib.sha256(data).hexdigest()
        chunks = [ContractChunk(**row) for row in flatten_chunk_tree(tree)]
        rec = record_from_extraction(
            extraction,
            brand_id=_brand_id(db, session["partnership_id"]),
            partnership_id=session["partnership_id"],
            title=name, file_name=name, file_sha256=digest, model=model, chunks=chunks,
        )
    except (ModelCapacityExceeded, ProviderError):
        raise
    except Exception as err:
        return {"status": "failed", "contract_id": None, "table": None, "pending": None, "error": str(err)}
    pending_id = f"cp_{uuid.uuid4().hex[:12]}"
    table = filing_table(rec)
    table.pop("contract_id", None)
    table["pending_id"] = pending_id
    table["confirmed"] = False
    unread = [other for other, _ in readable[1:]]
    if unread:
        table["unread_files"] = unread
    pending = {
        "id": pending_id,
        "extraction": extraction,
        "chunks": [chunk.model_dump() for chunk in rec.chunks],
        "file_name": name,
        "file_sha256": digest,
        "model": model,
        "title": name,
    }
    return {"status": "ready", "contract_id": None, "table": table, "pending": pending, "error": None}


def _brand_id(db: Database, partnership_id: str) -> str:
    partnership = db.partnership(partnership_id)
    if not partnership:
        raise KeyError("partnership not found")
    return partnership.brand_id


def filing_table(rec: Any) -> dict[str, Any]:
    """Rows the chat model may quote. Every figure comes from the stored record."""
    contract, terms = rec.contract, rec.terms
    return {
        "contract_id": contract.id,
        "title": contract.title,
        "file_name": contract.file_name,
        "contract_type": contract.contract_type,
        "term_start_date": contract.term_start_date,
        "term_end_date": contract.term_end_date,
        "auto_renewal": contract.auto_renewal,
        "exclusivity_type": contract.exclusivity_type,
        "territory_text": contract.territory_text,
        "discount_rules": [
            {
                "title": rule.title,
                "section_reference": rule.section_reference,
                "applies_per": rule.applies_per,
                "basis": rule.basis,
                "source_clause_text": rule.source_clause_text,
                "tiers": [
                    {
                        "from_container": tier.from_container,
                        "to_container": tier.to_container,
                        "discount_percent": tier.discount_percent,
                        "notes": tier.notes,
                    }
                    for tier in rule.tiers
                ],
            }
            for rule in terms.discount_rules
        ],
        "moqs": [row.model_dump(exclude_none=True) for row in terms.moqs],
        "territories": [row.model_dump() for row in terms.territories],
        "price_list": [row.model_dump(exclude_none=True) for row in terms.price_list],
        "derived_rules": [
            {
                "rule_type": rule.rule_type,
                "name": rule.name,
                "severity": rule.severity,
                "rule_config": rule.rule_config,
            }
            for rule in rec.derived_rules
        ],
        "skipped": list(rec.skipped),
    }
