"""users.notify_on_household_posts

Revision ID: eeda927cec0a
Revises: 85d081a451a1
Create Date: 2026-11-04 10:00:00

Expand-only (MEMNG-972). One NEW column on `users`: the per-user opt-in to a push when another
household member posts a memo (MEMNG-967 reads it). NOT NULL with a server default of false, so
every existing row reads false the moment the column exists and the previous image — whose ORM does
not name it — keeps inserting users that the database fills in as false. No row is rewritten by
hand; the downgrade drops the column and nothing else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eeda927cec0a"
down_revision: str | None = "85d081a451a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "notify_on_household_posts",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_on_household_posts")
