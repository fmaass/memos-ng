from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import Pat, User
from app.services import pats as svc

router = APIRouter(prefix="/api/pats", tags=["pats"])


class PatCreate(BaseModel):
    name: str


class PatOut(BaseModel):
    id: int
    name: str
    last_used: datetime | None
    created_at: datetime


class PatCreated(PatOut):
    # The plaintext token is returned exactly once, on creation, and never stored.
    token: str


@router.post("", response_model=PatCreated, status_code=201)
async def create_pat(
    body: PatCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PatCreated:
    pat, token = await svc.create_pat(db, user, body.name)
    await db.commit()
    return PatCreated(
        id=pat.id, name=pat.name, last_used=pat.last_used, created_at=pat.created_at, token=token
    )


@router.get("", response_model=list[PatOut])
async def list_pats(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[PatOut]:
    rows = (await db.execute(select(Pat).where(Pat.user_id == user.id))).scalars().all()
    return [
        PatOut(id=p.id, name=p.name, last_used=p.last_used, created_at=p.created_at) for p in rows
    ]


@router.delete("/{pat_id}", status_code=204)
async def revoke_pat(
    pat_id: int,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    pat = (await db.execute(select(Pat).where(Pat.id == pat_id))).scalar_one_or_none()
    if pat is None or pat.user_id != user.id:
        raise HTTPException(status_code=404, detail="PAT not found")
    await db.delete(pat)
    await db.commit()
