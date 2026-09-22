"""attachment detach instant: detached_at

Revision ID: a4e7c1b20f36
Revises: d3f81b06c9a4
Create Date: 2026-09-22 14:10:00.000000

Expand-only, one nullable column on ONE table, no backfill and no data rewrite.

`attachments.detached_at` is WHEN a row stopped belonging to a memo, written by the application at
the sites that null `memo_id`. Nothing on the table could say that before: `created_at` is the
public `createTime` (`app/routers/compat.py`) and the map-marker date fallback
(`app/services/map_markers.py`), so it cannot be repurposed — and creation age is the wrong age
anyway, because a file uploaded last month and detached ten seconds ago is a month old by it.

NULL therefore means "nothing detached this row": a standalone upload that was never linked, a
0.29.1 import, a row the FK's ON DELETE SET NULL nulled without the application, and every row
detached before this migration. That is why there is NO backfill — an instant invented for those
rows would be a lie about when their bytes stopped being wanted, and the honest answer is that
they are not candidates for anything that grants a grace period until something detaches them
again.

The column is NULLABLE, which is what makes the ROLLBACK of the release that carries this
migration an expand-preserving one: the PREVIOUS image's ORM does not mention it and runs against
this schema unchanged. Production therefore never downgrades — the downgrade below exists for the
rehearsal on a throwaway restore, where it proves the script is reversible, and it drops exactly
what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4e7c1b20f36"
down_revision: str | None = "d3f81b06c9a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("detached_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("attachments", "detached_at")
