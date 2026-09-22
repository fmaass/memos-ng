"""memos-ng media worker: claims media_jobs (FOR UPDATE SKIP LOCKED) and runs them.

Same image as the app, started with `command: worker`. Each worker drains the lanes in
WORKER_LANES: the fast lane (thumbs/mobile/poster/blurhash/dims) and/or the slow lane (the
CPU-heavy H.264 transcode). Deployed as ONE in-stack worker running both lanes, recreated
atomically with the app container from the same compose stack — schema and code move together,
so no mixed-version worker ever runs against a newer app. (The Saturn-fast/Merkur-slow split
this replaced is historical; see ADR-0006.)

Inside that one process the queue is drained by two consumer tasks, not one serial loop: the
`transcode` class (a slow-lane derive job on a video) and the `light` class (everything else),
each claiming only its own class, so a transcode that runs for minutes cannot hold the photos
queued behind it. run() supervises them and owns the heartbeat, which is why the stamp can stop
for a consumer that died or went quiet while the other is still working.
"""

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
from app.services import frame_share, media, push, reclaim, reconcile
from app.services.digest import gate as digest_gate
from app.services.digest import runner as digest_runner
from app.services.uploads import reap_expired_committed
from app.version import __version__

configure_logging()  # the worker is where a coordinate is written, so it renders logs the same way
logger = structlog.get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
POLL_SECONDS = 10
REAP_INTERVAL_SECONDS = 3600
# How long a detached attachment is left alone before the sweep below treats it as garbage. It has
# to outlast the longest gap between a request committing its detach and that request reclaiming
# what it detached — the two are deliberately in separate transactions (`app/services/reclaim.py`),
# so in between, a row a live request is about to clean up looks exactly like one nobody wants. An
# hour is far more than that gap and is the cadence the sweep already rides, so the wait costs the
# user nothing: the row is off their memo the moment the request commits, whatever happens to it
# afterwards.
DETACH_GRACE_SECONDS = REAP_INTERVAL_SECONDS
# The worker healthcheck's staleness bar, restated here because the bounds below are chosen
# against it: the compose healthcheck calls the worker unhealthy once the heartbeat is older than
# this, and `autoheal=true` then RECREATES the container — with two consumers mid-job. It is not
# read from here (the healthcheck is compose's), it is the budget every bound is spent out of.
HEARTBEAT_STALE_SECONDS = 1800
# The TOTAL bound on each step run() awaits on its OWN coroutine, in seconds. Every such step is
# time between two heartbeat stamps, so an unbounded one is a healthy worker killed by its own
# healthcheck — which is what these exist to prevent. Named here rather than inline so the whole
# budget is reviewable in one place and a later edit has to move a number somebody can see.
#
# Not settings: they are the shape of THIS loop (which step can hold the stamp, and for how long),
# not a per-deployment knob, and an operator raising one in the environment would be raising it
# past the healthcheck bar with nothing to stop them. Each is generous against the work it covers
# — a bound reached is an incident, never routine pacing.
HOUSEKEEPING_BOUNDS: dict[str, int] = {
    # One frame-share job: a claim, an external album send, a commit. The send is the slow part
    # and the only one that can hang on somebody else's server.
    "frame_share_drain": 120,
    # Two indexed reads and a CAS on one row. Generous by two orders of magnitude; anything near
    # this is the database being unreachable, not a slow query.
    #
    # What THIS bound can cost, written down because nothing in the code recovers it: the CAS is
    # the claim, and `claim_run` commits it inside the call. A database that stalls for ~30 s
    # exactly across that commit lets the bound expire AFTER the row was claimed, so the supervisor
    # reads the fallback `None`, never spawns the run, and no later poll can adopt it — the claim
    # is `heartbeat_at IS NOT NULL` and `digest_runs` records no owner, so a claimed-but-unspawned
    # row is indistinguishable from one a live worker is executing, and adopting it on a hunch is
    # how two workers send the same digest. The operator loses that one "Run now": the stale-run
    # sweep marks the row `failed` after 15 minutes of silence, and pressing Run now again then
    # works (the single-inflight index only refuses while the dead row is still NULL-status). Worth
    # more than it costs to fix: recovering it automatically needs an owner column and another
    # query on this poll path, to close a window that a 30 s database stall has to open first.
    "adopt_manual_digest": 30,
    "digest_is_due": 30,
    # Deletes expired upload sessions and unlinks their part files under plain FOR UPDATE (no SKIP
    # LOCKED): it can legitimately WAIT on a finalize that holds the row, and that wait is the
    # reason this one needs a bound at all.
    "reap_expired_committed": 120,
    # Two bounded UPDATEs over one index each, at startup and on the reap cadence.
    "frame_share_recover": 60,
    "media_recover": 60,
    # An anti-join delete over the progress side channel — telemetry, and the smallest sweep here.
    "job_progress_sweep": 60,
    # Scans recent memos and re-enqueues poster jobs; the scan is bounded by the window, not by
    # the table.
    "commit_race_reconcile": 120,
    # One indexed read, then a locked reclaim per candidate — each of which stats a directory on
    # the data volume, so the cost is the backlog, not the table. Deliberately modest: the sweep is
    # idempotent and hourly, so a pass cut short mid-backlog finishes next hour, and the rows it
    # did reach are already committed one by one.
    "detached_attachment_sweep": 45,
    # The largest by far, and the step the ticket was raised on: the hosted-media retention sweep
    # deletes from Dropbox with per-operation timeouts but no total, so a big expiry batch against
    # a slow remote is exactly the unbounded case. Missing a sweep costs a cycle of retention
    # lateness; missing a heartbeat costs the container.
    "digest_sweep": 240,
}
# Under half the healthcheck bar, and asserted in the tests so a later edit cannot quietly exceed
# it. It is a deliberate OVER-estimate of any real gap: no single stamp-to-stamp gap contains every
# step (the startup pair runs before the loop, the hourly block only on the reap cadence, and the
# two digest-gate reads are the only pair that can both be spent in one pass), so the sum bounds
# the worst gap with room for the POLL_SECONDS back-off on top.
HOUSEKEEPING_BOUND_TOTAL_SECONDS = sum(HOUSEKEEPING_BOUNDS.values())
ERROR_TEXT_LIMIT = 500  # media_jobs.error is Text, but a stack-sized message helps nobody
# The signals a deliberate stop arrives as: SIGTERM is what `docker compose up -d
# --force-recreate` sends (the entrypoint `exec`s this process, so it arrives here and not at a
# shell), SIGINT is the same stop from a terminal.
STOP_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)
# The stop path's own bounds, and the budget they were chosen against: the stack compose sets no
# `stop_grace_period`, so docker allows 10 s between SIGTERM and SIGKILL. A consumer stopping a
# transcode spends the ffmpeg tree stop's TERM step first (media.KILL_GRACE_SECONDS = 5 s), then
# up to 3 s returning the claim, then up to 1 s on the progress row — 9 s of the 10, and the two
# consumers run their own in parallel rather than one after the other.
#
# DESIGNED bounds, not hard caps on the process: an ffmpeg that also has to be SIGKILLed adds a
# second 5 s step, and nothing here can bound the kernel. The 9 s bounds the CONSUMERS' stop path
# and nothing else — what follows it is unmeasured and unbounded by this code: the consumers'
# sessions closing, asyncpg tearing down a connection whose operation was cancelled, the
# supervisor unwinding, and asyncio.run()'s own shutdown of whatever tasks are left (which
# re-cancels an abandoned requeue write without honouring the bound above). Docker's SIGKILL at
# the end of the grace is the only deadline over the whole stop; a second SIGTERM meanwhile is
# deliberately a no-op, so it cannot shorten it either. Past the grace docker sends SIGKILL and
# nothing can be written — a claim that was in flight then stays `processing` for the stale-claim
# sweep (MEDIA_CLAIM_STALE_SECONDS), exactly as an OOM kill leaves it, while a stop that happened
# to be holding no claim leaves nothing behind at all.
CANCEL_REQUEUE_TIMEOUT_SECONDS = 3.0
CANCEL_PROGRESS_CLEAR_SECONDS = 1.0

