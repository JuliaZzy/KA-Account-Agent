"""The one object the rest of the code talks to for persistence.

It speaks pydantic models (`backend/schemas/`) and plain dicts outward and SQLAlchemy
rows (`orm/`) inward — nothing outside this package imports `orm`. Every method is one
transaction; the multi-row writes (an order with its items and rule snapshot, a contract
with its terms and derived rules) commit together or not at all.

Construct it with a PostgreSQL URL, or nothing to use `config.db_url()`.
The schema is migrated to head on construction.

The methods live on mixins in this package so one file does not hold every table.
`Database` is still the only type callers construct.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..engine import make_engine, run_migrations
from .brand import BrandMixin
from .contract import ContractMixin
from .document import DocumentMixin
from .memory import MemoryMixin
from .order import OrderMixin
from .queue import QueueMixin
from .records import RecordMixin
from .report import ReportMixin
from .run import RunMixin
from .session import SessionMixin
from .support import ChatTurnBusy, InvalidRunTransition, _as_url, utcnow
from .text2sql import Text2SqlMixin


class Database(
    BrandMixin,
    ContractMixin,
    SessionMixin,
    RunMixin,
    QueueMixin,
    RecordMixin,
    OrderMixin,
    DocumentMixin,
    ReportMixin,
    MemoryMixin,
    Text2SqlMixin,
):
    def __init__(self, target: str | None = None, engine: Engine | None = None):
        self.url = _as_url(target) if engine is None else str(engine.url)
        self.engine = engine or make_engine(self.url)
        run_migrations(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def tx(self) -> Session:
        """`with db.tx() as s:` — commits on exit, rolls back on error."""
        return self._sessions.begin()

    def _upsert(self, s: Session, cls: type, pk: Any, values: dict[str, Any]) -> None:
        ts = utcnow()
        row = s.get(cls, pk)
        cols = {c.key for c in cls.__table__.columns}
        values = {k: v for k, v in values.items() if k in cols}
        if row is None:
            if "created_at" in cols:
                values.setdefault("created_at", ts)
            if "updated_at" in cols:
                values["updated_at"] = ts
            s.add(cls(**values))
        else:
            values.pop("created_at", None)
            if "updated_at" in cols:
                values["updated_at"] = ts
            for k, v in values.items():
                setattr(row, k, v)


__all__ = ["ChatTurnBusy", "Database", "InvalidRunTransition", "utcnow"]
