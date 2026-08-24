import hashlib
import os
import secrets
import shutil
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
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
    Memo,
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


class UploadsUnlinkable(Exception):
    """Requested upload_uids that cannot become attachments, split by cause (MEMNG-113).

    Raised BEFORE the batch's first side effect, so the caller's transaction still holds nothing to
    undo: no part file moved, no attachment row inserted, no upload row consumed. Silently skipping
    these instead is what let a client that uploaded everything, waited past the expiry and then
    posted receive a 201 for a TEXT-ONLY memo — after which an outbox client deletes its only copy
    of the media. `missing` deliberately also covers an upload owned by ANOTHER user: the lookup is
    owner-scoped, so a foreign uid is indistinguishable from one that never existed (the same
    fail-closed answer the direct upload routes give) and is no cross-user existence oracle."""

    def __init__(self, missing: list[str], incomplete: list[str]) -> None:
        super().__init__(
            f"{len(missing)} missing and {len(incomplete)} incomplete upload(s) cannot be linked"
        )
        self.missing = missing
        self.incomplete = incomplete


# Blocking disk I/O runs off the event loop (anyio.to_thread) so large chunk writes /
# sha256 reads don't stall the server.
def _open_spool_sync(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "wb")


def _unlink_sync(path: Path) -> None:
    path.unlink(missing_ok=True)


def _unlink_many_sync(paths: list[Path]) -> None:
    """A whole batch of part files in ONE hop — N separate offloads would be N round-trips
    against a bind-mounted share. Errors propagate exactly as a single unlink's would."""
    for path in paths:
        path.unlink(missing_ok=True)


def _create_part_sync(path: Path) -> None:
    """Create the empty part file and its parent directory in one hop."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _mkdir_sync(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _part_size_sync(path: Path) -> int:
    return path.stat().st_size


def _is_complete_sync(path: Path, declared_size: int) -> bool:
    """Whether the part file is fully received: the existence check and the size read share one
    hop. Semantics are the caller's previous `exists() and stat().st_size == declared` verbatim —
    including a stat that loses a race with an unlink still raising, rather than being softened
    into a False."""
    return path.exists() and path.stat().st_size == declared_size


def _move_into_sync(src: Path, dest: Path) -> None:
    """Create the destination folder and atomically move `src` onto `dest`, in one hop."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)


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
    await to_thread.run_sync(_create_part_sync, part)
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


async def _memo_screen_sibling(
    db: AsyncSession, memo: Memo, base: str, owner_id: int
) -> Attachment | None:
    """The memo's already-linked SCREEN attachment for `base`, or None.

    The match parses `Attachment.filename` — the client-facing name `finalize` copies verbatim off
    the upload and nothing ever rewrites — with the SAME grammar the batch was parsed with, so a
    screen stored as `X.png` or `X - screen.png` both answer to base `X`. The storage path is not
    usable for this: it carries a uuid prefix while staged and a sanitized, possibly ` (n)`-bumped
    name once placed, neither of which a dumped print's base can be compared against.

    A print that an earlier batch already PROMOTED to a visible photo parses as a print and is
    therefore never a bind target (repairing those rows is a separate job) — only a screen owns
    print masters. Rows are ordered by id so a memo pathologically carrying two same-base screens
    always yields the same (oldest) one: two identical calls must not bind to different rows.
    """
    rows = (
        (
            await db.execute(
                select(Attachment)
                .where(Attachment.memo_id == memo.id, Attachment.owner_id == owner_id)
                .order_by(Attachment.id)
            )
        )
        .scalars()
        .all()
    )
    for att in rows:
        parsed = parse_variant(att.filename)
        if not parsed.is_print and parsed.base == base:
            return att
    return None


