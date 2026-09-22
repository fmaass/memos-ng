import hashlib
import os
import secrets
import shutil
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Literal

import puremagic
import structlog
from anyio import fail_after, to_thread
from sqlalchemy import ColumnElement, or_, select
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
from app.services import asset_paths, capture, location, poster, storage
from app.services.variants import ParsedName, parse_variant

logger = structlog.get_logger(__name__)


def _data_root() -> Path:
    return Path(settings.DATA_ROOT)


# How long the capture read (MEMNG-262) may take before the upload is called settled without one.
# It is metadata only — a JPEG header, or a video's `moov` box found by seeking over the samples —
# so the budget is generous by an order of magnitude and exists for the pathological file, not the
# ordinary one. A timeout is a settled answer of "no date", never a stuck `pending`.
CAPTURE_READ_TIMEOUT_S = 5.0


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


# Why a video poster override can be refused, as a closed machine-readable set. Each value is one
# CATEGORY of reason, so a client can act on it without parsing prose: the `poster_missing` /
# `poster_incomplete` pair means "send those bytes again", every other value means "this request is
# wrong, do not repeat it". Exactly one is reported per rejected mapping — the first that applies,
# checked video-side before poster-side.
VideoPosterReason = Literal[
    # the mapping names a video that this memo is not posting: no such entry in upload_uids
    "video_not_uploaded",
    # the named upload is in the batch but its BYTES are not a video (mime is sniffed, not trusted)
    "video_not_a_video",
    # the named upload's filename carries the ingest grammar's print suffix (`… - print.ext`), so it
    # becomes a hidden print rendition of another photo rather than an attachment with a poster tier
    "video_is_a_print_variant",
    # no such poster upload for this caller — unknown, expired, or owned by somebody else (all one
    # answer on purpose: fail-closed, and no cross-user existence oracle)
    "poster_missing",
    # the poster upload exists but its bytes are not all there yet (resume it, then retry)
    "poster_incomplete",
    # the poster's bytes are not a JPEG, which is what the uid-keyed poster tier is served as
    "poster_not_a_jpeg",
    # the poster uid is ALSO listed in upload_uids: it would become a visible attachment of its own
    "poster_listed_as_attachment",
    # the same poster upload is mapped to a second video; one upload is one file, consumed once
    "poster_reused",
]


@dataclass
class PosterCopy:
    """One poster override on its way onto disk, and the arbitration that stops it from outliving
    the memo write it belongs to (`attach_video_poster`).

    A copy, unlike the rename every other placement here uses, leaves the source in place — so
    neither file can be settled until the transaction has: `placed` describes an attachment that
    may still be rolled back, and `part` still holds bytes an upload row may still promise. The
    caller removes exactly one of them:

    * the write does NOT land → `discard()`. The attachment row is rolled back, so the poster would
      be an orphan under a uid nothing references; `part` stays untouched, which is what leaves the
      client's poster re-postable (the restored Upload row names it, intact).
    * the write DOES land → unlink `part` after the commit. Its upload row is consumed, and the
      expiry reaper only unlinks part files by walking `uploads` rows, so a part left behind here
      is leaked permanently rather than collected later. A crash in between leaves at worst one
      unreferenced file, never a row without its bytes.

    The lock is what makes that a decision rather than a race. The copy runs in a worker thread,
    and a cancelled request does NOT wait for it: anyio raises the cancellation out of the await
    while the thread keeps going (verified against anyio 4.14 — an external task cancel abandons
    it, `abandon_on_cancel=False` notwithstanding). So publishing and discarding are serialised
    against each other and each acts on state the other cannot have changed underneath it:
    a discard that gets there first leaves `publish()` with nothing to do, and one that arrives
    mid-copy waits out the (bounded, sub-second) copy and then removes what it produced. Neither
    order can leave a file behind.
    """

    part: str  # the upload's part file — the copy READS it, so it stays where it is
    placed: str  # where the poster tier lands
    tmp: str  # the in-flight name inside the destination directory
    _lock: Lock = dataclass_field(default_factory=Lock, repr=False)
    _published: bool = False
    _discarded: bool = False

    def publish(self) -> None:
        """Put the picture in place. Runs in a worker thread, which nothing can interrupt — so this
        either finishes or fails, and never stops half way at someone else's request.

        Re-encoded, not copied: the destination is a DERIVED tier served to anyone the signed
        poster URL reaches, and a client JPEG carries whatever its camera wrote — coordinates
        included. `media.sanitize_poster_override` is the same date-only save every other derived
        asset goes through, and it keeps the copy's cross-device shape (a temp inside the
        DESTINATION directory, then a within-device rename), which is what the EXDEV incident
        behind `_copy_into_sync` bought.

        The import is deferred because `app/services/media.py` is the worker's module by design
        (see the split documented in `app/services/poster.py`): the app process pays for it only
        when a request actually brings a poster along.
        """
        from app.services import media

        with self._lock:
            if self._discarded:
                return  # the write unwound before this got its turn: publish nothing at all
            media.sanitize_poster_override(Path(self.part), Path(self.placed), Path(self.tmp))
            self._published = True

    def discard(self) -> list[str]:
        """Take back this copy because the write did not land; returns the paths it could NOT
        remove, for the caller to log.

        Never raises: it runs while an exception is already unwinding (a rollback, or a
        cancellation), where an error of its own would replace the outcome that got us here.
        """
        with self._lock:
            self._discarded = True
            doomed = [self.tmp] + ([self.placed] if self._published else [])
        failed: list[str] = []
        for path in doomed:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                failed.append(path)
        return failed


@dataclass(frozen=True)
class VideoPosterProblem:
    """One `video_posters` mapping that cannot be honoured, and why."""

    video_upload_uid: str
    poster_upload_uid: str
    reason: VideoPosterReason


class VideoPostersInvalid(Exception):
    """Every `video_posters` mapping a memo write cannot honour, one problem per mapping.

    Raised BEFORE the batch's first side effect, exactly like UploadsUnlinkable and for the same
    reason: the refusal has to leave the caller's media untouched and the request re-postable once
    the offending mapping is fixed. Reporting every offender in one answer (rather than the first)
    is what keeps a client from discovering them one round-trip at a time.
    """

    def __init__(self, problems: list[VideoPosterProblem]) -> None:
        super().__init__(f"{len(problems)} video poster override(s) cannot be consumed")
        self.problems = problems


