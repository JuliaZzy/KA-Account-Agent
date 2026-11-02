"""deliver runs through celery

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-02 06:38:27.502685
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A stored partnership id becomes that partnership's distributor before the foreign key.
    for table in ("agent_runs", "usage_records", "audit_logs"):
        op.execute(
            f"""
            UPDATE {table} AS target
            SET org_id = partnership.distributor_id
            FROM partnerships AS partnership
            WHERE target.org_id = partnership.id
              AND NOT EXISTS (
                  SELECT 1 FROM distributors AS distributor
                  WHERE distributor.id = target.org_id
              )
            """
        )
    op.add_column("agent_runs", sa.Column("trace_id", sa.String(length=64), nullable=True))
    op.create_foreign_key("agent_runs_org_id_fkey", "agent_runs", "distributors", ["org_id"], ["id"])
    op.create_foreign_key("audit_logs_org_id_fkey", "audit_logs", "distributors", ["org_id"], ["id"])
    op.create_foreign_key("usage_records_org_id_fkey", "usage_records", "distributors", ["org_id"], ["id"])
    op.drop_table("job_queue")


def downgrade() -> None:
    op.create_table(
        "job_queue",
        sa.Column("id", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column("job_type", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
        sa.Column("status", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column("attempts", sa.INTEGER(), server_default=sa.text("0"), autoincrement=False, nullable=False),
        sa.Column("max_attempts", sa.INTEGER(), server_default=sa.text("3"), autoincrement=False, nullable=False),
        sa.Column(
            "available_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"),
            autoincrement=False, nullable=False,
        ),
        sa.Column("lock_by", sa.VARCHAR(length=64), autoincrement=False, nullable=True),
        sa.Column("lock_until", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
        sa.Column("last_error_json", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"),
            autoincrement=False, nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("job_queue_pkey")),
    )
    op.drop_constraint("usage_records_org_id_fkey", "usage_records", type_="foreignkey")
    op.drop_constraint("audit_logs_org_id_fkey", "audit_logs", type_="foreignkey")
    op.drop_constraint("agent_runs_org_id_fkey", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "trace_id")
