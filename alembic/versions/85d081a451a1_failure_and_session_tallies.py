"""media job failure and upload session tallies

Revision ID: 85d081a451a1
Revises: f9af5db6b98c
Create Date: 2026-10-31 10:00:00

Expand-only (MEMNG-1046). Two NEW tables of durable counters the app's /metrics scrape reads,
because the worker that does the counting serves no /metrics: `media_job_failure_tallies` (one row
per consumer class and failure reason) and `upload_session_tallies` (one row per session result
only the worker sees — `expired`). No existing table or row is touched; the previous image's ORM
names neither, and the worker writes them under a savepoint, so an image that runs before the
upgrade loses a count, never a job.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "85d081a451a1"
down_revision: str | None = "f9af5db6b98c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_job_failure_tallies",
        sa.Column("lane", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("lane", "reason", name=op.f("media_job_failure_tallies_pkey")),
    )
    op.create_table(
        "upload_session_tallies",
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("result", name=op.f("upload_session_tallies_pkey")),
    )


def downgrade() -> None:
    op.drop_table("upload_session_tallies")
    op.drop_table("media_job_failure_tallies")
