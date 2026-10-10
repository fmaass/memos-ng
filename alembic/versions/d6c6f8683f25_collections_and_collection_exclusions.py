"""collections and collection exclusions

Revision ID: d6c6f8683f25
Revises: a4e7c1b20f36
Create Date: 2026-10-10 11:23:43.606832

Expand-only: two NEW tables and their indexes, nothing on an existing table changes, no backfill
and no data rewrite (there is no collection data anywhere to migrate — the iOS app's trips are
device-local until its own half of MEMNG-746 uploads them).

`collections` is the household-shared trip record; its membership is derived at read time from
`tag` and the span, so nothing here stores a member list. `collection_exclusions` holds one row per
attachment taken out of one collection, keyed on the pair so concurrent identical adds are one row;
it cascades from both sides. The three named CHECKs carry the record's shape (a source exists, a
span is whole or absent, a span is not reversed). No index is added to `attachments`: membership by
date reads `taken_at_expr()`, an expression no index could serve, over a table of ~2 000 rows.

The previous image's ORM names neither table, so it runs against this schema unchanged; production
never downgrades. The downgrade exists for the rehearsal on a throwaway restore and drops exactly
what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6c6f8683f25"
down_revision: str | None = "a4e7c1b20f36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("tag", sa.String(length=255), nullable=True),
        sa.Column("span_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("span_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tz", sa.Text(), nullable=True),
        sa.Column("cover_attachment_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
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
            "(span_start IS NULL) = (span_end IS NULL)", name="ck_collections_span_pair"
        ),
        sa.CheckConstraint("span_end >= span_start", name="ck_collections_span_order"),
        sa.CheckConstraint(
            "tag IS NOT NULL OR span_start IS NOT NULL", name="ck_collections_source"
        ),
        sa.ForeignKeyConstraint(["cover_attachment_id"], ["attachments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_collections_span_start"), "collections", ["span_start"], unique=False)
    op.create_index(op.f("ix_collections_uid"), "collections", ["uid"], unique=True)
    op.create_index(
        "uq_collections_creator_idempotency_key",
        "collections",
        ["created_by", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "collection_exclusions",
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collection_id", "attachment_id"),
    )
    op.create_index(
        op.f("ix_collection_exclusions_attachment_id"),
        "collection_exclusions",
        ["attachment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_collection_exclusions_attachment_id"), table_name="collection_exclusions"
    )
    op.drop_table("collection_exclusions")
    op.drop_index(
        "uq_collections_creator_idempotency_key",
        table_name="collections",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_index(op.f("ix_collections_uid"), table_name="collections")
    op.drop_index(op.f("ix_collections_span_start"), table_name="collections")
    op.drop_table("collections")
