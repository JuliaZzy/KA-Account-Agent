"""record the agent run, loop turn and self-rating on each text2sql attempt

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-10 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("text2sql_logs", sa.Column("run_id", sa.String(length=64), nullable=True))
    op.add_column("text2sql_logs", sa.Column("turn", sa.Integer(), nullable=True))
    op.add_column("text2sql_logs", sa.Column("score", sa.Integer(), nullable=True))
    op.create_index("ix_text2sql_logs_run_id", "text2sql_logs", ["run_id"])
    op.create_foreign_key(
        "fk_text2sql_logs_run_id", "text2sql_logs", "agent_runs", ["run_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_text2sql_logs_run_id", "text2sql_logs", type_="foreignkey")
    op.drop_index("ix_text2sql_logs_run_id", table_name="text2sql_logs")
    op.drop_column("text2sql_logs", "score")
    op.drop_column("text2sql_logs", "turn")
    op.drop_column("text2sql_logs", "run_id")
