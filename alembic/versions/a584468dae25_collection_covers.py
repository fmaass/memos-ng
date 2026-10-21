"""collection covers: one chosen cover per tag, month and year collection

Revision ID: a584468dae25
Revises: 5c2e9a7d41b8
Create Date: 2026-10-21 09:08:53.184842

Expand-only (MEMNG-853): ONE new table and its index, nothing on an existing table changes, no
backfill and no data rewrite. Tag, month and year collections are derived at read time and have no
row to hold a cover, so `collection_covers` keys one chosen attachment by `(kind, key)` — the tag
name, `YYYY-MM` or `YYYY` — for the whole household. The row cascades from its attachment, so a
deleted picture takes its choice with it; the index on `attachment_id` serves that cascade.

The previous image's ORM does not name the table, so it runs against this schema unchanged;
production never downgrades. The downgrade exists for the rehearsal on a throwaway restore and
drops exactly what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a584468dae25"
down_revision: str | None = "5c2e9a7d41b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_covers",
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'tag' AND key <> '') "
            "OR (kind = 'month' AND key ~ '^[0-9]{4}-(0[1-9]|1[0-2])$') "
            "OR (kind = 'year' AND key ~ '^[0-9]{4}$')",
            name="ck_collection_covers_key",
        ),
        sa.CheckConstraint("kind IN ('tag', 'month', 'year')", name="ck_collection_covers_kind"),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("kind", "key"),
    )
    op.create_index(
        op.f("ix_collection_covers_attachment_id"),
        "collection_covers",
        ["attachment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_collection_covers_attachment_id"), table_name="collection_covers")
    op.drop_table("collection_covers")
