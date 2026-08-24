"""memo edit keys

Revision ID: 8b7d391cbf19
Revises: a7ed8c1a028c
Create Date: 2026-08-24 16:01:37.512044

Expand-only (MEMNG-124): ONE new table and nothing else. No existing column is added, rewritten or
back-filled, so an app process running the pre-migration code alongside this schema simply never
writes it, and the rollback is a plain drop of a table only the new code touches.

The lock_timeout guard is not ceremonial even for a CREATE TABLE: the two FOREIGN KEY constraints
take a SHARE ROW EXCLUSIVE lock on `memos` and `users` while they are added, which would queue
behind a long-running transaction on either — and this runs against the live household database at
app start. Five seconds and then fail is the right answer there: the deploy stops loudly instead of
holding the memo table hostage. Same guard, same reasoning, as a7ed8c1a028c.

`uq_memo_edit_keys_owner_key` is the feature's concurrency settlement, not merely a data-integrity
constraint: two patches racing with one key cannot both insert, so the loser reads the winner back
rather than applying the edit a second time.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8b7d391cbf19'
down_revision: str | None = 'a7ed8c1a028c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        'memo_edit_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('memo_id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('edit_key', sa.String(length=64), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['memo_id'], ['memos.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'edit_key', name='uq_memo_edit_keys_owner_key'),
    )
    # memo_id only: the unique constraint above is a btree LEADING with owner_id, so it already
    # serves the replay lookup and the ON DELETE CASCADE from users. memo_id has no such cover, and
    # deleting a memo has to find its rows by it.
    op.create_index(op.f('ix_memo_edit_keys_memo_id'), 'memo_edit_keys', ['memo_id'], unique=False)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(op.f('ix_memo_edit_keys_memo_id'), table_name='memo_edit_keys')
    op.drop_table('memo_edit_keys')
