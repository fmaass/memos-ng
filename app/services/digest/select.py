"""Which memos a run mails, and which it defers for media that is not ready yet.

Ported from the satellite's `app/memos.py:select_candidates`, with two differences that follow
from living beside the data instead of behind an API:

* the registry is a JOIN, not a JSON set — a memo with a `digest_deliveries` row is excluded by
  the query itself, so "already mailed" cannot drift from what was actually mailed;
* readiness reads the derived tiers through this repo's OWN map,
  `app.services.asset_paths.derived_tier_path`, rather than a second copy of the layout. The
  satellite's paths and memos-ng's happen to be identical today (`assets/thumbs/{uid}.jpg`,
  `assets/web/{uid}.mp4`); a second copy is how they would stop being.

The scan's decision is a pure function over a set of paths that exist, so every case — the grace
`break`, the past-grace `incomplete` flag, the ordering — is testable without a filesystem, and
the one filesystem hop is a single offloaded `Path.exists()` sweep rather than one per attachment
over a bind-mounted share.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from anyio import to_thread
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DigestDelivery,
    DigestSettings,
    DigestVisibilityScope,
    Memo,
    Visibility,
)
from app.services.asset_paths import derived_tier_path
from app.services.digest.gate import start_of_local_day_utc

# Which memo visibilities each scope admits. PRIVATE is in neither, in any configuration: the
# digest goes to six mailboxes outside the household.
SCOPE_VISIBILITIES: dict[str, tuple[Visibility, ...]] = {
    DigestVisibilityScope.PUBLIC: (Visibility.PUBLIC,),
    DigestVisibilityScope.PUBLIC_PROTECTED: (Visibility.PUBLIC, Visibility.PROTECTED),
}

DEFERRED_MEDIA_NOT_READY = "media not ready, still within grace"


@dataclass(frozen=True)
class MediaRef:
    """One attachment of a candidate memo, reduced to what the pipeline needs downstream."""

    attachment_id: int
    uid: str
    filename: str
    mime: str


@dataclass
class Candidate:
    """A memo that passed the query filters, with its media split by kind.

    A plain value rather than the ORM row, so render/split/mail never hold a session-bound object
    across the SMTP and Dropbox calls a run makes between commits.
    """

    memo_id: int
    uid: str
    content: str
    created_at: datetime
    images: list[MediaRef] = field(default_factory=list)
    videos: list[MediaRef] = field(default_factory=list)
    # Set past the grace: some of this memo's media never became ready and it goes out anyway,
    # with the template's "Some images weren't ready at send time" note.
    incomplete: bool = False


@dataclass
class Deferral:
    uid: str
    reason: str


@dataclass
class Selection:
    included: list[Candidate]
    deferred: list[Deferral]


def _media_of(memo: Memo) -> tuple[list[MediaRef], list[MediaRef]]:
    images: list[MediaRef] = []
    videos: list[MediaRef] = []
    for att in memo.attachments:
        ref = MediaRef(
            attachment_id=att.id, uid=att.uid, filename=att.filename, mime=att.mime or ""
        )
        if ref.mime.startswith("image/"):
            images.append(ref)
        elif ref.mime.startswith("video/"):
            videos.append(ref)
    return images, videos


def readiness_paths(candidate: Candidate) -> list[Path]:
    """Every file that must exist for this memo to be READY: a thumb per photo, a web mp4 per
    video. The poster is deliberately not in this set — a video with an mp4 but no poster still
    mails (it renders as a play button rather than a poster + badge), and holding the whole scan
    for it would defer the memo behind a file the template can do without.
    """
    paths: list[Path] = []
    for image in candidate.images:
        resolved = derived_tier_path(image.uid, "thumb")
        if resolved is not None:
            paths.append(resolved[0])
    for video in candidate.videos:
        resolved = derived_tier_path(video.uid, "web")
        if resolved is not None:
            paths.append(resolved[0])
    return paths


def decide_selection(
    candidates: list[Candidate],
    existing: set[Path],
    now_utc: datetime,
    grace_minutes: int,
) -> Selection:
    """Split ordered candidates into included and deferred — the satellite's rule, as a pure
    function over "which of these paths exist".

    The `break` is load-bearing and is not an optimisation: when a memo is not ready and is still
    inside its grace, the scan STOPS. Skipping past it would mail a later memo first and then, once
    the earlier one's thumb landed, mail that one the next morning — the digest would deliver the
    day out of order and permanently. Past the grace the memo goes out flagged `incomplete`, and
    its `digest_deliveries` row keeps it out of every later selection, so a thumb that never
    arrives cannot hold the digest open forever.
    """
    included: list[Candidate] = []
    deferred: list[Deferral] = []
    for index, candidate in enumerate(candidates):
        missing = [p for p in readiness_paths(candidate) if p not in existing]
        if not missing:
            included.append(candidate)
            continue
        age = now_utc - candidate.created_at
        if age < timedelta(minutes=grace_minutes):
            deferred.extend(
                Deferral(uid=rest.uid, reason=DEFERRED_MEDIA_NOT_READY)
                for rest in candidates[index:]
            )
            break
        candidate.incomplete = True
        included.append(candidate)
    return Selection(included=included, deferred=deferred)


def _existing_paths_sync(paths: list[Path]) -> set[Path]:
    """One offloaded hop for the whole scan. Every one of these is a network round-trip on the
    deployed bind mount, and a run may look at hundreds."""
    return {p for p in paths if p.exists()}


async def load_candidates(db: AsyncSession, settings: DigestSettings) -> list[Candidate]:
    """Every memo eligible by scope, archive state, start date and the delivery registry, oldest
    first.

    `NOT EXISTS` against `digest_deliveries` rather than a LEFT JOIN … IS NULL: the registry's
    primary key IS `memo_id`, so the anti-join is a single index probe per memo and cannot
    duplicate a row if the table ever gains a second one per memo.
    """
    visibilities = SCOPE_VISIBILITIES.get(
        settings.visibility_scope, SCOPE_VISIBILITIES[DigestVisibilityScope.PUBLIC_PROTECTED]
    )
    # `exists().where(...)` rather than `select(...).exists()`: identical SQL, and the leading
    # `exists` is what tells `tests/test_no_blocking_io.py`'s AST scanner that this is a SQL
    # construct and not a `Path.exists()` on the request loop.
    delivered = exists().where(DigestDelivery.memo_id == Memo.id)
    statement = (
        select(Memo)
        .where(
            Memo.visibility.in_(visibilities),
            Memo.archived_at.is_(None),
            Memo.created_at >= start_of_local_day_utc(settings.start_date, settings.timezone),
            ~delivered,
        )
        .order_by(Memo.created_at.asc(), Memo.id.asc())
    )
    rows = (await db.execute(statement)).scalars().unique().all()
    candidates: list[Candidate] = []
    for memo in rows:
        images, videos = _media_of(memo)
        candidates.append(
            Candidate(
                memo_id=memo.id,
                uid=memo.uid,
                content=memo.content or "",
                created_at=memo.created_at,
                images=images,
                videos=videos,
            )
        )
    return candidates


async def select_for_run(
    db: AsyncSession, settings: DigestSettings, now_utc: datetime
) -> Selection:
    """Load the candidates and apply the readiness rule — the whole selection step of a run."""
    candidates = await load_candidates(db, settings)
    if not candidates:
        return Selection(included=[], deferred=[])
    paths: list[Path] = []
    for candidate in candidates:
        paths.extend(readiness_paths(candidate))
    existing = await to_thread.run_sync(_existing_paths_sync, paths)
    return decide_selection(candidates, existing, now_utc, settings.grace_minutes)


def unresolved_attachment_ids(selection: Selection) -> list[int]:
    """Every attachment id the included memos carry — the hosting step's lookup key."""
    ids: list[int] = []
    for candidate in selection.included:
        ids.extend(ref.attachment_id for ref in candidate.images)
        ids.extend(ref.attachment_id for ref in candidate.videos)
    return ids