async def finalize_pending(
    db: AsyncSession,
    upload_uids: list[str],
    owner_id: int,
    moved_out: list[str] | None = None,
    memo: Memo | None = None,
) -> list[str]:
    """Finalize-on-post: turn each fully-received pending upload into the owner's attachment and
    return their uids. A missing/expired or incomplete upload raises UploadsUnlinkable naming every
    offender, and the batch as a whole is refused (MEMNG-113) — an abandoned compose still creates
    no orphan attachment, because a memo that never mentions an upload never reaches this function.

    The lookup is scoped to `owner_id`, so this second entry point (memo create/edit carrying
    upload_uids) binds sessions exactly as the direct upload routes do: another user's uid — or a
    pre-ownership row with no owner — is INDISTINGUISHABLE from an unknown uid here and lands in
    `missing` exactly like one that never existed. Without that scope, submitting a foreign uid
    would finalize the victim's bytes into an attachment stamped with the SUBMITTER's owner_id.

    finalize()/finalize_rendition() move the part file to its final path BEFORE the (now shared)
    memo transaction commits — a non-transactional side effect. Each genuinely-moved path is
    appended to `moved_out` (inside those functions, new-file branch only) so the caller can
    unlink it if the post rolls back. A dedup return moves nothing and appends nothing.

    Screen/print grouping runs in three steps, in this order (MEMNG-79 — the in-batch-only rule of
    PLAN_2026-06-21_variant-renditions.md is superseded by the memo-sibling step):
    1. IN-BATCH: screens (non-print files) become Attachments first, then each print attaches to the
       same-base screen of this batch as a hidden rendition.
    2. MEMO SIBLING: a print with no screen in this batch binds onto the `memo` the batch is being
       finalized into, if that memo already carries the same-base screen (finalized by an earlier
       edit) — it fills that attachment's print slot instead of becoming a second visible copy of a
       photo the memo already shows. `memo` is None on create (the memo does not exist yet, so it
       can carry nothing) and on the one-shot finalize route; then this step cannot apply.
    3. PROMOTE: anything still unbound — no screen anywhere, an in-batch (screen, variant_key)
       collision, or a sibling whose print slot already holds a different master — becomes a normal
       picture (decision: a print is never silently dropped, never silently hidden without a
       screen)."""
    pending: list[tuple[Upload, ParsedName]] = []
    missing: list[str] = []
    incomplete: list[str] = []
    for up_uid in upload_uids:
        upload = (
            await db.execute(
                select(Upload).where(Upload.upload_uid == up_uid, Upload.owner_id == owner_id)
            )
        ).scalar_one_or_none()
        if upload is None:
            missing.append(up_uid)
            continue
        part = part_path(up_uid)
        if not await to_thread.run_sync(_is_complete_sync, part, upload.declared_size):
            incomplete.append(up_uid)
            continue
        pending.append((upload, parse_variant(upload.filename)))
    if missing or incomplete:
        # Before pass 1 on purpose: nothing has moved or been written yet, so the whole request can
        # fail with the caller's media still intact and re-postable.
        raise UploadsUnlinkable(missing, incomplete)

    out: list[str] = []
    base_to_attachment: dict[str, Attachment] = {}
    for upload, parsed in pending:  # PASS 1 — screens become Attachments (first-wins per base)
        if parsed.is_print:
            continue
        att = await finalize(db, upload, owner_id, moved_out)
        base_to_attachment.setdefault(parsed.base, att)
        out.append(att.uid)

    seen: set[tuple[int, str]] = set()
    # The memo sibling's print slot (print_path) holds exactly ONE master, so only the first print
    # of this batch may bind to a given sibling — a second would overwrite the first's file. The
    # loser is promoted, mirroring the in-batch (screen, variant_key) collision rule: never lost.
    bound_siblings: set[int] = set()
    for upload, parsed in pending:  # PASS 2 — batch screen, then memo sibling, else promote
        if not parsed.is_print:
            continue
        screen = base_to_attachment.get(parsed.base)
        vk = parsed.variant_key
        if screen is not None and vk is not None and (screen.id, vk) not in seen:
            await finalize_rendition(db, upload, screen, parsed, moved_out)
            seen.add((screen.id, vk))
            continue
        # No screen in THIS batch: before promoting, look for the screen on the memo being saved —
        # a print exported minutes after its screen (a later edit) belongs to that photo, not to a
        # duplicate of it. An in-batch (screen, variant_key) collision deliberately does NOT fall
        # through here: its loser keeps promoting exactly as before (that screen is in this batch).
        if screen is None and memo is not None:
            sibling = await _memo_screen_sibling(db, memo, parsed.base, owner_id)
            if (
                sibling is not None
                and sibling.id not in bound_siblings
                and await bind_print_to_sibling(db, upload, sibling, memo, moved_out)
            ):
                bound_siblings.add(sibling.id)
                continue
        att = await finalize(db, upload, owner_id, moved_out)
        out.append(att.uid)
    return out


