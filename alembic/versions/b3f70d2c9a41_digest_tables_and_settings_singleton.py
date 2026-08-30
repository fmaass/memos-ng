"""digest tables + the digest_settings singleton

Revision ID: b3f70d2c9a41
Revises: e4a91c07b5d2
Create Date: 2026-08-30 16:20:54.925656

Expand-only, in every half (MEMNG-233):

* All five tables are BRAND NEW and nothing existing references them, so a process running the old
  code alongside the migrated schema is unaffected and the rollback is a plain drop. Nothing is
  dropped, renamed or retyped here; `memos` and `attachments` are touched only as FK targets.
* LSN-0024 ("a NOT NULL added to a populated table needs a default that IS the backfill") therefore
  binds only through the ORM's own inserts — but the defaults are written anyway, because they are
  what makes the seed row below correct and what gives a column added by a LATER expand-only
  migration a defined value for the one row that already exists.
* `digest_deliveries.run_id` deliberately carries NO server default. It is a required FK, and a
  default would let an insert that forgot it either fail obscurely on the FK or, worse, attach the
  delivery to an arbitrary run and destroy exactly the audit relationship the column exists for.
  An omitted `run_id` must fail loudly at the NOT NULL.
* `digest_runs.status` is NULLABLE on purpose: NULL is "in flight", and the terminal value is
  written exactly once (a DB-derived Counter cannot move backwards). The two unique partial indexes
  over `digest_runs` are the single-flight primitive — one scheduled run per local day, and
  `UNIQUE ((1)) WHERE status IS NULL` for at most one in-flight run of ANY trigger; a third one
  gives the one-shot import its single row.
* `digest_settings.max_video_attach_bytes` is deliberately ABSENT (user decision, 2026-08-30:
  poster + Dropbox link only — nothing ever attaches an mp4).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b3f70d2c9a41'
down_revision: str | None = 'e4a91c07b5d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('digest_recipients',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('added_by', sa.String(length=255), nullable=True),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_digest_recipients_email'), 'digest_recipients', ['email'], unique=True)
    op.create_table('digest_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('memos_sent', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('parts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('parts_sent', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('parts_claimed', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('recipients', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('local_date', sa.Date(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_digest_runs_import', 'digest_runs', ['trigger'], unique=True, postgresql_where=sa.text("trigger = 'import'"))
    op.create_index('uq_digest_runs_scheduled_local_date', 'digest_runs', ['local_date'], unique=True, postgresql_where=sa.text("trigger = 'scheduled'"))
    op.create_index('uq_digest_runs_single_inflight', 'digest_runs', [sa.literal_column('(1)')], unique=True, postgresql_where=sa.text('status IS NULL'))
    op.create_table('digest_settings',
    sa.Column('id', sa.Integer(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('send_time', sa.Time(), server_default=sa.text("'07:00'"), nullable=False),
    sa.Column('timezone', sa.String(length=64), server_default='Europe/Zurich', nullable=False),
    sa.Column('visibility_scope', sa.String(length=32), server_default='PUBLIC_PROTECTED', nullable=False),
    sa.Column('start_date', sa.Date(), server_default=sa.text("'2026-06-01'"), nullable=False),
    sa.Column('grace_minutes', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('max_email_bytes', sa.Integer(), server_default=sa.text('18874368'), nullable=False),
    sa.Column('max_videos_per_memo', sa.Integer(), server_default=sa.text('2'), nullable=False),
    sa.Column('max_memos_per_run', sa.Integer(), server_default=sa.text('40'), nullable=False),
    sa.Column('mail_title', sa.String(length=255), server_default='The Daily Dami Memos Mail', nullable=False),
    sa.Column('from_name', sa.String(length=255), server_default='The Daily Dami Memos Mail', nullable=False),
    sa.Column('from_email', sa.String(length=320), server_default='', nullable=False),
    sa.Column('hosting_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('dropbox_folder', sa.String(length=255), server_default='/memos-mail', nullable=False),
    sa.Column('retention_days', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('link_images_full', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.String(length=255), nullable=True),
    sa.CheckConstraint('id = 1', name='ck_digest_settings_singleton'),
    sa.PrimaryKeyConstraint('id')
    )
    # The singleton, seeded with the MIGRATION's own column defaults and nothing else: naming only
    # `id` is what makes "the seeded row IS the defaults" true by construction rather than by two
    # lists agreeing. The satellite's REAL configuration arrives later, through the import (P5-1) —
    # seeding it here would arm a digest nobody had reviewed. `CHECK (id = 1)` plus a router with no
    # INSERT path is what keeps this the only row there can ever be.
    op.execute(sa.text("INSERT INTO digest_settings (id) VALUES (1)"))


    op.create_table('digest_deliveries',
    sa.Column('memo_id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('part_index', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('incomplete', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['memo_id'], ['memos.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['digest_runs.id'], ),
    sa.PrimaryKeyConstraint('memo_id')
    )
    op.create_index(op.f('ix_digest_deliveries_run_id'), 'digest_deliveries', ['run_id'], unique=False)
    op.create_table('digest_hosted_media',
    sa.Column('attachment_id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('dropbox_path', sa.Text(), nullable=False),
    sa.Column('shared_link', sa.Text(), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('bytes', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('swept_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['attachment_id'], ['attachments.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('attachment_id')
    )


def downgrade() -> None:
    op.drop_table('digest_hosted_media')
    op.drop_index(op.f('ix_digest_deliveries_run_id'), table_name='digest_deliveries')
    op.drop_table('digest_deliveries')
    op.drop_table('digest_settings')
    op.drop_index('uq_digest_runs_single_inflight', table_name='digest_runs', postgresql_where=sa.text('status IS NULL'))
    op.drop_index('uq_digest_runs_scheduled_local_date', table_name='digest_runs', postgresql_where=sa.text("trigger = 'scheduled'"))
    op.drop_index('uq_digest_runs_import', table_name='digest_runs', postgresql_where=sa.text("trigger = 'import'"))
    op.drop_table('digest_runs')
    op.drop_index(op.f('ix_digest_recipients_email'), table_name='digest_recipients')
    op.drop_table('digest_recipients')
