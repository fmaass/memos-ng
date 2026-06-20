import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import async_session, get_session
from app.models import Attachment, AttachmentStatus, MediaJob, Memo, User
from app.services.memos import visible_clause

router = APIRouter(prefix="/api", tags=["assets"])

TIERS = ("original", "thumb", "mobile", "web", "poster")


def _tier_path(attachment: Attachment, tier: str) -> tuple[Path, str] | None:
    assets = Path(settings.DATA_ROOT) / "assets"
    uid = attachment.uid
    if tier == "original":
        return (Path(attachment.storage_path), attachment.mime) if attachment.storage_path else None
    if tier == "thumb":
        return assets / "thumbs" / f"{uid}.jpg", "image/jpeg"
    if tier == "mobile":
        return assets / "mobile" / f"{uid}.jpg", "image/jpeg"
    if tier == "web":
        return assets / "web" / f"{uid}.mp4", "video/mp4"
    if tier == "poster":
        return assets / "web" / f"{uid}.poster.jpg", "image/jpeg"
    return None


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
    downloadUrl: str
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
    orphan: bool  # not linked to a memo
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    posterUrl: str
    downloadUrl: str


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
        orphan=att.memo_id is None,
        thumbnailUrl=f"{base}/thumb",
        previewUrl=f"{base}/mobile",
        webUrl=f"{base}/web",
        posterUrl=f"{base}/poster",
        downloadUrl=f"{base}/original",
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
        downloadUrl=f"{base}/original",
        thumbnailUrl=f"{base}/thumb",
        previewUrl=f"{base}/mobile",
        webUrl=f"{base}/web",
        posterUrl=f"{base}/poster",
    )


@router.get("/attachments/{uid}/{tier}")
async def serve_asset(
    uid: str,
    tier: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
    format: str | None = None,
) -> FileResponse:
    if tier not in TIERS:
        raise HTTPException(status_code=404, detail="Unknown tier")
    att = await _viewable(db, user, uid)
    resolved = _tier_path(att, tier)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown tier")
    path, media_type = resolved
    # Format negotiation: prefer a pre-generated webp variant for image tiers when asked.
    if media_type == "image/jpeg" and (
        format == "webp" or "image/webp" in request.headers.get("accept", "")
    ):
        webp = path.with_suffix(".webp")
        if webp.exists():
            path, media_type = webp, "image/webp"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset not generated yet")
    # FileResponse adds ETag + Last-Modified (mtime == captured_at for derived tiers) +
    # Accept-Ranges, and answers Range requests with 206 automatically. Tiers are uid-keyed and
    # immutable, so cache hard — repeat feed/gallery renders then hit the browser cache, not the
    # server (cuts request fan-out + DB connection pressure).
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": f"public, max-age={settings.ASSET_CACHE_SECONDS}, immutable"},
    )


def _all_asset_paths(att: Attachment) -> list[Path]:
    """Every on-disk file for an attachment: the original + each derived tier (+ webp variant)."""
    paths: list[Path] = []
    if att.storage_path:
        paths.append(Path(att.storage_path))
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


# --- SSE media status channel (poll-based; LISTEN/NOTIFY is a future enhancement) ---


def sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def media_event_stream(
    snapshot: Callable[[], Awaitable[list[dict]]], *, interval: float = 1.5
) -> AsyncIterator[str]:
    """Yield SSE events as the user's attachments change; close when nothing is in-flight.
    `snapshot()` returns {uid,status,progress_pct} for the user's NON-ready attachments
    (uploading/pending/processing/failed). When a watched uid drops out of the snapshot it
    has become 'ready' → emit a synthetic ready event. Unit-tested with an injected snapshot."""
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
        for uid in list(watching - present):  # dropped from non-ready → it finished
            yield sse_event({"uid": uid, "status": "ready", "progress_pct": 100})
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


@router.get("/events")
async def events(user: Annotated[User, Depends(auth.require_user)]) -> StreamingResponse:
    async def snap() -> list[dict]:
        return await _user_snapshot(user.id)

    return StreamingResponse(media_event_stream(snap), media_type="text/event-stream")
