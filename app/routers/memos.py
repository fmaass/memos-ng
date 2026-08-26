import os
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
from app.models import (
    CREATE_KEY_CONSTRAINT,
    EDIT_KEY_CONSTRAINT,
    Attachment,
    AttachmentStatus,
    Memo,
    User,
    Visibility,
)
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


def _restore_moved(moves: list[tuple[str, str]]) -> None:
    """Put every original a failed write moved back where it came from (off-loop).

    The DB rollback returns each upload row to pending, so the bytes must return to the part file
    that row names — otherwise the row promises a file that is gone, and the client's only copy is
    destroyed. Deleting them instead is safe ONLY if the write can never be retried; a keyed create
    retries by design (MEMNG-110), and it re-reads exactly these part files.

    Reversed, because a freshly finalized master moves TWICE — upload part → originals/_unfiled/ →
    the memo folder — and undoing the last hop first is what walks the bytes back to the part path.
    A restore that fails is logged and LEFT IN PLACE: a file in the wrong directory can still be
    recovered by hand, an unlinked one cannot."""
    for src, dest in reversed(moves):
        try:
            destination = Path(dest)
            if not destination.exists():
                continue  # never moved, or already restored
            source = Path(src)
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        except OSError as exc:
            logger.error(
                "memo.write.restore_failed", src=src, dest=dest, error=str(exc)[:200]
            )


def _discard_poster_copies(copies: list[uploads_svc.PosterCopy]) -> None:
    """Take back every poster this write copied into place, because it did not land.

    Called INLINE rather than through `to_thread.run_sync`, and this is the one place in this
    module where that is the point. The caller may be unwinding a CANCELLED request — a client
    that hung up, a server shutting down — and an await in that state is not guaranteed to resume:
    the cleanup would be skipped and the poster left orphaned under a uid the rollback has already
    erased. A synchronous call cannot be cut short (same reasoning as the transcode's inline temp
    sweep in app/services/media.process_slow). It costs an uncontended lock and at most two
    unlinks, except when it lands mid-copy, where it waits out a bounded sub-second copy on a
    request that is already dead.

    The AST guard in tests/test_no_blocking_io.py cannot see this one — it resolves blocking
    helpers within a module, and these are methods on a class imported from another — so it is
    named here instead of hidden behind a marker.
    """
    for copy in copies:
        for path in copy.discard():
            logger.error("memo.write.poster_discard_failed", path=path)


def _unlink_all(paths: list[str]) -> None:
    """Remove files a memo write no longer has any use for (off-loop). Never raises.

    Runs on both sides of the commit — after one that already landed, where an OSError must not
    turn a stored memo into a 500, and inside the failure path, where it must not replace the error
    that got us there. A file it cannot remove is logged and left; the alternative is losing the
    real outcome to a cleanup problem.
    """
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            logger.error("memo.write.cleanup_failed", path=path, error=str(exc)[:200])


class AttachmentRef(BaseModel):
    uid: str
    mime: str
    status: AttachmentStatus
    blurhash: str | None
    width: int | None
    height: int | None
    duration: float | None
    # Content identity of the original bytes, same field and same NULL story as
    # AttachmentMeta.sha256 (app/routers/assets.py) — carried here too so a caller that already
    # holds the memo does not need a per-attachment round trip to check it. Off the ORM row, no
    # extra query.
    sha256: str | None
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
# Shared by both retry handles — MemoCreate.idempotency_key and MemoPatch.edit_key — because the
# storage and the junk-rejection are identical; only what the token NAMES differs.
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


class EditKeyConflictDetail(BaseModel):
    """Why a keyed edit was refused (MEMNG-124): this owner already used `edit_key` on a DIFFERENT
    memo. Both alternatives to refusing are wrong — replaying would answer a PATCH of one memo with
    another memo's body (a uid the client never asked about), and applying would leave one key
    naming two edits, after which neither could be replayed. `memo_uid` names the memo the key is
    bound to so a client can tell a stale key apart from a mis-generated one. Retrying is pointless:
    the fix is a fresh key, which is why this is a 4xx and not the 503 a raced edit gets."""

    code: Literal["edit_key_conflict"]  # required, not defaulted: generated clients switch on it
    memo_uid: str
    message: str


