from typing import Annotated, Any, Literal

from anyio import to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.config import settings
from app.database import get_session
from app.models import AttachmentStatus, Upload, User
from app.services import uploads as svc

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _upload_offset_header() -> dict[str, Any]:
    """A fresh dict per call: FastAPI merges `openapi_extra` into the operation by reference, so a
    shared constant would leave two operations aliasing the same node in the served document."""
    return {
        "Upload-Offset": {
            "description": "Bytes the server now holds for this upload session — the offset the "
            "next chunk must start at.",
            "schema": {"type": "integer"},
        }
    }


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
    captured_at: Rfc3339Datetime | None
    # What the FILE's own metadata said, as opposed to `captured_at`, which falls back to the
    # browser's modification time and then to the upload time and so always holds something
    # (MEMNG-262). `capture_civil` is a wall-clock reading with no zone — the client resolves it.
    capture_instant: Rfc3339Datetime | None
    capture_civil: str | None
    capture_source: str | None
    # Only ever set once the attachment belongs to a memo — this route answers a STANDALONE
    # finalize, which links nothing, so it is null here and carries the saved order in the memo
    # responses (`AttachmentRef`).
    position: int | None
    width: int | None
    height: int | None
    duration: float | None


class CaptureOut(BaseModel):
    """What the server read out of an upload's own metadata, and whether it has read it yet.

    `pending` means the read has not happened (the bytes are still arriving, or nothing has asked
    yet); `done` is a SETTLED answer even when all three values are null — a file with no capture
    date is an answer, not a reason for a composer to keep waiting.
    """

    state: Literal["pending", "done"]
    capture_instant: Rfc3339Datetime | None
    capture_civil: str | None
    capture_source: str | None


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


# The chunk arrives as a raw `request.stream()`, which FastAPI cannot see, so it declares no
# request body at all and a generated client has no way to send one (MEMNG-109). The
# offset-gap 409 is likewise invisible to FastAPI, and the Upload-Offset response header is
# set on the injected Response so resync clients can read the cursor without parsing JSON.
@router.patch(
    "/{upload_uid}",
    response_model=UploadOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        "responses": {
            "200": {"headers": _upload_offset_header()},
            "409": {
                "description": "Offset gap — re-sync with HEAD and resend from the offset it "
                "reports.",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"detail": {"type": "string"}},
                        }
                    }
                },
            },
        },
    },
)
async def append_chunk(
    upload_uid: str,
    request: Request,
    response: Response,
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
    response.headers["Upload-Offset"] = str(upload.bytes_received)
    return UploadOut(
        upload_uid=upload.upload_uid,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        bytes_received=upload.bytes_received,
    )


@router.head(
    "/{upload_uid}",
    openapi_extra={"responses": {"200": {"headers": _upload_offset_header()}}},
)
async def upload_offset(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    upload = await _get_upload(db, upload_uid, user.id)
    return Response(status_code=200, headers={"Upload-Offset": str(upload.bytes_received)})


@router.get("/{upload_uid}/capture", response_model=CaptureOut)
async def upload_capture(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CaptureOut:
    """What this upload's own metadata says about when it was captured (MEMNG-262).

    NON-CONSUMING and idempotent: it finalizes nothing, deletes nothing and moves nothing, so a
    composer may poll it while the person is still typing and then post the memo exactly as it
    would have without it. Owner-scoped through the same `_get_upload` as every other verb, so
    another owner's uid answers with the same 404 as one that never existed.

    It may still WRITE — once. `ensure_capture` performs the read the completing append normally
    does, for the two cases that append cannot cover: a session that finished before this feature
    shipped, and a zero-byte file, which is complete without an append at all. The row lock makes
    that exactly-once, and every later poll is a plain read.
    """
    upload = await _get_upload(db, upload_uid, user.id)
    settled = await svc.ensure_capture(db, upload)
    if settled is None:
        # Finalized (or reaped) while this read waited for the row: the session is gone, and a
        # gone session answers here exactly as an unknown uid does.
        raise HTTPException(status_code=404, detail="Upload not found")
    await db.commit()
    return CaptureOut(
        state="done" if settled.capture_state == "done" else "pending",
        capture_instant=settled.capture_instant,
        capture_civil=settled.capture_civil,
        capture_source=settled.capture_source,
    )


@router.post("/{upload_uid}/finalize", response_model=AttachmentOut)
async def finalize_upload(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentOut:
    upload = await _get_upload(db, upload_uid, user.id)
    # A dedup hit hands back an attachment that already holds these bytes, and its part file is
    # only reaped once the commit below makes the Upload row's deletion real. Unlinking it inside
    # finalize would destroy the client's only copy if this commit then failed, leaving a restored
    # upload row promising bytes that are gone (MEMNG-142).
    consumed: list[str] = []
    try:
        attachment = await svc.finalize(db, upload, owner_id=user.id, consumed_out=consumed)
    except svc.UploadsUnlinkable:
        # The session was consumed by another finalize while this one waited for its row. Same
        # answer as an unknown uid — because that is what it now is.
        raise HTTPException(status_code=404, detail="Upload not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await db.commit()
    if consumed:
        await to_thread.run_sync(svc.reap_consumed_sync, consumed)
    return AttachmentOut(
        uid=attachment.uid,
        filename=attachment.filename,
        mime=attachment.mime,
        size=attachment.size,
        status=attachment.status,
        captured_at=attachment.captured_at,
        capture_instant=attachment.capture_instant,
        capture_civil=attachment.capture_civil,
        capture_source=attachment.capture_source,
        position=attachment.position,
        width=attachment.width,
        height=attachment.height,
        duration=attachment.duration,
    )
