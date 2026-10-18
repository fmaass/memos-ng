import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import available_timezones

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime, TagName
from app.config import settings
from app.database import async_session, get_session
from app.models import (
    Attachment,
    AttachmentStatus,
    JobState,
    MediaJob,
    MediaJobProgress,
    Memo,
    Upload,
    User,
    Visibility,
)
from app.services import asset_urls, poster, printready, reclaim, storage, variants
from app.services import attachment_tags as tag_svc
from app.services import uploads as up_svc
from app.services.asset_paths import derived_tier_path, tier_path, web_ready_uids
from app.services.attachments import (
    TAKEN_ORDERS,
    BucketSize,
    LibraryOrder,
    bucket_bound_clause,
    library_buckets,
    library_filters,
    library_page_statement,
    library_picture_tag_counts,
    library_tag_counts,
    parse_bucket_key,
)
from app.services.cursors import InvalidCursor, decode_cursor_payload, encode_cursor_payload
from app.services.memos import visible_clause

router = APIRouter(prefix="/api", tags=["assets"])

# `print` and `printready` are different things and both are load-bearing: `print` streams a
# separately-uploaded print master verbatim (satellites depend on it), `printready` is the
# generated 4096 px book rendition (MEMNG-49). Neither is signed — see asset_urls.DERIVED_TIERS.
TIERS = ("original", "thumb", "mobile", "web", "poster", "print", printready.TIER)

# The key an UNLABELLED print master carries — the same one `variants.parse_variant` mints for a
# `… - print.ext` dump, so a master attached in the UI and one dumped by filename are one thing.
DEFAULT_PRINT_VARIANT_KEY = "print"
# How much of a master's sha256 rides in its URL as `rev`. The value only has to change when the
# BYTES change (see RenditionItem.rev); 8 hex chars is 4 billion buckets over the handful of
# masters one photo ever has.
REV_LENGTH = 8


def _negotiated_path_sync(
    path: Path,
    media_type: str,
    want_webp: bool,
    fallback: tuple[Path, str] | None = None,
) -> tuple[Path, str] | None:
    """Pick the pre-generated webp variant when it was negotiated AND exists, else confirm the
    original tier is there, else try `fallback` the same way. None means 'nothing to serve' → the
    caller's 404.

    Both existence checks live in ONE off-loop hop: this is the highest-fan-out path in the app
    and the data volume is a bind-mounted share, so every stat is a network round-trip. (A webp
    hit needs a single stat here — the old code re-stat'd the file it had just found.)"""
    candidates = [(path, media_type)] if fallback is None else [(path, media_type), fallback]
    for candidate, candidate_type in candidates:
        if want_webp and candidate_type == "image/jpeg":
            webp = candidate.with_suffix(".webp")
            if webp.exists():
                return webp, "image/webp"
        if candidate.exists():
            return candidate, candidate_type
    return None


def _thumb_fallback(uid: str, tier: str) -> tuple[Path, str] | None:
    """What a `thumb` request is answered with when the thumb file is absent: the poster.

    A video is given a poster and never a thumb (app/services/poster.py), yet every serializer
    mints a thumbnailUrl for it, so without this the URL names a file that is never written. The
    fallback resolves from the uid alone, which keeps the signed path DB-free; `derived_tier_path`
    and `tier_path` stay one tier → one file, because the export and the delete set read them."""
    return derived_tier_path(uid, "poster") if tier == "thumb" else None


async def _serve_file(
    path: Path,
    media_type: str,
    request: Request,
    format: str | None,
    cache_control: str,
    fallback: tuple[Path, str] | None = None,
) -> FileResponse:
    # Prefer a pre-generated webp variant for image tiers when asked (falls back to JPEG if absent).
    want_webp = media_type == "image/jpeg" and (
        format == "webp" or "image/webp" in request.headers.get("accept", "")
    )
    resolved = await to_thread.run_sync(
        _negotiated_path_sync, path, media_type, want_webp, fallback
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="Asset not generated yet")
    # Cache-Control is `private` (never `public`): these responses are either signature-scoped or
    # auth-gated to one viewer, so a shared/intermediary cache must not store and replay them.
    return FileResponse(
        resolved[0], media_type=resolved[1], headers={"Cache-Control": cache_control}
    )


async def _viewable(db: AsyncSession, user: User, uid: str) -> Attachment:
    """An attachment is viewable if the user owns it OR it is attached to a memo the user can
    see (mirrors memo visibility: non-PRIVATE, or theirs). This is a shared household instance —
    if you can see a memo, you can see its media. Another user's PRIVATE/orphan media stays 404."""
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if att.owner_id == user.id:
        return att
    if att.memo_id is not None:
        memo = (
            await db.execute(select(Memo.id).where(Memo.id == att.memo_id, visible_clause(user)))
        ).scalar_one_or_none()
        if memo is not None:
            return att
    raise HTTPException(status_code=404, detail="Attachment not found")


class RenditionItem(BaseModel):
    variant_key: str  # "print" or "print:<slug>"
    label: str | None
    mime: str
    size: int
    downloadUrl: str
    # A content revision for cache-busting. The download route serves masters `immutable` (they are
    # archive files, fetched rarely and large), but since a master can now be REPLACED under the
    # same variant_key, the URL alone is no longer a stable identity: a browser that cached one
    # before a replace would keep serving the displaced bytes for the whole max-age. Clients append
    # it as `?v=<rev>` (see the frontend's renditionUrl), so replacing mints a fresh cache key.
    # None for a row with no digest — such a master is simply not cache-busted.
    rev: str | None


class RenditionDetail(RenditionItem):
    """One master with its pixel size — the shape the per-photo route returns.

    Dimensions are NOT on RenditionItem because they are not in the database: learning them means
    opening the file, and RenditionItem rides along on every feed page and library listing. This
    subclass exists so the one place that needs them (the print row in the viewer, opened for a
    single photo) can pay for them without putting a file read behind the feed.
    """

    width: int | None
    height: int | None


def rendition_items(att: Attachment) -> list[RenditionItem]:
    """The photo's hidden print renditions as download refs (selectin-loaded; no extra query)."""
    base = f"/api/attachments/{att.uid}/rendition"
    return [
        RenditionItem(
            variant_key=r.variant_key,
            label=r.label,
            mime=r.mime,
            size=r.size,
            downloadUrl=f"{base}/{r.variant_key}",
            rev=r.sha256[:REV_LENGTH] if r.sha256 else None,
        )
        for r in att.renditions
    ]


def has_print(att: Attachment) -> bool:
    """True if the photo has any print master — the legacy single print_path OR ≥1 rendition."""
    return att.print_path is not None or bool(att.renditions)