class EditKeyConflictError(BaseModel):
    detail: EditKeyConflictDetail


class EditRacedDetail(BaseModel):
    """Why a keyed edit could not be resolved (MEMNG-124): the write collided on the edit-key
    constraint, and the edit it collided with was gone by the time it could be read back — the memo
    itself was deleted, taking its recorded edits with it. Retrying with the same key is safe (that
    is what the key is for) and self-correcting: the retry finds no memo and answers 404."""

    code: Literal["edit_raced"]
    message: str


class EditRacedError(BaseModel):
    detail: EditRacedDetail


UNLINKABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "model": UploadsUnlinkableError,
        "description": "A requested upload_uid is missing, expired or incomplete. No memo is "
        "created and no attachment is linked — resolve the named uploads and retry.",
    }
}


PATCH_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": UploadsUnlinkableError | EditKeyConflictError,
        "description": "Either a requested upload_uid is missing, expired or incomplete "
        "(`uploads_unlinkable`), or the `edit_key` is already bound to a different memo "
        "(`edit_key_conflict`). Switch on `detail.code`. Nothing is edited and no attachment is "
        "linked in either case.",
    },
    503: {
        "model": EditRacedError,
        "description": "Concurrent writes raced this keyed edit and the edit they raced for is "
        "gone. Retry the same request, edit_key included.",
    },
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


# One sentence per refusal category (uploads_svc.VideoPosterReason), rendered into the `msg` of a
# validation error. The categories themselves are the contract — a client switches on `type`, never
# on this prose — so these exist to make a 422 readable in a log or a network tab.
VIDEO_POSTER_MESSAGES: dict[uploads_svc.VideoPosterReason, str] = {
    "video_not_uploaded": "no such upload in upload_uids: a poster belongs to a video this memo "
    "is posting",
    "video_not_a_video": "that upload is not a video, so it has no poster tier",
    "video_is_a_print_variant": "that upload's filename marks it a print variant, so it becomes a "
    "hidden print master rather than an attachment with a poster tier",
    "poster_missing": "no such upload for this account (unknown, expired, or not yours)",
    "poster_incomplete": "the poster upload is not fully received yet — resume it from its "
    "Upload-Offset and retry",
    "poster_not_a_jpeg": "the poster upload's bytes are not a JPEG, which is what the poster tier "
    "is served as",
    "poster_listed_as_attachment": "the poster upload is also listed in upload_uids — it is either "
    "a visible attachment or a video's poster, never both",
    "poster_reused": "that poster upload is already the poster of another video in this request; "
    "upload the image once per video",
}


def _video_posters_invalid(exc: uploads_svc.VideoPostersInvalid) -> HTTPException:
    """A 422 in FastAPI's OWN validation-error shape — `{"detail": [{loc, msg, type}]}` — rather
    than a bespoke body under the same status.

    These are request-shape complaints (this key names no video of this memo, that upload is not a
    JPEG), which is what 422 already means here, and a second body shape under one status is what
    makes a generated client unable to decode either. Reusing the shape also keeps the published
    schema honest: declaring a model for 422 would REPLACE FastAPI's HTTPValidationError entry for
    this operation (fastapi/openapi/utils.py only injects it when no 422 is declared), and the
    schema would then describe ordinary pydantic failures as something they are not.

    The machine-readable part is `type`, namespaced `video_poster.<reason>` — pydantic's `type` is
    already the field a client switches on — and `loc` names the offending video upload_uid, which
    is the key of the mapping the caller sent.
    """
    return HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", "video_posters", problem.video_upload_uid],
                "msg": VIDEO_POSTER_MESSAGES[problem.reason],
                "type": f"video_poster.{problem.reason}",
            }
            for problem in exc.problems
        ],
    )


