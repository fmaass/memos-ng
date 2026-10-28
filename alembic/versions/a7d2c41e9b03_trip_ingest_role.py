"""trip ingest role: suspension, change cursors, completion key, refusal counter

Revision ID: a7d2c41e9b03
Revises: 79cdd613ea6b
Create Date: 2026-10-28 02:00:00

Expand-only (trip pass P2). Four NULLABLE columns with no default and no backfill —
`passes.suspended_at`, `pass_devices.change_seq`, `ingest_batches.completion_key` (unique) and
`ingest_batches.change_seq` (both cursors uniquely indexed: a pull pages by `> since`, so a
shared value could fall across a page boundary) — one NEW sequence `ingest_change_seq` and one NEW
table `ingest_refusals`. No existing row is rewritten and no existing column changes; the previous image's
ORM names none of it. All of it is written only by the ingest roles; home's copies stay empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2c41e9b03"
down_revision: str | None = "79cdd613ea6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("passes", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pass_devices", sa.Column("change_seq", sa.BigInteger(), nullable=True))
    op.create_index(
        op.f("ix_pass_devices_change_seq"), "pass_devices", ["change_seq"], unique=True
    )
    op.add_column(
        "ingest_batches", sa.Column("completion_key", sa.String(length=64), nullable=True)
    )
    op.create_unique_constraint(
        op.f("ingest_batches_completion_key_key"), "ingest_batches", ["completion_key"]
    )
    op.add_column("ingest_batches", sa.Column("change_seq", sa.BigInteger(), nullable=True))
    op.create_index(
        op.f("ix_ingest_batches_change_seq"), "ingest_batches", ["change_seq"], unique=True
    )
    op.execute(sa.schema.CreateSequence(sa.Sequence("ingest_change_seq")))
    op.create_table(
        "ingest_refusals",
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refusals", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("ip_hash", "window_start", name=op.f("ingest_refusals_pkey")),
    )


def downgrade() -> None:
    op.drop_table("ingest_refusals")
    op.execute(sa.schema.DropSequence(sa.Sequence("ingest_change_seq")))
    op.drop_index(op.f("ix_ingest_batches_change_seq"), table_name="ingest_batches")
    op.drop_column("ingest_batches", "change_seq")
    op.drop_constraint(
        op.f("ingest_batches_completion_key_key"), "ingest_batches", type_="unique"
    )
    op.drop_column("ingest_batches", "completion_key")
    op.drop_index(op.f("ix_pass_devices_change_seq"), table_name="pass_devices")
    op.drop_column("pass_devices", "change_seq")
    op.drop_column("passes", "suspended_at")
