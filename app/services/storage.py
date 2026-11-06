"""Per-memo originals layout — placement of newly-written masters into a human-browsable tree.

Masters are finalized into a staging bucket (`originals/_unfiled/`) before the memo exists, then
ATOMICALLY MOVED into `originals/YYYY/MM/YYYY-MM-DD_HHMM_<memo-uid>/` under their real filename when
the memo is saved (`link_attachments`). Placement only ever acts on a brand-new `_unfiled/` file —
an existing (committed) master is never moved — so no GC, lock, or migration is needed: see
`docs/plans/PLAN_2026-06-22_memo-folder-layout.md`. New-writes-only is enforced by `is_unfiled`.
"""

import errno
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Protocol

import structlog
from anyio import to_thread

logger = structlog.get_logger(__name__)

# Cap the human-browsable name so a pathological dumped filename can't blow the share's NAME_MAX.
MAX_NAME_LEN = 200

# The ` (n)` tail `_reserve` appends on a collision — n starts at 2, so ` (1)` is a literal name.
_BUMPED = re.compile(r"^(?P<stem>.*) \((?:[2-9]|[1-9]\d+)\)$")


def free_bytes(root: Path) -> int:
    """Free bytes on the volume that holds `root` — the one probe of the disk floor, shared by the
    guest door (`app.ingest`) and the household upload session (MEMNG-961). Blocking: call it
    off the loop.

    `root` is made before it is measured. A volume too full to make it (ENOSPC, MEMNG-1175) has
    answered the probe's question itself: there is no room, so 0 — and each caller refuses with
    its own 507 (the session's `InsufficientSpace` reason, the guest door's floor) instead of the
    OSError escaping as a 500. The errno decides, not the message; any other failure (a
    permission, a read-only mount, an I/O fault) is a real error and propagates."""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            return 0
        raise
    return shutil.disk_usage(root).free


class MoveRecorder(Protocol):
    """A durable record of a rename, committed BEFORE the rename happens. Only trip filing has one
    (`trip_ingest._MoveJournal`, MEMNG-1037 #2); every other caller's move carries none and behaves
    exactly as it always did."""

    async def record(self, src: str, dest: str, kind: str) -> int: ...

    async def forget(self, row_id: int) -> None: ...


# What a journaled placement writes into its reservation: the row's own id, which is what lets a
# later reconcile tell THIS row's placeholder from a real file or another writer's reservation.
RESERVATION_PREFIX = b"memos-ng-ingest-move:"


def reservation_token(row_id: int) -> bytes:
    return RESERVATION_PREFIX + str(row_id).encode()


# Where a journaled placement writes its token BEFORE the reservation exists (MEMNG-1188). The
# reservation used to be an O_EXCL create followed by a separate token write, so a SIGKILL between
# the two left an EMPTY file at the journaled name — and an empty file proves nothing: an owner
# print upload can reserve that same free name and rename real media onto it, and the batch lock
# does not serialize with the print route, so deleting by size took another writer's original
# (tried at 2e2dda4, reproduced by the gate). So the token goes into this private temp first and is
# hard-linked onto the name: the name never holds anything but a complete token. The temp's NAME is
# half the ownership proof — it carries the row id, and no other writer can make a dot-name in a
# memo folder (`sanitize_name` trims leading dots to a fixed point). The other half is its bytes: a
# prefix of that row's token, since a sanitizer before the fix let a dot-name through and a file
# from then may sit at the name. Reconcile removes it on both, never by size.
RESERVATION_TEMP_PREFIX = ".memos-ng-reserve-"


# A link refused because THIS VOLUME will not make one, as opposed to an I/O or space error that
# would hit the same disk again on any fallback. EEXIST is not here: that is the lost race.
_LINK_UNSUPPORTED = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS}
)


def reservation_temp(dest: Path, row_id: int) -> Path:
    """The row's own temp, beside the dest it reserves: a hard link cannot cross a mount."""
    return dest.parent / f"{RESERVATION_TEMP_PREFIX}{row_id}"


class MoveAbandoned(Exception):
    """The write that asked for this move has already unwound, so the move must not happen."""


