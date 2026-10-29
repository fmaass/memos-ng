"""The guest door (`APP_ROLE=ingest`): redeem a pass, upload, complete a batch, read receipts.

A guest is a `pass_devices` row, never a user: its credential is `trip_dev_…`, stored as sha256 in
`pass_devices.token_hash`, and nothing else authenticates on these routes — a member PAT or device
token simply matches no device. Every lock in this module is taken in ONE order, pass → device →
upload session, so the redeem, create, append and completion paths never wait on each other in a
cycle.
"""

import html
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from anyio import to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.ingest import core
from app.models import (
    GuestUpload,
    IngestBatch,
    IngestBlob,
    IngestRefusal,
    Pass,
    PassDevice,
    PassEvent,
)
from app.services import uploads as upload_bytes

router = APIRouter()

GUEST_TOKEN_PREFIX = "trip_dev_"
MAX_BATCH_FILES = 500


class GuestRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


NOT_A_GUEST = GuestRefused(401, "Not a valid guest credential")
PASS_ENDED = GuestRefused(410, "This pass is no longer open")


@dataclass
class Guest:
    device: PassDevice
    pass_: Pass


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith(GUEST_TOKEN_PREFIX):
        return None
    return token.strip()


def _check(device: PassDevice | None, p: Pass | None) -> None:
    if device is None or not core.device_active(device):
        raise NOT_A_GUEST
    if p is None or core.pass_state(p) != "active":
        raise PASS_ENDED


async def require_guest(
    request: Request, db: Annotated[AsyncSession, Depends(get_session)]
) -> Guest:
    token = _bearer(request)
    if token is None:
        raise HTTPException(status_code=401, detail=NOT_A_GUEST.detail)
    row = (
        await db.execute(
            select(PassDevice, Pass)
            .join(Pass, Pass.id == PassDevice.pass_id)
            .where(PassDevice.token_hash == core.sha256_hex(token))
        )
    ).one_or_none()
    try:
        _check(row[0] if row else None, row[1] if row else None)
    except GuestRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    assert row is not None
    return Guest(device=row[0], pass_=row[1])


async def _lock_pass(db: AsyncSession, pass_id: int, *, share: bool) -> Pass | None:
    stmt = select(Pass).where(Pass.id == pass_id).execution_options(populate_existing=True)
    stmt = stmt.with_for_update(read=share)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _lock_device(db: AsyncSession, device_id: int, *, share: bool) -> PassDevice | None:
    stmt = (
        select(PassDevice)
        .where(PassDevice.id == device_id)
        .execution_options(populate_existing=True)
        .with_for_update(read=share)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _refence(db: AsyncSession, guest: Guest, *, device_for_update: bool) -> Guest:
    """Re-read the pass and the device under lock and re-judge them: the credential that earned
    the request is asked again at the instant its effect is written."""
    p = await _lock_pass(db, guest.pass_.id, share=True)
    device = await _lock_device(db, guest.device.id, share=not device_for_update)
    _check(device, p)
    assert device is not None and p is not None
    return Guest(device=device, pass_=p)


def _limits() -> dict[str, int]:
    return {
        "max_file_bytes": settings.MAX_UPLOAD_MIB * 1024 * 1024,
        "chunk_size": settings.UPLOAD_CHUNK_SIZE,
        "max_patch_bytes": settings.INGEST_PATCH_MAX_BYTES,
    }


# --- the page and the association file ---------------------------------------------------------

# The separate guest build (frontend/vite.guest.config.ts): one document and its hashed files.
# It exists only in the image; read per request so a fixture tree can stand in for it.
GUEST_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist-guest"
GUEST_DOCUMENT = "guest.html"
GUEST_ASSET_DIR = "guest-assets"
# The build's own output names: flat, hashed, and only the two types the page loads. Anything
# else — a dotfile, the document itself, a file beside it — is a 404 before the disk is asked.
GUEST_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*\.(js|css)$")
GUEST_ASSET_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
GUEST_ASSET_CACHE = "public, max-age=31536000, immutable"
# Placeholders in the built document the door fills in, so the page needs no request (and no
# credential) to know them: where the app is installed from, and what the picker accepts.
APP_URL_PLACEHOLDER = "__MEMOS_APP_URL__"
ACCEPT_PLACEHOLDER = "__MEMOS_ACCEPT__"

GUEST_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "worker-src 'self'; img-src 'self' blob:; media-src blob:; manifest-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
PAGE_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": GUEST_CSP,
}


