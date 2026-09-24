"""tus 1.0 (core + creation + expiration) at `/api/tus`, over the same upload store as the
tus-style `/api/uploads` routes: a tus upload IS an `Upload` row and its part file, so finalize,
dedup, expiry and the reaper apply unchanged. Completion stays explicit — the client calls
`POST /api/uploads/{upload_uid}/finalize` with the uid its `Location` named.

A separate prefix rather than extra verbs on `/api/uploads/{upload_uid}`: the two dialects answer
the same PATCH differently (200 with a JSON body and replays as no-ops there, 204 and a 409 for
any offset but the held one here), so one path cannot honour both.
"""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import get_session
from app.models import Upload, User
from app.routers.uploads import _get_upload
from app.services import uploads as svc
from app.services.tus import (
    OFFSET_STREAM,
    TUS_VERSION,
    MetadataRefused,
    creation_metadata,
    echoed_metadata,
    expires_header,
    non_negative_header,
)

router = APIRouter(prefix="/api/tus", tags=["tus"])
logger = structlog.get_logger(__name__)

TUS_EXTENSIONS = "creation,expiration"


def _cap() -> int:
    return settings.MAX_UPLOAD_MIB * 1024 * 1024


def _refuse(status_code: int, detail: str) -> HTTPException:
    # The protocol puts Tus-Resumable on every answer but OPTIONS', refusals included.
    return HTTPException(
        status_code=status_code, detail=detail, headers={"Tus-Resumable": TUS_VERSION}
    )


async def require_tus_resumable(
    tus_resumable: Annotated[str | None, Header(alias="Tus-Resumable")] = None,
) -> None:
    """The version precondition. Declared on the decorator so FastAPI resolves it ahead of the
    credential: a client speaking another version learns that first, whoever it is."""
    if tus_resumable != TUS_VERSION:
        raise HTTPException(
            status_code=412,
            detail=f"Tus-Resumable must be {TUS_VERSION}",
            headers={"Tus-Resumable": TUS_VERSION, "Tus-Version": TUS_VERSION},
        )


async def _owned_upload(db: AsyncSession, upload_uid: str, owner_id: int) -> Upload:
    try:
        return await _get_upload(db, upload_uid, owner_id)
    except HTTPException as exc:
        raise _refuse(exc.status_code, str(exc.detail)) from None


# Fresh dicts per call: FastAPI merges `openapi_extra` into the operation by reference, so shared
# constants would leave two operations aliasing one node in the served document.
def _header(description: str) -> dict[str, Any]:
    return {"description": description, "schema": {"type": "string"}}


def _int_header(description: str) -> dict[str, Any]:
    return {"description": description, "schema": {"type": "integer"}}


def _refusal(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {"Tus-Resumable": _header("Always 1.0.0.")},
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}
            }
        },
    }


def _precondition_failed() -> dict[str, Any]:
    refusal = _refusal("`Tus-Resumable` is missing or is not 1.0.0.")
    refusal["headers"]["Tus-Version"] = _header("The versions the server speaks: 1.0.0.")
    return refusal


def _expires() -> dict[str, Any]:
    return _header("When the server discards the upload session (RFC 9110 date).")


@router.options(
    "",
    status_code=204,
    response_class=Response,
    operation_id="tus_options",
    description="The server's tus capabilities. Needs no credential: it discloses no user data.",
    openapi_extra={
        "responses": {
            "204": {
                "headers": {
                    "Tus-Resumable": _header("1.0.0."),
                    "Tus-Version": _header("1.0.0."),
                    "Tus-Extension": _header("The extensions spoken, comma-separated."),
                    "Tus-Max-Size": _int_header("The largest upload accepted, in bytes."),
                }
            }
        }
    },
)
async def tus_options() -> Response:
    return Response(
        status_code=204,
        headers={
            "Tus-Resumable": TUS_VERSION,
            "Tus-Version": TUS_VERSION,
            "Tus-Extension": TUS_EXTENSIONS,
            "Tus-Max-Size": str(_cap()),
        },
    )


@router.post(
    "",
    status_code=201,
    response_class=Response,
    operation_id="tus_create",
    dependencies=[Depends(require_tus_resumable)],
    description="Create an upload session of `Upload-Length` bytes. `Upload-Metadata` must "
    "carry the keys filename and filetype and may carry lastModified (epoch milliseconds); any "
    "other key is refused, so HEAD can echo exactly what was accepted.",
    openapi_extra={
        "responses": {
            "201": {
                "headers": {
                    "Location": _header("`/api/tus/{upload_uid}`, path-absolute."),
                    "Upload-Expires": _expires(),
                    "Tus-Resumable": _header("1.0.0."),
                }
            },
            "400": _refusal("`Upload-Length` or `Upload-Metadata` is missing or not accepted."),
            "412": _precondition_failed(),
            "413": _refusal("`Upload-Length` exceeds `Tus-Max-Size`."),
        }
    },
)
async def tus_create(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    upload_length: Annotated[str | None, Header(alias="Upload-Length")] = None,
    upload_metadata: Annotated[str | None, Header(alias="Upload-Metadata")] = None,
) -> Response:
    try:
        size = non_negative_header(upload_length, "Upload-Length")
    except ValueError as exc:
        raise _refuse(400, str(exc)) from None
    if size > _cap():
        raise _refuse(413, f"Upload-Length exceeds the {settings.MAX_UPLOAD_MIB} MiB limit")
    try:
        metadata = creation_metadata(upload_metadata)
    except MetadataRefused as exc:
        raise _refuse(400, str(exc)) from None
    upload = await svc.create_upload(
        db,
        owner_id=user.id,
        filename=metadata.filename,
        mime=metadata.mime,
        size=size,
        last_modified=metadata.last_modified,
    )
    await db.commit()
    return Response(
        status_code=201,
        headers={
            # Path-absolute: TUSKit resolves Location against the POST's own URL, and a bare uid
            # relative to /api/tus would land on /api/<uid>.
            "Location": f"{router.prefix}/{upload.upload_uid}",
            **expires_header(upload.expires_at),
            "Tus-Resumable": TUS_VERSION,
        },
    )


