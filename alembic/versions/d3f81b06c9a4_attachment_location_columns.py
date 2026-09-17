"""attachment coordinates: latitude, longitude, location_source

Revision ID: d3f81b06c9a4
Revises: b6d2ef41a705
Create Date: 2026-09-17 21:10:00.000000

Expand-only, three nullable columns on ONE table, no backfill and no data rewrite (Places,
Phase 1). Nothing is added to `uploads`: an upload session is a short-lived row that outlives
nothing, and a coordinate on it would be a second place a location could leak from. The read
happens once, at attachment finalization, from the file's final path.

* `attachments.latitude` / `longitude` are degrees, WGS 84, double precision — the values the
  file's own metadata carried (EXIF GPS IFD, or the QuickTime ISO 6709 key). NULL means this row
  has no location, which is the answer for every row that predates this migration.
* `attachments.location_source` carries what NULL alone cannot say: NULL = never read, `'none'` =
  read and the file carried no usable location, `'unreadable'` = the original could not be read
  (missing mount, timeout, parse failure — retried only by `backfill_location.py
  --retry-unreadable`), `'exif'` / `'quicktime'` = where the value came from. The one-off backfill
  selects on exactly this column, so a killed run resumes where it stopped.
* `ck_attachments_location_pair` makes half a coordinate impossible: a row with one column set
  would put a marker at an implied zero, which is a real place in the Atlantic.
  `ck_attachments_location_range` refuses a magnitude that is a parse gone wrong rather than a
  point on Earth. Both are additive and hold trivially for every existing row (all NULL).

The columns are NULLABLE and the CHECKs are satisfied by NULL, which is what makes the ROLLBACK of
the release that carries this migration an expand-preserving one: the PREVIOUS image's ORM simply
does not mention the three columns and runs against this schema unchanged. Production therefore
never downgrades — the downgrade below exists for the rehearsal on a throwaway restore, where it
proves the script is reversible, and it drops exactly what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f81b06c9a4"
down_revision: str | None = "b6d2ef41a705"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("latitude", sa.Double()))
    op.add_column("attachments", sa.Column("longitude", sa.Double()))
    op.add_column("attachments", sa.Column("location_source", sa.String(length=16)))
    op.create_check_constraint(
        "ck_attachments_location_pair",
        "attachments",
        "(latitude IS NULL) = (longitude IS NULL)",
    )
    op.create_check_constraint(
        "ck_attachments_location_range",
        "attachments",
        "latitude IS NULL OR ("
        "latitude >= -90 AND latitude <= 90 AND longitude >= -180 AND longitude <= 180)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_attachments_location_range", "attachments", type_="check")
    op.drop_constraint("ck_attachments_location_pair", "attachments", type_="check")
    op.drop_column("attachments", "location_source")
    op.drop_column("attachments", "longitude")
    op.drop_column("attachments", "latitude")
