"""Contracts, their terms, and the commercial rules derived from them."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ...schemas import (
    CommercialRule,
    Contract,
    ContractChunk,
    ContractDiscountRule,
    ContractDiscountTier,
    ContractEvidence,
    ContractMoq,
    ContractPriceEntry,
    ContractRecord,
    ContractTerms,
    ContractTerritory,
)
from ...services.run_names import RunStatus
from .. import orm
from .support import _row_dict, utcnow


class ContractMixin:
    def save_contract(self, rec: ContractRecord, *, inline_run_id: str | None = None) -> str:
        """One transaction: supersede the partnership's active contract, write the contract +
        raw extraction + normalized terms, then the derived rules and product MOQs. Returns
        the contract id."""
        c, ts = rec.contract, utcnow()
        with self.tx() as s:
            run = self._lock_run(s, inline_run_id) if inline_run_id else None
            if inline_run_id and (
                run is None or run.status != RunStatus.RUNNING or run.cancel_requested_at is not None
            ):
                raise ValueError("Contract import run is no longer active.")
            if run is not None:
                partnership = s.get(orm.Partnership, c.partnership_id)
                if partnership is None or partnership.distributor_id != run.org_id:
                    raise ValueError("Contract import run belongs to another distributor.")
            for old in s.scalars(select(orm.Contract).where(orm.Contract.partnership_id == c.partnership_id, orm.Contract.status == "active", orm.Contract.id != c.id)):
                old.status, old.updated_at = "superseded", ts
            self._upsert(s, orm.Contract, c.id, {**c.model_dump(exclude={"created_at"}), "status": "active"})
            s.flush()  # children below reference it; no relationship tells the unit of work the order
            s.add(orm.ContractExtraction(
                id=uuid.uuid4().hex, contract_id=c.id, model=rec.model, confidence_score=rec.confidence_score,
                extraction_notes=rec.extraction_notes, raw=rec.extraction_raw, skipped=list(rec.skipped), extracted_at=ts,
            ))
            # Terms are replaced wholesale by the newest extraction of this contract.
            for cls in (
                orm.ContractDiscountRule, orm.ContractMoq, orm.ContractPriceEntry, orm.ContractTerritory,
                orm.ContractEvidence, orm.ContractChunk,
            ):
                for row in s.scalars(select(cls).where(cls.contract_id == c.id)):
                    s.delete(row)
            s.flush()
            for ch in rec.chunks:
                s.add(orm.ContractChunk(contract_id=c.id, **ch.model_dump()))
            for i, dr in enumerate(rec.terms.discount_rules):
                s.add(orm.ContractDiscountRule(
                    contract_id=c.id, position=i, title=dr.title, section_reference=dr.section_reference, applies_per=dr.applies_per,
                    basis=dr.basis, source_clause_text=dr.source_clause_text,
                    tiers=[orm.ContractDiscountTier(position=j, **t.model_dump(exclude={"position"})) for j, t in enumerate(dr.tiers)],
                ))
            for m in rec.terms.moqs:
                s.add(orm.ContractMoq(contract_id=c.id, **m.model_dump()))
            for pe in rec.terms.price_list:
                s.add(orm.ContractPriceEntry(contract_id=c.id, **pe.model_dump()))
            for t in rec.terms.territories:
                s.add(orm.ContractTerritory(contract_id=c.id, **t.model_dump()))
            for e in rec.terms.evidence:
                s.add(orm.ContractEvidence(contract_id=c.id, **e.model_dump()))
            # Rules previously derived from this contract are replaced, not accumulated.
            for row in s.scalars(select(orm.CommercialRule).where(orm.CommercialRule.contract_id == c.id)):
                s.delete(row)
            s.flush()
            for r in rec.derived_rules:
                self._upsert(s, orm.CommercialRule, r.id, {**r.model_dump(), "brand_id": c.brand_id, "contract_id": c.id})
            if rec.product_moq_units:
                for p in s.scalars(select(orm.Product).where(orm.Product.brand_id == c.brand_id)):
                    if p.sku in rec.product_moq_units:
                        p.moq_units, p.updated_at = rec.product_moq_units[p.sku], ts
            if run is not None:
                self._transition(s, run, RunStatus.SUCCEEDED, {}, utcnow(), actor_type="api", actor_id=run.user_id)
        return c.id

    def contract(self, id: str) -> Contract | None:
        with self.tx() as s:
            row = s.get(orm.Contract, id)
            return Contract(**_row_dict(row)) if row else None

    def contracts(self, partnership_id: str) -> list[Contract]:
        """Every agreement on file for the partnership — the active one first, then superseded."""
        with self.tx() as s:
            rows = s.scalars(
                select(orm.Contract).where(orm.Contract.partnership_id == partnership_id)
                .order_by(orm.Contract.status, orm.Contract.created_at.desc())
            ).all()
            return [Contract(**_row_dict(r)) for r in rows]

    def derived_rules(self, contract_id: str) -> list[CommercialRule]:
        """The commercial rules this contract's terms were mapped into."""
        with self.tx() as s:
            rows = s.scalars(select(orm.CommercialRule).where(orm.CommercialRule.contract_id == contract_id).order_by(orm.CommercialRule.id)).all()
            return [CommercialRule(**_row_dict(r)) for r in rows]

    def active_contract(self, partnership_id: str) -> Contract | None:
        with self.tx() as s:
            row = s.scalars(
                select(orm.Contract).where(orm.Contract.partnership_id == partnership_id, orm.Contract.status == "active")
                .order_by(orm.Contract.created_at.desc())
            ).first()
            return Contract(**_row_dict(row)) if row else None

    def contract_discount_rules(self, contract_id: str) -> list[ContractDiscountRule]:
        with self.tx() as s:
            rows = s.scalars(select(orm.ContractDiscountRule).where(orm.ContractDiscountRule.contract_id == contract_id).order_by(orm.ContractDiscountRule.position)).all()
            return [
                ContractDiscountRule(**{**_row_dict(r), "tiers": [ContractDiscountTier(**_row_dict(t)) for t in r.tiers]})
                for r in rows
            ]

    def contract_terms(self, contract_id: str) -> ContractTerms:
        with self.tx() as s:
            def rows(cls: type) -> list[dict[str, Any]]:
                return [_row_dict(r) for r in s.scalars(select(cls).where(cls.contract_id == contract_id).order_by(cls.id))]
            return ContractTerms(
                discount_rules=self.contract_discount_rules(contract_id),
                moqs=[ContractMoq(**d) for d in rows(orm.ContractMoq)],
                price_list=[ContractPriceEntry(**d) for d in rows(orm.ContractPriceEntry)],
                territories=[ContractTerritory(**d) for d in rows(orm.ContractTerritory)],
                evidence=[ContractEvidence(**d) for d in rows(orm.ContractEvidence)],
            )

    def contract_chunks(self, contract_id: str) -> list[ContractChunk]:
        with self.tx() as s:
            rows = s.scalars(
                select(orm.ContractChunk)
                .where(orm.ContractChunk.contract_id == contract_id)
                .order_by(orm.ContractChunk.page_start, orm.ContractChunk.sibling_order, orm.ContractChunk.id),
            ).all()
            return [ContractChunk(**_row_dict(r)) for r in rows]

    def contract_extractions(self, contract_id: str) -> list[dict[str, Any]]:
        with self.tx() as s:
            rows = s.scalars(select(orm.ContractExtraction).where(orm.ContractExtraction.contract_id == contract_id).order_by(orm.ContractExtraction.extracted_at)).all()
            return [_row_dict(r) for r in rows]
