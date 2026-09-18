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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import migrations
from app.config import settings
from app.database import async_session
from app.logging_privacy import configure_logging
from app.models import (
    Attachment,
    AttachmentStatus,
    FrameShareJob,
    FrameShareState,
    JobKind,
    JobLane,
    JobState,
    MediaJob,
)
from app.services import frame_share, media, push, reconcile
from app.services.digest import gate as digest_gate
from app.services.digest import runner as digest_runner
from app.services.uploads import reap_expired_committed
from app.version import __version__

configure_logging()  # the worker is where a coordinate is written, so it renders logs the same way
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
    """Stamp the liveness file the healthcheck reads.

    Stays synchronous on purpose (MEMNG-42). It runs only in the worker process, whose loop
    drains one job at a time with nothing else pending, so there is no request to stall — and
    the write must actually reach the share before the next (possibly slow) step, since a
    heartbeat deferred to a thread is a heartbeat that can be reordered behind the very stall it
    is supposed to make visible. It doubles as the share-ACL canary: this is the real file write
    that proves the container's uid can write the bind-mounted volume.
    """
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
    """Mark the photo failed when the work that PRODUCES it gives up — and only then.

    Scoped to `derive` (MEMNG-49): a printready job derives an extra, on-demand tier for a photo
    that is already published and displaying. Failing that job says nothing about the photo, and
    marking it `failed` would blank a perfectly good picture out of the feed because a print
    rendition nobody is looking at yet could not be encoded. The failure stays on the job row,
    which is where the enqueue path reads it back from.
    """
    if job.kind != JobKind.derive:
        return
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
    db: AsyncSession, job_id: int, attachment_id: int, job_kind: str, exc: Exception
) -> str:
    """Record one job's failure — a retry, or terminal after MEDIA_MAX_ATTEMPTS — in a FRESH
    transaction. Returns 'processed' (terminal) | 'deferred' (retry / could-not-record).

    ROLLS BACK FIRST (MEMNG-90 amplifier #1): the failing job may have poisoned the session with a
    bad flush, and committing a poisoned session raises PendingRollbackError — which before this fix
    turned the failure record itself into a second exception, so `job.attempts` never incremented
    and the job looped at attempts=0 forever. After the rollback the job is re-fetched by id and the
    attempt actually persists, so a persistently-bad job ages out to `failed` at MEDIA_MAX_ATTEMPTS.

    The whole record is still guarded: if even this commit cannot land (a DB-wide problem), the row
    stays `processing` (run_once committed the claim before processing) for the stale-claim sweep to
    recover. Because claim_one is queued-only, a stuck `processing` row is skipped, not re-claimed
    every poll — so a single unrecordable failure no longer blocks the queue head (amplifier #2).
    """
    await db.rollback()  # clear any poison/partial txn from process_job BEFORE writing
    error = sanitize_error(str(exc))
    try:
        job = await db.get(MediaJob, job_id)
        if job is None:  # discarded elsewhere (an attachment delete raced) — nothing to record
            return "deferred"
        job.attempts += 1
        job.error = error
        if job.attempts >= settings.MEDIA_MAX_ATTEMPTS:
            job.state = JobState.failed
            await _fail_attachment(db, job)
            logger.warning("media_job_failed_terminal", job_id=job_id, error=error)
            await db.commit()
            if job_kind == JobKind.derive:  # see run_once for why the push is kind-scoped
                await _notify_terminal_safe(attachment_id)  # separate session, after commit
            return "processed"
        job.state = JobState.queued  # retry
        job.claimed_by = None
        logger.warning("media_job_retry", job_id=job_id, attempts=job.attempts, error=error)
        await db.commit()
        return "deferred"
    except Exception:
        await db.rollback()
        logger.exception("media_job_failure_record_error", job_id=job_id)
        return "deferred"