async def finalize(
    db: AsyncSession, upload: Upload, owner_id: int, moved_out: list[str] | None = None
) -> Attachment:
    part = part_path(upload.upload_uid)
    actual = await to_thread.run_sync(_part_size_sync, part)
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
        await to_thread.run_sync(_unlink_sync, part)
        await db.delete(upload)
        await db.flush()
        return existing

    mime = await to_thread.run_sync(_sniff_mime, part, upload.mime)
    now = datetime.now(UTC)
    # Stage in originals/_unfiled/ — the memo (and so the per-memo folder) doesn't exist yet; the
    # master is atomically moved into originals/YYYY/MM/<memo-folder>/ on link_attachments.
    dest_dir = storage.unfiled_dir(_data_root())
    dest = dest_dir / f"{uuid.uuid4().hex}_{Path(upload.filename).name}"
    await to_thread.run_sync(_move_into_sync, part, dest)  # mkdir + os.replace in one hop
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
    actual = await to_thread.run_sync(_part_size_sync, part)
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
        await to_thread.run_sync(_unlink_sync, part)
        await db.delete(upload)
        await db.flush()
        return None

    mime = await to_thread.run_sync(_sniff_mime, part, upload.mime)
    digest = await to_thread.run_sync(_sha256_sync, part)
    # Stage beside the screen master in _unfiled/; link_attachments moves it into the memo folder.
    # The mkdir stays HERE rather than being folded into the move below: it must keep happening
    # before the savepoint insert, so a run that loses the race leaves the tree as it found it.
    dest_dir = storage.unfiled_dir(_data_root())
    await to_thread.run_sync(_mkdir_sync, dest_dir)
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
        await to_thread.run_sync(_unlink_sync, part)
        await db.delete(upload)
        await db.flush()
        return None

    await to_thread.run_sync(os.replace, part, dest)
    if moved_out is not None:
        moved_out.append(str(dest))
    await db.delete(upload)
    await db.flush()
    return rendition


@dataclass(frozen=True)
class PrintMasterPlacement:
    """Where `attach_print_master` put the bytes, and what the caller owes the filesystem."""

    rendition: AttachmentRendition
    displaced: str | None  # a file nothing references once the commit lands → delete AFTER it
    rollback_path: str  # this request's brand-new file → delete if the transaction rolls back


