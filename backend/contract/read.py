"""把协议文件读成 `ingest.ingest_extraction` 已经会入库的抽取结果。

`split_chunks` 建一棵标题树（入库时写入 PostgreSQL）。模型先看整份大纲并点名要读的块，只有这些块的正文会送去填表。
仍为空的字段会对着还没读过的块再试，一共最多三遍。
某一遍把自己的完整度打到 0.9 或更高时，循环提前结束。
这里不推导采购单规则，那一步是 `ingest.derive_rules`。不在文档里的引用会被丢掉。
没有文字层的扫描页会连同图片一起送，和订单照片同一条路。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

from ..db import Database
from ..documents import SUPPORTED_EXTS, load_pages, render_pdf_images
from ..llm import DeepSeekClient, deepseek_client
from ..llm import calls as model_calls
from ..schemas import ContractChunk
from .chunks import flatten_chunk_tree, split_chunks
from .ingest import ingest_extraction, record_from_extraction
from .prompt import EXTRACT, REFLECT, SELECT

MAX_SCAN_IMAGES = 6
MAX_READ_PASSES = 3
# A pass at or above this score says the empty fields are absent, not hiding in unread chunks.
CONFIDENCE_STOP = 0.9
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

SCALAR_KEYS = (
    "contract_type", "effective_date", "term_start_date", "term_end_date", "auto_renewal",
    "exclusivity_type", "territory_text", "annual_sales_target", "annual_sales_target_currency",
    "confidence_score", "extraction_notes",
)
LIST_KEYS = (
    "minimum_order_quantities", "commercial_discount_rules", "product_price_list",
    "allowed_countries", "excluded_countries",
)

EXTRACT_SYSTEM = EXTRACT
SELECT_SYSTEM = SELECT
REFLECT_SYSTEM = REFLECT
# Form slots a later pass may still find in an unread chunk. Confidence and notes are about
# the pass itself, not a clause the model failed to open.
_GAP_FIELDS = (*(k for k in SCALAR_KEYS if k not in {"confidence_score", "extraction_notes"}), *LIST_KEYS)

SELECT_SCHEMA: dict[str, Any] = {
    "name": "contract_chunk_select",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {"selected_ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["selected_ids"],
    },
}

_TIER = {
    "type": "object",
    "properties": {
        "from_container": {"type": ["integer", "null"]},
        "to_container": {"type": ["integer", "null"]},
        "discount_percent": {"type": ["number", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["from_container", "to_container", "discount_percent", "notes"],
}
_RULE = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "section_reference": {"type": ["string", "null"]},
        "applies_per": {"type": ["string", "null"]},
        "basis": {"type": ["string", "null"]},
        "source_clause_text": {"type": ["string", "null"]},
        "tiers": {"type": "array", "items": _TIER},
    },
    "required": ["title", "section_reference", "applies_per", "basis", "source_clause_text", "tiers"],
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "contract_extraction",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "contract_type": {"type": ["string", "null"]},
            "effective_date": {"type": ["string", "null"]},
            "term_start_date": {"type": ["string", "null"]},
            "term_end_date": {"type": ["string", "null"]},
            "auto_renewal": {"type": ["boolean", "null"]},
            "exclusivity_type": {"type": ["string", "null"]},
            "territory_text": {"type": ["string", "null"]},
            "allowed_countries": {"type": "array", "items": {"type": "string"}},
            "excluded_countries": {"type": "array", "items": {"type": "string"}},
            "annual_sales_target": {"type": ["object", "null"]},
            "annual_sales_target_currency": {"type": ["string", "null"]},
            "minimum_order_quantities": {"type": "array", "items": {"type": "object"}},
            "commercial_discount_rules": {"type": "array", "items": _RULE},
            "product_price_list": {"type": "array", "items": {"type": "object"}},
            "field_evidence": {"type": "object"},
            "confidence_score": {"type": ["number", "null"]},
            "extraction_notes": {"type": ["string", "null"]},
        },
        "required": ["commercial_discount_rules", "minimum_order_quantities", "allowed_countries", "excluded_countries", "field_evidence"],
    },
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _date(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    m = _DATE.search(s)
    return m.group(0) if m else None


def _countries(v: Any) -> list[str]:
    out: list[str] = []
    for c in v if isinstance(v, list) else []:
        code = re.sub(r"[^A-Za-z]", "", str(c)).upper()
        if len(code) == 2 and code not in out:
            out.append(code)
    return out


def _quote_kept(quote: str, page: int | None, pages: list[tuple[int, str]], image_pages: set[int]) -> bool:
    """A quote has to occur in the text we actually sent. A scanned page has no text to
    check against, so a quote cited on that page is kept."""
    q = _norm(quote)
    if len(q) < 8:
        return False
    if page in image_pages:
        return True
    hay = _norm("\n".join(t for _, t in pages if t))
    return q in hay


def sanitize_extraction(raw: dict[str, Any], pages: list[tuple[int, str]], image_pages: set[int]) -> dict[str, Any]:
    """Model output is not trusted: dates must be ISO, country codes must be two letters,
    and an evidence quote that is not in the document is dropped."""
    out: dict[str, Any] = {}
    for k in ("contract_type", "exclusivity_type", "territory_text", "annual_sales_target_currency", "extraction_notes"):
        v = raw.get(k)
        out[k] = v.strip() if isinstance(v, str) and v.strip() else None
    for k in ("effective_date", "term_start_date", "term_end_date"):
        out[k] = _date(raw.get(k))
    out["auto_renewal"] = raw.get("auto_renewal") if isinstance(raw.get("auto_renewal"), bool) else None
    target = raw.get("annual_sales_target")
    out["annual_sales_target"] = target if isinstance(target, dict) else None
    try:
        score = float(raw.get("confidence_score"))
        out["confidence_score"] = score if 0 <= score <= 1 else None
    except (TypeError, ValueError):
        out["confidence_score"] = None
    out["allowed_countries"] = _countries(raw.get("allowed_countries"))
    out["excluded_countries"] = _countries(raw.get("excluded_countries"))
    for k in ("minimum_order_quantities", "commercial_discount_rules", "product_price_list"):
        out[k] = [x for x in (raw.get(k) or []) if isinstance(x, dict)]
    evidence: dict[str, list[dict[str, Any]]] = {}
    fe = raw.get("field_evidence") if isinstance(raw.get("field_evidence"), dict) else {}
    for field, entries in fe.items():
        kept = []
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            quote = str(e.get("quote") or "").strip()
            page = e.get("page")
            try:
                page_n = int(page) if page is not None else None
            except (TypeError, ValueError):
                page_n = None
            if quote and _quote_kept(quote, page_n, pages, image_pages):
                kept.append({"quote": quote, "page": page_n, "section_hint": e.get("section_hint")})
        if kept:
            evidence[str(field)] = kept
    out["field_evidence"] = evidence
    return out


def _has_terms(extraction: dict[str, Any]) -> bool:
    return any(extraction.get(k) for k in (
        "commercial_discount_rules", "minimum_order_quantities", "product_price_list",
        "allowed_countries", "excluded_countries", "term_start_date", "effective_date", "territory_text",
    ))


def _index_chunks(nodes: list[dict]) -> dict[str, dict]:
    """Every node in the tree, depth-first. Insertion order is the outline order."""
    index: dict[str, dict] = {}

    def walk(level: list[dict]) -> None:
        for node in level:
            index[node["id"]] = node
            walk(node.get("children") or [])

    walk(nodes)
    return index


def _outline_title(node: dict) -> str:
    """Heading words the model matches on. A short title is stored on the node; a heading
    that was a whole sentence lives as the first body line and would otherwise be invisible."""
    title = (node.get("title") or "").strip()
    if title:
        return title
    first = next((line.strip() for line in (node.get("body") or "").splitlines() if line.strip()), "")
    if len(first) > 160:
        return first[:160].rstrip() + "…"
    return first


def _outline(index: dict[str, dict]) -> list[dict[str, Any]]:
    """Number plus heading text. Later body lines stay out until the model names the id."""
    return [{
        "id": node["id"],
        "number": node.get("number") or "",
        "title": _outline_title(node),
        "path": node.get("path") or "",
        "has_children": bool(node.get("children")),
    } for node in index.values()]


def _latest_confidence(parts: list[dict[str, Any]]) -> float | None:
    """The newest pass's score. An earlier low score must not hide a later 'this is enough'."""
    for part in reversed(parts):
        score = part.get("confidence_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            return float(score)
    return None


def _missing_fields(extraction: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in _GAP_FIELDS:
        value = extraction.get(key)
        if value is None or value in ("", [], {}):
            missing.append(key)
    return missing


def _chosen_chunks(raw: dict[str, Any], allowed: dict[str, dict]) -> list[dict]:
    picked = raw.get("selected_ids") if isinstance(raw.get("selected_ids"), list) else []
    chosen: list[dict] = []
    seen: set[str] = set()
    for cid in picked:
        if not isinstance(cid, str) or cid in seen or cid not in allowed:
            continue
        seen.add(cid)
        node = allowed[cid]
        if (node.get("body") or "").strip():
            chosen.append(node)
    return chosen


def _school_json(
    llm: Any, *, system: str, user: str,
    db: Database | None = None, run_id: str | None = None, purpose: str = "contract.select",
) -> dict[str, Any]:
    """Cheap official Qwen JSON. The selection is the message text."""
    return model_calls.school_json(
        client=llm, system=system,
        messages=[{"role": "user", "content": user}], temperature=0,
        db=db, run_id=run_id, purpose=purpose,
    )


def _select_chunks(
    llm: Any, index: dict[str, dict],
    *, db: Database | None = None, run_id: str | None = None,
) -> list[dict]:
    """One selection over the full outline. A single chunk has nothing to choose."""
    readable = {cid: node for cid, node in index.items() if (node.get("body") or "").strip()}
    if len(index) <= 1:
        return list(readable.values())
    raw = _school_json(
        llm,
        system=SELECT_SYSTEM,
        user=json.dumps({"outline": _outline(index)}, ensure_ascii=False),
        db=db, run_id=run_id, purpose="contract.select",
    )
    return _chosen_chunks(raw, readable)


def _reflect_chunks(
    llm: Any, missing: list[str], unread: dict[str, dict],
    *, db: Database | None = None, run_id: str | None = None,
) -> list[dict]:
    """Second and third pass: only chunks not yet read, aimed at fields still empty."""
    raw = _school_json(
        llm,
        system=REFLECT_SYSTEM,
        user=json.dumps(
            {"missing_fields": missing, "unread_outline": _outline(unread)},
            ensure_ascii=False,
        ),
        db=db, run_id=run_id, purpose="contract.reflect",
    )
    return _chosen_chunks(raw, unread)


def _chunk_extract_content(file_name: str, chunk: dict, *, images: dict[int, bytes]) -> Any:
    text = (
        f"Source file: {file_name}\n"
        f"Section id: {chunk['id']}\n"
        f"Section path: {chunk.get('path') or 'preamble'}\n"
        f"Pages {chunk['page_start']}–{chunk['page_end']}.\n\n"
        f"{chunk.get('body') or ''}"
    )
    lo, hi = int(chunk["page_start"]), int(chunk["page_end"])
    batch_images = {n: jpeg for n, jpeg in images.items() if lo <= n <= hi}
    if not batch_images:
        return text
    import base64

    parts: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for n in sorted(batch_images):
        url = "data:image/jpeg;base64," + base64.b64encode(batch_images[n]).decode()
        parts.append({"type": "input_text", "text": f"Image of page {n}."})
        parts.append({"type": "input_image", "image_url": url})
    return [{"role": "user", "content": parts}]


def _chunk_models(tree: list[dict]) -> list[ContractChunk]:
    return [ContractChunk(**row) for row in flatten_chunk_tree(tree)]


def _merge(parts: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(parts[0])
    for p in parts[1:]:
        for k in LIST_KEYS:
            out[k] = [*(out.get(k) or []), *(p.get(k) or [])]
        fe = dict(out.get("field_evidence") or {})
        for k, v in (p.get("field_evidence") or {}).items():
            fe[k] = [*(fe.get(k) or []), *v]
        out["field_evidence"] = fe
        for k in SCALAR_KEYS:
            if out.get(k) in (None, "", []) and p.get(k) not in (None, "", []):
                out[k] = p[k]
    return out


def read_agreement(
    llm: DeepSeekClient | Any, file_name: str, data: bytes,
    *, db: Database | None = None, run_id: str | None = None,
) -> tuple[dict[str, Any], list[dict]]:
    """Sanitized extraction plus the heading tree. Raises ValueError when the file cannot be
    read or contains no enforceable terms — nothing is written in that case."""
    if not file_name.lower().endswith(SUPPORTED_EXTS):
        raise ValueError(f"Unsupported agreement: {file_name} (use {', '.join(SUPPORTED_EXTS)})")
    pages = load_pages(file_name, data, db=db, run_id=run_id)
    if not pages:
        raise ValueError(f"No pages in {file_name!r}.")
    blank = [n for n, t in pages if not (t or "").strip()]
    images = render_pdf_images(data, blank[:MAX_SCAN_IMAGES]) if file_name.lower().endswith(".pdf") and blank else {}
    image_pages = set(images)
    tree = split_chunks(pages)
    index = _index_chunks(tree)
    unread = {cid: node for cid, node in index.items() if (node.get("body") or "").strip()}
    parts: list[dict[str, Any]] = []

    def take(chunks: list[dict]) -> None:
        for chunk in chunks:
            unread.pop(chunk["id"], None)
            content = _chunk_extract_content(file_name, chunk, images=images)
            if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("role"):
                messages = content
            else:
                messages = [{"role": "user", "content": content}]
            raw = model_calls.flash_json(
                client=llm, system=EXTRACT_SYSTEM, messages=messages,
                schema=EXTRACT_SCHEMA, temperature=0,
                db=db, run_id=run_id, purpose="contract.extract",
            )
            if not isinstance(raw, dict):
                raise ValueError(f"Extraction for {chunk['id']} was not a JSON object.")
            parts.append(sanitize_extraction(raw, pages, image_pages))

    first = _select_chunks(llm, index, db=db, run_id=run_id)
    if not first:
        raise ValueError(f"No agreement sections selected for reading in {file_name!r}.")
    take(first)
    for _ in range(MAX_READ_PASSES - 1):
        extraction = _merge(parts) if len(parts) > 1 else parts[0]
        missing = _missing_fields(extraction)
        score = _latest_confidence(parts)
        if not missing or not unread or (score is not None and score >= CONFIDENCE_STOP):
            break
        more = _reflect_chunks(llm, missing, unread, db=db, run_id=run_id)
        if not more:
            break
        take(more)
    extraction = _merge(parts) if len(parts) > 1 else parts[0]
    if not _has_terms(extraction):
        raise ValueError(f"No enforceable terms found in {file_name!r}.")
    return extraction, tree


def read_and_ingest(
    db: Database,
    llm: DeepSeekClient | Any,
    *,
    brand_id: str,
    partnership_id: str,
    file_name: str,
    data: bytes,
    title: str | None = None,
) -> Any:
    """Read the file and write the contract, its terms and the derived PO rules."""
    extraction, tree = read_agreement(llm, file_name, data)
    model = "deepseek-flash"
    return ingest_extraction(
        db, extraction, brand_id=brand_id, partnership_id=partnership_id,
        title=title or file_name, file_name=file_name, file_sha256=hashlib.sha256(data).hexdigest(), model=model,
        chunks=_chunk_models(tree),
    )


def summarize(rec: Any) -> str:
    """What a person needs to hear after a filing. Numbers come from the stored record."""
    c, terms = rec.contract, rec.terms
    bits = [f"Filed {c.title or c.file_name} as the active agreement."]
    if c.term_start_date or c.term_end_date:
        bits.append(f"Term {c.term_start_date or '?'} to {c.term_end_date or '?'}.")
    if terms.discount_rules:
        bits.append(f"{len(terms.discount_rules)} discount schedule(s).")
    if terms.moqs:
        bits.append(f"{len(terms.moqs)} minimum-order clause(s).")
    allowed = [t.country_code for t in terms.territories if t.kind == "allowed"]
    excluded = [t.country_code for t in terms.territories if t.kind == "excluded"]
    if allowed:
        bits.append("Territories: " + ", ".join(allowed) + ".")
    if excluded:
        bits.append("Excluded: " + ", ".join(excluded) + ".")
    if rec.derived_rules:
        bits.append(f"{len(rec.derived_rules)} PO rule(s) now apply to orders.")
    if rec.skipped:
        bits.append("Left out, not clear enough to enforce: " + "; ".join(rec.skipped[:4]) + ".")
    bits.append("The previous active agreement for this partnership, if there was one, is superseded. The terms are on the Contracts page.")
    return " ".join(bits)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read an agreement file and store the terms it states.")
    ap.add_argument("file")
    ap.add_argument("--brand", required=True)
    ap.add_argument("--partnership", required=True)
    ap.add_argument("--title")
    ap.add_argument("--apply", action="store_true", help="write the contract and its derived PO rules")
    args = ap.parse_args(argv)
    from pathlib import Path

    data = Path(args.file).read_bytes()
    llm = deepseek_client()
    if args.apply:
        rec = read_and_ingest(
            Database(), llm, brand_id=args.brand, partnership_id=args.partnership,
            file_name=Path(args.file).name, data=data, title=args.title,
        )
    else:
        extraction, tree = read_agreement(llm, Path(args.file).name, data)
        rec = record_from_extraction(
            extraction, brand_id=args.brand, partnership_id=args.partnership, title=args.title,
            file_name=Path(args.file).name, chunks=_chunk_models(tree),
        )
    print(json.dumps({
        "contract_id": rec.contract.id, "written": args.apply, "summary": summarize(rec), "skipped": rec.skipped,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