# A claim-time PREDICATE, not a column and not a row value: nothing is stored, and the class exists
# only in the WHERE of a claim and as the name of the consumer that issued it. That is what lets
# the split ship with no migration and no new enum value, and what makes a rollback image — whose
# claim has no class term — drain the very same rows serially.
JobClass = Literal["transcode", "light"]
# Both in-flight states block a successor, so there is no window between an earlier job's claim and
# its commit: a claimer reading its own snapshot still sees the predecessor as `queued`. Terminal
# states never block.
#
# Deliberately NARROWER than reconcile's IN_FLIGHT_JOBS, which also lists `claimed`: there the
# extra state only SUPPRESSES a requeue, while here it would WITHHOLD a claim. Nothing in the
# media queue ever writes `claimed` (the claim writes `processing`) and nothing would ever clear it
# (the claim selects `queued`, the stale sweep selects `processing`), so waiting on it would strand
# every later job of that attachment for good.
IN_FLIGHT_STATES = (JobState.queued, JobState.processing)


def validate_worker_slots() -> None:
    """Refuse a consumer configuration this release cannot honour. Fails loud at startup.

    The product being checked is a CPU BUDGET, never a thread count — see WORKER_TRANSCODE_CPU_COST
    for why `slots x FFMPEG_THREADS` would bound nothing.
    """
    slots = {
        "WORKER_TRANSCODE_SLOTS": settings.WORKER_TRANSCODE_SLOTS,
        "WORKER_LIGHT_SLOTS": settings.WORKER_LIGHT_SLOTS,
    }
    for name, count in slots.items():
        if count < 1:
            raise ValueError(
                f"{name}={count}: a zero-slot class starves that class silently — one worker "
                "process is the whole deployment, so nothing else would ever drain it"
            )
        if count > 1:
            raise ValueError(
                f"{name}={count}: slots above 1 are not supported in this release (uid-keyed temp "
                "files, synchronous image work on the event loop, and a 5+5 connection pool all "
                "have to be addressed first)"
            )

    # Checked BEFORE the arithmetic below, which a nonpositive number would quietly defeat: a
    # negative cost is SUBTRACTED from the demand and lets any configuration through.
    if settings.WORKER_TRANSCODE_CPU_COST < 1:
        raise ValueError(
            f"WORKER_TRANSCODE_CPU_COST={settings.WORKER_TRANSCODE_CPU_COST} must be at least 1: "
            "a transcode cannot cost no cores, and a negative cost would cancel the budget out"
        )
    if settings.WORKER_CPU_BUDGET < 1:
        raise ValueError(
            f"WORKER_CPU_BUDGET={settings.WORKER_CPU_BUDGET} must be at least 1: a worker with no "
            "cores to spend cannot run a consumer at all"
        )
    if settings.FFMPEG_THREADS < 1:
        raise ValueError(
            f"FFMPEG_THREADS={settings.FFMPEG_THREADS} must be at least 1: 0 is ffmpeg's "
            "'choose the thread count yourself', which leaves the CPU budget below with no "
            "encoder figure to be checked against"
        )

    demand = (
        settings.WORKER_TRANSCODE_SLOTS * settings.WORKER_TRANSCODE_CPU_COST
        + settings.WORKER_LIGHT_SLOTS
    )
    if demand > settings.WORKER_CPU_BUDGET:
        raise ValueError(
            f"configured consumers need {demand} cores but WORKER_CPU_BUDGET="
            f"{settings.WORKER_CPU_BUDGET}"
        )
    if settings.WORKER_TRANSCODE_CPU_COST < settings.FFMPEG_THREADS:
        raise ValueError(
            f"WORKER_TRANSCODE_CPU_COST={settings.WORKER_TRANSCODE_CPU_COST} is below "
            f"FFMPEG_THREADS={settings.FFMPEG_THREADS}: the nominal encoder threads alone already "
            "exceed the measured per-transcode cost the budget is computed from"
        )

    bounds = {
        "WORKER_TRANSCODE_STALL_SECONDS": settings.WORKER_TRANSCODE_STALL_SECONDS,
        "WORKER_LIGHT_STALL_SECONDS": settings.WORKER_LIGHT_STALL_SECONDS,
    }
    for name, bound in bounds.items():
        if bound <= POLL_SECONDS:
            raise ValueError(f"{name}={bound} must exceed POLL_SECONDS={POLL_SECONDS}")
        if bound >= settings.MEDIA_CLAIM_STALE_SECONDS:
            # Past the horizon the stale sweep would requeue a job the supervisor has not yet
            # given up on, and the two recoveries would fight over the same row.
            raise ValueError(
                f"{name}={bound} must stay below "
                f"MEDIA_CLAIM_STALE_SECONDS={settings.MEDIA_CLAIM_STALE_SECONDS}"
            )


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


