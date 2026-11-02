"""The relational schema — one SQLAlchemy class per table, split to match `backend/schemas/`.

Everything the agent reads or writes lives here as real columns with keys, so it can be
queried, joined and migrated. The only JSON columns are the ones whose shape is genuinely
polymorphic (a rule's `rule_config` depends on its `rule_type`; a contract extraction is
kept verbatim next to its normalized rows so it can be audited or re-mapped later).

Timestamps are `timestamptz` and every value written is UTC-aware (`database.utcnow`),
so comparisons, `BETWEEN` and `date_trunc` are the database's job rather than string
slicing. Plain dates — a contract term, a reporting period — stay `YYYY-MM-DD` text: they
are calendar labels with no instant behind them.

Naming: these are rows, referred to as `orm.Product` etc. The pydantic models the rest of
the code passes around are in `backend/schemas/`; `database/` converts between the two.
"""

from __future__ import annotations

from .base import Base, JSONCol, TimestampCol
from .brand import Brand, BrandAgentSettings
from .catalog import CommercialRule, Product
from .chat import ChatSession, Text2SQLLog
from .contract import (
    Contract,
    ContractChunk,
    ContractDiscountRule,
    ContractDiscountTier,
    ContractEvidence,
    ContractExtraction,
    ContractMoq,
    ContractPriceEntry,
    ContractTerritory,
)
from .document import BrandDocument, BrandDocumentPage
from .memory import Memory
from .order import PoEvent, PoRuleEvaluation, PurchaseOrder, PurchaseOrderItem
from .partnership import Distributor, Partnership
from .report import NarrativeReport, ReportExtraction, ReportFact, SalesReport
from .run import (
    AgentRun,
    AgentStep,
    AuditLog,
    Checkpoint,
    DeadLetterJob,
    ModelCall,
    OutboxJob,
    RunEvent,
    ToolCall,
    UsageRecord,
)
from .user import User

__all__ = [
    "AgentRun",
    "AgentStep",
    "AuditLog",
    "Base",
    "Brand",
    "BrandAgentSettings",
    "BrandDocument",
    "BrandDocumentPage",
    "ChatSession",
    "Checkpoint",
    "CommercialRule",
    "Contract",
    "ContractChunk",
    "ContractDiscountRule",
    "ContractDiscountTier",
    "ContractEvidence",
    "ContractExtraction",
    "ContractMoq",
    "ContractPriceEntry",
    "ContractTerritory",
    "DeadLetterJob",
    "Distributor",
    "JSONCol",
    "Memory",
    "ModelCall",
    "NarrativeReport",
    "OutboxJob",
    "Partnership",
    "PoEvent",
    "PoRuleEvaluation",
    "Product",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "ReportExtraction",
    "ReportFact",
    "RunEvent",
    "SalesReport",
    "Text2SQLLog",
    "TimestampCol",
    "ToolCall",
    "UsageRecord",
    "User",
]
