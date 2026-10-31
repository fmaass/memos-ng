"""The interface home pulls from (`APP_ROLE=ingest-home`), reachable only through home's port
forward. One principal: home, holding `INGEST_SERVICE_TOKEN`, compared in constant time; an unset
token refuses every request rather than matching an empty one.

Home is the authority for a pass. A pushed pass is stored as the ingest projection keyed by
`passes.uid`: the home-only references (`collection_id`, `created_by`, `revoked_by`) stay null
here, and a push lands only when its `version` is higher than the stored one. Device revocations
are their own records, applied whatever the pass version says, and never cleared from here.
"""

import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import structlog
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session, get_session
from app.ingest import core
from app.models import GuestUpload, IngestBatch, IngestBlob, Pass, PassDevice, PassEvent

logger = structlog.get_logger(__name__)


def service_token_ok(header: str) -> bool:
    expected = settings.INGEST_SERVICE_TOKEN
    if not expected:
        return False  # fail closed: no configured token admits nobody
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip().encode(), expected.encode())


async def require_home(request: Request) -> None:
    if not service_token_ok(request.headers.get("authorization", "")):
        raise HTTPException(status_code=401, detail="Unauthorized")


router = APIRouter(prefix="/ingest", dependencies=[Depends(require_home)])

SHA256 = re.compile(r"^[0-9a-f]{64}$")


# --- pushed pass state -------------------------------------------------------------------------


class PassStateIn(BaseModel):
    uid: str = Field(min_length=1, max_length=64)
    secret_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str = Field(max_length=255)
    scope: Literal["contribute", "contribute_view"]
    expires_at: datetime
    max_devices: int = Field(gt=0)
    byte_cap: int = Field(gt=0)
    version: int = Field(gt=0)
    revoked_at: datetime | None = None
    revoked_version: int | None = None
    unsuspend_version: int | None = Field(
        default=None,
        description="The version of the owner's newest lift of a suspension, if there was one.",
    )


class PassPushIn(BaseModel):
    passes: list[PassStateIn] = Field(max_length=500)


class PassPushResult(BaseModel):
    uid: str
    version: int = Field(description="The version this host now holds for the pass.")
    applied: bool
    suspended_at: datetime | None