@dataclass
class LockedMedia:
    """Everything pass 0 of `finalize_pending` acquired, handed to the per-upload work that follows
    so that work acquires NOTHING more (MEMNG-267 R3/R4).

    The batch used to alternate classes — an upload row, then an attachment row, then the next
    upload row — which is what let two batches hold each other's rows. Pass 0 takes the whole
    upload set in one ascending statement, does every read that could discover a new row (the
    completeness stat, the capture read, the sniff, the sha256), and only then takes the whole
    attachment set in one ascending statement. After it, the loop's dedup is a dictionary lookup
    and its `db.delete(upload)` calls can wait on nobody.

    `candidates` maps a sha256 to the unlinked attachment that holds those bytes, already
    revalidated under its lock (owner, and `memo_id IS NULL`). `finalize` also REGISTERS the rows
    it mints here: a fresh row is private to this transaction and carries no lock order (R8), and
    two uploads of identical bytes in one batch have always produced one attachment.
    """

    uploads: dict[str, Upload]
    digests: dict[str, str]
    candidates: dict[str, Attachment]


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


def reap_consumed_sync(paths: list[str]) -> None:
    """Unlink the part files of a COMMITTED write's consumed uploads (off-loop; never raises).

    Their Upload rows are gone for good now, and the expiry reaper only finds parts by walking
    those rows — so a part left here is leaked permanently rather than collected later. It is
    still only a leak: the write has landed, so a file that will not delete must be logged and
    left rather than turned into a 500 on a request that already succeeded.
    """
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            logger.error("uploads.consumed_reap_failed", path=path, error=str(exc)[:200])


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
    """Create the destination folder and atomically move `src` onto `dest`, in one hop.

    A rename, so both sides must live on ONE filesystem. Its callers all move an original within
    the data volume (uploads → originals/_unfiled → the memo folder), which is same-device by
    design; a derived asset under `assets/` is NOT — see `_copy_into_sync`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)


def _tracked_move_into_sync(track: storage.TrackedMove, dest: Path) -> Path:
    """`_move_into_sync` under the move's own arbitration — see `storage.TrackedMove.perform`.

    The mkdir it carries is a no-op for a caller that already made the directory
    (`finalize_rendition` has to, before its savepoint insert); one `exist_ok` mkdir is cheaper
    than a second tracked-move shape to maintain.
    """

    def move(src: Path) -> Path:
        _move_into_sync(src, dest)
        return dest

    return track.perform(move)


def _copy_into_sync(src: Path, dest: Path, tmp: Path) -> None:
    """Publish `src`'s bytes at `dest` atomically, WITHOUT assuming the two share a filesystem.

    In the deployed stack `/app/data/assets` is a separate mount from the rest of `/app/data`
    (derived tiers on NVMe, originals and upload parts on the HDD share), so renaming a part file
    onto a derived-asset path raises OSError EXDEV — "invalid cross-device link" — which is what
    took the first real poster override down in production. Copying into `tmp`, which the caller
    puts in the DESTINATION directory, keeps the publish itself a within-device rename, and that
    rename is what makes the file atomic for readers: nobody ever sees a half-written poster.

    Unconditional rather than a rename with an EXDEV fallback: the two would differ in whether
    `src` survives, so the caller's cleanup would have to differ too — and an env-dependent branch
    that only production ever takes is precisely the shape of the bug this replaces.

    fsync before the rename so the bytes are durable before a name points at them. `src` is left
    exactly as it was: the caller decides when its transaction has earned the right to remove it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(src, "rb") as reader, open(tmp, "wb") as writer:
            shutil.copyfileobj(reader, writer, STREAM_CHUNK_SIZE)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a half-written temp behind for the sweep to find
        raise


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


# The bytes every JPEG opens with: SOI (FF D8) plus the FF that begins the next marker segment
# (APP0/APP1/DQT/COM — all of them start FF). The same three-byte signature `file(1)` and puremagic
# match on, and the only positive JPEG identification this module needs.
JPEG_MAGIC = b"\xff\xd8\xff"


