"""Per-memo originals layout — placement of newly-written masters into a human-browsable tree.

Masters are finalized into a staging bucket (`originals/_unfiled/`) before the memo exists, then
ATOMICALLY MOVED into `originals/YYYY/MM/YYYY-MM-DD_HHMM_<memo-uid>/` under their real filename when
the memo is saved (`link_attachments`). Placement only ever acts on a brand-new `_unfiled/` file —
an existing (committed) master is never moved — so no GC, lock, or migration is needed: see
`docs/plans/PLAN_2026-06-22_memo-folder-layout.md`. New-writes-only is enforced by `is_unfiled`.
"""

import os
from datetime import datetime
from pathlib import Path

from anyio import to_thread

# Cap the human-browsable name so a pathological dumped filename can't blow the share's NAME_MAX.
MAX_NAME_LEN = 200


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


async def place_master(src: Path, dest_dir: Path, name: str) -> Path:
    """Atomically move a staged master `src` into `dest_dir` under a sanitized, collision-free
    `name`. Returns the final path. Off-loop (blocking disk I/O)."""
    return await to_thread.run_sync(_place_master_sync, src, dest_dir, name)


def _place_bytes_sync(data: bytes, dest_dir: Path, name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    reserved = _reserve(dest_dir, sanitize_name(name))  # O_EXCL → never overwrites another file
    try:
        with open(reserved, "wb") as f:
            f.write(data)
    except OSError:  # don't leave a partial/empty reservation behind on a write failure
        reserved.unlink(missing_ok=True)
        raise
    return reserved


async def place_bytes(data: bytes, dest_dir: Path, name: str) -> Path:
    """Write in-memory `data` into `dest_dir` under a sanitized, collision-free `name` — for a
    master that arrives as bytes rather than a staged file (a legacy /print upload). Off-loop."""
    return await to_thread.run_sync(_place_bytes_sync, data, dest_dir, name)