async def _requeue_claim(db: AsyncSession, job_id: int) -> None:
    """Return a job whose claim was committed (`processing`) to `queued`, in a fresh transaction —
    used when processing defers (thermal). Drops any partial transaction first; a job deleted out
    from under us (a delete raced the defer) is simply nothing to requeue."""
    await db.rollback()
    job = await db.get(MediaJob, job_id)
    if job is None:
        return
    job.state = JobState.queued
    job.claimed_by = None
    await db.commit()


async def _discard_claim(db: AsyncSession, job_id: int, attachment_id: int, reason: str) -> None:
    """Discard a job whose attachment is gone (MEMNG-41), in a fresh transaction. Drops any partial
    transaction, then deletes the job row if it still exists — delete_attachment may have already
    removed it (it no longer skips this row, since the claim released the media_jobs lock)."""
    await db.rollback()
    job = await db.get(MediaJob, job_id)
    if job is not None:
        await db.delete(job)
        await db.commit()
    logger.info("media_job_discarded", job_id=job_id, attachment_id=attachment_id, reason=reason)


async def run_once(db: AsyncSession) -> str:
    """Claim one job, COMMIT the claim, then process it in a SEPARATE transaction (MEMNG-90).
    Returns 'processed' | 'idle' | 'deferred' (deferred → caller backs off).

    The claim (`state=processing`) is committed BEFORE media.process_job so no DB transaction is
    held open across the transcode — an idle-in-transaction session across a slow-lane transcode is
    exactly what Postgres killed mid-job, wedging the queue. `processing` is therefore DURABLE: a
    worker that dies mid-job leaves the row for the stale-claim sweep (claim_one is queued-only, so
    it also never re-claims and blocks behind such a row), not a rollback-to-queued. The result is
    written in a fresh transaction that re-locks the job row before the attachment (guard_publish),
    matching delete_attachment's lock order so the two cannot deadlock."""
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
        await db.rollback()  # nothing claimed — drop the empty transaction
        return "deferred"
    job = await claim_one(db)
    if job is None:
        await db.rollback()
        return "idle"
    attachment_id = job.attachment_id  # captured before the commits below
    job_id = job.id  # ...and the progress side channel is keyed on it
    job_kind = job.kind  # ...decides whether this job's outcome is the PHOTO's outcome
    # Commit the claim BEFORE processing (see docstring): `processing` becomes durable and the
    # media_jobs row lock is released for the transcode's whole duration.
    await db.commit()
    # Cleanup BEFORE processing (MEMNG-36): drop any progress row a dead attempt left behind, so the
    # snapshot never reports a percentage that goes backwards. Its own committing session; a no-op
    # delete on a fast-lane job that never published.
    await media.clear_progress(job_id)
    try:
        await media.process_job(db, job)
        job.state = JobState.done
        job.progress_pct = 100
        await db.commit()
        if job_kind == JobKind.derive:
            # Only the pipeline that PRODUCES the photo announces it. A printready job leaves the
            # attachment exactly as it found it (already `ready`), so pushing here would send the
            # owner a second "your photo is ready" per photo — a few hundred of them per book.
            await _notify_terminal_safe(attachment_id)  # separate session, after job is durable
        return "processed"
    except media.ThermalDefer:
        await _requeue_claim(db, job_id)  # too hot — back to queued, not a failure
        return "deferred"
    except media.AttachmentGone as exc:
        # The attachment was deleted while this job ran (MEMNG-41): nothing to derive or notify,
        # no attempt to count. guard_publish already swept this run's output; discard the job row.
        await _discard_claim(db, job_id, attachment_id, str(exc))
        return "processed"
    except Exception as exc:  # a bad input must not kill the worker
        return await _record_job_failure(db, job_id, attachment_id, job_kind, exc)
    finally:
        # Cleanup AFTER the outcome is committed — every branch above commits before it returns.
        # Clearing the row from inside the transcode instead (media.process_slow) opened a window
        # between that clear and this transaction's commit in which the snapshot fell back to the
        # job row's committed progress (0) while the attachment still read `processing`: the
        # client saw its transcode reach 100%, snap back to 0%, and only then turn ready
        # (MEMNG-36). After the commit there is no such window — a finished job leaves the
        # snapshot's active set in the same transaction that publishes the result.
        await media.clear_progress(job_id)


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


