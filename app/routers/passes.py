"""Trip passes, the owner routes (plan PLAN_2026-10-27_trip-pass-pluto P1).

A pass hangs off a collection, and every signed-in member edits every collection
(app/routers/collections.py), so every member issues, lists, edits and revokes every pass and
moderates what came in through it; `createdBy` records who issued it and grants nothing. No guest
reaches any route here: the guest door is a separate installation with its own routes.

A revoked pass is terminal. Reading, editing or revoking it again answers 410 `pass_revoked` and
changes nothing; its devices and contributions stay readable, because moderation outlives the pass.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import Collection, Pass, PassDevice, User
from app.services import collections as coll_svc
from app.services import passes as pass_svc

router = APIRouter(prefix="/api", tags=["passes"])

_DETAIL = {
    "application/json": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}
}
_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No valid session cookie or bearer token.", "content": _DETAIL},
}
_NO_PASS: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    404: {"description": "No such pass. Nothing was changed.", "content": _DETAIL},
}
_REVOKED: dict[int | str, dict[str, Any]] = {
    **_NO_PASS,
    410: {
        "description": 'The pass is revoked (detail "pass_revoked"), which is terminal. Nothing '
        "was changed.",
        "content": _DETAIL,
    },
}

PassScope = Literal["contribute", "contribute_view"]
PassStateName = Literal["active", "expired", "revoked"]
Label = Annotated[str, Field(min_length=1, max_length=255)]
MaxDevices = Annotated[int, Field(ge=1, le=200)]
Notes = Annotated[str, Field(max_length=2000)]
Expiry = AwareDatetime


def _future(value: Any) -> Any:
    if value is not None and value <= datetime.now(UTC):
        raise ValueError("expiresAt must lie in the future")
    return value


class PassOut(BaseModel):
    uid: str
    collectionUid: str | None = Field(description="Null once the collection is deleted.")
    label: str
    scope: PassScope = Field(
        description="`contribute`: guests add pictures. `contribute_view`: they may also see the "
        "trip (not served by any route yet)."
    )
    state: PassStateName = Field(
        description="`revoked` is terminal; a pass whose collection is gone reads as revoked."
    )
    expiresAt: Rfc3339Datetime
    maxDevices: int
    notes: str | None
    version: int = Field(
        description="The pass's epoch: raised by every change, the revocation included."
    )
    revokedAt: Rfc3339Datetime | None
    revokedVersion: int | None = Field(description="The version the revocation produced.")
    deviceCount: int
    createdBy: str | None = Field(description="Who issued it; null once that member is gone.")
    createdAt: Rfc3339Datetime
    updatedAt: Rfc3339Datetime


class PassIssued(BaseModel):
    pass_: PassOut = Field(alias="pass", serialization_alias="pass")
    secret: str = Field(description="Shown once; only its sha256 is stored.")
    url: str = Field(description="The guest URL the QR code encodes.")
    qrSvg: str = Field(description="The URL as a standalone SVG QR code.")


class PassCreate(BaseModel):
    label: Label
    scope: PassScope = "contribute"
    expiresAt: Expiry | None = Field(
        default=None,
        description="An instant with its UTC offset, in the future. Default: the trip's span end "
        "plus 14 days, else 30 days from now.",
    )
    maxDevices: MaxDevices = pass_svc.DEFAULT_MAX_DEVICES
    notes: Notes | None = None

    _expiry_in_future = field_validator("expiresAt")(_future)


class PassPatch(BaseModel):
    # Absent leaves a field as it is; `notes` may be cleared with null, the others may not.
    label: Label | None = None
    scope: PassScope | None = None
    expiresAt: Expiry | None = None
    maxDevices: MaxDevices | None = None
    notes: Notes | None = None

    _expiry_in_future = field_validator("expiresAt")(_future)


class PassDeviceOut(BaseModel):
    uid: str
    displayName: str
    platform: Literal["web", "clip", "app"]
    firstSeen: Rfc3339Datetime
    lastSeen: Rfc3339Datetime | None
    uploadsCount: int
    bytesTotal: int
    revoked: bool
    revokedAt: Rfc3339Datetime | None
    revokedVersion: int | None


class ContributionOut(BaseModel):
    attachmentUid: str
    memoUid: str
    contributorName: str | None
    deviceUid: str | None
    caption: str | None
    hidden: bool = Field(
        description="Hidden from the pass's collection (excluded, or not a member)."
    )
    createdAt: Rfc3339Datetime


class ContributionSelection(BaseModel):
    attachmentUids: list[str] = Field(min_length=1, max_length=500)


ModerationOutcome = Literal["hidden", "unhidden", "removed", "notContribution", "error"]


class ModerationResult(BaseModel):
    attachmentUid: str
    outcome: ModerationOutcome


class ModerationOut(BaseModel):
    results: list[ModerationResult]


def _revoked() -> HTTPException:
    return HTTPException(status_code=410, detail="pass_revoked")


async def _pass(db: AsyncSession, uid: str) -> Pass:
    found = await pass_svc.get_pass(db, uid)
    if found is None:
        raise HTTPException(status_code=404, detail="Pass not found")
    return found


async def _live_pass(db: AsyncSession, uid: str) -> Pass:
    found = await _pass(db, uid)
    if pass_svc.pass_state(found) == "revoked":
        raise _revoked()
    return found


async def _render(db: AsyncSession, passes: list[Pass]) -> list[PassOut]:
    coll_ids = {p.collection_id for p in passes if p.collection_id is not None}
    colls: dict[int, str] = {}
    if coll_ids:
        rows = await db.execute(
            select(Collection.id, Collection.uid).where(Collection.id.in_(coll_ids))
        )
        colls = {cid: cuid for cid, cuid in rows.all()}
    user_ids = {p.created_by for p in passes if p.created_by is not None}
    subjects: dict[int, str] = {}
    if user_ids:
        rows = await db.execute(select(User.id, User.subject).where(User.id.in_(user_ids)))
        subjects = {uid: subj for uid, subj in rows.all()}
    counts = await pass_svc.device_counts(db, [p.id for p in passes])
    return [
        PassOut(
            uid=p.uid,
            collectionUid=colls.get(p.collection_id) if p.collection_id is not None else None,
            label=p.label,
            scope=cast(PassScope, p.scope),
            state=pass_svc.pass_state(p),
            expiresAt=p.expires_at,
            maxDevices=p.max_devices,
            notes=p.notes,
            version=p.version,
            revokedAt=p.revoked_at,
            revokedVersion=p.revoked_version,
            deviceCount=counts.get(p.id, 0),
            createdBy=subjects.get(p.created_by) if p.created_by is not None else None,
            createdAt=p.created_at,
            updatedAt=p.updated_at,
        )
        for p in passes
    ]


def _device_out(d: PassDevice) -> PassDeviceOut:
    return PassDeviceOut(
        uid=d.uid,
        displayName=d.display_name,
        platform=cast(Literal["web", "clip", "app"], d.platform),
        firstSeen=d.first_seen,
        lastSeen=d.last_seen,
        uploadsCount=d.uploads_count,
        bytesTotal=d.bytes_total,
        revoked=d.revoked_at is not None,
        revokedAt=d.revoked_at,
        revokedVersion=d.revoked_version,
    )


@router.post(
    "/collections/{uid}/passes",
    response_model=PassIssued,
    response_model_by_alias=True,
    status_code=201,
    responses={
        **_UNAUTHORIZED,
        404: {"description": "No such collection. Nothing was stored.", "content": _DETAIL},
        409: {
            "description": 'The collection is a trip without a tag (detail "trip_has_no_tag"): a '
            "guest picture taken outside the span joins the trip only by its tag. Nothing was "
            "stored.",
            "content": _DETAIL,
        },
    },
)
async def issue_pass(
    uid: str,
    body: PassCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PassIssued:
    """Issue a guest pass for this collection. The response carries the secret, its URL and the
    URL as a QR code; no later read returns the secret again."""
    collection = await coll_svc.get_collection(db, uid)
    if collection is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    try:
        p, secret = await pass_svc.issue_pass(
            db,
            collection,
            user,
            label=body.label,
            scope=body.scope,
            expires_at=body.expiresAt,
            max_devices=body.maxDevices,
            notes=body.notes,
        )
        url = pass_svc.pass_url(secret)
    except pass_svc.TripHasNoTag as exc:
        raise HTTPException(status_code=409, detail="trip_has_no_tag") from exc
    except pass_svc.DoorNotConfigured as exc:
        # The schema's 503 ({detail}) already stands on every operation (app.main); this one is an
        # operator fault, named so whoever reads the log or the answer knows which setting to fix.
        raise HTTPException(
            status_code=503,
            detail="TRIP_PASS_BASE_URL is not an absolute https origin; no pass was issued",
        ) from exc
    await db.commit()
    return PassIssued.model_validate(
        {
            "pass": (await _render(db, [p]))[0],
            "secret": secret,
            "url": url,
            "qrSvg": pass_svc.qr_svg(url),
        }
    )


@router.get(
    "/collections/{uid}/passes",
    response_model=list[PassOut],
    responses={
        **_UNAUTHORIZED,
        404: {"description": "No such collection.", "content": _DETAIL},
    },
)
async def list_passes(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[PassOut]:
    """Every pass of this collection, revoked ones included, the newest first."""
    collection = await coll_svc.get_collection(db, uid)
    if collection is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return await _render(db, await pass_svc.list_passes(db, collection))


@router.get("/passes/{uid}", response_model=PassOut, responses=_REVOKED)
async def get_pass(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PassOut:
    """One pass. A revoked one answers 410; the collection's list still shows it."""
    return (await _render(db, [await _live_pass(db, uid)]))[0]


