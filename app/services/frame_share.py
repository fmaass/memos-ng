"""Aura frame share — enqueue side (Phase 1).

`maybe_enqueue_frame_shares` is the single choke point both memo router paths (create + edit)
call after attachment linking and before commit. It inserts one `frame_share_jobs` row per linked
image attachment when the feature is fully configured and the memo is #curated. The worker (Phase 2)
drains the queue, renders each photo, and uploads it to the frame's Aura album over the album API.
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pillow_heif
import structlog
from anyio import to_thread
from PIL import Image, ImageOps
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import Attachment, FrameShareJob, FrameShareState, Memo, Tag, User, memo_tags

logger = structlog.get_logger(__name__)

pillow_heif.register_heif_opener()  # decode HEIC/HEIF originals (also registered in services.media)

# The tag that opts a memo's photos into frame sharing.
CURATED_TAG = "curated"


# Render/send tuning. q95 4:4:4 downscaled to a 2048px longest edge (user-decided 2026-07-04,
# reversing the original full-res choice). Real curated exports average ~8600px / 82MB source;
# full-res q95 rendered to 20-46MB JPEGs which iCloud rejects (552 message-size) and exceed Aura's
# ~9.5MB proxy cap. Aura's largest-ever panel is 2048x1536 and it resizes on ingestion, so a 2048px
# cap is visually lossless on the frame while keeping renders ~1-2MB, well under both limits.
FRAME_JPEG_QUALITY = 95
FRAME_MAX_EDGE = 2048  # downscale to this longest edge (never upscale); = Aura's largest panel
# Log (but still send) when a render exceeds Aura's ~9.5 MB web-uploader proxy cap.
FRAME_SIZE_WARN_BYTES = 9 * 1024 * 1024
FRAME_MAX_ATTEMPTS = 5
FRAME_BACKOFF_BASE_S = 60  # exponential: BASE * 2**(attempts-1)

_EXIF_ORIENTATION = 0x0112
_EXIF_OFFSET = 0x8769  # pointer to the Exif sub-IFD (where DateTimeOriginal lives)
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_DT_FMT = "%Y:%m:%d %H:%M:%S"


def is_frame_image(mime: str) -> bool:
    """True for raster image MIMEs the worker can decode + render to JPEG. Videos and other
    attachment types are never shared to the frame (Aura's email path rejects them). SVG is
    excluded — it is not a raster photo and Pillow cannot decode it."""
    m = (mime or "").lower()
    return m.startswith("image/") and m != "image/svg+xml"


def frame_api_configured() -> bool:
    """Global on/off switch for the HTTP album upload: enabled only when the endpoint URL and the
    bearer token are both set AND the album id is non-zero. Any missing piece disables the feature
    entirely — no jobs are enqueued and the worker claims none (same empty-default-disables pattern
    as SMTP/VAPID)."""
    return bool(settings.FRAME_API_URL and settings.FRAME_API_TOKEN and settings.FRAME_ALBUM_ID)


async def maybe_enqueue_frame_shares(db: AsyncSession, memo: Memo, user: User) -> int:
    """Enqueue frame-share jobs for a memo's image attachments, if all guards pass. Returns the
    number of NEW rows inserted (0 when any guard fails or every attachment is already queued).

    Guards (all required): the frame album API is configured (system-level destination) and the
    memo is #curated. There is no per-user guard any more — the send target is the shared album, not
    a per-user address.
    Idempotent per attachment via ON CONFLICT (attachment_id): a re-edit resurrects ONLY rows that
    went terminal `skipped`/`failed` (reset to queued so a transiently-missed photo gets another
    chance), while `sent` stays terminal (never resend), `claimed` is never touched (at-most-once
    tombstone), and a live `queued` retry keeps its backoff. A newly added image on an
    already-curated memo is enqueued fresh. Runs inside the caller's transaction (atomic with the
    memo write)."""
    if not frame_api_configured():
        return 0

    has_curated = (
        await db.execute(
            select(Tag.id)
            .join(memo_tags, memo_tags.c.tag_id == Tag.id)
            .where(memo_tags.c.memo_id == memo.id, Tag.name == CURATED_TAG)
        )
    ).first() is not None
    if not has_curated:
        return 0

    attachments = (
        (await db.execute(select(Attachment).where(Attachment.memo_id == memo.id))).scalars().all()
    )
    inserted = 0
    for att in attachments:
        if not is_frame_image(att.mime):
            continue
        # Core insert (needed for ON CONFLICT) does NOT apply the ORM column defaults, so set the
        # NOT-NULL state/attempts explicitly. created_at uses the DB server_default.
        ins = pg_insert(FrameShareJob).values(
            attachment_id=att.id,
            memo_id=memo.id,
            user_id=user.id,
            state=FrameShareState.queued,
            attempts=0,
        )
        result = await db.execute(
            # On conflict, resurrect only terminal rows (skipped/failed) back to queued and clear
            # their retry/claim bookkeeping; the WHERE keeps `sent`/`claimed`/live `queued` rows
            # untouched. RETURNING id yields a row on a fresh insert OR a matched resurrection and
            # nothing when the WHERE excludes the row → a typed "enqueued" counter.
            ins.on_conflict_do_update(
                index_elements=["attachment_id"],
                set_={
                    "state": FrameShareState.queued,
                    "attempts": 0,
                    "error": None,
                    "next_attempt_at": None,
                    "claimed_by": None,
                    "memo_id": ins.excluded.memo_id,
                    "user_id": ins.excluded.user_id,
                },
                where=FrameShareJob.state.in_(
                    [FrameShareState.skipped, FrameShareState.failed]
                ),
            ).returning(FrameShareJob.id)
        )
        if result.first() is not None:
            inserted += 1

    if inserted:
        logger.info("frame_share.enqueued", memo_id=memo.id, user_id=user.id, jobs=inserted)
    return inserted


# --- send side (Phase 2): render + upload to the Aura album API, driven by the worker ---


def render_for_frame(original_path: str, photo_date: datetime | None = None) -> bytes:
    """Render an attachment original into a frame-ready JPEG (returns bytes; never persisted).

    Downscaled to a 2048px longest edge (never upscaled — Aura resizes to its <=2048x1536 panel
    anyway, so this is visually lossless on the frame and keeps the JPEG under iCloud's 20MB and
    Aura's ~9.5MB caps), JPEG quality 95 with 4:4:4 chroma, EXIF preserved with Orientation
    normalized to 1 after ImageOps.exif_transpose bakes rotation into the pixels. DateTimeOriginal
    is backfilled from photo_date ONLY when the source lacks it (stored curated screen-PNGs have no
    EXIF, and Aura sorts chronologically by that tag — callers pass the memo's date, the intended
    frame position). HEIC decodes via pillow-heif. Synchronous CPU work — call under
    to_thread.run_sync."""
    with Image.open(original_path) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
        if max(img.size) > FRAME_MAX_EDGE:
            img.thumbnail((FRAME_MAX_EDGE, FRAME_MAX_EDGE))  # downscale large; never upscales
        exif = img.getexif()
        exif[_EXIF_ORIENTATION] = 1  # pixels already normalized; keep the tag consistent
        exif_ifd = exif.get_ifd(_EXIF_OFFSET)
        if not exif_ifd.get(_EXIF_DATETIME_ORIGINAL) and photo_date is not None:
            exif_ifd[_EXIF_DATETIME_ORIGINAL] = photo_date.strftime(_EXIF_DT_FMT)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=FRAME_JPEG_QUALITY, subsampling=0, exif=exif)
    return buf.getvalue()


def _safe_upload_error(exc: Exception) -> str:
    """Synthesize a token-free error string for the upload path from the exception TYPE and (only)
    the HTTP status code. A malformed-header failure (an illegal char anywhere in the token, even
    an internal newline) raises httpx.LocalProtocolError whose message embeds the whole Bearer
    header value in escaped forms that string redaction can't reliably catch. So we NEVER build
    this from str(exc)/the exception message — the type name and status code can never contain the
    token, which makes this leak-proof by construction rather than by pattern-matching."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"upload failed: HTTP {exc.response.status_code}"
    return f"upload failed: {type(exc).__name__}"


async def upload_to_album(filename: str, jpeg_bytes: bytes) -> None:
    """POST ONE rendered JPEG to the aura-frames album API, which then owns delivery to the frame
    (replacing the direct SMTP send). aura-frames returns 201 for a newly stored photo and 200 for a
    SHA-duplicate — both mean the photo is safely in the album, so BOTH are success. Any other
    status raises httpx.HTTPStatusError (carrying .response.status_code) so the caller can classify
    transient vs permanent. The bearer token is a secret — never log it (it lives only in the
    Authorization header, never in the URL or any raised exception message)."""
    # rstrip("/") so a trailing slash in FRAME_API_URL doesn't yield `//api/albums/...` (redirects
    # aren't followed, so a stray `//` could hard-fail the upload).
    url = f"{settings.FRAME_API_URL.rstrip('/')}/api/albums/{settings.FRAME_ALBUM_ID}/photos"
    headers = {
        "Authorization": f"Bearer {settings.FRAME_API_TOKEN}",
        "Content-Type": "image/jpeg",
        # httpx 0.28 requires ASCII header values; a curated photo can have a non-ASCII filename
        # (café.jpg), so percent-encode it. aura-frames uses X-Filename only as a display name
        # (it stores by sha), so a percent-encoded name is fine — and it stays non-empty + .jpg.
        "X-Filename": quote(filename),
    }
    # Explicit 30s timeout (httpx defaults to 5s connect / no overall cap here would let a hung
    # server stall the worker drain long enough to flap the healthcheck — 30s is generous for one
    # small JPEG, matching the old SMTP timeout).
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(url, content=jpeg_bytes, headers=headers)
    if resp.status_code in (200, 201):
        return
    # 4xx/5xx → HTTPStatusError with .response.status_code for the caller's transient/permanent
    # split; a stray non-2xx that raise_for_status ignores (e.g. an unfollowed 3xx) still errors.
    resp.raise_for_status()
    raise httpx.HTTPStatusError(
        f"unexpected status {resp.status_code}", request=resp.request, response=resp
    )


def _file_exists(path: str) -> bool:
    return Path(path).exists()


def _is_transient_http_error(exc: Exception) -> bool:
    """Transient album-upload failures worth retrying with backoff: any network/timeout/transport
    error (httpx.TimeoutException and httpx.ConnectError are both httpx.TransportError subclasses),
    and HTTP 408 (request timeout), 429 (rate limited), or any 5xx (server-side). Everything else —
    any other 4xx (400/401/403/404/413/415/422) — is a permanent client error retrying can't fix, so
    it returns False → the caller marks the job terminally `failed`."""
    # A LocalProtocolError is a malformed OUTGOING request (e.g. an illegal char in the bearer
    # token) — a config error retrying can't fix, so treat it as PERMANENT. It is a TransportError
    # subclass, so this check MUST precede the broad TransportError branch below. (RemoteProtocol
    # error — a malformed server response — stays transient via that branch.)
    if isinstance(exc, httpx.LocalProtocolError):
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in (408, 429) or 500 <= code < 600
    return False


async def _revalidate(
    db: AsyncSession, job: FrameShareJob
) -> tuple[Attachment, Memo, User] | None:
    """Best-effort send-time revalidation (see plan D2 — NOT lock-held across the upload call).
    Returns (attachment, memo, owner) if the job is still eligible, else None → the worker marks it
    `skipped`. Catches every realistic case: memo deleted / un-#curated / attachment unlinked /
    original missing / feature disabled. The destination is now the system-level album, so there is
    no per-user settings re-check here — only that the owner row still exists."""
    if not frame_api_configured():
        return None
    att = await db.get(Attachment, job.attachment_id)
    if att is None or att.memo_id != job.memo_id or not is_frame_image(att.mime):
        return None
    memo = await db.get(Memo, job.memo_id)
    if memo is None:
        return None
    user = await db.get(User, job.user_id)
    if user is None:
        return None
    still_curated = (
        await db.execute(
            select(Tag.id)
            .join(memo_tags, memo_tags.c.tag_id == Tag.id)
            .where(memo_tags.c.memo_id == memo.id, Tag.name == CURATED_TAG)
        )
    ).first() is not None
    if not still_curated:
        return None
    if not att.storage_path or not await to_thread.run_sync(_file_exists, att.storage_path):
        return None
    return att, memo, user


async def _write_claimed_outcome(
    db: AsyncSession,
    job: FrameShareJob,
    lease: tuple[str | None, datetime | None],
    **values: object,
) -> bool:
    """Apply a post-claim outcome write ONLY while this actor still holds the lease it claimed
    under.

    The claim commit releases the row lock (the tombstone is the ownership token, not the lock),
    so
    between it and the outcome write the stale-claim sweep can re-queue the row and another worker
    can claim it. Without a fence this actor's write would land on that newer generation: a `sent`
    landing on a re-claimed row, or a transient-retry write flipping an already-`sent` row back to
    `queued` (re-uploading a photo the album has), or a stale local `attempts` overwriting a higher
    counter and resurrecting a job already failed at the ceiling.

    So every outcome write is a conditional UPDATE on the lease identity — claimed_by AND the
    claimed_at generation stamp, both read when this actor took the row. The sweep clears exactly
    those two columns, which is what invalidates a superseded actor. A non-matching write touches
    zero rows and returns False: the newer generation owns the row, this actor logs and stands down.
    Holding the lease is also what makes the callers' local read-modify-write of `attempts` sound —
    nothing else may have touched the row while the lease stood.

    On success the instance is refreshed from the row actually written, so callers and tests read
    committed truth rather than a hopeful in-memory value.
    """
    claimed_by, claimed_at = lease
    result = await db.execute(
        update(FrameShareJob)
        .where(
            FrameShareJob.id == job.id,
            FrameShareJob.claimed_by == claimed_by,
            FrameShareJob.claimed_at == claimed_at,
        )
        .values(**values)
        .returning(FrameShareJob.id)  # RETURNING, not rowcount: a matched row yields a row
        .execution_options(synchronize_session=False)
    )
    if result.first() is None:
        logger.warning("frame_share.lease_lost", job_id=job.id, claimed_by=claimed_by)
        return False
    await db.refresh(job)
    return True


async def process_claimed_job(db: AsyncSession, job: FrameShareJob) -> str:
    """Render + send one job already committed as `claimed`. Writes the outcome through the lease
    fence and does NOT commit — the worker owns the commit, so a crash before it leaves the job
    `claimed`, to be re-queued by recover_stale_claimed once the lease goes stale (MEMNG-35).

    Returns the outcome label, or 'lease_lost' when the row moved on to a newer generation while
    this actor was working and the write was refused (see _write_claimed_outcome)."""
    lease = (job.claimed_by, job.claimed_at)  # the identity every write below is fenced on
    reval = await _revalidate(db, job)
    if reval is None:
        if not await _write_claimed_outcome(db, job, lease, state=FrameShareState.skipped):
            return "lease_lost"
        logger.info("frame_share.skipped", job_id=job.id)
        return "skipped"
    att, memo, _user = reval
    assert att.storage_path is not None  # _revalidate guarantees a present file
    filename = Path(att.filename).stem + ".jpg"
    # Stamp DateTimeOriginal from the MEMO's date, not attachment.captured_at: for curated exports
    # captured_at is unreliable (mostly NULL, else upload time); the memo's created_at is the user's
    # curated/back-dated event date — the intended chronological position on the frame.
    # Render step: a decode/render failure is a permanent data problem (corrupt original), not a
    # transient send error. Fail terminally on the first attempt instead of retrying with backoff
    # through all five (~15 min of churn) — and never reach the upload.
    try:
        jpeg = await to_thread.run_sync(render_for_frame, att.storage_path, memo.created_at)
    except Exception as exc:
        written = await _write_claimed_outcome(
            db, job, lease,
            attempts=job.attempts + 1,
            state=FrameShareState.failed,
            error=str(exc)[:500],
        )
        if not written:
            return "lease_lost"
        logger.warning(
            "frame_share.render_failed", job_id=job.id, attempts=job.attempts, error=str(exc)[:200]
        )
        return "failed"
    if len(jpeg) > FRAME_SIZE_WARN_BYTES:
        logger.warning("frame_share.render_large", job_id=job.id, bytes=len(jpeg))
    # Send step: upload the JPEG to the aura-frames album API (which then owns delivery). HTTP
    # failures keep the transient/permanent split (retry with backoff vs terminal): transient =
    # network/timeout + HTTP 408/429/5xx; permanent = any other 4xx. A 200 (SHA-duplicate) or 201
    # (new photo) is success — upload_to_album returns without raising.
    try:
        await upload_to_album(filename, jpeg)
    except Exception as exc:
        # A malformed-header failure (bad token) embeds the full `Bearer <token>` in str(exc), in
        # escaped forms redaction can't catch — so build the stored/logged error from the exception
        # TYPE + HTTP status ONLY (leak-proof by construction; never str(exc)).
        safe = _safe_upload_error(exc)
        attempts = job.attempts + 1  # sound because the write below refuses without the lease
        if not _is_transient_http_error(exc) or attempts >= FRAME_MAX_ATTEMPTS:
            written = await _write_claimed_outcome(
                db, job, lease, attempts=attempts, error=safe, state=FrameShareState.failed
            )
            if not written:
                return "lease_lost"
            logger.warning(
                "frame_share.failed", job_id=job.id, attempts=job.attempts, error=safe
            )
            return "failed"
        written = await _write_claimed_outcome(
            db, job, lease,
            attempts=attempts,
            error=safe,
            state=FrameShareState.queued,  # retry later
            next_attempt_at=datetime.now(UTC)
            + timedelta(seconds=FRAME_BACKOFF_BASE_S * 2 ** (attempts - 1)),
        )
        if not written:
            return "lease_lost"
        logger.warning(
            "frame_share.retry", job_id=job.id, attempts=job.attempts, error=safe
        )
        return "retry"
    written = await _write_claimed_outcome(
        db, job, lease, state=FrameShareState.sent, sent_at=datetime.now(UTC)
    )
    if not written:
        return "lease_lost"
    logger.info("frame_share.sent", job_id=job.id, bytes=len(jpeg))
    return "sent"


# --- stale-claim recovery (MEMNG-35): un-strand jobs a crashed worker left in `claimed` ---


async def recover_stale_claimed(db: AsyncSession, now: datetime | None = None) -> int:
    """Re-queue (or terminally fail) frame-share jobs stuck in `claimed` past the stale horizon.

    The worker COMMITS the `claimed` tombstone before the external album upload, and only `queued`
    rows are ever claimed — so a process death in that window (a `docker compose up -d
    --force-recreate` during a send, an OOM kill, a host reboot) left the delivery in `claimed`
    forever, invisible and never retried. This sweep is the lease side of that tombstone: a row
    whose claimed_at is older than FRAME_CLAIM_STALE_MINUTES has no live worker behind it.

    A NULL claimed_at is treated as stale: it can only be a row claimed before this column existed
    (i.e. already stranded for far longer than any horizon), never a fresh claim — every claim path
    stamps it.

    Recovery costs one attempt, so a job that reliably kills the worker mid-send walks to the
    visible `failed` state at FRAME_MAX_ATTEMPTS instead of re-queueing forever (a re-edit of the
    memo still resurrects it via maybe_enqueue_frame_shares). Re-queued rows are due immediately —
    the crash is not a rate-limit signal, so no backoff is scheduled.

    DUPLICATE-DELIVERY TRADE: the album upload is not transactional, so a crash AFTER the album
    accepted the photo but before the `sent` commit re-sends it. That is deliberate — MEMNG-35
    prefers a possible duplicate over a silent loss. In practice aura-frames dedups by sha256 (it
    answers 200 for a known photo, 201 for a new one, and upload_to_album treats both as success),
    and re-rendering an unchanged original with the same fixed parameters yields byte-identical
    JPEG bytes, so the duplicate is absorbed downstream. It is only truly visible if the source or
    the memo's date changed between the two attempts (the render then differs → a new sha).

    Returns the number of rows recovered; FLUSHES only — the caller owns the commit.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=settings.FRAME_CLAIM_STALE_MINUTES)
    rows = (
        (
            await db.execute(
                select(FrameShareJob)
                .where(
                    FrameShareJob.state == FrameShareState.claimed,
                    or_(
                        FrameShareJob.claimed_at.is_(None),
                        FrameShareJob.claimed_at < cutoff,
                    ),
                )
                # skip_locked: a row another worker holds mid-claim is someone else's business.
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for job in rows:
        job.attempts += 1
        job.claimed_by = None
        job.claimed_at = None
        if job.attempts >= FRAME_MAX_ATTEMPTS:
            job.state = FrameShareState.failed  # terminal + visible, never a silent drop
            job.error = f"stale claim recovery gave up after {job.attempts} attempts"
            logger.warning(
                "frame_share.stale_claim_failed", job_id=job.id, attempts=job.attempts
            )
            continue
        job.state = FrameShareState.queued
        job.next_attempt_at = None  # due immediately: a crash is not a backoff signal
        job.error = "recovered from stale claim (worker interrupted mid-send)"
        logger.warning(
            "frame_share.stale_claim_requeued", job_id=job.id, attempts=job.attempts
        )
    await db.flush()
    return len(rows)


async def recover_stale_claimed_committed(now: datetime | None = None) -> int:
    """The worker's recovery entry: a dedicated session that COMMITS. A flush alone dies with the
    session, so the re-queue would evaporate and the job stay stranded (the MEMNG-37 shape)."""
    async with async_session() as db:
        recovered = await recover_stale_claimed(db, now)
        await db.commit()
    return recovered
