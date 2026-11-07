"""digest delivery mode

Revision ID: 0c5e1d2a7b46
Revises: f47fc5fd5044
Create Date: 2026-11-07 07:00:00

Expand only (MEMNG-1246). `digest_settings.delivery_mode` says what the daily mail carries:
`attachments` (the self-contained mail, today's) or `link` (the small mail pointing at the day's
page). NOT NULL with server default 'attachments', checked to the two values, so the existing row
reads 'attachments' the moment the column exists and keeps sending exactly what it sent before.
The previous image never reads the column, so a rollback needs nothing; the downgrade drops the
column and its check and nothing else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0c5e1d2a7b46"
down_revision: str | None = "f47fc5fd5044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELIVERY_MODE_CHECK = "delivery_mode IN ('attachments', 'link')"


def upgrade() -> None:
    op.add_column(
        "digest_settings",
        sa.Column(
            "delivery_mode",
            sa.String(length=16),
            server_default="attachments",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_digest_settings_delivery_mode", "digest_settings", DELIVERY_MODE_CHECK
    )


def downgrade() -> None:
    op.drop_constraint("ck_digest_settings_delivery_mode", "digest_settings", type_="check")
    op.drop_column("digest_settings", "delivery_mode")
