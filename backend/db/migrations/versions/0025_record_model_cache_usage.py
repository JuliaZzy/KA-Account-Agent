"""record provider cache usage

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("model_calls", "usage_records"):
        op.add_column(table, sa.Column("cache_hit_tokens", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("cache_miss_tokens", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("cache_creation_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    for table in ("usage_records", "model_calls"):
        op.drop_column(table, "cache_creation_tokens")
        op.drop_column(table, "cache_miss_tokens")
        op.drop_column(table, "cache_hit_tokens")
