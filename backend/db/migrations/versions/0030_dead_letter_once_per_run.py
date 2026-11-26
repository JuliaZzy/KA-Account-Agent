"""one dead letter row per original job

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM dead_letter_jobs AS older
        WHERE older.id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY original_job_id
                    ORDER BY created_at ASC, id ASC
                ) AS n
                FROM dead_letter_jobs
            ) ranked
            WHERE n > 1
        )
        """
    )
    op.create_unique_constraint(
        "uq_dead_letter_original_job", "dead_letter_jobs", ["original_job_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_dead_letter_original_job", "dead_letter_jobs", type_="unique")
