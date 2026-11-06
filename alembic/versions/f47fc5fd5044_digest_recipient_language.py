"""digest recipient language, per-language delivery ledger, run groups

Revision ID: f47fc5fd5044
Revises: f8af654fa068
Create Date: 2026-11-06 20:00:00

Expand, with one add-only backfill (MEMNG-1245). Every recipient gets a language and a run sends
one mail per language group, so the delivery registry has to say WHICH group got a memo:

* `digest_recipients.language` — NOT NULL, server default 'de', checked to de|es|en. Every
  existing recipient reads 'de' from the default the moment the column exists.
* `digest_runs.groups_attempted` / `groups_accepted` — text arrays, default '{}'.
* `digest_deliveries.language` — added nullable, BACKFILLED, then NOT NULL and checked. The
  backfill writes 'de': the single language every recipient holds after the first step, so every
  memo already mailed stays mailed for the one group that exists today and nothing is re-sent.
  The column keeps no server default afterwards, like `run_id`: an insert that forgot to name its
  group must fail, not silently close the memo for 'de'.
* The primary key moves from `(memo_id)` to `(memo_id, language)`, under the same name.

No existing value is rewritten and nothing is dropped except the one-column key the new key
replaces. The downgrade is lossy by necessity — one row per memo cannot hold two languages — and
keeps the 'de' row (else the oldest) of a memo mailed to several groups.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f47fc5fd5044"
down_revision: str | None = "f8af654fa068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Today's single recipient language: what the server default below gives every existing recipient.
BACKFILL_LANGUAGE = "de"
LANGUAGE_CHECK = "language IN ('de', 'es', 'en')"


def upgrade() -> None:
    op.add_column(
        "digest_recipients",
        sa.Column("language", sa.String(length=2), server_default=sa.text("'de'"), nullable=False),
    )
    op.create_check_constraint("ck_digest_recipients_language", "digest_recipients", LANGUAGE_CHECK)

    for column in ("groups_attempted", "groups_accepted"):
        op.add_column(
            "digest_runs",
            sa.Column(
                column,
                postgresql.ARRAY(sa.String(length=2)),
                server_default=sa.text("'{}'"),
                nullable=False,
            ),
        )

    op.add_column("digest_deliveries", sa.Column("language", sa.String(length=2), nullable=True))
    op.execute(
        sa.text(
            "UPDATE digest_deliveries SET language = :language WHERE language IS NULL"
        ).bindparams(language=BACKFILL_LANGUAGE)
    )
    op.alter_column("digest_deliveries", "language", nullable=False)
    op.create_check_constraint("ck_digest_deliveries_language", "digest_deliveries", LANGUAGE_CHECK)
    op.drop_constraint("digest_deliveries_pkey", "digest_deliveries", type_="primary")
    op.create_primary_key("digest_deliveries_pkey", "digest_deliveries", ["memo_id", "language"])


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM digest_deliveries d USING digest_deliveries keep "
            "WHERE d.memo_id = keep.memo_id AND d.language <> keep.language "
            "AND (keep.language = 'de' OR (d.language <> 'de' AND "
            "(keep.sent_at, keep.language) < (d.sent_at, d.language)))"
        )
    )
    op.drop_constraint("digest_deliveries_pkey", "digest_deliveries", type_="primary")
    op.create_primary_key("digest_deliveries_pkey", "digest_deliveries", ["memo_id"])
    op.drop_constraint("ck_digest_deliveries_language", "digest_deliveries", type_="check")
    op.drop_column("digest_deliveries", "language")
    op.drop_column("digest_runs", "groups_accepted")
    op.drop_column("digest_runs", "groups_attempted")
    op.drop_constraint("ck_digest_recipients_language", "digest_recipients", type_="check")
    op.drop_column("digest_recipients", "language")