@dataclass
class TrackedMove:
    """One original on its way to a new path, and the arbitration that keeps a cancelled request
    from leaving it somewhere nothing recorded.

    Every one of these moves is a rename inside the data volume, made BEFORE the memo transaction
    commits — so a write that does not land has to walk each of them back. The bookkeeping used to
    be appended by the async caller AFTER awaiting the move, which is exactly where a cancellation
    lands: anyio raises out of the await while the worker thread carries on renaming, so the file
    moved and the append never ran, leaving an original in `originals/_unfiled/` or a memo folder
    that no rollback could find (MEMNG-145).

    So the record is appended to the write's move list BEFORE the worker starts, and it is the
    WORKER — which nothing can interrupt — that performs the move and fills in `dest`, both under
    this record's lock. `restore()` takes the same lock, so the two can only happen in one order or
    the other, never interleaved: a restore that gets there first marks the move abandoned and
    `perform` refuses (the file never moves); one that arrives after finds a destination to walk
    back. There is no ordering in which an original ends up somewhere unrecorded.

    `restore()` is synchronous for the same reason `PosterCopy.discard()` is: it runs while a
    handler is unwinding, where an `await` is not guaranteed to resume at all and the cleanup would
    simply be skipped.
    """

    src: str
    dest: str | None = None
    journal: MoveRecorder | None = None
    _lock: Lock = dataclass_field(default_factory=Lock, repr=False)
    _abandoned: bool = False

    def perform(self, move: Callable[[Path], Path]) -> Path:
        """Move the bytes and record where they went, as one indivisible step.

        Runs in a worker thread. `move` gets `src` and returns where it put the file — the two
        shapes in this codebase are a rename onto a known path and a placement that reserves its
        own collision-free name, and neither knows its destination early enough for the caller to
        record it up front. That is why the record is filled in here rather than by the caller.
        """
        with self._lock:
            if self._abandoned:
                raise MoveAbandoned(self.src)
            dest = move(Path(self.src))
            self.dest = str(dest)
            return dest

    def restore(self) -> str | None:
        """Put this original back where it came from; returns the path it could NOT restore.

        Never raises: it runs while an exception is already unwinding, where an error of its own
        would replace the outcome that got us here. A file it cannot move back is left in place and
        reported — one in the wrong directory can still be recovered by hand, an unlinked one
        cannot.
        """
        with self._lock:
            self._abandoned = True
            dest = self.dest
            if dest is None:
                return None  # never moved, or abandoned before the move began
            try:
                destination = Path(dest)
                if not destination.exists():
                    return None  # already restored, or the move failed after reserving the name
                source = Path(self.src)
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            except OSError:
                return dest
            self.dest = None
            return None


@dataclass
class TrackedCopy(TrackedMove):
    """A file COPIED into place before the write commits, walked back by removing the copy.

    The same arbitration as a move and kept in the same list, so every caller that already undoes
    its moves on a failed write undoes these too without knowing the difference: `perform` copies
    under the lock and records `dest`; `restore` takes the lock, marks the copy abandoned and
    unlinks what it placed. `src` is never touched — it belongs to another attachment (MEMNG-959:
    the derived tiers a staging hit reuses from its twin).
    """

    def restore(self) -> str | None:
        with self._lock:
            self._abandoned = True
            dest = self.dest
            if dest is None:
                return None
            try:
                Path(dest).unlink(missing_ok=True)
            except OSError:
                return dest
            self.dest = None
            return None


def _truncate_bytes(name: str, limit: int) -> str:
    """Cap `name` to `limit` ENCODED bytes (filesystem component limits are byte-based, ~255),
    never splitting a multibyte char. Keeps the extension when possible so the name stays usable."""
    if len(name.encode()) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    ext_b = (dot + ext).encode()
    keep = limit - len(ext_b) if dot and len(ext_b) < limit else limit
    body = (stem if dot else name).encode()[:keep]
    # back off a partial trailing multibyte sequence
    while body and (body[-1] & 0xC0) == 0x80:
        body = body[:-1]
    out = body.decode(errors="ignore") + (dot + ext if dot and keep != limit else "")
    return out or name.encode()[:limit].decode(errors="ignore")


def sanitize_name(filename: str) -> str:
    """Reduce a client-supplied filename to a safe in-folder name: directory components dropped,
    path separators + non-printable/control chars removed, leading/trailing dots and spaces trimmed
    (no hidden/Windows trailing-dot files), byte-capped. Empty result falls back to 'file'."""
    name = Path(filename).name  # drop posix dir components
    cleaned = _trim_dots_and_spaces(
        "".join(c for c in name if c.isprintable() and c not in ("/", "\\"))
    )
    # cap by encoded bytes (not code points) with headroom for a ` (NN)` collision suffix — and trim
    # again after it: a stem whose first char cannot fit leaves only `.ext`
    cleaned = _trim_dots_and_spaces(_truncate_bytes(cleaned, MAX_NAME_LEN)) if cleaned else ""
    return cleaned or "file"