def guest_app_url() -> str:
    """The configured app link, or "" — and "" for anything but an absolute https address, so a
    mistyped setting hides the action rather than offering a guest some other scheme."""
    url = settings.INGEST_GUEST_APP_URL.strip()
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.netloc else ""


def render_guest_document(template: str) -> str:
    accept = ",".join(f".{ext}" for ext in core.ALLOWED_EXTENSIONS)
    return template.replace(APP_URL_PLACEHOLDER, html.escape(guest_app_url(), quote=True)).replace(
        ACCEPT_PLACEHOLDER, html.escape(accept, quote=True)
    )


def _read_guest_document_sync(root: Path) -> str | None:
    try:
        return (root / GUEST_DOCUMENT).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _guest_asset_sync(root: Path, name: str) -> Path | None:
    path = root / GUEST_ASSET_DIR / name
    return path if path.is_file() else None


@router.get("/p/{secret}", response_class=HTMLResponse)
async def pass_page(secret: str) -> Response:
    # One document for every pass and for `/p/joined`: the secret stays in the path the client
    # already holds, and the page neither echoes nor checks it, so it never reaches a response or
    # a lookup from here.
    del secret
    template = await to_thread.run_sync(_read_guest_document_sync, GUEST_DIST)
    if template is None:
        return PlainTextResponse(
            "The guest page is not built", status_code=503, headers=PAGE_HEADERS
        )
    return HTMLResponse(render_guest_document(template), headers=PAGE_HEADERS)


@router.get("/guest-assets/{name}")
async def guest_asset(name: str) -> Response:
    match = GUEST_ASSET_NAME.match(name)
    if match is None:
        raise HTTPException(status_code=404, detail="Not found")
    path = await to_thread.run_sync(_guest_asset_sync, GUEST_DIST, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    # The policy rides the assets too: a worker script runs under the policy of its own response.
    return FileResponse(
        path,
        media_type=GUEST_ASSET_TYPES[match.group(1)],
        headers={**PAGE_HEADERS, "Cache-Control": GUEST_ASSET_CACHE},
    )


@router.get("/.well-known/apple-app-site-association")
async def association() -> JSONResponse:
    """Universal links and the App Clip for `/p/*` on the door. Absent while no team id is set,
    the way the home association file is."""
    team = settings.APPLE_TEAM_ID
    if not team:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(
        {
            "applinks": {
                "details": [
                    {
                        "appIDs": [f"{team}.{settings.IOS_BUNDLE_ID}"],
                        "components": [{"/": "/p/*"}],
                    }
                ]
            },
            "appclips": {"apps": [f"{team}.{settings.IOS_CLIP_BUNDLE_ID}"]},
        }
    )


# --- redeem ------------------------------------------------------------------------------------


class RedeemIn(BaseModel):
    secret: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=64)
    platform: Literal["web", "clip", "app"]
    device_hint: str | None = Field(default=None, max_length=512)


class PassOut(BaseModel):
    label: str
    scope: str
    expires_at: datetime


class RedeemOut(BaseModel):
    device_token: str
    device_id: str
    pass_: PassOut = Field(alias="pass", serialization_alias="pass")
    limits: dict[str, int]


def _hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


async def _address_refusals(db: AsyncSession, address: str, now: datetime) -> int:
    count = (
        await db.execute(
            select(IngestRefusal.refusals).where(
                IngestRefusal.ip_hash == address, IngestRefusal.window_start == _hour(now)
            )
        )
    ).scalar_one_or_none()
    return count or 0


