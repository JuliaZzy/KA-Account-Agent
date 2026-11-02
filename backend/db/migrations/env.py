"""Alembic environment. Two ways in:

  - in-process: `backend.db.engine.run_migrations(engine)` hands a live connection over
    `config.attributes["connection"]` — used by the app on startup and by every test;
  - CLI: `alembic upgrade head` / `alembic revision --autogenerate` from the repo root,
    against `KA_ACCOUNT_DB_URL`.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from backend.config import db_url
from backend.db.orm import Base

config = context.config
target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url") or db_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url") or db_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