def _transcode_class_predicate():
    """The TRANSCODE class: a slow-lane derive job whose attachment is a video.

    A correlated EXISTS in WHERE, never a join. Postgres applies a locking clause only to rows
    coming from the outer query's FROM list, so this subquery takes no lock at all — `attachments`
    is read unlocked and the job-then-attachment order every co-holding path acquires in stays
    untouched.

    A join would put `attachments` in that FROM list, where a bare locking clause would lock it
    too; the `of=MediaJob` the claim carries would in fact stop that. The subquery form is kept
    because it needs no such care, and because it leaves the claim's FROM list — and with it the
    lock census that is keyed on this statement — exactly as it already was.
    """
    return and_(
        MediaJob.lane == JobLane.slow,
        MediaJob.kind == JobKind.derive,
        select(1)
        .where(Attachment.id == MediaJob.attachment_id, Attachment.mime.like("video/%"))
        .correlate(MediaJob)
        .exists(),
    )


def class_predicate(job_class: JobClass):
    """The claim-time predicate for one class. `light` is the EXACT NEGATION of `transcode`.

    Written as a negation rather than as a positive list so the two classes partition the queue
    with no gap and no overlap: a row whose attachment is already gone (media_jobs carries no
    foreign key) matches no positive mime list, and would fall through both classes forever. As
    the negation it lands in `light`, where the existing discard path retires it.
    """
    predicate = _transcode_class_predicate()
    return predicate if job_class == "transcode" else ~predicate


def _order_guard(lanes: list[JobLane]):
    """No job may be claimed while an EARLIER job for the same attachment is still in flight.

    Two consumers make this necessary: the repository importer writes a fast poster job and a slow
    transcode job for one underived video in a single flush, with no ordering between them, and
    those two jobs' attachment-status writes are not order-independent — whichever finished last
    would decide what the owner sees. The serial loop gave this ordering away for free; the guard
    restores it explicitly rather than arguing the pair cannot arise.

    Scoped by `prior.lane IN (lanes)` on purpose: a worker orders only against jobs it could itself
    claim, so a lower-id job in a lane nobody here drains cannot block its successor forever.
    """
    prior = aliased(MediaJob, name="prior")
    return ~(
        select(1)
        .where(
            prior.attachment_id == MediaJob.attachment_id,
            prior.id < MediaJob.id,
            prior.state.in_(IN_FLIGHT_STATES),
            prior.lane.in_(lanes),
        )
        .correlate(MediaJob)
        .exists()
    )


async def claim_one(
    db: AsyncSession,
    lanes: list[JobLane] | None = None,
    job_class: JobClass | None = None,
) -> MediaJob | None:
    """Atomically claim the next queued job in this worker's lanes; concurrent workers skip a
    locked row. `lanes` defaults to the configured WORKER_LANES, so a job is claimed by exactly
    one worker. `job_class` narrows the claim to one consumer's class; omitted, the statement is
    today's — which is what keeps every existing caller and its call shape honest."""
    if lanes is None:
        lanes = parse_worker_lanes(settings.WORKER_LANES)
    where = [MediaJob.state == JobState.queued, MediaJob.lane.in_(lanes), _order_guard(lanes)]
    if job_class is not None:
        where.append(class_predicate(job_class))
    # The locking clause stays lexically HERE, and `of=MediaJob` is written out so the intent —
    # lock the queue row and nothing else — survives a later edit to the subqueries above. The
    # lock census names this one site by its enclosing def, so moving it would move the census.
    stmt = (
        select(MediaJob)
        .where(*where)
        .order_by(MediaJob.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=MediaJob)
    )
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is not None:
        job.state = JobState.processing
        # The class is a claim-time predicate and lives on no column, so the only place a claimed
        # row can say WHICH consumer took it is the claim identity: a stale `processing` row is
        # otherwise indistinguishable between the two consumers of one worker process.
        job.claimed_by = WORKER_ID if job_class is None else f"{WORKER_ID}/{job_class}"
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
        if _stop_in_flight():
            # An error raised while a cancellation was unwinding this call REPLACES it — the
            # session's own exit is one await that can, closing a connection whose operation was
            # just cancelled. Swallowing that error here would swallow the stop with it, and the
            # consumer would go round its loop again while the supervisor's teardown waits for it.
            raise asyncio.CancelledError from exc


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


