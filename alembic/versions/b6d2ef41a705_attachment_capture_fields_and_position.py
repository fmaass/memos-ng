"""attachment capture fields + attachment order position; upload capture read-once state

Revision ID: b6d2ef41a705
Revises: a1c9f4b70e23
Create Date: 2026-09-05 23:40:00.000000

Additive in every column, with no backfill and no data rewrite (MEMNG-262):

* `attachments.capture_instant` / `capture_civil` / `capture_source` are the capture-only read of
  the file's own metadata. They are deliberately NOT `captured_at`, which keeps its fallback chain
  (capture_instant → client_mtime → upload time) and therefore always holds something; these three
  hold what the FILE said, and NULL when it said nothing. Nothing is backfilled: every existing
  attachment's original is on disk, but re-reading a hundred thousand files to fill a column no
  existing memo's date depends on would be a long write for no behaviour, and the code treats NULL
  exactly as "this file had no capture date" already.
* `attachments.position` is the memo's saved attachment order, 0-based in the client's declared
  order at finalize. NULL is the legacy value and is ordered LAST, so every memo written before
  this column keeps the id order it has always been displayed in; the first edit that adds a file
  to such a memo renumbers it in that same displayed order (services.memos.link_attachments), so
  the numbering never reorders what someone already saw.
* `uploads.capture_state` carries a server default of 'pending', which is what makes the column
  additive: an upload session in flight during the deploy gets 'pending' and its read happens
  lazily on the first inspection (services.uploads.ensure_capture), rather than being stuck
  unanswerable forever. Uploads are short-lived rows, so no backfill applies to them either.

The downgrade drops all eight columns. It loses the capture reads of any upload/attachment written
while the new code ran, which is the honest cost of the rollback: nothing else depends on them,
and the next upload reads its own file again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d2ef41a705"
down_revision: str | None = "a1c9f4b70e23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("capture_instant", sa.DateTime(timezone=True)))
    op.add_column("attachments", sa.Column("capture_civil", sa.String(length=32)))
    op.add_column("attachments", sa.Column("capture_source", sa.String(length=16)))
    op.add_column("attachments", sa.Column("position", sa.Integer()))
    op.add_column(
        "uploads",
        sa.Column(
            "capture_state",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column("uploads", sa.Column("capture_instant", sa.DateTime(timezone=True)))
    op.add_column("uploads", sa.Column("capture_civil", sa.String(length=32)))
    op.add_column("uploads", sa.Column("capture_source", sa.String(length=16)))


def downgrade() -> None:
    op.drop_column("uploads", "capture_source")
    op.drop_column("uploads", "capture_civil")
    op.drop_column("uploads", "capture_instant")
    op.drop_column("uploads", "capture_state")
    op.drop_column("attachments", "position")
    op.drop_column("attachments", "capture_source")
    op.drop_column("attachments", "capture_civil")
    op.drop_column("attachments", "capture_instant")
