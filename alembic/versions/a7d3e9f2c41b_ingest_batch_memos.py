"""ingest_batch_memos

Revision ID: a7d3e9f2c41b
Revises: f8af654fa068
Create Date: 2026-11-06 14:00:00

Expand-only (MEMNG-1266). One NEW table: a guest batch now files one memo per capture day, and a
later part of the batch has to find the memo of its day. `ingest_batches.memo_id` is kept as it is
(the earliest day's memo), so the previous image, whose ORM does not name the table, keeps filing
and reading exactly as before. The backfill only ADDS a row per already-filed batch, keyed by its
memo's UTC day, so a batch half filed across the deploy appends to that memo when its next part
falls on the same day. The downgrade drops the table and nothing else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e9f2c41b"
down_revision: str | None = "f8af654fa068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_batch_memos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("memo_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["ingest_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memo_id"], ["memos.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "day", name="uq_ingest_batch_memos_batch_day"),
    )
    op.create_index(
        op.f("ix_ingest_batch_memos_memo_id"), "ingest_batch_memos", ["memo_id"], unique=False
    )
    op.execute(
        "INSERT INTO ingest_batch_memos (batch_id, day, memo_id) "
        "SELECT b.id, (m.created_at AT TIME ZONE 'UTC')::date, b.memo_id "
        "FROM ingest_batches b JOIN memos m ON m.id = b.memo_id"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ingest_batch_memos_memo_id"), table_name="ingest_batch_memos")
    op.drop_table("ingest_batch_memos")
