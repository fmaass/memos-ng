"""digest recipient language, per-language-group delivery ledger, run groups

Revision ID: f47fc5fd5044
Revises: f8af654fa068
Create Date: 2026-11-06 20:00:00

Expand only (MEMNG-1245). Every recipient gets a language and a run sends one mail per language
group, so the run needs a ledger that says WHICH group got a memo:

* `digest_recipients.language` — NOT NULL, server default 'de', checked to de|es|en. Every
  existing recipient reads 'de' from the default the moment the column exists.
* `digest_runs.groups_attempted` / `groups_accepted` — text arrays, default '{}'.
* NEW `digest_group_deliveries`, keyed `(memo_id, language)`, BACKFILLED with one 'de' row per
  existing `digest_deliveries` row: 'de' is the single language every recipient holds after the
  first step, so nothing already mailed is mailed again.

`digest_deliveries` is NOT touched — same columns, same `(memo_id)` key — so the previous image
keeps reading and writing it unchanged after a rollback, and the run keeps writing it beside the
new ledger. The downgrade drops what this revision added and nothing else.
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

    op.create_table(
        "digest_group_deliveries",
        sa.Column("memo_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("part_index", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("incomplete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(LANGUAGE_CHECK, name="ck_digest_group_deliveries_language"),
        sa.ForeignKeyConstraint(["memo_id"], ["memos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["digest_runs.id"]),
        sa.PrimaryKeyConstraint("memo_id", "language"),
    )
    op.create_index(
        op.f("ix_digest_group_deliveries_run_id"), "digest_group_deliveries", ["run_id"]
    )
    op.execute(
        sa.text(
            "INSERT INTO digest_group_deliveries "
            "(memo_id, language, run_id, sent_at, part_index, incomplete) "
            "SELECT memo_id, :language, run_id, sent_at, part_index, incomplete "
            "FROM digest_deliveries"
        ).bindparams(language=BACKFILL_LANGUAGE)
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_digest_group_deliveries_run_id"), table_name="digest_group_deliveries")
    op.drop_table("digest_group_deliveries")
    op.drop_column("digest_runs", "groups_accepted")
    op.drop_column("digest_runs", "groups_attempted")
    op.drop_constraint("ck_digest_recipients_language", "digest_recipients", type_="check")
    op.drop_column("digest_recipients", "language")