class AttachmentMeta(BaseModel):
    uid: str
    mime: str
    size: int
    # Hex sha256 of the stored ORIGINAL bytes, so a caller about to delete its own copy of a file it
    # posted here can prove this attachment is still exactly that file — size equality is not
    # identity. No new exposure: `_viewable` already lets this caller download the bytes.
    # None for every row that has no digest: the column arrived nullable with no backfill
    # (036cca35e101) and the old-memos import never computes one (migrate_from_memos.py), so only
    # attachments finalized through the upload path carry it.
    sha256: str | None
    status: AttachmentStatus
    width: int | None
    height: int | None
    duration: float | None
    blurhash: str | None
    captured_at: Rfc3339Datetime | None
    # The same three capture-only facts the memo's own attachment list carries (`AttachmentRef`):
    # what the file's metadata said, with no fallback, so all three are null when it said nothing.
    # `captured_at` above keeps its fallback chain and therefore always holds something — telling
    # the two apart is what these are for. `capture_civil` is a wall-clock reading with no zone.
    capture_instant: Rfc3339Datetime | None
    capture_civil: str | None
    capture_source: str | None
    mine: bool
    hasPrint: bool  # has a print master of any kind (legacy print_path OR a rendition)
    hasLegacyPrint: bool  # has the legacy single print_path → the /print download button applies
    # Whether `posterUrl` has a file behind it. Always false for anything that is not a video (no
    # other kind of attachment has a poster tier); for a video it is true once EITHER the fast lane
    # extracted a hero frame OR the post supplied its own poster (MemoCreate.video_posters) — which
    # is what makes it assertable straight after a create, before the worker has run at all.
    hasPoster: bool
    renditions: list[RenditionItem]
    tags: list[str] = Field(
        default=[], description="The picture's own tags, sorted by name; never its memo's tags."
    )
    downloadUrl: str
    printUrl: str
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    # Whether `webUrl` has a file behind it — same field and same reading as AttachmentRef.webReady
    # (app/routers/memos.py): false for anything that is not a video, and for a video until its
    # transcode is published.
    webReady: bool
    posterUrl: str
    # The Info sheet's File and Where rows (MEMNG-864). A deliberate, scoped disclosure: this route
    # answers only the viewers `_viewable` admits — the owner, or anyone who can see the memo, the
    # same audience the map's markers reach through `visible_clause` — so the coordinate reaches no
    # one who could not already find this picture on the map. The list and AttachmentRef ride on
    # every page a viewer scrolls and stay coordinate-free (tests/test_location_privacy.py).
    filename: str | None
    latitude: float | None
    longitude: float | None
    # Where the reading came from, as stored: 'exif' / 'quicktime', or 'none' / 'unreadable' / null
    # when there is no coordinate to show (app/models.py Attachment.location_source).
    locationSource: str | None
    # Who uploaded THIS picture: its own owner's display name, else their subject; null when the
    # row has no owner. Never the memo's owner, never the requester, never an email.
    addedBy: str | None


class AttachmentListItem(BaseModel):
    uid: str
    mime: str
    status: AttachmentStatus
    blurhash: str | None
    width: int | None  # for justified/aspect-ratio gallery layout (no CLS)
    height: int | None
    mine: bool  # the requesting user owns it (→ may delete)
    hasPrint: bool  # has a print master of any kind (legacy print_path OR a rendition)
    hasLegacyPrint: bool  # has the legacy single print_path → the /print download button applies
    renditions: list[RenditionItem]
    orphan: bool  # not linked to a memo
    memoUid: str | None  # the memo this hangs on, null for orphans (MEMNG-171)
    # When it was taken: the capture instant, else the memo's date, else (orphans) the upload's —
    # never `captured_at` (app/services/attachments.py `taken_at_expr`). MEMNG-507, additive.
    takenAt: Rfc3339Datetime
    filename: str  # the stored original name, so a share sheet can name the file (MEMNG-507)
    # The tags of the memo this hangs on, sorted by name like MemoOut.tags; empty for an orphan
    # and for an untagged memo. MEMNG-651, additive.
    tags: list[str]
    pictureTags: list[str] = Field(
        default=[], description="The picture's own tags, sorted by name; never its memo's tags."
    )
    # The visibility of the memo this hangs on; null for an orphan, which has none. MEMNG-785,
    # additive.
    visibility: Visibility | None
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    # Whether `webUrl` has a file behind it — same field and same reading as AttachmentRef.webReady
    # (app/routers/memos.py): false for anything that is not a video, and for a video until its
    # transcode is published.
    webReady: bool
    posterUrl: str
    downloadUrl: str
    printUrl: str


def _list_item(
    att: Attachment,
    user_id: int,
    memo_uid: str | None,
    taken_at: datetime,
    tags: list[str],
    web_ready: AbstractSet[str],
    visibility: Visibility | None,
) -> AttachmentListItem:
    """`memo_uid`, `taken_at`, `tags` and `visibility` are passed IN, never read off `att` —
    there is no `Attachment.memo` relationship and adding one would lazy-load per row. The caller
    selects them in the same statement."""
    base = f"/api/attachments/{att.uid}"
    return AttachmentListItem(
        uid=att.uid,
        mime=att.mime,
        status=att.status,
        blurhash=att.blurhash,
        width=att.width,
        height=att.height,
        mine=att.owner_id == user_id,
        hasPrint=has_print(att),
        hasLegacyPrint=att.print_path is not None,
        renditions=rendition_items(att),
        orphan=att.memo_id is None,
        memoUid=memo_uid,
        takenAt=taken_at,
        filename=att.filename,
        tags=tags,
        pictureTags=list(att.picture_tag_names),
        visibility=visibility,
        **asset_urls.signed_tier_urls(att.uid, user_id, now=time.time()),
        webReady=att.uid in web_ready,
        downloadUrl=f"{base}/original",
        printUrl=f"{base}/print",
    )


# The library list is a BARE ARRAY and stays one — the SPA reads the body as an array
# (frontend/src/views/ResourcesView.vue) — so the continuation token rides in a response HEADER
# rather than an envelope. That is the only additive shape available: wrapping the body breaks
# every existing caller, and an "envelope only when ?cursor is sent" variant has no way to hand the
# FIRST page (which by definition sends no cursor) the token it needs to ask for the second. The
# chunked upload already publishes an offset the same way (Upload-Offset, app/routers/uploads.py).
NEXT_CURSOR_HEADER = "X-Next-Cursor"


def _next_cursor_header() -> dict[str, Any]:
    """A fresh dict per call: FastAPI merges `openapi_extra` into the operation by reference, so a
    shared constant would leave two operations aliasing the same node in the served document."""
    return {
        NEXT_CURSOR_HEADER: {
            "description": "Opaque continuation token — resend it as `?cursor=` (with the same "
            "`type`, `tag`, `limit` and `order`) to get the next page. Absent on the last page.",
            "schema": {"type": "string"},
        }
    }


