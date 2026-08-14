"""The photo book: which photos it is made of, and how well each one will actually print.

A book is a QUERY, not a stored object (MEMNG-49; see the "Design" section of
docs/plans/PLAN_2026-07-30_photobook-print-tier.md): selection is dynamic and cheap — a tag, a date
range, one owner — while the rendition it is served in is a fixed ladder of exactly one rung, the
`printready` 4096 px tier Phase A built.
Nothing is re-encoded on the request path; the request only decides what the book contains and
schedules whatever is missing.

The high-value output is not the archive, it is the **effective dpi** per photo: the number that
tells you, before a 1 GB import, which pictures the layout software will put a warning triangle on.
`effective_dpi` below is where that is computed and where the placement model is written down.

Two things this module deliberately does NOT do:

* **It never re-encodes anything itself.** Generation is the worker's (app/services/media.py); this
  enqueues and reports. It reads image HEADERS to learn a finished rendition's real size, which is
  a few KB per file, never a decode.
* **It never auto-retries a terminally-failed photo.** See `build`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from anyio import to_thread
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, JobKind, JobState, MediaJob, Memo, Tag, memo_tags
from app.services import export as export_svc
from app.services import printready
from app.services.frame_share import CURATED_TAG, is_frame_image

# The single profile. It names the rung (printready.MAX_EDGE) and nothing else — the plan measured
# per-image rung snapping at a 6% saving over 238 photos and rejected it, so there is deliberately
# no ladder to choose from. A second profile would be a second rung, i.e. a second tier.
PROFILE = "print-4096"
Profile = Literal["print-4096"]

DEFAULT_TAG = CURATED_TAG  # "curated" — the set the plan's 238-photo measurement was taken over
DEFAULT_PAGE_CM = "21x21"  # the 21x21 cm layflat the rung was sized for
DEFAULT_MAX_PAGES = 1.5  # a 3:2 photo at full page height spans exactly this
DEFAULT_DPI = 300  # the print service's stated minimum; the bar `meets_dpi` reports against

CM_PER_INCH = 2.54
SEQUENCE_WIDTH = 4  # 0001_… — four digits sorts a book of up to 9999 photos correctly as text

Readiness = Literal["ready", "pending", "failed", "unavailable"]
SourceKind = Literal["print-master", "original"]

_PAGE_CM_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)$")
_MAX_PAGE_CM = 300.0  # a 3 m page is already absurd; the bound only exists to reject nonsense
# A zip entry name is a path. A uid carrying a separator or a `..` would place the entry outside
# the archive root when the user extracts it, so uids are reduced to this alphabet on the way in.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class GeometryError(ValueError):
    """A print geometry that cannot describe a page (bad shape, non-positive, absurd)."""


@dataclass(frozen=True, slots=True)
class Geometry:
    """The print size the dpi is computed against — the question, not a property of the photos."""

    page_w_cm: float
    page_h_cm: float
    max_pages: float
    min_dpi: int


def parse_page_cm(value: str) -> tuple[float, float]:
    """`"21x21"` → `(21.0, 21.0)`, the page's width and height in centimetres.

    Raises GeometryError on anything else: the value reaches the dpi formula as a divisor, so a
    silently-defaulted or zero page would report a confidently wrong resolution for every photo in
    the book — worse than a 422.
    """
    match = _PAGE_CM_RE.match(value.strip().lower())
    if match is None:
        raise GeometryError("page_cm must look like '21x21' — width x height in centimetres")
    width, height = float(match.group(1)), float(match.group(2))
    if not (0 < width <= _MAX_PAGE_CM) or not (0 < height <= _MAX_PAGE_CM):
        raise GeometryError(f"page_cm sides must be greater than 0 and at most {_MAX_PAGE_CM:g} cm")
    return width, height


def geometry(page_cm: str, max_pages: float, dpi: int) -> Geometry:
    """Validate the three print-geometry inputs together and freeze them into one value."""
    width, height = parse_page_cm(page_cm)
    if not 0 < max_pages <= 10:
        raise GeometryError("max_pages must be greater than 0 and at most 10")
    if not 0 < dpi <= 2400:
        raise GeometryError("dpi must be greater than 0 and at most 2400")
    return Geometry(page_w_cm=width, page_h_cm=height, max_pages=max_pages, min_dpi=dpi)


def effective_dpi(px_w: int | None, px_h: int | None, geom: Geometry) -> float | None:
    """The resolution this photo really prints at, laid out as large as the book allows.

    THE PLACEMENT MODEL (docs/plans/PLAN_2026-07-30_photobook-print-tier.md, "Design"): a photo is
    placed at FULL PAGE HEIGHT across at most `max_pages` facing pages, aspect preserved. That is a
    "contain" fit into a box `max_pages` pages wide and one page tall::

        box_w_cm     = page_w_cm * max_pages
        box_h_cm     = page_h_cm
        aspect       = px_w / px_h
        printed_w_cm = min(box_w_cm, box_h_cm * aspect)
        dpi          = px_w / (printed_w_cm / 2.54)

    `min` is what makes `max_pages` a CAP rather than a multiplier: a photo wide enough to run past
    it is cut back to the spread width (width-bound), while a narrower one stops at the page height
    (height-bound) and simply prints smaller. The scale is uniform in both axes, so taking the dpi
    on the width is exact rather than an approximation.

    Worked anchor from the plan: a 3:2 photo on the 21x21 cm layflat at max_pages=1.5 is the exact
    boundary case — box_h*aspect = 31.5 = box_w — so it prints 31.5 x 21 cm, the 1.5-page spread
    the plan calls "the largest thing ever printed", and 3720 px across that 31.5 cm is 300 dpi:
    the number the 4096 rung was chosen to clear. A SQUARE photo on the same book is height-bound
    (min(31.5, 21x1.0) = 21 cm), so it fills exactly one page and prints far above 300.

    None when the dimensions are unknown — a source whose width/height was never probed. Reporting
    a guess there would defeat the whole point of the field.
    """
    if not px_w or not px_h or px_w <= 0 or px_h <= 0:
        return None
    printed_w_cm = min(geom.page_w_cm * geom.max_pages, geom.page_h_cm * (px_w / px_h))
    if printed_w_cm <= 0:
        return None
    return px_w * CM_PER_INCH / printed_w_cm


def rung_px(width: int | None, height: int | None) -> tuple[int, int] | None:
    """The size a printready rendition WILL have, projected from its source's size.

    The rung is a long-edge cap applied with `Image.thumbnail`, which never upscales — so a source
    already inside the box keeps its own dimensions and a 6336 px master comes down to 4096. Used
    only for a photo whose rendition does not exist yet; a real measurement replaces it the moment
    one does, which is why every projected number is flagged as such in the manifest (Pillow's
    rounding of the short edge can differ from this by a pixel).
    """
    if not width or not height or width <= 0 or height <= 0:
        return None
    edge = printready.MAX_EDGE
    if max(width, height) <= edge:
        return (width, height)
    if width >= height:
        return (edge, max(1, round(height * edge / width)))
    return (max(1, round(width * edge / height)), edge)


def photobook_filename(sequence: int, memo_date: date, attachment_uid: str) -> str:
    """`0001_2026-01-31_<uid>.jpg` — the name this photo carries inside the archive.

    Layout software imports a folder in filename order, so the zero-padded sequence IS the book's
    page order; the date makes a directory listing readable and the uid makes the name unique and
    traceable back to its row. Always `.jpg`, because the printready tier is always JPEG whatever
    the source was.

    Numbers are assigned across the WHOLE selection, including photos that are not ready yet, so a
    member finishing later drops into its own slot instead of renumbering every page after it — a
    book downloaded twice a day apart must not come back reshuffled.
    """
    safe = _UNSAFE_NAME.sub("_", attachment_uid) or "unknown"
    return f"{sequence:0{SEQUENCE_WIDTH}d}_{memo_date.isoformat()}_{safe}.jpg"


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Selected:
    """One (memo, photo) pair in book order, before anything is known about its rendition."""

    memo_uid: str
    memo_date: date
    attachment: Attachment


async def select_photos(
    db: AsyncSession,
    owner_id: int,
    *,
    tag: str,
    date_from: date | None,
    date_to: date | None,
) -> list[Selected]:
    """Every (tagged memo, image attachment) pair this owner has, in book order.

    Mirrors the shipped curated filter exactly (app/scripts/backfill_frame_album.py: the same join
    and the same `is_frame_image`), which matters because the plan's measured set — 238 photos,
    18.2 GB — was counted with that filter and not with the memo-tag join that gives 220. Two
    consequences of joining over `Attachment` rows are load-bearing: a print master lives in
    `attachment_renditions` and is never an `Attachment` row, so it can never appear as a member in
    its own right; and `is_frame_image` admits image/* while excluding image/svg+xml and every
    video, so nothing enters the book that the print encoder cannot decode.

    Scoped to `owner_id`, NOT to the visibility rules the generic export uses: a book is the
    owner's own material, and another account's public photos have no business in it.

    `date_from`/`date_to` bound the MEMO's created_at and are inclusive whole UTC calendar days on
    both ends — `to=2026-06-30` includes everything posted that day, which is what a person typing
    a date range means.
    """
    stmt = (
        select(Memo.uid, Memo.created_at, Attachment)
        .join(memo_tags, memo_tags.c.memo_id == Memo.id)
        .join(Tag, Tag.id == memo_tags.c.tag_id)
        .join(Attachment, Attachment.memo_id == Memo.id)
        .where(Tag.name == tag, Memo.owner_id == owner_id)
        .order_by(Memo.created_at, Attachment.id)
    )
    if date_from is not None:
        stmt = stmt.where(Memo.created_at >= _day_start(date_from))
    if date_to is not None:
        stmt = stmt.where(Memo.created_at < _day_start(date_to + timedelta(days=1)))
    rows = (await db.execute(stmt)).all()
    return [
        Selected(memo_uid=memo_uid, memo_date=created_at.date(), attachment=attachment)
        for memo_uid, created_at, attachment in rows
        if is_frame_image(attachment.mime)
    ]


async def terminally_failed(db: AsyncSession, attachment_ids: Sequence[int]) -> set[int]:
    """Attachments whose LATEST print job failed for good.

    `JobState.failed` is only ever reached at MEDIA_MAX_ATTEMPTS (app/worker.py) — a transient
    failure goes back to `queued` and retries itself — so membership here means "tried to the
    attempt limit, still broken", never "hit a blip". Latest-job-wins so a later success or a
    running regeneration supersedes an old failure rather than condemning the photo forever.
    """
    ids = list(attachment_ids)
    if not ids:
        return set()
    latest = (
        select(
            MediaJob.attachment_id.label("attachment_id"),
            func.max(MediaJob.id).label("job_id"),
        )
        .where(MediaJob.attachment_id.in_(ids), MediaJob.kind == JobKind.printready)
        .group_by(MediaJob.attachment_id)
        .subquery()
    )
    rows = await db.execute(
        select(MediaJob.attachment_id)
        .join(latest, MediaJob.id == latest.c.job_id)
        .where(MediaJob.state == JobState.failed)
    )
    return set(rows.scalars())


def _dims_sync(targets: list[tuple[int, Path]]) -> dict[int, tuple[int, int]]:
    """Measure each finished rendition in ONE off-loop hop.

    `Image.open` parses the JPEG header and stops there — it does not decode — so this costs a few
    KB of reading per photo rather than the ~50 MB a 4096 px decode would, which is what makes it
    affordable to do for a whole book on every manifest request. A file that will not parse is
    simply left out and its caller falls back to the projection.
    """
    dims: dict[int, tuple[int, int]] = {}
    for attachment_id, path in targets:
        try:
            with Image.open(path) as img:
                dims[attachment_id] = (img.width, img.height)
        except Exception:  # a truncated or half-written rendition must not fail the manifest
            continue
    return dims


@dataclass(frozen=True, slots=True)
class PhotoEntry:
    """One photo's place in the book and its print verdict."""

    sequence: int
    attachment_uid: str
    memo_uid: str
    memo_date: date
    filename: str
    source: SourceKind
    status: Readiness
    width: int | None
    height: int | None
    projected: bool  # True = px predicted from the source, not measured off a real rendition
    effective_dpi: float | None
    meets_dpi: bool | None  # None = unknown dimensions, which is not the same as "too low"
    captured_at: datetime | None
    path: Path | None  # the file to put in the archive; set only when status == "ready"

    def record(self) -> dict[str, object]:
        """The photo as it appears in a manifest — the same shape in the JSON response and in the
        archive's own manifest.json, so the two can never describe a book differently."""
        return {
            "sequence": self.sequence,
            "attachment": self.attachment_uid,
            "memo": self.memo_uid,
            "memo_date": self.memo_date.isoformat(),
            "filename": self.filename,
            "source": self.source,
            "status": self.status,
            "width": self.width,
            "height": self.height,
            "projected": self.projected,
            "effective_dpi": None if self.effective_dpi is None else round(self.effective_dpi, 1),
            "meets_dpi": self.meets_dpi,
        }


@dataclass(frozen=True, slots=True)
class Photobook:
    """A resolved book: the question that was asked and the answer for every photo in it."""

    tag: str
    geometry: Geometry
    entries: tuple[PhotoEntry, ...]

    def totals(self) -> dict[str, int]:
        counts = {"ready": 0, "pending": 0, "failed": 0, "unavailable": 0}
        for entry in self.entries:
            counts[entry.status] += 1
        return {"selected": len(self.entries), **counts}


def _entry(
    sequence: int,
    selected: Selected,
    *,
    status: Readiness,
    geom: Geometry,
    real: tuple[int, int] | None,
) -> PhotoEntry:
    """Assemble one manifest row, resolving the dimensions the dpi is computed from.

    Measured beats projected: once a rendition exists its real pixel size is read off the file, so
    the reported dpi is the one the printer will actually get. Before then the size is projected by
    putting the source through the rung (`rung_px`) — which is the honest prediction, because that
    is literally the operation the worker is queued to perform — and flagged `projected` so nobody
    mistakes a forecast for a measurement.
    """
    attachment = selected.attachment
    if real is not None:
        width, height, projected = real[0], real[1], False
    else:
        px = rung_px(attachment.width, attachment.height)
        width, height = (px[0], px[1]) if px else (None, None)
        projected = px is not None
    dpi = effective_dpi(width, height, geom)
    return PhotoEntry(
        sequence=sequence,
        attachment_uid=attachment.uid,
        memo_uid=selected.memo_uid,
        memo_date=selected.memo_date,
        filename=photobook_filename(sequence, selected.memo_date, attachment.uid),
        source="print-master" if printready.print_rendition(attachment) else "original",
        status=status,
        width=width,
        height=height,
        projected=projected,
        effective_dpi=dpi,
        meets_dpi=None if dpi is None else dpi >= geom.min_dpi,
        captured_at=attachment.captured_at,
        path=printready.printready_path(attachment.uid) if status == "ready" else None,
    )


async def build(
    db: AsyncSession,
    owner_id: int,
    *,
    tag: str,
    date_from: date | None,
    date_to: date | None,
    geom: Geometry,
) -> Photobook:
    """Resolve the book: select it, read what is already on disk, queue what is missing, describe
    all of it.

    ASKING FOR A BOOK IS WHAT SCHEDULES IT. Every member without a current rendition is enqueued
    here, and `printready.enqueue_printready` supplies the idempotence — a current file, an
    already-running job, and an ineligible attachment each queue nothing — so re-requesting is free
    and re-requesting IS how progress is watched.

    A member whose last job failed terminally is deliberately NOT re-queued. `failed` means the
    attempt limit was exhausted on a source that does not decode, and a book is polled repeatedly
    while it builds: an automatic retry would burn a fresh round of 60 MP encodes on a thermally
    coupled shared host every time the manifest was fetched, which is exactly the storm the plan
    budgets against. Such a photo is reported `failed` — the state exists precisely so that a
    person, not a poll loop, decides to retry it.

    Order of operations matters and is not arbitrary: the FILE is probed before the job history is
    consulted, so a photo that has a good rendition on disk reads `ready` even if some later job
    failed against it. Judging by job state alone would hide a file that is sitting right there.
    """
    selected = await select_photos(db, owner_id, tag=tag, date_from=date_from, date_to=date_to)
    if not selected:
        return Photobook(tag=tag, geometry=geom, entries=())

    attachments = [s.attachment for s in selected]
    probes = await printready.probe_renditions(attachments)
    failed = await terminally_failed(db, [a.id for a in attachments])

    status: dict[int, Readiness] = {}
    to_enqueue: list[int] = []
    for attachment in attachments:
        probe = probes[attachment.id]
        if not probe.source_present:
            status[attachment.id] = "unavailable"
        elif probe.current:
            status[attachment.id] = "ready"
        elif attachment.id in failed:
            status[attachment.id] = "failed"
        else:
            to_enqueue.append(attachment.id)

    if to_enqueue:
        # Only the members that actually need work reach the enqueue, so its own stat pass shrinks
        # to nothing on a book that is already built. Its buckets are the authority for what it was
        # given: `fresh` means the file appeared between our probe and its own, and `ineligible`
        # means the source went away in the same window — both are races we take its answer for.
        result = await printready.enqueue_printready(db, to_enqueue)
        for attachment_id in result.fresh:
            status[attachment_id] = "ready"
        for attachment_id in (*result.enqueued, *result.in_flight):
            status[attachment_id] = "pending"
        for attachment_id in result.ineligible:
            status[attachment_id] = "unavailable"
        await db.commit()

    ready_ids = [a.id for a in attachments if status[a.id] == "ready"]
    by_uid = {a.id: a.uid for a in attachments}
    real = (
        await to_thread.run_sync(
            _dims_sync, [(i, printready.printready_path(by_uid[i])) for i in ready_ids]
        )
        if ready_ids
        else {}
    )

    entries = tuple(
        _entry(
            index,
            item,
            status=status[item.attachment.id],
            geom=geom,
            real=real.get(item.attachment.id),
        )
        for index, item in enumerate(selected, start=1)
    )
    return Photobook(tag=tag, geometry=geom, entries=entries)


def manifest(book: Photobook, *, page_cm: str, max_pages: float, dpi: int) -> dict[str, object]:
    """The manifest response: the question asked, the totals, and one record per photo."""
    return {
        "profile": PROFILE,
        "tag": book.tag,
        "page_cm": page_cm,
        "max_pages": max_pages,
        "dpi": dpi,
        "totals": book.totals(),
        "photos": [entry.record() for entry in book.entries],
    }


async def archive_items(book: Photobook) -> AsyncIterator[export_svc.ExportItem]:
    """The book as items for the shared streaming archive (app/services/export.py).

    One item per photo, carrying no text entry — a book is a folder of numbered JPEGs and a `.md`
    beside each one would be imported as junk. A photo that is not ready contributes its manifest
    record with no payload, which the archive writer records as missing: the same semantics the
    generic export gives a rendition the worker has not produced yet, and the only signal the user
    gets that a page is still building.

    Entry mtimes come from the attachment's captured_at, clamped into the zip format's range by the
    writer, so a book extracts in capture order by date as well as by name.

    Async because that is the archive writer's input contract; the book itself is already resolved
    in memory by then, so nothing here touches the database while the response streams.
    """
    for entry in book.entries:
        yield export_svc.ExportItem(
            mtime=entry.captured_at or datetime.combine(entry.memo_date, time.min, tzinfo=UTC),
            manifest=entry.record(),
            media=(
                export_svc.MediaFile(
                    uid=entry.attachment_uid,
                    arcname=entry.filename,
                    path=entry.path,
                    mtime=entry.captured_at,
                ),
            ),
        )
