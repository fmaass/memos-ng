from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import Memo, User, Visibility
from app.services import memos as svc

router = APIRouter(prefix="/api", tags=["memos"])


class MemoOut(BaseModel):
    uid: str
    owner: str
    content: str
    visibility: Visibility
    pinned: bool
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    tags: list[str]
    total_size: int = 0


class MemoCreate(BaseModel):
    content: str
    visibility: Visibility = Visibility.PRIVATE
    pinned: bool = False


class MemoPatch(BaseModel):
    content: str | None = None
    visibility: Visibility | None = None
    pinned: bool | None = None
    archived: bool | None = None


class MemoList(BaseModel):
    items: list[MemoOut]
    nextCursor: str | None
    totalCount: int


class TagCount(BaseModel):
    name: str
    count: int


def _out(memo: Memo) -> MemoOut:
    return MemoOut(
        uid=memo.uid,
        owner=memo.owner.subject,
        content=memo.content,
        visibility=memo.visibility,
        pinned=memo.pinned,
        created_at=memo.created_at,
        updated_at=memo.updated_at,
        archived_at=memo.archived_at,
        tags=sorted(t.name for t in memo.tags),
    )


@router.get("/memos", response_model=MemoList)
async def list_memos(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    created_before: datetime | None = None,
    created_after: datetime | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    visibility: Visibility | None = None,
    owner: str | None = None,
    q: str | None = None,
    include_archived: bool = False,
    sort: str = "-created_at",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> MemoList:
    items, next_cursor, total = await svc.list_memos(
        db,
        user,
        created_before=created_before,
        created_after=created_after,
        tags=tag,
        visibility=visibility,
        owner=owner,
        q=q,
        include_archived=include_archived,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
    return MemoList(items=[_out(m) for m in items], nextCursor=next_cursor, totalCount=total)


@router.post("/memos", response_model=MemoOut, status_code=201)
async def create_memo(
    body: MemoCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await svc.create_memo(
        db, user, content=body.content, visibility=body.visibility, pinned=body.pinned
    )
    await db.commit()
    return _out(memo)


async def _require_owned(db: AsyncSession, user: User, uid: str) -> Memo:
    memo = await svc.get_memo(db, user, uid)
    if memo is None:
        raise HTTPException(status_code=404, detail="Memo not found")
    return memo


@router.get("/memos/{uid}", response_model=MemoOut)
async def get_memo(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    return _out(await _require_owned(db, user, uid))


@router.patch("/memos/{uid}", response_model=MemoOut)
async def patch_memo(
    uid: str,
    body: MemoPatch,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)
    if memo.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your memo")
    memo = await svc.update_memo(db, memo, **body.model_dump(exclude_unset=True))
    await db.commit()
    return _out(memo)


@router.delete("/memos/{uid}", status_code=204)
async def delete_memo(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    memo = await _require_owned(db, user, uid)
    if memo.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your memo")
    await svc.delete_memo(db, memo)
    await db.commit()


@router.post("/memos/{uid}/pin", response_model=MemoOut)
async def pin_memo(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)
    memo = await svc.update_memo(db, memo, pinned=True)
    await db.commit()
    return _out(memo)


@router.post("/memos/{uid}/unpin", response_model=MemoOut)
async def unpin_memo(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)
    memo = await svc.update_memo(db, memo, pinned=False)
    await db.commit()
    return _out(memo)


@router.get("/tags", response_model=list[TagCount])
async def list_tags(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[TagCount]:
    return [TagCount(name=n, count=c) for n, c in await svc.list_tags(db, user)]


@router.get("/stats/heatmap")
async def stats_heatmap(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    return await svc.heatmap(db, user)
