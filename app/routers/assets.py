import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
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
)
from app.services import asset_urls, poster, printready, storage, variants
from app.services import uploads as up_svc
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


def _derived_tier_path(uid: str, tier: str) -> tuple[Path, str] | None:
    """Derived tiers resolve from the uid alone (no DB row) — used by the signed fast path."""
    assets = Path(settings.DATA_ROOT) / "assets"
    if tier == "thumb":
        return assets / "thumbs" / f"{uid}.jpg", "image/jpeg"
    if tier == "mobile":
        return assets / "mobile" / f"{uid}.jpg", "image/jpeg"
    if tier == "web":
        return assets / "web" / f"{uid}.mp4", "video/mp4"
    if tier == "poster":
        return poster.poster_path(uid), poster.MIME
    if tier == printready.TIER:
        return printready.printready_path(uid), "image/jpeg"
    return None


def tier_path(attachment: Attachment, tier: str) -> tuple[Path, str] | None:
    """Resolve an attachment's file for a tier — public because the export builds its media
    payloads from the same map (app/routers/export.py); a second copy would drift the moment a
    rendition rung is added."""
    if tier == "original":
        return (Path(attachment.storage_path), attachment.mime) if attachment.storage_path else None
    if tier == "print":  # separately-uploaded print master (download-only); None if absent
        return (
            (Path(attachment.print_path), attachment.print_mime or "application/octet-stream")
            if attachment.print_path
            else None
        )
    return _derived_tier_path(attachment.uid, tier)


def _negotiated_path_sync(
    path: Path, media_type: str, want_webp: bool
) -> tuple[Path, str] | None:
    """Pick the pre-generated webp variant when it was negotiated AND exists, else confirm the
    original tier is there. None means 'nothing to serve' → the caller's 404.

    Both existence checks live in ONE off-loop hop: this is the highest-fan-out path in the app
    and the data volume is a bind-mounted share, so every stat is a network round-trip. (A webp
    hit needs a single stat here — the old code re-stat'd the file it had just found.)"""
    if want_webp:
        webp = path.with_suffix(".webp")
        if webp.exists():
            return webp, "image/webp"
    if not path.exists():
        return None
    return path, media_type


async def _serve_file(
    path: Path, media_type: str, request: Request, format: str | None, cache_control: str
) -> FileResponse:
    # Prefer a pre-generated webp variant for image tiers when asked (falls back to JPEG if absent).
    want_webp = media_type == "image/jpeg" and (
        format == "webp" or "image/webp" in request.headers.get("accept", "")
    )
    resolved = await to_thread.run_sync(_negotiated_path_sync, path, media_type, want_webp)
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
    status: AttachmentStatus
    width: int | None
    height: int | None
    duration: float | None
    blurhash: str | None
    captured_at: datetime | None
    mine: bool
    hasPrint: bool  # has a print master of any kind (legacy print_path OR a rendition)
    hasLegacyPrint: bool  # has the legacy single print_path → the /print download button applies
    # Whether `posterUrl` has a file behind it. Always false for anything that is not a video (no
    # other kind of attachment has a poster tier); for a video it is true once EITHER the fast lane
    # extracted a hero frame OR the post supplied its own poster (MemoCreate.video_posters) — which
    # is what makes it assertable straight after a create, before the worker has run at all.
    hasPoster: bool
    renditions: list[RenditionItem]
    downloadUrl: str
    printUrl: str
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    posterUrl: str


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
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    posterUrl: str
    downloadUrl: str
    printUrl: str


def _list_item(att: Attachment, user_id: int) -> AttachmentListItem:
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
        **asset_urls.signed_tier_urls(att.uid, user_id, now=time.time()),
        downloadUrl=f"{base}/original",
        printUrl=f"{base}/print",
    )


