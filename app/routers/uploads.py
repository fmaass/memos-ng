from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import get_session
from app.models import AttachmentStatus, Upload, User
from app.services import uploads as svc

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


class UploadCreate(BaseModel):
    filename: str
    mime: str
    size: int
    last_modified: int | None = None  # client File.lastModified, epoch ms


class UploadOut(BaseModel):
    upload_uid: str
    chunk_size: int
    bytes_received: int


class AttachmentOut(BaseModel):
    uid: str
    filename: str
    mime: str
    size: int
    status: AttachmentStatus
    captured_at: datetime | None
    width: int | None
    height: int | None
    duration: float | None


async def _get_upload(db: AsyncSession, upload_uid: str, owner_id: int) -> Upload:
    """Resolve an upload session the caller OWNS. Scoping the lookup (rather than fetching then
    comparing) makes another user's session answer exactly as an unknown uid does — same 404, same
    body — so the endpoints disclose no existence. `owner_id IS NULL` rows predate the ownership
    column and match no owner, so they fail closed here and age out via reap_expired."""
    upload = (
        await db.execute(
            select(Upload).where(Upload.upload_uid == upload_uid, Upload.owner_id == owner_id)
        )
    ).scalar_one_or_none()
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return upload


@router.post("", response_model=UploadOut, status_code=201)
async def start_upload(
    body: UploadCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> UploadOut:
    if body.size > settings.MAX_UPLOAD_MIB * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"File exceeds the {settings.MAX_UPLOAD_MIB} MiB limit"
        )
    upload = await svc.create_upload(
        db,
        owner_id=user.id,
        filename=body.filename,
        mime=body.mime,
        size=body.size,
        last_modified=body.last_modified,
    )
    await db.commit()
    return UploadOut(
        upload_uid=upload.upload_uid,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        bytes_received=upload.bytes_received,
    )


@router.patch("/{upload_uid}", response_model=UploadOut)
async def append_chunk(
    upload_uid: str,
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    upload_offset: Annotated[int, Header(alias="Upload-Offset")] = 0,
) -> UploadOut:
    upload = await _get_upload(db, upload_uid, user.id)
    try:
        upload = await svc.append_stream(db, upload, upload_offset, request.stream())
    except svc.UploadTooLarge as exc:
        cap = settings.MAX_UPLOAD_MIB * 1024 * 1024
        detail = (
            f"Upload exceeds the {settings.MAX_UPLOAD_MIB} MiB limit"
            if exc.total > cap
            else f"Upload exceeds its declared size of {upload.declared_size} bytes"
        )
        raise HTTPException(status_code=413, detail=detail) from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Offset gap; re-sync via HEAD") from None
    await db.commit()
    return UploadOut(
        upload_uid=upload.upload_uid,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        bytes_received=upload.bytes_received,
    )


@router.head("/{upload_uid}")
async def upload_offset(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    upload = await _get_upload(db, upload_uid, user.id)
    return Response(status_code=200, headers={"Upload-Offset": str(upload.bytes_received)})


@router.post("/{upload_uid}/finalize", response_model=AttachmentOut)
async def finalize_upload(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentOut:
    upload = await _get_upload(db, upload_uid, user.id)
    try:
        attachment = await svc.finalize(db, upload, owner_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await db.commit()
    return AttachmentOut(
        uid=attachment.uid,
        filename=attachment.filename,
        mime=attachment.mime,
        size=attachment.size,
        status=attachment.status,
        captured_at=attachment.captured_at,
        width=attachment.width,
        height=attachment.height,
        duration=attachment.duration,
    )
