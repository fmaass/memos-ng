import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import Attachment, Memo, User, Visibility
from app.services import asset_paths, storage
from app.services import export as export_svc
from app.services import export_settings as export_settings_svc
from app.services import memos as svc
from app.services import photobook as photobook_svc

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
        resolved = asset_paths.tier_path(att, tier)
        path = resolved[0] if resolved is not None else None
        name = _unique_name(used, _media_name(att, path))
        files.append(
            export_svc.MediaFile(
                uid=att.uid,
                arcname=f"media/{memo.uid}/{name}",
                path=path,
                mtime=att.captured_at,
            )
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
    visibility: Visibility | SkipJsonSchema[None] = None,  # see list_memos
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


class PhotobookPhotoOut(BaseModel):
    """One photo's page in the book. `width`/`height` are the print rendition's real pixels once it
    exists and a projection from the source until then — `projected` says which."""

    sequence: int
    attachment: str
    memo: str
    memo_date: date
    filename: str
    source: photobook_svc.SourceKind
    status: photobook_svc.Readiness
    width: int | None
    height: int | None
    projected: bool
    effective_dpi: float | None
    meets_dpi: bool | None


class PhotobookTotalsOut(BaseModel):
    selected: int
    ready: int
    pending: int
    failed: int
    unavailable: int


class PhotobookManifestOut(BaseModel):
    """The book's manifest: the question that was asked, the tally, and every photo in it."""

    # The RESOLVED profile key, not an enum: the admissible set is two keys that depend on the
    # deployment's current export settings (app/services/export_settings.py), so freezing it into
    # the schema would publish a contract the server stops honouring the moment they change.
    profile: str
    tag: str | None  # null = the untagged selection: every image memo in the range
    page_cm: str
    max_pages: float
    dpi: int
    totals: PhotobookTotalsOut
    photos: list[PhotobookPhotoOut]


@router.get("/exports/photobook", response_model=PhotobookManifestOut)
async def export_photobook(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    tag: str | None = None,
    profile: str | None = None,
    format: Literal["manifest", "zip"] = "manifest",
    page_cm: str = photobook_svc.DEFAULT_PAGE_CM,
    max_pages: float = photobook_svc.DEFAULT_MAX_PAGES,
    dpi: int = photobook_svc.DEFAULT_DPI,
) -> Response:
    """The photo book for a date range: a print-readiness manifest, or the archive itself.

    Selection is the owner's image attachments in memo-date order; `from`/`to` bound the memo date
    and are inclusive whole days. `tag` NARROWS that to the memos carrying it — omitting it selects
    every image memo in the range, which is what "give me all my photos between two dates" means.

    `profile` names the print profile the book is derived at. Omitting it follows this
    deployment's export settings, so an admin changing the print profile changes what an export
    produces without any client being told; naming one pins the request to it. Two keys are
    accepted — the permanent default `print-4096`, whose renditions are the shipped tier, and
    whatever the settings currently name; anything else is 422. A non-default profile derives into
    its own cache directory, so requesting one never rewrites or invalidates the default tier.

    **Requesting a book schedules it.** Both formats enqueue a print rendition for every member
    that lacks a current one, so the first request starts the work and later requests report on it;
    the enqueue is idempotent, so polling costs nothing.

    `format=manifest` (the default) answers with one record per photo — the name it will carry in
    the archive, whether its rendition is `ready`/`pending`/`failed`/`unavailable`, its pixel size,
    and the **effective dpi** it will print at for the given `page_cm`/`max_pages` geometry, judged
    against `dpi`. That is the field worth having: it names the photos a layout program will flag,
    before the import rather than after it. Re-fetching it is also how progress is followed.

    `format=zip` streams the ready renditions as `0001_<date>_<uid>.jpg`, numbered in book order so
    a folder import lands in sequence. Photos that are not ready yet are left out and recorded in
    the archive's manifest.json, exactly as the generic export treats a rendition that is missing.
    """
    try:
        geom = photobook_svc.geometry(page_cm, max_pages, dpi)
    except photobook_svc.GeometryError as exc:
        # 422, not 400: these are malformed query parameters, so they answer in the same shape as
        # the ones FastAPI rejects itself (a bad `format`).
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        resolved = await export_settings_svc.resolve_print_profile(db, profile)
    except export_settings_svc.UnknownProfile as exc:
        # Same 422 family as the geometry above — a query parameter naming something that is not a
        # profile. It used to be FastAPI's own enum rejection; the admissible set now depends on
        # the settings row, so the check moved into the handler and the status code did not.
        raise HTTPException(
            status_code=422, detail=f"unknown print profile {profile!r}"
        ) from exc
    except export_settings_svc.SettingsMissing as exc:
        raise HTTPException(status_code=500, detail="export settings row is missing") from exc

    book = await photobook_svc.build(
        db, user.id, tag=tag, date_from=date_from, date_to=date_to, geom=geom, profile=resolved
    )
    if format == "manifest":
        # Validated against the declared model rather than passed through: the service owns the
        # manifest's shape, this is the schema the frontend's types are generated from, and
        # validating here is what stops the two drifting silently.
        payload = photobook_svc.manifest(book, page_cm=page_cm, max_pages=max_pages, dpi=dpi)
        return JSONResponse(PhotobookManifestOut.model_validate(payload).model_dump(mode="json"))
    return StreamingResponse(
        # Same concurrency cap and same archive writer as the generic export — a book is a large
        # sequential read off the same share, and there is one zip implementation in this codebase.
        _capped(photobook_svc.archive_items(book)),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=photobook.zip"},
    )
