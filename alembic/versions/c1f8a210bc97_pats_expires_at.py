"""pats expires_at

Revision ID: c1f8a210bc97
Revises: 8b7d6acb2567
Create Date: 2026-08-23 06:59:39.691082

Expand-only and deliberately un-backfilled: NULL is the correct value for every PAT minted before
device tokens existed — a token that never expires — and app.auth.authenticate_pat reads NULL as
exactly that. No default, so a not-yet-restarted process inserting a row during a rolling deploy
also lands on NULL.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c1f8a210bc97'
down_revision: str | None = '8b7d6acb2567'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('pats', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('pats', 'expires_at')
