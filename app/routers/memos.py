import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import Attachment, AttachmentStatus, Memo, User, Visibility
from app.routers import RequestVisibility
from app.routers.assets import RenditionItem, has_print, rendition_items
from app.services import frame_share as frame_share_svc
from app.services import memos as svc
from app.services import uploads as uploads_svc
from app.services.asset_urls import signed_tier_urls

router = APIRouter(prefix="/api", tags=["memos"])

logger = structlog.get_logger(__name__)


async def _enqueue_frame_shares_safe(db: AsyncSession, memo: Memo, user: User) -> None:
    """Enqueue frame shares inside a SAVEPOINT so an unexpected error in this optional feature
    (tag join, pg_insert) can't abort the core memo write. On error the savepoint rolls back its
    partial work — leaving the outer transaction usable — and the memo save proceeds. The core
    write path must never be hostage to frame sharing."""
    try:
        async with db.begin_nested():
            await frame_share_svc.maybe_enqueue_frame_shares(db, memo, user)
    except Exception as exc:
        logger.warning("frame_share.enqueue_error", memo_id=memo.id, error=str(exc)[:200])


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


# Bounded and opaque: the shape gate only has to bound what the unique index stores and reject
# junk, so a client is free to use a UUID, a ULID or a random token instead of one id library.
IdempotencyKey = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class UploadsUnlinkableDetail(BaseModel):
    """Why a memo write was refused: the requested uploads cannot become attachments (MEMNG-113).

    A dedicated 409 with a machine-readable `code`, NOT the 422 FastAPI already uses for a malformed
    request — a retrying client has to tell "that upload is gone, re-upload the bytes" apart from
    "this request is malformed, never send it again", and a body it cannot classify is how an outbox
    loops forever. The two buckets carry different remedies: `missing` (never existed, or expired
    and reaped — an upload owned by another user reads as missing too) needs a fresh upload;
    `incomplete` (bytes_received < declared size) needs the transfer resumed from HEAD."""

    code: Literal["uploads_unlinkable"]  # required, not defaulted: generated clients switch on it
    missing_upload_uids: list[str]
    incomplete_upload_uids: list[str]


class UploadsUnlinkableError(BaseModel):
    detail: UploadsUnlinkableDetail


class CreateRacedDetail(BaseModel):
    """Why a keyed create could not be resolved (MEMNG-110): concurrent writes raced it and the
    memo they raced for was deleted before it could be read back. Retrying with the same key is
    safe — that is what the key is for — which is why this is a 5xx the client may repeat and not
    a 4xx telling it to stop."""

    code: Literal["create_raced"]
    message: str


class CreateRacedError(BaseModel):
    detail: CreateRacedDetail


UNLINKABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "model": UploadsUnlinkableError,
        "description": "A requested upload_uid is missing, expired or incomplete. No memo is "
        "created and no attachment is linked — resolve the named uploads and retry.",
    }
}


CREATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **UNLINKABLE_RESPONSE,
    503: {
        "model": CreateRacedError,
        "description": "Concurrent writes raced this keyed create. Nothing was stored under the "
        "key — retry the same request, idempotency_key included.",
    },
}


def _unlinkable(exc: uploads_svc.UploadsUnlinkable) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=UploadsUnlinkableDetail(
            code="uploads_unlinkable",
            missing_upload_uids=exc.missing,
            incomplete_upload_uids=exc.incomplete,
        ).model_dump(),
    )


class MemoCreate(BaseModel):
    content: str
    visibility: RequestVisibility = None  # unset → the user's default_visibility setting
    pinned: bool = False
    # Finalize-on-post: pending upload_uids become attachments only when the memo is created, so
    # an abandoned compose leaves only Uploads (which expire) — never an orphan attachment.
    upload_uids: list[str] = []
    attachment_uids: list[str] = []  # already-finalized attachments (back-compat)
    created_at: datetime | None = None  # custom display date (None → server now), parity w/ memos
    idempotency_key: IdempotencyKey | None = Field(
        default=None,
        description=(
            "Optional client-generated retry token: a UUID, or any opaque 8-64 character token of "
            "[A-Za-z0-9_-], unique per logical create. Posting it a second time returns the memo "
            "the first call stored — same uid, same attachments — and returns it with **201, the "
            "same status as the original create**. The body is the whole answer (the uid tells a "
            "client which memo is now stored), so the status is not used as a second channel, and "
            "a safe retry looks exactly like the success it is repeating. Two owners may reuse one "
            "token. Without a key, a retry stores a SECOND memo."
        ),
    )


class MemoPatch(BaseModel):
    content: str | None = None
    visibility: RequestVisibility = None
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
    # SkipJsonSchema, same as the request bodies — no BeforeValidator: a query string cannot
    # carry a JSON null, so absent is already the only way this one arrives unset.
    visibility: Visibility | SkipJsonSchema[None] = None,
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


