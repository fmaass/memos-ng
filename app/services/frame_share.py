"""Aura frame share — enqueue side (Phase 1).

`maybe_enqueue_frame_shares` is the single choke point both memo router paths (create + edit)
call after attachment linking and before commit. It inserts one `frame_share_jobs` row per linked
image attachment when the feature is fully configured and the memo is #curated. The worker (Phase 2)
drains the queue, renders each photo, and emails it to the frame.
"""

import io
import re
import tempfile
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
import pillow_heif
import structlog
from anyio import to_thread
from PIL import Image, ImageDraw, ImageOps
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, FrameShareJob, FrameShareState, Memo, Tag, User, memo_tags

logger = structlog.get_logger(__name__)

pillow_heif.register_heif_opener()  # decode HEIC/HEIF originals (also registered in services.media)

# The tag that opts a memo's photos into frame sharing.
CURATED_TAG = "curated"

# Pragmatic email shape check (not full RFC 5322): one @, non-empty local + domain, a dotted TLD,
# no whitespace. Enough to reject typos in the settings form; the real proof is Phase 0/the frame.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value or ""))


def render_test_image() -> bytes:
    """Produce the settings 'Send test photo' JPEG by running a generated source through the REAL
    render_for_frame — so the test-send exercises the actual render path (open, exif_transpose,
    q95 encode, captured_at→DateTimeOriginal backfill), not just SMTP. The source is a PNG with no
    EXIF, mirroring the curated screen-PNGs, so the captured_at backfill is genuinely exercised."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        img = Image.new("RGB", (1200, 800), (38, 70, 110))
        draw = ImageDraw.Draw(img)
        draw.rectangle((60, 60, 1140, 740), outline=(255, 255, 255), width=8)
        draw.text((110, 110), "memos-ng frame test", fill=(255, 255, 255))
        img.save(path, "PNG")  # PNG source = no EXIF, like the stored curated renditions
        return render_for_frame(path, datetime.now(UTC))
    finally:
        Path(path).unlink(missing_ok=True)

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


def server_smtp_configured() -> bool:
    """Global on/off switch: empty username or password disables the feature entirely."""
    return bool(settings.SMTP_USERNAME and settings.SMTP_PASSWORD)


async def maybe_enqueue_frame_shares(db: AsyncSession, memo: Memo, user: User) -> int:
    """Enqueue frame-share jobs for a memo's image attachments, if all guards pass. Returns the
    number of NEW rows inserted (0 when any guard fails or every attachment is already queued).

    Guards (all required): server SMTP configured, owner has both Aura settings, memo is #curated.
    Idempotent per attachment via ON CONFLICT (attachment_id) DO NOTHING — so re-editing a curated
    memo never resends existing photos, while a newly added image on an already-curated memo is
    enqueued. Runs inside the caller's transaction (atomic with the memo write)."""
    if not server_smtp_configured():
        return 0
    if not (user.frame_send_to and user.frame_send_from):
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
        result = await db.execute(
            # Core insert (needed for ON CONFLICT) does NOT apply the ORM column defaults, so set
            # the NOT-NULL state/attempts explicitly. created_at uses the DB server_default.
            # RETURNING id yields a row on insert and nothing on conflict → a typed insert counter.
            pg_insert(FrameShareJob)
            .values(
                attachment_id=att.id,
                memo_id=memo.id,
                user_id=user.id,
                state=FrameShareState.queued,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["attachment_id"])
            .returning(FrameShareJob.id)
        )
        if result.first() is not None:
            inserted += 1

    if inserted:
        logger.info("frame_share.enqueued", memo_id=memo.id, user_id=user.id, jobs=inserted)
    return inserted


# --- send side (Phase 2): render + email, driven by the worker ---


def render_for_frame(original_path: str, captured_at: datetime | None = None) -> bytes:
    """Render an attachment original into a frame-ready JPEG (returns bytes; never persisted).

    Downscaled to a 2048px longest edge (never upscaled — Aura resizes to its <=2048x1536 panel
    anyway, so this is visually lossless on the frame and keeps the JPEG under iCloud's 20MB and
    Aura's ~9.5MB caps), JPEG quality 95 with 4:4:4 chroma, EXIF preserved with Orientation
    normalized to 1 after ImageOps.exif_transpose bakes rotation into the pixels. DateTimeOriginal
    is backfilled from captured_at ONLY when the source lacks it (stored curated screen-PNGs have no
    EXIF, and Aura sorts chronologically by that tag). HEIC decodes via pillow-heif. Synchronous CPU
    work — call under to_thread.run_sync."""
    with Image.open(original_path) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
        if max(img.size) > FRAME_MAX_EDGE:
            img.thumbnail((FRAME_MAX_EDGE, FRAME_MAX_EDGE))  # downscale large; never upscales
        exif = img.getexif()
        exif[_EXIF_ORIENTATION] = 1  # pixels already normalized; keep the tag consistent
        exif_ifd = exif.get_ifd(_EXIF_OFFSET)
        if not exif_ifd.get(_EXIF_DATETIME_ORIGINAL) and captured_at is not None:
            exif_ifd[_EXIF_DATETIME_ORIGINAL] = captured_at.strftime(_EXIF_DT_FMT)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=FRAME_JPEG_QUALITY, subsampling=0, exif=exif)
    return buf.getvalue()