async def apply_pass_state(db: AsyncSession, state: PassStateIn) -> PassPushResult:
    revoked = state.revoked_at is not None
    values = {
        "secret_hash": state.secret_hash,
        "label": state.label,
        "scope": state.scope,
        "expires_at": state.expires_at,
        "max_devices": state.max_devices,
        "byte_cap": state.byte_cap,
        "version": state.version,
        "revoked_at": state.revoked_at,
        "revoked_version": (state.revoked_version or state.version) if revoked else None,
    }
    inserted = (
        await db.execute(
            pg_insert(Pass)
            .values(uid=state.uid, **values)
            .on_conflict_do_nothing(index_elements=[Pass.uid])
            .returning(Pass.id)
        )
    ).scalar_one_or_none()
    p = (
        await db.execute(
            select(Pass)
            .where(Pass.uid == state.uid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    applied = inserted is not None
    held = p.version
    if not applied and state.version > held:
        if p.revoked_at is not None:
            # A revocation is terminal: a later version without one cannot reopen the pass.
            values = {k: v for k, v in values.items() if k not in ("revoked_at", "revoked_version")}
        for name, value in values.items():
            setattr(p, name, value)
        applied = True
        # A lift clears only a suspension this host held before the lift's version arrived: once
        # held, every later push carries the same lift, and a suspension set since stands.
        lift = state.unsuspend_version
        if p.suspended_at is not None and lift is not None and held < lift <= state.version:
            p.suspended_at = None
            db.add(PassEvent(pass_id=p.id, kind="updated", version=lift, detail=core.LIFTED))
    if applied:
        db.add(PassEvent(pass_id=p.id, kind="updated", version=p.version, detail="pushed"))
    await db.flush()
    return PassPushResult(
        uid=p.uid, version=p.version, applied=applied, suspended_at=p.suspended_at
    )


@router.put("/passes", response_model=list[PassPushResult])
async def push_passes(
    body: PassPushIn, db: Annotated[AsyncSession, Depends(get_session)]
) -> list[PassPushResult]:
    # Pass rows locked in uid order whatever order the push lists them in, so two bulk pushes
    # (or a push and a revocation batch, which sorts the same way) cannot deadlock.
    order = sorted(range(len(body.passes)), key=lambda i: body.passes[i].uid)
    results: dict[int, PassPushResult] = {}
    for i in order:
        results[i] = await apply_pass_state(db, body.passes[i])
    await db.commit()
    return [results[i] for i in range(len(body.passes))]


class DeviceRevocationIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64, description="pass_devices.uid")
    pass_uid: str = Field(min_length=1, max_length=64)
    version: int = Field(gt=0, description="The home pass version that performed the revocation.")
    revoked_at: datetime


class RevocationPushIn(BaseModel):
    revocations: list[DeviceRevocationIn] = Field(max_length=1000)


class RevocationAck(BaseModel):
    applied: list[DeviceRevocationIn]
    unknown: list[str]
    highest_version: int | None


@router.put("/device-revocations", response_model=RevocationAck)
async def push_device_revocations(
    body: RevocationPushIn, db: Annotated[AsyncSession, Depends(get_session)]
) -> RevocationAck:
    """Apply home's device revocations. Each record is judged on its own: the pass version this
    host holds plays no part, so a pass push at version N arriving first never makes the device
    revocation at N look stale. Only records that now stand on this host are acknowledged."""
    applied: list[DeviceRevocationIn] = []
    unknown: list[str] = []
    changed: list[PassDevice] = []
    for record in sorted(body.revocations, key=lambda r: (r.pass_uid, r.device_id)):
        p = (
            await db.execute(
                select(Pass)
                .where(Pass.uid == record.pass_uid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        device = None
        if p is not None:
            device = (
                await db.execute(
                    select(PassDevice)
                    .where(PassDevice.uid == record.device_id, PassDevice.pass_id == p.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if p is None or device is None:
            unknown.append(record.device_id)
            continue
        if device.revoked_at is None:
            device.revoked_at = record.revoked_at
            device.revoked_version = record.version
            db.add(
                PassEvent(
                    pass_id=p.id, device_id=device.id, kind="device_revoked", version=record.version
                )
            )
            changed.append(device)
        applied.append(record)
    # One cursor value per device: a page boundary inside a shared value would lose the rest of
    # the group to the next request's strict `> since`.
    for device in sorted(changed, key=lambda d: d.id):
        device.change_seq = await core.next_change_seq(db)
    await db.commit()
    return RevocationAck(
        applied=applied,
        unknown=unknown,
        highest_version=max((r.version for r in applied), default=None),
    )


# --- pulls -------------------------------------------------------------------------------------


class DeviceOut(BaseModel):
    device_id: str
    pass_uid: str
    display_name: str
    platform: str
    first_seen: datetime
    last_seen: datetime | None
    uploads_count: int
    bytes_total: int
    revoked_at: datetime | None
    revoked_version: int | None
    cursor: int


class DevicePage(BaseModel):
    devices: list[DeviceOut]
    next: int


@router.get("/devices", response_model=DevicePage)
async def pull_devices(
    db: Annotated[AsyncSession, Depends(get_session)],
    since: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> DevicePage:
    rows = (
        await db.execute(
            select(PassDevice, Pass.uid)
            .join(Pass, Pass.id == PassDevice.pass_id)
            .where(PassDevice.change_seq > since)
            .order_by(PassDevice.change_seq)
            .limit(limit)
        )
    ).all()
    devices = [
        DeviceOut(
            device_id=d.uid,
            pass_uid=pass_uid,
            display_name=d.display_name,
            platform=d.platform,
            first_seen=d.first_seen,
            last_seen=d.last_seen,
            uploads_count=d.uploads_count,
            bytes_total=d.bytes_total,
            revoked_at=d.revoked_at,
            revoked_version=d.revoked_version,
            cursor=d.change_seq or 0,
        )
        for d, pass_uid in rows
    ]
    return DevicePage(devices=devices, next=devices[-1].cursor if devices else since)


class BlobOut(BaseModel):
    sha256: str
    size: int
    mime: str
    filename: str
    last_modified: datetime | None


class BatchOut(BaseModel):
    batch_id: str
    pass_uid: str
    device_id: str | None
    display_name: str | None
    platform: str | None
    caption: str | None
    state: str
    completed_at: datetime | None
    blobs: list[BlobOut]
    cursor: int


class BatchPage(BaseModel):
    batches: list[BatchOut]
    next: int


@router.get("/batches", response_model=BatchPage)
async def pull_batches(
    db: Annotated[AsyncSession, Depends(get_session)],
    since: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> BatchPage:
    rows = (
        await db.execute(
            select(IngestBatch, Pass.uid, PassDevice)
            .join(Pass, Pass.id == IngestBatch.pass_id)
            .outerjoin(PassDevice, PassDevice.id == IngestBatch.pass_device_id)
            .where(IngestBatch.change_seq > since)
            .order_by(IngestBatch.change_seq)
            .limit(limit)
        )
    ).all()
    out: list[BatchOut] = []
    for batch, pass_uid, device in rows:
        blobs = (
            await db.execute(
                select(IngestBlob).where(IngestBlob.batch_id == batch.id).order_by(IngestBlob.id)
            )
        ).scalars()
        out.append(
            BatchOut(
                batch_id=batch.uid,
                pass_uid=pass_uid,
                device_id=device.uid if device else None,
                display_name=device.display_name if device else None,
                platform=device.platform if device else None,
                caption=batch.caption,
                state=batch.state,
                completed_at=batch.completed_at,
                blobs=[
                    BlobOut(
                        sha256=b.sha256,
                        size=b.size_bytes,
                        mime=b.mime,
                        filename=b.filename,
                        last_modified=b.last_modified,
                    )
                    for b in blobs
                ],
                cursor=batch.change_seq or 0,
            )
        )
    return BatchPage(batches=out, next=out[-1].cursor if out else since)


@router.get("/blobs/{sha256}")
async def blob(sha256: str, db: Annotated[AsyncSession, Depends(get_session)]) -> FileResponse:
    """The bytes of one received file, by hash, with Range. Served only to home."""
    if not SHA256.fullmatch(sha256):
        raise HTTPException(status_code=404, detail="Not found")
    named = (await db.execute(select(exists().where(IngestBlob.sha256 == sha256)))).scalar_one()
    path = core.blob_path(sha256)
    if not named or not await to_thread.run_sync(path.is_file):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="application/octet-stream")


# --- batch transitions -------------------------------------------------------------------------


class BatchStateOut(BaseModel):
    batch_id: str
    state: str


async def _transition(db: AsyncSession, uid: str, target: Literal["acked", "quarantined"]):
    batch = (
        await db.execute(
            select(IngestBatch)
            .where(IngestBatch.uid == uid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail="Not found")
    if batch.state == target:
        return BatchStateOut(batch_id=uid, state=batch.state)
    if batch.state == "quarantined":
        raise HTTPException(status_code=409, detail="The batch is quarantined")
    now = datetime.now(UTC)
    if target == "acked":
        batch.acked_at = now
    else:
        batch.quarantined_at = now
    batch.state = target
    await db.commit()
    return BatchStateOut(batch_id=uid, state=target)


@router.post("/batches/{uid}/ack", response_model=BatchStateOut)
async def ack(uid: str, db: Annotated[AsyncSession, Depends(get_session)]) -> BatchStateOut:
    return await _transition(db, uid, "acked")


@router.post("/batches/{uid}/quarantine", response_model=BatchStateOut)
async def quarantine(uid: str, db: Annotated[AsyncSession, Depends(get_session)]) -> BatchStateOut:
    return await _transition(db, uid, "quarantined")


# --- retention ---------------------------------------------------------------------------------


def _unlink_sync(path) -> None:
    path.unlink(missing_ok=True)


def _sweep_parts_sync(live: frozenset[str], expired: frozenset[str], horizon: float) -> int:
    """Unlink what lies in parts/ and belongs to no live upload session: a spool a killed append
    left behind, a part whose row is gone. The OWNING SESSION decides first — a live session's
    files stay however old they look, since a stalled append may still write them. A file of a
    session this sweep just expired goes now: its row's expires_at was its horizon. Any other file
    goes once it is older than the upload horizon: a create makes the part before its row
    commits, so a young file may simply not be visible yet. `<uid>.part` and
    `<uid>.<token>.spool` both start with the uid, and token_urlsafe has no dot. Finalized blobs
    live in blobs/, which this never reads."""
    parts = core.ingest_root() / "parts"
    if not parts.is_dir():
        return 0
    swept = 0
    for path in parts.iterdir():
        try:
            uid = path.name.split(".", 1)[0]
            if uid in live or (uid not in expired and path.stat().st_mtime >= horizon):
                continue
            path.unlink()
            swept += 1
        except OSError:  # vanished under us: its own request or completion just removed it
            continue
    return swept


async def purge(now: datetime | None = None) -> dict[str, int]:
    """Delete what home has acknowledged for longer than the retention window, and expired upload
    sessions. Unacknowledged and quarantined batches are never touched. A file goes only when no
    remaining batch names its hash, decided under that hash's lock, which a completion linking
    the same bytes also takes."""
    at = now or datetime.now(UTC)
    cutoff = at - timedelta(days=settings.INGEST_RETENTION_DAYS)
    async with async_session() as db:
        # FOR UPDATE, ascending, in one statement: a quarantine (or any transition) holding a
        # batch makes this wait, and Postgres re-checks the WHERE on the row as that transition
        # left it, so a batch quarantined meanwhile drops out instead of being deleted.
        doomed = (
            (
                await db.execute(
                    select(IngestBatch.id)
                    .where(IngestBatch.state == "acked", IngestBatch.acked_at < cutoff)
                    .order_by(IngestBatch.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        hashes = set(
            (
                await db.execute(select(IngestBlob.sha256).where(IngestBlob.batch_id.in_(doomed)))
            ).scalars()
        )
        if doomed:
            await db.execute(
                delete(IngestBatch).where(
                    IngestBatch.id.in_(doomed),
                    IngestBatch.state == "acked",
                    IngestBatch.acked_at < cutoff,
                )
            )
        expired = (
            (
                await db.execute(
                    delete(GuestUpload)
                    .where(GuestUpload.expires_at < at)
                    .returning(GuestUpload.upload_uid)
                )
            )
            .scalars()
            .all()
        )
        await db.commit()
    for uid in expired:
        await to_thread.run_sync(_unlink_sync, core.part_path(uid))
    # Read after the expired rows are gone, so their leftovers (a spool) count as no one's.
    async with async_session() as db:
        live = frozenset((await db.execute(select(GuestUpload.upload_uid))).scalars())
    horizon = (at - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS)).timestamp()
    strays = await to_thread.run_sync(_sweep_parts_sync, live, frozenset(expired), horizon)
    removed = 0
    for sha in sorted(hashes):
        async with async_session() as db:
            await core.lock_blob_hash(db, sha)
            still = (
                await db.execute(select(exists().where(IngestBlob.sha256 == sha)))
            ).scalar_one()
            cancelled = None
            if not still:
                # Under the hash lock until the unlink has RETURNED, even when shutdown cancels
                # the sweeper: a completion linking these bytes waits for the lock, and must find
                # the file gone or present, never vanishing after it named it.
                _, cancelled = await core.finish_off_the_loop(_unlink_sync, core.blob_path(sha))
                removed += 1
            await db.commit()
        if cancelled is not None:
            raise cancelled
    result = {
        "batches": len(doomed),
        "sessions": len(expired),
        "files": removed,
        "strays": strays,
    }
    logger.info("ingest.purge", **result)
    return result
