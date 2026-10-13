"""attachment sibling outcome

Revision ID: ad7a00effd6d
Revises: d6c6f8683f25
Create Date: 2026-10-13 09:05:03.048045

Expand-only (MEMNG-796): one NEW nullable column, no default, no backfill, no data rewrite.
`attachments.sibling_outcome` records what the img-manager stem lookup concluded for a row
('unique' | 'ambiguous' | 'none'); NULL means never asked, which is every existing row. No CHECK:
like `capture_source` and `location_source` it is a free string so a later outcome costs no
migration.

The previous image's ORM does not name the column, so it runs against this schema unchanged;
production never downgrades. The downgrade exists for the rehearsal on a throwaway restore and drops
exactly what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ad7a00effd6d"
down_revision: str | None = "d6c6f8683f25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("sibling_outcome", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("attachments", "sibling_outcome")
