from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import Pat, User
from app.services import pats as svc

router = APIRouter(prefix="/api/pats", tags=["pats"])

# No bearer reaches these routes (Decision 5): a leaked device token must not be able to mint
# successors that outlive its own revocation. What DOES reach them, besides the browser session, is
# a step-up management grant — bought minutes earlier with a fresh Authelia authentication, never
# with a token (MEMNG-138 option 2, ADR-0015). That is how a native client manages its account's
# tokens without the capability ever becoming something a stolen token carries.


class PatCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Omitted = a token that never expires (every PAT that existed before device tokens). The
    # upper bound keeps a device token's blast radius bounded to a year even when the device is
    # lost and nobody revokes it; the lower bound refuses a token that is already dead on arrival.
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class PatOut(BaseModel):
    id: int
    name: str
    last_used: Rfc3339Datetime | None
    created_at: Rfc3339Datetime
    expires_at: Rfc3339Datetime | None


class PatCreated(PatOut):
    # The plaintext token is returned exactly once, on creation, and never stored.
    token: str


@router.post("", response_model=PatCreated, status_code=201)
async def create_pat(
    body: PatCreate,
    user: Annotated[User, Depends(auth.require_session_or_grant)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PatCreated:
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    pat, token = await svc.create_pat(db, user, body.name, expires_at=expires_at)
    await db.commit()
    return PatCreated(
        id=pat.id,
        name=pat.name,
        last_used=pat.last_used,
        created_at=pat.created_at,
        expires_at=pat.expires_at,
        token=token,
    )


@router.get("", response_model=list[PatOut])
async def list_pats(
    user: Annotated[User, Depends(auth.require_session_or_grant)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[PatOut]:
    stmt = (
        select(Pat)
        .where(Pat.user_id == user.id)
        # Newest first, so a token just minted is the row the client shows at the top; the id
        # keeps the order total when two mints share a created_at.
        .order_by(Pat.created_at.desc(), Pat.id.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        PatOut(
            id=p.id,
            name=p.name,
            last_used=p.last_used,
            created_at=p.created_at,
            expires_at=p.expires_at,
        )
        for p in rows
    ]


@router.delete("/{pat_id}", status_code=204)
async def revoke_pat(
    pat_id: int,
    user: Annotated[User, Depends(auth.require_session_or_grant)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    pat = (await db.execute(select(Pat).where(Pat.id == pat_id))).scalar_one_or_none()
    if pat is None or pat.user_id != user.id:
        raise HTTPException(status_code=404, detail="PAT not found")
    await db.delete(pat)
    await db.commit()
