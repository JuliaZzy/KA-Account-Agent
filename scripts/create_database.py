"""Create the database named in KA_ACCOUNT_DB_URL, if it is not there yet.

    ./dev db

Alembic owns the schema, but it cannot create the database that holds it. This is the one
step that has to happen before `Database()` can migrate anything, so it lives here rather
than in the app: a deploy runs it once.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.config import db_url


def main() -> int:
    url = make_url(db_url())
    if "YOUR_PASSWORD" in str(url):
        print("KA_ACCOUNT_DB_URL still has the YOUR_PASSWORD placeholder — edit .env first.", file=sys.stderr)
        return 1

    target = url.database
    # "postgres" always exists; connect there to ask about the one we actually want.
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("select 1 from pg_database where datname = :n"), {"n": target}).scalar()
            if exists:
                print(f"database {target!r} already exists")
            else:
                conn.execute(text(f'CREATE DATABASE "{target}"'))
                print(f"created database {target!r}")
    finally:
        admin.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