def _trim_dots_and_spaces(name: str) -> str:
    """Strip dots and whitespace from both ends until neither is left there. One pass is not enough
    (MEMNG-1188 review): `. .memos-ng-reserve-7` loses its dot, then its space, and the next dot is
    exposed — a client name in the reservation-temp namespace (`RESERVATION_TEMP_PREFIX`)."""
    while True:
        trimmed = name.strip().strip(".").strip()
        if trimmed == name:
            return name
        name = trimmed


def memo_folder_name(created_at: datetime, uid: str) -> str:
    """`YYYY-MM-DD_HHMM_<uid>` — full timestamp keeps day-level chronological sort; uid
    disambiguates two memos in the same minute. Named once at first placement; never renamed."""
    return f"{created_at:%Y-%m-%d_%H%M}_{uid}"


def memo_dir(data_root: Path, created_at: datetime, uid: str) -> Path:
    """`originals/YYYY/MM/<memo-folder>` — year/month parents reuse the existing originals tree."""
    return (
        data_root
        / "originals"
        / f"{created_at:%Y}"
        / f"{created_at:%m}"
        / memo_folder_name(created_at, uid)
    )


def unfiled_dir(data_root: Path) -> Path:
    """Staging bucket for a master finalized before its memo exists (or never linked)."""
    return data_root / "originals" / "_unfiled"


def is_unfiled(path: str | Path, data_root: Path) -> bool:
    """The placement guard: True only for a master still in the staging bucket. A master already
    in a memo folder or the legacy flat layout returns False and is therefore never moved."""
    return Path(path).parent == unfiled_dir(data_root)


def _reserve(dest_dir: Path, name: str) -> Path:
    """Reserve a unique name in `dest_dir` with O_CREAT|O_EXCL — atomic under concurrency, never
    overwrites an existing file. On collision, bump to `<stem> (2)<suffix>`, ` (3)`, …"""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = dest_dir / name
    n = 2
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate = dest_dir / f"{stem} ({n}){suffix}"
            n += 1


