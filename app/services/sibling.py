"""Borrow a date and a location from the RAW original img-manager indexes (MEMNG-796).

Lightroom's PNG exports carry no EXIF, so such a photo groups under its upload day and never reaches
the Places map, while the RAW it was exported from — indexed by img-manager — still knows both.
The export keeps the RAW's file stem, and this module asks img-manager about that stem.

The rules that make borrowing safe:

* **A borrowed value never overwrites one the file provided.** The fill is one UPDATE whose CASE /
  COALESCE decide against the row's CURRENT contents under its row lock — never a Python snapshot
  (the `backfill_media_metadata.fill_statement` discipline). A capture date is filled only when the
  file said nothing at all: `capture_civil` is a file-provided date too (EXIF without an offset).
* **Only an unambiguous sibling is believed.** Exactly one pick, or — with no pick at all — exactly
  one candidate. Camera counters wrap every 10,000 frames, so a stem shared by several RAWs is
  recorded as `ambiguous` and left undated: a date the app cannot prove is worth less than no date.
  The one exception is a `consensus` (MEMNG-797): the common ambiguous case is one RAW held in
  both img-manager tiers (121 of 254 on 2026-10-13), so when every candidate carries the same
  capture second and the identical GPS pair, whichever one is borrowed the value is the same.
* **A stem is not an identity** (MEMNG-843). iPhone `IMG_####` and Sony `_AR5####` counters repeat
  across bodies and years, so a candidate is first judged against the attachment's own timeline
  (`plausible`) and dropped before the election when it could not be this file's RAW: shot more
  than `CAPTURE_SKEW` after the export is known to have existed, or more than `SIBLING_WINDOW`
  before the memo it was posted on. The second signal the ticket considered — file size or pixel
  dimensions — is not in img-manager's answer, and a RAW's would not match its export's anyway.
* **Could-not-ask is not an answer.** Only a 200 is an outcome; a 422 (the stems themselves were
  refused) is `none`; everything else — transport error, timeout, 5xx, 401/403, 404 — raises
  `SiblingUnavailable`, which the worker's failure path retries, so an infrastructure state is
  never recorded as a durable absence.
* **One member's library.** Only attachments of `SIBLING_OWNER_IDS` are ever looked up.
* **No value in a log line.** Events carry outcome names, counts, status codes and exception class
  names — never a date, a coordinate, a stem, a filename or the token.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
import structlog
from sqlalchemy import DateTime, Double, and_, case, cast, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, MediaJob, Memo
from app.services import media

logger = structlog.get_logger()

Outcome = Literal["unique", "consensus", "ambiguous", "none"]
OUTCOME_UNIQUE: Outcome = "unique"
OUTCOME_CONSENSUS: Outcome = "consensus"
OUTCOME_AMBIGUOUS: Outcome = "ambiguous"
OUTCOME_NONE: Outcome = "none"

# `capture_source` / `location_source` of a value this module filled.
SOURCE_SIBLING = "sibling"

LOOKUP_PATH = "/api/lookup/stems"
# Lightroom's export suffix (`_ARA8207 - screen.png`) starts at the first " - ".
EXPORT_SUFFIX_SEPARATOR = " - "
# Lightroom's own rename of an edited or duplicated master: `_AR5123-Edit`, `_AR5123-2`.
LIGHTROOM_SUFFIX = re.compile(r"-(Edit|\d+)$")

# A RAW is shot before anything is exported from it, so a candidate later than the export's own
# file time is another frame. The day of slack is the clock disagreement between a camera that
# recorded no offset (img-manager resolved it in a configured zone) and the device that stamped
# the export — the same day `capture.MAX_HEADER_SKEW` allows a movie header.
CAPTURE_SKEW = timedelta(days=1)
# How long before its memo a borrowed capture may lie. A counter repeats every 10,000 frames, so
# the further back a candidate is, the likelier it is another pass of the counter; the wrong
# borrowings found in production lay between 129 days and 15 years before the file they dated
# (MEMNG-843). Ninety days covers the workflow this
# lookup serves — export a shoot, post it within the season — and a refusal costs only a date
# the file never had: the photo groups under its memo day, exactly as before MEMNG-796. A memo
# DATED back to an old shoot moves the anchor with it, so posting an old RAW stays possible.
SIBLING_WINDOW = timedelta(days=90)


class SiblingUnavailable(Exception):
    """img-manager could not be asked — retried by the worker, never recorded as an outcome.

    The message is what the worker stores in `media_jobs.error`, so it names the class and a
    status or an exception type, and never a stem, a URL or the token.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"SiblingUnavailable: {reason}")