def encode_attachment_cursor(att: Attachment) -> str:
    """The keyset cursor of the library list: the last row of the page, opaque and base64url like
    the memo feed's (app/services/cursors.py mints both). The id alone IS the whole key here — see
    the ordering note in `list_attachments`."""
    return encode_cursor_payload(str(att.id))


def decode_attachment_cursor(cursor: str) -> int:
    """`InvalidCursor` (→ 422) — never a 500, and never a silent restart from the top of the
    library, which would make a paging client loop forever over the first page. Strict since
    MEMNG-163: the old permissive base64 read junk like `M!Q==` as a valid id."""
    raw = decode_cursor_payload(cursor)
    try:
        return int(raw)
    except ValueError:
        raise InvalidCursor("cursor is not an attachment id") from None


TAKEN_CURSOR_TAG = "t"


def encode_taken_cursor(taken_at: datetime, att_id: int) -> str:
    """The keyset cursor of the TAKEN orders: `t|<instant>|<id>`, the instant in UTC at FULL
    microsecond precision. It is never the rendered `takenAt`, which is truncated to the second
    (app/api_types.py): three rows tied at 12:00:00.5 and paged two at a time would lose the third
    if the cursor resumed from 12:00:00. The `t` prefix is what lets each decode path refuse the
    other kind — an id cursor is digits only."""
    utc = taken_at.astimezone(UTC).replace(tzinfo=None)
    return encode_cursor_payload(
        f"{TAKEN_CURSOR_TAG}|{utc.isoformat(timespec='microseconds')}Z|{att_id}"
    )


def decode_taken_cursor(cursor: str) -> tuple[datetime, int]:
    """`InvalidCursor` (→ 422) for an id cursor, a naive or unparsable instant, or a missing id —
    never a silent restart from the top."""
    parts = decode_cursor_payload(cursor).split("|")
    if len(parts) != 3 or parts[0] != TAKEN_CURSOR_TAG:
        raise InvalidCursor("cursor is not a taken-order cursor")
    try:
        taken_at = datetime.fromisoformat(parts[1])
        att_id = int(parts[2])
    except ValueError:
        raise InvalidCursor("cursor is not a taken-order cursor") from None
    if taken_at.tzinfo is None:
        raise InvalidCursor("cursor instant carries no zone")
    return taken_at, att_id


@router.get(
    "/attachments",
    response_model=list[AttachmentListItem],
    openapi_extra={"responses": {"200": {"headers": _next_cursor_header()}}},
)
async def list_attachments(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    type: str | None = None,
    limit: int = 200,
    cursor: str | None = None,
    order: Literal["desc", "asc", "taken_desc", "taken_asc"] = "desc",
    tag: str | None = None,
    bucket: str | None = None,
    tz: str = "Europe/Zurich",
    mine: bool = False,
    # SkipJsonSchema like the memo listing's own `visibility`: a query string cannot carry a JSON
    # null, so absent is the only way it arrives unset.
    visibility: Visibility | SkipJsonSchema[None] = None,
    pictureTag: str | None = None,
) -> list[AttachmentListItem]:
    """The household media library the user can see, newest first: their own attachments
    (including orphans) PLUS media on any memo visible to them (non-PRIVATE). ?type=image|video
    filters by mime prefix; `orphan` flags media not attached to any memo, and `memoUid` names the
    memo it does hang on. Another user's PRIVATE or orphan media is excluded — the same visibility
    rule the memo surfaces apply. ?order=asc walks the same library oldest-first.
    ?order=taken_desc|taken_asc walks it by `takenAt` (ties broken by upload order); ?tag=<name>
    keeps only media whose memo carries exactly that tag — an unknown tag is an empty page.
    Each item names the tags of its memo, by name; an orphan's are empty.
    ?pictureTag=<name> keeps only media that itself carries exactly that picture tag, orphans
    included; a memo's tags never match it. Each item names its own picture tags too.
    ?mine=true keeps only the caller's own attachments, orphans included; ?visibility=<V> keeps
    only media on a memo of that visibility, so no orphan. Both only narrow what the caller may
    already see. Each item names its memo's visibility, null for an orphan.
    ?bucket=<key> opens a taken-order walk at a day, month or year — a `key` exactly as the
    buckets of `tz` (an IANA zone, default Europe/Zurich) name it — so the first item is that
    bucket's newest under taken_desc, its oldest under taken_asc; an empty bucket opens at the
    next item beyond it. A key the buckets could not name, an unknown zone or a bucket under an
    upload order is a 422.

    Paged by keyset, like the memo feed: when more rows follow, the response carries an
    `X-Next-Cursor` header; resend it as `?cursor=` — with the same filters, which the cursor does
    not carry — for the page after. The body is a plain array on every page, cursor or not.
    """
    if tz not in _known_zones():
        raise HTTPException(status_code=422, detail="tz is not a known IANA time zone")
    opened = None
    if bucket is not None:
        if order not in ("taken_desc", "taken_asc"):
            raise HTTPException(status_code=422, detail="bucket needs a taken order")
        opened = parse_bucket_key(bucket)
        if opened is None:
            raise HTTPException(status_code=422, detail="bucket is not a bucket key")
    filters = library_filters(
        user, type, tag, mine=mine, visibility=visibility, picture_tag=pictureTag
    )
    if opened is not None:
        # Beside the cursor, not instead of it: a continuation's cursor already lies beyond the
        # bucket's start, so resending the bucket with it changes nothing.
        filters.append(bucket_bound_clause(*opened, tz, order == "taken_asc"))
    return await library_page(db, user, response, filters, order, limit, cursor)


async def library_page(
    db: AsyncSession,
    user: User,
    response: Response,
    filters: list[Any],
    order: LibraryOrder,
    limit: int,
    cursor: str | None,
) -> list[AttachmentListItem]:
    """One keyset page of the library under `filters` — own (incl. orphans, where Memo is NULL) OR
    on a memo the user can see, then whatever narrows it — with the `X-Next-Cursor` header set
    when more rows follow. Shared by `GET /api/attachments` and a collection's member read, so the
    two cannot differ in cursor, order or item shape. A cursor of the wrong kind is a 422
    (`InvalidCursor`), never a silent restart from the top."""
    taken_order = order in TAKEN_ORDERS
    stmt = library_page_statement(
        filters,
        order,
        after_taken=decode_taken_cursor(cursor) if taken_order and cursor is not None else None,
        after_id=decode_attachment_cursor(cursor)
        if not taken_order and cursor is not None
        else None,
    )
    page = max(1, min(limit, 500))
    # limit + 1: one extra row is how the page learns another page exists, without a second query
    # (list_memos does the same). It is dropped before serializing, so the body is unchanged.
    rows = list((await db.execute(stmt.limit(page + 1))).all())
    if len(rows) > page:
        last, _, last_taken, _, _ = rows[page - 1]
        response.headers[NEXT_CURSOR_HEADER] = (
            encode_taken_cursor(last_taken, last.id)
            if taken_order
            else encode_attachment_cursor(last)
        )
        rows = rows[:page]
    ready = await web_ready_uids(att for att, _, _, _, _ in rows)
    return [
        _list_item(att, user.id, memo_uid, when, list(tags), ready, visibility)
        for att, memo_uid, when, tags, visibility in rows
    ]


