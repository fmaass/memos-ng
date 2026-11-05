"""uploads.withdrawn_at

Revision ID: a9d3e57c1b20
Revises: ae23178140ce
Create Date: 2026-11-05 01:00:00

Expand-only (MEMNG-953). One NEW nullable column on `uploads`: when the owner withdrew the session
through `DELETE /api/uploads/{uid}`. Every existing row reads NULL — never withdrawn — the moment
the column exists, and the previous image, whose ORM does not name it, keeps creating and consuming
sessions exactly as before. No row is rewritten; the downgrade drops the column and nothing else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d3e57c1b20"
down_revision: str | None = "ae23178140ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("uploads", "withdrawn_at")
