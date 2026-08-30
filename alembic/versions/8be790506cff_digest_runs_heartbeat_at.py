"""digest_runs.heartbeat_at — the liveness stamp the stale-run sweep judges

Revision ID: 8be790506cff
Revises: b3f70d2c9a41
Create Date: 2026-08-30 17:30:48.891027

The ONE schema change Phase 2 makes (MEMNG-234), and expand-only in both halves:

* The column is NULLABLE with no server default, so no existing row is rewritten and no NOT NULL
  lands on populated data (LSN-0024 does not bite here for that reason). NULL is meaningful and is
  the correct value for every run that predates this migration and for every run that has not yet
  stamped one: the sweep reads `COALESCE(heartbeat_at, started_at)`, so a never-beaten run is
  judged on when it started, at the same 15-minute bar.
* A process running the OLD code against the migrated schema simply never writes it — its runs
  read as never-beaten, which is exactly what they are.
* The rollback is a plain column drop; nothing references it and no index covers it.

Why the column exists at all: the stale-run sweep must terminate a run whose PROCESS died, and it
must never terminate a healthy long one. A legitimate run can exceed two hours (up to
`max_memos_per_run` memos, several videos each, Dropbox upload sessions whose per-operation
timeout is 600 s), so a sweep measuring elapsed time from `started_at` would mark a live run
`failed`, drop it out of `uq_digest_runs_single_inflight`, and let a second run insert and mail the
same memos beside it — an unsendable duplicate, not a tuning miss. Liveness is the only signal
that tells those two states apart.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8be790506cff'
down_revision: str | None = 'b3f70d2c9a41'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'digest_runs', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('digest_runs', 'heartbeat_at')
