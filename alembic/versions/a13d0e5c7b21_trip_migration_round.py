"""Trip Pass migration round: the move journal, the admitting version, owner resolve, the unknown
count, the door's suspended view, the lift kinds (MEMNG-1169). Additive; the constraint swap only
widens, and no writer in this train uses the new kinds, so no row can block the downgrade.

Revision ID: a13d0e5c7b21
Revises: 85d081a451a1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a13d0e5c7b21"
down_revision: str | None = "85d081a451a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_CURRENT = (
    "issued",
    "updated",
    "revoked",
    "device_revoked",
    "redeemed",
    "named",
    "upload_started",
    "upload_finalized",
    "refused",
    "hidden",
    "unhidden",
    "removed",
)


def _swap_kinds(kinds: Sequence[str]) -> None:
    listed = ", ".join(f"'{k}'" for k in kinds)
    op.drop_constraint("ck_pass_events_kind", "pass_events", type_="check")
    op.execute(
        "ALTER TABLE pass_events ADD CONSTRAINT ck_pass_events_kind "
        f"CHECK (kind IN ({listed})) NOT VALID"
    )
    op.execute("ALTER TABLE pass_events VALIDATE CONSTRAINT ck_pass_events_kind")


def upgrade() -> None:
    op.create_table(
        "ingest_moves",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("src", sa.Text(), nullable=False),
        sa.Column("dest", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("flagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('part', 'master')", name="ck_ingest_moves_kind"),
    )
    op.create_index("ix_ingest_moves_batch_id", "ingest_moves", ["batch_id"])
    op.add_column("ingest_batches", sa.Column("pass_version", sa.Integer(), nullable=True))
    op.add_column(
        "ingest_batches", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "ingest_batches",
        sa.Column(
            "resolved_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_ingest_batches_resolved_by"),
            nullable=True,
        ),
    )
    for name in ("quarantined_unknown_total", "quarantined_unknown_cleared"):
        op.add_column(
            "trip_ingest_cursor",
            sa.Column(name, sa.BigInteger(), server_default="0", nullable=False),
        )
    for name in ("door_suspended_at", "door_suspended_seen_at"):
        op.add_column("pass_sync_state", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    _swap_kinds((*KINDS_CURRENT, "unsuspend_requested", "unsuspended"))


def downgrade() -> None:
    _swap_kinds(KINDS_CURRENT)
    for name in ("door_suspended_seen_at", "door_suspended_at"):
        op.drop_column("pass_sync_state", name)
    for name in ("quarantined_unknown_cleared", "quarantined_unknown_total"):
        op.drop_column("trip_ingest_cursor", name)
    op.drop_constraint("fk_ingest_batches_resolved_by", "ingest_batches", type_="foreignkey")
    op.drop_column("ingest_batches", "resolved_by")
    op.drop_column("ingest_batches", "resolved_at")
    op.drop_column("ingest_batches", "pass_version")
    op.drop_index("ix_ingest_moves_batch_id", table_name="ingest_moves")
    op.drop_table("ingest_moves")
