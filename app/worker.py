"""memos-ng media worker: claims media_jobs (FOR UPDATE SKIP LOCKED) and runs the fast lane.

Same image as the app, started with `command: worker`. The slow-lane H.264 transcode
lands in T2.3; this worker handles the fast lane (thumbs/mobile/poster/blurhash/dims).
"""

import asyncio
import os
import socket
import time
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobState, MediaJob
from app.services import media, push
from app.services.uploads import reap_expired
from app.version import __version__

logger = structlog.get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
POLL_SECONDS = 10
REAP_INTERVAL_SECONDS = 3600


def heartbeat_path() -> Path:
    return Path(settings.DATA_ROOT) / "worker.heartbeat"


def beat() -> None:
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


async def claim_one(db: AsyncSession) -> MediaJob | None:
    """Atomically claim the next queued job (fast or slow); concurrent workers skip a locked row."""
    stmt = (
        select(MediaJob)
        .where(MediaJob.state == JobState.queued)
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
    """Web-push the owner when an attachment reaches a terminal state (ready/failed).

    Gates on the *attachment's* status, not the job's: an image fast-lane job finishes at
    'ready', but a video fast-lane job finishes at 'processing' (the slow-lane job fires
    this later when it reaches 'ready'). Best-effort — a push failure never breaks the job.
    """
    att = (
        await db.execute(select(Attachment).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    if att is None or att.status not in (AttachmentStatus.ready, AttachmentStatus.failed):
        return
    try:
        await push.send_to_user(
            db,
            att.owner_id,
            {"uid": att.uid, "status": att.status.value, "title": att.filename},
        )
    except Exception as exc:  # push is best-effort; it must not fail the media job
        logger.warning("push_notify_failed", attachment_id=attachment_id, error=str(exc))


async def run_once(db: AsyncSession) -> str:
    """Claim+process one job in one transaction (row locked until commit).
    Returns 'processed' | 'idle' | 'deferred' (deferred → caller backs off)."""
    job = await claim_one(db)
    if job is None:
        await db.commit()
        return "idle"
    try:
        await media.process_job(db, job)
        job.state = JobState.done
        job.progress_pct = 100
        await _notify_terminal(db, job.attachment_id)
        await db.commit()
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
            await _notify_terminal(db, job.attachment_id)
            logger.warning("media_job_failed_terminal", job_id=job.id, error=str(exc))
            await db.commit()
            return "processed"
        job.state = JobState.queued  # retry with backoff
        job.claimed_by = None
        logger.warning("media_job_retry", job_id=job.id, attempts=job.attempts, error=str(exc))
        await db.commit()
        return "deferred"


async def run() -> None:
    logger.info("worker_started", version=__version__, worker_id=WORKER_ID)
    beat()
    last_reap = 0.0
    while True:
        async with async_session() as db:
            status = await run_once(db)
        beat()
        if time.time() - last_reap > REAP_INTERVAL_SECONDS:
            async with async_session() as db:
                reaped = await reap_expired(db)
            if reaped:
                logger.info("uploads_reaped", count=reaped)
            last_reap = time.time()
        if status != "processed":  # idle or deferred (thermal/retry backoff)
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
