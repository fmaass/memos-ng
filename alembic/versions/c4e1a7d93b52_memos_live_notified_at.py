"""memos.live_notified_at

Revision ID: c4e1a7d93b52
Revises: a9d3e57c1b20
Create Date: 2026-11-05 03:00:00

Expand-only (MEMNG-967). One NEW nullable column on `memos`: when its one "memo is live" push was
claimed. Added WITH a `now()` default and the default dropped again in the same transaction: `now()`
is stable, so Postgres stores it once as the column's missing value instead of rewriting the table,
and every EXISTING memo reads as already announced — a re-derive of an old memo's file must never
announce a memo the household saw long ago. Every row written after this reads NULL, which is what
the claim waits for. The previous image's ORM does not name the column and keeps creating memos as
before (those read NULL and are announced by the new image when their files finish). The downgrade
drops the column and nothing else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e1a7d93b52"
down_revision: str | None = "a9d3e57c1b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memos",
        sa.Column(
            "live_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.alter_column("memos", "live_notified_at", server_default=None)


def downgrade() -> None:
    op.drop_column("memos", "live_notified_at")
