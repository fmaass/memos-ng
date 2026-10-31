"""Durable counters the WORKER bumps and the app's `/metrics` scrape reads (MEMNG-1046).

The worker serves no `/metrics`, and what it counts here leaves no row behind to count later: a
reaped session is deleted, and a failed job's row leaves `media_jobs` with its attachment. So each
event bumps a tally row in the transaction that does the work — a rollback un-counts it with the
work — and `upload_queue_metrics` reads the tallies per scrape.

A tally is telemetry, so it never costs the work it describes: the bump runs under a SAVEPOINT,
and a refused write (a worker that started before the app ran the migration, say) is logged and
rolled back alone while the caller's transaction goes on.
"""

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FailureReason

logger = structlog.get_logger(__name__)

# Module constants so a test can point a bump at a table that refuses it.
MEDIA_FAILURES_TABLE = "media_job_failure_tallies"
UPLOAD_SESSIONS_TABLE = "upload_session_tallies"


async def _bump(db: AsyncSession, statement: str, params: dict[str, object]) -> None:
    try:
        async with db.begin_nested():
            await db.execute(text(statement), params)
    except Exception:
        logger.exception("tally_write_failed", **params)


async def count_media_job_failures(
    db: AsyncSession, lane: str, reason: FailureReason, n: int = 1
) -> None:
    """Add `n` terminal failures of `lane` (a worker consumer class) for `reason`."""
    if n <= 0:
        return
    await _bump(
        db,
        f"INSERT INTO {MEDIA_FAILURES_TABLE} AS t (lane, reason, count) "
        "VALUES (:lane, :reason, :n) "
        "ON CONFLICT (lane, reason) DO UPDATE SET count = t.count + EXCLUDED.count",
        {"lane": lane, "reason": reason.value, "n": n},
    )


async def count_upload_sessions(db: AsyncSession, result: str, n: int) -> None:
    """Add `n` upload sessions that ended with `result` where only the worker saw it end."""
    if n <= 0:
        return
    await _bump(
        db,
        f"INSERT INTO {UPLOAD_SESSIONS_TABLE} AS t (result, count) VALUES (:result, :n) "
        "ON CONFLICT (result) DO UPDATE SET count = t.count + EXCLUDED.count",
        {"result": result, "n": n},
    )