_FIELDS = {
    "label": "label",
    "scope": "scope",
    "expiresAt": "expires_at",
    "maxDevices": "max_devices",
    "notes": "notes",
}


@router.patch("/passes/{uid}", response_model=PassOut, responses=_REVOKED)
async def update_pass(
    uid: str,
    body: PassPatch,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PassOut:
    """Change the named fields; an absent field stays. Any change raises `version` by one; a body
    that changes nothing keeps it. Only `notes` may be cleared with null."""
    sent = body.model_dump(exclude_unset=True)
    for key in ("label", "scope", "expiresAt", "maxDevices"):
        if key in sent and sent[key] is None:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": ["body", key], "msg": "may not be null", "type": "value_error"}],
            )
    p = await _live_pass(db, uid)
    try:
        updated = await pass_svc.update_pass(db, p, user, {_FIELDS[k]: v for k, v in sent.items()})
    except pass_svc.PassRevoked as exc:
        await db.rollback()
        raise _revoked() from exc
    except pass_svc.PassGone as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Pass not found") from exc
    await db.commit()
    return (await _render(db, [updated]))[0]


@router.delete("/passes/{uid}", response_model=PassOut, responses=_REVOKED)
async def revoke_pass(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PassOut:
    """Revoke the pass and every device on it, and push that to the guest door at once. Terminal:
    the answer carries `revokedVersion`, and a second revoke is 410."""
    p = await _live_pass(db, uid)
    try:
        revoked = await pass_svc.revoke_pass(db, p, user)
    except pass_svc.PassRevoked as exc:
        await db.rollback()
        raise _revoked() from exc
    except pass_svc.PassGone as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Pass not found") from exc
    await db.commit()
    return (await _render(db, [revoked]))[0]


@router.get("/passes/{uid}/devices", response_model=list[PassDeviceOut], responses=_NO_PASS)
async def list_devices(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[PassDeviceOut]:
    """The guest devices enrolled on this pass, revoked ones included, the earliest first."""
    return [_device_out(d) for d in await pass_svc.list_devices(db, await _pass(db, uid))]


@router.delete(
    "/passes/{uid}/devices/{device_uid}",
    response_model=PassDeviceOut,
    responses={
        **_NO_PASS,
        410: {
            "description": 'The pass is revoked (detail "pass_revoked") or this device already is '
            '(detail "device_revoked"). Nothing was changed.',
            "content": _DETAIL,
        },
    },
)
async def revoke_device(
    uid: str,
    device_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PassDeviceOut:
    """Revoke one device; the others keep working. Raises the pass's `version` and pushes at
    once."""
    p = await _live_pass(db, uid)
    try:
        device = await pass_svc.revoke_device(db, p, device_uid, user)
    except pass_svc.PassRevoked as exc:
        await db.rollback()
        raise _revoked() from exc
    except pass_svc.DeviceRevoked as exc:
        await db.rollback()
        raise HTTPException(status_code=410, detail="device_revoked") from exc
    except pass_svc.PassGone as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Pass not found") from exc
    if device is None:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Device not found")
    await db.commit()
    return _device_out(device)


@router.get("/passes/{uid}/contributions", response_model=list[ContributionOut], responses=_NO_PASS)
async def list_contributions(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[ContributionOut]:
    """Every picture that came in through this pass, the newest first, with who brought it and
    whether it is hidden. Readable after a revoke."""
    found = await pass_svc.list_contributions(db, await _pass(db, uid))
    return [
        ContributionOut(
            attachmentUid=c.attachment_uid,
            memoUid=c.memo_uid,
            contributorName=c.contributor_name,
            deviceUid=c.device_uid,
            caption=c.caption,
            hidden=bool(c.hidden),
            createdAt=c.created_at,
        )
        for c in found
    ]


async def _moderate(
    db: AsyncSession,
    user: User,
    uid: str,
    verb: Literal["hide", "unhide", "remove"],
    body: ContributionSelection,
) -> ModerationOut:
    p = await _pass(db, uid)
    wanted = list(dict.fromkeys(body.attachmentUids))
    outcomes = await pass_svc.moderate(db, p, user, verb, wanted)
    return ModerationOut(
        results=[ModerationResult(attachmentUid=u, outcome=outcomes[u]) for u in wanted]
    )


@router.post("/passes/{uid}/contributions/hide", response_model=ModerationOut, responses=_NO_PASS)
async def hide_contributions(
    uid: str,
    body: ContributionSelection,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ModerationOut:
    """Hide pictures that came in through this pass from its collection: an exclusion for a trip,
    the member row taken away for an album. A uid that is not this pass's is `notContribution`."""
    return await _moderate(db, user, uid, "hide", body)


@router.post("/passes/{uid}/contributions/unhide", response_model=ModerationOut, responses=_NO_PASS)
async def unhide_contributions(
    uid: str,
    body: ContributionSelection,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ModerationOut:
    """Undo `hide`."""
    return await _moderate(db, user, uid, "unhide", body)


@router.post("/passes/{uid}/contributions/remove", response_model=ModerationOut, responses=_NO_PASS)
async def remove_contributions(
    uid: str,
    body: ContributionSelection,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ModerationOut:
    """Delete pictures that came in through this pass — the original and every derived file — one
    at a time. `error` means that picture's files could not be removed and nothing of it was."""
    return await _moderate(db, user, uid, "remove", body)
