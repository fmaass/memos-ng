"""native_auth_codes

Revision ID: d26180abb26d
Revises: c1f8a210bc97
Create Date: 2026-08-23 10:12:00.000000

Expand-only: a brand-new table nothing else references, so an old process running alongside the
migrated schema is unaffected and a rollback is a plain drop. Rows are ephemeral by construction
(120 s TTL, swept an hour past expiry), so there is nothing to back-fill and nothing to preserve.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd26180abb26d'
down_revision: str | None = 'c1f8a210bc97'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'native_auth_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=128), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('code_challenge', sa.String(length=128), nullable=False),
        sa.Column('device_name', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # UNIQUE as well as indexed: the exchange looks a code up by this column on every attempt, and
    # uniqueness is what makes "the row" unambiguous for the single-statement consume.
    op.create_index(
        op.f('ix_native_auth_codes_code_hash'), 'native_auth_codes', ['code_hash'], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_native_auth_codes_code_hash'), table_name='native_auth_codes')
    op.drop_table('native_auth_codes')
