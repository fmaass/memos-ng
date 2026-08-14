"""The print-ready rendition tier: what it is, where it lives, and how work for it is enqueued.

A photo book is laid out from 40-60 MP masters that no layout program can hold — 18.2 GB of PNG
across the curated set, ~180 MB decoded per image, and single files past the 50 MB upload cap of
the book service. The `printready` tier is the answer: ONE rung at 4096 px long edge, always JPEG,
colour-managed to sRGB at 300 dpi (MEMNG-49 / docs/plans/PLAN_2026-07-30_photobook-print-tier.md).
The geometry is not a round number pulled from the air: a 21x21 cm layflat page at 300 dpi is
2480 px, a 3:2 photo across 1.5 pages is 3720 px, plus bleed → 4096 (which is also exactly 2x the
`mobile` tier).

This module is the half of the feature the APP process may import: the tier's identity, its path,
which file it derives from, and the enqueue path. The generation itself (Pillow/ImageCms) lives in
app/services/media.py, which only the worker imports — nothing here pulls in an image library or
performs I/O outside an off-loop hop.

Generation is ON DEMAND and never part of an upload: re-encoding a 60 MP master costs seconds of
CPU on a thermally-bound shared host, so it happens when a book actually needs the photo, on the
slow lane, one at a time — never 238 times over because someone opened the library.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, AttachmentRendition, JobKind, JobLane, JobState, MediaJob

# The tier key, deliberately NOT `print`: `print` already means "the separately-uploaded print
# master, streamed verbatim" and satellite clients depend on that meaning (app/routers/assets.py).
TIER = "printready"
SUBDIR = "printready"  # assets/<SUBDIR>/<uid>.jpg, the layout thumb/mobile use
IMAGE_PREFIX = "image/"

# The rung: 4096 px on the long edge, never upscaled. It lives here rather than beside the encoder
# in app/services/media.py because it is tier IDENTITY, not an encoder setting — the photo-book
# manifest runs in the APP process (which must not import an image library to learn how big a
# rendition will be) and projects a not-yet-generated size from it. media.py re-exports it as
# PRINTREADY_MAX, so there is exactly one definition.
MAX_EDGE = 4096


def printready_path(uid: str) -> Path:
    """Where this attachment's print rendition lives. Resolves from the uid alone, like every
    other derived tier — read at call time so DATA_ROOT stays overridable in tests."""
    return Path(settings.DATA_ROOT) / "assets" / SUBDIR / f"{uid}.jpg"


def is_print_variant(variant_key: str) -> bool:
    """A rendition holding a print master: the bare `print` or a labelled `print:<slug>` (live data
    carries `print:glossy`). Matched exactly, not by prefix — a future `printable`/`print-proof`
    key must not be mistaken for one."""
    return variant_key == "print" or variant_key.startswith("print:")


def print_rendition(attachment: Attachment) -> AttachmentRendition | None:
    """The print master to derive from, or None. `renditions` is selectin-loaded, so this reads
    already-fetched rows and issues no query.

    Lowest id wins when a photo carries several (glossy AND matte): the earliest-attached master.
    Choosing between them per book is a selection question, and selection is Phase B's — this only
    has to be DETERMINISTIC, so that re-running an export cannot silently swap the source.
    """
    prints = [r for r in attachment.renditions if is_print_variant(r.variant_key)]
    return min(prints, key=lambda r: r.id) if prints else None


def source_path(attachment: Attachment) -> Path | None:
    """The file the print rendition is made FROM: the print master when the photo has one (it is
    the user's own edited crop for print), else the original.

    Never a screen tier — thumb/mobile are 800/2048 px and upscaling one to 4096 would print a
    blurred image at full page size. None means there is nothing to derive from at all.
    """
    rendition = print_rendition(attachment)
    if rendition is not None:
        return Path(rendition.storage_path)
    return Path(attachment.storage_path) if attachment.storage_path else None


def _effective_mtime(st: os.stat_result) -> float:
    """The moment the file itself last changed — st_ctime, deliberately not st_mtime.

    mtime is untrustworthy in BOTH directions here: media._stamp_mtime rewrites a derived tier's
    mtime to the photo's capture time, and capture time descends from the client-supplied upload
    last_modified, which nothing bounds — a future-dated stamp would make a rendition read as
    "fresh" forever, silently shipping stale print bytes after its source is replaced.

    st_ctime is the inode's change time: every write, replace, and the utime stamp itself bump it
    to NOW, and userland cannot set it. Comparing ctime on both sides therefore reads "rendition
    generated after the source last changed", errs toward regenerating (a source restored from
    backup or re-copied gets a fresh ctime and its rendition is rebuilt — the safe direction),
    and is immune to whatever value the mtime was stamped with.
    """
    return st.st_ctime


@dataclass(frozen=True, slots=True)
class SourceProbe:
    source_present: bool  # the file to derive from exists (nothing to do if it doesn't)
    current: bool  # a non-empty rendition exists and is at least as new as that source


def _probe_sync(pairs: list[tuple[int, Path, Path]]) -> dict[int, SourceProbe]:
    """Stat one batch of (attachment id, source, rendition) triples in ONE off-loop hop.

    Batched on purpose: the data volume is a bind-mounted share where every stat is a network
    round-trip, and a photo-book export asks about a few hundred attachments at once.
    """
    probes: dict[int, SourceProbe] = {}
    for attachment_id, source, dest in pairs:
        try:
            source_stat = source.stat()
        except OSError:  # the master is gone — a job could only fail
            probes[attachment_id] = SourceProbe(source_present=False, current=False)
            continue
        try:
            dest_stat = dest.stat()
        except OSError:  # not generated yet
            probes[attachment_id] = SourceProbe(source_present=True, current=False)
            continue
        probes[attachment_id] = SourceProbe(
            source_present=True,
            # Size guards the one case mtimes cannot see: a 0-byte file left by a crash between
            # create and write would otherwise count as a finished rendition forever.
            current=dest_stat.st_size > 0
            and _effective_mtime(dest_stat) >= _effective_mtime(source_stat),
        )
    return probes


async def probe_renditions(attachments: Iterable[Attachment]) -> dict[int, SourceProbe]:
    """Each attachment's print-rendition freshness, in ONE batched off-loop stat pass.

    Exactly the two facts `enqueue_printready` decides on, exposed as a READ-ONLY query. The
    photo-book manifest needs them for members it deliberately does not enqueue (one whose last
    job failed terminally is reported, not retried), and deriving the freshness rule a second time
    there is how the two would drift — so both callers go through `_probe_sync`.

    A non-image, or an attachment with no source file recorded at all, comes back
    `source_present=False`: there is nothing to derive from, so there is nothing to wait for.
    """
    absent: dict[int, SourceProbe] = {}
    pairs: list[tuple[int, Path, Path]] = []
    for attachment in attachments:
        source = source_path(attachment) if attachment.mime.startswith(IMAGE_PREFIX) else None
        if source is None:
            absent[attachment.id] = SourceProbe(source_present=False, current=False)
            continue
        pairs.append((attachment.id, source, printready_path(attachment.uid)))
    probes = await to_thread.run_sync(_probe_sync, pairs) if pairs else {}
    return absent | probes


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Every requested attachment lands in exactly one bucket, each in request order.

    The caller (the photo-book export, Phase B) needs the split, not just a count: `fresh` is ready
    to serve NOW, `enqueued` + `in_flight` are what a progress view has to wait for, and
    `ineligible` is what will never arrive and must not be waited on.
    """

    enqueued: tuple[int, ...]  # a job was created by this call
    fresh: tuple[int, ...]  # a current rendition is already on disk
    in_flight: tuple[int, ...]  # a printready job for it is already queued/claimed/processing
    ineligible: tuple[int, ...]  # no such attachment, not an image, or no source file on disk