class AttachmentBucket(BaseModel):
    key: str  # the bucket's local start: "2026-09-04" (day), "2026-09" (month), "2026" (year)
    count: int
    coverUid: str  # the bucket's newest item by (takenAt, upload order)
    coverThumbnailUrl: str  # that item's signed thumbnail, minted for the requesting viewer
    # The tags of the memos the bucket's pictures hang on: the tag on the most of them first, then
    # by name, every one of them (a client showing three counts the rest). MEMNG-651, additive.
    tags: list[str]


class AttachmentTagCount(BaseModel):
    name: str
    count: int  # media items, not memos: a memo with three pictures counts three


@cache
def _known_zones() -> frozenset[str]:
    return frozenset(available_timezones())


# The literal routes are registered BEFORE `/attachments/{uid}` below: FastAPI matches in
# registration order, and `{uid}` would otherwise answer `buckets`, `tags` and `picture-tags` as
# attachment uids.
@router.get("/attachments/buckets", response_model=list[AttachmentBucket])
async def attachment_buckets(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    size: BucketSize,
    type: str | None = None,
    tz: str = "Europe/Zurich",
    tag: str | None = None,
    mine: bool = False,
    visibility: Visibility | SkipJsonSchema[None] = None,
    pictureTag: str | None = None,
) -> list[AttachmentBucket]:
    """The library the list shows — same visibility, same `type`, `tag`, `mine`, `visibility`
    and `pictureTag` filters — counted per
    calendar day, month or year of `takenAt` in `tz` (an IANA zone, default Europe/Zurich), newest
    first. Each bucket names its newest item as the cover, with that item's signed thumbnail —
    for a video its poster frame, the one picture a video has — and the tags of the memos its
    items hang on, the tag on the most items first, then by name."""
    if tz not in _known_zones():
        raise HTTPException(status_code=422, detail="tz is not a known IANA time zone")
    now = time.time()
    return [
        AttachmentBucket(
            key=key,
            count=count,
            coverUid=uid,
            coverThumbnailUrl=asset_urls.signed_cover_url(uid, mime, user.id, now=now),
            tags=tags,
        )
        for key, count, uid, mime, tags in await library_buckets(
            db, user, size, tz, type, tag, mine=mine, visibility=visibility, picture_tag=pictureTag
        )
    ]


@router.get("/attachments/tags", response_model=list[AttachmentTagCount])
async def attachment_tags(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    type: str | None = None,
    mine: bool = False,
    visibility: Visibility | SkipJsonSchema[None] = None,
) -> list[AttachmentTagCount]:
    """Every tag on a memo carrying media the user can see, with the number of such media under
    the same `type`, `mine` and `visibility` filters the list applies — so a tag's count is the
    length of the list under `?tag=` and those filters. Most items first, then by name; a tag
    with no media is absent."""
    return [
        AttachmentTagCount(name=name, count=count)
        for name, count in await library_tag_counts(
            db, user, type, mine=mine, visibility=visibility
        )
    ]


@router.get("/attachments/picture-tags", response_model=list[AttachmentTagCount])
async def attachment_picture_tags(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    type: str | None = None,
    mine: bool = False,
    visibility: Visibility | SkipJsonSchema[None] = None,
) -> list[AttachmentTagCount]:
    """Every picture tag on media the user can see, with the number of such media under the same
    `type`, `mine` and `visibility` filters the list applies — so a tag's count is the length of
    the list under `?pictureTag=` and those filters. Memo tags are not counted here. Most items
    first, then by name; a tag on no visible media is absent."""
    return [
        AttachmentTagCount(name=name, count=count)
        for name, count in await library_picture_tag_counts(
            db, user, type, mine=mine, visibility=visibility
        )
    ]


