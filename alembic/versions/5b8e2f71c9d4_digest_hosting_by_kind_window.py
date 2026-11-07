"""digest hosting by kind, the seven-day window, daily pushes and the sync outbox

Revision ID: 5b8e2f71c9d4
Revises: 0c5e1d2a7b46
Create Date: 2026-11-07 12:00:00

Expand only (MEMNG-1244).

* `digest_hosted_media.expires_at` (nullable) — what the retention sweep now deletes by. Existing
  `uploaded` rows are backfilled with `uploaded_at + retention_days`, the exact moment the
  previous image would have swept them, so nothing on Dropbox lives a day shorter or longer for
  the switch. `pending` rows stay NULL: the reconciliation stamps them when it settles them.
* `ix_digest_hosted_media_attachment_kind` (NOT unique). The ledger's identity becomes
  (attachment_id, kind), but live data may already hold several rows of one kind for one
  attachment — swept and re-hosted, or a stale `pending` reconciled after a newer upload of the
  same file — so a unique key would fail this migration on production data and would make the
  previous image's reconciliation UPDATE raise. The identity is enforced by the lookup (newest
  `uploaded` row per key) and by the upload pass's per-kind skip.
* `daily_pushes` — one row per published local day; `published_at` is the door's, kept from the
  FIRST publication.
* `daily_sync_outbox` — what home owes the door; this revision's code only enqueues
  `day_delete`, the drain is MEMNG-1247.

The previous image never reads any of it: it writes NULL `expires_at` and keeps sweeping by
`uploaded_at`, so a rollback needs nothing. The downgrade drops exactly what this added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5b8e2f71c9d4"
down_revision: str | None = "0c5e1d2a7b46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "digest_hosted_media",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE digest_hosted_media SET expires_at = uploaded_at + make_interval(hours => 24 * "
        "COALESCE((SELECT retention_days FROM digest_settings WHERE id = 1), 30)) "
        "WHERE status = 'uploaded' AND uploaded_at IS NOT NULL AND expires_at IS NULL"
    )
    op.create_index(
        "ix_digest_hosted_media_attachment_kind",
        "digest_hosted_media",
        ["attachment_id", "kind"],
        unique=False,
    )
    op.create_table(
        "daily_pushes",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_table(
        "daily_sync_outbox",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('recipients', 'revocation', 'day_delete')",
            name="ck_daily_sync_outbox_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_daily_sync_outbox_unacked",
        "daily_sync_outbox",
        ["id"],
        unique=False,
        postgresql_where=sa.text("acked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_daily_sync_outbox_unacked", table_name="daily_sync_outbox")
    op.drop_table("daily_sync_outbox")
    op.drop_table("daily_pushes")
    op.drop_index("ix_digest_hosted_media_attachment_kind", table_name="digest_hosted_media")
    op.drop_column("digest_hosted_media", "expires_at")
