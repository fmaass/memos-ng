"""upload owner_id

Revision ID: 75d3053aadf3
Revises: f156a444d37e
Create Date: 2026-08-13 12:55:48.145898

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '75d3053aadf3'
down_revision: str | None = 'f156a444d37e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FK = 'fk_uploads_owner_id_users'


def upgrade() -> None:
    # Nullable, and left NULL for existing rows on purpose: an in-flight session created before this
    # column has no verifiable creator, so it matches no owner and fails closed on every verb
    # (HEAD/append/finalize) rather than being claimable by whoever submits its uid. Those rows are
    # removed by the normal expiry reaper. No index: upload_uid is the PK and every ownership lookup
    # is PK-anchored, so an owner_id index would never be chosen.
    # SET NULL rather than CASCADE: a .part file is unlinked only by walking its uploads row, so
    # deleting the row on user deletion would strand those bytes on disk permanently. A NULLed owner
    # is the same fail-closed state as a pre-migration row and is reclaimed by the expiry reaper.
    op.add_column('uploads', sa.Column('owner_id', sa.Integer(), nullable=True))
    op.create_foreign_key(FK, 'uploads', 'users', ['owner_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(FK, 'uploads', type_='foreignkey')
    op.drop_column('uploads', 'owner_id')
