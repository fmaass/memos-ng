"""Out-of-band reconciliation of memo creates that raced their own commit (MEMNG-146).

`POST /api/memos` does non-transactional work — placing originals, copying a poster override —
before the transaction that makes the memo real, and unwinds all of it if the write does not land.
Every step of that is arbitrated except one: a cancellation delivered INSIDE `db.commit()` is
genuinely ambiguous. The server may have committed while the driver raises, so the handler's
`committed` flag stays False and its cleanup runs against a memo that IS stored — discarding the
poster of a live video, moving a live memo's originals back out of its folder.

No in-request bookkeeping can be right in both worlds, which is why this lives outside the request.
The window is small (one commit's own duration) and its blast radius is one memo, so the answer is
a periodic sweep that finds the two shapes it can leave behind and reports them:

* a stored video whose poster tier file is gone → re-enqueue its fast-lane job, which derives a
  hero frame again. A supplied override cannot be recovered (its part file was the client's copy
  and the reap took it), but a video with no poster at all is the visible defect.
* a stored attachment whose original is not where the row says → REPORTED, never guessed at. The
  bytes were moved back to an upload part path whose row is committed-deleted, so nothing left in
  the database names them; restoring the wrong file onto a memo would be worse than the leak.

Idempotent by construction: both checks read the filesystem, and the poster re-enqueue skips an
attachment that already has a job waiting, so a second sweep over an unchanged estate does nothing.
"""

from dataclasses import dataclass
from pathlib import Path

import structlog
from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import poster

logger = structlog.get_logger(__name__)

# The sweep stats one file per stored attachment (two for a video) against a bind-mounted share, so
# it is bounded rather than unbounded-by-table-size. A household instance is far below this; a run
# that hits the cap says so, and the next one starts from the same end of the table, so the cap
# being reached is a signal to raise it rather than a silent half-sweep.
MAX_SCAN = 5000

# A job that will still get around to writing the poster.
IN_FLIGHT_JOBS = (JobState.queued, JobState.claimed, JobState.processing)

# A stored attachment: linked to a memo that committed. `pending` is IN — a video is pending until
# its fast lane runs, which is exactly the state a commit-raced create leaves behind, and an
# ordinary pending video is protected by the in-flight-job check rather than by this filter.
# `uploading` has no bytes yet, and `failed` has already exhausted the retry policy: re-enqueueing
# one of those would retry a genuinely broken video for ever.
LIVE_STATUSES = (
    AttachmentStatus.pending,
    AttachmentStatus.processing,
    AttachmentStatus.ready,
)


@dataclass(frozen=True)
class ReconcileReport:
    """What one sweep found. Uids rather than ids: they are what every log and URL already names."""

    posters_requeued: list[str]
    misplaced_originals: list[str]
    scanned: int
    truncated: bool


def _missing_sync(paths: list[str]) -> set[str]:
    """Which of these files are absent — the WHOLE batch in one off-loop hop, because each stat is
    a network round-trip against the data share."""
    return {path for path in paths if not Path(path).exists()}


async def reconcile_commit_races(db: AsyncSession, *, limit: int = MAX_SCAN) -> ReconcileReport:
    """One sweep. Returns what it found; commits nothing (the caller owns the transaction)."""
    rows = list(
        (
            await db.execute(
                select(Attachment)
                .where(
                    Attachment.memo_id.is_not(None),
                    Attachment.status.in_(LIVE_STATUSES),
                    Attachment.storage_path.is_not(None),
                )
                .order_by(Attachment.id)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return ReconcileReport([], [], 0, False)

    videos = [att for att in rows if att.mime.startswith("video/")]
    posters = {att.uid: str(poster.poster_path(att.uid)) for att in videos}
    originals = {att.uid: str(att.storage_path) for att in rows}
    missing = await to_thread.run_sync(_missing_sync, [*originals.values(), *posters.values()])

    misplaced = [att.uid for att in rows if originals[att.uid] in missing]

    # A video with no poster AND no job waiting to make one: the fast lane is not going to run
    # again on its own. One query for the whole batch — a per-attachment lookup would be N.
    posterless = [att for att in videos if posters[att.uid] in missing]
    requeued: list[str] = []
    if posterless:
        pending_jobs = set(
            (
                await db.execute(
                    select(MediaJob.attachment_id).where(
                        MediaJob.attachment_id.in_([att.id for att in posterless]),
                        MediaJob.state.in_(IN_FLIGHT_JOBS),
                    )
                )
            )
            .scalars()
            .all()
        )
        for att in posterless:
            if att.id in pending_jobs:
                continue  # a job is already on its way — enqueueing a second would double the work
            db.add(MediaJob(attachment_id=att.id, lane=JobLane.fast, state=JobState.queued))
            requeued.append(att.uid)
        await db.flush()

    report = ReconcileReport(requeued, misplaced, len(rows), truncated)
    if requeued:
        logger.warning("reconcile.poster_requeued", uids=requeued)
    if misplaced:
        # Deliberately loud and deliberately not repaired: a stored memo is showing a photo whose
        # file is not where its row says, and only a human can decide what to put back.
        logger.error("reconcile.original_misplaced", uids=misplaced)
    if truncated:
        logger.warning("reconcile.scan_truncated", limit=limit)
    return report
