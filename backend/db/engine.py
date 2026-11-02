"""Engine construction and schema migration.

One engine per `Database`, always PostgreSQL — `KA_ACCOUNT_DB_URL` is required and there
is no fallback engine (see `config.db_url` for why).

Pool size is env-tunable (`KA_ACCOUNT_POOL_SIZE`, `KA_ACCOUNT_MAX_OVERFLOW`) because load
testing is exactly the exercise of moving it: the ceiling on concurrent in-flight requests is
`pool_size + max_overflow`, and past it callers queue rather than fail. `pool_pre_ping`
discards connections the server closed under us — without it a restarted or idle-timed-out
Postgres surfaces as a burst of errors in the middle of a run.

The schema is owned by Alembic (`backend/db/migrations`). `run_migrations` brings a
database to head in-process — the app calls it on startup and the tests on every fresh
file, so there is exactly one definition of the schema and it is the versioned one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine

from ..config import env

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _json_dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)


def _int_env(name: str, default: int) -> int:
    raw = env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def make_engine(url: str) -> Engine:
    return create_engine(
        url,
        json_serializer=_json_dumps,
        future=True,
        # Read `timestamptz` back in UTC whatever the server's own timezone is. The instant
        # is the same either way, but without this the datetimes the app formats and
        # compares carry whatever offset the database host happens to be set to.
        connect_args={"options": "-c timezone=UTC"},
        pool_size=_int_env("KA_ACCOUNT_POOL_SIZE", 10),
        max_overflow=_int_env("KA_ACCOUNT_MAX_OVERFLOW", 20),
        pool_timeout=_int_env("KA_ACCOUNT_POOL_TIMEOUT", 30),
        pool_recycle=1800,
        pool_pre_ping=True,
    )


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def run_migrations(engine: Engine) -> None:
    """Upgrade to head using the given engine's connection (so tests and the app migrate
    the very database they are about to use, whatever its URL)."""
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
