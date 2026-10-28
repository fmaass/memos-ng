"""trip ingest puller: the home pull cursors and the device-revocation ack

Revision ID: c3e8b5a1d2f4
Revises: a7d2c41e9b03
Create Date: 2026-10-28 06:00:00

Expand-only (trip pass P3a). One NULLABLE column with no default and no backfill —
`pass_devices.revocation_acked_version`, the home version of a device revocation the ingest host
has confirmed — and one NEW single-row table `trip_ingest_cursor` holding the two pull cursors.
No existing row is rewritten and no existing column changes; the previous image's ORM names none
of it. Both are written only by home's puller; the ingest host's copies stay empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8b5a1d2f4"
down_revision: str | None = "a7d2c41e9b03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pass_devices", sa.Column("revocation_acked_version", sa.Integer(), nullable=True)
    )
    op.add_column("ingest_blobs", sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "trip_ingest_cursor",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("devices_since", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("batches_since", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_trip_ingest_cursor_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("trip_ingest_cursor_pkey")),
    )


def downgrade() -> None:
    op.drop_table("trip_ingest_cursor")
    op.drop_column("ingest_blobs", "filed_at")
    op.drop_column("pass_devices", "revocation_acked_version")
