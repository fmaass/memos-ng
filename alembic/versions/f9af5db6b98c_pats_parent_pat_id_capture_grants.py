"""pats.parent_pat_id: upload-only capture grants

Revision ID: f9af5db6b98c
Revises: c3e8b5a1d2f4
Create Date: 2026-10-30 11:05:35.102450

Expand-only (MEMNG-938). One nullable column, its unique index and its self-referencing foreign
key; nothing existing is dropped, renamed or retyped, and every existing row reads NULL, which is
what an ordinary token is. A process running the old code alongside the migrated schema never
names the column, and it cannot authenticate a grant row either: a grant is stored under a
domain-separated hash (`auth.hash_capture_grant`), so the old `sha256(token)` lookup matches
nothing.

The downgrade deletes the grant rows before it drops the column. They are ephemeral by
construction (24 h, re-minted on every app foreground), and left behind they would show up as
unnamed tokens in Manage Tokens that no client can present.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f9af5db6b98c'
down_revision: str | None = 'c3e8b5a1d2f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('pats', sa.Column('parent_pat_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_pats_parent_pat_id'), 'pats', ['parent_pat_id'], unique=True)
    op.create_foreign_key(
        'pats_parent_pat_id_fkey', 'pats', 'pats', ['parent_pat_id'], ['id'], ondelete='CASCADE'
    )


def downgrade() -> None:
    op.execute('DELETE FROM pats WHERE parent_pat_id IS NOT NULL')
    op.drop_constraint('pats_parent_pat_id_fkey', 'pats', type_='foreignkey')
    op.drop_index(op.f('ix_pats_parent_pat_id'), table_name='pats')
    op.drop_column('pats', 'parent_pat_id')
