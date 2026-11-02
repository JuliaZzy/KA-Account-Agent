"""record model usage source and run budgets

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-02 07:36:33.345359
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("max_model_calls", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("max_tokens", sa.Integer(), nullable=True))
    op.add_column("model_calls", sa.Column("usage_source", sa.String(length=32), nullable=True))
    op.add_column("usage_records", sa.Column("usage_source", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_records", "usage_source")
    op.drop_column("model_calls", "usage_source")
    op.drop_column("agent_runs", "max_tokens")
    op.drop_column("agent_runs", "max_model_calls")
