"""attachment camera EXIF: eight camera columns and the camera_source ledger

Revision ID: 5c2e9a7d41b8
Revises: 339b3d02ae60
Create Date: 2026-10-18 18:00:00.000000

Expand-only (MEMNG-865), nine nullable columns on ONE table, no backfill and no data rewrite in
the migration itself. The values are what the original's own EXIF carried, read once at
finalization (`app/services/camera.py`) and, for the existing library, by
`app/scripts/backfill_camera.py`.

* `camera_make` / `camera_model` / `lens_model` are TEXT — already cleaned and capped by the
  reader, which is the one guard between a file's bytes and these columns.
* `f_number`, `exposure_time_s`, `focal_length_mm`, `exposure_bias_ev` are double precision and
  `iso` an INTEGER, each finite and in range or NULL (validated per field in the reader).
* `camera_source` is the outcome ledger NULL alone cannot say: NULL = never visited, `'exif'` = at
  least one field survived, `'none'` = read and nothing survived, `'unreadable'` = the read failed
  (retried only by `backfill_camera.py --retry-unreadable`), `'not-image'` = classified by mime and
  never read. The backfill resumes on `camera_source IS NULL`, never on whether a column is set.

Every column is NULLABLE with no default, which is what makes the rollback of the release that
carries this migration an expand-preserving one: the PREVIOUS image's ORM does not mention them
and runs against this schema unchanged. Production never downgrades — the downgrade exists for
the rehearsal on a throwaway restore and drops exactly what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c2e9a7d41b8"
down_revision: str | None = "339b3d02ae60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("camera_make", sa.Text()),
    ("camera_model", sa.Text()),
    ("lens_model", sa.Text()),
    ("f_number", sa.Double()),
    ("exposure_time_s", sa.Double()),
    ("focal_length_mm", sa.Double()),
    ("exposure_bias_ev", sa.Double()),
    ("iso", sa.Integer()),
    ("camera_source", sa.String(length=16)),
)


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("attachments", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(COLUMNS):
        op.drop_column("attachments", name)