def _place_master_sync(src: Path, dest_dir: Path, name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    reserved = _reserve(dest_dir, sanitize_name(name))
    try:
        os.replace(src, reserved)  # atomic move onto the reserved (empty) name
    except OSError:
        # don't leak the empty reservation — a stale 0-byte file would make the next placement of
        # the same name mis-number to ` (2)` and clutter the folder
        reserved.unlink(missing_ok=True)
        raise
    return reserved


def _tracked_place_master_sync(track: TrackedMove, dest_dir: Path, name: str) -> Path:
    """`_place_master_sync` under the move's own arbitration — see `TrackedMove.perform`."""
    return track.perform(lambda src: _place_master_sync(src, dest_dir, name))


def _free_candidate(dest_dir: Path, name: str, attempt: int) -> tuple[Path, int]:
    """The first name free right now from `attempt` on, the numbering `_reserve` uses. Creates
    nothing, so a crash before the journal commit leaves nothing behind."""
    stem, suffix = Path(name).stem, Path(name).suffix
    while True:
        candidate = dest_dir / (name if attempt == 1 else f"{stem} ({attempt}){suffix}")
        if not candidate.exists():
            return candidate, attempt
        attempt += 1


def _journaled_place_sync(track: TrackedMove, candidate: Path, row_id: int) -> Path:
    """Reserve exactly the journaled name, marked as that row's own, then rename onto it. A lost
    race raises FileExistsError with nothing moved; the caller re-journals under another name.

    The reservation is `os.link(temp, candidate)`: an exclusive create that already holds the
    token, refusing a taken name with FileExistsError exactly as O_EXCL does (see
    `RESERVATION_TEMP_PREFIX`). A kill before the link leaves only the temp; one after it leaves the
    token at the name, which `_is_reservation` already proves, and maybe the temp. Any OSError
    from the link other than a taken name is the placement's own error, and the filing unwinds
    as it does for a failed rename — except a refusal that says this volume makes no hard links
    (`_LINK_UNSUPPORTED`). Whether the originals share links was never proven live, and a trip
    ingest stalled on every placement is worse than the empty-file window this closes, so there
    the reservation falls back to the O_CREAT|O_EXCL create and the token write it used before,
    logged loudly each time so a deploy onto such a volume is seen (MEMNG-1188)."""

    def move(src: Path) -> Path:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        temp = reservation_temp(candidate, row_id)
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            try:
                _write_all(fd, reservation_token(row_id))
            finally:
                os.close(fd)
            try:
                os.link(temp, candidate)
            except FileExistsError:
                raise
            except OSError as exc:
                if exc.errno not in _LINK_UNSUPPORTED:
                    raise
                logger.error(
                    "storage.reservation_link_unsupported",
                    errno=errno.errorcode.get(exc.errno or 0, str(exc.errno)),
                    dest=str(candidate),
                )
                _reserve_with_token_sync(candidate, reservation_token(row_id))
        finally:
            temp.unlink(missing_ok=True)
        _before_journaled_rename()
        try:
            os.replace(src, candidate)
        except OSError:
            candidate.unlink(missing_ok=True)
            raise
        return candidate

    return track.perform(move)


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of `data`. `os.write` may take fewer than asked without raising, and a token
    cut short is one `_is_reservation` rejects — linked onto the name, a kill before the rename
    would leave exactly the flagged leftover MEMNG-1188 removes. A write that takes nothing is an
    error, so the caller unwinds instead of reserving an incomplete token."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "a reservation token write made no progress")
        view = view[written:]


def _reserve_with_token_sync(candidate: Path, token: bytes) -> None:
    """The pre-1188 reservation, for a volume without hard links: an exclusive create, then the
    token. A kill between the two leaves an empty file that reconcile flags and never deletes. A
    write that fails in-process takes the name back: this call just created it, so it is ours."""
    fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        try:
            _write_all(fd, token)
        finally:
            os.close(fd)
    except OSError:
        candidate.unlink(missing_ok=True)
        raise


def _before_journaled_rename() -> None:
    """A test seam: the reservation exists, the journal row is committed, the rename has not run."""


async def _journaled_place(
    track: TrackedMove, journal: MoveRecorder, src: Path, dest_dir: Path, name: str
) -> Path:
    clean = sanitize_name(name)
    attempt = 1
    while True:
        candidate, attempt = await to_thread.run_sync(_free_candidate, dest_dir, clean, attempt)
        row_id = await journal.record(str(src), str(candidate), "master")
        try:
            return await to_thread.run_sync(_journaled_place_sync, track, candidate, row_id)
        except FileExistsError:
            await journal.forget(row_id)
            attempt += 1


async def place_master(
    src: Path, dest_dir: Path, name: str, track: TrackedMove | None = None
) -> Path:
    """Atomically move a staged master `src` into `dest_dir` under a sanitized, collision-free
    `name`. Returns the final path. Off-loop (blocking disk I/O).

    `track` is the caller's already-appended rollback record: passing it moves the bookkeeping
    into the worker thread, so a cancelled request cannot lose it (MEMNG-145)."""
    if track is None:
        return await to_thread.run_sync(_place_master_sync, src, dest_dir, name)
    if track.journal is not None:
        return await _journaled_place(track, track.journal, src, dest_dir, name)
    return await to_thread.run_sync(_tracked_place_master_sync, track, dest_dir, name)


def is_placement_of(existing: str | Path, dest_dir: Path, name: str) -> bool:
    """True when `existing` is the file an earlier placement of client filename `name` into
    `dest_dir` produced — its sanitized name, or the ` (n)` form `_reserve` bumped it to.

    Re-uploading the SAME file (a corrected print master) must land ON that file rather than
    reserve yet another numbered sibling, which would orphan the previous one. The bumped form
    counts because a first placement is routinely bumped already: a print export usually carries
    the screen master's filename, and the screen is sitting in the same memo folder.
    """
    path = Path(existing)
    if path.parent != dest_dir:
        return False
    desired = Path(sanitize_name(name))
    if path.name == desired.name:
        return True
    if path.suffix != desired.suffix:
        return False
    bumped = _BUMPED.match(path.stem)
    return bumped is not None and bumped.group("stem") == desired.stem


def _replace_master_sync(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest


async def replace_master(src: Path, dest: Path) -> Path:
    """Move a staged master `src` ONTO the existing `dest`, replacing it. Off-loop (blocking I/O).

    No torn file: `src` is already the caller's private staging copy (the temp half of a
    temp-then-replace), and `os.replace` is rename(2) — the swap is atomic, and a reader that
    opened the old file keeps reading that complete inode until it closes. Only for a path the
    caller OWNS (this attachment's own print master); `place_master` is the one that reserves.
    """
    return await to_thread.run_sync(_replace_master_sync, src, dest)
