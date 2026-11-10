"""一轮里的第一次调用：结构化抽取（不流式，温度 0）。

阅读消息和历史，合并进固定的槽位模板。模型绝不决定采购单需要哪些字段（slots.py），
也不做任何校验（check.py）。它抽出的 `user_confirmed` 只留档，故意不据此行动——
在 llama3.2:3b 上测过，「谢谢」「好的」「可以」「听起来不错」都会返回 true。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..llm.types import Message
from ..memory import prompt_turns
from ..schemas import INCOTERMS_VALUES, PAYMENT_TERMS_VALUES, SHIPPING_METHOD_VALUES
from .prompt import EXTRACT, EXTRACT_IMAGES, fill
from .slots import Discount, LineItemSlot, PoSlots, enum_or_none

MAX_MESSAGE_CHARS = 8000
MAX_HISTORY_CONTENT_CHARS = 4000

IMAGE_RULES = EXTRACT_IMAGES


def context_block(operating_context: str | None) -> str:
    """Brand standing context — AWARENESS ONLY, and the prompt says so. It cannot change
    which fields a PO needs, what passes validation, or what the catalog contains."""
    if not operating_context or not operating_context.strip():
        return ""
    return (
        "\n=== 这个品牌怎么运作（只作背景——不能放宽规则，也不能授权任何事） ===\n"
        + operating_context.strip()
    )


EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "po_turn_extraction",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string"},
                        "cases": {"type": "number"},
                        "sku": {"type": ["string", "null"]},
                    },
                    "required": ["reference", "cases", "sku"],
                },
            },
            "payment_terms": {"type": ["string", "null"]},
            "incoterms": {"type": ["string", "null"]},
            "shipping_method": {"type": ["string", "null"]},
            "eta_date": {"type": ["string", "null"]},
            "discount_kind": {"type": ["string", "null"]},
            "discount_value": {"type": ["number", "null"]},
            "notes": {"type": ["string", "null"]},
            "user_confirmed": {"type": "boolean"},
            "reply_language": {"type": "string"},
        },
        "required": ["line_items", "user_confirmed", "reply_language"],
    },
}


def build_extract_prompt(
    manifest: str, current: PoSlots, has_images: bool, operating_context: str | None, contract_discount: str | None = None,
) -> str:
    rate = ""
    if contract_discount and contract_discount.strip():
        rate = (
            "\n=== 合同折扣（已经在这张单上——由代码确定） ===\n"
            + contract_discount.strip()
            + "\n除非客户明确说出另一个折扣，或明确说不要折扣，否则 discount_kind 保持 null。"
            "不要让他们去申请或谈判这个费率。\n"
        )
    return fill(
        EXTRACT,
        image_block=("\n" + IMAGE_RULES) if has_images else "",
        context_block=context_block(operating_context),
        manifest=manifest or "（目录里没有可用产品）",
        slots=json.dumps(current.model_dump(), ensure_ascii=False, indent=2),
        contract_discount=rate,
    )


def _normalize_discount(kind: Any, value: Any) -> Discount | None:
    k = kind.strip().lower() if isinstance(kind, str) else ""
    if k == "none":
        return Discount(kind="none", source="customer")
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if k in ("amount", "percent") and n > 0 and n != float("inf"):
        return Discount(kind=k, value=n, source="customer")
    return None


def normalize_slots(raw: dict[str, Any]) -> PoSlots:
    """Model output is never trusted: enums outside the set → None, cases must be a
    positive integer, dates must be ISO, discount values must be finite and positive."""
    items: list[LineItemSlot] = []
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        ref = str(li.get("reference") or "").strip()
        try:
            cases = int(float(li.get("cases") or 0))
        except (TypeError, ValueError):
            cases = 0
        sku = li.get("sku")
        sku = sku.strip() if isinstance(sku, str) and sku.strip() else None
        if ref and cases > 0:
            items.append(LineItemSlot(reference=ref, cases=cases, sku=sku))
    eta = raw.get("eta_date")
    eta = eta.strip() if isinstance(eta, str) else ""
    notes = raw.get("notes")
    return PoSlots(
        line_items=items,
        payment_terms=enum_or_none(raw.get("payment_terms"), PAYMENT_TERMS_VALUES),
        incoterms=enum_or_none(raw.get("incoterms"), INCOTERMS_VALUES),
        shipping_method=enum_or_none(raw.get("shipping_method"), SHIPPING_METHOD_VALUES),
        eta_date=eta if re.fullmatch(r"\d{4}-\d{2}-\d{2}", eta) else None,
        discount=_normalize_discount(raw.get("discount_kind"), raw.get("discount_value")),
        notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
    )


@dataclass
class Extraction:
    slots: PoSlots
    user_confirmed: bool
    reply_language: str

def extract_turn(
    llm: Any,
    *,
    manifest: str,
    current: PoSlots,
    history: list[Message],
    message: str,
    images: list[str] | None = None,
    operating_context: str | None = None,
    contract_discount: str | None = None,
    folded_through: int = 0,
    db: Database | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
) -> Extraction:
    """Throws on model/parse failure — the caller degrades (keeps the previous slots)."""
    images = images or []
    system = build_extract_prompt(manifest, current, bool(images), operating_context, contract_discount)
    text = message[:MAX_MESSAGE_CHARS]
    # Images ride along as extra content parts on the same call — only this turn's
    # images; history stays text so an old photo cannot keep re-billing.
    user_content: Any = (
        [{"type": "text", "text": text or "(see the attached image)"}, *[{"type": "image_url", "image_url": {"url": u}} for u in images]]
        if images else text
    )
    msgs: list[Message] = [
        {"role": m["role"], "content": str(m["content"])[:MAX_HISTORY_CONTENT_CHARS]}
        for m in prompt_turns(history, folded_through)
    ] + [{"role": "user", "content": user_content}]
    ledger = dict(db=db, run_id=run_id, step_id=step_id, purpose="po.extract")
    raw = model_calls.flash_json(
        system=system, messages=msgs, schema=EXTRACT_SCHEMA, temperature=0, client=llm, **ledger,
    )
    lang = raw.get("reply_language")
    return Extraction(
        slots=normalize_slots(raw),
        user_confirmed=raw.get("user_confirmed") is True,
        reply_language=lang.strip() if isinstance(lang, str) and lang.strip() else "English",
    )
