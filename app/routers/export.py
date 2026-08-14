import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import Attachment, Memo, User, Visibility
from app.routers import assets
from app.services import export as export_svc
from app.services import memos as svc
from app.services import storage

router = APIRouter(prefix="/api", tags=["export"])

BATCH_SIZE = 200  # rows pulled per DB round-trip; the export never holds more than one batch
_sem = asyncio.Semaphore(2)  # cap concurrent exports

# The tier keys the asset routes serve — an export carries the same renditions the app does.
ExportTier = Literal["original", "thumb", "mobile", "web", "poster", "print"]


def _media_name(att: Attachment, path: Path | None) -> str:
    """The attachment's own filename, carrying the tier file's real extension: the `thumb` of an
    .heic original is a JPEG, and naming it .heic would hand the user a file nothing opens."""
    name = storage.sanitize_name(att.filename)
    if path is None or path.suffix.lower() == Path(name).suffix.lower():
        return name
    return Path(name).stem + path.suffix


def _unique_name(used: set[str], name: str) -> str:
    """Two attachments on one memo can carry the same client filename, and the archive needs
    distinct entries — so a collision bumps to `<stem> (2)<suffix>`, the same form the on-disk
    placement (storage._reserve) gives it."""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{stem} ({n}){suffix}"
        n += 1
    used.add(candidate)
    return candidate


def _media_files(memo: Memo, tier: str) -> tuple[export_svc.MediaFile, ...]:
    """This memo's attachments as payloads in the requested tier, under `media/<memo-uid>/`.

    Media lands in its own subtree so the text layout at the archive root is untouched. Every
    attachment yields an entry even when the tier resolves to nothing for it — the manifest then
    records it as missing, which is the only signal the user gets that a rendition wasn't ready.
    """
    used: set[str] = set()
    files = []
    for att in memo.attachments:
        resolved = assets.tier_path(att, tier)
        path = resolved[0] if resolved is not None else None
        name = _unique_name(used, _media_name(att, path))
        files.append(
            export_svc.MediaFile(uid=att.uid, arcname=f"media/{memo.uid}/{name}", path=path)
        )
    return tuple(files)


def _export_item(memo: Memo, *, include_media: bool, tier: str) -> export_svc.ExportItem:
    return export_svc.ExportItem(
        arcname=f"{memo.uid}.md",
        # each zip entry's mtime is the memo's created_at (the captured_at analogue)
        mtime=memo.created_at,
        text=memo.content,
        manifest={
            "uid": memo.uid,
            "created_at": memo.created_at.isoformat(),
            "visibility": memo.visibility.value,
            "tags": sorted(t.name for t in memo.tags),
        },
        media=_media_files(memo, tier) if include_media else (),
    )


async def _export_items(
    db: AsyncSession,
    user: User,
    *,
    created_before: datetime | None,
    created_after: datetime | None,
    tag: list[str] | None,
    visibility: Visibility | None,
    owner: str | None,
    q: str | None,
    include_media: bool,
    tier: str,
) -> AsyncIterator[export_svc.ExportItem]:
    """Walk the matching memos in cursor-paged batches.

    Holding one batch of rows instead of the whole result set is what replaces the old fixed
    `EXPORT_MAX` cap: an export of any size now costs the same memory.
    """
    cursor: str | None = None
    while True:
        memos, cursor, _ = await svc.list_memos(
            db,
            user,
            created_before=created_before,
            created_after=created_after,
            tags=tag,
            visibility=visibility,
            owner=owner,
            q=q,
            limit=BATCH_SIZE,
            cursor=cursor,
            # Nothing gates on a count any more (the 413 is gone), and that COUNT was the single
            # most expensive part of the query — it scanned every visible memo, once per batch.
            with_total=False,
        )
        for memo in memos:
            yield _export_item(memo, include_media=include_media, tier=tier)
        # Without this the session's identity map keeps every row (and its selectin-loaded tags
        # and attachments) the export has ever read. `user` is detached along with them and
        # stays usable — it is fully loaded and this session never commits again
        # (expire_on_commit=False).
        db.expunge_all()
        if cursor is None:
            break


async def _capped(items: AsyncIterator[export_svc.ExportItem]) -> AsyncIterator[bytes]:
    """Hold the concurrency cap for the WHOLE stream.

    It used to guard a handler that materialized the archive before returning; with the work
    moved into the response body, acquiring it in the handler would release it the instant the
    handler returned and let any number of exports run at once. What it rations now is not
    memory (no export can blow that up any more) but the DB connection and the disk read
    bandwidth each concurrent export holds for its duration.
    """
    async with _sem:
        async for chunk in export_svc.stream_zip(items):
            yield chunk


@router.get("/export")
async def export_memos(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    created_before: datetime | None = None,
    created_after: datetime | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    visibility: Visibility | None = None,
    owner: str | None = None,
    q: str | None = None,
    include_media: bool = False,
    tier: ExportTier = "original",
) -> StreamingResponse:
    """Stream the matching memos as a zip: `<memo-uid>.md` per memo plus a manifest.

    With `include_media`, each memo's attachments are carried alongside its text under
    `media/<memo-uid>/`, in the rendition the `tier` names (the same tier keys the asset routes
    serve). A rendition that isn't on disk is skipped and noted in the manifest.
    """
    items = _export_items(
        db,
        user,
        created_before=created_before,
        created_after=created_after,
        tag=tag,
        visibility=visibility,
        owner=owner,
        q=q,
        include_media=include_media,
        tier=tier,
    )
    return StreamingResponse(
        _capped(items),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=memos-export.zip"},
    )
