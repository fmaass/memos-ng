"""live activity tokens: one ActivityKit push-to-start token per iOS device

Revision ID: ae23178140ce
Revises: eeda927cec0a
Create Date: 2026-11-05 09:00:00

Expand-only (MEMNG-946): ONE new table and its index, nothing on an existing table changes, no
backfill and no data rewrite. `live_activity_tokens` holds the push-to-start token each iOS device
registers, as lowercase hex, unique across the table — one device's token belongs to one account at
a time — with the APNs environment it was issued for. The row cascades from its user, and the index
on `user_id` serves that cascade and the per-user lookup a sender will make.

The previous image's ORM does not name the table, so it runs against this schema unchanged;
production never downgrades. The downgrade exists for the rehearsal on a throwaway restore and
drops exactly what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ae23178140ce"
down_revision: str | None = "eeda927cec0a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_activity_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column(
            "environment",
            sa.String(length=16),
            server_default=sa.text("'production'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'sandbox')",
            name="ck_live_activity_tokens_environment",
        ),
        sa.CheckConstraint(
            "token ~ '^[0-9a-f]+$' AND length(token) BETWEEN 32 AND 512",
            name="ck_live_activity_tokens_token",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(
        op.f("ix_live_activity_tokens_user_id"),
        "live_activity_tokens",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_live_activity_tokens_user_id"), table_name="live_activity_tokens")
    op.drop_table("live_activity_tokens")
