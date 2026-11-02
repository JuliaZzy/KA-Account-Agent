"""Shared fixtures.

`make_db` is the one way tests get a database: a throwaway PostgreSQL database per name,
created on the server `KA_ACCOUNT_TEST_DB_URL` points at and dropped at teardown. Asking
for the same `name` twice returns the same database, so a test can close one and reopen it.

Tests run on the same engine the app deploys on — there is no lighter stand-in, because a
stand-in only proves the code works on the stand-in.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.config import env
from backend.db import Database
from backend.po.catalog import Catalog
from backend.schemas import AgentSettings, CommercialRule, Partnership, Product

# MOQ is stored in UNITS and enforced in CASES: 100 units / 12 per case = 9 cases.
CATALOG_ROWS = [
    dict(id="p-fo", sku="FO-100", product_name_en="Deep Sea Fish Oil", product_name_native="深海鱼油omega-3", case_pack=12, case_price=100, moq_units=100, cases_per_layer=5),
    dict(id="p-sr", sku="SR-200", product_name_en="Radiance Serum", case_pack=6, case_price=50),
    dict(id="p-srp", sku="SR-201", product_name_en="Radiance Serum Plus", case_pack=6, case_price=60),
    dict(id="p-np", sku="NP-300", product_name_en="Unpriced Item", case_pack=6, case_price=None),
    dict(id="p-gs", sku="GS-010", product_name_en="Gift Set Mini", case_pack=24, case_price=40, product_status="赠品"),
    dict(id="p-gu", sku="GU-020", product_name_en="Gift Sachet", case_pack=24, case_price=None, product_status="赠品"),
    dict(id="p-old", sku="OLD-900", product_name_en="Legacy Night Cream", case_pack=6, case_price=45, product_status="停产"),
]


@pytest.fixture
def catalog() -> Catalog:
    return Catalog([Product(**r) for r in CATALOG_ROWS])


@pytest.fixture
def settings() -> AgentSettings:
    return AgentSettings()


@pytest.fixture
def partnership() -> Partnership:
    return Partnership(id="ps-1", brand_id="brand-1", distributor_id="dist-1", brand_name="Brand", distributor_name="Dist", ship_to_country="US", currency="USD")


def _server_url(server: str, database: str) -> str:
    return make_url(server).set(database=database).render_as_string(hide_password=False)


def _admin(server: str, statement: str) -> None:
    """CREATE/DROP DATABASE cannot run inside a transaction — hence AUTOCOMMIT."""
    engine = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


@pytest.fixture
def make_db():
    server = env("KA_ACCOUNT_TEST_DB_URL")
    if not server:
        pytest.skip("KA_ACCOUNT_TEST_DB_URL is not set — see .env.example")
    stores: dict[str, str] = {}
    live: list[Database] = []

    def build(name: str = "test") -> Database:
        if name not in stores:
            stores[name] = f"pdtest_{uuid.uuid4().hex[:12]}"
            _admin(server, f'CREATE DATABASE "{stores[name]}"')
        d = Database(_server_url(server, stores[name]))
        live.append(d)
        return d

    yield build

    # Postgres refuses to drop a database with sessions still on it, so every pool goes first.
    for d in live:
        d.engine.dispose()
    for database in stores.values():
        _admin(server, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


@pytest.fixture
def db(make_db, partnership, settings) -> Database:
    d = make_db()
    d.upsert_partnership(partnership)  # creates brand-1 / dist-1 from the names on it
    for r in CATALOG_ROWS:
        d.upsert_product("brand-1", Product(**r))
    d.put_settings("brand-1", settings)
    return d


def _nearest_double(method: str) -> Any:
    """The scripted model on this call. Production never loads this module."""
    frame = sys._getframe(2)
    while frame is not None:
        for value in frame.f_locals.values():
            try:
                fn = getattr(value, method, None)
            except Exception:
                continue
            if callable(fn):
                return value
        frame = frame.f_back
    return None


@pytest.fixture(autouse=True)
def _scripted_models(monkeypatch):
    """Send model calls at the FakeLLM on the stack. The app itself calls the real clients."""
    import backend.llm.calls as calls

    real = {
        "school_json": calls.school_json,
        "school_text": calls.school_text,
        "flash_json": calls.flash_json,
        "flash_tools": calls.flash_tools,
    }

    def school_json(model, *, system, messages, schema=None, temperature=0, client=None):
        llm = _nearest_double("complete_json")
        if llm is None:
            return real["school_json"](model, system=system, messages=messages, schema=schema, temperature=temperature)
        return llm.complete_json(system=system, messages=messages, schema=schema, temperature=temperature)

    def school_text(model, *, system, messages, temperature=0, client=None):
        llm = _nearest_double("stream_text")
        if llm is None:
            yield from real["school_text"](model, system=system, messages=messages, temperature=temperature)
            return
        yield from llm.stream_text(system=system, messages=messages, temperature=temperature)

    def flash_json(*, system, messages, schema=None, temperature=0, client=None):
        llm = _nearest_double("complete_json")
        if llm is None:
            return real["flash_json"](system=system, messages=messages, schema=schema, temperature=temperature)
        return llm.complete_json(system=system, messages=messages, schema=schema, temperature=temperature)

    def flash_tools(*, system, messages, tools, client=None):
        llm = _nearest_double("complete")
        if llm is None:
            return real["flash_tools"](system=system, messages=messages, tools=tools)
        return llm.complete(system=system, messages=messages, tools=tools)

    monkeypatch.setattr(calls, "school_json", school_json)
    monkeypatch.setattr(calls, "school_text", school_text)
    monkeypatch.setattr(calls, "flash_json", flash_json)
    monkeypatch.setattr(calls, "flash_tools", flash_tools)


def rule(**over) -> CommercialRule:
    base = dict(id="r", name="rule", rule_type="min_order_value", rule_config={}, severity="block")
    return CommercialRule(**{**base, **over})
