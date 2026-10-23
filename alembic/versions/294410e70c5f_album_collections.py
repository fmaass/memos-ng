"""album collections: a collection kind and the hand-picked member rows of an album

Revision ID: 294410e70c5f
Revises: a584468dae25
Create Date: 2026-10-23 10:23:32.000000

Expand-only (MEMNG-802): `collections` gains `kind` (`trip` | `album`) with the server default
`trip`, so every existing row becomes a trip without being rewritten (a constant default is a
catalogue change in Postgres 11+), and the source CHECK is rewritten so a trip still needs a tag or
a span while an album has neither. ONE new table, `collection_members`, holds an album's pictures:
`(collection_id, attachment_id)` is its key, both parents cascade, and the index on
`attachment_id` serves the cascade from an attachment delete.

The previous image's ORM names neither the column nor the table: its inserts take the default
`trip`, which the rewritten CHECK judges exactly as the old one did. Production never downgrades;
the downgrade exists for the rehearsal on a throwaway restore. An album cannot be expressed in the
old shape, so the downgrade REFUSES while one exists rather than deleting it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "294410e70c5f"
down_revision: str | None = "a584468dae25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SOURCE = "tag IS NOT NULL OR span_start IS NOT NULL"
_NEW_SOURCE = (
    "(kind = 'trip' AND (tag IS NOT NULL OR span_start IS NOT NULL)) "
    "OR (kind = 'album' AND tag IS NULL AND span_start IS NULL AND span_end IS NULL)"
)


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("kind", sa.String(length=8), server_default="trip", nullable=False),
    )
    op.create_check_constraint("ck_collections_kind", "collections", "kind IN ('trip', 'album')")
    op.drop_constraint("ck_collections_source", "collections", type_="check")
    op.create_check_constraint("ck_collections_source", "collections", _NEW_SOURCE)
    op.create_table(
        "collection_members",
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("collection_id", "attachment_id"),
    )
    op.create_index(
        op.f("ix_collection_members_attachment_id"),
        "collection_members",
        ["attachment_id"],
        unique=False,
    )


def downgrade() -> None:
    albums = op.get_bind().execute(sa.text("SELECT count(*) FROM collections WHERE kind = 'album'"))
    if albums.scalar_one():
        raise RuntimeError("refusing to downgrade: an album exists and has no trip shape")
    op.drop_index(op.f("ix_collection_members_attachment_id"), table_name="collection_members")
    op.drop_table("collection_members")
    op.drop_constraint("ck_collections_source", "collections", type_="check")
    op.create_check_constraint("ck_collections_source", "collections", _OLD_SOURCE)
    op.drop_constraint("ck_collections_kind", "collections", type_="check")
    op.drop_column("collections", "kind")
