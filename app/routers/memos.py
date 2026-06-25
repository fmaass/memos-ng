import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import Attachment, AttachmentStatus, Memo, User, Visibility
from app.routers.assets import RenditionItem, has_print, rendition_items
from app.services import memos as svc
from app.services import uploads as uploads_svc
from app.services.asset_urls import signed_tier_urls

router = APIRouter(prefix="/api", tags=["memos"])


def _unlink_paths(paths: list[str]) -> None:
    """Best-effort remove files finalize moved to disk when a post rolls back (off-loop)."""
    for path in paths:
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)


class AttachmentRef(BaseModel):
    uid: str
    mime: str
    status: AttachmentStatus
    blurhash: str | None
    width: int | None
    height: int | None
    duration: float | None
    mine: bool  # the requesting user owns it (→ may delete); shared media is view-only
    hasPrint: bool  # has a print master of any kind (legacy print_path OR a rendition)
    hasLegacyPrint: bool  # has the legacy single print_path → the /print download button applies
    renditions: list[RenditionItem]
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    posterUrl: str
    downloadUrl: str
    printUrl: str


class ReactionSummary(BaseModel):
    emoji: str
    count: int
    reacted: bool  # did the requesting user react with this emoji


class MemoOut(BaseModel):
    uid: str
    owner: str
    mine: bool  # the requesting user owns it (→ may edit/delete)
    content: str
    visibility: Visibility
    pinned: bool
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    tags: list[str]
    attachments: list[AttachmentRef] = []
    reactions: list[ReactionSummary] = []
    total_size: int = 0


class MemoCreate(BaseModel):
    content: str
    visibility: Visibility | None = None  # None → the user's default_visibility setting
    pinned: bool = False
    # Finalize-on-post: pending upload_uids become attachments only when the memo is created, so
    # an abandoned compose leaves only Uploads (which expire) — never an orphan attachment.
    upload_uids: list[str] = []
    attachment_uids: list[str] = []  # already-finalized attachments (back-compat)
    created_at: datetime | None = None  # custom display date (None → server now), parity w/ memos


class MemoPatch(BaseModel):
    content: str | None = None
    visibility: Visibility | None = None
    pinned: bool | None = None
    archived: bool | None = None
    created_at: datetime | None = None  # edit a memo's display date
    # New images attached while editing — pending uploads finalized + linked, same as create.
    # Optional/nullable (not []) so PATCH stays a true partial update: callers that only change
    # content/archived/etc. omit it. (Removing an attachment is DELETE /api/attachments/{uid}.)
    upload_uids: list[str] | None = None


class ReactionIn(BaseModel):
    emoji: str


class MemoList(BaseModel):
    items: list[MemoOut]
    nextCursor: str | None
    # null unless the caller asks (?with_total=true); the COUNT is expensive and the feed omits it
    totalCount: int | None


class TagCount(BaseModel):
    name: str
    count: int


def _att_ref(att: Attachment, user_id: int) -> AttachmentRef:
    base = f"/api/attachments/{att.uid}"
    now = time.time()
    # Derived tiers get HMAC-signed URLs → served DB-free (no pool pressure on the feed fan-out).
    # original/print stay unsigned (low fan-out, need the DB row; served via the auth path).
    return AttachmentRef(
        uid=att.uid,
        mime=att.mime,
        status=att.status,
        blurhash=att.blurhash,
        width=att.width,
        height=att.height,
        duration=att.duration,
        mine=att.owner_id == user_id,
        hasPrint=has_print(att),
        hasLegacyPrint=att.print_path is not None,
        renditions=rendition_items(att),
        **signed_tier_urls(att.uid, user_id, now=now),
        downloadUrl=f"{base}/original",
        printUrl=f"{base}/print",
    )


def _reactions(memo: Memo, user_id: int) -> list[ReactionSummary]:
    counts: dict[str, int] = {}
    mine: set[str] = set()
    for r in memo.reactions:
        counts[r.emoji] = counts.get(r.emoji, 0) + 1
        if r.user_id == user_id:
            mine.add(r.emoji)
    return [
        ReactionSummary(emoji=e, count=c, reacted=e in mine) for e, c in sorted(counts.items())
    ]