class MemoCreate(BaseModel):
    content: str
    visibility: RequestVisibility = None  # unset → the user's default_visibility setting
    pinned: bool = False
    # Finalize-on-post: pending upload_uids become attachments only when the memo is created, so
    # an abandoned compose leaves only Uploads (which expire) — never an orphan attachment.
    upload_uids: list[str] = []
    attachment_uids: list[str] = []  # already-finalized attachments (back-compat)
    video_posters: dict[str, str] | None = Field(
        default=None,
        description=(
            "Give a video its own poster frame: `{video upload_uid: poster upload_uid}`. The video "
            "uid must appear in `upload_uids` (it becomes the attachment); the poster uid must NOT "
            "— it is consumed as that video's poster tier and never becomes an attachment of its "
            "own, so it stays out of the feed, the library and the export. Without one, the worker "
            "picks a hero frame ~12% into the clip, which is the wrong frame for a video that "
            "opens on a title card. The poster upload must be a finished JPEG belonging to the "
            "caller. Consumed atomically with the memo: if the post fails, both uploads are still "
            "there to retry with, and a replayed `idempotency_key` answers with the stored memo "
            "without consuming anything a second time."
        ),
    )
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
    edit_key: IdempotencyKey | None = Field(
        default=None,
        description=(
            "Optional client-generated retry token: a UUID, or any opaque 8-64 character token of "
            "[A-Za-z0-9_-], unique per logical edit. Sending it a second time does NOT re-apply "
            "the edit; it answers **200 with the memo's CURRENT state** — which is the state after "
            "any later write, not a snapshot of what this edit produced. The token records that "
            "the edit landed, not what the memo looked like afterwards. Crucially the replay is "
            "answered BEFORE `upload_uids` are checked, so a retry whose uploads the first attempt "
            "already consumed gets its memo instead of a 409. A key already used on a DIFFERENT "
            "memo is refused (409 `edit_key_conflict`); two owners may reuse one token. Without a "
            "key, a retried media edit is refused with 409 `uploads_unlinkable`."
        ),
    )


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
        sha256=att.sha256,
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
    owner: Annotated[
        list[str] | None,
        Query(
            description=(
                "Filter to memos owned by the named household members (`User.subject`, as "
                "published by `GET /api/members`). Repeatable, and repeating it ORs: "
                "`?owner=alice&owner=bob` returns the union of what alice and bob wrote that "
                "the caller may see, never a memo of anyone else's, and never a memo of "
                "theirs the caller could not already see. Composes with every other filter "
                "(they still AND), and must be resent unchanged on each page — like every "
                "filter here, it is not carried inside `cursor`. A single `owner=alice` "
                "behaves exactly as it did before it was repeatable; omitting it filters by "
                "owner not at all."
            )
        ),
    ] = None,
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


def violated_constraint(exc: IntegrityError) -> str | None:
    """The name of the database constraint a write was refused on, or None if the driver won't say.

    Verified against the live stack rather than assumed: SQLAlchemy's asyncpg dialect wraps the
    driver error in its own DBAPI shim, which carries NEITHER `constraint_name` nor psycopg's
    `diag` — the asyncpg `UniqueViolationError`, and the name, are one level down on `__cause__`.
    psycopg2 (the sync test/migration path) instead exposes `diag.constraint_name` on `orig` itself,
    so both shapes are read and the first that answers wins.

    None is the honest answer for a driver-agnostic or fabricated error, and callers must read it as
    "not the constraint I am looking for" — the fail-closed direction, where an unrecognised error
    keeps propagating instead of being absorbed into someone's retry path."""
    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if candidate is None:
            continue
        for name in (
            getattr(candidate, "constraint_name", None),
            getattr(getattr(candidate, "diag", None), "constraint_name", None),
        ):
            if isinstance(name, str) and name:
                return name
    return None


def _is_create_key_collision(exc: IntegrityError) -> bool:
    """True only for a violation of the create-side idempotency-key index (MEMNG-131).

    Read when a keyed create has run out of retries, to decide whether the 503 that tells the
    client to repeat itself with the same key is honest: it is exactly when the key is what kept
    colliding. Unlike the edit side this does NOT gate entry to the retry loop — a foreign conflict
    is still retried there, because it is usually transient and the retry is what saves the memo."""
    return violated_constraint(exc) == CREATE_KEY_CONSTRAINT


