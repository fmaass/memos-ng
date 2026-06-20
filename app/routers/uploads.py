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


async def _get_upload(db: AsyncSession, upload_uid: str) -> Upload:
    upload = (
        await db.execute(select(Upload).where(Upload.upload_uid == upload_uid))
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
    upload = await svc.create_upload(
        db, filename=body.filename, mime=body.mime, size=body.size, last_modified=body.last_modified
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
    upload = await _get_upload(db, upload_uid)
    data = await request.body()
    try:
        upload = await svc.append_chunk(db, upload, upload_offset, data)
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
    upload = await _get_upload(db, upload_uid)
    return Response(status_code=200, headers={"Upload-Offset": str(upload.bytes_received)})


@router.post("/{upload_uid}/finalize", response_model=AttachmentOut)
async def finalize_upload(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentOut:
    upload = await _get_upload(db, upload_uid)
    try:
        attachment = await svc.finalize(db, upload)
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
