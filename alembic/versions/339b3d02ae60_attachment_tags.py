"""attachment tags

Revision ID: 339b3d02ae60
Revises: ad7a00effd6d
Create Date: 2026-10-18 14:08:28.884022

Expand-only (MEMNG-863): one NEW table, no backfill, no change to any existing row. A picture tag
is the pair (attachment, tag) — the name row is the memo tags' own `tags` row, the relation is its
own table, so a memo's `#tag` and a picture's tag never share membership. Both foreign keys
cascade: deleting an attachment or a tag row takes its pairs with it, and nothing else ever deletes
one implicitly. The primary key leads with `attachment_id` (the per-picture read); `tag_id` has its
own index for the per-tag listing filter and count.

The previous image's ORM does not name the table, so it runs against this schema unchanged;
production never downgrades. The downgrade exists for the rehearsal on a throwaway restore and drops
exactly what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "339b3d02ae60"
down_revision: str | None = "ad7a00effd6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachment_tags",
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attachment_id", "tag_id"),
    )
    op.create_index("ix_attachment_tags_tag_id", "attachment_tags", ["tag_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_attachment_tags_tag_id", table_name="attachment_tags")
    op.drop_table("attachment_tags")
