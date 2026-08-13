import hashlib
import os
import secrets
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import puremagic
from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Attachment,
    AttachmentRendition,
    AttachmentStatus,
    JobLane,
    JobState,
    MediaJob,
    Upload,
)
from app.services import storage
from app.services.variants import ParsedName, parse_variant


def _data_root() -> Path:
    return Path(settings.DATA_ROOT)


# Bounded read granularity for receiving a request body. Chosen well under the 5 MiB client chunk
# so an over-limit upload is refused after ~1 MiB of RAM, not after the whole chunk is buffered.
STREAM_CHUNK_SIZE = 1 << 20


class UploadTooLarge(Exception):
    """An in-flight append would push the upload past its ceiling. Carries the running total that
    tripped it so the caller can name which limit (server cap or declared size) applied."""

    def __init__(self, total: int) -> None:
        super().__init__(f"upload exceeds its size ceiling at {total} bytes")
        self.total = total


# Blocking disk I/O runs off the event loop (anyio.to_thread) so large chunk writes /
# sha256 reads don't stall the server.
def _open_part_sync(path: Path, offset: int) -> BinaryIO:
    """Open the part file positioned at `offset`, dropping anything past it — a chunk that was
    rejected or cut short must never bleed into the next append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "r+b" if path.exists() else "w+b")
    f.truncate(offset)
    f.seek(offset)
    return f


def _truncate_sync(path: Path, size: int) -> None:
    if not path.exists():
        return
    with open(path, "r+b") as f:
        f.truncate(size)


def _sha256_sync(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def part_path(upload_uid: str) -> Path:
    return _data_root() / "uploads" / f"{upload_uid}.part"


# HEIF-family ISOBMFF brands. HEIC (Apple's iPhone codec) shares the ISOBMFF container with MP4,
# so content sniffers (puremagic) mislabel it `video/mp4` — `detect_heif_mime` corrects that from
# the ftyp brand so Apple-codec photos route as images (and to the Merkur slow lane), not video.
_HEIF_HEVC_BRANDS = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
)
_AVIF_BRANDS = frozenset({b"avif", b"avis"})
# Heavy image codecs whose decode is offloaded to the Merkur slow worker (kept as archive masters).
HEAVY_IMAGE_MIMES = frozenset({"image/heic", "image/avif"})


def detect_heif_mime(header: bytes) -> str | None:
    """Classify an ISOBMFF header as a HEIF/HEIC or AVIF still image, else None.

    Reads the `ftyp` box brands. Returns 'image/heic' for the HEVC-HEIF family (iPhone .heic),
    'image/avif' for AVIF, or None for MP4/MOV video and non-ISOBMFF files. This is the
    discriminator content sniffers get wrong (they only see the shared MP4 container)."""
    if len(header) < 12 or header[4:8] != b"ftyp":
        return None
    brands = {header[8:12]}  # major_brand
    size = int.from_bytes(header[0:4], "big")
    end = min(size if size >= 16 else len(header), len(header))
    for off in range(16, end - 3, 4):  # compatible_brands follow the 4-byte minor_version
        brands.add(header[off : off + 4])
    if brands & _AVIF_BRANDS:
        return "image/avif"
    if brands & _HEIF_HEVC_BRANDS:
        return "image/heic"
    return None


def _sniff_mime(path: Path, declared: str) -> str:
    try:
        with open(path, "rb") as f:
            header = f.read(64)
        heif = detect_heif_mime(header)  # before puremagic — it mislabels HEIC as video/mp4
        if heif is not None:
            return heif
        guessed = puremagic.from_file(str(path), mime=True)
        return guessed or declared
    except (puremagic.PureError, ValueError, OSError):
        return declared


async def create_upload(
    db: AsyncSession, *, filename: str, mime: str, size: int, last_modified: int | None = None
) -> Upload:
    upload_uid = secrets.token_urlsafe(24)
    part = part_path(upload_uid)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.touch()
    client_mtime = datetime.fromtimestamp(last_modified / 1000, UTC) if last_modified else None
    upload = Upload(
        upload_uid=upload_uid,
        filename=filename,
        mime=mime,
        declared_size=size,
        bytes_received=0,
        client_mtime=client_mtime,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.UPLOAD_EXPIRY_HOURS),
    )
    db.add(upload)
    await db.flush()
    return upload


async def stream_to_part(
    stream: AsyncIterable[bytes], path: Path, *, start: int, ceiling: int
) -> int:
    """Write `stream` into `path` from byte `start`, chunk by chunk, and stop the moment the
    RUNNING total would pass `ceiling` — the bytes are never accumulated in memory, so an
    over-limit body is refused at receive time (UploadTooLarge) rather than after it has landed.
    Returns the byte count received. On any failure the file is truncated back to `start`, so a
    rejected or interrupted transfer leaves the on-disk state matching the recorded offset."""
    received = 0
    try:
        f = await to_thread.run_sync(_open_part_sync, path, start)
        try:
            async for chunk in stream:
                if not chunk:
                    continue
                received += len(chunk)
                if start + received > ceiling:
                    raise UploadTooLarge(start + received)
                await to_thread.run_sync(f.write, chunk)
        finally:
            await to_thread.run_sync(f.close)
    except BaseException:
        await to_thread.run_sync(_truncate_sync, path, start)
        raise
    return received


def size_ceiling(upload: Upload) -> int:
    """The lower of the session's declared size and the server cap. The cap is re-read here rather
    than trusted from session-create time, so a cap lowered since then still binds."""
    return min(upload.declared_size, settings.MAX_UPLOAD_MIB * 1024 * 1024)


async def append_stream(
    db: AsyncSession, upload: Upload, offset: int, stream: AsyncIterable[bytes]
) -> Upload:
    """Receive an append at `offset` straight to disk. Idempotent: re-sending an already-received
    range is a no-op; a forward gap is rejected (caller → 409); a body that would carry the upload
    past its size ceiling raises UploadTooLarge (caller → 413) with nothing partial kept."""
    if offset > upload.bytes_received:
        raise ValueError("offset gap")
    if offset < upload.bytes_received:
        return upload  # already have it; no-op
    start = upload.bytes_received
    received = await stream_to_part(
        stream, part_path(upload.upload_uid), start=start, ceiling=size_ceiling(upload)
    )
    upload.bytes_received = start + received
    await db.flush()
    return upload


async def append_chunk(db: AsyncSession, upload: Upload, offset: int, data: bytes) -> Upload:
    """Append an already-materialized `data` chunk — the in-process path (migration, tests).
    Network callers use append_stream so the bytes are never held whole in RAM."""

    async def _one() -> AsyncIterator[bytes]:
        yield data

    return await append_stream(db, upload, offset, _one())


async def finalize_pending(
    db: AsyncSession, upload_uids: list[str], owner_id: int, moved_out: list[str] | None = None
) -> list[str]:
    """Finalize-on-post: turn each fully-received pending upload into the owner's attachment and
    return their uids. A missing/expired or incomplete upload is skipped (it expires via
    reap_expired) so an abandoned compose never creates an orphan attachment.

    finalize()/finalize_rendition() move the part file to its final path BEFORE the (now shared)
    memo transaction commits — a non-transactional side effect. Each genuinely-moved path is
    appended to `moved_out` (inside those functions, new-file branch only) so the caller can
    unlink it if the post rolls back. A dedup return moves nothing and appends nothing.

    Screen/print grouping is IN-BATCH ONLY: screens (non-print files) become Attachments first, then
    each print attaches to the same-base screen as a hidden rendition. A print with no screen
    in this batch — or an in-batch (screen, variant_key) collision — is PROMOTED to a normal picture
    (decision: a print is never silently dropped, never silently hidden without a screen)."""
    pending: list[tuple[Upload, ParsedName]] = []
    for up_uid in upload_uids:
        upload = (
            await db.execute(select(Upload).where(Upload.upload_uid == up_uid))
        ).scalar_one_or_none()
        if upload is None:
            continue
        part = part_path(up_uid)
        if not part.exists() or part.stat().st_size != upload.declared_size:
            continue  # incomplete — leave it to expire, don't half-finalize
        pending.append((upload, parse_variant(upload.filename)))

    out: list[str] = []
    base_to_attachment: dict[str, Attachment] = {}
    for upload, parsed in pending:  # PASS 1 — screens become Attachments (first-wins per base)
        if parsed.is_print:
            continue
        att = await finalize(db, upload, owner_id, moved_out)
        base_to_attachment.setdefault(parsed.base, att)
        out.append(att.uid)

    seen: set[tuple[int, str]] = set()
    for upload, parsed in pending:  # PASS 2 — prints attach to their batch screen, else promote
        if not parsed.is_print:
            continue
        screen = base_to_attachment.get(parsed.base)
        vk = parsed.variant_key
        if screen is not None and vk is not None and (screen.id, vk) not in seen:
            await finalize_rendition(db, upload, screen, parsed, moved_out)
            seen.add((screen.id, vk))
        else:  # no matching screen, or an in-batch variant_key collision → promote to a photo
            att = await finalize(db, upload, owner_id, moved_out)
            out.append(att.uid)
    return out


async def finalize(
    db: AsyncSession, upload: Upload, owner_id: int, moved_out: list[str] | None = None
) -> Attachment:
    part = part_path(upload.upload_uid)
    actual = part.stat().st_size
    if actual != upload.declared_size:
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")

    digest = await to_thread.run_sync(_sha256_sync, part)

    # sha256 dedup — identical bytes already stored BY THIS OWNER → return the existing
    # attachment. Scoped per-owner: a global dedup would hand one user a reference to another
    # user's attachment row (cross-user linkage), so dedup only within the finalizing owner.
    # ONLY dedup against an UNLINKED attachment (memo_id IS NULL): an attachment already linked to a
    # memo is that memo's — reusing it would let link_attachments re-point memo_id and STRAND the
    # original memo (the same photo in a second memo must get its own row + file, like renditions).
    # limit(1) since several memos may legitimately share identical bytes now.
    existing = (
        (
            await db.execute(
                select(Attachment)
                .where(
                    Attachment.sha256 == digest,
                    Attachment.owner_id == owner_id,
                    Attachment.memo_id.is_(None),
                )
                .limit(1)
                # FOR UPDATE so two concurrent posts can't both reuse the same unlinked row: the
                # loser blocks, then Postgres re-evaluates `memo_id IS NULL` against the now-linked
                # row, excludes it, and the loser creates its own attachment instead of stealing.
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        part.unlink(missing_ok=True)
        await db.delete(upload)
        await db.flush()
        return existing

    mime = _sniff_mime(part, upload.mime)
    now = datetime.now(UTC)
    # Stage in originals/_unfiled/ — the memo (and so the per-memo folder) doesn't exist yet; the
    # master is atomically moved into originals/YYYY/MM/<memo-folder>/ on link_attachments.
    dest_dir = storage.unfiled_dir(_data_root())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}_{Path(upload.filename).name}"
    await to_thread.run_sync(os.replace, part, dest)
    if moved_out is not None:  # only a genuinely-moved file; the dedup return above appends nothing
        moved_out.append(str(dest))

    is_video = mime.startswith("video/")
    attachment = Attachment(
        uid=secrets.token_urlsafe(16),
        owner_id=owner_id,
        filename=upload.filename,
        mime=mime,
        size=actual,
        sha256=digest,
        captured_at=upload.client_mtime or now,
        status=AttachmentStatus.pending if is_video else AttachmentStatus.processing,
        storage_path=str(dest),
        # width/height/duration are filled by the worker fast lane (T2.2 pyvips/ffprobe).
    )
    db.add(attachment)
    await db.flush()

    # Lane routing: video → fast (poster/dims), and the fast lane then enqueues the slow
    # (transcode) job — so the two never race a status overwrite under concurrent workers (a slow
    # job setting 'ready' before the fast job sets 'processing' would strand finished media). A
    # heavy image (HEIC/AVIF — Apple's CPU-costly codec) goes straight to the slow lane so its
    # decode runs on the Merkur worker, off the thermally-bound NAS; ordinary images stay fast.
    initial_lane = JobLane.slow if mime in HEAVY_IMAGE_MIMES else JobLane.fast
    db.add(MediaJob(attachment_id=attachment.id, lane=initial_lane, state=JobState.queued))
    await db.delete(upload)
    await db.flush()
    return attachment


async def finalize_rendition(
    db: AsyncSession,
    upload: Upload,
    attachment: Attachment,
    parsed: ParsedName,
    moved_out: list[str] | None = None,
) -> AttachmentRendition | None:
    """Attach a hidden, download-only rendition (print master) to its screen `attachment`. Writes a
    UNIQUE uuid-named file (never a shared/deterministic path → safe to delete, never overwrites a
    committed file) and NEVER enqueues a MediaJob — a rendition is an archive master, not a tier,
    so it must not touch the thermally-bound worker. Write-once per (attachment, variant_key): a
    re-dump of an existing key is an idempotent no-op. Any mime is accepted (PNG/TIFF/RAW/…)."""
    assert parsed.variant_key is not None  # callers pass only prints (is_print ⇒ variant_key set)
    part = part_path(upload.upload_uid)
    actual = part.stat().st_size
    if actual != upload.declared_size:
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")

    existing = (
        await db.execute(
            select(AttachmentRendition).where(
                AttachmentRendition.attachment_id == attachment.id,
                AttachmentRendition.variant_key == parsed.variant_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:  # write-once — a re-dump of the same key changes nothing
        part.unlink(missing_ok=True)
        await db.delete(upload)
        await db.flush()
        return None

    mime = _sniff_mime(part, upload.mime)
    digest = await to_thread.run_sync(_sha256_sync, part)
    # Stage beside the screen master in _unfiled/; link_attachments moves it into the memo folder.
    dest_dir = storage.unfiled_dir(_data_root())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}{Path(upload.filename).suffix or '.bin'}"
    rendition = AttachmentRendition(
        attachment_id=attachment.id,
        variant_key=parsed.variant_key,
        label=parsed.label,
        filename=upload.filename,
        mime=mime,
        size=actual,
        sha256=digest,
        storage_path=str(dest),
    )
    # Insert inside a savepoint BEFORE moving the file. The existence pre-check above is racy: a
    # CONCURRENT same-key finalize could pass it twice, so the UNIQUE constraint makes the loser
    # raise — the savepoint turns that into a graceful no-op. Moving the file only after the insert
    # wins means the loser leaves no orphan file behind.
    try:
        async with db.begin_nested():
            db.add(rendition)
            await db.flush()
    except IntegrityError:  # lost a concurrent race for this (attachment, variant_key) → no-op
        part.unlink(missing_ok=True)
        await db.delete(upload)
        await db.flush()
        return None

    await to_thread.run_sync(os.replace, part, dest)
    if moved_out is not None:
        moved_out.append(str(dest))
    await db.delete(upload)
    await db.flush()
    return rendition


async def reap_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete abandoned uploads (past expires_at) + part files. Worker-wired in T2.2."""
    now = now or datetime.now(UTC)
    rows = (await db.execute(select(Upload).where(Upload.expires_at < now))).scalars().all()
    for upload in rows:
        part_path(upload.upload_uid).unlink(missing_ok=True)
        await db.delete(upload)
    await db.flush()
    return len(rows)