def _is_edit_key_collision(exc: IntegrityError) -> bool:
    """True only for a violation of the edit-key uniqueness constraint (MEMNG-124).

    The replay path answers exactly ONE conflict: two patches racing with the same edit_key. A
    keyed patch can raise IntegrityError for reasons that have nothing to do with the key — two
    patches introducing the same new hashtag collide on the tags index — and absorbing those would
    report "concurrent writes raced this edit, retry with the same edit_key" for a conflict that
    retrying cannot fix, while hiding a real fault that surfaced plainly before this feature."""
    return violated_constraint(exc) == EDIT_KEY_CONSTRAINT


async def _apply_or_replay(
    *,
    attempt: Callable[[], Awaitable[Memo]],
    lookup: Callable[[], Awaitable[Memo | None]],
    rollback: Callable[[], Awaitable[None]],
    exhausted: HTTPException,
    replayable: Callable[[IntegrityError], bool],
    key_collision: Callable[[IntegrityError], bool] | None = None,
    tries: int = 2,
) -> Memo:
    """Perform a keyed memo write, resolving a lost race against its unique index.

    Shared by the two keyed writes because the control flow IS the contract and there is exactly
    one of it: collide → roll back → read the winner back → answer with it. What differs between
    create (MEMNG-110) and edit (MEMNG-124) is what the caller passes in — the work, the read-back,
    and the error for an unresolvable race — never the resolution itself, which is why this is one
    function and not two look-alikes drifting apart.

    Losing the race normally means the winner is there to be read back and returned. It can also
    mean the winner was DELETED in the moment between our write failing and our reading it back —
    the key is then free again. Whether a fresh attempt is legitimate then depends on the caller:
    a create passes `tries=2` and takes one more run at it; an edit passes `tries=1`, because a
    freed edit key means the memo itself is gone and there is nothing left to edit.

    `key_collision` decides what an EXHAUSTED loop may claim, defaulting to `replayable`. Running
    out of retries only justifies the caller's 503 — "someone kept beating you to this key, repeat
    yourself" — if the key really is what kept colliding; when the last failure was a foreign
    constraint, that error is re-raised instead, because no amount of repeating the same key can
    resolve it.

    `replayable` decides which IntegrityError is even a lost race. Not every integrity violation a
    keyed write can raise is one — the caller's own work may collide on an unrelated constraint —
    and absorbing one of those into "retry with the same key" would both answer a conflict retrying
    cannot fix and hide a real fault. Anything it rejects is re-raised untouched.

    Bounded rather than looping until success: each extra round needs a concurrent write AND a
    concurrent delete of that write to land inside the same window, so more rounds buy nothing an
    unbounded retry could not turn into a hot loop. Exhausting them on the caller's OWN key raises
    the caller's 503 rather than a bare IntegrityError as a 500, because the client's remedy is
    real there: retrying with the same key is safe by construction, and 5xx is the class our retry
    contract already says to repeat. Exhausting them on anything else is `key_collision`'s call,
    above — that error has no such remedy and is handed back as itself."""
    # Default: the predicate that let a conflict INTO the retry loop is the one that decides what
    # an exhausted loop may claim about it. That is the right reading for a caller whose
    # `replayable` is already narrow (the edit path admits only its own key, so only its own key
    # can ever reach the end); a caller that retries broadly has to say separately what it may
    # claim, which is exactly what the create path passes in.
    claims = key_collision or replayable
    last: IntegrityError | None = None
    for _ in range(tries):
        try:
            return await attempt()
        except IntegrityError as exc:
            if not replayable(exc):
                raise  # someone else's constraint — not a lost race, and not ours to absorb
            last = exc
            await rollback()
            winner = await lookup()
            if winner is not None:
                return winner
    if last is not None and not claims(last):
        # The retries were spent on a conflict that was never about the key, and it outlived them.
        # `exhausted` would answer it with "retry with the same key", which cannot resolve someone
        # else's constraint and would bury a fault that surfaced plainly before keys existed. The
        # error that actually kept happening is the honest answer. An error naming no constraint
        # lands here too: unrecognised is not "mine", the same fail-closed reading
        # `violated_constraint` documents.
        raise last
    raise exhausted


