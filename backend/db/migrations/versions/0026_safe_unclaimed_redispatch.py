"""track queued age and bounded sweeper redispatches

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    queued_type = sa.DateTime(timezone=True)
    op.add_column("agent_runs", sa.Column("queued_at", queued_type, nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("redispatch_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE agent_runs SET queued_at = COALESCE(updated_at, created_at)")
    op.alter_column(
        "agent_runs",
        "queued_at",
        existing_type=queued_type,
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.create_index(
        "idx_agent_runs_queued_at",
        "agent_runs",
        ["queued_at"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("idx_agent_runs_queued_at", table_name="agent_runs")
    op.drop_column("agent_runs", "redispatch_count")
    op.drop_column("agent_runs", "queued_at")