async def record_refusal(
    db: AsyncSession, address: str, p: Pass | None, reason: str, now: datetime
) -> None:
    """Count one refused redemption: always per address and hour, and on the pass when the secret
    named one. A pass passing the hourly limit is suspended (its own column — never a revoke)."""
    stmt = pg_insert(IngestRefusal).values(ip_hash=address, window_start=_hour(now), refusals=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[IngestRefusal.ip_hash, IngestRefusal.window_start],
        set_={"refusals": IngestRefusal.refusals + 1},
    )
    await db.execute(stmt)
    if p is None:
        return
    db.add(
        PassEvent(pass_id=p.id, kind="refused", version=p.version, ip_hash=address, detail=reason)
    )
    await db.flush()
    refused = (
        await db.execute(
            select(func.count())
            .select_from(PassEvent)
            .where(
                PassEvent.pass_id == p.id,
                PassEvent.kind == "refused",
                PassEvent.at > now - timedelta(hours=1),
            )
        )
    ).scalar_one()
    if refused > settings.INGEST_REFUSALS_PER_HOUR and p.suspended_at is None:
        p.suspended_at = now
        db.add(
            PassEvent(
                pass_id=p.id,
                kind="updated",
                version=p.version,
                detail=f"suspended: {refused} refused redemptions within an hour",
            )
        )


async def _refuse(
    db: AsyncSession, address: str, p: Pass | None, status: int, detail: str, reason: str
) -> HTTPException:
    await record_refusal(db, address, p, reason, datetime.now(UTC))
    await db.commit()
    headers = {"Retry-After": "60"} if status == 429 else None
    return HTTPException(status_code=status, detail=detail, headers=headers)


