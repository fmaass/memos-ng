"""memo idempotency key

Revision ID: a7ed8c1a028c
Revises: d26180abb26d
Create Date: 2026-08-24 12:27:20.685175

Expand-only: one nullable column plus a PARTIAL unique index over it. Nothing is back-filled and
nothing is rewritten — every existing memo keeps a NULL key, which the index's WHERE clause leaves
out entirely, so the index only ever holds keys clients actually sent. An app process running the
pre-migration code alongside this schema simply never writes the column, and the rollback is a
plain drop of both.

The index is built normally rather than CONCURRENTLY: it must be created inside the migration's
transaction (CONCURRENTLY cannot be), and on a household-sized memos table the write lock is
momentary. It is also the concurrency settlement for the feature — two racing creates carrying one
key cannot both insert, so the loser reads the winner back.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a7ed8c1a028c'
down_revision: str | None = 'd26180abb26d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column('memos', sa.Column('idempotency_key', sa.String(length=64), nullable=True))
    op.create_index(
        'uq_memos_owner_idempotency_key',
        'memos',
        ['owner_id', 'idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(
        'uq_memos_owner_idempotency_key',
        table_name='memos',
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )
    op.drop_column('memos', 'idempotency_key')
