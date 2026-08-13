import hashlib
import os
import secrets
import shutil
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
from app.database import async_session
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
def _open_spool_sync(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "wb")


def _unlink_sync(path: Path) -> None:
    path.unlink(missing_ok=True)


def _concat_sync(spool: Path, part: Path, offset: int) -> None:
    """Append the spooled body onto the part file at `offset`. The caller holds the row lock and
    `offset` equals the committed end of the file, so this only ever EXTENDS it — bytes an earlier
    append committed are never rewritten. The closing truncate drops debris past the write (a
    process killed between concat and commit leaves the file longer than the recorded offset);
    those bytes are uncommitted by definition, so dropping them is safe."""
    part.parent.mkdir(parents=True, exist_ok=True)
    with open(part, "r+b" if part.exists() else "w+b") as dst, open(spool, "rb") as src:
        dst.seek(offset)
        shutil.copyfileobj(src, dst, STREAM_CHUNK_SIZE)
        dst.truncate()


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
    db: AsyncSession,
    *,
    owner_id: int,
    filename: str,
    mime: str,
    size: int,
    last_modified: int | None = None,
) -> Upload:
    """Open an upload session BOUND to `owner_id`. The uid is unguessable but is not a capability:
    every later verb re-checks the owner, so the binding — not the uid's secrecy — is what keeps
    another user out of these bytes."""
    upload_uid = secrets.token_urlsafe(24)
    part = part_path(upload_uid)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.touch()
    client_mtime = datetime.fromtimestamp(last_modified / 1000, UTC) if last_modified else None
    upload = Upload(
        upload_uid=upload_uid,
        owner_id=owner_id,
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


async def spool_stream(
    stream: AsyncIterable[bytes], path: Path, *, start: int, ceiling: int
) -> int:
    """Write `stream` into `path`, chunk by chunk, and stop the moment the RUNNING total — counted
    from `start`, the offset this body claims to continue from — would pass `ceiling`. The bytes
    are never accumulated in memory, so an over-limit body is refused at receive time
    (UploadTooLarge). Returns the byte count received.

    `path` MUST be private to this request: on any failure it is unlinked, so a request that is
    rejected or cut short discards only its own bytes and can never disturb shared state."""
    received = 0
    try:
        f = await to_thread.run_sync(_open_spool_sync, path)
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
        await to_thread.run_sync(_unlink_sync, path)
        raise
    return received


def size_ceiling(upload: Upload) -> int:
    """The lower of the session's declared size and the server cap. The cap is re-read here rather
    than trusted from session-create time, so a cap lowered since then still binds."""
    return min(upload.declared_size, settings.MAX_UPLOAD_MIB * 1024 * 1024)


def _spool_path(upload_uid: str) -> Path:
    """A spool file private to ONE append request — two concurrent appends to the same session get
    different names and so never write through each other."""
    return _data_root() / "uploads" / f"{upload_uid}.{secrets.token_urlsafe(8)}.spool"


async def append_stream(
    db: AsyncSession, upload: Upload, offset: int, stream: AsyncIterable[bytes]
) -> Upload:
    """Receive an append at `offset`: spool the body to a private file, then merge it into the
    shared part file. Idempotent — replaying an already-received range is a no-op; a forward gap
    raises ValueError (caller → 409); an over-ceiling body raises UploadTooLarge (caller → 413).

    The offset the body was built against may be STALE by the time it is fully received, so the
    merge re-reads the row FOR UPDATE and re-decides gap/replay/match against the refreshed
    offset. The row lock — not an in-process lock — is the serialization point because it is held
    until the caller COMMITS, which is when this append's bytes and offset actually become visible
    to another request; an in-process lock released at function exit would still let the next
    append read a pre-commit offset and write over these bytes."""
    # Offset precedence outranks the size ceiling: a body that cannot land whatever its size gets
    # the protocol's answer without being consumed at all — a gap is a 409 to re-sync from, and a
    # replay is a no-op. This is an EARLY EXIT on the request's own snapshot, not the decision of
    # record; the authoritative gap/replay/match call is re-taken under the row lock below against
    # the refreshed offset.
    if offset > upload.bytes_received:
        raise ValueError("offset gap")
    if offset < upload.bytes_received:
        return upload
    spool = _spool_path(upload.upload_uid)
    try:
        # Receive-time bound: no single body may exceed the session's whole ceiling, so an oversize
        # upload is refused mid-stream. Where it LANDS is not yet known — that needs the offset the
        # merge settles on, so the offset-relative check is the one under the lock below.
        received = await spool_stream(stream, spool, start=0, ceiling=size_ceiling(upload))
        upload = (
            await db.execute(
                select(Upload)
                .where(Upload.upload_uid == upload.upload_uid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if offset > upload.bytes_received:
            raise ValueError("offset gap")
        if offset == upload.bytes_received:
            if offset + received > size_ceiling(upload):
                raise UploadTooLarge(offset + received)
            await to_thread.run_sync(_concat_sync, spool, part_path(upload.upload_uid), offset)
            upload.bytes_received = offset + received
            await db.flush()
        # offset < bytes_received → a concurrent append already covered this range; no-op
        return upload
    finally:
        await to_thread.run_sync(_unlink_sync, spool)


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

    The lookup is scoped to `owner_id`, so this second entry point (memo create/edit carrying
    upload_uids) binds sessions exactly as the direct upload routes do: another user's uid — or a
    pre-ownership row with no owner — is INDISTINGUISHABLE from an unknown uid here and is simply
    skipped. Without that scope, submitting a foreign uid would finalize the victim's bytes into an
    attachment stamped with the SUBMITTER's owner_id.

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
            await db.execute(
                select(Upload).where(Upload.upload_uid == up_uid, Upload.owner_id == owner_id)
            )
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


def _reap_spools_sync(spool_dir: Path, cutoff: float) -> int:
    """Delete spool files last written before `cutoff`.

    Safe against an in-flight append: a spool's mtime advances on every chunk written to it, so a
    file untouched since a whole session-expiry window ago cannot still have a writer — while a
    live request's spool was touched a chunk ago. The `finally` in append_stream removes a spool
    the moment its request ends; this only catches what a killed process left behind."""
    if not spool_dir.is_dir():
        return 0
    reaped = 0
    for spool in spool_dir.glob("*.spool"):
        try:
            if spool.stat().st_mtime < cutoff:
                spool.unlink()
                reaped += 1
        except OSError:  # vanished under us — its own request just cleaned it up
            continue
    return reaped


async def reap_expired_committed(now: datetime | None = None) -> int:
    """The worker's reap entry: a dedicated session that COMMITS the deletions. A flush alone
    dies with the session, which is how expired rows resurrected every cycle — their part files
    already unlinked — while the hourly log counted the same rows forever (MEMNG-37)."""
    async with async_session() as db:
        reaped = await reap_expired(db)
        await db.commit()
    return reaped


async def reap_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete abandoned uploads (past expires_at) + part files, and any spool file a killed process
    orphaned mid-append. Returns the number of upload SESSIONS reaped. Worker-wired in T2.2."""
    now = now or datetime.now(UTC)
    rows = (await db.execute(select(Upload).where(Upload.expires_at < now))).scalars().all()
    for upload in rows:
        part_path(upload.upload_uid).unlink(missing_ok=True)
        await db.delete(upload)
    await db.flush()
    cutoff = (now - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS)).timestamp()
    await to_thread.run_sync(_reap_spools_sync, _data_root() / "uploads", cutoff)
    return len(rows)
