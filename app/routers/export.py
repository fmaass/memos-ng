import asyncio
import io
import json
import zipfile
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import User, Visibility
from app.services import memos as svc

router = APIRouter(prefix="/api", tags=["export"])

EXPORT_MAX = 5000
_sem = asyncio.Semaphore(2)  # cap concurrent exports


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
) -> StreamingResponse:
    # NOTE (Phase 2 / T2.4): a `tier` param (original|web|thumbnail) and per-file mtimes
    # from attachment.captured_at get wired once attachments exist. For now we export the
    # memo text; each zip entry's mtime is the memo's created_at (the captured_at analogue).
    async def query(limit: int):
        return await svc.list_memos(
            db,
            user,
            created_before=created_before,
            created_after=created_after,
            tags=tag,
            visibility=visibility,
            owner=owner,
            q=q,
            limit=limit,
            with_total=True,  # export gates on the count, so it must be computed
        )

    # Acquire the concurrency cap BEFORE the (potentially large) query + materialize + zip,
    # so concurrent exports can't each pull EXPORT_MAX rows into memory at once.
    async with _sem:
        _, _, total = await query(1)
        assert total is not None  # with_total=True above guarantees a count
        if total > EXPORT_MAX:
            raise HTTPException(
                status_code=413,
                detail=f"Export too large ({total} memos > {EXPORT_MAX}). Narrow it with filters.",
            )
        items, _, _ = await query(EXPORT_MAX)

        buf = io.BytesIO()
        manifest = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for memo in items:
                info = zipfile.ZipInfo(f"{memo.uid}.md", date_time=memo.created_at.timetuple()[:6])
                zf.writestr(info, memo.content)
                manifest.append(
                    {
                        "uid": memo.uid,
                        "created_at": memo.created_at.isoformat(),
                        "visibility": memo.visibility.value,
                        "tags": sorted(t.name for t in memo.tags),
                    }
                )
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        buf.seek(0)

    def chunks():
        while data := buf.read(65536):
            yield data

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=memos-export.zip"},
    )
