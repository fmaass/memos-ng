import time
from typing import Annotated, Any, Literal

import structlog
from anyio import to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.config import settings
from app.database import get_session
from app.models import AttachmentStatus, Upload, User
from app.services import upload_metrics
from app.services import uploads as svc

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
logger = structlog.get_logger(__name__)


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


def _retry_after_header() -> dict[str, Any]:
    return {
        "Retry-After": {
            "description": "Seconds to wait before sending the request again.",
            "schema": {"type": "integer"},
        }
    }


class BodyAdmission:
    """How many upload bodies are streaming right now, per user and in total."""

    def __init__(self) -> None:
        self.per_user: dict[int, int] = {}
        self.total = 0

    def admit(self, user_id: int) -> bool:
        # No lock: every request runs on the one event loop, and nothing between the check and the
        # increment awaits, so no other request can interleave there.
        held = self.per_user.get(user_id, 0)
        if held >= settings.UPLOAD_BODIES_PER_USER:
            upload_metrics.count_rejection("user")
            return False
        if self.total >= settings.UPLOAD_BODIES_PER_PROCESS:
            upload_metrics.count_rejection("process")
            return False
        self.per_user[user_id] = held + 1
        self.total += 1
        return True

    def release(self, user_id: int) -> None:
        held = self.per_user[user_id] - 1
        if held:
            self.per_user[user_id] = held
        else:
            del self.per_user[user_id]
        self.total -= 1


body_admission = BodyAdmission()


class UploadCreate(BaseModel):
    filename: str
    mime: str
    size: int
    last_modified: int | None = None  # client File.lastModified, epoch ms


class UploadOut(BaseModel):
    upload_uid: str
    chunk_size: int = Field(
        description="A hint for clients that chunk; the body of a PATCH may be any length from "
        "1 byte to the bytes remaining."
    )
    bytes_received: int = Field(
        description="Bytes the server holds for this upload session — the offset the next PATCH "
        "must start at."
    )


