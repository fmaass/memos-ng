"""Per-memo originals layout — placement of newly-written masters into a human-browsable tree.

Masters are finalized into a staging bucket (`originals/_unfiled/`) before the memo exists, then
ATOMICALLY MOVED into `originals/YYYY/MM/YYYY-MM-DD_HHMM_<memo-uid>/` under their real filename when
the memo is saved (`link_attachments`). Placement only ever acts on a brand-new `_unfiled/` file —
an existing (committed) master is never moved — so no GC, lock, or migration is needed: see
`docs/plans/PLAN_2026-06-22_memo-folder-layout.md`. New-writes-only is enforced by `is_unfiled`.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from threading import Lock

from anyio import to_thread

# Cap the human-browsable name so a pathological dumped filename can't blow the share's NAME_MAX.
MAX_NAME_LEN = 200

# The ` (n)` tail `_reserve` appends on a collision — n starts at 2, so ` (1)` is a literal name.
_BUMPED = re.compile(r"^(?P<stem>.*) \((?:[2-9]|[1-9]\d+)\)$")


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
    cleaned = "".join(c for c in name if c.isprintable() and c not in ("/", "\\"))
    cleaned = cleaned.strip().strip(".").strip()
    # cap by encoded bytes (not code points) with headroom for a ` (NN)` collision suffix
    return _truncate_bytes(cleaned, MAX_NAME_LEN) if cleaned else "file"


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


async def place_master(
    src: Path, dest_dir: Path, name: str, track: TrackedMove | None = None
) -> Path:
    """Atomically move a staged master `src` into `dest_dir` under a sanitized, collision-free
    `name`. Returns the final path. Off-loop (blocking disk I/O).

    `track` is the caller's already-appended rollback record: passing it moves the bookkeeping
    into the worker thread, so a cancelled request cannot lose it (MEMNG-145)."""
    if track is None:
        return await to_thread.run_sync(_place_master_sync, src, dest_dir, name)
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
