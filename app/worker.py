"""memos-ng media worker: claims media_jobs (FOR UPDATE SKIP LOCKED) and runs them.

Same image as the app, started with `command: worker`. Each worker drains the lanes in
WORKER_LANES: the fast lane (thumbs/mobile/poster/blurhash/dims) and/or the slow lane (the
CPU-heavy H.264 transcode). Deployed as ONE in-stack worker running both lanes, recreated
atomically with the app container from the same compose stack — schema and code move together,
so no mixed-version worker ever runs against a newer app. (The Saturn-fast/Merkur-slow split
this replaced is historical; see ADR-0006.)
"""

import asyncio
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    FrameShareJob,
    FrameShareState,
    JobLane,
    JobState,
    MediaJob,
)
from app.services import frame_share, media, push
from app.services.uploads import reap_expired_committed
from app.version import __version__

logger = structlog.get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
POLL_SECONDS = 10
REAP_INTERVAL_SECONDS = 3600
ERROR_TEXT_LIMIT = 500  # media_jobs.error is Text, but a stack-sized message helps nobody


def parse_worker_lanes(raw: str) -> list[JobLane]:
    """Parse WORKER_LANES (e.g. 'slow' or 'fast,slow') into the lanes this worker drains.

    Empty/unset → both lanes (preserves the single-worker default). An unknown token is a
    misconfiguration and raises — fail loud rather than silently draining the wrong lane.
    """
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not tokens:
        return [JobLane.fast, JobLane.slow]
    lanes: list[JobLane] = []
    for t in tokens:
        try:
            lane = JobLane(t)
        except ValueError as exc:
            raise ValueError(f"Invalid WORKER_LANES token {t!r} (expected fast/slow)") from exc
        if lane not in lanes:
            lanes.append(lane)
    return lanes


def heartbeat_path() -> Path:
    return Path(settings.DATA_ROOT) / "worker.heartbeat"


def beat() -> None:
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