@router.head(
    "/{upload_uid}",
    response_class=Response,
    operation_id="tus_head",
    dependencies=[Depends(require_tus_resumable)],
    openapi_extra={
        "responses": {
            "200": {
                "headers": {
                    "Upload-Offset": _int_header("Bytes the server holds for this upload."),
                    "Upload-Length": _int_header("The size declared at creation."),
                    "Upload-Metadata": _header("The metadata accepted at creation."),
                    "Upload-Expires": _expires(),
                    "Cache-Control": _header("no-store."),
                    "Tus-Resumable": _header("1.0.0."),
                }
            },
            "404": _refusal("No such upload session of the caller's."),
            "412": _precondition_failed(),
        }
    },
)
async def tus_head(
    upload_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    upload = await _owned_upload(db, upload_uid, user.id)
    return Response(
        status_code=200,
        headers={
            "Upload-Offset": str(upload.bytes_received),
            "Upload-Length": str(upload.declared_size),
            "Upload-Metadata": echoed_metadata(upload),
            **expires_header(upload.expires_at),
            "Cache-Control": "no-store",
            "Tus-Resumable": TUS_VERSION,
        },
    )


@router.patch(
    "/{upload_uid}",
    status_code=204,
    response_class=Response,
    operation_id="tus_patch",
    dependencies=[Depends(require_tus_resumable)],
    description="Append the body at `Upload-Offset`, which must equal the offset the server "
    "holds. Bytes received before a disconnect are kept, and HEAD reports the offset they "
    "reached.",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {OFFSET_STREAM: {"schema": {"type": "string", "format": "binary"}}},
        },
        "responses": {
            "204": {
                "headers": {
                    "Upload-Offset": _int_header("Bytes the server now holds."),
                    "Upload-Expires": _expires(),
                    "Tus-Resumable": _header("1.0.0."),
                }
            },
            "400": _refusal("`Upload-Offset` is missing or not a non-negative integer."),
            "404": _refusal("No such upload session of the caller's."),
            "409": _refusal(
                "`Upload-Offset` is not the offset the server holds; nothing was written. "
                "Re-sync with HEAD."
            ),
            "412": _precondition_failed(),
            "413": _refusal("The body would exceed the declared length or the server limit."),
            "415": _refusal(f"The Content-Type header is not `{OFFSET_STREAM}`."),
        },
    },
)
async def tus_patch(
    upload_uid: str,
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    upload_offset: Annotated[str | None, Header(alias="Upload-Offset")] = None,
) -> Response:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != OFFSET_STREAM:
        raise _refuse(415, f"Content-Type must be {OFFSET_STREAM}")
    try:
        offset = non_negative_header(upload_offset, "Upload-Offset")
    except ValueError as exc:
        raise _refuse(400, str(exc)) from None
    upload = await _owned_upload(db, upload_uid, user.id)
    try:
        # strict_offset: the 409 is decided under the row lock that performs the append, not
        # from the snapshot `_owned_upload` just read.
        appended = await svc.append_stream(db, upload, offset, request.stream(), strict_offset=True)
    except svc.UploadTooLarge as exc:
        detail = (
            f"Upload exceeds the {settings.MAX_UPLOAD_MIB} MiB limit"
            if exc.total > _cap()
            else f"Upload exceeds its declared length of {upload.declared_size} bytes"
        )
        raise _refuse(413, detail) from None
    except ValueError:
        raise _refuse(409, "Upload-Offset does not match the server's; re-sync via HEAD") from None
    if appended is None:
        # Consumed (finalized) or reaped while this append waited for the row: it is unknown now.
        raise _refuse(404, "Upload not found")
    upload = appended.upload
    await db.commit()
    if appended.kept_from_cut:
        logger.info(
            "upload.partial_commit",
            upload_uid=upload.upload_uid,
            offset=offset,
            bytes=appended.kept_from_cut,
        )
    return Response(
        status_code=204,
        headers={
            "Upload-Offset": str(upload.bytes_received),
            **expires_header(upload.expires_at),
            "Tus-Resumable": TUS_VERSION,
        },
    )
