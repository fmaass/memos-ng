"""memos-ng media worker: claims media_jobs (FOR UPDATE SKIP LOCKED) and runs them.

Same image as the app, started with `command: worker`. Each worker drains the lanes in
WORKER_LANES: the fast lane (thumbs/mobile/poster/blurhash/dims) and/or the slow lane (the
CPU-heavy H.264 transcode). The Saturn in-stack worker runs `fast` and a Merkur worker runs
`slow`, moving transcode off the thermally-bound NAS (ADR-0006 revisit). Default = both lanes.
"""

import asyncio
import os
import socket
import time
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
    except Exception as exc:  # a bad input must not kill the worker
        job.attempts += 1
        job.error = str(exc)[:500]
        if job.attempts >= settings.MEDIA_MAX_ATTEMPTS:
            job.state = JobState.failed
            await _fail_attachment(db, job)
            logger.warning("media_job_failed_terminal", job_id=job.id, error=str(exc))
            await db.commit()
            await _notify_terminal_safe(attachment_id)  # separate session, after commit
            return "processed"
        job.state = JobState.queued  # retry with backoff
        job.claimed_by = None
        logger.warning("media_job_retry", job_id=job.id, attempts=job.attempts, error=str(exc))
        await db.commit()
        return "deferred"


async def claim_frame_share_one(db: AsyncSession) -> FrameShareJob | None:
    """Atomically claim the next due frame-share job (queued AND next_attempt_at reached), skipping
    rows another worker holds. Sets state=claimed so a crash mid-send never re-runs it (at-most-once
    — plan D1). Honors next_attempt_at for exponential retry backoff."""
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
        await db.flush()
    return job


async def run_frame_share_once(db: AsyncSession) -> str:
    """Claim + process one frame-share job. Returns 'processed' | 'idle' | 'deferred'.

    The `claimed` state is COMMITTED before any render/upload, so a crash between the album API
    accepting the photo and the result commit strands the job in `claimed` (never re-claimed) rather
    than re-uploading it."""
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


async def run() -> None:
    logger.info("worker_started", version=__version__, worker_id=WORKER_ID)
    beat()
    last_reap = 0.0
    while True:
        async with async_session() as db:
            status = await run_once(db)
        beat()  # heartbeat before the (possibly slow HTTP) frame drain — keep liveness honest
        # Drain a frame-share job opportunistically in its own session (like reap): the album upload
        # is decoupled from the media job's transaction so an upload error can't poison media work.
        frame_status = await run_frame_share_drain()
        beat()
        if time.time() - last_reap > REAP_INTERVAL_SECONDS:
            reaped = await reap_expired_committed()
            if reaped:
                logger.info("uploads_reaped", count=reaped)
            last_reap = time.time()
        if status != "processed" and frame_status != "processed":  # idle/deferred → back off
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