async def recover_stale_media_claims(db: AsyncSession, now: datetime | None = None) -> int:
    """Re-queue (or terminally fail) media jobs stuck in `processing` past the stale horizon.

    run_once commits the claim (`state=processing`) before the transcode so no DB transaction is
    held across it (MEMNG-90). The price of that durable claim: a worker that dies mid-job — a
    deploy --force-recreate, an OOM kill, a host reboot — leaves the row in `processing`, which
    claim_one (queued-only) never re-claims. This sweep is the lease side of the claim: a
    `processing` row whose updated_at is older than MEDIA_CLAIM_STALE_SECONDS has no live worker
    behind it. (Mirrors run_frame_share_recover_safe for the media queue.)

    The horizon MUST exceed SLOW_TRANSCODE_TIMEOUT_S — a legitimately running transcode keeps
    updated_at pinned at claim time for its whole duration (progress lives in a separate table), so
    a shorter horizon would re-queue a live job and double-transcode it. Recovery costs one attempt,
    so a job that reliably kills its worker walks to the visible `failed` state at the attempt
    ceiling instead of looping forever. TRADE: because the horizon must sit above the transcode cap,
    a crash mid-transcode delays that one video's re-processing by up to the horizon — accepted, as
    a deploy landing during an active transcode is rare and the alternative (holding the txn/lock to
    keep instant rollback-recovery) is the wedge this fixes. Returns rows recovered; FLUSHES only —
    the caller owns the commit."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.MEDIA_CLAIM_STALE_SECONDS)
    rows = (
        (
            await db.execute(
                select(MediaJob)
                .where(MediaJob.state == JobState.processing, MediaJob.updated_at < cutoff)
                # Ascending by id, because the protocol acquires every class ascending and a sweep
                # is bound by that rule exactly as a request is (MEMNG-267 R2). It closes nothing
                # on its own — SKIP LOCKED never waits — but it is the order the class is written
                # down in, and an unordered set here is what a later change would inherit.
                .order_by(MediaJob.id)
                # skip_locked: a row a worker is actively finishing (its brief result txn) is theirs
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    # The attachment rows this sweep will write, taken as ONE ascending set BEFORE the loop
    # (MEMNG-267 R7a). `_fail_attachment` writes them one at a time in JOB order, autoflushed
    # inside the single transaction the caller commits, so without this the sweep acquires A in an
    # order nothing else shares — and a request that holds a memo's attachments ascending (the
    # protocol's own order) inverts against it.
    #
    # The PRECISE set, not every attachment a stale job names: a job that merely re-queues writes
    # no attachment row, and this sweep runs hourly across the whole backlog. It is decidable from
    # the rows already loaded, because `_fail_attachment` is kind-scoped and the ceiling test is
    # arithmetic. J is still acquired first, so J → A is preserved.
    terminal = sorted(
        {
            job.attachment_id
            for job in rows
            if job.kind == JobKind.derive and job.attempts + 1 >= settings.MEDIA_MAX_ATTEMPTS
        }
    )
    if terminal:
        await db.execute(
            select(Attachment.id)
            .where(Attachment.id.in_(terminal))
            .order_by(Attachment.id)
            .with_for_update()
        )
    for job in rows:
        job.attempts += 1
        job.claimed_by = None
        if job.attempts >= settings.MEDIA_MAX_ATTEMPTS:
            job.state = JobState.failed  # terminal + visible, never a silent drop
            job.error = f"stale claim recovery gave up after {job.attempts} attempts"
            await _fail_attachment(db, job)  # kind-scoped, like the failure path
            logger.warning("media_stale_claim_failed", job_id=job.id, attempts=job.attempts)
        else:
            job.state = JobState.queued
            job.error = "recovered from stale claim (worker interrupted mid-job)"
            logger.warning("media_stale_claim_requeued", job_id=job.id, attempts=job.attempts)
    await db.flush()
    return len(rows)


async def run_media_recover_safe() -> int:
    """Re-queue media jobs a dead worker left in `processing` (MEMNG-90), in its own committing
    session with ALL errors isolated — the boundary guard every periodic sweep gets, and at startup
    an escaping error would kill the worker before the loop begins. Returns rows recovered (0 on
    error)."""
    try:
        async with async_session() as db:
            recovered = await recover_stale_media_claims(db)
            await db.commit()
    except Exception:
        logger.exception("media.recover_error")
        return 0
    if recovered:
        logger.info("media_stale_claims_recovered", count=recovered)
    return recovered


async def sweep_job_progress_safe() -> int:
    """Remove progress rows no running job stands behind (MEMNG-36), in its own session with ALL
    errors isolated — the same boundary guard the other periodic sweeps get. Progress telemetry
    must never be able to halt media processing. Returns the number of rows removed (0 on error).

    The backstop for a killed worker only: run_once clears at claim time and after the outcome
    commits, so an ordinary run leaves nothing here to find."""
    try:
        swept = await media.sweep_stale_progress()
    except Exception:
        logger.exception("job_progress.sweep_error")
        return 0
    if swept:
        logger.info("job_progress_swept", count=swept)
    return swept


async def run_commit_race_reconcile_safe() -> int:
    """Reconcile memo creates that raced their own commit (MEMNG-146), in their own committing
    session with ALL errors isolated — the same boundary guard the other periodic sweeps get.

    Returns the number of poster jobs re-enqueued (0 on error). Misplaced originals are reported by
    the sweep itself rather than counted here: they are not something a worker can put right.
    """
    try:
        async with async_session() as db:
            report = await reconcile.reconcile_commit_races(db)
            await db.commit()
    except Exception:
        logger.exception("reconcile.sweep_error")
        return 0
    if report.posters_requeued or report.misplaced_originals:
        logger.info(
            "commit_race_reconciled",
            posters_requeued=len(report.posters_requeued),
            misplaced_originals=len(report.misplaced_originals),
            scanned=report.scanned,
        )
    return len(report.posters_requeued)


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


# The digest task currently in flight in THIS process, or None. Single-flight is two-layered and
# both layers are needed: this handle refuses a second spawn inside one worker (cheap, immediate),
# and the committed `digest_runs` row refuses one across processes (durable, authoritative). The
# handle alone would not survive a restart; the row alone would let one worker spawn a second task
# that then wasted a database round trip to be told no.
_digest_task: asyncio.Task[object] | None = None


def digest_task_in_flight() -> bool:
    return _digest_task is not None and not _digest_task.done()


def _clear_digest_task(task: asyncio.Task[object]) -> None:
    global _digest_task
    if _digest_task is task:
        _digest_task = None


async def digest_is_due() -> bool:
    """Read the two facts the pure gate needs, and ask it. All errors isolated.

    An error here answers "not due" rather than propagating: this runs on every poll, and a
    database hiccup must not be able to halt media processing (the same boundary guard every other
    periodic concern in this loop gets).
    """
    try:
        async with async_session() as db:
            settings_row = await digest_runner.load_settings(db)
            last = await digest_runner.last_ledger_local_date(db)
        return digest_gate.digest_due(datetime.now(UTC), settings_row, last)
    except Exception:
        logger.exception("digest.gate_error")
        return False


async def adopt_manual_digest() -> int | None:
    """Take ownership of a manual run row the admin API committed, if there is one. Errors
    isolated, like every other read this loop makes.

    The API only CLAIMS: `POST /api/admin/digest/run` inserts the row (which is the single-flight
    lock) and answers 202, because the work behind it takes minutes to hours and binding it to a
    request lifetime would make a browser tab, a proxy timeout or a reload the thing that decides
    whether the household gets its mail. This is the other half — the worker, which already owns
    the scheduled run, executing the manual one through exactly the same supervised spawn.

    The adoption is a CAS on `heartbeat_at IS NULL`, not a bare read: the committed row no longer
    protects itself with the INSERT's unique index, so "nobody has started this yet" has to be
    claimed rather than observed.
    """
    try:
        async with async_session() as db:
            run_id = await digest_runner.pending_manual_run_id(db)
            if run_id is None:
                return None
            if not await digest_runner.claim_run(db, run_id):
                return None
        return int(run_id)
    except Exception:
        logger.exception("digest.manual_adopt_error")
        return None


def spawn_digest() -> bool:
    """Start a digest run as a SUPERVISED BACKGROUND TASK. Returns whether one was started.

    Never awaited inline, and that is the whole point. This loop is serial — it drains a media job,
    drains a frame-share job and stamps the heartbeat in one pass — while a digest run makes SMTP
    sessions and Dropbox calls whose upload-session operations alone allow 600 s each. Awaiting it
    would stall media and frame drains for minutes and starve the heartbeat, and the worker
    healthcheck's 30-minute staleness bar plus `autoheal=true` would eventually kill the container
    MID-SEND — which is exactly the crash window the delivery guarantee is about. Every blocking
    call inside the task goes through `to_thread.run_sync`, so the loop keeps beating for the whole
    run.
    """
    global _digest_task
    if digest_task_in_flight():
        return False
    task = asyncio.create_task(digest_runner.run_scheduled_digest_safe())
    task.add_done_callback(_clear_digest_task)
    _digest_task = task
    return True


def spawn_claimed_digest(run_id: int) -> bool:
    """Start an ALREADY-COMMITTED run (the manual endpoint's row) as the same supervised task.

    Same handle, same single-flight, same never-awaited-in-the-loop rule as `spawn_digest` — the
    only difference is which entry point the task runs, because the row already exists.
    """
    global _digest_task
    if digest_task_in_flight():
        return False
    task = asyncio.create_task(digest_runner.run_claimed_digest_safe(run_id))
    task.add_done_callback(_clear_digest_task)
    _digest_task = task
    return True


async def sweep_digest_safe() -> None:
    """The two periodic digest sweeps, on the existing reap cadence.

    BOTH go through an error-isolated entry point. This loop is serial and unguarded: an exception
    escaping either sweep propagates out of `run()` and stops the worker outright — no heartbeat,
    no media drain, no frame drain — which is the optional digest feature taking the media pipeline
    down with it, the one failure every `*_safe` helper in this module exists to prevent.
    """
    await digest_runner.sweep_stale_runs_safe()
    await digest_runner.sweep_hosted_media_safe()


class _DrainHealth:
    """Heartbeat honesty for the media loop (MEMNG-90). The loop stamps a liveness file every pass,
    which stayed green even while every drain failed — the wedge hid for ~5 h. An ordinary job
    failure is handled inside run_once and returns processed/deferred, so it never reaches here;
    this counts only run_media_drain() == 'error' — an exception that ESCAPED run_once (a DB outage,
    a bug), the systemic state that should surface. After `threshold` consecutive such errors the
    loop stops stamping so the Docker healthcheck can trip; a non-error drain resets the streak."""

    def __init__(self, threshold: int) -> None:
        self._streak = 0
        self._threshold = threshold

    def record(self, status: str) -> None:
        self._streak = self._streak + 1 if status == "error" else 0

    @property
    def healthy(self) -> bool:
        return self._streak < self._threshold

    @property
    def streak(self) -> int:
        return self._streak


async def run() -> None:
    logger.info("worker_started", version=__version__, worker_id=WORKER_ID)
    # Say what the thermal guard is actually reading, ONCE per process and here rather than in the
    # guard: this runs before any consumer task exists, so it needs no latch, no lock and no
    # module-level "have I logged this" flag that two concurrent drains could race on — and it
    # cannot become a flood the way a line inside the per-job guard would. UNKNOWN is a real
    # answer worth seeing in the log: it means the worker is draining with no thermal protection,
    # which is correct on a dev host and would be a finding in production.
    logger.info(
        "worker_thermal_sensor",
        state=str(media.thermal_state()),
        sensor=str(media.thermal_sensor() or ""),
        max_c=settings.THERMAL_MAX_C,
    )
    beat()  # blocking-ok: the liveness stamp itself — see beat()
    # Schema gate (MEMNG-86): a deploy recreates app + worker together, but only the app migrates.
    # Wait for the live schema to reach the head this image was built with before running the
    # recovery sweep or the loop — otherwise the sweep hits UndefinedColumnError against the old
    # schema. Raises (worker exits, Docker restarts) if the schema never catches up.
    await migrations.wait_for_migrated_schema()
    # Startup sweeps: the common stranding cause is this very restart (a deploy --force-recreate
    # killing work mid-flight), so adopt orphaned claims — frame-share AND media (MEMNG-90) — before
    # draining anything new.
    await run_frame_share_recover_safe()
    await run_media_recover_safe()
    health = _DrainHealth(settings.WORKER_UNHEALTHY_ERROR_STREAK)
    last_reap = 0.0
    while True:
        status = await run_media_drain()  # own session, all errors isolated from this loop
        health.record(status)
        # Heartbeat honesty (MEMNG-90): stamp only while the media loop is not hard-erroring in a
        # streak, so a systemically failing worker (e.g. DB down) trips the healthcheck instead of
        # looking alive.
        if health.healthy:
            beat()  # blocking-ok: the liveness stamp itself — see beat()
        else:
            logger.error("worker_unhealthy_error_streak", streak=health.streak)
        # Drain a frame-share job opportunistically in its own session (like reap): the album upload
        # is decoupled from the media job's transaction so an upload error can't poison media work.
        frame_status = await run_frame_share_drain()
        if health.healthy:
            beat()  # blocking-ok: the liveness stamp itself — see beat()
        # The digest: a cheap gate read, and a SPAWN when it fires. The loop continues draining
        # immediately — see spawn_digest for why this is never awaited here. A manual row the
        # admin API committed is picked up FIRST and by the same spawn: while one is in flight the
        # gate answers not-due anyway (its local_date is in the ledger), so the order costs a
        # scheduled run nothing and gives an operator's "Run now" the shortest path to starting.
        if not digest_task_in_flight():
            manual_run_id = await adopt_manual_digest()
            if manual_run_id is not None:
                if spawn_claimed_digest(manual_run_id):
                    logger.info("digest_run_spawned", trigger="manual", run_id=manual_run_id)
            elif await digest_is_due():
                if spawn_digest():
                    logger.info("digest_run_spawned", trigger="scheduled")
        if time.time() - last_reap > REAP_INTERVAL_SECONDS:
            reaped = await reap_expired_committed()
            if reaped:
                logger.info("uploads_reaped", count=reaped)
            # Piggybacks the reap cadence: a claim orphaned by something other than a restart
            # (an OOM-killed send that leaves the worker alive) is recovered within the hour —
            # frame-share and media alike.
            await run_frame_share_recover_safe()
            await run_media_recover_safe()
            # Same cadence for the progress side channel: a killed worker cannot clear its own
            # row, so the anti-join sweep keeps the table bounded by what is actually running.
            await sweep_job_progress_safe()
            # …and for the commit-race window (MEMNG-146), which no in-request bookkeeping can
            # close: a stored video whose poster the unwind discarded gets its fast lane back.
            await run_commit_race_reconcile_safe()
            # The digest's two sweeps ride the same cadence: a run whose process died is marked
            # `failed` (so the 26 h stale alert can see it), and hosted media past its retention
            # is deleted from Dropbox.
            await sweep_digest_safe()
            last_reap = time.time()
        if status != "processed" and frame_status != "processed":  # idle/deferred → back off
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
