"""frame_share_jobs.claimed_at lease stamp

Revision ID: 99505b747742
Revises: 75d3053aadf3
Create Date: 2026-08-13 17:16:39.599562

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '99505b747742'
down_revision: str | None = '75d3053aadf3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive and nullable, backfilled for nobody on purpose: a row already sitting in `claimed`
    # when this ships was stranded by the very crash MEMNG-35 fixes, and NULL is exactly how the
    # recovery sweep reads "no live worker behind this claim" — stamping it with now() would instead
    # grant those stranded rows a fresh lease and delay their recovery by the stale horizon.
    op.add_column('frame_share_jobs', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('frame_share_jobs', 'claimed_at')
