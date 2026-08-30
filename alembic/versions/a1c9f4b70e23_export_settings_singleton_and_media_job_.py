"""export_settings singleton + media_jobs.profile_key

Revision ID: a1c9f4b70e23
Revises: c7a4e91b02df
Create Date: 2026-08-30 21:40:00.000000

Expand-only, in both halves (MEMNG-238):

* `export_settings` is a BRAND NEW table nothing existing references, so a process running the old
  code alongside the migrated schema is unaffected and the rollback is a plain drop. It is seeded
  the way `digest_settings` is — naming only `id`, so "the seeded row IS the migration's defaults"
  is true by construction rather than by two lists agreeing.
* The four seeded `print_*` defaults are exactly what the shipped printready tier (MEMNG-49) was
  built with, so the seeded row resolves to the profile key `print-4096` and the renditions already
  on disk stay valid at the path they already occupy. A migration that seeded anything else would
  invalidate the whole print cache on the first request after deploy.
* `media_jobs.profile_key` is NULLABLE and carries NO server default, deliberately. NULL means the
  default profile: every printready job row written before this column existed produced the shipped
  tier, and a server default would be indistinguishable from a value a caller actually chose. The
  column is also meaningless for the upload pipeline (`kind = derive`), which has no profile at all
  — LSN-0024's "a NOT NULL needs a default that IS the backfill" is answered by not making it NOT
  NULL. Not indexed: every lookup on it is already scoped by attachment_id (indexed), and
  media_jobs is a write-heavy queue where an index nothing selects on alone is pure insert cost.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c9f4b70e23'
down_revision: str | None = 'c7a4e91b02df'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('export_settings',
    sa.Column('id', sa.Integer(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('print_long_edge', sa.Integer(), server_default=sa.text('4096'), nullable=False),
    sa.Column('print_jpeg_quality', sa.Integer(), server_default=sa.text('92'), nullable=False),
    sa.Column('print_metadata_policy', sa.String(length=16), server_default='strip', nullable=False),
    sa.Column('print_grayscale_single_channel', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('mail_long_edge', sa.Integer(), server_default=sa.text('1600'), nullable=False),
    sa.Column('mail_jpeg_quality', sa.Integer(), server_default=sa.text('80'), nullable=False),
    sa.Column('mail_subsampling', sa.String(length=8), server_default='4:2:0', nullable=False),
    sa.Column('mail_progressive', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.String(length=255), nullable=True),
    sa.CheckConstraint('id = 1', name='ck_export_settings_singleton'),
    sa.PrimaryKeyConstraint('id')
    )
    # The singleton, seeded with the MIGRATION's own column defaults and nothing else. `CHECK
    # (id = 1)` plus a router with no INSERT path (app/routers/admin_export.py) is what keeps this
    # the only row there can ever be.
    op.execute(sa.text("INSERT INTO export_settings (id) VALUES (1)"))
    op.add_column('media_jobs', sa.Column('profile_key', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('media_jobs', 'profile_key')
    op.drop_table('export_settings')
