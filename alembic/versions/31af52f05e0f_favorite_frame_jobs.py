"""favorite_frame_jobs

Revision ID: 31af52f05e0f
Revises: a7d3e9f2c41b
Create Date: 2026-11-06 22:00:00

Expand-only (plan PLAN_2026-11-06_favorites-to-aura-frame.md, B1). One NEW table: the queue of
hearted pictures bound for the favorites album. Nothing is backfilled here (a one-off script
enqueues the existing favorites), `frame_share_jobs` and its indexes are not touched, and the
previous image — whose ORM does not know the table — runs unchanged beside it.

`state` reuses the existing `frame_share_state` type (created by f156a444d37e, present in prod), so
the column is `postgresql.ENUM(..., create_type=False)`: the generic `sa.Enum(create_type=False)`
still emits CREATE TYPE on SQLAlchemy 2.0 and would fail with DuplicateObject. The downgrade drops
the table only; the type belongs to `frame_share_jobs`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "31af52f05e0f"
down_revision: str | None = "a7d3e9f2c41b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "favorite_frame_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            postgresql.ENUM(
                "queued",
                "claimed",
                "sent",
                "failed",
                "skipped",
                name="frame_share_state",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("aura_album_photo_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_favorite_frame_jobs_attachment_id"),
        "favorite_frame_jobs",
        ["attachment_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_favorite_frame_jobs_state"), "favorite_frame_jobs", ["state"], unique=False
    )
    op.create_index(
        op.f("ix_favorite_frame_jobs_user_id"), "favorite_frame_jobs", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("favorite_frame_jobs")