async def _create_or_replay(
    *,
    attempt: Callable[[], Awaitable[Memo]],
    lookup: Callable[[], Awaitable[Memo | None]],
    rollback: Callable[[], Awaitable[None]],
    tries: int = 2,
) -> Memo:
    """Insert a keyed memo, resolving a lost race against the partial unique index (MEMNG-110).

    Losing that race normally means the winner is there to be read back and returned. It can also
    mean the winner was DELETED in the moment between our insert failing and our reading it back —
    the key is then free again, so a fresh insert is legitimate and we take one more run at it
    rather than answering an error for a memo nobody has.

    Two tries, not a loop until success: each round needs a concurrent create AND a concurrent
    delete of that create to land inside the same window, so a third round buys nothing an
    unbounded retry could not turn into a hot loop. Exhausting them raises 503 — never a bare
    IntegrityError as a 500 — because the client's remedy is real: retrying with the same key is
    safe by construction, and 5xx is the class our retry contract already says to repeat."""
    for _ in range(tries):
        try:
            return await attempt()
        except IntegrityError:
            await rollback()
            winner = await lookup()
            if winner is not None:
                return winner
    raise HTTPException(
        status_code=503,
        detail=CreateRacedDetail(
            code="create_raced",
            message=(
                "Concurrent writes raced this create and the memo they raced for was deleted "
                "before it could be read back. Retry with the same idempotency_key."
            ),
        ).model_dump(),
    )


@router.post("/memos", response_model=MemoOut, status_code=201, responses=CREATE_RESPONSES)
async def create_memo(
    body: MemoCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    # Read off the ORM user BEFORE any rollback below: rolling back expires every object in the
    # session, and a later `user.id` would then lazy-refresh — sync IO in an async context, which
    # is a MissingGreenlet, not a working attribute read.
    owner_id = user.id
    key = body.idempotency_key
    if key is not None:
        # The replay lookup comes FIRST — ahead of the uploads (MEMNG-110 before MEMNG-113). By the
        # time a retry arrives, its upload_uids are gone: the first attempt finalized them, or they
        # expired. Validating them first would answer 409 to precisely the client the key exists to
        # protect, and the memo it is asking about is already stored WITH those attachments.
        replayed = await svc.get_by_idempotency_key(db, owner_id, key)
        if replayed is not None:
            return _out(replayed, owner_id)

    async def attempt() -> Memo:
        """One whole create, from finalize to commit. Owns its own `moved` list because it may run
        twice: each try must clean up only the originals IT moved, or a retry that succeeds would
        leave the previous try's files behind with no row referencing them."""
        # Finalize-on-post: pending uploads become attachments now, then link them alongside any
        # already-finalized attachment_uids (back-compat). Abandoned composes leave only Uploads.
        # finalize moves files to disk before the commit — if this try then fails, unlink those
        # moved originals so they don't leak (the DB row reverts but the file would remain).
        moved: list[str] = []
        try:
            try:
                finalized = (
                    await uploads_svc.finalize_pending(
                        db, body.upload_uids, owner_id, moved_out=moved
                    )
                    if body.upload_uids
                    else []
                )
            except uploads_svc.UploadsUnlinkable as exc:
                raise _unlinkable(exc) from None
            memo = await svc.create_memo(
                db,
                user,
                content=body.content,
                visibility=body.visibility or user.default_visibility,  # per-user default (parity)
                pinned=body.pinned,
                attachment_uids=[*body.attachment_uids, *finalized],
                created_at=body.created_at,
                moved_out=moved,  # placement into the memo folder appends here → unlinked on fail
                idempotency_key=key,
            )
            # After all attachments are linked, enqueue frame shares for #curated photos (no-op
            # unless configured). Savepoint-isolated so a feature error can't fail the save.
            await _enqueue_frame_shares_safe(db, memo, user)
            await db.commit()
            return memo
        except BaseException:
            if moved:
                await to_thread.run_sync(_unlink_paths, moved)
            raise

    if key is None:
        # No key, no dedupe and no recovery: an IntegrityError here is a real fault, exactly as
        # before this feature existed.
        return _out(await attempt(), owner_id)
    memo = await _create_or_replay(
        attempt=attempt,
        lookup=lambda: svc.get_by_idempotency_key(db, owner_id, key),
        rollback=db.rollback,
    )
    return _out(memo, owner_id)


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


@router.patch("/memos/{uid}", response_model=MemoOut, responses=UNLINKABLE_RESPONSE)
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
            # `memo=` is what lets a print uploaded in a LATER edit than its screen bind onto that
            # screen instead of promoting a second visible copy of the same photo (MEMNG-79); on
            # create there is no memo yet, so only this path can carry one.
            try:
                finalized = await uploads_svc.finalize_pending(
                    db, body.upload_uids, user.id, moved_out=moved, memo=memo
                )
            except uploads_svc.UploadsUnlinkable as exc:
                # The edits applied above are still uncommitted: this raise leaves the session to be
                # rolled back by get_session, so a refused edit changes nothing at all.
                raise _unlinkable(exc) from None
            await svc.link_attachments(db, user.id, memo, finalized, moved_out=moved)
        # Enqueue after linking (new uploads are linked above) AND after update_memo re-parsed tags,
        # so both "added #curated on edit" and "added a new photo to a curated memo" are caught.
        # Savepoint-isolated so a feature error can't fail the memo save.
        await _enqueue_frame_shares_safe(db, memo, user)
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