@router.post("/api/pass/redeem", response_model=RedeemOut, status_code=201)
async def redeem(
    body: RedeemIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> RedeemOut:
    address = core.ip_hash(core.client_ip(request))
    now = datetime.now(UTC)
    presented = core.sha256_hex(body.secret)
    # FOR UPDATE: the device-cap count below and the enrollment are one decision per pass.
    p = (
        await db.execute(
            select(Pass)
            .where(Pass.secret_hash == presented)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if p is not None and not core.same_secret(presented, p.secret_hash):
        p = None
    if not core.redeem_window.hit(address, settings.INGEST_REDEEM_PER_MINUTE):
        raise await _refuse(db, address, p, 429, "Too many attempts; retry later", "rate")
    if await _address_refusals(db, address, now) > settings.INGEST_REFUSALS_PER_HOUR:
        raise await _refuse(db, address, p, 429, "Too many attempts; retry later", "address")
    if p is None:
        raise await _refuse(db, address, None, 404, "Not found", "unknown")
    if core.pass_state(p, now) != "active":
        raise await _refuse(db, address, p, 410, "This pass is no longer open", "ended")
    enrolled = (
        await db.execute(
            select(func.count())
            .select_from(PassDevice)
            .where(PassDevice.pass_id == p.id, PassDevice.revoked_at.is_(None))
        )
    ).scalar_one()
    if enrolled >= p.max_devices:
        raise await _refuse(db, address, p, 429, "This pass has no room for another device", "cap")
    token = GUEST_TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = PassDevice(
        uid=secrets.token_urlsafe(16),
        pass_id=p.id,
        token_hash=core.sha256_hex(token),
        display_name=body.display_name.strip()[:64] or "Guest",
        platform=body.platform,
        user_agent=(body.device_hint or request.headers.get("user-agent") or "")[:512] or None,
        last_seen=now,
    )
    db.add(device)
    await db.flush()
    db.add(
        PassEvent(
            pass_id=p.id, device_id=device.id, kind="redeemed", version=p.version, ip_hash=address
        )
    )
    device.change_seq = await core.next_change_seq(db)
    await db.commit()
    return RedeemOut.model_validate(
        {
            "device_token": token,
            "device_id": device.uid,
            "pass": PassOut(label=p.label, scope=p.scope, expires_at=p.expires_at),
            "limits": _limits(),
        }
    )


# --- me ----------------------------------------------------------------------------------------


class MeOut(BaseModel):
    device_id: str
    display_name: str
    platform: str
    pass_: PassOut = Field(alias="pass", serialization_alias="pass")
    limits: dict[str, int]
    uploads_count: int
    bytes_total: int


@router.get("/api/guest/me", response_model=MeOut)
async def me(guest: Annotated[Guest, Depends(require_guest)]) -> MeOut:
    d, p = guest.device, guest.pass_
    return MeOut.model_validate(
        {
            "device_id": d.uid,
            "display_name": d.display_name,
            "platform": d.platform,
            "pass": PassOut(label=p.label, scope=p.scope, expires_at=p.expires_at),
            "limits": _limits(),
            "uploads_count": d.uploads_count,
            "bytes_total": d.bytes_total,
        }
    )


# --- upload sessions ---------------------------------------------------------------------------


class GuestUploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    mime: str = Field(default="", max_length=255)
    size: int = Field(ge=1)
    last_modified: int | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class GuestUploadOut(BaseModel):
    already_received: bool = False
    upload_uid: str | None = None
    bytes_received: int = 0
    chunk_size: int = 0
    max_patch_bytes: int = 0


def _part_create_sync(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


async def _already_received(db: AsyncSession, pass_id: int, sha256: str) -> bool:
    return (
        await db.execute(
            select(IngestBlob.id)
            .join(IngestBatch, IngestBatch.id == IngestBlob.batch_id)
            .where(
                IngestBatch.pass_id == pass_id,
                IngestBatch.state != "quarantined",
                IngestBlob.sha256 == sha256,
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _pass_bytes(db: AsyncSession, pass_id: int) -> int:
    batches = (
        await db.execute(
            select(func.coalesce(func.sum(IngestBatch.byte_total), 0)).where(
                IngestBatch.pass_id == pass_id
            )
        )
    ).scalar_one()
    sessions = (
        await db.execute(
            select(func.coalesce(func.sum(GuestUpload.declared_size), 0))
            .join(PassDevice, PassDevice.id == GuestUpload.pass_device_id)
            .where(PassDevice.pass_id == pass_id)
        )
    ).scalar_one()
    return int(batches) + int(sessions)


async def _outstanding_bytes(db: AsyncSession) -> int:
    owed = (
        await db.execute(
            select(
                func.coalesce(func.sum(GuestUpload.declared_size - GuestUpload.bytes_received), 0)
            )
        )
    ).scalar_one()
    return int(owed)


@router.post("/api/guest/uploads", response_model=GuestUploadOut, status_code=201)
async def create_upload(
    body: GuestUploadIn,
    response: Response,
    guest: Annotated[Guest, Depends(require_guest)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> GuestUploadOut:
    if core.extension_of(body.filename) is None:
        raise HTTPException(status_code=415, detail="This file type is not accepted")
    if body.size > settings.MAX_UPLOAD_MIB * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"File exceeds the {settings.MAX_UPLOAD_MIB} MiB limit"
        )
    sha = body.sha256.lower() if body.sha256 else None
    if sha is not None and await _already_received(db, guest.pass_.id, sha):
        response.status_code = 200
        return GuestUploadOut(already_received=True)
    try:
        # The pass row FOR UPDATE: two creates on one pass reserve its bytes one after the other.
        p = await _lock_pass(db, guest.pass_.id, share=False)
        device = await _lock_device(db, guest.device.id, share=True)
        _check(device, p)
    except GuestRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    assert p is not None and device is not None
    if await _pass_bytes(db, p.id) + body.size > p.byte_cap:
        raise HTTPException(status_code=507, detail="This pass has no room for more pictures")
    # The floor protects a root disk other services share. Every open session still owes the
    # volume what it has not sent yet, so that is spoken for; the reservation lock makes two
    # creates on different passes count each other.
    await core.lock_disk_reservation(db)
    free = await to_thread.run_sync(core.free_bytes, core.ingest_root())
    if free - await _outstanding_bytes(db) - body.size < settings.INGEST_MIN_FREE_BYTES:
        raise HTTPException(status_code=507, detail="The door is full; try again later")
    uid = secrets.token_urlsafe(24)
    await to_thread.run_sync(_part_create_sync, core.part_path(uid))
    mtime = (
        upload_bytes.EPOCH + timedelta(milliseconds=body.last_modified)
        if body.last_modified
        else None
    )
    db.add(
        GuestUpload(
            upload_uid=uid,
            pass_device_id=device.id,
            filename=body.filename,
            mime=body.mime or "application/octet-stream",
            declared_size=body.size,
            bytes_received=0,
            sha256=sha,
            client_mtime=mtime,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.UPLOAD_EXPIRY_HOURS),
        )
    )
    db.add(PassEvent(pass_id=p.id, device_id=device.id, kind="upload_started", version=p.version))
    await db.commit()
    return GuestUploadOut(
        upload_uid=uid,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        max_patch_bytes=settings.INGEST_PATCH_MAX_BYTES,
    )


async def _own_upload(db: AsyncSession, guest: Guest, upload_uid: str) -> GuestUpload:
    row = (
        await db.execute(
            select(GuestUpload).where(
                GuestUpload.upload_uid == upload_uid,
                GuestUpload.pass_device_id == guest.device.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return row


@router.head("/api/guest/uploads/{upload_uid}", response_class=Response)
async def upload_offset(
    upload_uid: str,
    guest: Annotated[Guest, Depends(require_guest)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    row = await _own_upload(db, guest, upload_uid)
    return Response(status_code=200, headers={"Upload-Offset": str(row.bytes_received)})


def guest_ceiling(row: GuestUpload) -> int:
    return min(row.declared_size, settings.MAX_UPLOAD_MIB * 1024 * 1024)


def _spool_path(upload_uid: str) -> Path:
    return core.ingest_root() / "parts" / f"{upload_uid}.{secrets.token_urlsafe(8)}.spool"


@router.patch("/api/guest/uploads/{upload_uid}", response_model=GuestUploadOut)
async def append(
    upload_uid: str,
    request: Request,
    response: Response,
    guest: Annotated[Guest, Depends(require_guest)],
    db: Annotated[AsyncSession, Depends(get_session)],
    upload_offset: Annotated[int, Header(alias="Upload-Offset")] = 0,
) -> GuestUploadOut:
    row = await _own_upload(db, guest, upload_uid)
    # The lookup transaction ends before the body arrives, so a slow body pins no connection.
    await db.commit()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.INGEST_PATCH_MAX_BYTES:
        raise HTTPException(status_code=413, detail="A single upload body is at most 64 MiB")
    # The body lands twice before its spool goes: once in the spool, once appended to the part.
    remaining = max(row.declared_size - row.bytes_received, 0)
    body = int(declared) if declared.isdigit() else settings.INGEST_PATCH_MAX_BYTES
    free = await to_thread.run_sync(core.free_bytes, core.ingest_root())
    if free - 2 * min(body, remaining) < settings.INGEST_MIN_FREE_BYTES:
        raise HTTPException(status_code=507, detail="The door is full; try again later")
    # Read once: a rollback below expires every loaded row, the device included.
    device_id = guest.device.id
    if not core.body_admission.admit(device_id):
        raise HTTPException(
            status_code=429,
            detail="Too many uploads in progress; retry later",
            headers={"Retry-After": str(settings.UPLOAD_RETRY_AFTER_SECONDS)},
        )

    async def relock() -> GuestUpload | None:
        await _refence(db, guest, device_for_update=False)
        return (
            await db.execute(
                select(GuestUpload)
                .where(GuestUpload.upload_uid == upload_uid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    try:
        appended = await upload_bytes.append_offset_stream(
            db,
            row,
            upload_offset,
            request.stream(),
            part=core.part_path(upload_uid),
            spool=_spool_path(upload_uid),
            body_ceiling=min(settings.INGEST_PATCH_MAX_BYTES, guest_ceiling(row)),
            ceiling=guest_ceiling,
            relock=relock,
        )
        if appended is None:
            raise HTTPException(status_code=404, detail="Upload not found")
        await db.commit()
    except GuestRefused as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except upload_bytes.UploadTooLarge:
        await db.rollback()
        raise HTTPException(status_code=413, detail="Upload exceeds its size") from None
    except ValueError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Offset gap; re-sync via HEAD") from None
    finally:
        core.body_admission.release(device_id)
    row = appended.row
    response.headers["Upload-Offset"] = str(row.bytes_received)
    return GuestUploadOut(
        upload_uid=row.upload_uid,
        bytes_received=row.bytes_received,
        chunk_size=settings.UPLOAD_CHUNK_SIZE,
        max_patch_bytes=settings.INGEST_PATCH_MAX_BYTES,
    )


# --- completion ----------------------------------------------------------------------------------


class CompleteIn(BaseModel):
    upload_uids: list[str] = Field(min_length=1, max_length=MAX_BATCH_FILES)
    caption: str | None = Field(default=None, max_length=2000)


class ReceiptFile(BaseModel):
    filename: str
    sha256: str
    size: int


class Receipt(BaseModel):
    batch_id: str
    status: Literal["received", "held"]
    file_count: int
    byte_total: int
    caption: str | None
    completed_at: datetime | None
    files: list[ReceiptFile]


async def _receipt(db: AsyncSession, batch: IngestBatch) -> Receipt:
    blobs = (
        await db.execute(
            select(IngestBlob).where(IngestBlob.batch_id == batch.id).order_by(IngestBlob.id)
        )
    ).scalars()
    return Receipt(
        batch_id=batch.uid,
        status="held" if batch.state == "quarantined" else "received",
        file_count=batch.file_count,
        byte_total=batch.byte_total,
        caption=batch.caption,
        completed_at=batch.completed_at,
        files=[ReceiptFile(filename=b.filename, sha256=b.sha256, size=b.size_bytes) for b in blobs],
    )


def completion_key(device_uid: str, upload_uids: list[str]) -> str:
    return core.sha256_hex("\n".join([device_uid, *sorted(set(upload_uids))]))


async def _batch_by_key(db: AsyncSession, key: str, device_id: int) -> IngestBatch | None:
    return (
        await db.execute(
            select(IngestBatch)
            .where(IngestBatch.completion_key == key, IngestBatch.pass_device_id == device_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _hash_and_sniff_sync(path: Path, filename: str) -> tuple[str | None, str]:
    return core.sniff_matches(path, filename), upload_bytes._sha256_sync(path)


def _link_blob_sync(part: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(part, dest)
    except FileExistsError:
        pass  # content-addressed: the bytes already held under this name are these bytes


def _unlink_all_sync(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


async def _discard(db: AsyncSession, row: GuestUpload) -> None:
    await db.execute(delete(GuestUpload).where(GuestUpload.upload_uid == row.upload_uid))
    await db.commit()
    await to_thread.run_sync(_unlink_all_sync, [core.part_path(row.upload_uid)])


@router.post("/api/guest/memos", response_model=Receipt)
async def complete(
    body: CompleteIn,
    guest: Annotated[Guest, Depends(require_guest)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Receipt:
    """Finish a batch. Overlapping or replayed completions of the same uploads JOIN: the device
    row lock makes them take turns, and the one that comes second finds the batch the first made
    (by its completion key) and answers that batch's receipt — one batch, one answer."""
    key = completion_key(guest.device.uid, body.upload_uids)
    existing = await _batch_by_key(db, key, guest.device.id)
    if existing is not None:
        return await _receipt(db, existing)
    try:
        guest = await _refence(db, guest, device_for_update=True)
    except GuestRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    # The join point of record: under the device lock, the completion this one queued behind has
    # committed or rolled back, and its batch is visible here.
    existing = await _batch_by_key(db, key, guest.device.id)
    if existing is not None:
        return await _receipt(db, existing)
    uids = sorted(set(body.upload_uids))
    rows = list(
        (
            await db.execute(
                select(GuestUpload)
                .where(
                    GuestUpload.upload_uid.in_(uids),
                    GuestUpload.pass_device_id == guest.device.id,
                )
                .order_by(GuestUpload.upload_uid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if len(rows) != len(uids):
        raise HTTPException(status_code=409, detail="An upload is unknown or already completed")
    if any(r.bytes_received != r.declared_size for r in rows):
        raise HTTPException(status_code=409, detail="An upload is not fully received")
    checked: list[tuple[GuestUpload, str, str]] = []
    for r in rows:
        mime, sha = await to_thread.run_sync(
            _hash_and_sniff_sync, core.part_path(r.upload_uid), r.filename
        )
        if mime is None:
            await _discard(db, r)
            raise HTTPException(
                status_code=415, detail=f"{r.filename} is not the picture or video its name says"
            )
        if r.sha256 is not None and r.sha256 != sha:
            await _discard(db, r)
            raise HTTPException(status_code=422, detail=f"{r.filename} arrived damaged")
        checked.append((r, mime, sha))
    now = datetime.now(UTC)
    batch = IngestBatch(
        uid=secrets.token_urlsafe(16),
        pass_id=guest.pass_.id,
        pass_device_id=guest.device.id,
        caption=(body.caption or "").strip() or None,
        state="complete",
        file_count=0,
        byte_total=0,
        completed_at=now,
        completion_key=key,
    )
    db.add(batch)
    await db.flush()
    # Every hash lock up front, in hash order: two completions sharing bytes then meet in one
    # order whatever order their uploads were named in, and cannot deadlock.
    for sha in sorted({sha for _, _, sha in checked}):
        await core.lock_blob_hash(db, sha)
    seen: set[str] = set()
    for r, mime, sha in checked:
        if sha in seen:
            continue  # the same bytes twice in one batch are one file of it
        seen.add(sha)
        # Under the hash lock until the link has RETURNED: a cancelled request rolls back only
        # after the thread is done, so a purge never decides on this hash mid-link.
        _, cancelled = await core.finish_off_the_loop(
            _link_blob_sync, core.part_path(r.upload_uid), core.blob_path(sha)
        )
        if cancelled is not None:
            raise cancelled
        db.add(
            IngestBlob(
                batch_id=batch.id,
                sha256=sha,
                size_bytes=r.declared_size,
                mime=mime,
                filename=r.filename,
                last_modified=r.client_mtime,
                storage_path=core.blob_relpath(sha),
            )
        )
        batch.file_count += 1
        batch.byte_total += r.declared_size
    await db.execute(delete(GuestUpload).where(GuestUpload.upload_uid.in_(uids)))
    device = guest.device
    device.uploads_count += batch.file_count
    device.bytes_total += batch.byte_total
    device.last_seen = now
    db.add(
        PassEvent(
            pass_id=guest.pass_.id,
            device_id=device.id,
            kind="upload_finalized",
            version=guest.pass_.version,
            detail=batch.uid,
        )
    )
    seq = await core.next_change_seq(db)
    batch.change_seq = seq
    device.change_seq = seq
    await db.commit()
    # After the commit: the parts' bytes now live under their hashes, and a rolled-back
    # completion must still find its parts where it left them.
    await to_thread.run_sync(_unlink_all_sync, [core.part_path(u) for u in uids])
    return await _receipt(db, batch)


@router.get("/api/guest/batches", response_model=list[Receipt])
async def batches(
    guest: Annotated[Guest, Depends(require_guest)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[Receipt]:
    rows = (
        await db.execute(
            select(IngestBatch)
            .where(IngestBatch.pass_device_id == guest.device.id)
            .order_by(IngestBatch.completed_at.desc(), IngestBatch.id.desc())
            .limit(100)
        )
    ).scalars()
    return [await _receipt(db, b) for b in rows]