async def _create_or_replay(
    *,
    attempt: Callable[[], Awaitable[Memo]],
    lookup: Callable[[], Awaitable[Memo | None]],
    rollback: Callable[[], Awaitable[None]],
    tries: int = 2,
) -> Memo:
    """Insert a keyed memo, resolving a lost race against the partial unique index (MEMNG-110).

    The create half of `_apply_or_replay`: two tries (a deleted winner frees the key, so a fresh
    insert is legitimate) and the create-flavoured 503, kept here with the create contract it
    describes rather than pushed into the shared loop."""
    return await _apply_or_replay(
        attempt=attempt,
        lookup=lookup,
        rollback=rollback,
        tries=tries,
        # Every IntegrityError is worth a try here, and that breadth is load-bearing (MEMNG-131):
        # a foreign conflict is usually TRANSIENT — two creates inventing the same hashtag at once
        # collide on the tags index, and by the retry the winner's tag is committed — so the retry
        # is what turns a lost race on someone else's constraint into a stored memo rather than a
        # 500 and a memo the user posted but does not have. Narrowing this to the key would delete
        # that self-heal; what is narrowed instead is what an exhausted loop may CLAIM.
        replayable=lambda _: True,
        key_collision=_is_create_key_collision,
        exhausted=HTTPException(
            status_code=503,
            detail=CreateRacedDetail(
                code="create_raced",
                message=(
                    "Concurrent writes raced this create and the memo they raced for was deleted "
                    "before it could be read back. Retry with the same idempotency_key."
                ),
            ).model_dump(),
        ),
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
    # Everything the create reads off the ORM `user`, resolved ONCE here: a rollback inside the
    # retry loop expires every object in the session, and a plain attribute read on an expired one
    # lazy-loads — sync IO in an async context, i.e. MissingGreenlet, not a working read.
    visibility = body.visibility or user.default_visibility  # prod parity: per-user default
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
        # finalize moves files to disk before the commit — if this try then fails, move those
        # originals BACK, so the rolled-back upload rows still name the bytes they promise.
        moved: list[tuple[str, str]] = []
        # A poster override is COPIED into the derived-asset tree (a separate mount, so it cannot
        # be renamed there), which leaves BOTH files alive until this transaction settles: see
        # uploads.PosterCopy for which one each outcome removes. Per-attempt like `moved`.
        poster_copies: list[uploads_svc.PosterCopy] = []
        # Part files this attempt CONSUMED without moving — a dedup hit hands back an attachment
        # that already holds these bytes. Their upload rows are deleted transactionally, so the
        # bytes may only be unlinked once the commit makes that deletion real; until then they are
        # what a rolled-back row still promises (MEMNG-142). Per-attempt, like `moved`.
        consumed: list[str] = []
        # The line between "this write may still be undone" and "this memo is stored". Everything
        # the failure path does — discarding posters, moving originals back — destroys files that
        # a COMMITTED memo owns, so it must never run once this is set.
        committed = False
        try:
            try:
                # `or body.video_posters`: a mapping sent with no upload_uids at all names no video
                # of this memo, and must be answered as the mistake it is rather than dropped in
                # silence because the batch looked empty.
                finalized = (
                    await uploads_svc.finalize_pending(
                        db,
                        body.upload_uids,
                        owner_id,
                        moved_out=moved,
                        video_posters=body.video_posters,
                        poster_copies_out=poster_copies,
                        consumed_out=consumed,
                    )
                    if body.upload_uids or body.video_posters
                    else []
                )
            except uploads_svc.UploadsUnlinkable as exc:
                raise _unlinkable(exc) from None
            except uploads_svc.VideoPostersInvalid as exc:
                raise _video_posters_invalid(exc) from None
            memo = await svc.create_memo(
                db,
                user,
                content=body.content,
                visibility=visibility,
                pinned=body.pinned,
                attachment_uids=[*body.attachment_uids, *finalized],
                created_at=body.created_at,
                moved_out=moved,  # placement into the memo folder appends here → undone on fail
                idempotency_key=key,
            )
            # After all attachments are linked, enqueue frame shares for #curated photos (no-op
            # unless configured). Savepoint-isolated so a feature error can't fail the save.
            await _enqueue_frame_shares_safe(db, memo, user)
            await db.commit()
            # A plain assignment with NO await between it and the commit that earned it: a
            # cancellation cannot be delivered in that gap, so this flag can never be missing for
            # a memo that is stored.
            committed = True
            # AFTER the commit, and only then: each consumed poster's part file — and each part
            # a dedup hit consumed — is now bytes no row names, and nothing else will ever collect
            # it (the expiry reaper walks `uploads` rows, and those are gone). Best-effort by
            # construction — the memo is already stored, so a file that will not delete must not
            # fail the request. The failure path deliberately reaps NEITHER: a part left behind by
            # a write that did not land is exactly what its restored upload row promises.
            reap = [c.part for c in poster_copies] + consumed
            if reap:
                await to_thread.run_sync(_unlink_all, reap)
            return memo
        except BaseException:
            if committed:
                # The memo is STORED, and the only thing that can raise past this point is a
                # cancellation delivered during the part reap above. The cleanups below would then
                # strip media off a LIVE memo — discarding its poster, moving its originals back
                # out of the memo folder — which is far worse than the leak they would be tidying.
                # The worst outcome here is an unreaped part file; the memo is complete without it.
                raise
            # Posters FIRST, and synchronously. The attachments they belong to are rolled back with
            # this write, so the placed files would be orphans under uids nothing references —
            # and the `await` below is not guaranteed to resume when this handler is unwinding a
            # cancellation, so anything sequenced after it can be skipped entirely. Their part
            # files stay untouched: that is what leaves the client's poster re-postable.
            if poster_copies:
                _discard_poster_copies(poster_copies)
            if moved:
                await to_thread.run_sync(_restore_moved, moved)
            raise

    if key is None:
        # No key, no dedupe and no recovery: an IntegrityError here is a real fault, exactly as
        # before this feature existed.
        return _out(await attempt(), owner_id)
    async def rollback_for_retry() -> None:
        await db.rollback()
        # `user` itself outlives the rollback and the next attempt still hands it to create_memo
        # (which reads owner.id) and to the frame-share enqueue (user.id). Reload it HERE, where
        # the load can be awaited, rather than letting those reads lazy-load off an expired object.
        await db.refresh(user)

    memo = await _create_or_replay(
        attempt=attempt,
        lookup=lambda: svc.get_by_idempotency_key(db, owner_id, key),
        rollback=rollback_for_retry,
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


def _replayed_or_conflict(found: Memo, memo_id: int, key: str) -> Memo:
    """The memo a known `edit_key` names — provided it is the memo this request addressed."""
    if found.id == memo_id:
        return found
    raise HTTPException(
        status_code=409,
        detail=EditKeyConflictDetail(
            code="edit_key_conflict",
            memo_uid=found.uid,
            message=(
                f"edit_key {key} was already used on memo {found.uid}. An edit key names one edit "
                "of one memo — use a fresh key for this one."
            ),
        ).model_dump(),
    )


@router.patch("/memos/{uid}", response_model=MemoOut, responses=PATCH_RESPONSES)
async def patch_memo(
    uid: str,
    body: MemoPatch,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MemoOut:
    memo = await _require_owned(db, user, uid)
    # Read the ids off the ORM objects BEFORE anything can roll back: a rollback expires every
    # object in the session, and a later `user.id` / `memo.id` would lazy-refresh — sync IO in an
    # async context, i.e. a MissingGreenlet rather than a working attribute read (MEMNG-110).
    owner_id, memo_id = user.id, memo.id
    if memo.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="Not your memo")
    key = body.edit_key
    if key is not None:
        # The replay lookup comes FIRST — ahead of the uploads (MEMNG-124 before MEMNG-113). By the
        # time a retry arrives its upload_uids are gone: the first attempt finalized them, or they
        # expired. Validating them first would answer 409 to precisely the client the key exists to
        # protect, and the edit it is asking about is already applied, media included.
        replayed = await svc.get_memo_by_edit_key(db, owner_id, key)
        if replayed is not None:
            return _out(_replayed_or_conflict(replayed, memo_id, key), owner_id)

    async def attempt() -> Memo:
        """One whole edit, from recording the key to commit. Owns its own `moved` list: if the write
        does not land, every original it moved goes back where it came from."""
        # Same finalize-on-post pattern as create: new uploads become attachments and link to this
        # memo; if the patch rolls back, unlink the moved originals so they don't leak.
        moved: list[tuple[str, str]] = []
        # Same reason as on create: a dedup hit's part file belongs to an upload row this edit may
        # still roll back, so it is unlinked after the commit, never before (MEMNG-142).
        consumed: list[str] = []
        committed = False
        try:
            if key is not None:
                # Recorded BEFORE any work: the loser of a same-key race blocks on the unique
                # constraint and fails here, having moved no bytes — so it has none to move back.
                # It lands in the same transaction as the edit, so the two commit or roll back
                # together and a recorded key never outlives the edit it names.
                await svc.record_edit_key(db, memo, owner_id, key)
            fields = body.model_dump(exclude_unset=True, exclude={"upload_uids", "edit_key"})
            edited = await svc.update_memo(db, memo, **fields)
            if body.upload_uids:
                # `memo=` is what lets a print uploaded in a LATER edit than its screen bind onto
                # that screen instead of promoting a second visible copy of the same photo
                # (MEMNG-79); on create there is no memo yet, so only this path can carry one.
                try:
                    finalized = await uploads_svc.finalize_pending(
                        db,
                        body.upload_uids,
                        owner_id,
                        moved_out=moved,
                        memo=edited,
                        consumed_out=consumed,
                    )
                except uploads_svc.UploadsUnlinkable as exc:
                    # The edits applied above are still uncommitted: this raise leaves the session
                    # to be rolled back by get_session, so a refused edit changes nothing at all —
                    # the recorded key included, which keeps it free for the client's real retry.
                    raise _unlinkable(exc) from None
                await svc.link_attachments(db, owner_id, edited, finalized, moved_out=moved)
            # Enqueue after linking (new uploads are linked above) AND after update_memo re-parsed
            # tags, so both "added #curated on edit" and "added a new photo to a curated memo" are
            # caught. Savepoint-isolated so a feature error can't fail the memo save.
            await _enqueue_frame_shares_safe(db, edited, user)
            await db.commit()
            committed = True
            if consumed:
                await to_thread.run_sync(_unlink_all, consumed)
            await db.refresh(edited)
            return edited
        finally:
            if not committed and moved:
                await to_thread.run_sync(_restore_moved, moved)

    if key is None:
        # No key, no dedupe and no recovery: an IntegrityError here is a real fault, exactly as
        # before this feature existed.
        return _out(await attempt(), owner_id)

    async def read_back() -> Memo | None:
        found = await svc.get_memo_by_edit_key(db, owner_id, key)
        return None if found is None else _replayed_or_conflict(found, memo_id, key)

    # tries=1: unlike a create, a collision this read-back cannot resolve is NOT retryable here. The
    # only way the key frees itself is the memo being deleted (the record cascades with it), and a
    # second attempt would then be editing a memo that no longer exists. The 503 sends the client
    # back with the same key, and that retry answers the honest 404.
    settled = await _apply_or_replay(
        attempt=attempt,
        lookup=read_back,
        rollback=db.rollback,
        tries=1,
        replayable=_is_edit_key_collision,
        exhausted=HTTPException(
            status_code=503,
            detail=EditRacedDetail(
                code="edit_raced",
                message=(
                    "Concurrent writes raced this edit and the edit they raced for is gone — the "
                    "memo was deleted. Retry with the same edit_key."
                ),
            ).model_dump(),
        ),
    )
    return _out(settled, owner_id)


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