def _stop_in_flight() -> bool:
    """Is a cancellation being delivered to this task right now?

    `Task.cancelling()` counts cancels requested and not un-requested; nothing in the worker
    uncancels, and the bounded `asyncio.timeout` blocks it passes through restore their own count,
    so inside a consumer this is 0 during ordinary work and 1 from the moment the stop reaches it.
    What it decides is whether a piece of best-effort cleanup may still take unbounded time.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _requeue_claim_conditional(job_id: int, claim: str) -> int:
    """Return THIS claim to `queued` in a session of its OWN; answers how many rows it changed.

    CONDITIONAL on the row still being `processing` under this exact `claimed_by`, because the
    cancel path cannot know where in the attempt the stop landed: a cancellation delivered in the
    push notification that follows the `done` commit, or in a failure branch that already
    committed its own outcome, would otherwise resurrect a finished job as `queued` and run it a
    second time. Zero rows changed is the correct, silent answer for a row that is done, failed,
    deleted, or has since been claimed by somebody else.

    Its OWN session, not the consumer's: the consumer's session is being torn down around this
    call (a statement of ITS may have just been cancelled mid-flight), and a write that shared it
    could be interrupted by that teardown or interrupt it in turn. `attempts` is untouched — a
    deploy is not a failed attempt.
    """
    async with async_session() as db:
        # RETURNING rather than rowcount, as everywhere else here: the id that comes back IS the
        # row this write claimed to have moved, and it is typed.
        moved = (
            (
                await db.execute(
                    update(MediaJob)
                    .where(
                        MediaJob.id == job_id,
                        MediaJob.state == JobState.processing,
                        MediaJob.claimed_by == claim,
                    )
                    .values(state=JobState.queued, claimed_by=None)
                    .returning(MediaJob.id)
                )
            )
            .scalars()
            .all()
        )
        await db.commit()
        return len(moved)


async def _requeue_cancelled_claim(job_id: int, claim: str) -> None:
    """Run that write while THIS task is being CANCELLED — the deploy stop.

    Two bounds, both because the container this runs in is already stopping:

    * SHIELDED — a second cancellation (a second TERM, an impatient caller) must not interrupt the
      write half-done, or the row this exists to free stays `processing` after all.
    * TIMED OUT at CANCEL_REQUEUE_TIMEOUT_SECONDS — a database that does not answer must not hold
      the process past docker's stop grace. Past the bound the row stays `processing` and the
      stale-claim sweep is the backstop, which is where a SIGKILL would have left it anyway. The
      abandoned write owns nothing but its own session, so whatever it does while unwinding cannot
      reach the session the consumer is closing behind it.

    Every failure is swallowed on purpose, cancellation included: run_once re-raises the
    cancellation immediately after this returns, so it cannot be masked on the way out — while an
    error raised from HERE would replace it, be caught by run_media_drain's `except Exception`,
    and leave the consumer draining on while the supervisor waits for it to end.
    """
    write = asyncio.ensure_future(_requeue_claim_conditional(job_id, claim))
    try:
        async with asyncio.timeout(CANCEL_REQUEUE_TIMEOUT_SECONDS):
            rows = await asyncio.shield(write)
        if rows:
            logger.info("media_job_requeued_on_cancel", job_id=job_id, claim=claim)
        else:
            # Not this attempt's row any more: finished, failed, deleted or re-claimed.
            logger.info("media_job_cancel_requeue_skipped", job_id=job_id, claim=claim)
    except (TimeoutError, asyncio.CancelledError):
        write.cancel()  # the bound reached, or a second cancel — let the stop proceed
        logger.warning("media_job_cancel_requeue_unfinished", job_id=job_id, claim=claim)
    except Exception:
        logger.exception("media_job_cancel_requeue_error", job_id=job_id, claim=claim)


async def _clear_progress_on_stop(job_id: int) -> None:
    """The progress side channel's cleanup, BOUNDED, on the stop path.

    Unbounded (as the post-outcome clear in run_once is) a lock wait here would spend the rest of
    docker's grace after the requeue has already succeeded — and this is telemetry. Past the bound
    the row simply outlives the stop: the next attempt's claim-time clear in run_once removes it
    before it derives anything, which is the same path that covers a killed worker.
    """
    clear = asyncio.ensure_future(media.clear_progress(job_id))
    try:
        async with asyncio.timeout(CANCEL_PROGRESS_CLEAR_SECONDS):
            await asyncio.shield(clear)
    except (TimeoutError, asyncio.CancelledError):
        clear.cancel()
        logger.warning("media_job_cancel_progress_clear_unfinished", job_id=job_id)
    except Exception:
        logger.exception("media_job_cancel_progress_clear_error", job_id=job_id)


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


async def run_once(db: AsyncSession, job_class: JobClass | None = None) -> str:
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
    # The class travels down ONLY when one was given: with none, this is byte-for-byte today's
    # call, which is what lets a caller that knows nothing of classes keep working unchanged.
    job = await claim_one(db) if job_class is None else await claim_one(db, job_class=job_class)
    if job is None:
        await db.rollback()
        return "idle"
    attachment_id = job.attachment_id  # captured before the commits below
    job_id = job.id  # ...and the progress side channel is keyed on it
    job_kind = job.kind  # ...decides whether this job's outcome is the PHOTO's outcome
    # The claim identity THIS attempt owns, read BEFORE the commit rather than after it: claim_one
    # has already written it, the session does not expire it, and reading it here is what lets the
    # commit itself sit under the handler below.
    claim = job.claimed_by
    # One cancellation handler around the claim commit and EVERYTHING that follows it — the
    # claim-time clear, the work itself, every outcome branch and the post-outcome clear. A stop can
    # land in any of them, and each one is a window in which the row is `processing` with nobody
    # left to finish it. The COMMIT is inside for the sharpest of those windows: a cancellation
    # delivered while it is in flight, after Postgres accepted it, is raised out of the commit
    # itself, and with the handler below it would go past the hand-back and strand the very row the
    # commit had just made durable. The handler's write is conditional, so it is safe wherever the
    # stop landed — a commit that never reached the database leaves a row that is still `queued`
    # for everyone else, which that write matches not at all.
    try:
        # Commit the claim BEFORE processing (see docstring): `processing` becomes durable and the
        # media_jobs row lock is released for the transcode's whole duration.
        await db.commit()
        # Cleanup BEFORE processing (MEMNG-36): drop any progress row a dead attempt left behind,
        # so the snapshot never reports a percentage that goes backwards. Its own committing
        # session; a no-op delete on a fast-lane job that never published.
        await media.clear_progress(job_id)
        try:
            await media.process_job(db, job)
            job.state = JobState.done
            job.progress_pct = 100
            await db.commit()
            if job_kind == JobKind.derive:
                # Only the pipeline that PRODUCES the photo announces it. A printready job leaves
                # the attachment exactly as it found it (already `ready`), so pushing here would
                # send the owner a second "your photo is ready" per photo — hundreds per book.
                await _notify_terminal_safe(attachment_id)  # own session, after job is durable
            return "processed"
        except media.ThermalDefer:
            await _requeue_claim(db, job_id)  # too hot — back to queued, not a failure
            return "deferred"
        except media.AttachmentGone as exc:
            # The attachment was deleted while this job ran (MEMNG-41): nothing to derive or
            # notify, no attempt to count. guard_publish already swept this run's output; discard
            # the job row.
            await _discard_claim(db, job_id, attachment_id, str(exc))
            return "processed"
        except Exception as exc:  # a bad input must not kill the worker
            return await _record_job_failure(db, job_id, attachment_id, job_kind, exc)
        finally:
            # Cleanup AFTER the outcome is committed — every branch above commits before it
            # returns. Clearing the row from inside the transcode instead (media.process_slow)
            # opened a window between that clear and this transaction's commit in which the
            # snapshot fell back to the job row's committed progress (0) while the attachment
            # still read `processing`: the client saw its transcode reach 100%, snap back to 0%,
            # and only then turn ready (MEMNG-36). After the commit there is no such window.
            #
            # NOT on the stop path: there the handler below clears the row under a bound instead,
            # because an unbounded delete here would spend grace the requeue has already earned.
            if not _stop_in_flight():
                await media.clear_progress(job_id)
    except asyncio.CancelledError:
        # A DELIBERATE stop — SIGTERM → the supervisor's teardown → this consumer's cancellation —
        # arriving anywhere after the claim was committed. Without this the row keeps `processing`
        # and a claim identity no live process answers to, and nothing requeues it until
        # MEDIA_CLAIM_STALE_SECONDS (2 h), during which _order_guard also withholds every LATER
        # job of the same attachment. Both consumers get it: a recreate strands one row per class
        # otherwise. The requeue comes first because it is what the grace exists for; the progress
        # row is telemetry and follows. The cancellation keeps propagating — a swallowed stop is a
        # worker that keeps draining while the supervisor waits for it.
        if claim is not None:
            await _requeue_cancelled_claim(job_id, claim)
        await _clear_progress_on_stop(job_id)
        raise


async def run_media_drain(job_class: JobClass | None = None) -> str:
    """Drain one media job in its own session, isolating ALL errors from run()'s loop.

    `job_class` narrows the drain to one consumer's class; omitted, the drain is today's.

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
            # Same forwarding rule as run_once: no class given, no keyword passed on.
            if job_class is None:
                return await run_once(db)
            return await run_once(db, job_class=job_class)
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


