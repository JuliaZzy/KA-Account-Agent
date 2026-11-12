"""What an agreement read produces: the contract, its normalized terms, and one ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .catalog import CommercialRule


class ContractDiscountTier(BaseModel):
    model_config = ConfigDict(extra="ignore")

    position: int = 0
    from_container: int | None = None  # 1-based; None = from the first
    to_container: int | None = None  # inclusive; None = "and all further"
    discount_percent: float | None = None  # 0 = list price for this band
    notes: str | None = None

    def covers(self, n: int) -> bool:
        lo = self.from_container or 1
        return n >= lo and (self.to_container is None or n <= self.to_container)


class ContractDiscountRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    position: int = 0
    title: str | None = None
    section_reference: str | None = None
    applies_per: str | None = None  # each_contract_year | calendar_year | per_order | …
    basis: str | None = None  # shipping_container_sequence | purchase_volume | …
    source_clause_text: str | None = None
    tiers: list[ContractDiscountTier] = Field(default_factory=list)

    @property
    def max_percent(self) -> float | None:
        pcts = [t.discount_percent for t in self.tiers if t.discount_percent is not None and t.discount_percent > 0]
        return max(pcts) if pcts else None


class ContractMoq(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quantity: float | None = None
    unit: str | None = None
    applies_per: str | None = None
    product_scope: str | None = None
    sku: str | None = None
    currency: str | None = None
    source_clause_text: str | None = None


class ContractPriceEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_name: str | None = None
    product_sku: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    unit: str | None = None
    list_price_reference: str | None = None
    product_specific_discount_text: str | None = None
    source_clause_text: str | None = None


class ContractTerritory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    country_code: str
    kind: Literal["allowed", "excluded"]


class ContractEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    quote: str
    page: int | None = None
    section_hint: str | None = None
    char_start: int | None = None


class ContractChunk(BaseModel):
    """One node from `split_chunks`, stored for reference and UI outline."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    parent_chunk_id: str | None = None
    number: str = ""
    title: str = ""
    body: str = ""
    path: str = ""
    page_start: int
    page_end: int
    sibling_order: int = 0


class ContractTerms(BaseModel):
    discount_rules: list[ContractDiscountRule] = Field(default_factory=list)
    moqs: list[ContractMoq] = Field(default_factory=list)
    price_list: list[ContractPriceEntry] = Field(default_factory=list)
    territories: list[ContractTerritory] = Field(default_factory=list)
    evidence: list[ContractEvidence] = Field(default_factory=list)


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    brand_id: str
    partnership_id: str
    title: str | None = None
    file_name: str | None = None
    file_sha256: str | None = None
    contract_type: str | None = None
    status: Literal["active", "superseded"] = "active"
    effective_from: str | None = None  # YYYY-MM-DD
    term_start_date: str | None = None
    term_end_date: str | None = None
    auto_renewal: bool | None = None
    exclusivity_type: str | None = None
    territory_text: str | None = None
    annual_sales_target: dict[str, Any] | None = None
    annual_sales_target_currency: str | None = None
    created_at: datetime | None = None


class ContractRecord(BaseModel):
    """Contract, extraction and derived terms, plus the optional import run reference."""

    contract: Contract
    ingestion_run_id: str | None = None
    extraction_raw: dict[str, Any]
    model: str | None = None
    confidence_score: float | None = None
    extraction_notes: str | None = None
    skipped: list[str] = Field(default_factory=list)
    terms: ContractTerms = Field(default_factory=ContractTerms)
    derived_rules: list[CommercialRule] = Field(default_factory=list)
    product_moq_units: dict[str, int] = Field(default_factory=dict)  # sku → units
    chunks: list[ContractChunk] = Field(default_factory=list)
