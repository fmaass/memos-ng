"""media_jobs kind (printready on-demand tier)

Revision ID: 8b7d6acb2567
Revises: d987e7d66011
Create Date: 2026-08-14 11:27:15.738795

Additive by construction: the server default makes every existing job row (and any INSERT from a
not-yet-restarted process during a rolling deploy) read back as `derive`, the upload pipeline that
was the only kind of work before MEMNG-49. A plain string column, not a Postgres enum — see
app/models.py:JobKind.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8b7d6acb2567'
down_revision: str | None = 'd987e7d66011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'media_jobs',
        sa.Column('kind', sa.String(length=32), server_default='derive', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('media_jobs', 'kind')
