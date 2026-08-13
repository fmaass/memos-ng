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
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import async_session, get_session
from app.models import Attachment, AttachmentStatus, MediaJob, Memo, User
from app.services import asset_urls, storage
from app.services import uploads as up_svc
from app.services.memos import visible_clause

router = APIRouter(prefix="/api", tags=["assets"])

TIERS = ("original", "thumb", "mobile", "web", "poster", "print")


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
        return assets / "web" / f"{uid}.poster.jpg", "image/jpeg"
    return None


def _tier_path(attachment: Attachment, tier: str) -> tuple[Path, str] | None:
    if tier == "original":
        return (Path(attachment.storage_path), attachment.mime) if attachment.storage_path else None
    if tier == "print":  # separately-uploaded print master (download-only); None if absent
        return (
            (Path(attachment.print_path), attachment.print_mime or "application/octet-stream")
            if attachment.print_path
            else None
        )
    return _derived_tier_path(attachment.uid, tier)


def _serve_file(
    path: Path, media_type: str, request: Request, format: str | None, cache_control: str
) -> FileResponse:
    # Prefer a pre-generated webp variant for image tiers when asked (falls back to JPEG if absent).
    if media_type == "image/jpeg" and (
        format == "webp" or "image/webp" in request.headers.get("accept", "")
    ):
        webp = path.with_suffix(".webp")
        if webp.exists():
            path, media_type = webp, "image/webp"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset not generated yet")
    # Cache-Control is `private` (never `public`): these responses are either signature-scoped or
    # auth-gated to one viewer, so a shared/intermediary cache must not store and replay them.
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": cache_control})


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


@router.get("/attachments/{uid}", response_model=AttachmentMeta)
async def attachment_meta(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentMeta:
    att = await _viewable(db, user, uid)
    base = f"/api/attachments/{uid}"
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
        renditions=rendition_items(att),
        downloadUrl=f"{base}/original",
        printUrl=f"{base}/print",
        **asset_urls.signed_tier_urls(att.uid, user.id, now=time.time()),
    )


@router.get("/attachments/{uid}/renditions", response_model=list[RenditionItem])
async def list_renditions(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[RenditionItem]:
    """A photo's hidden print renditions (household-visible, like the legacy print tier). Registered
    BEFORE the generic /{uid}/{tier} route so the literal `renditions` segment isn't a tier."""
    att = await _viewable(db, user, uid)
    return rendition_items(att)


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
    return _serve_file(Path(resolved[0]), resolved[1], request, None, cache)


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
        return _serve_file(resolved[0], resolved[1], request, format, cache)
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
        resolved = _tier_path(att, tier)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown tier")
    # immutable uid-keyed bytes, but private (one authed viewer) — never cached by a shared proxy.
    cache = f"private, max-age={settings.ASSET_CACHE_SECONDS}, immutable"
    return _serve_file(resolved[0], resolved[1], request, format, cache)


def _all_asset_paths(att: Attachment) -> list[Path]:
    """Every on-disk file for an attachment: the original + each derived tier (+ webp variant)."""
    paths: list[Path] = []
    if att.storage_path:
        paths.append(Path(att.storage_path))
    if att.print_path:
        paths.append(Path(att.print_path))
    for rendition in att.renditions:  # hidden print masters (unique, unshared files)
        paths.append(Path(rendition.storage_path))
    for tier in ("thumb", "mobile", "web", "poster"):
        resolved = _tier_path(att, tier)
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
    att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one_or_none()
    if att is None or att.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    paths = _all_asset_paths(att)
    await db.execute(sa_delete(MediaJob).where(MediaJob.attachment_id == att.id))
    await db.delete(att)
    await db.commit()
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
        # A print uploaded to an attachment already in a memo lands in that memo's folder under its
        # real filename (beside the screen); an unlinked attachment keeps the legacy print/{uid}.
        memo = (
            (await db.execute(select(Memo).where(Memo.id == att.memo_id))).scalar_one_or_none()
            if att.memo_id is not None
            else None
        )
        if memo is not None:
            dest = storage.memo_dir(data_root, memo.created_at, memo.uid)
            path = str(
                await storage.place_master(staged, dest, file.filename or f"{uid}-print{suffix}")
            )
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
        # Only the memo-folder file is a brand-new UNIQUE (O_EXCL) name this request created, so
        # it's safe to unlink on rollback. The legacy print/{uid}{suffix} path is deterministic and
        # may be a file another (committed) request owns — never unlink it (would break it).
        if memo is not None:
            await to_thread.run_sync(lambda: Path(path).unlink(missing_ok=True))
        raise
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
    async with async_session() as db:
        progress = (
            select(MediaJob.attachment_id, func.max(MediaJob.progress_pct).label("pct"))
            .group_by(MediaJob.attachment_id)
            .subquery()
        )
        stmt = (
            select(Attachment.uid, Attachment.status, func.coalesce(progress.c.pct, 0))
            .join(progress, progress.c.attachment_id == Attachment.id, isouter=True)
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