async def claim_one(db: AsyncSession, lanes: list[JobLane] | None = None) -> MediaJob | None:
    """Atomically claim the next queued job in this worker's lanes; concurrent workers skip a
    locked row. `lanes` defaults to the configured WORKER_LANES — the Saturn worker drains
    `fast`, the Merkur worker drains `slow`, so a job is claimed by exactly one of them."""
    if lanes is None:
        lanes = parse_worker_lanes(settings.WORKER_LANES)
    stmt = (
        select(MediaJob)
        .where(MediaJob.state == JobState.queued, MediaJob.lane.in_(lanes))
        .order_by(MediaJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is not None:
        job.state = JobState.processing
        job.claimed_by = WORKER_ID
        await db.flush()
    return job


def sanitize_error(text: str, limit: int = ERROR_TEXT_LIMIT) -> str:
    """Render an exception message so that STORING it can never become the next failure.

    `job.error` is a Postgres text column, and Postgres refuses the NUL byte in text outright;
    lone surrogates — what surrogateescape-decoded paths and raw ffmpeg/pyvips byte output leave
    behind in a message — cannot be UTF-8 encoded by the driver at all. Either one turned the
    *failure record's* own commit into a second exception, raised from inside run_once's except
    handler: it escaped run(), killed the process, and the restarted container crash-looped on
    the very same row (MEMNG-39).
    """
    # Truncate LAST: escaping a surrogate lengthens the text, so bounding first would not bind.
    cleaned = text.replace("\x00", "")
    return cleaned.encode("utf-8", "backslashreplace").decode("utf-8")[:limit]


async def _fail_attachment(db: AsyncSession, job: MediaJob) -> None:
    att = (
        await db.execute(select(Attachment).where(Attachment.id == job.attachment_id))
    ).scalar_one_or_none()
    if att is not None:
        att.status = AttachmentStatus.failed  # terminal + UI-visible, never a silent drop


async def _notify_terminal(db: AsyncSession, attachment_id: int) -> None:
    """Web-push the owner when an attachment is in a terminal state (ready/failed).

    Gates on the *attachment's* status, not the job's: an image fast-lane job finishes at
    'ready', but a video fast-lane job finishes at 'processing' (the slow-lane job fires
    this later when it reaches 'ready'). Errors propagate here; `_notify_terminal_safe`
    isolates them — this function itself never runs inside the media job's transaction.
    """
    att = (
        await db.execute(select(Attachment).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    if att is None or att.status not in (AttachmentStatus.ready, AttachmentStatus.failed):
        return
    await push.send_to_user(
        db,
        att.owner_id,
        {"uid": att.uid, "status": att.status.value, "title": att.filename},
    )


async def _notify_terminal_safe(attachment_id: int) -> None:
    """Best-effort push in its OWN session, AFTER the media job has committed.

    Decoupled deliberately: running push (and its 404/410 prune flush) inside the job's
    transaction means a push/flush error would poison that session and fail the *already
    finished* media job. Here the job is durable before we ever touch push, and any error
    — including a poisoned session — is swallowed.
    """
    try:
        async with async_session() as db:
            await _notify_terminal(db, attachment_id)
            await db.commit()
    except Exception as exc:  # push is best-effort; the job is already committed
        logger.warning("push_notify_failed", attachment_id=attachment_id, error=str(exc))


async def _record_job_failure(
    db: AsyncSession, job: MediaJob, attachment_id: int, exc: Exception
) -> str:
    """Record one job's failure — a retry, or terminal after MEDIA_MAX_ATTEMPTS.

    The whole body is guarded because this is the write that must not itself explode. The error
    text is sanitized (see sanitize_error), but the commit can still fail for reasons this
    process does not control: a session already poisoned by the failed flush inside process_job
    (after which every statement on it errors), or a dropped connection. Before MEMNG-39 that
    second exception escaped run_once and took the worker down with it. Here it is rolled back,
    logged, and reported as 'deferred' so the loop backs off and keeps draining.

    That rollback also undoes claim_one's claim: the claim and the outcome share ONE transaction
    and media.process_job only ever flushes, so `processing` is never a durable state. The row
    reverts to `queued` and the next poll re-claims it — the recovery path is the ordinary claim,
    not a sweep (there is none for media_jobs; claim_one selects state=queued only). The cost is
    that this attempt goes uncounted, so a failure whose *recording* keeps failing retries every
    poll instead of aging out through MEDIA_MAX_ATTEMPTS — acceptable, because a commit that
    cannot land is a DB-wide problem and a fresh session next poll records the failure normally.
    """
    job_id = job.id  # a rollback expires the instance — keep the id for the log lines
    error = sanitize_error(str(exc))
    try:
        job.attempts += 1
        job.error = error
        if job.attempts >= settings.MEDIA_MAX_ATTEMPTS:
            job.state = JobState.failed
            await _fail_attachment(db, job)
            logger.warning("media_job_failed_terminal", job_id=job_id, error=error)
            await db.commit()
            await _notify_terminal_safe(attachment_id)  # separate session, after commit
            return "processed"
        job.state = JobState.queued  # retry with backoff
        job.claimed_by = None
        logger.warning("media_job_retry", job_id=job_id, attempts=job.attempts, error=error)
        await db.commit()
        return "deferred"
    except Exception:
        await db.rollback()
        logger.exception("media_job_failure_record_error", job_id=job_id)
        return "deferred"


async def run_once(db: AsyncSession) -> str:
    """Claim+process one job in one transaction (row locked until commit).
    Returns 'processed' | 'idle' | 'deferred' (deferred → caller backs off)."""
    # Whole-worker thermal gate: when the host is too hot, claim/process NOTHING (the fast
    # lane's image work has no inner thermal check, so a bulk import could otherwise drive a
    # transcode/thumbnail storm). Back off until it cools — protects the thermally-coupled host.
    if media.too_hot():
        temp_mc = media.read_temp_millideg()
        logger.info(
            "worker_thermal_defer",  # the safety guard firing — observable, not just inferable
            temp_c=round(temp_mc / 1000, 1) if temp_mc is not None else None,
            max_c=settings.THERMAL_MAX_C,
        )
        await db.commit()
        return "deferred"
    job = await claim_one(db)
    if job is None:
        await db.commit()
        return "idle"
    attachment_id = job.attachment_id  # captured before commit expires the job instance
    try:
        await media.process_job(db, job)
        job.state = JobState.done
        job.progress_pct = 100
        await db.commit()
        await _notify_terminal_safe(attachment_id)  # separate session, after the job is durable
        return "processed"
    except media.ThermalDefer:
        job.state = JobState.queued  # too hot — try again later, not a failure
        job.claimed_by = None
        await db.commit()
        return "deferred"
    except media.AttachmentGone as exc:
        # The attachment was deleted while this job sat queued or ran (MEMNG-41). Nothing is left
        # to derive, nothing to notify, and no attempt to count — the job is discarded here,
        # where this worker still holds the row lock. That row is precisely the one
        # delete_attachment could NOT remove (it skips locked rows rather than waiting out a
        # transcode), so dropping it is the second half of that delete, not bookkeeping.
        # A commit failure here is left to run_media_drain: the claim rolls back to `queued` and
        # the next poll discards the job again.
        job_id = job.id  # the commit expires the instance — keep the id for the log line
        await db.delete(job)
        await db.commit()
        logger.info(
            "media_job_discarded", job_id=job_id, attachment_id=attachment_id, reason=str(exc)
        )
        return "processed"
    except Exception as exc:  # a bad input must not kill the worker
        return await _record_job_failure(db, job, attachment_id, exc)


async def run_media_drain() -> str:
    """Drain one media job in its own session, isolating ALL errors from run()'s loop.

    The same boundary guard run_frame_share_drain applies to the optional frame feature, now
    applied to the media branch itself (MEMNG-39). run_once handles a *job* raising; this handles
    everything outside that handler — the failure record's own commit, claim_one, the thermal
    commit, and the session context manager's exit. Any of those escaping run() killed the
    process, and because the restarted container re-claims the same row it crash-looped on one
    poisoned job. Returns run_once's status, or 'error' when the drain raised (non-'processed',
    so the loop backs off but keeps running).

    `except Exception` deliberately: CancelledError and KeyboardInterrupt are BaseExceptions and
    must keep propagating, or shutdown stops being prompt and clean.
    """
    try:
        async with async_session() as db:
            return await run_once(db)
    except Exception:
        logger.exception("media.drain_error")
        return "error"


async def sweep_job_progress_safe() -> int:
    """Remove progress rows whose job is gone (MEMNG-36), in its own session with ALL errors
    isolated — the same boundary guard the other periodic sweeps get. Progress telemetry must
    never be able to halt media processing. Returns the number of rows removed (0 on error)."""
    try:
        swept = await media.sweep_orphan_progress()
    except Exception:
        logger.exception("job_progress.sweep_error")
        return 0
    if swept:
        logger.info("job_progress_swept", count=swept)
    return swept


async def claim_frame_share_one(db: AsyncSession) -> FrameShareJob | None:
    """Atomically claim the next due frame-share job (queued AND next_attempt_at reached), skipping
    rows another worker holds. Sets state=claimed so a crash mid-send never re-runs it (at-most-once
    — plan D1). Honors next_attempt_at for exponential retry backoff.

    claimed_at stamps the lease the recovery sweep reads: at-most-once holds while a worker is
    alive, but a claim no worker is behind any more expires instead of stranding (MEMNG-35)."""
    stmt = (
        select(FrameShareJob)
        .where(
            FrameShareJob.state == FrameShareState.queued,
            or_(
                FrameShareJob.next_attempt_at.is_(None),
                FrameShareJob.next_attempt_at <= func.now(),
            ),
        )
        .order_by(FrameShareJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is not None:
        job.state = FrameShareState.claimed
        job.claimed_by = WORKER_ID
        # App clock on both ends (this stamp and the sweep's cutoff), so cross-worker clock skew is
        # the only exposure — irrelevant at a minutes-scale horizon.
        job.claimed_at = datetime.now(UTC)
        await db.flush()
    return job


async def run_frame_share_once(db: AsyncSession) -> str:
    """Claim + process one frame-share job. Returns 'processed' | 'idle' | 'deferred'.

    The `claimed` state is COMMITTED before any render/upload, so a crash between the album API
    accepting the photo and the result commit does not re-run the send on the next poll
    (at-most-once while a worker is alive). It no longer strands the job either:
    claim_frame_share_one stamps the lease and recover_stale_claimed re-queues a claim nobody is
    behind past the stale horizon, at the price of a possible duplicate upload — see that
    function's trade note (MEMNG-35)."""
    if not frame_share.frame_api_configured():
        # Only a worker that can actually upload may CLAIM these jobs. The Merkur slow-lane worker
        # runs the same image without the frame API configured — without this guard it would win the
        # claim race for some jobs and, unable to upload, mark them terminally `skipped`. Leaving
        # them queued lets the frame-API-configured (Saturn) worker drain them.
        await db.commit()
        return "idle"
    if media.too_hot():  # a photo render is light, but honor the same host thermal guard
        await db.commit()
        return "deferred"
    job = await claim_frame_share_one(db)
    if job is None:
        await db.commit()
        return "idle"
    await db.commit()  # commit the `claimed` tombstone BEFORE the external send (plan D1)
    await frame_share.process_claimed_job(db, job)
    await db.commit()
    return "processed"


async def run_frame_share_drain() -> str:
    """Drain one frame-share job in its own session, isolating ALL errors so a DB failure inside the
    claim/revalidate/commit path can never propagate into run()'s loop and halt media processing.
    Returns run_frame_share_once's status, or 'error' when the drain raised (treated as
    non-processed so the loop backs off but keeps running). This is the boundary guard for
    "media processing is immune to the optional frame-share feature"."""
    try:
        async with async_session() as db:
            return await run_frame_share_once(db)
    except Exception:
        logger.exception("frame_share.drain_error")
        return "error"


async def run_frame_share_recover_safe() -> int:
    """Re-queue frame-share jobs a dead worker left in `claimed` (MEMNG-35), in its own committing
    session with ALL errors isolated. Same boundary guard as run_frame_share_drain — the optional
    frame feature must never halt media processing — and at startup an escaping error would kill
    the worker before the loop even begins. Returns the number of rows recovered (0 on error)."""
    try:
        recovered = await frame_share.recover_stale_claimed_committed()
    except Exception:
        logger.exception("frame_share.recover_error")
        return 0
    if recovered:
        logger.info("frame_share_recovered", count=recovered)
    return recovered


async def run() -> None:
    logger.info("worker_started", version=__version__, worker_id=WORKER_ID)
    beat()
    # Startup sweep: the common stranding cause is this very restart (a deploy --force-recreate
    # killing a send mid-flight), so adopt orphaned claims before draining anything new.
    await run_frame_share_recover_safe()
    last_reap = 0.0
    while True:
        status = await run_media_drain()  # own session, all errors isolated from this loop
        beat()  # heartbeat before the (possibly slow HTTP) frame drain — keep liveness honest
        # Drain a frame-share job opportunistically in its own session (like reap): the album upload
        # is decoupled from the media job's transaction so an upload error can't poison media work.
        frame_status = await run_frame_share_drain()
        beat()
        if time.time() - last_reap > REAP_INTERVAL_SECONDS:
            reaped = await reap_expired_committed()
            if reaped:
                logger.info("uploads_reaped", count=reaped)
            # Piggybacks the reap cadence: a claim orphaned by something other than a restart
            # (an OOM-killed send that leaves the worker alive) is recovered within the hour.
            await run_frame_share_recover_safe()
            # Same cadence for the progress side channel: a killed worker cannot clear its own
            # row, so the anti-join sweep keeps the table bounded by what is actually running.
            await sweep_job_progress_safe()
            last_reap = time.time()
        if status != "processed" and frame_status != "processed":  # idle/deferred → back off
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
