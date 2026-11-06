"""run parent

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-29 02:16:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("parent_run_id", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_agent_runs_parent_run_id"), "agent_runs", ["parent_run_id"], unique=False)
    op.create_foreign_key(
        "agent_runs_parent_run_id_fkey", "agent_runs", "agent_runs", ["parent_run_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("agent_runs_parent_run_id_fkey", "agent_runs", type_="foreignkey")
    op.drop_index(op.f("ix_agent_runs_parent_run_id"), table_name="agent_runs")
    op.drop_column("agent_runs", "parent_run_id")