async def sweep_detached_attachments(
    *, unlink: Callable[[list[Path]], list[str]] | None = None, now: datetime | None = None
) -> int:
    """Reclaim the attachments that have been off their memo longer than the grace period.

    Nothing else finds them. The commit-race reconcile wants a memo (`app/services/reconcile.py`)
    and the leaked-orphan predicate wants an original under a dated memo folder
    (`app/services/reclaim.py`) — which an original imported from 0.29.1 never has, because it
    stays flat. What both are reaching for is "something detached this and never came back", and
    the detach instant says that directly, for an import and a native upload alike.

    The cutoff is computed ONCE per pass and carried into each reclaim, so selection and
    revalidation judge every row against the same instant. Discovery holds no lock; the reclaim
    then takes the row's jobs and the row itself, in that order, in its own transaction. The
    attachment row is NOT skipped when locked: a row a dedup is holding is one this pass must wait
    for and re-read, because skipping it would leave the half-reclaimed row a later dedup could
    still adopt.

    Returns the number of rows reclaimed. `unlink` is the seam the sweep's tests inject at.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=DETACH_GRACE_SECONDS)
    async with async_session() as db:
        candidates = list(
            (
                await db.execute(
                    select(Attachment.id)
                    .where(
                        Attachment.memo_id.is_(None),
                        Attachment.detached_at.is_not(None),
                        Attachment.detached_at <= cutoff,
                    )
                    .order_by(Attachment.id)
                )
            )
            .scalars()
            .all()
        )
    reclaimed = 0
    deadline = time.monotonic() + HOUSEKEEPING_BOUNDS["detached_attachment_sweep"]
    for attachment_id in candidates:
        if time.monotonic() >= deadline:
            logger.info(
                "detached_attachments.sweep_deadline",
                reclaimed=reclaimed,
                candidates=len(candidates),
            )
            break
        # SHIELDED, and the session is opened INSIDE the shielded unit: the bound's cancellation
        # may not land between the row's lock and the end of its unlink. There it would unwind the
        # transaction — releasing the lock and putting the row back — while the thread doing the
        # unlinking runs on to completion, which is the one outcome `reclaim` is built to prevent:
        # a row that is live again and files that are gone. Shielded, the cancellation is delivered
        # HERE instead, the row's own transaction finishes off the loop with its lock held, and the
        # pass ends at a row boundary.
        result = await asyncio.shield(_reclaim_detached_row(attachment_id, cutoff, unlink))
        if result.outcome == "reclaimed":
            reclaimed += 1
    return reclaimed


async def _reclaim_detached_row(
    attachment_id: int, cutoff: datetime, unlink: Callable[[list[Path]], list[str]] | None
) -> reclaim.ReclaimResult:
    """One candidate row's reclaim, session and all, as ONE unit the sweep can shield.

    The session lives in here rather than around the shield so that the lock, the unlink and the
    commit-or-rollback are inseparable: everything this row holds is released by this coroutine
    finishing, never by a cancellation arriving in the middle of it.
    """
    async with async_session() as db:
        return await reclaim.reclaim_attachment(
            db,
            attachment_id,
            reclaim.Revalidate.DETACHED,
            unlink=unlink,
            detached_before=cutoff,
        )


async def sweep_detached_attachments_safe() -> int:
    """Reclaim detached attachments past their grace period, in sessions of its own with ALL errors
    isolated — the same boundary guard the other periodic sweeps get. Returns rows reclaimed.

    The count is logged on every pass this returns from, zero and the caught-error path included: a
    pass that found nothing and a worker that never reached its hourly block are otherwise the same
    silence, and this sweep deletes rows, so what it did each hour is the thing an operator needs on
    the record. (A pass its bound cancels returns nothing and logs no count; `_bounded` announces
    that one by name as `worker_housekeeping_timeout`.)
    """
    reclaimed = 0
    try:
        reclaimed = await sweep_detached_attachments()
    except Exception:
        logger.exception("detached_attachments.sweep_error")
    logger.info("detached_attachments_swept", count=reclaimed)
    return reclaimed


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


def _now() -> float:
    """The one clock the watermarks and the liveness verdict read.

    An indirection, not a wrapper for its own sake: the stall bound is the only part of the
    supervisor whose verdict depends on how much time has passed, so a proof of it has to be able
    to decide when time moves. Reading `time.time()` directly made those tests measure the host's
    scheduler instead: a pause longer than the bound failed them with nothing wrong.
    """
    return time.time()


class _Watermark:
    """The last moment a consumer was observably alive, read by the supervisor's beat gate.

    Death and systemic error are what `_DrainHealth` and `task.done()` see; a consumer suspended
    forever INSIDE an awaited operation is neither — it is simply never heard from again. Without
    this the supervisor would keep reporting a hung worker healthy, which is the hole the old
    single-job loop closed only by accident (it could not stamp while it was stuck).
    """

    def __init__(self) -> None:
        self.at = _now()

    def stamp(self) -> None:
        self.at = _now()


async def consume_media(job_class: JobClass, health: _DrainHealth, watermark: _Watermark) -> None:
    """Drain ONE class forever. One of these runs per class, each in its own task.

    The watermark is stamped on BOTH sides of the drain, so an idle pass and a thermally deferred
    pass re-stamp it: neither is a stall, and only a pass that never comes back lets the elapsed
    time grow. `health` is this consumer's own, never shared — a shared one would let this
    consumer's success reset the other's error streak and hide a class that fails systemically.
    """
    while True:
        watermark.stamp()
        status = await run_media_drain(job_class)  # own session, all errors isolated from the task
        watermark.stamp()
        health.record(status)
        if status != "processed":  # idle/deferred/error → back off, like the old single loop
            await asyncio.sleep(POLL_SECONDS)


_Consumers = dict[JobClass, tuple[asyncio.Task[None], _DrainHealth, _Watermark]]


def _consumers_healthy(
    consumers: _Consumers,
    stall_bounds: dict[JobClass, int],
    reported_exits: set[JobClass],
    *,
    report: bool,
) -> bool:
    """Is every consumer alive, draining and being heard from RIGHT NOW?

    Heartbeat honesty asked of every consumer rather than of one serial loop: the stamp means "all
    media work is being drained", so ONE bad consumer has to stop it. Three ways a consumer stops
    counting as alive — a streak of escaped errors, having exited, and having gone quiet past its
    class's bound — each logged distinctly so the log says which one it was.

    The verdict is a function of the moment it is asked for, which is why it lives here instead of
    in a variable: a pass asks twice, either side of an await, and holding the first answer over
    the second would stamp a consumer that died while the supervisor was waiting. `report` is what
    makes asking twice affordable — only the first verdict of a pass speaks, so the log keeps
    saying each condition once per pass and no faster.
    """
    now = _now()
    healthy = True
    for job_class, (task, job_health, job_watermark) in consumers.items():
        if not job_health.healthy:
            if report:
                logger.error(
                    "worker_unhealthy_error_streak",
                    job_class=job_class,
                    streak=job_health.streak,
                )
            healthy = False
        if task.done():
            if report and job_class in reported_exits:
                logger.error("worker_consumer_exited", job_class=job_class)
            elif report:
                reported_exits.add(job_class)
                # The one line that can carry WHY the class stopped: nothing else retrieves this
                # exception until the supervisor itself is torn down, so a worker that keeps
                # running would otherwise log a dead class without ever naming what killed it.
                # Once only — repeating the traceback every ten seconds would bury the rest.
                logger.error(
                    "worker_consumer_exited",
                    job_class=job_class,
                    exc_info=None if task.cancelled() else task.exception(),
                )
            healthy = False
        elapsed = now - job_watermark.at
        if elapsed > stall_bounds[job_class]:
            if report:
                logger.error(
                    "worker_consumer_stalled",
                    job_class=job_class,
                    elapsed_seconds=round(elapsed, 1),
                    bound_seconds=stall_bounds[job_class],
                )
            healthy = False
    return healthy


class _StopRequest:
    """Whether a deliberate stop signal asked this supervisor to end.

    The supervisor is ended by cancelling its own task — the same shutdown every in-process caller
    performs — so this flag is the only thing that tells "docker asked us to stop" from "a caller
    cancelled run()", and it decides whether run() returns (the process exits 0) or lets the
    cancellation keep propagating.
    """

    def __init__(self) -> None:
        self.signum: int | None = None

    @property
    def fired(self) -> bool:
        return self.signum is not None


def _install_stop_handlers(
    stop: _StopRequest, supervisor: asyncio.Task[None]
) -> list[signal.Signals]:
    """Make SIGTERM/SIGINT end the supervisor through its OWN teardown; returns what was installed.

    Cancelling the supervisor task is deliberately the same shutdown the in-process callers use:
    the `finally` in run() already cancels and awaits both consumers, and each consumer's
    in-flight claim is returned to the queue by run_once's cancel branch. So the signal path adds
    no second teardown to keep in step with the first.

    The handler does nothing but flag and cancel: it runs on the event loop, and a stop that
    waited on anything here would spend the grace before the teardown even started.
    """
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def request_stop(signum: signal.Signals) -> None:
        if stop.fired:
            return  # a second TERM cannot make a teardown that is already running any faster
        stop.signum = int(signum)
        logger.info("worker_stopping", signal=signum.name)
        supervisor.cancel()

    for sig in STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            # This loop cannot take signal handlers (not a Unix loop, or not the main thread — as
            # in some test runners). Said out loud rather than passed over: production's stop path
            # IS a Unix main-thread signal, so a worker that logs this is one whose claims a
            # recreate would strand again.
            logger.warning("worker_stop_handler_unavailable", signal=sig.name)
            continue
        installed.append(sig)
    return installed


def _remove_stop_handlers(installed: list[signal.Signals]) -> None:
    """Hand the signals back on the way out — a handler still bound to a supervisor task that has
    ended would otherwise outlive it, and one process can run run() more than once (the tests do).
    """
    loop = asyncio.get_running_loop()
    for sig in installed:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def _bounded[T](step: str, work: Awaitable[T], on_timeout: T) -> T:
    """Await one supervisor step under its TOTAL bound; past the bound, say so and carry on.

    WHY a bound here rather than the `spawn_digest` treatment, for every step this wraps: each one
    is a SHORT, idempotent sweep whose answer the supervisor's very next lines act on — the frame
    drain's status decides this pass's back-off, the two gate reads decide whether a digest is
    spawned at all, and the hourly sweeps have to finish before the cadence timer is re-armed or
    the next hour would start a second copy over the same rows. A spawn would therefore need a
    single-flight handle per sweep (what `_digest_task` is for the one step that genuinely runs for
    minutes to hours), which is more machinery than the thing it would protect. The digest RUN
    itself stays spawned; this is only its cheap gate.

    A bound reached is an EVENT, not a failure of the loop: it is logged by name and the caller
    gets `on_timeout`, so both consumers and the next stamp are untouched.

    Telling a bound's expiry from a deliberate stop is the whole hazard here, and both directions
    hold:

    * A bound expiring cancels the supervisor's await and `asyncio.timeout` converts that
      cancellation into `TimeoutError` at the context exit — caught HERE, so it never reaches
      run()'s `except asyncio.CancelledError` and can never be read as a stop.
    * A stop arriving while a bounded step runs cancels the supervisor task a SECOND time.
      `asyncio.timeout` converts only while `Task.uncancel()` brings the count back to what it was
      on entry (the same cancelling census `_stop_in_flight` reads), so with a stop outstanding the
      `CancelledError` propagates untouched and the teardown runs at once. The stop is never
      swallowed as a timeout, and it does not wait the bound out.

    The step's session survives either cancellation: every `*_safe` wrapper isolates `Exception`,
    which a `CancelledError` is not, so the cancellation unwinds through the `async with
    async_session()` that opened the transaction and that context manager rolls it back and closes
    it — nothing is left mid-transaction for the next pass to trip over.
    """
    bound = HOUSEKEEPING_BOUNDS[step]
    try:
        async with asyncio.timeout(bound):
            return await work
    except TimeoutError:
        logger.warning("worker_housekeeping_timeout", step=step, bound_seconds=bound)
        return on_timeout


async def run() -> None:
    logger.info("worker_started", version=__version__, worker_id=WORKER_ID)
    # Before anything is spawned or stamped: a configuration this release cannot honour must fail
    # loud and leave no trace of liveness behind, or the healthcheck would read the refusal as a
    # healthy worker for one whole bar.
    validate_worker_slots()
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
    # A deliberate stop is a signal, and it is installed HERE — before the schema gate, which can
    # wait minutes — so that a recreate arriving during startup also ends through the teardown
    # below rather than by SIGKILL. `consumers` exists this early for the same reason: the
    # teardown is the one exit path, and before the spawn it simply has nothing to cancel.
    stop = _StopRequest()
    supervisor = asyncio.current_task()
    installed = _install_stop_handlers(stop, supervisor) if supervisor is not None else []
    consumers: _Consumers = {}
    try:
        # Schema gate (MEMNG-86): a deploy recreates app + worker together, but only the app
        # migrates. Wait for the live schema to reach the head this image was built with before
        # running the recovery sweep or the loop — otherwise the sweep hits UndefinedColumnError
        # against the old schema. Raises (worker exits, Docker restarts) if it never catches up.
        await migrations.wait_for_migrated_schema()
        # Startup sweeps: the common stranding cause is this very restart (a deploy
        # --force-recreate killing work mid-flight), so adopt orphaned claims — frame-share AND
        # media (MEMNG-90) — before draining anything new.
        # Bounded like every other awaited step, and for the sharper reason: these two run AFTER
        # the startup stamp and BEFORE the loop exists, so until they return the heartbeat has
        # exactly one value and nothing else can refresh it.
        await _bounded("frame_share_recover", run_frame_share_recover_safe(), 0)
        await _bounded("media_recover", run_media_recover_safe(), 0)
        # One consumer task per class, each with its OWN health and watermark. The supervisor keeps
        # a strong reference to every task: a task only weakly referenced by the loop can be
        # garbage collected mid-drain, and the teardown below needs them anyway.
        stall_bounds: dict[JobClass, int] = {
            "transcode": settings.WORKER_TRANSCODE_STALL_SECONDS,
            "light": settings.WORKER_LIGHT_STALL_SECONDS,
        }
        spawn_classes: tuple[JobClass, ...] = ("transcode", "light")
        for job_class in spawn_classes:
            job_health = _DrainHealth(settings.WORKER_UNHEALTHY_ERROR_STREAK)
            job_watermark = _Watermark()
            consumers[job_class] = (
                asyncio.create_task(
                    consume_media(job_class, job_health, job_watermark),
                    name=f"media-consumer-{job_class}",
                ),
                job_health,
                job_watermark,
            )
        last_reap = 0.0
        # Which classes have already had their exit reported with the cause attached. A local,
        # because it belongs to this supervisor's run and must not outlive it.
        reported_exits: set[JobClass] = set()
        while True:
            if _consumers_healthy(consumers, stall_bounds, reported_exits, report=True):
                beat()  # blocking-ok: the liveness stamp itself — see beat()
            # Drain a frame-share job opportunistically in its own session (like reap): the album
            # upload is decoupled from the media job's transaction so an upload error can't poison
            # media work.
            # Bounded: this is the await that sits BETWEEN the pass's two stamps, and the album
            # send inside it hangs on somebody else's server. A drain that reached its bound
            # answers "error" — an existing status, and the right one: it is not "processed", so
            # the pass backs off exactly as it does for a drain that raised.
            frame_status = await _bounded("frame_share_drain", run_frame_share_drain(), "error")
            # Ask again, silently, on a fresh clock: the drain above is an await of unbounded
            # length, and a consumer that dies, crosses its bound or reaches its error streak
            # inside it would otherwise be stamped alive on a verdict taken before it happened.
            if _consumers_healthy(consumers, stall_bounds, reported_exits, report=False):
                beat()  # blocking-ok: the liveness stamp itself — see beat()
            # The digest: a cheap gate read, and a SPAWN when it fires. The loop continues
            # immediately — see spawn_digest for why this is never awaited here. A manual row the
            # admin API committed is picked up FIRST and by the same spawn: while one is in flight
            # the gate answers not-due anyway (its local_date is in the ledger), so the order costs
            # a scheduled run nothing and gives an operator's "Run now" the shortest path to
            # starting.
            # Both reads are bounded: they are cheap by construction, so the only way either takes
            # real time is an unreachable database — and then "no manual run" / "not due" is the
            # same answer their own error isolation already gives, one poll earlier.
            if not digest_task_in_flight():
                manual_run_id = await _bounded("adopt_manual_digest", adopt_manual_digest(), None)
                if manual_run_id is not None:
                    if spawn_claimed_digest(manual_run_id):
                        logger.info("digest_run_spawned", trigger="manual", run_id=manual_run_id)
                elif await _bounded("digest_is_due", digest_is_due(), False):
                    if spawn_digest():
                        logger.info("digest_run_spawned", trigger="scheduled")
            if time.time() - last_reap > REAP_INTERVAL_SECONDS:
                # The whole hourly block is bounded step by step. Every one of these is idempotent
                # and runs again next hour, so a step cut short costs a cycle of lateness — which
                # is the cheaper of the two outcomes, the other being a recreated container.
                reaped = await _bounded("reap_expired_committed", reap_expired_committed(), 0)
                if reaped:
                    logger.info("uploads_reaped", count=reaped)
                # Piggybacks the reap cadence: a claim orphaned by something other than a restart
                # (an OOM-killed send that leaves the worker alive) is recovered within the hour —
                # frame-share and media alike.
                await _bounded("frame_share_recover", run_frame_share_recover_safe(), 0)
                await _bounded("media_recover", run_media_recover_safe(), 0)
                # Same cadence for the progress side channel: a killed worker cannot clear its own
                # row, so the anti-join sweep keeps the table bounded by what is actually running.
                await _bounded("job_progress_sweep", sweep_job_progress_safe(), 0)
                # …and for the commit-race window (MEMNG-146), which no in-request bookkeeping can
                # close: a stored video whose poster the unwind discarded gets its fast lane back.
                await _bounded("commit_race_reconcile", run_commit_race_reconcile_safe(), 0)
                # The digest's two sweeps ride the same cadence: a run whose process died is marked
                # `failed` (so the 26 h stale alert can see it), and hosted media past its retention
                # is deleted from Dropbox. The retention half is the step this bound was raised on.
                # The same cadence the grace period is measured in: a row detached longer ago
                # than this block's own interval is one no request is still cleaning up after.
                await _bounded("detached_attachment_sweep", sweep_detached_attachments_safe(), 0)
                await _bounded("digest_sweep", sweep_digest_safe(), None)
                last_reap = time.time()
            if frame_status != "processed":  # idle/deferred → back off
                # The media term is gone from this condition with the media drain: the consumers
                # pace themselves on their own backoff now, and the supervisor's own pass is cheap.
                await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        if not stop.fired:
            raise  # somebody else's cancellation (the loop tests) keeps propagating, as before
        # Our own stop signal: the teardown below runs either way, and returning afterwards is
        # what makes a deliberate stop exit 0 instead of dying on an escaped CancelledError.
    finally:
        # The teardown is what the stop signal ENDS IN, and what every caller that cancels run()
        # (the loop tests do) relies on: it would otherwise leave a consumer draining against
        # patched collaborators or a closed session. Cancelling and AWAITING is load-bearing for
        # the stop — each consumer returns its in-flight claim to the queue while it unwinds
        # (run_once's cancel branch), and only the await makes that write part of the stop rather
        # than a race with the process exiting.
        try:
            for task, _, _ in consumers.values():
                task.cancel()
            for task, _, _ in consumers.values():
                try:
                    await task
                except asyncio.CancelledError:
                    pass  # the cancellation this teardown just requested, and only that one
                except Exception:
                    # A consumer that had ALREADY died: its exception is retrieved here rather
                    # than left to surface as an unretrieved-task warning, and it must not mask
                    # the shutdown that is in progress.
                    logger.exception("worker_consumer_teardown_error")
        finally:
            # Only NOW are the signals given back. Removing them before the teardown would leave
            # the default action installed for exactly the seconds the consumers spend writing
            # their claims back, so an impatient second SIGTERM would kill the process mid-write —
            # the one thing this whole path exists to prevent. While they are still installed a
            # repeat signal is a no-op (`stop.fired`), so it cannot re-cancel the supervisor
            # either.
            _remove_stop_handlers(installed)
    # Reached only on a signalled stop (the loop above never exits on its own): every consumer has
    # been cancelled AND awaited by now, so whatever they held is back in the queue and the next
    # worker start can claim it at once.
    logger.info(
        "worker_stopped",
        signal=signal.Signals(stop.signum).name if stop.signum is not None else None,
        worker_id=WORKER_ID,
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
