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


def sanitize_name(filename: str) -> str:
    """Reduce a client-supplied filename to a safe in-folder name: directory components dropped,
    path separators + non-printable/control chars removed, leading/trailing dots and spaces trimmed
    (no hidden/Windows-trailing-dot files), length-capped. Empty result falls back to 'file'."""
    name = Path(filename).name  # drop posix dir components
    cleaned = "".join(c for c in name if c.isprintable() and c not in ("/", "\\"))
    cleaned = cleaned.strip().strip(".").strip()
    return cleaned[:MAX_NAME_LEN] or "file"


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
    os.replace(src, reserved)  # atomic move onto the reserved (empty) name
    return reserved


async def place_master(src: Path, dest_dir: Path, name: str) -> Path:
    """Atomically move a staged master `src` into `dest_dir` under a sanitized, collision-free
    `name`. Returns the final path. Off-loop (blocking disk I/O)."""
    return await to_thread.run_sync(_place_master_sync, src, dest_dir, name)
