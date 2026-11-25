"""Brands, distributors, users, partnerships, products, and the shared catalog."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from ...schemas import AgentSettings, CommercialRule, Partnership, Product, User
from .. import orm
from .support import _row_dict, utcnow


class BrandMixin:
    def upsert_brand(self, id: str, name: str, preferred_currency: str = "USD") -> None:
        with self.tx() as s:
            self._upsert(s, orm.Brand, id, {"id": id, "name": name, "preferred_currency": preferred_currency})

    def upsert_distributor(self, id: str, name: str, country: str | None = None) -> None:
        with self.tx() as s:
            self._upsert(s, orm.Distributor, id, {"id": id, "name": name, "country": country})

    def upsert_user(self, id: str, distributor_id: str, name: str) -> None:
        with self.tx() as s:
            self._upsert(s, orm.User, id, {"id": id, "distributor_id": distributor_id, "name": name})

    def upsert_partnership(self, p: Partnership) -> None:
        """Creates the brand and distributor rows from the names on the model when they
        don't exist yet, so a partnership can be written from one record."""
        with self.tx() as s:
            if s.get(orm.Brand, p.brand_id) is None:
                self._upsert(s, orm.Brand, p.brand_id, {"id": p.brand_id, "name": p.brand_name, "preferred_currency": p.currency})
            if s.get(orm.Distributor, p.distributor_id) is None:
                self._upsert(s, orm.Distributor, p.distributor_id, {"id": p.distributor_id, "name": p.distributor_name, "country": p.ship_to_country})
            self._upsert(s, orm.Partnership, p.id, p.model_dump(exclude={"brand_name", "distributor_name"}))

    def upsert_product(self, brand_id: str, p: Product) -> None:
        self.upsert_products(brand_id, [p])

    def upsert_products(self, brand_id: str, products: list[Product]) -> None:
        """One transaction for a batch of products."""
        with self.tx() as s:
            # Load the existing rows up front so `_upsert`'s get() is an identity-map hit, not a query each.
            s.scalars(select(orm.Product).where(orm.Product.id.in_([p.id for p in products]))).all()
            for p in products:
                self._upsert(s, orm.Product, p.id, {**p.model_dump(), "brand_id": brand_id})

    def upsert_rule(self, brand_id: str, r: CommercialRule, contract_id: str | None = None) -> None:
        with self.tx() as s:
            self._upsert(s, orm.CommercialRule, r.id, {**r.model_dump(), "brand_id": brand_id, "contract_id": contract_id})

    def put_settings(self, brand_id: str, settings: AgentSettings) -> None:
        with self.tx() as s:
            self._upsert(s, orm.BrandAgentSettings, brand_id, {**settings.model_dump(), "brand_id": brand_id})

    def products(self, brand_id: str) -> list[Product]:
        with self.tx() as s:
            rows = s.scalars(select(orm.Product).where(orm.Product.brand_id == brand_id).order_by(orm.Product.id)).all()
            return [Product(**_row_dict(r)) for r in rows]

    def product_names(self, brand_id: str, skus: list[str]) -> dict[str, str]:
        """Native name, else English, for these SKUs on this brand. Unknown SKUs are omitted.

        The catalog is large, so this looks up the given SKUs instead of loading every product.
        """
        wanted = list(dict.fromkeys(sku.strip() for sku in skus if isinstance(sku, str) and sku.strip()))
        if not wanted:
            return {}
        with self.tx() as s:
            found = s.execute(
                select(orm.Product.sku, orm.Product.product_name_native, orm.Product.product_name_en)
                .where(orm.Product.brand_id == brand_id, orm.Product.sku.in_(wanted))
            ).all()
        names: dict[str, str] = {}
        for sku, native, english in found:
            name = (native or "").strip() or (english or "").strip()
            if name:
                names[sku] = name
        return names

    def rules(self, brand_id: str) -> list[CommercialRule]:
        with self.tx() as s:
            rows = s.scalars(select(orm.CommercialRule).where(orm.CommercialRule.brand_id == brand_id).order_by(orm.CommercialRule.id)).all()
            return [CommercialRule(**_row_dict(r)) for r in rows]

    @staticmethod
    def _partnership(row: orm.Partnership) -> Partnership:
        return Partnership(
            id=row.id, brand_id=row.brand_id, distributor_id=row.distributor_id, brand_name=row.brand.name,
            distributor_name=row.distributor.name, ship_to_country=row.ship_to_country, currency=row.currency,
        )

    def partnership(self, id: str) -> Partnership | None:
        with self.tx() as s:
            row = s.get(orm.Partnership, id)
            return self._partnership(row) if row else None

    def partnerships(self) -> list[Partnership]:
        with self.tx() as s:
            return [self._partnership(r) for r in s.scalars(select(orm.Partnership).order_by(orm.Partnership.id)).all()]

    def user(self, id: str) -> User | None:
        with self.tx() as s:
            row = s.get(orm.User, id)
            return User(id=row.id, distributor_id=row.distributor_id, name=row.name) if row else None

    def distributor_ids(self) -> list[str]:
        with self.tx() as s:
            return list(s.scalars(select(orm.Distributor.id).order_by(orm.Distributor.id)))

    def users(self, distributor_id: str | None = None) -> list[User]:
        with self.tx() as s:
            q = select(orm.User).order_by(orm.User.name, orm.User.id)
            if distributor_id:
                q = q.where(orm.User.distributor_id == distributor_id)
            return [User(id=r.id, distributor_id=r.distributor_id, name=r.name) for r in s.scalars(q).all()]

    def settings(self, brand_id: str) -> AgentSettings:
        with self.tx() as s:
            row = s.get(orm.BrandAgentSettings, brand_id)
            d = _row_dict(row) if row else {}
            return AgentSettings(**{k: v for k, v in d.items() if v is not None and k != "brand_id"})

    def replace_catalog(self, brands: Iterable[dict[str, Any]], products: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Make the product table exactly `products`, in one transaction.

        Hundreds of thousands of rows, so it goes through COPY into a staging table and one
        upsert rather than an ORM object per row. Only brands some product belongs to are
        written, upserted and never deleted — they carry partnerships, rules and settings.
        A product missing from the new load is deleted, unless an order line points at it:
        an order keeps its product."""
        product_cols = [c.key for c in orm.Product.__table__.columns if c.key not in ("created_at", "updated_at")]
        json_cols = {c.key for c in orm.Product.__table__.columns if isinstance(c.type, JSONB)}
        cols = ", ".join(product_cols)
        ts = utcnow()
        raw = self.engine.raw_connection()
        try:
            with raw.cursor() as cur:
                # CREATE TABLE AS copies the column types but none of the NOT NULLs, which the
                # timestamps would trip over: those are stamped on the way out of staging.
                cur.execute("CREATE TEMP TABLE stage_brands ON COMMIT DROP AS SELECT id, name, preferred_currency FROM brands WITH NO DATA")
                with cur.copy("COPY stage_brands (id, name, preferred_currency) FROM STDIN") as cp:
                    for b in brands:
                        cp.write_row((b["id"], b["name"], b.get("preferred_currency") or "USD"))
                cur.execute(f"CREATE TEMP TABLE stage_products ON COMMIT DROP AS SELECT {cols} FROM products WITH NO DATA")
                with cur.copy(f"COPY stage_products ({cols}) FROM STDIN") as cp:
                    for p in products:
                        cp.write_row([json.dumps(p.get(c) or []) if c in json_cols else p.get(c) for c in product_cols])

                cur.execute(
                    "INSERT INTO brands (id, name, preferred_currency, created_at, updated_at)"
                    " SELECT id, name, preferred_currency, %(ts)s, %(ts)s FROM stage_brands b"
                    " WHERE EXISTS (SELECT 1 FROM stage_products p WHERE p.brand_id = b.id)"
                    " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, updated_at = EXCLUDED.updated_at",
                    {"ts": ts},
                )
                n_brands = cur.rowcount
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in product_cols if c != "id")
                cur.execute(
                    f"INSERT INTO products ({cols}, created_at, updated_at) SELECT {cols}, %(ts)s, %(ts)s FROM stage_products"
                    f" ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = EXCLUDED.updated_at",
                    {"ts": ts},
                )
                n_products = cur.rowcount
                cur.execute(
                    "DELETE FROM products p WHERE NOT EXISTS (SELECT 1 FROM stage_products s WHERE s.id = p.id)"
                    " AND NOT EXISTS (SELECT 1 FROM purchase_order_items i WHERE i.product_id = p.id)"
                )
                n_removed = cur.rowcount
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()
        return {"brands": n_brands, "products": n_products, "removed": n_removed}

    @staticmethod
    def _catalog_where(query: str | None, brand: str | None, category: str | None, status: str | None) -> list[Any]:
        def like(text: str) -> str:
            return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        where: list[Any] = []
        if q := (query or "").strip():
            where.append(or_(*(col.ilike(like(q), escape="\\") for col in (
                orm.Product.sku, orm.Product.product_name_en, orm.Product.product_name_native,
            ))))
        if b := (brand or "").strip():
            where.append(orm.Brand.name.ilike(like(b), escape="\\"))
        if category:
            where.append(orm.Product.category == category)
        if status:
            where.append(orm.Product.product_status == status)
        return where

    def search_catalog(
        self, query: str | None = None, *, brand: str | None = None, category: str | None = None,
        status: str | None = None, limit: int = 100, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of the catalog across all brands, with the total the filters match.
        `query` is a substring of the SKU or either name; `brand` a substring of the brand name."""
        where = self._catalog_where(query, brand, category, status)
        joined = select(orm.Product, orm.Brand.name).join(orm.Brand, orm.Brand.id == orm.Product.brand_id).where(*where)
        with self.tx() as s:
            total = s.scalar(select(func.count()).select_from(joined.subquery())) or 0
            rows = s.execute(joined.order_by(orm.Brand.name, orm.Product.sku).limit(limit).offset(offset)).all()
            return [
                {**Product(**_row_dict(p)).model_dump(), "brand_id": p.brand_id, "brand_name": name} for p, name in rows
            ], total

    def catalog_stats(
        self, query: str | None = None, *, brand: str | None = None, category: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        """Counts for the same filters as `search_catalog`: products per status, brands, categories."""
        where = self._catalog_where(query, brand, category, status)
        base = select(orm.Product.product_status, orm.Product.brand_id, orm.Product.category).join(
            orm.Brand, orm.Brand.id == orm.Product.brand_id,
        ).where(*where).subquery()
        with self.tx() as s:
            by_status = dict(s.execute(select(base.c.product_status, func.count()).group_by(base.c.product_status)).all())
            brands, categories = s.execute(
                select(func.count(base.c.brand_id.distinct()), func.count(base.c.category.distinct()))
            ).one()
        return {"by_status": by_status, "brands": brands, "categories": categories}

    def catalog_categories(self) -> list[str]:
        with self.tx() as s:
            return list(s.scalars(select(orm.Product.category).where(orm.Product.category.is_not(None)).distinct().order_by(orm.Product.category)))
