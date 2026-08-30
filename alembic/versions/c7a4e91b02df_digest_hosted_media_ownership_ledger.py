"""digest_hosted_media becomes a durable ownership ledger for the Dropbox objects it names

Revision ID: c7a4e91b02df
Revises: 8be790506cff
Create Date: 2026-08-30 19:40:00.000000

The table's first shape keyed a row on `attachment_id` and cascaded with the attachment, and both
halves of that lose the only locator of a file that still exists on Dropbox:

* a run recorded its uploads in ONE batch after the whole hosting pass, so a crash between Dropbox
  accepting a file and that write left an object no row named and no sweep could ever find;
* reclaiming the attachment CASCADEd the row away, while the retention sweep discovers remote
  objects exclusively through surviving rows — so the file became permanently unowned.

Dropbox cannot participate in a database cascade. This migration makes the row a tombstone that
outlives both the process and the attachment:

* `id` is a surrogate primary key (one attachment can be hosted more than once — a later run
  writes a new run folder and therefore a new path — and each remote object needs its own row);
* `attachment_id` is NULLABLE with `ON DELETE SET NULL`, so reclamation empties the reference and
  keeps the locator;
* `dropbox_path` is UNIQUE: it is the natural key of the OBJECT, which is what lets the intention
  be committed before the upload and re-committed idempotently on a retry;
* `status` spells the lifecycle the code now writes — `pending` before the upload, `uploaded`
  straight after it, `swept` only once the remote delete has actually succeeded — under a CHECK
  that no other value can enter;
* `created_at` is when the INTENTION was committed, which is the only age a `pending` row has: the
  reconciliation sweep measures a stuck pending row from it, while retention keeps measuring from
  `uploaded_at`.

**The premise of the rekey, stated because a re-key is not ordinarily an expand-only change.**
`digest_hosted_media` is EMPTY in production: the feature that writes it has never run there, and
the two migrations that created it shipped in the same undeployed series as this one. Deploys of
this stack are single-host and atomic (`docker compose up -d --force-recreate` replaces app and
worker together, and the worker waits for the migrated schema before its loop starts), so old and
new code never run against this table concurrently. Those two facts are what make dropping and
re-adding the primary key safe here; neither would hold for a populated table under a rolling
deploy, and this migration must not be used as a precedent for one.

Expand-and-backfill all the same, so the statements are correct for any rows that DO exist. Rows
written by the old code were only ever inserted AFTER a successful upload, so they are `uploaded` —
or `swept` where the retention sweep had already stamped `swept_at`. `created_at` is backfilled
from `uploaded_at`, which is the closest true statement about a row whose intention was never
separately recorded.

The downgrade REFUSES rather than being lossy where loss would matter. The old shape cannot hold a
row whose attachment is gone, and such a row is an ownership tombstone: the only record that a file
exists on Dropbox. Deleting it to fit the old primary key would strand that file for ever, silently,
during a rollback — so the downgrade raises instead and names what has to be dealt with first. Rows
that merely duplicate an attachment (a second remote object for one attachment) ARE collapsed to the
newest, because the old shape had no way to express the others and they are recoverable from the
run history. Nothing outside this table references it, so that is contained.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c7a4e91b02df'
down_revision: str | None = '8be790506cff'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SERIAL in one statement: Postgres creates `digest_hosted_media_id_seq` OWNED BY the column,
    # defaults it to nextval and populates every existing row — which is the shape alembic's
    # autogenerate recognises as an Integer identity column and omits from a diff.
    op.execute(sa.text("ALTER TABLE digest_hosted_media ADD COLUMN id SERIAL"))
    op.drop_constraint('digest_hosted_media_pkey', 'digest_hosted_media', type_='primary')
    op.create_primary_key('digest_hosted_media_pkey', 'digest_hosted_media', ['id'])

    op.drop_constraint(
        'digest_hosted_media_attachment_id_fkey', 'digest_hosted_media', type_='foreignkey'
    )
    op.alter_column('digest_hosted_media', 'attachment_id', existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(
        'digest_hosted_media_attachment_id_fkey',
        'digest_hosted_media',
        'attachments',
        ['attachment_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        op.f('ix_digest_hosted_media_attachment_id'),
        'digest_hosted_media',
        ['attachment_id'],
        unique=False,
    )

    # WHICH run committed each intention. The reconciliation sweep judges a `pending` row by its
    # owning run's LIVENESS rather than by elapsed time — a legitimate upload-session operation
    # allows 600 s and a whole run can last hours, so an age-only rule reconciles live uploads out
    # from under themselves. Nullable, and `ON DELETE SET NULL` like `attachment_id`: the
    # tombstone outlives everything it points at.
    op.add_column(
        'digest_hosted_media', sa.Column('run_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'digest_hosted_media_run_id_fkey',
        'digest_hosted_media',
        'digest_runs',
        ['run_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        op.f('ix_digest_hosted_media_run_id'), 'digest_hosted_media', ['run_id'], unique=False
    )
    op.create_index(
        op.f('ix_digest_hosted_media_dropbox_path'),
        'digest_hosted_media',
        ['dropbox_path'],
        unique=True,
    )

    # The default IS the backfill for a brand-new lifecycle column (LSN-0024), and then the rows
    # that predate it are corrected to what they actually are: every one of them was written after
    # a successful upload.
    op.add_column(
        'digest_hosted_media',
        sa.Column('status', sa.String(length=16), server_default='pending', nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE digest_hosted_media "
            "SET status = CASE WHEN swept_at IS NULL THEN 'uploaded' ELSE 'swept' END"
        )
    )
    op.alter_column(
        'digest_hosted_media', 'status', existing_type=sa.String(length=16), nullable=False
    )
    op.create_check_constraint(
        'ck_digest_hosted_media_status',
        'digest_hosted_media',
        "status IN ('pending', 'uploaded', 'swept')",
    )

    op.add_column(
        'digest_hosted_media', sa.Column('created_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(sa.text("UPDATE digest_hosted_media SET created_at = uploaded_at"))
    op.alter_column(
        'digest_hosted_media',
        'created_at',
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text('now()'),
        nullable=False,
    )

    # `uploaded_at` and `shared_link` are what an `uploaded` row PROMISES, so a pending row must be
    # able to hold neither. The server default goes with the NOT NULL: a row that defaulted its
    # upload stamp to now() would claim an upload that has not happened.
    op.alter_column(
        'digest_hosted_media',
        'uploaded_at',
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
        nullable=True,
    )
    op.alter_column(
        'digest_hosted_media', 'shared_link', existing_type=sa.Text(), nullable=True
    )


def downgrade() -> None:
    # REFUSE before touching anything. A row with no attachment is an ownership tombstone — the
    # only record that a file exists on Dropbox — and the old shape has nowhere to put it. Deleting
    # it to make the old primary key fit would strand that file permanently, during a rollback,
    # with nothing to notice it. The rollback stops here and says what to do instead.
    orphans = op.get_bind().execute(
        sa.text("SELECT count(*) FROM digest_hosted_media WHERE attachment_id IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"refusing to downgrade: {orphans} digest_hosted_media row(s) have no attachment. "
            "Each one is the ONLY record of a file on Dropbox, and the pre-ledger shape cannot "
            "hold it — this rollback would delete the file's sole locator. Sweep them first "
            "(retention deletes the remote object and stamps the row), or export them, then "
            "delete the rows deliberately and re-run the downgrade."
        )
    op.drop_index(op.f('ix_digest_hosted_media_run_id'), table_name='digest_hosted_media')
    op.drop_constraint(
        'digest_hosted_media_run_id_fkey', 'digest_hosted_media', type_='foreignkey'
    )
    op.drop_column('digest_hosted_media', 'run_id')
    # A second remote object for ONE attachment has no expression in the old shape either, but it
    # is recoverable from the run history rather than being a sole locator, so the newest wins.
    op.execute(
        sa.text(
            "DELETE FROM digest_hosted_media WHERE id NOT IN ("
            "  SELECT max(id) FROM digest_hosted_media GROUP BY attachment_id"
            ")"
        )
    )
    op.execute(sa.text("UPDATE digest_hosted_media SET shared_link = '' WHERE shared_link IS NULL"))
    op.execute(
        sa.text("UPDATE digest_hosted_media SET uploaded_at = created_at WHERE uploaded_at IS NULL")
    )
    op.alter_column(
        'digest_hosted_media', 'shared_link', existing_type=sa.Text(), nullable=False
    )
    op.alter_column(
        'digest_hosted_media',
        'uploaded_at',
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text('now()'),
        nullable=False,
    )
    op.drop_column('digest_hosted_media', 'created_at')
    op.drop_constraint(
        'ck_digest_hosted_media_status', 'digest_hosted_media', type_='check'
    )
    op.drop_column('digest_hosted_media', 'status')
    op.drop_index(op.f('ix_digest_hosted_media_dropbox_path'), table_name='digest_hosted_media')
    op.drop_index(op.f('ix_digest_hosted_media_attachment_id'), table_name='digest_hosted_media')
    op.drop_constraint(
        'digest_hosted_media_attachment_id_fkey', 'digest_hosted_media', type_='foreignkey'
    )
    op.alter_column(
        'digest_hosted_media', 'attachment_id', existing_type=sa.Integer(), nullable=False
    )
    op.create_foreign_key(
        'digest_hosted_media_attachment_id_fkey',
        'digest_hosted_media',
        'attachments',
        ['attachment_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.drop_constraint('digest_hosted_media_pkey', 'digest_hosted_media', type_='primary')
    op.drop_column('digest_hosted_media', 'id')
    op.create_primary_key(
        'digest_hosted_media_pkey', 'digest_hosted_media', ['attachment_id']
    )
