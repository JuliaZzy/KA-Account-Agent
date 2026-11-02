"""After submit: the distributor says the goods arrived, that they paid, or that
something is wrong. The agent presses the same buttons they would in the app —
Mark Received / Mark Payment Made / open a damage claim. It does not verify the money.

Three small classifiers (temperature 0, MIN_CONFIDENCE 0.7) and the pure logic that
turns a verdict into a status change. The pure parts have no I/O so they test cold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..schemas import PurchaseOrder
from .prompt import CLAIM, PAYMENT, RECEIPT

MIN_CONFIDENCE = 0.8

# --- pure logic --------------------------------------------------------------

def terms_key(raw: str | None) -> str:
    t = (raw or "").strip()
    return {"net30": "net_30", "net60": "net_60"}.get(t, t)


def prepayment_due_now(terms: str, total: float) -> float:
    t = 0.0 if math.isnan(total) else total
    if terms == "50_50":
        return round(t * 0.5, 2)
    if terms == "100_prepaid":
        return round(t, 2)
    return 0.0


@dataclass
class InspectionOutcome:
    status: str  # completed | awaiting_payment | awaiting_refund
    remaining: float
    payment_stage: str | None
    payment_due_days: int | None
    refund_amount: float | None


def inspection_ok_outcome(
    *, 
    payment_terms: str | None, 
    total: float, 
    prepaid: float, 
    balance_paid: float, 
    claim_credit: float = 0.0, 
    deductions: float = 0.0
) -> InspectionOutcome:
    """All OK, then route by what is still owed."""
    terms = terms_key(payment_terms)
    remaining = round(total - prepaid - balance_paid - claim_credit - deductions, 2)
    if remaining < 0:
        return InspectionOutcome("awaiting_refund", remaining, None, None, round(-remaining, 2))
    if remaining == 0:
        return InspectionOutcome("completed", 0.0, None, None, None)
    due_days = 30 if terms == "net_30" else 60 if terms == "net_60" else 0
    return InspectionOutcome("awaiting_payment", remaining, "final_payment", due_days, None)


def pick_claim_target(pos: list[PurchaseOrder], session_po_id: str | None) -> PurchaseOrder | None:
    """The order this message is about: the session's own PO, else the only candidate.
    Several candidates and no session → None. Never guess which order they paid."""
    if session_po_id:
        hit = next((p for p in pos if p.id == session_po_id), None)
        if hit:
            return hit
    return pos[0] if len({p.id for p in pos}) == 1 else None


def match_claim_lines(items: list[dict[str, Any]], raw_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map the model's {reference, qty, issue_type, description} to PO lines in code:
    exact SKU (case-insensitive) → name contains reference → only one line on the PO.
    Several lines and no match → dropped (do not guess which line)."""
    out = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        ref = str(raw.get("reference") or "").strip().lower()
        try:
            qty = round(float(raw.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        line = next((i for i in items if i["sku"].lower() == ref), None) or \
            next((i for i in items if ref and ref in i["product_name"].lower()), None) or \
            (items[0] if len(items) == 1 else None)
        if not line:
            continue
        out.append({
            "sku": line["sku"], "product_name": line["product_name"], "qty": qty,
            "issue_type": str(raw.get("issue_type") or "damaged"), "description": str(raw.get("description") or "").strip(),
            "unit_price": line["case_price"], "amount": round(line["case_price"] * qty, 2),
        })
    return out


# --- classifiers ---------------------------------------------------------------

RECEIPT_PROMPT = RECEIPT
PAYMENT_PROMPT = PAYMENT
CLAIM_PROMPT = CLAIM


def _conf(raw: dict[str, Any]) -> float:
    try:
        c = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        return 0.0
    return c if 0 <= c <= 1 else 0.0

def _school_llm(
    llm: Any,
    system: str,
    message: str,
    *,
    db: Database | None,
    run_id: str | None,
    step_id: str | None,
) -> dict[str, Any]:
    msg = [{"role": "user", "content": message[:4000]}]
    return model_calls.school_json(
        model_calls.SCHOOL_CHAT, system=system, messages=msg, temperature=0, client=llm,
        db=db, run_id=run_id, step_id=step_id, purpose="po.claim",
    )

def classify_receipt(llm: Any, message: str, *, 
    db: Database | None = None, run_id: str | None = None, step_id: str | None = None
) -> tuple[str, float]:
    raw = _school_llm(llm, RECEIPT_PROMPT, message, db=db, run_id=run_id, step_id=step_id)
    v = raw.get("verdict")
    return (v if v in ("none", "received_ok", "received_ok_paid", "issues") else "none"), _conf(raw)


def classify_payment(llm: Any, message: str, *,
    db: Database | None = None, run_id: str | None = None, step_id: str | None = None
) -> tuple[bool, float]:
    raw = _school_llm(llm, PAYMENT_PROMPT, message, db=db, run_id=run_id, step_id=step_id)
    return raw.get("paid") is True, _conf(raw)


def extract_claim(llm: Any, message: str, *,
    db: Database | None = None, run_id: str | None = None, step_id: str | None = None
) -> dict[str, Any]:
    raw = _school_llm(llm, CLAIM_PROMPT, message, db=db, run_id=run_id, step_id=step_id)
    return {"lines": raw.get("lines") or [], "resolution": raw.get("resolution"), "confidence": _conf(raw)}


# --- apply ---------------------------------------------------------------------

@dataclass
class ClaimOutcome:
    kind: str  # none | received | payment | claim
    message: str
    po_number: str | None = None
    data: dict[str, Any] | None = None

def handle_post_submit(
    db: Database, llm: Any, po: PurchaseOrder, message: str,
    run_id: str | None = None, step_id: str | None = None,
) -> ClaimOutcome:
    """Run the classifiers that make sense for the order's state and apply the first
    confident verdict. Anything below MIN_CONFIDENCE is left for a human."""
    recorded = {"db": db, "run_id": run_id, "step_id": step_id}
    verdict, conf = classify_receipt(llm, message, **recorded)
    if verdict == "issues" and conf >= MIN_CONFIDENCE:
        claim = extract_claim(llm, message, **recorded)
        lines = match_claim_lines(db.po_items(po.id), claim["lines"])
        if lines and claim["confidence"] >= MIN_CONFIDENCE:
            db.update_po(po.id, status="claim_filed")
            db.add_po_event(po.id, "claim_filed", {"lines": lines, "resolution": claim["resolution"]})
            total = round(sum(l["amount"] for l in lines), 2)
            return ClaimOutcome("claim", f"Claim opened on {po.po_number}: {len(lines)} line(s), {po.currency} {total:.2f}. The brand will review it.", po.po_number, {"lines": lines})
        return ClaimOutcome("none", "I can see there's a problem with the shipment, but I couldn't tell which products or how many — could you list them?", po.po_number)
    if verdict in ("received_ok", "received_ok_paid") and conf >= MIN_CONFIDENCE:
        balance_paid = po.balance_paid
        if verdict == "received_ok_paid":
            balance_paid = round(po.total_amount - po.prepaid_amount, 2)
        outcome = inspection_ok_outcome(payment_terms=po.payment_terms, total=po.total_amount, prepaid=po.prepaid_amount, balance_paid=balance_paid)
        db.update_po(po.id, status=outcome.status, balance_paid=balance_paid)
        db.add_po_event(po.id, "received", {"verdict": verdict, "status": outcome.status, "remaining": outcome.remaining})
        if outcome.status == "completed":
            return ClaimOutcome("received", f"Marked {po.po_number} as received and all OK — nothing left to pay, the order is complete.", po.po_number)
        if outcome.status == "awaiting_refund":
            return ClaimOutcome("received", f"Marked {po.po_number} as received. You're owed a refund of {po.currency} {outcome.refund_amount:.2f}; the brand will process it.", po.po_number)
        due = f" within {outcome.payment_due_days} days" if outcome.payment_due_days else ""
        return ClaimOutcome("received", f"Marked {po.po_number} as received and all OK. {po.currency} {outcome.remaining:.2f} is still due{due}.", po.po_number)
    paid, pconf = classify_payment(llm, message, **recorded)
    if paid and pconf >= MIN_CONFIDENCE:
        due = prepayment_due_now(terms_key(po.payment_terms), po.total_amount) if po.status == "submitted" else round(po.total_amount - po.prepaid_amount, 2)
        db.update_po(po.id, status="payment_claimed")
        db.add_po_event(po.id, "payment_claimed", {"amount": due})
        return ClaimOutcome("payment", f"Noted — you've told me {po.currency} {due:.2f} was sent for {po.po_number}. The brand will confirm it on their side.", po.po_number)
    return ClaimOutcome("none", "", po.po_number)