@router.get("/attachments", response_model=list[AttachmentListItem])
async def list_attachments(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    type: str | None = None,
    limit: int = 200,
) -> list[AttachmentListItem]:
    """The household media library the user can see, newest first: their own attachments
    (including orphans) PLUS media on any memo visible to them (non-PRIVATE). ?type=image|video
    filters by mime prefix; `orphan` flags media not attached to any memo. Another user's PRIVATE
    or orphan media is excluded — mirrors `_viewable` / memo visibility."""
    stmt = (
        select(Attachment)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        # own (incl. orphans, where Memo is NULL) OR on a memo the user can see (non-PRIVATE)
        .where((Attachment.owner_id == user.id) | visible_clause(user))
    )
    if type == "image":
        stmt = stmt.where(Attachment.mime.like("image/%"))
    elif type == "video":
        stmt = stmt.where(Attachment.mime.like("video/%"))
    stmt = stmt.order_by(Attachment.id.desc()).limit(max(1, min(limit, 500)))
    rows = (await db.execute(stmt)).scalars().all()
    return [_list_item(a, user.id) for a in rows]


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
    return AttachmentMeta(
        uid=att.uid,
        mime=att.mime,
        size=att.size,
        status=att.status,
        width=att.width,
        height=att.height,
        duration=att.duration,
        blurhash=att.blurhash,
        captured_at=att.captured_at,
        mine=att.owner_id == user.id,
        hasPrint=has_print(att),
        hasLegacyPrint=att.print_path is not None,
        hasPoster=has_poster,
        renditions=rendition_items(att),
        downloadUrl=f"{base}/original",
        printUrl=f"{base}/print",
        **asset_urls.signed_tier_urls(att.uid, user.id, now=time.time()),
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


@router.post("/attachments/{uid}/renditions", response_model=AttachmentMeta, status_code=201)
async def attach_rendition(
    uid: str,
    body: RenditionAttach,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentMeta:
    """Attach — or replace — the print master of an OWNED image, from a finished resumable upload.

    Takes an upload_uid rather than a multipart body so a multi-hundred-MB master gets the chunked,
    resumable, progress-reporting path every other upload in this app uses. The legacy multipart
    `/print` route, which writes the separate print_path column, is untouched (MEMNG-51 retires it).
    """
    variant_key, label = _print_variant_key(body.variant_key)
    # Take the attachment's row lock FIRST and read its rendition state THROUGH it. The placement
    # below decides from the master this photo currently has, and the post-commit sweep unlinks the
    # file that decision displaced — so a request that WAITED on this lock must not decide from the
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
    if att is None or att.owner_id != user.id:  # OWNER-only: a mutation, unlike viewing
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not att.mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Print masters are for images only")
    # Scoped to the caller like every other upload verb, so another user's session answers exactly
    # as an unknown uid does.
    upload = (
        await db.execute(
            select(Upload).where(Upload.upload_uid == body.upload_uid, Upload.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
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
        resolved = _derived_tier_path(uid, tier)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Unknown tier")
        # Cap the browser cache to the signature's remaining life so a now-revoked (e.g.
        # memo-gone-PRIVATE) tier can't be replayed from cache far past exp — bounds the mint-time-
        # auth staleness window (ADR-0009). `exp` already passed verify_asset_sig (numeric, future),
        # so remaining is > 0 and at most ASSET_URL_TTL. (`exp is not None` here for the type
        # checker; verify_asset_sig already rejected a None/non-numeric exp.)
        remaining = (int(exp) - int(time.time())) if exp is not None else settings.ASSET_URL_TTL
        cache = f"private, max-age={max(1, remaining)}, immutable"
        return await _serve_file(resolved[0], resolved[1], request, format, cache)
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
    return await _serve_file(resolved[0], resolved[1], request, format, cache)


def _all_asset_paths(att: Attachment) -> list[Path]:
    """Every on-disk file for an attachment: the original + each derived tier (+ webp variant)."""
    paths: list[Path] = []
    if att.storage_path:
        paths.append(Path(att.storage_path))
    if att.print_path:
        paths.append(Path(att.print_path))
    for rendition in att.renditions:  # hidden print masters (unique, unshared files)
        paths.append(Path(rendition.storage_path))
    for tier in ("thumb", "mobile", "web", "poster", printready.TIER):
        resolved = tier_path(att, tier)
        if resolved is not None:
            p = resolved[0]
            paths.append(p)
            paths.append(p.with_suffix(".webp"))  # format-negotiated variant (may not exist)
    return paths


def _unlink_all(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort; the DB row is already gone


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
    # This request NEVER waits on a running media job. A worker claims a job with SELECT … FOR
    # UPDATE and holds that row lock until the job commits — for a slow-lane transcode, the whole
    # run (up to SLOW_TRANSCODE_TIMEOUT_S). Both statements this used to issue queued behind that
    # lock: the plain `DELETE FROM media_jobs WHERE attachment_id = …`, and the attachment delete
    # itself through the old FK's ON DELETE CASCADE. Either one pinned a request-serving
    # connection for the transcode's duration, and a handful of those exhaust DB_POOL_SIZE +
    # overflow on the shared Postgres (MEMNG-41). So the FK is gone (see MediaJob) and the job
    # cleanup below takes only the rows it can lock immediately; the in-flight row belongs to its
    # worker, which discards job and partial output on finding the attachment gone
    # (media.AttachmentGone → worker.run_once).
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None or att.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    paths = _all_asset_paths(att)
    unlocked_jobs = (
        select(MediaJob.id)
        .where(MediaJob.attachment_id == att.id)
        .with_for_update(skip_locked=True)
    )
    await db.execute(
        sa_delete(MediaJob)
        .where(MediaJob.id.in_(unlocked_jobs))
        .execution_options(synchronize_session=False)
    )
    await db.delete(att)
    await db.commit()
    # After the commit, so a worker that published a derived tier while this request was in flight
    # published it BEFORE the row vanished (its publish holds the attachment row lock, which this
    # delete waits on for those milliseconds) — and `paths` covers every uid-derived tier, so the
    # sweep below removes it. Nothing the worker writes after this point survives: it re-reads the
    # row under that lock and discards its output when the row is gone.
    await to_thread.run_sync(_unlink_all, paths)


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
    export — see ADR web/print renditions). The web rendition keeps displaying; this file is
    hidden from the feed and download-only via the `print` tier."""
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
        size = await up_svc.spool_stream(
            _file_chunks(file), staged, start=0, ceiling=settings.MAX_UPLOAD_MIB * 1024 * 1024
        )
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