# PAT-or-session like the original-tier download below (not a docstring: FastAPI would publish it
# as the operation's OpenAPI description). A headless client that may already fetch an attachment's
# BYTES with its token can read that attachment's metadata too — strictly less sensitive than what
# it can already download, and `_viewable` applies the identical visibility rules to either caller.
@router.get("/attachments/{uid}", response_model=AttachmentMeta)
async def attachment_meta(
    uid: str,
    user: Annotated[User, Depends(auth.require_pat_or_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentMeta:
    att = await _viewable(db, user, uid)
    base = f"/api/attachments/{uid}"
    # One stat, and only for a video: this route serves a single attachment, so the round-trip is
    # affordable here in a way it would not be on AttachmentRef, which rides on every item of every
    # feed page.
    has_poster = await poster.has_poster(att.uid) if att.mime.startswith("video/") else False
    # One query for the uploader's name, only once `_viewable` has admitted the caller, and only
    # the two columns the field is built from — the email column is never loaded.
    added_by: str | None = None
    if att.owner_id is not None:
        owner = (
            await db.execute(select(User.display_name, User.subject).where(User.id == att.owner_id))
        ).one_or_none()
        if owner is not None:
            added_by = owner.display_name or owner.subject
    return AttachmentMeta(
        uid=att.uid,
        mime=att.mime,
        size=att.size,
        sha256=att.sha256,
        status=att.status,
        width=att.width,
        height=att.height,
        duration=att.duration,
        blurhash=att.blurhash,
        captured_at=att.captured_at,
        capture_instant=att.capture_instant,
        capture_civil=att.capture_civil,
        capture_source=att.capture_source,
        mine=att.owner_id == user.id,
        hasPrint=has_print(att),
        hasLegacyPrint=att.print_path is not None,
        hasPoster=has_poster,
        renditions=rendition_items(att),
        tags=list(att.picture_tag_names),
        downloadUrl=f"{base}/original",
        printUrl=f"{base}/print",
        **asset_urls.signed_tier_urls(att.uid, user.id, now=time.time()),
        webReady=att.uid in await web_ready_uids([att]),
        filename=att.filename,
        latitude=att.latitude,
        longitude=att.longitude,
        locationSource=att.location_source,
        addedBy=added_by,
    )


def _rendition_dims_sync(paths: list[str]) -> dict[str, tuple[int, int]]:
    """Measure every master of ONE photo in a single off-loop hop.

    `Image.open` parses the header and stops — it does not decode — so this reads a few KB per
    file rather than the tens of MB a full decode of a print master would (same trick the photo-book
    manifest uses). A file Pillow cannot parse (an exotic TIFF/RAW, a truncated master) is left out
    and its row reports null dimensions rather than failing the request.
    """
    dims: dict[str, tuple[int, int]] = {}
    for path in paths:
        try:
            with Image.open(path) as img:
                dims[path] = (img.width, img.height)
        except Exception:
            continue
    return dims


@router.get("/attachments/{uid}/renditions", response_model=list[RenditionDetail])
async def list_renditions(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[RenditionDetail]:
    """A photo's hidden print renditions, WITH their pixel dimensions (household-visible, like the
    legacy print tier). Registered BEFORE the generic /{uid}/{tier} route so the literal
    `renditions` segment isn't a tier."""
    att = await _viewable(db, user, uid)
    dims = await to_thread.run_sync(_rendition_dims_sync, [r.storage_path for r in att.renditions])
    return [
        RenditionDetail(
            **item.model_dump(),
            width=dims.get(row.storage_path, (None, None))[0],
            height=dims.get(row.storage_path, (None, None))[1],
        )
        for item, row in zip(rendition_items(att), att.renditions, strict=True)
    ]


def _print_variant_key(raw: str | None) -> tuple[str, str | None]:
    """Validate a client-supplied print variant key → (variant_key, label).

    Only print keys, and only in the shape the filename ingest itself produces (`variants`): the
    bare `print`, or `print:<slug>` whose slug is ALREADY normalized. The key is a path segment on
    the download route and the identity the photo-book selector keys off, so an unnormalized or
    non-print key would mint a master the rest of the system cannot address.
    """
    # Only an ABSENT key defaults. An explicitly supplied empty string is a client sending
    # nonsense, and it gets the 400 the grammar promises rather than a silent `print`.
    key = DEFAULT_PRINT_VARIANT_KEY if raw is None else raw.strip()
    if not printready.is_print_variant(key):
        raise HTTPException(status_code=400, detail="Not a print master variant key")
    if key == "print":
        return key, None
    label = key.split(":", 1)[1]
    if not label or label != variants.slug_label(label):
        raise HTTPException(status_code=400, detail="Print master label must be a slug")
    return key, label


class RenditionAttach(BaseModel):
    upload_uid: str  # a finished resumable upload (POST /api/uploads → PATCH chunks)
    variant_key: str | None = None  # absent (only) → the bare `print`


# The legacy multipart `/print` route, which writes the separate print_path column, is untouched
# by this one and is retired separately.
@router.post("/attachments/{uid}/renditions", response_model=AttachmentMeta, status_code=201)
async def attach_rendition(
    uid: str,
    body: RenditionAttach,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentMeta:
    """Attach — or replace — the print master of an OWNED image, from a finished resumable upload.

    Takes an upload_uid rather than a multipart body so a multi-hundred-MB master gets the chunked,
    resumable, progress-reporting path every other upload in this app uses.
    """
    variant_key, label = _print_variant_key(body.variant_key)
    # UPLOAD BEFORE ATTACHMENT (MEMNG-267 R5). This route consumes the upload — it deletes the row
    # at the end — so taking the attachment first meant holding A while waiting for U, against
    # every finalize, which holds U and then waits for A. The two orders are a cycle; this one is
    # the protocol's.
    #
    # The unlocked read comes first all the same, and only to answer the 404/400 the API answers
    # today: a request for somebody else's photo, or for a video, must not be able to queue on an
    # upload lock, and the error precedence the clients see must not change. Nothing is DECIDED
    # from it — every value the placement acts on is re-read through the locks below.
    # COLUMNS, not the entity: `renditions` is selectin-loaded, and loading the entity here would
    # read the rendition state before the lock is taken — the very thing this route must not do.
    # These two columns settle the 404 and the 400 and decide nothing else.
    precheck = (
        await db.execute(select(Attachment.owner_id, Attachment.mime).where(Attachment.uid == uid))
    ).first()
    if precheck is None or precheck.owner_id != user.id:  # OWNER-only: a mutation, unlike viewing
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not precheck.mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Print masters are for images only")
    # Scoped to the caller like every other upload verb, so another user's session answers exactly
    # as an unknown uid does — and LOCKED, because this request will delete it. A row that vanished
    # while this call waited was consumed by whoever got there first, and a consumed session
    # answers exactly as an unknown uid does.
    upload = (
        await db.execute(
            select(Upload)
            .where(Upload.upload_uid == body.upload_uid, Upload.owner_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    # Now the attachment's row lock, and its rendition state read THROUGH it. The placement below
    # decides from the master this photo currently has, and the post-commit sweep unlinks the file
    # that decision displaced — so a request that WAITED on this lock must not decide from the
    # state it read before waiting: that stale decision would reap the file the winner just made
    # current. `renditions` is selectin-loaded, i.e. a follow-up query issued once the lock is
    # granted, and populate_existing overwrites anything this session already held.
    att = (
        await db.execute(
            select(Attachment)
            .where(Attachment.uid == uid)
            .with_for_update(of=Attachment)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if att is None or att.owner_id != user.id:  # deleted or handed on while this call waited
        raise HTTPException(status_code=404, detail="Attachment not found")
    data_root = Path(settings.DATA_ROOT)
    # A master of a photo that lives in a memo files into that memo's folder beside the screen
    # master, under its own filename; an unlinked photo stages in _unfiled/ exactly like the ingest
    # path, and link_attachments moves it when the photo is attached to a memo.
    memo = (
        (await db.execute(select(Memo).where(Memo.id == att.memo_id))).scalar_one_or_none()
        if att.memo_id is not None
        else None
    )
    dest_dir = (
        storage.memo_dir(data_root, memo.created_at, memo.uid)
        if memo is not None
        else storage.unfiled_dir(data_root)
    )
    try:
        placed = await up_svc.attach_print_master(db, upload, att, variant_key, label, dest_dir)
    except ValueError as exc:  # an incomplete upload — its part file and row are left to the reaper
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        await db.commit()
    except Exception:
        # Only this request's brand-new file goes; the master it would have displaced is untouched
        # and still exactly what the (still-committed) row describes.
        await to_thread.run_sync(_unlink_all, [Path(placed.rollback_path)])
        raise
    # AFTER the commit, so the ordering is crash-safe in one direction: until it lands the row still
    # names the old file and that file still exists; once it lands the row names the new one, which
    # is already on disk. A crash between leaves at worst an unreferenced master.
    if placed.displaced is not None:
        await to_thread.run_sync(_unlink_all, [Path(placed.displaced)])
    # The session does not expire on commit, and `attachment_meta` re-reads the SAME
    # identity-mapped attachment — so without this the response would carry the collection loaded
    # BEFORE the attach and report the photo as having no master at all.
    await db.refresh(att, ["renditions"])
    return await attachment_meta(uid, user, db)


@router.delete("/attachments/{uid}/rendition/{variant_key}", status_code=204)
async def delete_rendition(
    uid: str,
    variant_key: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove one print master — OWNER-only (a mutation; viewing a master is household-wide). The
    screen photo and every derived tier are untouched: only the archive master goes."""
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None or att.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    rendition = next((r for r in att.renditions if r.variant_key == variant_key), None)
    if rendition is None:
        raise HTTPException(status_code=404, detail="Rendition not found")
    path = Path(rendition.storage_path)
    await db.delete(rendition)
    await db.commit()
    # After the commit, for the same crash-safety reason as the attach above: a rendition file is
    # unique and unshared (never a deterministic path), so once the row is gone nothing names it.
    await to_thread.run_sync(_unlink_all, [path])


@router.get("/attachments/{uid}/rendition/{variant_key}")
async def serve_rendition(uid: str, variant_key: str, request: Request) -> FileResponse:
    """Download a single print rendition — download-only, household-visible, on the short-session
    auth path (never the signed/public fast path; prints are low fan-out, not public-by-uid)."""
    async with async_session() as db:
        user = await auth.require_user(request, db)
        att = await _viewable(db, user, uid)
        match = next((r for r in att.renditions if r.variant_key == variant_key), None)
        resolved = (match.storage_path, match.mime) if match is not None else None
    if resolved is None:
        raise HTTPException(status_code=404, detail="Rendition not found")
    cache = f"private, max-age={settings.ASSET_CACHE_SECONDS}, immutable"  # immutable master
    return await _serve_file(Path(resolved[0]), resolved[1], request, None, cache)


@router.get("/attachments/{uid}/{tier}")
async def serve_asset(
    uid: str,
    tier: str,
    request: Request,
    exp: str | None = None,
    scope: str | None = None,
    sig: str | None = None,
    format: str | None = None,
) -> FileResponse:
    if tier not in TIERS:
        raise HTTPException(status_code=404, detail="Unknown tier")
    # SIGNED FAST PATH: a derived tier with a valid HMAC signature is served with NO DB and NO auth
    # dependency — the feed already authorized at mint time. This keeps the high-fan-out media path
    # entirely off the DB pool (the QueuePool-exhaustion / missing-thumbnails class of bug).
    if sig is not None and tier in asset_urls.DERIVED_TIERS:
        if not asset_urls.verify_asset_sig(uid, tier, exp, scope, sig, now=time.time()):
            raise HTTPException(status_code=403, detail="Bad or expired signature")
        resolved = derived_tier_path(uid, tier)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Unknown tier")
        # Cap the browser cache to the signature's remaining life so a now-revoked (e.g.
        # memo-gone-PRIVATE) tier can't be replayed from cache far past exp — bounds the mint-time-
        # auth staleness window (ADR-0009). `exp` already passed verify_asset_sig (numeric, future),
        # so remaining is > 0 and at most ASSET_URL_TTL. (`exp is not None` here for the type
        # checker; verify_asset_sig already rejected a None/non-numeric exp.)
        remaining = (int(exp) - int(time.time())) if exp is not None else settings.ASSET_URL_TTL
        cache = f"private, max-age={max(1, remaining)}, immutable"
        fallback = _thumb_fallback(uid, tier)
        return await _serve_file(resolved[0], resolved[1], request, format, cache, fallback)
    # UNSIGNED / AUTH PATH (original/print on THIS route): DB work in a short-lived session released
    # BEFORE streaming, so the download holds no pool connection either. (The /file/* compat route
    # is separate — app/routers/compat.py — and serves derived tiers only, per ADR-0004 /
    # fixtures F2; it does not pass through here. Since MEMNG-25 it gates those tiers on the same
    # visibility rules as `_viewable` below, with anonymous limited to PUBLIC.)
    # PAT-or-session so headless API clients (e.g. the memos-slideshow generator) can fetch the
    # original tier with a Personal Access Token, matching the /api/v1/* compat surface.
    async with async_session() as db:
        user = await auth.require_pat_or_user(request, db)
        att = await _viewable(db, user, uid)
        resolved = tier_path(att, tier)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown tier")
    # immutable uid-keyed bytes, but private (one authed viewer) — never cached by a shared proxy.
    cache = f"private, max-age={settings.ASSET_CACHE_SECONDS}, immutable"
    fallback = _thumb_fallback(uid, tier)
    return await _serve_file(resolved[0], resolved[1], request, format, cache, fallback)


def _unlink_all(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort; the DB row is already gone


# Postgres' foreign_key_violation: the picture-tag insert names an attachment that was deleted after
# this request looked it up.
_FK_VIOLATION = "23503"

_TAG_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such attachment, or the caller does not own it."},
}


async def _owned(db: AsyncSession, user: User, uid: str) -> Attachment:
    """The caller's own attachment by uid; anyone else's, visible or not, is the same 404 as an
    unknown uid, so a write never tells a member which uids exist."""
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None or att.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return att


@router.put("/attachments/{uid}/tags/{name}", status_code=204, responses=_TAG_WRITE_RESPONSES)
async def add_picture_tag(
    uid: str,
    name: TagName,
    user: Annotated[User, Depends(auth.require_pat_or_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Tag the caller's own picture with `name`. Idempotent: tagging it again changes nothing and
    answers 204 again. A picture tag is the picture's own and never its memo's."""
    att = await _owned(db, user, uid)
    try:
        await tag_svc.add(db, att, name)
        await db.commit()
    except IntegrityError as exc:
        # Only a vanished attachment is the caller's 404; any other integrity failure is not.
        if getattr(exc.orig, "sqlstate", None) != _FK_VIOLATION:
            raise
        await db.rollback()
        raise HTTPException(status_code=404, detail="Attachment not found") from exc


@router.delete("/attachments/{uid}/tags/{name}", status_code=204, responses=_TAG_WRITE_RESPONSES)
async def remove_picture_tag(
    uid: str,
    name: TagName,
    user: Annotated[User, Depends(auth.require_pat_or_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Take the picture tag `name` off the caller's own picture. Idempotent: a tag it does not
    carry stays absent and answers 204."""
    att = await _owned(db, user, uid)
    await tag_svc.remove(db, att, name)
    await db.commit()


@router.delete("/attachments/{uid}", status_code=204)
async def delete_attachment(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete an attachment — OWNER-only (a mutation, unlike viewing). Removes its media jobs,
    DB row, and all on-disk files (original + derived tiers). If it was linked to a memo, the
    memo keeps its text; only the media is removed."""
    # (Kept out of the docstring, which FastAPI publishes as this endpoint's OpenAPI description.)
    # This request NEVER waits on a running media job, and never inverts against one. When
    # MEMNG-41 was filed a worker held its job row for the whole transcode, so both statements
    # this used to issue queued behind that lock for up to an hour — the plain `DELETE FROM
    # media_jobs WHERE attachment_id = …` and the attachment delete itself through the old FK's
    # ON DELETE CASCADE — and a handful of those exhaust DB_POOL_SIZE + overflow on the shared
    # Postgres. MEMNG-90 shortened the hold to the worker's two short transactions (the claim is
    # committed before processing, `app/worker.py:279`), so what is left to avoid is not the long
    # wait but the ORDER: a cascade would take the attachment row before the job row, against the
    # J → A every worker and reclaim path acquires in (MEMNG-267 R7). So the FK is gone (see
    # MediaJob) and the job cleanup below takes only the rows it can lock immediately; the
    # in-flight row belongs to its worker, which discards job and partial output on finding the
    # attachment gone (media.AttachmentGone → worker.run_once).
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None or att.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # One seam for every attachment deletion (app/services/reclaim.py): jobs, then the row locked
    # and revalidated, then its files, then the row — all in one transaction. This route used to
    # delete the row, commit, and unlink afterwards, so a crash in that gap left files that no row
    # named any more and no sweep could ever find. The new order can only lose the other way: an
    # interrupted delete leaves the row with its files already gone, which a retry of this very
    # request finishes off. That is deliberately the recoverable half of the trade — the caller
    # asked for this attachment to go.
    #
    # `IDENTITY`: this deletes the attachment the caller NAMED, memo or no memo — being on a memo
    # is this route's ORDINARY case (the memo keeps its text, the media goes), never a reason to
    # stand down. Re-reading the row under its own lock is the whole revalidation here, and it is
    # what makes two concurrent deletes resolve to one winner instead of one 500.
    # The shared unit, in a task of its own holding its own session — the way PATCH and DELETE on a
    # memo run it, and for the same reason: this request's session must not be what holds the row.
    # The unlinking is held to its end wherever the unit is awaited from, this request's own
    # session included (`reclaim._unlink_off_the_loop`), so that is not what the task buys. What it
    # buys is the other two. Cancelled — the client goes away, the process is stopping — this
    # request ends promptly where it was cancelled instead of waiting out a thread nobody is there
    # for any more, and the unit re-raises, so the cancellation is not swallowed on the way. And
    # the reclaim it leaves behind is registered where a shutdown can still await it
    # (`reclaim.drain_pending_reclaims`), which a reclaim awaited on this request's session would
    # not be once the request is gone.
    result = await reclaim.reclaim_row_shielded(att.id, reclaim.Revalidate.IDENTITY)
    if result.outcome == "vanished":  # a concurrent delete won between the read above and the lock
        raise HTTPException(status_code=404, detail="Attachment not found")
    if result.outcome == "failed_unlink":
        # The row was KEPT on purpose (app/services/reclaim.py): its files are still on disk and it
        # is the only record of where they are. Answering 204 here would tell the caller their
        # media was deleted when it is all still there.
        raise HTTPException(
            status_code=500, detail="Could not remove this attachment's files; nothing was deleted"
        )


def _save_print_sync(part_dir: Path, uid: str, staged: Path, suffix: str) -> str:
    part_dir.mkdir(parents=True, exist_ok=True)
    dest = part_dir / f"{uid}{suffix}"
    os.replace(staged, dest)
    return str(dest)


async def _file_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    """Yield the upload in bounded reads. Starlette spools the multipart part to disk, but a
    single `file.read()` would still pull the whole master into memory before anything checks it."""
    while chunk := await file.read(up_svc.STREAM_CHUNK_SIZE):
        yield chunk


@router.post("/attachments/{uid}/print", response_model=AttachmentMeta)
async def upload_print(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    file: UploadFile,
) -> AttachmentMeta:
    """Attach a print-quality master to an OWNED image attachment (a separate Lightroom print
    export). The web rendition keeps displaying; this file is hidden from the feed and
    download-only via the `print` tier."""
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None or att.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not att.mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Print masters are for images only")
    data_root = Path(settings.DATA_ROOT)
    suffix = Path(file.filename or "").suffix or ".bin"
    # Receive to a staging file with the cap enforced on the running count, so an oversize export is
    # refused mid-stream instead of after the whole master has been buffered.
    staged = data_root / "uploads" / f"print-{uuid.uuid4().hex}.part"
    try:
        size = (
            await up_svc.spool_stream(
                _file_chunks(file), staged, start=0, ceiling=settings.MAX_UPLOAD_MIB * 1024 * 1024
            )
        ).received
        # Lock this attachment's row for the rest of the request: the placement decision below
        # reads print_path and the post-commit sweep unlinks the file it named, so two concurrent
        # print uploads could otherwise leave the committed print_path pointing at a file the other
        # one deleted. Taken AFTER the receive, so the lock spans only placement + commit (ms), and
        # it also re-reads print_path — the value loaded before the upload streamed may be stale.
        await db.refresh(att, ["print_path"], with_for_update=True)
        old_print = Path(att.print_path) if att.print_path else None
        # A print uploaded to an attachment already in a memo lands in that memo's folder under its
        # real filename (beside the screen); an unlinked attachment keeps the legacy print/{uid}.
        memo = (
            (await db.execute(select(Memo).where(Memo.id == att.memo_id))).scalar_one_or_none()
            if att.memo_id is not None
            else None
        )
        if memo is not None:
            dest = storage.memo_dir(data_root, memo.created_at, memo.uid)
            name = file.filename or f"{uid}-print{suffix}"
            if old_print is not None and storage.is_placement_of(old_print, dest, name):
                # Re-upload of this attachment's print under the same filename: replace the file
                # print_path already names. Reserving a fresh `name (n)` here (what _reserve does,
                # correctly, for a DIFFERENT attachment) would orphan the previous print on disk
                # and renumber print_path on every re-upload. The idempotence key is the
                # ATTACHMENT: it has exactly one legacy print master, and this is it.
                path = str(await storage.replace_master(staged, old_print))
            else:
                path = str(await storage.place_master(staged, dest, name))
        else:  # legacy: deterministic print/{uid}{suffix} — may overwrite a prior print of this att
            path = await to_thread.run_sync(
                _save_print_sync, data_root / "print", uid, staged, suffix
            )
    except BaseException as exc:  # never leave the staged part behind — nothing else reaps it
        await to_thread.run_sync(staged.unlink, True)
        if isinstance(exc, up_svc.UploadTooLarge):
            raise HTTPException(
                status_code=413, detail=f"Exceeds {settings.MAX_UPLOAD_MIB} MiB"
            ) from None
        raise
    att.print_path = path
    att.print_mime = file.content_type or "application/octet-stream"
    att.print_size = size
    try:
        await db.commit()
    except Exception:
        # Only a memo-folder file placed under a brand-new UNIQUE (O_EXCL) name by THIS request is
        # safe to unlink on rollback. The legacy print/{uid}{suffix} path is deterministic and may
        # be a file another (committed) request owns — never unlink it (would break it). Nor the
        # in-place replacement: `path` is then the committed print_path, and removing it would
        # leave the attachment's print tier pointing at nothing.
        if memo is not None and (old_print is None or path != str(old_print)):
            await to_thread.run_sync(lambda: Path(path).unlink(missing_ok=True))
        raise
    # The previous print file is now unreferenced — the upload either renamed the print (a different
    # client filename) or moved it out of the legacy print/ dir into the memo folder. Delete it:
    # nothing else can point at it (every print master is either O_EXCL-reserved or the uid-keyed
    # legacy file of this one attachment), and keeping it would leak a full-size master per rename.
    # AFTER the commit, so the ordering is crash-safe in one direction: until the commit lands,
    # print_path still names the old file and that file still exists (the consistent OLD state);
    # once it lands, print_path names the new file, which is already on disk (the consistent NEW
    # state). A crash between the two leaves at worst an unreferenced file, never a dangling
    # print_path. Best-effort (`_unlink_all` swallows OSError): the upload is already committed, so
    # a failed cleanup must not turn a successful request into a 500.
    if old_print is not None and path != str(old_print):
        await to_thread.run_sync(_unlink_all, [old_print])
    return await attachment_meta(uid, user, db)


# --- SSE media status channel (poll-based; LISTEN/NOTIFY is a future enhancement) ---


def sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def media_event_stream(
    snapshot: Callable[[], Awaitable[list[dict]]],
    ready_meta: Callable[[list[str]], Awaitable[dict[str, dict]]],
    *,
    interval: float = 1.5,
) -> AsyncIterator[str]:
    """Yield SSE events as the user's attachments change; close when nothing is in-flight.
    `snapshot()` returns {uid,status,progress_pct} for the user's NON-ready attachments
    (uploading/pending/processing/failed). When a watched uid drops out of the snapshot it
    has become 'ready' → emit a synthetic ready event, carrying the metadata `ready_meta()`
    reads back for it. Unit-tested with both callables injected."""
    seen: dict[str, tuple] = {}
    watching: set[str] = set()
    while True:
        rows = await snapshot()
        present: set[str] = set()
        in_flight = False
        for row in rows:
            present.add(row["uid"])
            watching.add(row["uid"])
            cur = (row["status"], row["progress_pct"])
            if seen.get(row["uid"]) != cur:
                seen[row["uid"]] = cur
                yield sse_event(row)
            if row["status"] in ("uploading", "pending", "processing"):
                in_flight = True
        finished = sorted(watching - present)  # dropped from non-ready → it finished
        if finished:
            # The ready event is synthesised from an ABSENCE, so the row that now carries the
            # worker's width/height/blurhash is not in hand — it is read back here. Without it the
            # client learns only that the status changed and lays a fresh upload out in the
            # fallback box, with no srcset, until something refetches the feed.
            meta = await ready_meta(finished)
            for uid in finished:
                yield sse_event(
                    {"uid": uid, "status": "ready", "progress_pct": 100, **meta.get(uid, {})}
                )
                watching.discard(uid)
        if not in_flight:
            break
        await asyncio.sleep(interval)


async def _user_snapshot(user_id: int) -> list[dict]:
    """{uid,status,progress_pct} for this user's non-ready attachments — the stream's poll.

    progress_pct is ONE job's own percentage: the least-advanced job of that attachment that has
    not finished. It used to be max() across every job of the attachment, which reported a
    video's completed fast lane (poster/thumbs, done in seconds at 100) for the entire slow-lane
    transcode that followed — the client sat at 100% from the first second to the last (MEMNG-36).
    Finished and failed jobs are excluded rather than blended in: what the user is waiting for is
    the work still outstanding, and the least-advanced of those jobs is what the wait is bounded by.

    A running job's live percentage comes from media_job_progress, because the worker's own
    media_jobs row is locked and unreadable-through until the job commits (see MediaJobProgress).
    The job row's stored progress_pct is the fallback for a job with no sample yet (a queued one,
    at 0).
    """
    async with async_session() as db:
        active = (
            select(
                MediaJob.attachment_id.label("attachment_id"),
                func.min(func.coalesce(MediaJobProgress.progress_pct, MediaJob.progress_pct)).label(
                    "pct"
                ),
            )
            .join(MediaJobProgress, MediaJobProgress.job_id == MediaJob.id, isouter=True)
            .where(MediaJob.state.notin_((JobState.done, JobState.failed)))
            .group_by(MediaJob.attachment_id)
            .subquery()
        )
        stmt = (
            select(Attachment.uid, Attachment.status, func.coalesce(active.c.pct, 0))
            .join(active, active.c.attachment_id == Attachment.id, isouter=True)
            .where(Attachment.owner_id == user_id, Attachment.status != AttachmentStatus.ready)
        )
        rows = (await db.execute(stmt)).all()
    return [{"uid": uid, "status": status.value, "progress_pct": pct} for uid, status, pct in rows]


async def _ready_meta(user_id: int, uids: list[str]) -> dict[str, dict]:
    """The layout metadata a just-finished attachment gained, per uid.

    Scoped to the owner like every other read here, even though the uids came from this user's own
    snapshot — the stream must not become a way to read another account's rows if that ever changes.
    """
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    Attachment.uid, Attachment.width, Attachment.height, Attachment.blurhash
                ).where(Attachment.owner_id == user_id, Attachment.uid.in_(uids))
            )
        ).all()
    return {uid: {"width": w, "height": h, "blurhash": b} for uid, w, h, b in rows}


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    # Authenticate in a short-lived session and release it before the stream opens — otherwise the
    # SSE connection would pin a pool connection for its whole lifetime. Each poll inside
    # media_event_stream opens its own short session (_user_snapshot), so the stream holds none.
    async with async_session() as db:
        user = await auth.require_user(request, db)
    user_id = user.id

    async def snap() -> list[dict]:
        return await _user_snapshot(user_id)

    async def meta(uids: list[str]) -> dict[str, dict]:
        return await _ready_meta(user_id, uids)

    return StreamingResponse(media_event_stream(snap, meta), media_type="text/event-stream")