async def send_frame_share(
    *, from_addr: str, to_addr: str, filename: str, jpeg_bytes: bytes
) -> None:
    """Email ONE photo to the frame (Aura mandates one photo per email). Subject/body are empty —
    Aura ignores them. Never log the credentials or the message body."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = ""
    msg.add_attachment(jpeg_bytes, maintype="image", subtype="jpeg", filename=filename)
    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        start_tls=True,
        username=settings.SMTP_USERNAME,
        password=settings.SMTP_PASSWORD,
    )


def _file_exists(path: str) -> bool:
    return Path(path).exists()


def _is_permanent_smtp_error(exc: Exception) -> bool:
    """Permanent SMTP failures (bad credentials, refused sender/recipient, 5xx policy) will never
    succeed on retry — mark them terminal immediately instead of churning through all attempts."""
    if isinstance(
        exc,
        aiosmtplib.SMTPAuthenticationError
        | aiosmtplib.SMTPSenderRefused
        | aiosmtplib.SMTPRecipientsRefused,
    ):
        return True
    code = getattr(exc, "code", None)
    return isinstance(code, int) and 500 <= code < 600


async def _revalidate(db: AsyncSession, job: FrameShareJob) -> tuple[Attachment, User] | None:
    """Best-effort send-time revalidation (see plan D2 — NOT lock-held across the SMTP round-trip).
    Returns (attachment, owner) if the job is still eligible, else None → the worker marks it
    `skipped`. Catches every realistic case: memo deleted / un-#curated / attachment unlinked /
    settings cleared / original missing / feature disabled."""
    if not server_smtp_configured():
        return None
    att = await db.get(Attachment, job.attachment_id)
    if att is None or att.memo_id != job.memo_id or not is_frame_image(att.mime):
        return None
    memo = await db.get(Memo, job.memo_id)
    if memo is None:
        return None
    user = await db.get(User, job.user_id)
    if user is None or not (user.frame_send_to and user.frame_send_from):
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
    return att, user


async def process_claimed_job(db: AsyncSession, job: FrameShareJob) -> str:
    """Render + send one job already committed as `claimed`. Mutates job state and FLUSHES only —
    the worker owns the commit, so a crash before that commit leaves the job `claimed` and it is
    never re-claimed (at-most-once; no duplicate email — plan D1). Returns the outcome label."""
    reval = await _revalidate(db, job)
    if reval is None:
        job.state = FrameShareState.skipped
        await db.flush()
        logger.info("frame_share.skipped", job_id=job.id)
        return "skipped"
    att, user = reval
    assert att.storage_path is not None  # _revalidate guarantees a present file
    assert user.frame_send_from is not None and user.frame_send_to is not None
    filename = Path(att.filename).stem + ".jpg"
    try:
        jpeg = await to_thread.run_sync(render_for_frame, att.storage_path, att.captured_at)
        if len(jpeg) > FRAME_SIZE_WARN_BYTES:
            logger.warning("frame_share.render_large", job_id=job.id, bytes=len(jpeg))
        await send_frame_share(
            from_addr=user.frame_send_from,
            to_addr=user.frame_send_to,
            filename=filename,
            jpeg_bytes=jpeg,
        )
    except Exception as exc:
        job.attempts += 1
        job.error = str(exc)[:500]
        if _is_permanent_smtp_error(exc) or job.attempts >= FRAME_MAX_ATTEMPTS:
            job.state = FrameShareState.failed
            await db.flush()
            logger.warning(
                "frame_share.failed", job_id=job.id, attempts=job.attempts, error=str(exc)[:200]
            )
            return "failed"
        job.state = FrameShareState.queued  # retry later
        job.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=FRAME_BACKOFF_BASE_S * 2 ** (job.attempts - 1)
        )
        await db.flush()
        logger.warning(
            "frame_share.retry", job_id=job.id, attempts=job.attempts, error=str(exc)[:200]
        )
        return "retry"
    job.state = FrameShareState.sent
    job.sent_at = datetime.now(UTC)
    await db.flush()
    logger.info("frame_share.sent", job_id=job.id, bytes=len(jpeg))
    return "sent"
