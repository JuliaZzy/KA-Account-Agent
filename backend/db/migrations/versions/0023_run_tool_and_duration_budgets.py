"""run tool and duration budgets

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-02 09:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("max_tool_calls", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("max_duration_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "max_duration_seconds")
    op.drop_column("agent_runs", "max_tool_calls")