async def attach_print_master(
    db: AsyncSession,
    upload: Upload,
    attachment: Attachment,
    variant_key: str,
    label: str | None,
    dest_dir: Path,
) -> PrintMasterPlacement:
    """Attach — or REPLACE — the print master a person picked in the web UI (MEMNG-75). The
    interactive counterpart to `finalize_rendition`, which serves the filename-ingest path.

    Deliberately NOT write-once. An ingest re-dump of an existing key is a no-op because the dump
    is a mirror of a folder, so "already there" means "already correct". Someone clicking
    "Replace…" is stating the opposite intent, and keeping the old master while reporting success
    is the same class of silent lie MEMNG-74 fixed.

    Returns the row plus the path of the file it displaced, if any. The displaced file is NOT
    unlinked here: the caller removes it only AFTER its commit, so a transaction that rolls back
    leaves the surviving row pointing at a file that still exists — the invariant this function
    exists to protect is that the only copy of the CURRENT master is never destroyed before the
    transaction dereferencing it commits.

    Callers must take the attachment's row lock BEFORE reading the state passed in here (see the
    endpoint): that is what serialises two concurrent replaces of the same key, so the loser
    displaces the winner's file instead of deciding from state the winner already replaced.
    """
    part = part_path(upload.upload_uid)
    actual = await to_thread.run_sync(_part_size_sync, part)
    if actual != upload.declared_size:  # an abandoned/partial upload must not become a master
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")
    mime = await to_thread.run_sync(_sniff_mime, part, upload.mime)
    digest = await to_thread.run_sync(_sha256_sync, part)
    existing = (
        await db.execute(
            select(AttachmentRendition).where(
                AttachmentRendition.attachment_id == attachment.id,
                AttachmentRendition.variant_key == variant_key,
            )
        )
    ).scalar_one_or_none()
    old_path = existing.storage_path if existing is not None else None
    # ALWAYS a new, uniquely-named (O_EXCL) file — NEVER a write onto the file the current row still
    # names, not even when the client filename is identical. Writing in place keeps the folder
    # tidier, and an earlier draft did exactly that, but it destroys the only copy of the current
    # master BEFORE the transaction that dereferences it commits: a failed flush or commit then
    # leaves the still-committed row's size/sha256 describing bytes that no longer exist. The
    # invariant is worth the cosmetics — no committed row may ever disagree with its file, so the
    # old master survives until the new one is committed, and only then is it reaped. A same-name
    # replace therefore lands as `name (2).ext`; the post-commit reap frees `name.ext` again, so
    # nothing accumulates and the next replace takes the plain name back.
    dest = await storage.place_master(part, dest_dir, upload.filename)
    displaced, rollback_path = old_path, str(dest)
    try:
        rendition = (
            existing
            if existing is not None
            else AttachmentRendition(attachment_id=attachment.id, variant_key=variant_key)
        )
        rendition.label = label
        rendition.filename = upload.filename
        rendition.mime = mime
        rendition.size = actual
        rendition.sha256 = digest
        rendition.storage_path = str(dest)
        if existing is None:
            db.add(rendition)
        await db.delete(upload)
        await db.flush()
    except BaseException:
        # A raise between placement and return never reaches the handler's rollback-unlink (it
        # only learns the path from the return value) — reap the just-placed file here so a
        # failed flush cannot strand an orphan no row references. The old master is untouched.
        await to_thread.run_sync(_unlink_sync, dest)
        raise
    return PrintMasterPlacement(
        rendition=rendition, displaced=displaced, rollback_path=rollback_path
    )


async def bind_print_to_sibling(
    db: AsyncSession,
    upload: Upload,
    attachment: Attachment,
    memo: Memo,
    moved_out: list[str] | None = None,
) -> bool:
    """Bind a batch-orphan print onto an already-linked screen `attachment` of `memo`: fill its
    print slot (print_path/print_mime/print_size) and place the file beside the screen in the memo
    folder. Returns True when the print was consumed, False when it was left untouched for the
    caller to promote. Enqueues NO MediaJob — a print master is an archive file, not a tier.

    A slot already holding a DIFFERENT master is not touched — this returns False. Overwriting it
    would destroy a committed print the owner uploaded deliberately (two labels of one photo, or an
    earlier `/print` upload), and this call cannot sweep the superseded file afterwards: it runs
    inside the caller's memo transaction, which may still roll back. Promotion is the framework's
    existing never-lose answer, so the second master stays visible rather than vanishing.

    Replacement therefore demands PROVABLE same-file evidence, and the only evidence this path has
    is the stored name: the slot is three columns (path/mime/size) carrying no variant identity, and
    the newcomer was matched by INFERENCE (a shared base) rather than aimed at this attachment by an
    uploader. So it replaces only on an EXACT stored-name match of the sanitized client filename —
    NOT on `storage.is_placement_of`, whose ` (n)` bumped form is ambiguous here: the grammar admits
    a numeric label, so a stored `X - print (2).png` is either a bump of `X - print.png` or a
    genuine `print:2` master, and reading it as the former would destroy the latter. A bumped-form
    match consequently promotes. (`POST /api/attachments/{uid}/print` keeps the looser rule: there
    the uploader names the attachment whose one print slot it is replacing, which IS the evidence.)

    Only a freshly-placed file is tracked in `moved_out`. The replaced path must NOT be: a rolled
    back edit unlinking it would leave the committed print_path pointing at nothing."""
    part = part_path(upload.upload_uid)
    actual = await to_thread.run_sync(_part_size_sync, part)
    if actual != upload.declared_size:
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")

    # Lock the row before reading print_path so the placement decision and the column write see one
    # value — a concurrent print upload for the same attachment (the `/print` route takes the same
    # lock) could otherwise leave the column naming neither file.
    await db.refresh(attachment, ["print_path"], with_for_update=True)
    dest_dir = storage.memo_dir(_data_root(), memo.created_at, memo.uid)
    # The one file a re-send of THIS client filename can prove it owns: the exact name an
    # unobstructed placement of it produces. Any other occupant is another master → promote.
    same_file = dest_dir / storage.sanitize_name(upload.filename)
    old_print = Path(attachment.print_path) if attachment.print_path else None
    if old_print is not None and old_print != same_file:
        return False  # occupied by a different master → caller promotes instead

    mime = await to_thread.run_sync(_sniff_mime, part, upload.mime)  # before the move
    if old_print is not None:  # exact stored-name match ⇒ a re-send of this very master
        path = await storage.replace_master(part, old_print)
    else:
        path = await storage.place_master(part, dest_dir, upload.filename)
        if moved_out is not None:
            moved_out.append(str(path))
    attachment.print_path = str(path)
    attachment.print_mime = mime
    attachment.print_size = actual
    await db.delete(upload)
    await db.flush()
    return True


