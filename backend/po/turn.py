"""采购单草稿，以及确认闸门。

合同折扣（代码）→ 抽取（模型）→ 合并 → 代码检查 → 草稿。`prepare_order` 不写会话，也不写回复。
编排器拿到草稿后再写会话。确认对着当前目录和规则重跑检查，草稿哈希必须等于签发令牌时的那个，
并且令牌在创建订单之前烧掉。
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contract import contract_discount_for_next_order
from ..db import Database
from ..llm.calls import JsonRepairExhausted
from ..llm.errors import ModelCapacityExceeded, ProviderError
from ..memory import prompt_context, recall, summary_block, with_memory
from ..orchestrator.status import status
from ..schemas import AgentSettings, CommercialRule, Partnership
from .catalog import Catalog
from .check import CheckResult, run_code_check, typed_in
from .draft import build_draft, build_draft_summary, hash_draft
from .extract import extract_turn
from .resolver import resolve_references
from .slots import Discount, PoSlots, merge_slots
from .submit import create_and_submit_po

# A chat confirm is pressed while the draft is on screen; a day bounds a tab left open.
CONFIRM_TOKEN_TTL = timedelta(hours=24)


@dataclass
class Bundle:
    partnership: Partnership
    settings: AgentSettings
    catalog: Catalog
    rules: list[CommercialRule]


def load_bundle(db: Database, partnership_id: str, user_id: str | None = None) -> Bundle:
    p = db.partnership(partnership_id)
    if not p:
        raise KeyError(f"partnership {partnership_id} not found")
    settings = db.settings(p.brand_id)
    # Long-term memory rides in the same context slot as the brand's standing context:
    # awareness for the model, never an input to the code-check. One person's notes only.
    memories = recall(db, partnership_id, user_id=user_id)
    if memories:
        settings = settings.model_copy(update={"operating_context": with_memory(settings.operating_context, memories)})
    return Bundle(partnership=p, settings=settings, catalog=Catalog(db.products(p.brand_id)), rules=db.rules(p.brand_id))


async def _check(
    bundle: Bundle, slots: PoSlots, llm: Any | None, *, resolve_names: bool = True, user_text: str = "",
) -> CheckResult:
    # Confirm passes resolve_names=False. The conversation already settled the slots.
    # Names are resolved before the price check, so the check itself stays synchronous.
    resolve = None
    if resolve_names:
        from .check import _resolve_lines

        named = _resolve_lines(slots, bundle.catalog, user_text)
        refs = list({line.reference for line in named if line.unresolved and line.reference})
        mapping = await resolve_references(llm, refs, bundle.catalog.resolver_rows()) if refs else {}

        def resolve(refs: list[str], found: dict[str, list[str]] = mapping) -> dict[str, list[str]]:
            del refs
            return found
    check = run_code_check(
        slots=slots, catalog=bundle.catalog, rules=bundle.rules,
        partnership_id=bundle.partnership.id, ship_to_country=bundle.partnership.ship_to_country, resolve=resolve,
        user_text=user_text,
    )
    # Persist resolved SKUs into the slots so a resolved line isn't re-searched next turn.
    for l, li in zip(check.lines, slots.line_items, strict=True):
        if l.resolved and l.sku:
            li.sku = l.sku
            if typed_in(l.sku, user_text):
                # Confirm re-checks without the chat. Keep the customer's own SKU as the reference
                # so the line resolves the same way then (an exact SKU always wins the search).
                li.reference = l.sku
    return check


@dataclass
class PoPrepared:
    """The draft and the confirm token. The caller writes the session once."""

    slots: PoSlots
    language: str
    stage: str
    check: CheckResult
    draft: dict[str, Any] | None
    token: str | None
    draft_hash: str | None
    expires: datetime | None
    applied: str | None
    context: str | None
    folded: int
    history: list[dict[str, str]]
    events: list[dict[str, Any]]


def _gap_detail(check: CheckResult) -> list[dict[str, Any]]:
    return [g.to_dict() for g in check.gaps] + [
        {"kind": "rule", "message": r.message, "reference": None, "candidates": []} for r in check.blocking
    ]


async def prepare_order(
    db: Database, llm: Any, bundle: Bundle, session: dict[str, Any], message: str, images: list[str] | None = None,
    run_id: str | None = None, step_id: str | None = None,
) -> PoPrepared:
    """Fill slots and price the draft. Does not speak and does not write the session."""
    images = images or []
    current = PoSlots(**session["slots"]) if session.get("slots") else PoSlots()
    history: list[dict[str, str]] = list(session.get("history") or [])
    events: list[dict[str, Any]] = []

    # The contract rate is known as soon as this is an order, before any product is named.
    # Pre-filled once (source="contract"); whatever the customer says afterwards overrides it.
    applied = None
    if current.discount is None:
        cd = contract_discount_for_next_order(db, bundle.partnership.id)
        if cd:
            current.discount = Discount(
                kind="percent" if cd.percent > 0 else "none", value=cd.percent if cd.percent > 0 else None,
                source="contract", note=cd.note,
            )
            applied = cd.note

    # Extract this turn into the slot template; merge, never replace.
    # The rolling summary is this chat's, so it joins the prompt here and not the saved settings.
    events.append(status("po-extract", "抽取", "正在把这句话填进订单", state="running"))
    folded = int(session.get("folded_through") or 0)
    context = prompt_context(bundle.settings.operating_context, summary_block(session.get("summary"))) or None
    try:
        ex = await extract_turn(
            llm, manifest=bundle.catalog.manifest(), current=current, history=history, message=message,
            images=images, operating_context=context, contract_discount=applied, folded_through=folded,
            db=db, run_id=run_id, step_id=step_id,
        )
        slots, language = merge_slots(current, ex.slots), ex.reply_language
        events.append(status("po-extract", "抽取", "订单字段已填上", state="done"))
    except (ModelCapacityExceeded, ProviderError, JsonRepairExhausted):
        raise
    except Exception as err:
        events.append({"type": "warning", "message": f"extraction failed: {err}"})
        events.append(status("po-extract", "抽取", "没读出来，沿用已有订单", state="done"))
        slots, language = current, "English"

    events.append(status("po-check", "核对", "正在算价格、起订量和规则", state="running"))
    said = "\n".join([message, *(str(m.get("content") or "") for m in history if m.get("role") == "user")])
    check = await _check(bundle, slots, llm, user_text=said)
    stage = "confirm" if check.ready else "gathering"
    events.append(status("po-check", "核对", "可以确认" if stage == "confirm" else "还有缺项", state="done"))
    draft = build_draft(slots, check, bundle.partnership.currency, bundle.settings)

    # Mint the confirm token only when the order is complete and compliant; bind it to
    # the draft the user is about to see.
    token = draft_hash = expires = None
    if stage == "confirm" and draft:
        token, draft_hash = secrets.token_urlsafe(32), hash_draft(draft)
        expires = datetime.now(UTC) + CONFIRM_TOKEN_TTL
    return PoPrepared(
        slots=slots, language=language, stage=stage, check=check, draft=draft,
        token=token, draft_hash=draft_hash, expires=expires, applied=applied,
        context=context, folded=folded, history=history, events=events,
    )


def order_result(prepared: PoPrepared, bundle: Bundle, *, clear_po: bool = False) -> dict[str, Any]:
    """JSON-safe draft the parallel turn stores, then writes once."""
    check = prepared.check
    expires = prepared.expires.isoformat() if prepared.expires else None
    return {
        "slots": prepared.slots.model_dump(),
        "stage": prepared.stage,
        "draft": prepared.draft,
        "summary": build_draft_summary(
            prepared.slots, check, bundle.settings, bundle.partnership.currency,
        ),
        "confirm_token": prepared.token,
        "confirm_hash": prepared.draft_hash,
        "confirm_expires_at": expires,
        "gaps": check.gap_messages,
        "gaps_detail": _gap_detail(check),
        "advisories": [rule.message for rule in check.advisory],
        "clear_po": clear_po,
    }


def confirm_order(db: Database, bundle: Bundle, session: dict[str, Any], token: str) -> dict[str, Any]:
    """The gate. Returns {state: submitted | done | used | expired | changed, ...}."""
    if session.get("stage") == "submitted":
        po = db.po(session["po_id"]) if session.get("po_id") else None
        return {"state": "done", "po_number": po.po_number if po else None}
    stored = session.get("confirm_token")
    if not stored or stored != token:
        return {"state": "used"}
    if not session.get("confirm_expires_at") or session["confirm_expires_at"] < datetime.now(UTC):
        return {"state": "expired"}

    # Rebuild from the live catalog + live rules. No LLM: the slots were settled by the
    # conversation, and re-extracting at click time would let the model re-decide an
    # order the user has already read.
    slots = PoSlots(**session["slots"])
    check = asyncio.run(_check(bundle, slots, None, resolve_names=False))
    draft = build_draft(slots, check, bundle.partnership.currency, bundle.settings)
    if not draft or not check.ready or hash_draft(draft) != session.get("confirm_hash"):
        db.update_session(session["id"], stage="gathering", confirm_token=None, confirm_hash=None, confirm_expires_at=None)
        return {"state": "changed", "draft": draft, "gaps": check.gap_messages}

    # Burn the token BEFORE creating the order. Two clicks a second apart: one UPDATE finds
    # the token set, the other finds nothing and returns "used" without reaching create.
    if db.burn_confirm_token(token) is None:
        return {"state": "used"}
    result = create_and_submit_po(db, partnership=bundle.partnership, settings=bundle.settings, slots=slots, check=check)
    db.update_session(session["id"], stage="submitted", po_id=result.po_id)
    return {
        "state": "submitted", "po_id": result.po_id, "po_number": result.po_number, "total": result.total,
        "currency": result.currency, "advisories": [r.message for r in result.advisory], "draft": draft,
    }
