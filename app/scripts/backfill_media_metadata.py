"""One-shot backfill: the image metadata (width/height/blurhash) migrated attachments never got.

``migrate_from_memos.import_attachment`` marks an attachment ``ready`` as soon as its derived tiers
already exist on disk and enqueues no ``MediaJob`` for it — the right call, since re-deriving tiers
for the whole library is exactly the mass re-decode the shared Saturn host cannot afford. But dims
and blurhash are only ever written inside ``process_job``, so the migrated half of the library sits
at NULL width/height/blurhash and the feed cannot lay a photo out at its own aspect ratio or show a
placeholder while it loads.

This computes the missing values directly rather than enqueuing jobs, which would regenerate the
tiers for metadata that can be read cheaply:

* **dims** from the ORIGINAL's header, oriented exactly the way ``process_image`` orients it —
  ``ImageOps.exif_transpose`` exchanges the axes for EXIF orientations 5-8, and nothing else;
* **blurhash** from the PERSISTED thumb tier — the very file the feed tiles serve — through the
  worker's own encoder and its default components. For the migrated rows this targets, that tier is
  a legacy file copied in from the old stack's resizer and never touched by this worker; for a new
  upload the worker hashes its in-memory thumb before JPEG compression. Either way a backfilled
  hash is not bit-identical to a worker-produced one, and does not need to be: it is a 4x3 colour
  placeholder for the exact image the tile will load.

Never overwrites: a column is filled only where it is NULL at the instant of the write, decided by
the database rather than by this process's snapshot (see ``fill_statement``). Width and height are
additionally all-or-nothing whatever they were derived from — a row already carrying one of them
keeps its NULL, because a dimension paired with one from another source describes neither image.

Default is a DRY RUN — it prints the per-attachment plan and the summary, and writes nothing. Pass
``--execute`` to write. Run inside the app container, which has the DB and the files::

    docker exec memos-ng-app python -m app.scripts.backfill_media_metadata                  # plan
    docker exec memos-ng-app python -m app.scripts.backfill_media_metadata --limit 1 --execute
    docker exec memos-ng-app python -m app.scripts.backfill_media_metadata --execute
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pillow_heif
import structlog
from anyio import to_thread
from app.config import settings
from app.database import async_session
from app.models import Attachment
from app.services import blurhash_pure
from PIL import Image
from sqlalchemy import Update, and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

pillow_heif.register_heif_opener()  # the same HEIC/HEIF opener the worker registers

logger = structlog.get_logger(__name__)

IMAGE_PREFIX = "image/"

# The raster types Pillow (+pillow_heif) actually decodes in this deployment. ``process_job`` has no
# such predicate — it routes every non-video mime into image processing — and it is deliberately not
# changed here, so the allowlist lives in this script alone. Anything else (image/svg+xml, unknown
# subtypes) is left untouched and counted as skipped.
RASTER_MIMES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/tiff",
        "image/bmp",
    }
)

# EXIF orientations 5-8 exchange the axes; 1-4 keep them. This is precisely what
# ImageOps.exif_transpose does to the size that process_image then records.
_TRANSPOSING_ORIENTATIONS = frozenset({5, 6, 7, 8})
_ORIENTATION_TAG = 0x0112  # ExifTags.Base.Orientation — the tag exif_transpose itself reads


@dataclass(frozen=True)
class Computed:
    """What could be derived for one attachment. ``None`` means "not wanted, or not derivable"."""

    size: tuple[int, int] | None = None
    size_from_tier: bool = False
    blurhash: str | None = None


@dataclass
class BackfillStats:
    candidates: int = 0  # image rows missing at least one of width/height/blurhash
    skipped_unsupported_mime: int = 0  # candidates outside RASTER_MIMES (svg, unknown subtypes)
    processed: int = 0  # eligible rows this run looked at (--limit caps it)
    dims_filled: int = 0
    dims_from_tier: int = 0  # subset of dims_filled: original unreadable, ratio from a derived tier
    blurhash_filled: int = 0
    dims_unresolved: int = 0  # dims still NULL: no readable original and no derived tier
    skipped_inconsistent_dims: int = 0  # exactly one dim stored: the pair is left as it is
    blurhash_unresolved: int = 0  # blurhash still NULL: no thumb tier on disk
    failed: int = 0  # an item raised (per-item; the run continued)

    def format(self, *, execute: bool) -> str:
        mode = "EXECUTE" if execute else "DRY RUN (no writes)"
        return (
            f"[{mode}] candidates={self.candidates} "
            f"unsupported_mime={self.skipped_unsupported_mime} processed={self.processed} | "
            f"dims_filled={self.dims_filled} (from_tier={self.dims_from_tier}) "
            f"blurhash_filled={self.blurhash_filled} | "
            f"dims_unresolved={self.dims_unresolved} "
            f"skipped_inconsistent_dims={self.skipped_inconsistent_dims} "
            f"blurhash_unresolved={self.blurhash_unresolved} failed={self.failed}"
        )


def oriented_size(size: tuple[int, int], orientation: int) -> tuple[int, int]:
    width, height = size
    return (height, width) if orientation in _TRANSPOSING_ORIENTATIONS else (width, height)


def _tier_file(tier: str, uid: str) -> Path:
    return Path(settings.DATA_ROOT) / "assets" / tier / f"{uid}.jpg"


def read_original_size(storage_path: str) -> tuple[int, int]:
    """The dims process_image records for this file.

    The ``load()`` is load-bearing rather than incidental. Some Pillow loaders apply the EXIF
    orientation themselves while decoding and then drop the tag — the TIFF plugin does, so an
    orientation-6 TIFF already reports the rotated size and no longer answers for tag 274 — while
    others (JPEG) leave both alone. Reading the tag off an undecoded image therefore rotates a TIFF
    twice. ``ImageOps.exif_transpose`` avoids that only because it calls ``load()`` first, so this
    follows the same order and then applies the same rule: the transpose methods it picks for
    orientations 5-8 are exactly the ones that exchange the axes. Agreeing with the worker on every
    container format costs a decode, which is what ``--limit`` is there to pace.
    """
    with Image.open(storage_path) as raw:
        raw.load()
        return oriented_size(raw.size, raw.getexif().get(_ORIENTATION_TAG, 1))


def read_tier_size(uid: str) -> tuple[int, int] | None:
    """Fallback for an original that cannot be read: the ratio of a derived tier that does exist.

    For the rows this targets that tier is usually a LEGACY file — the old stack's resizer output,
    copied in by the migration, which is precisely why ``import_attachment`` marked the attachment
    ready. It is not something this pipeline produced, so the ratio it carries reflects whatever
    orientation that resizer applied, not this worker's ``exif_transpose``. That, plus its being
    smaller than the original, makes it a ratio of last resort — counted separately, and never
    paired with a dimension that came from anywhere else. Mobile first: it is the larger tier.
    """
    for tier in ("mobile", "thumbs"):
        path = _tier_file(tier, uid)
        if path.exists():
            with Image.open(path) as img:
                return img.size
    return None


def read_thumb_blurhash(uid: str) -> str | None:
    """Encode from the persisted thumb tier with the worker's encoder and its default components.

    The thumb is whatever the tiles actually serve — a legacy resizer output for the migrated rows
    this targets, the worker's own JPEG for a new upload — so the placeholder always describes the
    exact image it stands in for, which is the only property a blurhash needs to have.
    """
    path = _tier_file("thumbs", uid)
    if not path.exists():
        return None
    with Image.open(path) as img:
        return blurhash_pure.encode(img)


def compute(uid: str, storage_path: str | None, want_size: bool, want_blurhash: bool) -> Computed:
    """All of one attachment's blocking image work, in a single call the caller hands to a thread.

    An unreadable original is not fatal — that is what the tier fallback is for. A corrupt tier
    file is: it propagates so the item is counted failed rather than half-filled.
    """
    size: tuple[int, int] | None = None
    from_tier = False
    if want_size:
        if storage_path:
            try:
                size = read_original_size(storage_path)
            except (OSError, ValueError):
                size = None
        if size is None:
            size = read_tier_size(uid)
            from_tier = size is not None
    return Computed(
        size=size,
        size_from_tier=from_tier,
        blurhash=read_thumb_blurhash(uid) if want_blurhash else None,
    )


def _computed_values(computed: Computed) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if computed.size is not None:
        values["width"], values["height"] = computed.size
    if computed.blurhash is not None:
        values["blurhash"] = computed.blurhash
    return values


def plan_fields(attachment: Attachment, computed: Computed) -> list[str]:
    """Which currently-NULL columns ``computed`` could fill, in write order.

    A plan, not a decision: it reads the snapshot the candidate query loaded, so it says what is
    worth attempting. ``fill_statement`` re-decides each column against the live row. Pure on
    purpose — a dry run must not touch the ORM objects, since anything mutated in the session
    would be flushed by the next query that runs.

    A stored value always wins. Migrated rows carry heterogeneous metadata (some of it
    worker-produced), and a backfilled value is the less authoritative of the two — tier-derived
    dims are smaller than the original's, a re-encoded blurhash differs in its trailing digits.

    Dims are additionally all-or-nothing, from EVERY source: a computed width beside an
    already-stored height invents an aspect ratio matching neither image, which is the one thing
    these fields are read for — a stored ``width=7`` completed with a 1200x900 original's height
    would lay every tile of that photo out as 7:900. The pair is written together or not at all.
    Blurhash carries no such pairing and stays per-column.
    """
    fields: list[str] = []
    if computed.size is not None and attachment.width is None and attachment.height is None:
        fields.extend(("width", "height"))
    if computed.blurhash is not None and attachment.blurhash is None:
        fields.append("blurhash")
    return fields


def fill_statement(attachment_id: int, computed: Computed, fields: list[str]) -> Update | None:
    """An UPDATE that re-decides every column against the row's CURRENT contents.

    ``plan_fields`` reads a snapshot taken when the candidate query ran, which can be many minutes
    old by the time the write happens. A worker finishing a freshly uploaded attachment's MediaJob
    in that window writes real dims and a blurhash from the original — and a check-then-write
    backfill, still remembering NULL, would overwrite them. ``COALESCE(column, :value)`` moves the
    decision inside the row's own write lock, so the concurrent writer always wins and this run
    fills only what is still NULL at the instant it writes.

    Dims keep their all-or-nothing pairing in SQL too (the CASE): a width from this run beside a
    height a concurrent writer landed in the meantime would invent a ratio matching neither, so the
    pair is written only while BOTH columns are still NULL. A per-column COALESCE cannot express
    that — it would let exactly one of the two through.
    """
    new = _computed_values(computed)
    both_dims_null = and_(Attachment.width.is_(None), Attachment.height.is_(None))
    values: dict[str, Any] = {}
    for name in fields:
        column = getattr(Attachment, name)
        values[name] = (
            case((both_dims_null, new[name]), else_=column)
            if name in ("width", "height")
            else func.coalesce(column, new[name])
        )
    if not values:
        return None
    return update(Attachment).where(Attachment.id == attachment_id).values(**values)


async def write_fill(
    db: AsyncSession, attachment: Attachment, computed: Computed, fields: list[str]
) -> list[str]:
    """Run the conditional UPDATE and report the columns this run actually put there.

    Read back from the row rather than assumed: for a column a concurrent writer reached first the
    statement is a no-op, and the summary should not claim it.
    """
    statement = fill_statement(attachment.id, computed, fields)
    if statement is None:
        return []
    await db.execute(statement)
    await db.refresh(attachment, ["width", "height", "blurhash"])
    new = _computed_values(computed)
    return [name for name in fields if getattr(attachment, name) == new[name]]


async def load_candidates(db: AsyncSession) -> list[Attachment]:
    """Every image attachment missing at least one of width/height/blurhash, oldest first.

    The ``image/%`` predicate keeps videos out entirely: their dims and duration come from ffprobe,
    and touching a video is the one thing this must never do. The mime allowlist is applied
    afterwards by ``partition`` so the excluded types can be counted rather than silently dropped.
    """
    rows = await db.execute(
        select(Attachment)
        .where(
            Attachment.mime.startswith(IMAGE_PREFIX),
            or_(
                Attachment.width.is_(None),
                Attachment.height.is_(None),
                Attachment.blurhash.is_(None),
            ),
        )
        .order_by(Attachment.id)
    )
    return list(rows.scalars().all())


def partition(candidates: list[Attachment]) -> tuple[list[Attachment], list[Attachment]]:
    """Split candidates into (eligible, unsupported-mime) against the raster allowlist."""
    eligible = [att for att in candidates if att.mime in RASTER_MIMES]
    unsupported = [att for att in candidates if att.mime not in RASTER_MIMES]
    return eligible, unsupported


async def run_backfill(
    db: AsyncSession, *, execute: bool, limit: int | None = None
) -> BackfillStats:
    candidates = await load_candidates(db)
    eligible, unsupported = partition(candidates)
    stats = BackfillStats(candidates=len(candidates), skipped_unsupported_mime=len(unsupported))
    for att in unsupported:
        logger.info("backfill.unsupported_mime", attachment_id=att.id, uid=att.uid, mime=att.mime)

    for att in eligible if limit is None else eligible[: max(limit, 0)]:
        stats.processed += 1
        # Dims are computed only when BOTH are NULL: a row already carrying one of them cannot be
        # completed (see plan_fields), so decoding its original would be work with no destination.
        stored = (att.width is not None, att.height is not None)
        want_size = not any(stored)
        inconsistent_dims = any(stored) and not all(stored)
        want_blurhash = att.blurhash is None
        try:
            computed = await to_thread.run_sync(
                compute, att.uid, att.storage_path, want_size, want_blurhash
            )
            fields = plan_fields(att, computed)
            if execute:
                fields = await write_fill(db, att, computed, fields)
        except Exception as exc:  # per-item isolation: one unreadable file never aborts the run
            stats.failed += 1
            logger.warning(
                "backfill.item_failed", attachment_id=att.id, uid=att.uid, error=str(exc)[:200]
            )
            continue

        if inconsistent_dims:
            stats.skipped_inconsistent_dims += 1
        elif want_size:
            if "width" in fields or "height" in fields:
                stats.dims_filled += 1
                stats.dims_from_tier += int(computed.size_from_tier)
            else:
                stats.dims_unresolved += 1
        if want_blurhash:
            if "blurhash" in fields:
                stats.blurhash_filled += 1
            else:
                stats.blurhash_unresolved += 1

        logger.info(
            "backfill.filled" if execute else "backfill.plan",
            attachment_id=att.id,
            uid=att.uid,
            mime=att.mime,
            fields=",".join(fields) or "-",
            dims_source="tier" if computed.size_from_tier else "original",
        )
    return stats


async def _amain(*, execute: bool, limit: int | None) -> BackfillStats:
    async with async_session() as db:
        stats = await run_backfill(db, execute=execute, limit=limit)
        if execute:
            # One commit for the whole run: every write is an independent conditional UPDATE, so
            # committing once keeps the run atomic without the connection ever sitting idle (each
            # statement exercises it) long enough to be reaped server-side.
            await db.commit()
    return stats


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Backfill missing image width/height/blurhash (idempotent, NULL fields only)."
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
        help="process at most N eligible attachments (victim probe before the full run)",
    )
    args = ap.parse_args()
    stats = asyncio.run(_amain(execute=args.execute, limit=args.limit))
    print(stats.format(execute=args.execute))


if __name__ == "__main__":  # pragma: no cover
    main()