async def enqueue_printready(db: AsyncSession, attachment_ids: Iterable[int]) -> EnqueueResult:
    """Queue print renditions for existing attachments, skipping every attachment that needs none.

    Idempotent by three independent rules, because the expensive thing (a 60 MP re-encode on a
    thermally-bound host) must not happen twice for one photo:

    * a rendition that is already current is left alone (see `_effective_mtime`);
    * an attachment whose printready job is still queued/claimed/processing gets no second job —
      `done`/`failed` are terminal, so a finished job never blocks a legitimate regeneration and a
      failed one can be retried by asking again;
    * non-images, unknown ids, and attachments whose source file is missing are refused outright
      rather than queued to fail (a failing job would otherwise burn MEDIA_MAX_ATTEMPTS).

    Adds the rows and FLUSHES; the caller owns the transaction and commits. Two callers racing on
    the same attachment can both pass the in-flight check and create a duplicate job — harmless
    (the second regenerates the same bytes) and not worth a partial unique index today; the
    photo-book export serialises its own requests.
    """
    ids = list(dict.fromkeys(attachment_ids))  # de-duplicated, request order preserved
    if not ids:
        return EnqueueResult((), (), (), ())
    attachments = {
        a.id: a
        for a in (await db.execute(select(Attachment).where(Attachment.id.in_(ids)))).scalars()
    }
    active = set(
        (
            await db.execute(
                select(MediaJob.attachment_id).where(
                    MediaJob.attachment_id.in_(ids),
                    MediaJob.kind == JobKind.printready,
                    MediaJob.state.notin_((JobState.done, JobState.failed)),
                )
            )
        ).scalars()
    )

    disposition: dict[int, str] = {}
    pairs: list[tuple[int, Path, Path]] = []
    for attachment_id in ids:
        attachment = attachments.get(attachment_id)
        if attachment is None or not attachment.mime.startswith(IMAGE_PREFIX):
            disposition[attachment_id] = "ineligible"
            continue
        source = source_path(attachment)
        if source is None:
            disposition[attachment_id] = "ineligible"
            continue
        pairs.append((attachment_id, source, printready_path(attachment.uid)))

    probes = await to_thread.run_sync(_probe_sync, pairs) if pairs else {}
    for attachment_id, _source, _dest in pairs:
        probe = probes[attachment_id]
        if not probe.source_present:
            disposition[attachment_id] = "ineligible"
        elif attachment_id in active:
            # Checked BEFORE freshness: a running job is about to rewrite the file, so what is on
            # disk right now says nothing about what this attachment still needs.
            disposition[attachment_id] = "in_flight"
        elif probe.current:
            disposition[attachment_id] = "fresh"
        else:
            db.add(
                MediaJob(
                    attachment_id=attachment_id,
                    lane=JobLane.slow,
                    kind=JobKind.printready,
                    state=JobState.queued,
                )
            )
            disposition[attachment_id] = "enqueued"
    if any(d == "enqueued" for d in disposition.values()):
        await db.flush()
    return EnqueueResult(
        enqueued=tuple(i for i in ids if disposition[i] == "enqueued"),
        fresh=tuple(i for i in ids if disposition[i] == "fresh"),
        in_flight=tuple(i for i in ids if disposition[i] == "in_flight"),
        ineligible=tuple(i for i in ids if disposition[i] == "ineligible"),
    )