@dataclass(frozen=True)
class Candidate:
    """One non-missing img-manager image whose stem matched — only what an election needs."""

    image_id: int
    datetime_original: datetime | None
    gps_latitude: float | None
    gps_longitude: float | None
    is_pick: bool


@dataclass(frozen=True)
class StemResult:
    """img-manager's answer for one stem. `truncated` means it capped the list: candidates exist
    past the ones returned, so the list alone cannot prove a match unique."""

    candidates: list[Candidate]
    truncated: bool = False


@dataclass(frozen=True)
class Election:
    outcome: Outcome
    chosen: Candidate | None = None


def derive_stems(filename: str | None) -> list[str]:
    """The stems to ask about, most specific first: the export's stem, then — when it differs —
    the stem without Lightroom's `-Edit` / `-<digits>` rename."""
    if not filename:
        return []
    dot = filename.rfind(".")
    name = filename[:dot] if dot >= 0 else filename
    stem = name.split(EXPORT_SUFFIX_SEPARATOR, 1)[0].strip()
    if not stem:
        return []
    stems = [stem]
    stripped = LIGHTROOM_SUFFIX.sub("", stem)
    if stripped and stripped != stem:
        stems.append(stripped)
    return stems


def elect(candidates: Sequence[Candidate], *, truncated: bool = False) -> Election:
    """One pick wins; several candidates that all agree are a consensus; with no pick, one
    candidate wins; anything else is ambiguous.

    A truncated list is ambiguous whatever it holds: img-manager orders picks first, but a client
    that leaned on that ordering would turn a server-side sort into a correctness rule — a second
    pick past the cap would be invisible here."""
    if truncated:
        return Election(OUTCOME_AMBIGUOUS)
    picks = [c for c in candidates if c.is_pick]
    if len(picks) == 1:
        return Election(OUTCOME_UNIQUE, picks[0])
    if len(candidates) >= 2 and _all_agree(candidates):
        return Election(OUTCOME_CONSENSUS, picks[0] if picks else candidates[0])
    if picks:
        return Election(OUTCOME_AMBIGUOUS)
    if len(candidates) == 1:
        return Election(OUTCOME_UNIQUE, candidates[0])
    if candidates:
        return Election(OUTCOME_AMBIGUOUS)
    return Election(OUTCOME_NONE)


def plausible(taken: datetime | None, *, existed_by: datetime, posted_at: datetime) -> bool:
    """Whether a candidate shot at `taken` can be the RAW of a file that existed by `existed_by`
    and was posted on a memo of `posted_at`. An undated candidate cannot be judged by time and is
    left to the election, which never borrows a date from it."""
    if taken is None:
        return True
    return posted_at - SIBLING_WINDOW <= taken <= existed_by + CAPTURE_SKEW


def time_anchors(
    captured_at: datetime | None, created_at: datetime, memo_created_at: datetime | None
) -> tuple[datetime, datetime]:
    """`(existed_by, posted_at)` out of the attachment's own columns.

    `captured_at` is the file's capture read, else the client's file time, else the upload instant
    (`uploads.finalize`), and the upload row's `created_at` is when the bytes arrived; the file
    existed by both, so the earlier is the tighter ceiling. A borrowed date is never written to
    either, so re-judging a borrowed row reads the same anchors the lookup did. A standalone
    upload has no memo yet and is anchored on its upload instant."""
    existed_by = created_at if captured_at is None else min(captured_at, created_at)
    return existed_by, memo_created_at or created_at


def _all_agree(candidates: Sequence[Candidate]) -> bool:
    """Every candidate has a capture date, all on the same second, and the identical GPS pair —
    `(None, None)` included. Exact float equality: two copies of one RAW carry the same EXIF."""
    first = candidates[0]
    if first.datetime_original is None:
        return False
    second = first.datetime_original.replace(microsecond=0)
    place = (first.gps_latitude, first.gps_longitude)
    for c in candidates:
        if c.datetime_original is None or c.datetime_original.replace(microsecond=0) != second:
            return False
        if (c.gps_latitude, c.gps_longitude) != place:
            return False
    return True


