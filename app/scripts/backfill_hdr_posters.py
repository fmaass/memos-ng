"""One-shot backfill: re-derive the poster of every HDR video with the tone-mapping chain.

A video's poster was extracted without tone-mapping until MEMNG-226, so an HLG or PQ source — what
an iPhone records by default — got a JPEG holding HDR code words that every viewer then read as
sRGB: lifted blacks, flat contrast, washed-out colour, next to a clip that plays correctly because
its container kept the colour tags. ``media.poster_video_filter`` fixes that at derivation, but
only for posters derived AFTER it ships: the worker never overwrites a poster that already exists
(``media.process_video_poster``), and reconcile only re-enqueues videos whose poster is MISSING.
The files already on disk are this script's job.

Selection is every ``ready`` video attachment with an original on disk, oldest first; the decision
is the ORIGINAL's own ``color_transfer`` (``media.is_hdr`` — HLG or PQ, nothing else), so an SDR
library is scanned and left alone. On production today that is 3 HDR clips out of 117 videos.

**The poster it replaces may be a client's own frame.** ``MemoCreate.video_posters`` lets a client
send the frame it wants on the tile, and nothing on disk or in the database distinguishes such a
file from a worker-derived one. An HDR clip's client frame is captured through the SPA's canvas and
is therefore already display-referred — it does not have the fault this script exists to fix, and
replacing it swaps a chosen frame for a correct one at a different timestamp. That is accepted here
because the alternative is leaving broken posters unfixable, the population is 3 files, and the
original is kept: everything replaced is copied to ``DATA_ROOT/backfill/hdr-posters/`` first, which
is outside the served and enumerated ``assets/`` tree, so nothing takes it for an orphan.

Safety sequence, per attachment, in this order and never rearranged:

1. **back up** ``<uid>.poster.jpg`` and any format-negotiated variant beside it
   (``asset_paths.variant_paths``) into ``DATA_ROOT/backfill/hdr-posters/``. An existing backup is
   NEVER overwritten — it is the original, and a retry must not replace it with the output of a
   half-finished earlier attempt;
2. **extract** the new frame with ``media.poster_ffmpeg_cmd(..., colour=<probe>)`` to the tier's own
   tmp path;
3. **guard**: re-read the attachment row under the same ``FOR UPDATE`` lock the worker takes before
   it publishes (``media.attachment_still_exists``) and re-check that it is still ``ready``. An
   attachment deleted or reprocessed while the extraction ran is counted ``vanished``, its tmp file
   swept, and nothing is published — the one thing that would leave a poster no row names;
4. **publish**: ``os.replace`` onto the poster path, ``media.stamp_mtime`` (the poster carries the
   attachment's capture date like every other derived tier, so a correct replacement ends with the
   SAME mtime it had), then unlink the stale webp variants so the serving path falls back to the
   new JPEG;
5. **blurhash**: recompute it from the new file with the worker's own encoder and UPDATE the row
   (``WHERE uid = … AND status = 'ready'``, so the write re-decides against the live row), then
   commit — otherwise the tile's placeholder fades into a different picture;
6. **mark done**: write ``<uid>.done`` beside the backup, holding the new poster's sha256 and the
   time it landed. Only after the commit, so the marker means "finished", never "attempted".

Completion is that marker, not the backup: a run that dies between the backup and the commit leaves
a backup with no marker, and the retry redoes the work while keeping the original backup untouched.
A uid whose marker parses is counted ``already_done`` and skipped — which is what makes the runbook
(dry run, then one victim with ``--uid``, then the rest) safe to finish with a bare ``--execute``.

A video with NO poster on disk is counted ``missing_poster`` and skipped. Deriving one here would
be doing the reconcile lane's work in a script whose whole purpose is replacing existing files
(``app/services/reconcile.py`` re-enqueues a fast-lane job for exactly that case).

Default is a DRY RUN: it prints one line per video — uid, ``color_transfer``, HDR yes/no, the
variants it found and what it would do — plus the summary, and writes nothing at all. Pass
``--execute`` to write. Exits non-zero if any item failed or vanished. Run inside the app
container, which has the database and the files::

    docker exec memos-ng-app python -m app.scripts.backfill_hdr_posters                       # plan
    docker exec memos-ng-app python -m app.scripts.backfill_hdr_posters --uid <uid> --execute
    docker exec memos-ng-app python -m app.scripts.backfill_hdr_posters --execute
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from anyio import to_thread
from app.api_types import to_rfc3339
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus
from app.services import asset_paths, blurhash_pure, media, poster
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

VIDEO_PREFIX = "video/"
# Outside `assets/`: the served tiers are enumerated from that tree (app/services/asset_paths.py)
# and a stray JPEG in it would read as an orphan to anything auditing it.
BACKUP_SUBDIR = ("backfill", "hdr-posters")
DONE_SUFFIX = ".done"
# The same 60 s the worker allows its own poster extraction (media.process_video_poster).
EXTRACT_TIMEOUT_S = 60


class BackupRefused(RuntimeError):
    """The backup directory cannot be created or written, so no replacement may start.

    Fail closed and before the first item, not per item: the backup IS the rollback, and a run
    that cannot keep what it overwrites has nothing to roll back to.
    """


@dataclass(frozen=True)
class Candidate:
    """One video row, read off the database ONCE, as plain values.

    Not an ORM instance on purpose. A per-item rollback — how the publish guard's row lock is
    released after a failure — EXPIRES every instance in the session, so the next attribute read
    would be lazy IO, issued from the worker thread this loop hands its file work to: a
    MissingGreenlet instead of a poster. Nothing here needs a live row. The one thing that MUST be
    re-read against the database, whether the attachment is still there and still `ready`, is
    re-read explicitly under a lock by `still_publishable`.
    """

    id: int
    uid: str
    storage_path: str
    captured_at: datetime | None


@dataclass
class HdrBackfillStats:
    scanned: int = 0  # video attachments looked at (--limit caps it)
    hdr: int = 0  # of those, HLG/PQ originals — the population this script exists for
    replaced: int = 0  # posters re-derived, published and committed
    already_done: int = 0  # HDR, but a completion marker from an earlier run is already there
    missing_poster: int = 0  # HDR with no poster file: reconcile's business, not this script's
    skipped_sdr: int = 0  # scanned but not HDR — left completely alone
    vanished: int = 0  # deleted or no longer `ready` between the extraction and the publish
    failed: int = 0  # an item raised (per-item; the run continued)

    def format(self, *, execute: bool) -> str:
        mode = "EXECUTE" if execute else "DRY RUN (no writes)"
        return (
            f"[{mode}] scanned={self.scanned} hdr={self.hdr} replaced={self.replaced} "
            f"already_done={self.already_done} missing_poster={self.missing_poster} "
            f"skipped_sdr={self.skipped_sdr} vanished={self.vanished} failed={self.failed}"
        )


def exit_code(stats: HdrBackfillStats) -> int:
    """Non-zero when anything did not land: an unreadable original, a failed extraction, an
    attachment that went away mid-flight. A caller running this from a runbook has to be able to
    tell "3 posters replaced" from "2 replaced and one I never looked at again"."""
    return 1 if (stats.failed or stats.vanished) else 0


def backup_dir() -> Path:
    """Reads DATA_ROOT at call time, like every other path helper here, so tests can move it."""
    return Path(settings.DATA_ROOT).joinpath(*BACKUP_SUBDIR)


def backup_path(uid: str) -> Path:
    """Where this uid's ORIGINAL poster is kept. Same basename as the tier file, so a restore is
    a copy back and the pairing needs no index."""
    return backup_dir() / f"{uid}{poster.SUFFIX}"


def done_path(uid: str) -> Path:
    return backup_dir() / f"{uid}{DONE_SUFFIX}"


def ensure_backup_dir() -> Path:
    """Create the backup directory AND prove a file can be written into it.

    `mkdir(exist_ok=True)` no-ops on a directory that is already there, so it says nothing about
    permission — and the deployed layout is a bind-mounted share whose ACL masks the unix mode,
    which is exactly the case a mkdir hides. The probe file is the only honest answer, and it is
    taken once, before the loop, so a permission problem stops the run instead of surfacing after
    the first poster has already been overwritten.
    """
    directory = backup_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".writable"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise BackupRefused(f"cannot write the backup directory {directory}: {exc}") from exc
    return directory


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sweep(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # best-effort: an unreapable tmp file must not mask the real outcome
        pass


def existing_poster_files(uid: str) -> tuple[Path | None, list[Path]]:
    """The poster this uid has on disk and the format-negotiated variants beside it.

    Variants come from `asset_paths.variant_paths`, the same list a delete unlinks — a second
    spelling of `.webp` here is how a replaced JPEG keeps being served as its old picture.
    """
    poster_file = poster.poster_path(uid)
    variants = [path for path in asset_paths.variant_paths(poster_file) if path.exists()]
    return (poster_file if poster_file.exists() else None, variants)


def back_up(uid: str, poster_file: Path, variants: list[Path]) -> None:
    """Copy the poster and its variants aside, never overwriting a backup that is already there.

    An existing backup is the ORIGINAL — from an earlier run that got this far and then failed —
    and overwriting it with the output of that half-finished attempt would destroy the only copy
    of the file this whole script promises to keep.
    """
    for source, destination in [
        (poster_file, backup_path(uid)),
        *((variant, backup_dir() / variant.name) for variant in variants),
    ]:
        if destination.exists():
            continue
        # The copy lands under a private name and is LINKED into place: a copy interrupted
        # halfway must never be readable at the final name, because the next run would keep the
        # truncated file as "the original" for ever. link publishes whole-or-not-at-all, and it
        # refuses to clobber — a complete backup another run just published wins over this copy.
        tmp = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
        shutil.copy2(source, tmp)
        try:
            os.link(tmp, destination)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)


def extract(uid: str, src: str, duration: float | None, colour: dict[str, Any]) -> Path:
    """Derive the tone-mapped frame to the tier's own tmp path (a sibling of the poster, so the
    later rename cannot cross the assets mount and hit EXDEV — MEMNG-143)."""
    poster_file = poster.poster_path(uid)
    poster_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = poster.poster_tmp_path(uid)
    subprocess.run(
        media.poster_ffmpeg_cmd(src, str(tmp), duration, colour=colour),
        capture_output=True,
        timeout=EXTRACT_TIMEOUT_S,
        check=True,
    )
    return tmp


def publish(tmp: Path, item: Candidate, variants: list[Path]) -> str:
    """Rename the new frame into place, restamp it, drop the stale variants, hash the result.

    The mtime restamp is the worker's own (`media.stamp_mtime`): the poster carries the capture
    date like every other derived tier, so a CORRECT replacement ends with the same mtime it had.
    """
    poster_file = poster.poster_path(item.uid)
    os.replace(tmp, poster_file)
    media.stamp_mtime(poster_file, item.captured_at)
    for variant in variants:
        _sweep(variant)  # else every webp-capable client keeps being served the old picture
    with Image.open(poster_file) as image:
        return blurhash_pure.encode(image)


def mark_done(uid: str) -> Path:
    """Write the completion marker — AFTER the blurhash commit, so it can only mean 'finished'.

    It carries the new poster's sha256 and when it landed, which is what makes a later audit
    ("is this file the one the backfill wrote, or has something re-derived it since?") answerable
    without a database.
    """
    marker = done_path(uid)
    digest = _sha256(poster.poster_path(uid))
    # Written aside and renamed in: a marker interrupted mid-write would otherwise sit at the
    # final name, and `is_done` would believe it. The rename is the only way it appears at all.
    tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    tmp.write_text(f"sha256={digest}\nreplaced_at={to_rfc3339(datetime.now(UTC))}\n")
    os.replace(tmp, marker)
    return marker


_DONE_LINE = re.compile(r"^sha256=[0-9a-f]{64}$")


def is_done(uid: str) -> bool:
    """Only a marker that PARSES counts: it must open and its first line must be the sha256.

    Existence alone is not completion — a truncated or empty file at the marker's name (a crash,
    a full disk) would otherwise turn a half-finished uid into one no retry ever repairs.
    """
    try:
        first = done_path(uid).read_text().splitlines()[0]
    except (OSError, IndexError):
        return False
    return bool(_DONE_LINE.match(first))


async def still_publishable(db: AsyncSession, attachment_id: int) -> bool:
    """The worker's publish checkpoint, minus the media_jobs half a script has no job for.

    `media.attachment_still_exists` takes the same `SELECT … FOR UPDATE` on the attachment row
    that `guard_publish` takes before a job writes its result, and the lock is held until this
    item commits. So a `delete_attachment` racing this replacement either wins outright (the row
    is gone, this answers False and nothing is published) or waits the few milliseconds until the
    new poster and its blurhash are durable and then sweeps both — never the third order, where a
    poster lands after the delete swept the directory and outlives the row that named it.

    Only one row is locked here, so the lock ORDERING that `guard_publish` has to observe
    (jobs before attachment) has nothing to interleave with; there is no second lock to deadlock
    against. The status is re-read under that same lock because "exists" is not enough: an
    attachment that stopped being `ready` is being torn down or reprocessed, and a poster written
    into that is a file nobody asked for.
    """
    if not await media.attachment_still_exists(db, attachment_id):
        return False
    status = (
        await db.execute(select(Attachment.status).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    return status is AttachmentStatus.ready


async def load_candidates(db: AsyncSession, uid: str | None = None) -> list[Candidate]:
    """Every ready video with an original on disk, oldest first (`--uid` narrows it to one).

    Deliberately NOT filtered by anything HDR: the transfer characteristic lives in the file, not
    in the database, so the probe is the only way to know and the scan is what pays for it.
    """
    statement = (
        select(Attachment.id, Attachment.uid, Attachment.storage_path, Attachment.captured_at)
        .where(
            Attachment.mime.startswith(VIDEO_PREFIX),
            Attachment.status == AttachmentStatus.ready,
            Attachment.storage_path.is_not(None),
        )
        .order_by(Attachment.id)
    )
    if uid is not None:
        statement = statement.where(Attachment.uid == uid)
    rows = await db.execute(statement)
    return [
        Candidate(id=row.id, uid=row.uid, storage_path=path, captured_at=row.captured_at)
        for row in rows
        if (path := row.storage_path) is not None  # the WHERE already says so; this types it
    ]


async def run_backfill(
    db: AsyncSession, *, execute: bool, limit: int | None = None, uid: str | None = None
) -> HdrBackfillStats:
    """Scan, classify, and — with `execute` — replace. Raises BackupRefused before touching
    anything if the backups cannot be kept."""
    candidates = await load_candidates(db, uid=uid)
    stats = HdrBackfillStats()
    if execute:
        # Off the loop like every other filesystem call here — it creates a directory and writes a
        # probe file on a bind-mounted share.
        await to_thread.run_sync(ensure_backup_dir)  # fail closed, before the first replacement

    for item in candidates if limit is None else candidates[: max(limit, 0)]:
        stats.scanned += 1
        try:
            probed = await to_thread.run_sync(media._ffprobe, item.storage_path)
        except Exception as exc:  # per-item isolation: one unreadable original is not the run
            stats.failed += 1
            logger.warning(
                "hdr_backfill.probe_failed",
                attachment_id=item.id,
                uid=item.uid,
                error=str(exc)[:200],
            )
            continue

        transfer = probed.get("color_transfer")
        if not media.is_hdr(transfer):
            stats.skipped_sdr += 1
            logger.info(
                "hdr_backfill.item", uid=item.uid, color_transfer=transfer, hdr=False, action="skip"
            )
            continue
        stats.hdr += 1

        if await to_thread.run_sync(is_done, item.uid):
            stats.already_done += 1
            logger.info(
                "hdr_backfill.item",
                uid=item.uid,
                color_transfer=transfer,
                hdr=True,
                action="skip (already done)",
            )
            continue

        poster_file, variants = await to_thread.run_sync(existing_poster_files, item.uid)
        if poster_file is None:
            stats.missing_poster += 1
            logger.info(
                "hdr_backfill.item",
                uid=item.uid,
                color_transfer=transfer,
                hdr=True,
                action="skip (no poster; reconcile derives a missing one)",
            )
            continue

        if not execute:
            logger.info(
                "hdr_backfill.item",
                uid=item.uid,
                color_transfer=transfer,
                hdr=True,
                action="replace",
                variants=[path.name for path in variants],
            )
            continue

        tmp = poster.poster_tmp_path(item.uid)
        try:
            await to_thread.run_sync(back_up, item.uid, poster_file, variants)
            tmp = await to_thread.run_sync(
                extract, item.uid, item.storage_path, probed.get("duration"), probed
            )
            if not await still_publishable(db, item.id):
                await db.rollback()
                await to_thread.run_sync(_sweep, tmp)
                stats.vanished += 1
                logger.warning("hdr_backfill.vanished", attachment_id=item.id, uid=item.uid)
                continue
            blurhash = await to_thread.run_sync(publish, tmp, item, variants)
            await db.execute(
                update(Attachment)
                .where(Attachment.uid == item.uid, Attachment.status == AttachmentStatus.ready)
                .values(blurhash=blurhash)
            )
            # Per item, not once at the end: the file replacement is not part of any transaction,
            # so a run that dies later must leave every poster it already published with the
            # blurhash that describes it — and the marker below may only be written after this.
            await db.commit()
            await to_thread.run_sync(mark_done, item.uid)
        except Exception as exc:
            await db.rollback()
            await to_thread.run_sync(_sweep, tmp)
            stats.failed += 1
            logger.warning(
                "hdr_backfill.item_failed",
                attachment_id=item.id,
                uid=item.uid,
                error=str(exc)[:200],
            )
            continue

        stats.replaced += 1
        logger.info(
            "hdr_backfill.replaced",
            uid=item.uid,
            color_transfer=transfer,
            variants_removed=[path.name for path in variants],
        )
    return stats


async def _amain(*, execute: bool, limit: int | None, uid: str | None) -> HdrBackfillStats:
    async with async_session() as db:
        return await run_backfill(db, execute=execute, limit=limit, uid=uid)


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(
        description=(
            "Re-derive the poster of every HDR (HLG/PQ) video with the tone-mapping chain. "
            "Backs up what it replaces; dry run unless --execute."
        )
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="actually write; the default is a dry run that only prints the plan",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="look at at most N video attachments (pacing; --uid is the one-victim probe)",
    )
    ap.add_argument(
        "--uid",
        default=None,
        help="restrict the run to one attachment uid",
    )
    args = ap.parse_args()
    stats = asyncio.run(_amain(execute=args.execute, limit=args.limit, uid=args.uid))
    print(stats.format(execute=args.execute))
    raise SystemExit(exit_code(stats))


if __name__ == "__main__":  # pragma: no cover
    main()