def _out(memo: Memo, user_id: int) -> MemoOut:
    return MemoOut(
        uid=memo.uid,
        owner=memo.owner.subject,
        mine=memo.owner_id == user_id,
        content=memo.content,
        visibility=memo.visibility,
        pinned=memo.pinned,
        created_at=memo.created_at,
        updated_at=memo.updated_at,
        archived_at=memo.archived_at,
        tags=sorted(t.name for t in memo.tags),
        attachments=[_att_ref(a, user_id) for a in memo.attachments],
        reactions=_reactions(memo, user_id),
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
    archived_only: bool = False,
    sort: str = "-created_at",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    with_total: bool = False,
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
        archived_only=archived_only,
        sort=sort,
        cursor=cursor,
        limit=limit,
        with_total=with_total,
    )
    return MemoList(
        items=[_out(m, user.id) for m in items], nextCursor=next_cursor, totalCount=total
    )


@router.post("/memos", response_model=MemoOut, status_code=201)
async def create_memo(
    body: MemoCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    # Finalize-on-post: pending uploads become attachments now, then link them alongside any
    # already-finalized attachment_uids (back-compat). Abandoned composes leave only Uploads.
    # finalize moves files to disk before the shared commit — if the post then rolls back, unlink
    # those moved originals so they don't leak (the DB row is reverted but the file would remain).
    moved: list[str] = []
    committed = False
    try:
        finalized = (
            await uploads_svc.finalize_pending(db, body.upload_uids, user.id, moved_out=moved)
            if body.upload_uids
            else []
        )
        memo = await svc.create_memo(
            db,
            user,
            content=body.content,
            visibility=body.visibility or user.default_visibility,  # prod parity: per-user default
            pinned=body.pinned,
            attachment_uids=[*body.attachment_uids, *finalized],
            created_at=body.created_at,
            moved_out=moved,  # placement into the memo folder appends here → unlinked on rollback
        )
        await db.commit()
        committed = True
    finally:
        if not committed and moved:
            await to_thread.run_sync(_unlink_paths, moved)
    return _out(memo, user.id)


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
    return _out(await _require_owned(db, user, uid), user.id)


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
    # Same finalize-on-post pattern as create: new uploads become attachments and link to this memo;
    # if the patch rolls back, unlink the moved originals so they don't leak.
    moved: list[str] = []
    committed = False
    try:
        fields = body.model_dump(exclude_unset=True, exclude={"upload_uids"})
        memo = await svc.update_memo(db, memo, **fields)
        if body.upload_uids:
            finalized = await uploads_svc.finalize_pending(
                db, body.upload_uids, user.id, moved_out=moved
            )
            await svc.link_attachments(db, user.id, memo, finalized, moved_out=moved)
        await db.commit()
        committed = True
    finally:
        if not committed and moved:
            await to_thread.run_sync(_unlink_paths, moved)
    await db.refresh(memo)
    return _out(memo, user.id)


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
    if memo.owner_id != user.id:  # pinning is a mutation — owner-only, not visible-to-user
        raise HTTPException(status_code=403, detail="Not your memo")
    memo = await svc.update_memo(db, memo, pinned=True)
    await db.commit()
    return _out(memo, user.id)


@router.post("/memos/{uid}/unpin", response_model=MemoOut)
async def unpin_memo(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)
    if memo.owner_id != user.id:  # unpinning is a mutation — owner-only, not visible-to-user
        raise HTTPException(status_code=403, detail="Not your memo")
    memo = await svc.update_memo(db, memo, pinned=False)
    await db.commit()
    return _out(memo, user.id)


@router.post("/memos/{uid}/reactions", response_model=MemoOut)
async def react(
    uid: str,
    body: ReactionIn,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)  # any memo visible to the user
    await svc.add_reaction(db, memo, user, body.emoji)
    await db.commit()
    await db.refresh(memo, attribute_names=["reactions"])  # expire_on_commit=False → reload
    return _out(memo, user.id)


@router.delete("/memos/{uid}/reactions/{emoji}", response_model=MemoOut)
async def unreact(
    uid: str,
    emoji: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)
    await svc.remove_reaction(db, memo, user, emoji)
    await db.commit()
    await db.refresh(memo, attribute_names=["reactions"])
    return _out(memo, user.id)


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
