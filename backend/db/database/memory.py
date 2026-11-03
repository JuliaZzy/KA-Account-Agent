"""Long-term notes for a partnership."""

from __future__ import annotations

from sqlalchemy import or_, select, update

from ...schemas import Memory
from .. import orm
from .support import _row_dict, utcnow


class MemoryMixin:
    def add_memory(self, m: Memory) -> int:
        ts = utcnow()
        with self.tx() as s:
            row = orm.Memory(**m.model_dump(exclude={"id", "created_at", "updated_at"}), created_at=ts, updated_at=ts)
            s.add(row)
            s.flush()
            return row.id

    def memories(
        self, partnership_id: str, *, active_only: bool = True, user_id: str | None = None,
        visible_to: str | None = None,
    ) -> list[Memory]:
        """Standing notes for a partnership.

        `user_id` is that person only (the memory page). `visible_to` is that person
        plus notes nobody owns. Another person's notes stay out.
        """
        with self.tx() as s:
            q = select(orm.Memory).where(orm.Memory.partnership_id == partnership_id)
            if user_id is not None:
                q = q.where(orm.Memory.user_id == user_id)
            elif visible_to is not None:
                q = q.where(or_(orm.Memory.user_id == visible_to, orm.Memory.user_id.is_(None)))
            if active_only:
                q = q.where(orm.Memory.is_active.is_(True))
            return [Memory(**_row_dict(r)) for r in s.scalars(q.order_by(orm.Memory.id.desc())).all()]

    def set_memory_active(self, id: int, active: bool) -> None:
        with self.tx() as s:
            row = s.get(orm.Memory, id)
            if row is None:
                raise KeyError(f"memory {id} not found")
            row.is_active, row.updated_at = active, utcnow()

    def mark_memories_recalled(self, ids: list[int]) -> None:
        if not ids:
            return
        ts = utcnow()
        with self.tx() as s:
            s.execute(update(orm.Memory).where(orm.Memory.id.in_(ids)).values(last_recalled_at=ts))

    def delete_memory(self, id: int) -> None:
        with self.tx() as s:
            row = s.get(orm.Memory, id)
            if row is not None:
                s.delete(row)
