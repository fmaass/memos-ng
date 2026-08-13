"""media_jobs: drop attachment fk so deletes never wait on a transcode

The FK's ON DELETE CASCADE made `DELETE FROM attachments` lock every media_jobs row of that
attachment — including one a worker is running, whose row lock is held for the whole job (up to
SLOW_TRANSCODE_TIMEOUT_S for a transcode). Deleting a photo mid-transcode therefore pinned a
request-serving connection for as long as the transcode ran (MEMNG-41). The cascade was redundant
anyway: the delete endpoint removes the job rows explicitly (now with SKIP LOCKED), and the
worker discards the one in-flight row itself when it finds the attachment gone.

Revision ID: 3553a42d1b3b
Revises: 99505b747742
Create Date: 2026-08-13 19:11:22.331736

"""

import time
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from alembic import op

revision: str = "3553a42d1b3b"
down_revision: str | None = "99505b747742"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "media_jobs_attachment_id_fkey"
LOCK_NOT_AVAILABLE = "55P03"
# DROP CONSTRAINT needs ACCESS EXCLUSIVE on media_jobs, which an in-flight transcode's row lock
# holds off. Waiting unboundedly would be worse than failing: the lock REQUEST queues ahead of
# every later reader/writer, so an hour-long transcode would freeze uploads and the SSE snapshot
# behind it. Bounded attempts instead — the app container re-runs the migration on restart, and
# a deploy stops the old worker anyway, so a genuinely busy moment self-heals within a retry.
LOCK_TIMEOUT = "3s"
ATTEMPTS = 5
RETRY_SLEEP_SECONDS = 2.0


def _fk_name(conn: sa.Connection) -> str | None:
    """The actual constraint name (never assumed): it was created unnamed, so it carries whatever
    Postgres generated — and it may already be gone when this migration is re-run."""
    return conn.execute(
        sa.text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'media_jobs'::regclass "
            "AND contype = 'f' AND confrelid = 'attachments'::regclass"
        )
    ).scalar_one_or_none()


def upgrade() -> None:
    conn = op.get_bind()
    name = _fk_name(conn)
    if name is None:
        return
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # Nested = SAVEPOINT: a lock timeout must not abort the whole migration transaction,
            # or there would be nothing left to retry in.
            with conn.begin_nested():
                conn.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                conn.execute(sa.text(f'ALTER TABLE media_jobs DROP CONSTRAINT "{name}"'))
            return
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) != LOCK_NOT_AVAILABLE or attempt == ATTEMPTS:
                raise
            time.sleep(RETRY_SLEEP_SECONDS)


def downgrade() -> None:
    # Rows whose attachment is gone are expected without the FK (that is the point of dropping
    # it) and would make ADD CONSTRAINT fail — they are queue rows for work nobody wants, so
    # purge them rather than block the downgrade.
    op.execute(
        "DELETE FROM media_jobs m "
        "WHERE NOT EXISTS (SELECT 1 FROM attachments a WHERE a.id = m.attachment_id)"
    )
    op.create_foreign_key(
        FK_NAME, "media_jobs", "attachments", ["attachment_id"], ["id"], ondelete="CASCADE"
    )
