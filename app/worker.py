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
from app.models import JobLane, JobState, MediaJob
from app.services import media
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
    """Atomically claim the next queued fast-lane job; concurrent workers skip a locked row."""
    stmt = (
        select(MediaJob)
        .where(MediaJob.state == JobState.queued, MediaJob.lane == JobLane.fast)
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


async def run_once(db: AsyncSession) -> bool:
    """Claim+process one job in a single transaction (the row stays locked until commit)."""
    job = await claim_one(db)
    if job is None:
        await db.commit()
        return False
    try:
        await media.process_job(db, job)
        job.state = JobState.done
    except Exception as exc:  # a bad input must not kill the worker
        job.state = JobState.failed
        job.attempts += 1
        job.error = str(exc)[:500]
        logger.warning("media_job_failed", job_id=job.id, error=str(exc))
    await db.commit()
    return True


async def run() -> None:
    logger.info("worker_started", version=__version__, worker_id=WORKER_ID)
    beat()
    last_reap = 0.0
    while True:
        async with async_session() as db:
            processed = await run_once(db)
        beat()
        if time.time() - last_reap > REAP_INTERVAL_SECONDS:
            async with async_session() as db:
                reaped = await reap_expired(db)
            if reaped:
                logger.info("uploads_reaped", count=reaped)
            last_reap = time.time()
        if not processed:
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