def _spool_owner_uid(spool: Path) -> str | None:
    """The upload session a spool file belongs to, or None if its name is not one this service
    writes. `_spool_path` composes `<upload_uid>.<token>.spool` out of two token_urlsafe values,
    and that alphabet contains no dot — so a well-formed name has exactly three fields and the
    first one IS the uid. Anything else is unattributable debris."""
    fields = spool.name.split(".")
    if len(fields) != 3 or "" in fields or fields[2] != "spool":
        return None
    return fields[0]


def _list_spools_sync(spool_dir: Path) -> list[Path]:
    return sorted(spool_dir.glob("*.spool")) if spool_dir.is_dir() else []


def _reap_spools_sync(spools: list[Path], cutoff: float, live_uids: frozenset[str]) -> int:
    """Delete the spool files that can no longer have a writer — and only those.

    The OWNING SESSION decides, not the clock. A spool belongs to one append request of one upload
    session, and while that session is open the file may be written again at any moment however old
    it looks: a stalled connection can leave an in-flight spool untouched for longer than the whole
    expiry window, and unlinking it under its writer fails that request. So a spool whose uid is
    still in `live_uids` is never touched, and one whose session is gone or already expired is
    debris a killed process left behind — the `finally` in append_stream removes a spool the moment
    its request ends normally.

    The mtime horizon stays as the fallback for names carrying no parseable uid: those have no
    session to ask about, so age is all there is to go on (they age out exactly as before)."""
    reaped = 0
    for spool in spools:
        try:
            uid = _spool_owner_uid(spool)
            if uid is not None:
                if uid in live_uids:
                    continue
            elif spool.stat().st_mtime >= cutoff:
                continue
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
    # List the spool directory BEFORE reading which sessions are live. In that order every listed
    # file's session row was already committed when the read below ran, so a session opened while
    # this sweep runs can never be mistaken for a dead one — its spool simply waits a cycle.
    spools = await to_thread.run_sync(_list_spools_sync, _data_root() / "uploads")
    rows = (await db.execute(select(Upload).where(Upload.expires_at < now))).scalars().all()
    # Every part file in ONE hop, still before the flush — an hourly sweep of a large backlog
    # would otherwise be that many blocking unlinks in a row.
    await to_thread.run_sync(_unlink_many_sync, [part_path(u.upload_uid) for u in rows])
    for upload in rows:
        await db.delete(upload)
    await db.flush()
    # Whatever survived the deletion above IS the live set. Reading it as that complement rather
    # than re-deriving it from expires_at keeps a never-expiring row (NULL expires_at), which the
    # row sweep also never collects, on the spared side instead of orphaning its spool.
    live_uids = frozenset((await db.execute(select(Upload.upload_uid))).scalars().all())
    cutoff = (now - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS)).timestamp()
    await to_thread.run_sync(_reap_spools_sync, spools, cutoff, live_uids)
    return len(rows)