def _is_jpeg_sync(path: Path) -> bool:
    """Whether these bytes ARE a JPEG — a positive identification, not an absence of doubt.

    Deliberately NOT `_sniff_mime(...) == "image/jpeg"`. That helper answers with the CLIENT'S
    declared mime whenever puremagic raises or recognises nothing (`guessed or declared`), which is
    the right lenient default for an ordinary upload — the attachment merely carries a mime it may
    be wrong about — and exactly the wrong one for a gate: arbitrary unrecognisable bytes declared
    `image/jpeg` would pass it, land as the poster tier, and then be skipped by the worker (the
    file exists) and fail its blurhash decode, leaving the video with a broken poster and no way
    back. Anything this cannot read or does not recognise is refused.
    """
    try:
        with open(path, "rb") as f:
            return f.read(len(JPEG_MAGIC)) == JPEG_MAGIC
    except OSError:
        return False


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
) -> Upload | None:
    """Receive an append at `offset`: spool the body to a private file, then merge it into the
    shared part file. Idempotent — replaying an already-received range is a no-op; a forward gap
    raises ValueError (caller → 409); an over-ceiling body raises UploadTooLarge (caller → 413).
    Answers None when the session is GONE by the time the row lock is granted (caller → 404).

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
        # `scalar_one_or_none`, not `scalar_one`: waiting on this lock is exactly when the row can
        # disappear — the writer this append queued behind may have been a finalize, which consumes
        # the session and deletes it. A lock acquisition may lose its row, and losing it is an
        # ordinary answer, not a 500 (MEMNG-278; the protocol's R6a, the same contract
        # `ensure_capture` and `reclaim_attachment` already honour).
        locked = (
            await db.execute(
                select(Upload)
                .where(Upload.upload_uid == upload.upload_uid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked is None:
            return None
        upload = locked
        if offset > upload.bytes_received:
            raise ValueError("offset gap")
        if offset == upload.bytes_received:
            if offset + received > size_ceiling(upload):
                raise UploadTooLarge(offset + received)
            await to_thread.run_sync(_concat_sync, spool, part_path(upload.upload_uid), offset)
            upload.bytes_received = offset + received
            await db.flush()
            if upload.bytes_received >= upload.declared_size:
                # The upload just COMPLETED — the one moment its bytes are all here and nothing
                # has consumed them yet. The capture read happens here, inside the same row lock
                # and the same transaction as the offset that completed it, so it can neither run
                # twice nor be visible without the bytes it describes (MEMNG-262).
                # This transaction already holds the row, so the read cannot come back empty;
                # the walrus keeps the type honest without inventing a branch that cannot run.
                upload = await ensure_capture(db, upload) or upload
        # offset < bytes_received → a concurrent append already covered this range; no-op
        return upload
    finally:
        await to_thread.run_sync(_unlink_sync, spool)


async def append_chunk(db: AsyncSession, upload: Upload, offset: int, data: bytes) -> Upload | None:
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


async def _sniffed_mime(upload: Upload) -> str:
    """What an upload's BYTES are — read the same way `finalize` reads them, so a poster override
    is matched to the mime its attachment will actually be stored with.

    Carries `_sniff_mime`'s fallback to the declared mime for bytes puremagic cannot place, which
    is why it decides only the VIDEO side of a mapping (an unrecognisable file declared a video
    becomes a video attachment here exactly as it does without an override). The poster side is
    gated by `_is_jpeg_sync` instead, which refuses anything it cannot positively identify.
    """
    return await to_thread.run_sync(_sniff_mime, part_path(upload.upload_uid), upload.mime)


async def _resolve_video_poster(
    db: AsyncSession,
    video_uid: str,
    poster_uid: str,
    pending: dict[str, tuple[Upload, ParsedName]],
    claimed: set[str],
    owner_id: int,
    locked: LockedMedia | None = None,
) -> tuple[Upload | None, VideoPosterReason | None]:
    """One mapping: the poster Upload it will consume, or the reason it cannot be honoured.

    Video side first, poster side second, and the first failure is the answer — a mapping whose
    video is not even in this memo says nothing useful about its poster.
    """
    entry = pending.get(video_uid)
    if entry is None:  # not in upload_uids (every uid that IS has a complete row by now)
        return None, "video_not_uploaded"
    video, parsed = entry
    if parsed.is_print:
        # A print-suffixed name binds as a hidden rendition of another photo (see the pass 2 rules
        # below) or is promoted as a still picture; either way this upload owns no poster tier.
        return None, "video_is_a_print_variant"
    if not (await _sniffed_mime(video)).startswith("video/"):
        return None, "video_not_a_video"
    if poster_uid in pending:
        # Listed in upload_uids AND named as a poster: it cannot be both a visible attachment of
        # this memo and the derived asset of one. Refusing beats guessing — silently dropping it
        # from the attachments is how a memo loses media its author sent.
        return None, "poster_listed_as_attachment"
    if poster_uid in claimed:
        return None, "poster_reused"
    if locked is not None:
        # Read the row pass 0 LOCKED, not a fresh unlocked one: the poster is deleted later in this
        # same transaction, and R4 requires the row it will delete to be one it already holds. The
        # pass-0 statement was owner-scoped, so a foreign uid is absent here exactly as it was
        # absent from the old owner-scoped select.
        poster_upload = locked.uploads.get(poster_uid)
    else:
        poster_upload = (
            await db.execute(
                select(Upload).where(Upload.upload_uid == poster_uid, Upload.owner_id == owner_id)
            )
        ).scalar_one_or_none()
    if poster_upload is None:  # unknown, expired, or another user's — one fail-closed answer
        return None, "poster_missing"
    complete = await to_thread.run_sync(
        _is_complete_sync, part_path(poster_uid), poster_upload.declared_size
    )
    if not complete:
        return None, "poster_incomplete"
    if not await to_thread.run_sync(_is_jpeg_sync, part_path(poster_uid)):
        # A POSITIVE identification (see _is_jpeg_sync), never `_sniff_mime`'s declared-mime
        # fallback: what the caller called these bytes cannot be what decides they may become a
        # tier served to everyone as image/jpeg.
        return None, "poster_not_a_jpeg"
    return poster_upload, None


async def _validate_video_posters(
    db: AsyncSession,
    video_posters: dict[str, str],
    pending: dict[str, tuple[Upload, ParsedName]],
    owner_id: int,
    locked: LockedMedia | None = None,
) -> dict[str, Upload]:
    """Resolve every video→poster mapping to the poster Upload it consumes, or refuse the batch.

    Runs before pass 1 — before anything has moved — so a refusal costs the caller nothing: no part
    file relocated, no attachment row inserted, no upload consumed, and the identical request
    succeeds once the named mappings are fixed.

    Mappings are checked in sorted order so a request carrying several offenders always reports the
    same problems in the same order (the `poster_reused` loser included, which would otherwise
    depend on JSON key order).
    """
    problems: list[VideoPosterProblem] = []
    resolved: dict[str, Upload] = {}
    claimed: set[str] = set()
    for video_uid, poster_uid in sorted(video_posters.items()):
        poster_upload, reason = await _resolve_video_poster(
            db, video_uid, poster_uid, pending, claimed, owner_id, locked
        )
        if reason is not None:
            problems.append(VideoPosterProblem(video_uid, poster_uid, reason))
            continue
        assert poster_upload is not None  # no reason ⇒ a resolved row (mutually exclusive)
        claimed.add(poster_uid)
        resolved[video_uid] = poster_upload
    if problems:
        raise VideoPostersInvalid(problems)
    return resolved


async def attach_video_poster(
    db: AsyncSession,
    upload: Upload,
    attachment: Attachment,
    poster_copies_out: list[PosterCopy],
) -> Path:
    """Consume a validated poster upload as `attachment`'s poster tier; returns where it landed.

    The bytes are moved into the exact path the worker would have written its hero frame to
    (app/services/poster.py), so every consumer — the signed poster tier, the grid tile, the
    lightbox, the export — picks the override up with no knowledge that it is one, and the worker
    skips deriving a frame it would only overwrite (media.process_video_poster).

    The poster is deliberately NOT an Attachment. It is a derived asset of the video, so it must
    stay out of every surface that walks attachment rows — the feed, the library, the compat API,
    an export listing, and above all the #curated frame-share queue, which mails every image
    ATTACHMENT of a curated memo to a photo frame (app/services/frame_share.py).

    The bytes are COPIED, not renamed: the poster tier lives under `assets/`, which is a separate
    mount from the upload parts in the deployed stack, and a rename across it fails with EXDEV
    (see `_copy_into_sync` — this is the production incident that followed the feature's first
    real use). So the placement is NOT recorded in `moved_out`, whose restore would re-attempt
    exactly that cross-device rename; it is recorded as a `PosterCopy`, which the caller settles
    after its commit — discarding the copy if the write did not land, and unlinking the
    now-consumed part file if it did.

    The record is appended BEFORE the copy starts, and deliberately not after the await returns.
    A request cancelled mid-copy raises out of that await while the worker thread carries on
    (see PosterCopy), so a record appended afterwards would never exist — and the thread would
    then place a file no cleanup could ever know about, orphaned under a rolled-back uid.

    The destination always belongs to a brand-new attachment uid — a video carrying an override
    finalizes with dedup disabled (see finalize_pending) — so this can never displace a poster some
    other memo is already showing.
    """
    part = part_path(upload.upload_uid)
    actual = await to_thread.run_sync(_part_size_sync, part)
    if actual != upload.declared_size:  # re-checked here, as finalize does for a master
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")
    dest = poster.poster_path(attachment.uid)
    record = PosterCopy(
        part=str(part), placed=str(dest), tmp=str(poster.poster_tmp_path(attachment.uid))
    )
    poster_copies_out.append(record)  # before the copy, never after — see above
    await to_thread.run_sync(record.publish)
    await db.delete(upload)
    await db.flush()
    return dest


async def _consume(part: Path, consumed_out: list[str] | None) -> None:
    """Settle the part file of an upload this call consumed WITHOUT moving it — a dedup hit, a
    write-once rendition re-dump, a lost rendition race. Each of those deletes the Upload row and
    returns something that already exists on disk.

    Deleting the bytes here was wrong (MEMNG-142): the row deletion is transactional and the
    unlink is not, so a LATER failure in the same write — another upload's finalize, the memo
    commit itself — rolls the row back to pending while its bytes are already gone. The retry the
    client is entitled to then fails `uploads_incomplete` on a row the API says is consumable, and
    nothing can put it right: the only copy was on the client.

    So a caller that has a transaction to settle passes `consumed_out` and unlinks after its
    commit (the same after-the-commit reap a consumed poster's part gets — see `PosterCopy`);
    the record is appended with NO await between the decision and the append, so a cancellation
    cannot leave a consumed part unrecorded. `None` keeps the old immediate unlink for a caller
    with nothing to roll back to.
    """
    if consumed_out is not None:
        consumed_out.append(str(part))
        return
    await to_thread.run_sync(_unlink_sync, part)


async def ensure_capture(db: AsyncSession, upload: Upload) -> Upload | None:
    """Read this upload's capture date ONCE, as soon as all of its bytes are here (MEMNG-262).

    Two callers reach it: the append that lands the last chunk, and the inspection endpoint. The
    second one to arrive does not read the file again — the row lock is the serialization point,
    so it blocks, then finds `capture_state == 'done'` and JOINS the first one's answer. That is
    also what keeps a retried or duplicated request from turning into two reads of the same file.

    The inspection endpoint calls it for two cases the append cannot cover: a session that
    completed BEFORE this code shipped (its state is the column's 'pending' default and no further
    append will ever come), and a zero-byte file, which is complete without a single append. Both
    would otherwise leave a composer waiting for a decision that could never arrive.

    Blocking work runs in a worker thread under a deadline. On timeout the read is ABANDONED — the
    thread finishes into nothing — and the upload settles with no date, because a capture date is
    a nicety and a stuck upload is not.

    Answers None when the row is GONE. Waiting on the lock is exactly when it can disappear: the
    writer this call queued behind may have been another finalize, which consumes the session and
    deletes the row. A caller that finds None must take its "unknown upload" path — the bytes and
    the row it was about to work from are somebody else's now.
    """
    locked = (
        await db.execute(
            select(Upload)
            .where(Upload.upload_uid == upload.upload_uid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:  # reaped or finalized under us — see the docstring
        return None
    if locked.capture_state == "done":
        return locked
    part = part_path(locked.upload_uid)
    if not await to_thread.run_sync(_is_complete_sync, part, locked.declared_size):
        return locked  # still uploading — stays pending, which is exactly what it is
    mime = await to_thread.run_sync(_sniff_mime, part, locked.mime)
    result = capture.NOTHING
    try:
        with fail_after(CAPTURE_READ_TIMEOUT_S):
            result = await to_thread.run_sync(
                capture.read_capture, part, mime, abandon_on_cancel=True
            )
    except TimeoutError:
        # The uid and the mime only — a capture read never puts metadata in a log line.
        logger.warning("capture.read_timeout", upload_uid=locked.upload_uid, mime=mime)
    locked.capture_instant = result.capture_instant
    locked.capture_civil = result.capture_civil
    locked.capture_source = result.capture_source
    locked.capture_state = "done"
    await db.flush()
    return locked


async def _read_location(dest: Path, mime: str) -> tuple[float | None, float | None, str]:
    """This attachment's coordinates, read from its FINAL path — or why there are none.

    Deliberately not on the `uploads` row, where the capture date lives: a capture date is read
    when the last chunk lands and is wanted by the composer before a memo exists, while a location
    is wanted only by the map and belongs to the attachment. Keeping it off the session means an
    upload row — which outlives a cancelled compose, and which several endpoints serialize — can
    never carry a coordinate at all.

    One header read, in a worker thread, under the capture read's deadline. A timeout or an
    unreadable original settles as `'unreadable'`, which `backfill_location.py --retry-unreadable`
    re-reads later; it never fails the upload, because a stored photo with no pin is a far better
    outcome than a refused one. The warning carries the mime and the exception TYPE only — a
    location read never puts a value in a log line.
    """
    try:
        with fail_after(CAPTURE_READ_TIMEOUT_S):
            found = await to_thread.run_sync(
                location.read_location, dest, mime, abandon_on_cancel=True
            )
    except TimeoutError:
        logger.warning("location.read_timeout", mime=mime)
        return None, None, location.SOURCE_UNREADABLE
    except Exception as exc:  # noqa: BLE001 — a metadata read must never fail an upload
        logger.warning("location.read_failed", mime=mime, error=type(exc).__name__)
        return None, None, location.SOURCE_UNREADABLE
    if found is None:
        return None, None, location.SOURCE_NONE
    return found.latitude, found.longitude, found.source


async def _lock_attachments(
    db: AsyncSession, predicates: Sequence[ColumnElement[bool]]
) -> set[int]:
    """Take ONE ascending FOR UPDATE over every attachment any of `predicates` names, and return
    the ids it ACTUALLY RETURNED.

    `populate_existing` because a row the caller already read unlocked is in the identity map, and
    every revalidation behind this has to judge the LOCKED row. The returned ids are the answer to
    a question `populate_existing` cannot answer on its own: a refresh says nothing about a row
    that is absent from the result, and absent is precisely what a row deleted between the unlocked
    read and the lock looks like.
    """
    if not predicates:
        return set()
    return {
        row.id
        for row in (
            (
                await db.execute(
                    select(Attachment)
                    .where(or_(*predicates))
                    .order_by(Attachment.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    }


async def acquire_attachment_set(
    db: AsyncSession, *, memo: Memo | None = None, attachment_ids: Sequence[int] = ()
) -> None:
    """Take the attachment class the way `finalize_pending`'s pass 0 takes it, for a caller that
    has no uploads to finalize.

    An edit that only re-declares a memo's picture list still writes attachments — it links,
    reorders and detaches them — so those rows have to be held before the first of those writes, in
    the ascending order every other actor takes them in. With uploads, pass 0 carries that; without
    them there is no pass 0 to carry it, and acquiring the rows where they are first written would
    be a second acquisition in an order nothing else shares.
    """
    predicates: list[ColumnElement[bool]] = []
    if attachment_ids:
        predicates.append(Attachment.id.in_(set(attachment_ids)))
    if memo is not None:
        predicates.append(Attachment.memo_id == memo.id)
    await _lock_attachments(db, predicates)


async def finalize_pending(
    db: AsyncSession,
    upload_uids: list[str],
    owner_id: int,
    moved_out: list[storage.TrackedMove] | None = None,
    memo: Memo | None = None,
    video_posters: dict[str, str] | None = None,
    poster_copies_out: list[PosterCopy] | None = None,
    consumed_out: list[str] | None = None,
    positions_out: dict[str, int] | None = None,
    resolved_out: dict[str, str] | None = None,
    named_attachment_ids: Sequence[int] | None = None,
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
    appended to `moved_out` as (source, destination) (inside those functions, new-file branch only)
    so a caller whose post rolls back can move it BACK to where it came from, leaving the upload
    pending and intact. A dedup return moves nothing, and appends its part file to `consumed_out`
    instead: those bytes are still promised by an Upload row this transaction may yet restore, so
    they are unlinked by the CALLER after its commit, never here (MEMNG-142, see `_consume`).

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
       screen).

    `video_posters` maps a VIDEO upload_uid in this batch to a POSTER upload_uid that is NOT in it.
    The poster never becomes an Attachment: it becomes that video's poster tier
    (`attach_video_poster`), replacing the hero frame the worker would otherwise seek out. Every
    mapping is validated before pass 1 and the batch is refused WHOLE (VideoPostersInvalid) if any
    one of them cannot be honoured — the same all-or-nothing rule, for the same reason, as the
    missing/incomplete refusal above. A poster is COPIED into place, so passing `video_posters`
    also means passing `poster_copies_out` for the caller to settle after its commit (PosterCopy);
    omitting it is refused outright rather than leaking a file per post.

    `named_attachment_ids` are the ids of attachments the CALLER will link alongside this batch's
    output — the create route's back-compat `attachment_uids`, resolved by the router. They are not
    finalized here and nothing reads them; they exist so pass 0's single ascending attachment
    acquisition covers the whole set the transaction touches (MEMNG-267 R3.4), instead of leaving
    `link_attachments` to acquire them afterwards in an order no other writer shares.

    `positions_out` collects attachment uid -> the 0-based index, IN `upload_uids`, of the uid that
    produced or named it (MEMNG-262). It is what the caller SORTS the returned uids by, so the
    memo's attachments are linked in the order the client declared (`link_attachments` then ranks
    them, since a bound print and a dedup hit consume no place of their own). It is the ORIGINAL
    request index in every branch: a screen finalized in pass
    1, a print promoted in pass 2 (its own uid's index), and an attachment the dedup path handed
    back (the index of the uid that named it). A print BOUND to a screen adds nothing — it rides
    with that screen and takes no place of its own. The returned uid list keeps its pass order;
    a caller that wants the DECLARED order sorts it by this map.

    `resolved_out` runs the other way and is TOTAL over `upload_uids`: every uid in the batch ->
    the uid of the attachment it produced, deduped onto, or was BOUND to. A client that names its
    pictures cannot name a new one by its attachment uid, because that uid is minted in here and
    never leaves before this call returns; this is the map that turns what the client CAN say into
    what the memo holds. `positions_out` cannot serve: it is keyed the other way, so it has no
    inverse for two uids that deduped onto one attachment, and a print bound to a screen never
    reaches the code that writes it at all."""
    if video_posters and poster_copies_out is None:
        raise ValueError("video_posters requires poster_copies_out to settle after the commit")
    # First mention wins: the same uid twice, or two uids whose bytes dedup onto one attachment,
    # both name ONE attachment, and its place is where the client first asked for it.
    request_index: dict[str, int] = {}
    for position, up_uid in enumerate(upload_uids):
        request_index.setdefault(up_uid, position)
    # ---- PASS 0 (MEMNG-267 R3/R4/R4a) ------------------------------------------------------
    # ONE ascending acquisition of the upload class, over the UNION of the two roles: the uploads
    # that become attachments and the uploads that become poster tiers. Every row this transaction
    # will later DELETE is in it, so no deletion can wait once an attachment row is held; a uid
    # named in both roles is locked once, because this is a set.
    requested = set(upload_uids) | set((video_posters or {}).values())
    locked_uploads: dict[str, Upload] = {
        row.upload_uid: row
        for row in (
            (
                await db.execute(
                    select(Upload)
                    .where(Upload.upload_uid.in_(requested), Upload.owner_id == owner_id)
                    .order_by(Upload.upload_uid)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    }

    pending: list[tuple[Upload, ParsedName]] = []
    missing: list[str] = []
    incomplete: list[str] = []
    # The ATTACHMENT role. Its continuation is the one it has always had — a 409 naming every
    # offender — and the poster role's is a 422 in FastAPI's validation shape. One acquisition, two
    # continuations: merging them would answer `poster_missing` with the wrong status (R4a).
    for up_uid in upload_uids:
        upload = locked_uploads.get(up_uid)
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

    # Keyed by uid for the poster validation below: every uid in upload_uids now has a complete,
    # owner-scoped row, so a mapping whose video is absent here names a video this memo is not
    # posting at all.
    by_uid = {upload.upload_uid: (upload, parsed) for upload, parsed in pending}
    # The POSTER role, resolved against the SAME locked set and refused whole before any
    # attachment is touched — so a batch this rejects has locked no attachment at all.
    locked = LockedMedia(uploads=locked_uploads, digests={}, candidates={})
    posters = (
        await _validate_video_posters(db, video_posters, by_uid, owner_id, locked)
        if video_posters
        else {}
    )

    # Every read that could otherwise DISCOVER an attachment late, done here under the upload
    # locks: the capture read, and the digest the dedup decision is made from. Hashing a print that
    # will bind to a memo sibling is one extra read of a file that is about to be moved anyway —
    # the alternative, hashing lazily, is precisely the defect being fixed, because it finds a
    # dedup candidate AFTER the attachment set has been locked.
    for upload, _parsed in pending:
        settled = await ensure_capture(db, upload)
        if settled is None:  # cannot happen under the lock above; answered as an unknown uid would
            raise UploadsUnlinkable([upload.upload_uid], [])
        locked.uploads[settled.upload_uid] = settled
        locked.digests[settled.upload_uid] = await to_thread.run_sync(
            _sha256_sync, part_path(settled.upload_uid)
        )

    # An UNLOCKED read that makes no ownership claim — it produces ids, and every one of them is
    # revalidated under its own lock below. `ORDER BY id` so the candidate a digest resolves to is
    # the same one on every run (it was arbitrary under a bare LIMIT 1).
    for att in (
        (
            await db.execute(
                select(Attachment)
                .where(
                    Attachment.sha256.in_(set(locked.digests.values())),
                    Attachment.owner_id == owner_id,
                    Attachment.memo_id.is_(None),
                )
                .order_by(Attachment.id)
            )
        )
        .scalars()
        .all()
        if locked.digests
        else []
    ):
        if att.sha256 is not None:
            locked.candidates.setdefault(att.sha256, att)

    # ONE ascending acquisition of the attachment class, over the COMPLETE set this transaction
    # will touch: the dedup candidates, the attachments the caller NAMED (the create's back-compat
    # `attachment_uids`), and the memo's own attachments. `link_attachments` and a sibling print
    # bind take all three afterwards, and after this they re-lock rather than acquire — which is
    # what makes "nothing below acquires another row" true rather than nearly true. A named
    # attachment left out of it is a second acquisition in an order nothing else shares, and R7a's
    # sweep — which takes its terminal attachments ascending — inverts against exactly that.
    # `populate_existing` because the candidates are already in the identity map from the unlocked
    # read above, and the revalidation must judge the LOCKED row.
    a_predicates = []
    if locked.candidates:
        a_predicates.append(Attachment.id.in_({a.id for a in locked.candidates.values()}))
    if named_attachment_ids:
        a_predicates.append(Attachment.id.in_(set(named_attachment_ids)))
    if memo is not None:
        a_predicates.append(Attachment.memo_id == memo.id)
    # Judging the stale identity-map object instead of what the lock returned would let a
    # reclaimed attachment pass `memo_id IS NULL` and be handed back as this upload's attachment —
    # a memo stored with no media at all, its upload row deleted and its part file reaped.
    live_ids = await _lock_attachments(db, a_predicates)
    # Revalidated under the lock, which is what Postgres used to do implicitly by re-evaluating the
    # predicate after the wait: a candidate that has since been deleted, linked or changed hands is
    # dropped, and its upload mints its own attachment and its own file instead of stealing one or
    # reusing a row that is not there any more (X2).
    locked.candidates = {
        digest: att
        for digest, att in locked.candidates.items()
        if att.id in live_ids and att.owner_id == owner_id and att.memo_id is None
    }
    # …and then the stat, under the same lock and before a single upload is consumed. A row can
    # satisfy every column above and name bytes that are no longer there (`asset_paths.adoptable`);
    # the discovery read could not answer that, because the files may go in the gap between it and
    # the lock. Held to commit, so the answer cannot go stale under this transaction either.
    locked.candidates = {
        digest: att for digest, att in locked.candidates.items() if await asset_paths.adoptable(att)
    }
    # ---- end of pass 0. Nothing below acquires another row. ---------------------------------

    async def _finalize_one(upload: Upload) -> Attachment:
        """Finalize one upload as an attachment and consume its poster override, if it has one.

        Dedup is disabled for an upload that carries one. Reusing an unlinked twin of the same bytes
        would point the override at an attachment this request did not create — one that may already
        be showing a poster of its own, which the placement would then overwrite (and a rolled-back
        write would restore as nothing at all). A fresh row costs one more copy of a video that was
        never attached to anything and keeps the override's destination provably unused.
        """
        poster_upload = posters.get(upload.upload_uid)
        att = await finalize(
            db,
            upload,
            owner_id,
            moved_out,
            dedup=poster_upload is None,
            consumed_out=consumed_out,
            locked=locked,
        )
        if poster_upload is not None:
            assert poster_copies_out is not None  # guaranteed by the guard above
            await attach_video_poster(db, poster_upload, att, poster_copies_out)
        if resolved_out is not None:
            # The only name the CLIENT can use for this picture. It holds the upload uid and never
            # sees the attachment uid this call minted — or the uid of the unlinked twin dedup
            # handed back instead — so a declared picture list can reach this attachment through
            # nothing else.
            resolved_out[upload.upload_uid] = att.uid
        if positions_out is not None and upload.upload_uid in request_index:
            # The EARLIEST request index wins, not the first one to be finalized. Pass 2 runs after
            # pass 1, and a print promoted there can dedup onto an attachment pass 1 already made
            # from identical bytes — so the uid that asked for it FIRST is the one whose place the
            # attachment takes, whichever pass happened to create it.
            rank = request_index[upload.upload_uid]
            current = positions_out.get(att.uid)
            positions_out[att.uid] = rank if current is None else min(current, rank)
        return att

    out: list[str] = []
    base_to_attachment: dict[str, Attachment] = {}
    for upload, parsed in pending:  # PASS 1 — screens become Attachments (first-wins per base)
        if parsed.is_print:
            continue
        att = await _finalize_one(upload)
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
            await finalize_rendition(db, upload, screen, parsed, moved_out, consumed_out)
            seen.add((screen.id, vk))
            if resolved_out is not None:
                # A print bound as a rendition mints no attachment of its own, so the picture the
                # client named by this upload uid IS the screen it bound to.
                resolved_out[upload.upload_uid] = screen.uid
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
                if resolved_out is not None:
                    # Same as the in-batch bind: this upload became the sibling's print master, so
                    # the sibling is the picture its upload uid names.
                    resolved_out[upload.upload_uid] = sibling.uid
                continue
        att = await _finalize_one(upload)
        out.append(att.uid)
    return out


async def finalize(
    db: AsyncSession,
    upload: Upload,
    owner_id: int,
    moved_out: list[storage.TrackedMove] | None = None,
    *,
    dedup: bool = True,
    consumed_out: list[str] | None = None,
    locked: LockedMedia | None = None,
) -> Attachment:
    """Turn one fully-received upload into this owner's attachment (or, by dedup, hand back the
    unlinked one that already holds these bytes).

    `locked` is what a BATCH passes: the upload set, the digests and the dedup candidates its
    pass 0 already acquired and revalidated (MEMNG-267 R3). With it, this call takes no attachment
    row lock of its own — the candidate is a dictionary lookup, and the row behind it is already
    held. Without it (the standalone finalize route) the behaviour is unchanged, down to the single
    `FOR UPDATE` statement that lets Postgres re-evaluate the predicate for the loser of a race.

    `dedup=False` opts out of that reuse and always creates a new row. Its one caller is a video
    carrying a client-supplied poster: the override is written to a path derived from the returned
    attachment's uid, so that uid has to be one this request minted (see finalize_pending).

    `consumed_out` collects the part files this call CONSUMED without moving — see
    `finalize_pending` for why the caller has to unlink them after its commit rather than here.
    """
    # Under the upload's row lock BEFORE anything is copied off it. The append that completes an
    # upload writes its capture read in the same transaction as the last chunk, so a finalize that
    # raced it would otherwise find the bytes complete on disk and the capture columns still empty
    # — and stamp an attachment with no date the file plainly has. Locking queues this finalize
    # behind that commit; `ensure_capture` also performs the read itself when nothing has yet
    # (a session that completed before this feature shipped, or a zero-byte file).
    settled = await ensure_capture(db, upload)
    if settled is None:
        # Gone while this call waited for the lock — another finalize consumed it. Its part file
        # is that finalize's business now (moved into place, or reaped after its commit), so this
        # one must not touch it: it answers exactly as an unknown uid does.
        raise UploadsUnlinkable([upload.upload_uid], [])
    upload = settled
    part = part_path(upload.upload_uid)
    actual = await to_thread.run_sync(_part_size_sync, part)
    if actual != upload.declared_size:
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")

    digest = (locked.digests if locked is not None else {}).get(upload.upload_uid)
    if digest is None:
        digest = await to_thread.run_sync(_sha256_sync, part)

    # sha256 dedup — identical bytes already stored BY THIS OWNER → return the existing
    # attachment. Scoped per-owner: a global dedup would hand one user a reference to another
    # user's attachment row (cross-user linkage), so dedup only within the finalizing owner.
    # ONLY dedup against an UNLINKED attachment (memo_id IS NULL): an attachment already linked to a
    # memo is that memo's — reusing it would let link_attachments re-point memo_id and STRAND the
    # original memo (the same photo in a second memo must get its own row + file, like renditions).
    # limit(1) since several memos may legitimately share identical bytes now.
    if not dedup:
        existing = None
    elif locked is not None:
        # The batch: the candidate was resolved, LOCKED and revalidated in pass 0, so this decision
        # acquires nothing. A digest the map does not name has no reusable twin (or had one that
        # failed revalidation), and this upload mints its own row below.
        existing = locked.candidates.get(digest)
    else:
        existing = (
            (
                await db.execute(
                    select(Attachment)
                    .where(
                        Attachment.sha256 == digest,
                        Attachment.owner_id == owner_id,
                        Attachment.memo_id.is_(None),
                    )
                    # ORDER BY id so several identical unlinked twins always yield the same one:
                    # a bare LIMIT 1 picked arbitrarily, which is not something a probe can pin.
                    .order_by(Attachment.id)
                    .limit(1)
                    # FOR UPDATE so two concurrent posts can't both reuse the same unlinked row: the
                    # loser blocks, then Postgres re-evaluates `memo_id IS NULL` against the
                    # now-linked row, excludes it, and the loser creates its own attachment instead
                    # of stealing.
                    .with_for_update()
                    # `populate_existing` for the same reason the batch's acquisition carries it: a
                    # service session that already holds this row would otherwise be judged on the
                    # snapshot it loaded it in, and `memo_id` / `detached_at` are exactly the
                    # columns another transaction moves under it.
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        # The locked stat, in the path the batch's pass 0 does not cover: the same candidate, the
        # same question, asked where this route resolves it.
        if existing is not None and not await asset_paths.adoptable(existing):
            existing = None
    if existing is not None:
        # Adopting is the inverse of detaching, so the instant goes with the link: a row handed to
        # a memo is not something a sweep of detached rows may still take.
        existing.detached_at = None
        await _consume(part, consumed_out)
        await db.delete(upload)
        await db.flush()
        return existing

    mime = await to_thread.run_sync(_sniff_mime, part, upload.mime)
    now = datetime.now(UTC)
    # Stage in originals/_unfiled/ — the memo (and so the per-memo folder) doesn't exist yet; the
    # master is atomically moved into originals/YYYY/MM/<memo-folder>/ on link_attachments.
    dest_dir = storage.unfiled_dir(_data_root())
    dest = dest_dir / f"{uuid.uuid4().hex}_{Path(upload.filename).name}"
    # Recorded BEFORE the move, not after it: a request cancelled during the await used to let the
    # rename land with the bookkeeping skipped, stranding an original in _unfiled/ that no rollback
    # could find (MEMNG-145). Only a genuinely-moved file is tracked — the dedup return above
    # appends nothing.
    track = storage.TrackedMove(src=str(part))
    if moved_out is not None:
        moved_out.append(track)
    await to_thread.run_sync(_tracked_move_into_sync, track, dest)  # mkdir + os.replace in one hop

    is_video = mime.startswith("video/")
    latitude, longitude, location_source = await _read_location(dest, mime)
    attachment = Attachment(
        uid=secrets.token_urlsafe(16),
        owner_id=owner_id,
        filename=upload.filename,
        mime=mime,
        size=actual,
        sha256=digest,
        # The documented precedence, finally whole: what the FILE said, else what the browser said
        # about its own copy, else when it arrived (MEMNG-262).
        captured_at=upload.capture_instant or upload.client_mtime or now,
        capture_instant=upload.capture_instant,
        capture_civil=upload.capture_civil,
        capture_source=upload.capture_source,
        # WHERE the file says it was taken, read ONCE here, from the final path, and stored on this
        # row alone — never on the upload session (see `_read_location`).
        latitude=latitude,
        longitude=longitude,
        location_source=location_source,
        status=AttachmentStatus.pending if is_video else AttachmentStatus.processing,
        storage_path=str(dest),
        # width/height/duration are filled by the worker fast lane (T2.2 pyvips/ffprobe).
    )
    db.add(attachment)
    await db.flush()
    if locked is not None:
        # A row this transaction just inserted is invisible to every other one, so it carries no
        # lock order (R8) — but it IS a dedup target for the rest of the batch, exactly as it was
        # when the loop re-ran the query and found it. Two uploads of identical bytes in one batch
        # have always produced one attachment; registering it here is what keeps that true.
        locked.candidates.setdefault(digest, attachment)

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
    moved_out: list[storage.TrackedMove] | None = None,
    consumed_out: list[str] | None = None,
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
        await _consume(part, consumed_out)
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
        await _consume(part, consumed_out)
        await db.delete(upload)
        await db.flush()
        return None

    track = storage.TrackedMove(src=str(part))  # recorded before the move — see finalize above
    if moved_out is not None:
        moved_out.append(track)
    await to_thread.run_sync(_tracked_move_into_sync, track, dest)
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
    moved_out: list[storage.TrackedMove] | None = None,
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
        track = storage.TrackedMove(src=str(part))  # recorded before the move — see finalize above
        if moved_out is not None:
            moved_out.append(track)
        path = await storage.place_master(part, dest_dir, upload.filename, track)
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
    orphaned mid-append. Returns the number of upload SESSIONS reaped — the size of the set it
    actually claimed under the row lock, so a session another transaction consumed while this sweep
    queued counts for nobody. Worker-wired in T2.2."""
    now = now or datetime.now(UTC)
    # List the spool directory BEFORE reading which sessions are live. In that order every listed
    # file's session row was already committed when the read below ran, so a session opened while
    # this sweep runs can never be mistaken for a dead one — its spool simply waits a cycle.
    spools = await to_thread.run_sync(_list_spools_sync, _data_root() / "uploads")
    # CLAIMED before a single byte is unlinked. The sweep used to read this set unlocked, unlink
    # every part file, and only then let the DELETEs take the row locks — so it destroyed the only
    # copy of an upload a finalize already held and had not yet moved, and that finalize then died
    # on the missing bytes (MEMNG-279). The claim is the protocol's canonical U acquisition: ONE
    # statement, ascending by `upload_uid`, the same clause every other U site emits (§2.1/R2), and
    # no other lock class is involved. Plain `FOR UPDATE`, deliberately not `SKIP LOCKED`: a sweep
    # that stepped around a held row would stop being a writer that serializes behind the holder,
    # which is the property the census probe beside this one exists to pin.
    #
    # The set is judged AFTER the wait, not before it (R10): under READ COMMITTED a locking select
    # re-evaluates its predicate once the lock is granted, so a row a finalize consumed while this
    # statement queued is simply absent from the result — the same re-evaluation the standalone
    # dedup `FOR UPDATE` already relies on. Nothing extends `expires_at` (it is written once, at
    # `create_upload`), so there is no second value here to re-read by hand.
    rows = (
        (
            await db.execute(
                select(Upload)
                .where(Upload.expires_at < now)
                .order_by(Upload.upload_uid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # Every part file in ONE hop — an hourly sweep of a large backlog would otherwise be that many
    # blocking unlinks in a row — and now INSIDE the window those locks fence. §2.5 permits it: an
    # unlink under DATA_ROOT is bounded local file I/O of the same class as the finalize's own
    # placements, not a third party, not a subprocess and not a client stream. The alternative
    # (unlink after the caller's commit) would need a second commit boundary, which §2.5 rejects.
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