def _parse_instant(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError("datetime_original")
    value = datetime.fromisoformat(raw)
    # img-manager stores a UTC instant; a value with no offset would need a zone this side cannot
    # prove, so it is no date at all rather than a guessed one.
    return value if value.tzinfo is not None else None


def _parse_coordinate(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    # Both-or-neither and in range, as the attachments CHECKs require: half a coordinate is not a
    # location, and a value out of range is a parse that went wrong, not a place on Earth.
    if lat is None or lon is None:
        return None, None
    lat_f, lon_f = float(lat), float(lon)
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None, None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None, None
    return lat_f, lon_f


def _parse_candidate(raw: dict[str, Any]) -> Candidate:
    image_id = raw["image_id"]
    if not isinstance(image_id, int) or isinstance(image_id, bool):
        raise TypeError("image_id")
    lat, lon = _parse_coordinate(raw.get("gps_latitude"), raw.get("gps_longitude"))
    return Candidate(
        image_id=image_id,
        datetime_original=_parse_instant(raw.get("datetime_original")),
        gps_latitude=lat,
        gps_longitude=lon,
        is_pick=bool(raw.get("is_pick", False)),
    )


def _parse_result(raw: Any) -> StemResult:
    # Shape-checked rather than iterated: a mapping or a string where the list belongs would
    # otherwise read as no candidates at all and be recorded as a durable `none`.
    if not isinstance(raw, dict):
        raise TypeError("result")
    candidates, truncated = raw["candidates"], raw.get("truncated", False)
    if not isinstance(candidates, list) or not isinstance(truncated, bool):
        raise TypeError("result")
    return StemResult([_parse_candidate(c) for c in candidates], truncated)


async def lookup(
    stems: Sequence[str], *, transport: httpx.AsyncBaseTransport | None = None
) -> list[StemResult]:
    """Ask img-manager about every stem variant in ONE request; one result per stem, in request
    order. `transport` is the seam tests stand img-manager in at."""
    if not stems:
        return []
    url = settings.IMGMANAGER_URL.rstrip("/") + LOOKUP_PATH
    try:
        async with httpx.AsyncClient(
            timeout=settings.IMGMANAGER_TIMEOUT_S, transport=transport
        ) as client:
            response = await client.post(
                url,
                json={"stems": list(stems), "picks_only": False},
                headers={"Authorization": f"Bearer {settings.IMGMANAGER_TOKEN}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("sibling.unavailable", error=type(exc).__name__)
        raise SiblingUnavailable(type(exc).__name__) from None
    if response.status_code == 422:
        return [StemResult([]) for _ in stems]
    if response.status_code != 200:
        logger.warning("sibling.unavailable", status=response.status_code)
        raise SiblingUnavailable(f"img-manager answered {response.status_code}")
    try:
        results = response.json()["results"]
        if not isinstance(results, list) or len(results) != len(stems):
            raise ValueError("results")
        return [_parse_result(result) for result in results]
    except (ValueError, KeyError, TypeError) as exc:
        # A 200 this side cannot read is a version mismatch, i.e. an infrastructure state — never
        # an absence.
        logger.warning("sibling.unavailable", error=type(exc).__name__)
        raise SiblingUnavailable(f"unreadable answer ({type(exc).__name__})") from None


def _fillable(row: Any) -> bool:
    nothing_said = row.capture_instant is None and row.capture_civil is None
    return nothing_said or row.latitude is None


async def process_sibling_job(db: AsyncSession, job: MediaJob) -> None:
    """Look the attachment's stem up and write the result. The caller commits.

    Raises `SiblingUnavailable` when the feature is off or img-manager could not be asked (the
    worker re-queues it), and `media.AttachmentGone` when the attachment or this job vanished
    before the write (the worker discards the job)."""
    if not settings.sibling_enabled:
        # Never `done` with no outcome: such a job would fall outside every backfill population
        # and the enrichment would be lost until somebody noticed.
        raise SiblingUnavailable("disabled")
    row = (
        await db.execute(
            select(
                Attachment.owner_id,
                Attachment.mime,
                Attachment.filename,
                Attachment.capture_instant,
                Attachment.capture_civil,
                Attachment.latitude,
                Attachment.captured_at,
                Attachment.created_at,
                Memo.created_at.label("memo_created_at"),
            )
            .outerjoin(Memo, Memo.id == Attachment.memo_id)
            .where(Attachment.id == job.attachment_id)
        )
    ).one_or_none()
    # NO transaction is held across the HTTP call below: the read ends here.
    await db.commit()
    if row is None:
        raise media.AttachmentGone(f"attachment {job.attachment_id} no longer exists")

    stems = derive_stems(row.filename)
    asked = (
        row.owner_id in settings.sibling_owner_ids
        and row.mime.startswith("image/")
        and _fillable(row)
        and bool(stems)
    )
    election = Election(OUTCOME_NONE)
    candidates = refused = 0
    if asked:
        existed_by, posted_at = time_anchors(row.captured_at, row.created_at, row.memo_created_at)
        for found in await lookup(stems):
            # Dropped BEFORE the election: a refused candidate is another frame, so it neither
            # wins nor makes the real one ambiguous, and a variant left with none falls through
            # to the next exactly as an empty answer does.
            kept = [
                c
                for c in found.candidates
                if plausible(c.datetime_original, existed_by=existed_by, posted_at=posted_at)
            ]
            refused += len(found.candidates) - len(kept)
            if kept or found.truncated:
                election = elect(kept, truncated=found.truncated)
                candidates = len(kept)
                break
    await _write_result(db, job.id, job.attachment_id, election)
    logger.info(
        "sibling.resolved",
        outcome=election.outcome,
        asked=asked,
        stem_variants=len(stems) if asked else 0,
        candidates=candidates,
        refused=refused,
    )


async def _write_result(
    db: AsyncSession, job_id: int, attachment_id: int, election: Election
) -> None:
    """Lock this job's row, THEN the attachment's — the order `reclaim.reclaim_attachment` and
    `media.guard_publish` take them in — and write the result in one UPDATE.

    A job row that is gone means a delete removed it while img-manager was being asked; an
    attachment that is gone or detached is being reclaimed. Either way nothing is written and the
    worker discards the job."""
    locked_job = (
        await db.execute(select(MediaJob.id).where(MediaJob.id == job_id).with_for_update())
    ).scalar_one_or_none()
    if locked_job is None:
        raise media.AttachmentGone(f"job {job_id} was removed while its lookup ran")
    target = (
        await db.execute(
            select(Attachment.memo_id, Attachment.detached_at)
            .where(Attachment.id == attachment_id)
            .with_for_update()
        )
    ).one_or_none()
    if target is None or (target.memo_id is None and target.detached_at is not None):
        raise media.AttachmentGone(f"attachment {attachment_id} is gone or detached")
    await db.execute(_result_statement(attachment_id, election))


def _result_statement(attachment_id: int, election: Election):
    chosen = election.chosen
    if election.outcome not in (OUTCOME_UNIQUE, OUTCOME_CONSENSUS) or chosen is None:
        return (
            update(Attachment)
            .where(Attachment.id == attachment_id)
            .values(sibling_outcome=election.outcome)
        )
    dt = cast(literal(chosen.datetime_original, DateTime(timezone=True)), DateTime(timezone=True))
    lat = cast(literal(chosen.gps_latitude, Double()), Double())
    lon = cast(literal(chosen.gps_longitude, Double()), Double())
    # A capture date is borrowed only when the file said NOTHING: `capture_civil` is a date the
    # file provided too (EXIF DateTimeOriginal without an offset) and keeps its provenance.
    nothing_said = and_(
        Attachment.capture_instant.is_(None),
        Attachment.capture_civil.is_(None),
    )
    return (
        update(Attachment)
        .where(Attachment.id == attachment_id)
        .values(
            capture_instant=case((nothing_said, dt), else_=Attachment.capture_instant),
            capture_source=case(
                (and_(nothing_said, dt.is_not(None)), SOURCE_SIBLING),
                else_=Attachment.capture_source,
            ),
            latitude=func.coalesce(Attachment.latitude, lat),
            longitude=func.coalesce(Attachment.longitude, lon),
            location_source=case(
                (and_(Attachment.latitude.is_(None), lat.is_not(None)), SOURCE_SIBLING),
                else_=Attachment.location_source,
            ),
            sibling_outcome=election.outcome,
        )
    )
