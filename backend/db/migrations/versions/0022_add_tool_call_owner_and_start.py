"""add tool call owner and start

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-02 08:24:32.753813
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("org_id", sa.String(length=64), nullable=True))
    op.add_column("tool_calls", sa.Column("user_id", sa.String(length=64), nullable=True))
    op.add_column("tool_calls", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE tool_calls AS call
        SET org_id = run.org_id, user_id = run.user_id
        FROM agent_runs AS run
        WHERE call.run_id = run.id
        """
    )
    op.create_foreign_key("tool_calls_org_id_fkey", "tool_calls", "distributors", ["org_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("tool_calls_org_id_fkey", "tool_calls", type_="foreignkey")
    op.drop_column("tool_calls", "started_at")
    op.drop_column("tool_calls", "user_id")
    op.drop_column("tool_calls", "org_id")