class UploadLimitsOut(BaseModel):
    max_bytes: int = Field(
        description="The largest `size` POST /api/uploads accepts; anything larger is answered 413."
    )
    wifi_recommended_above_bytes: int = Field(
        description="Advisory: above this size a client should wait for Wi-Fi by default. The "
        "server enforces nothing at it."
    )
    bodies_per_user: int = Field(
        description="Upload bodies one user may stream at once; one more PATCH is answered 429."
    )


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
    # Also an upload-only capture grant (MEMNG-938); nothing else in this router takes one.
    user: Annotated[User, Depends(auth.require_upload_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> UploadOut:
    if body.size > settings.MAX_UPLOAD_MIB * 1024 * 1024:
        upload_metrics.count_size_rejection()
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


# Declared before every `/{upload_uid}` route: Starlette matches in declaration order, so a
# parametric single-segment route declared first would read `limits` as an upload uid.
@router.get("/limits", response_model=UploadLimitsOut)
async def upload_limits(_: Annotated[User, Depends(auth.require_user)]) -> UploadLimitsOut:
    return UploadLimitsOut(
        max_bytes=settings.MAX_UPLOAD_MIB * 1024 * 1024,
        wifi_recommended_above_bytes=settings.UPLOAD_WIFI_RECOMMENDED_MIB * 1024 * 1024,
        bodies_per_user=settings.UPLOAD_BODIES_PER_USER,
    )


# The chunk arrives as a raw `request.stream()`, which FastAPI cannot see, so it declares no
# request body at all and a generated client has no way to send one (MEMNG-109). The
# offset-gap 409 is likewise invisible to FastAPI, and the Upload-Offset response header is
# set on the injected Response so resync clients can read the cursor without parsing JSON.
@router.patch(
    "/{upload_uid}",
    response_model=UploadOut,
    description="Append bytes at `Upload-Offset`. The body may be any length from 1 byte to the "
    "bytes remaining; `chunk_size` is a hint for clients that chunk. Bytes received before a "
    "disconnect are kept, and HEAD reports the offset they reached.",
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
            "429": {
                "description": "Too many uploads are streaming at once, for this account or for "
                "the server. The body was not read and nothing was stored; resend the same "
                "request after Retry-After seconds.",
                "headers": _retry_after_header(),
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
    # Also an upload-only capture grant (MEMNG-938); nothing else in this router takes one.
    user: Annotated[User, Depends(auth.require_upload_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    upload_offset: Annotated[int, Header(alias="Upload-Offset")] = 0,
) -> UploadOut:
    upload = await _get_upload(db, upload_uid, user.id)
    # End the lookup transaction before the body arrives: left open, it pins a pooled connection
    # for as long as the client takes to send the bytes, and a few slow uploads drain the pool for
    # every request (MEMNG-621). Commit, not rollback — `expire_on_commit=False` keeps `upload`
    # readable for append_stream's snapshot checks, and its merge re-reads the row FOR UPDATE.
    await db.commit()
    # Admission comes after the lookup because authentication and ownership need the database,
    # and before the first read of the body: an over-cap request is refused at once, never parked
    # waiting for a permit, and its bytes stay unread.
    if not body_admission.admit(user.id):
        raise HTTPException(
            status_code=429,
            detail="Too many uploads in progress; retry later",
            headers={"Retry-After": str(settings.UPLOAD_RETRY_AFTER_SECONDS)},
        )
    started = time.monotonic()
    try:
        try:
            # Each wait for the next body chunk is bounded (MEMNG-934): a body that goes silent
            # ends here as a cut, its bytes kept by the disconnect path and its permit released
            # by the `finally` below, instead of holding both until Traefik's read timeout. The
            # answer is the cut path's own — 200 with the offset the bytes reached — because that
            # answer is already the contract's "offset the next PATCH must start at", which is
            # all a client still listening needs to resume; a new status would be a schema change
            # every generated client must learn, for no different action on its side.
            body = svc.idle_bounded(request.stream(), settings.UPLOAD_BODY_IDLE_SECONDS)
            appended = await svc.append_stream(db, upload, upload_offset, body)
        except svc.UploadTooLarge as exc:
            upload_metrics.count_size_rejection()
            cap = settings.MAX_UPLOAD_MIB * 1024 * 1024
            detail = (
                f"Upload exceeds the {settings.MAX_UPLOAD_MIB} MiB limit"
                if exc.total > cap
                else f"Upload exceeds its declared size of {upload.declared_size} bytes"
            )
            raise HTTPException(status_code=413, detail=detail) from None
        except ValueError:
            raise HTTPException(status_code=409, detail="Offset gap; re-sync via HEAD") from None
        if appended is None:
            # The session was consumed (or reaped) while this append waited for its row. Same
            # answer as an unknown uid — because that is what it now is — rather than the 500
            # `.scalar_one()` used to raise on the very interleaving the row lock exists to make
            # safe.
            raise HTTPException(status_code=404, detail="Upload not found")
        upload = appended.upload
        await db.commit()
        # After the commit, so bytes a 413 or a rollback discarded are never counted.
        upload_metrics.count_bytes(getattr(request.state, "auth_kind", "session"), appended.merged)
    finally:
        body_admission.release(user.id)
        upload_metrics.UPLOAD_PATCH_SECONDS.observe(time.monotonic() - started)
    if appended.kept_from_cut:
        # The client is gone and never reads this answer; the route still returns a valid
        # UploadOut, because FastAPI validates every return against `response_model`.
        logger.info(
            "upload.partial_commit",
            upload_uid=upload.upload_uid,
            offset=upload_offset,
            bytes=appended.kept_from_cut,
        )
    response.headers["Upload-Offset"] = str(upload.bytes_received)
    return UploadOut(
        upload_uid=upload.upload_uid,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        bytes_received=upload.bytes_received,
    )


# A HEAD answer carries no body, so an operation that declares one leaves every generated client
# waiting for bytes that never arrive (MEMNG-198). `response_class=Response` is what says so in the
# schema: FastAPI publishes the response content under the media type it reads off the response
# class, and the base Response names none, so nothing is published under `content` at all. The
# status stays 200 and the Upload-Offset header, which is the whole answer, stays declared; the
# wire is untouched, because this handler already returns that Response itself.
@router.head(
    "/{upload_uid}",
    response_class=Response,
    openapi_extra={"responses": {"200": {"headers": _upload_offset_header()}}},
)
async def upload_offset(
    upload_uid: str,
    # Also an upload-only capture grant (MEMNG-938); nothing else in this router takes one.
    user: Annotated[User, Depends(auth.require_upload_user)],
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
    """What this upload's own metadata says about when it was captured.

    NON-CONSUMING and idempotent: it finalizes nothing, deletes nothing and moves nothing, so a
    composer may poll it while the person is still typing and then post the memo exactly as it
    would have without it. Owner-scoped exactly like every other upload verb, so another owner's
    uid answers with the same 404 as one that never existed.

    It may still WRITE — once. The first call performs the read the completing append normally
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
