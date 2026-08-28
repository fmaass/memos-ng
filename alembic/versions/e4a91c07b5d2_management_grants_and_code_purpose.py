"""management_grants + native_auth_codes.purpose

Revision ID: e4a91c07b5d2
Revises: 8b7d391cbf19
Create Date: 2026-08-28 07:10:00.000000

Expand-only, in both halves (MEMNG-138 option 2):

* `management_grants` is a brand-new table nothing else references, so an old process running
  alongside the migrated schema is unaffected and a rollback is a plain drop. Its rows are
  ephemeral by construction (a 600 s TTL, swept at the next mint), so there is nothing to preserve.
* `native_auth_codes.purpose` is added NOT NULL with a server default of 'login', which is what
  every code minted before this column existed was — so the default IS the backfill, and a process
  running the old code (which does not name the column) keeps inserting valid rows.

Nothing existing is dropped, renamed or retyped; `pats` is untouched.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e4a91c07b5d2'
down_revision: str | None = '8b7d391cbf19'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'native_auth_codes',
        sa.Column(
            'purpose', sa.String(length=16), server_default='login', nullable=False
        ),
    )
    op.create_table(
        'management_grants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('hashed_grant', sa.String(length=128), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # UNIQUE as well as indexed, for the reason ix_native_auth_codes_code_hash is: every
    # authentication looks a grant up by this column, and uniqueness is what makes "the row"
    # unambiguous.
    op.create_index(
        op.f('ix_management_grants_hashed_grant'),
        'management_grants',
        ['hashed_grant'],
        unique=True,
    )
    op.create_index(
        op.f('ix_management_grants_user_id'), 'management_grants', ['user_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_management_grants_user_id'), table_name='management_grants')
    op.drop_index(op.f('ix_management_grants_hashed_grant'), table_name='management_grants')
    op.drop_table('management_grants')
    op.drop_column('native_auth_codes', 'purpose')
