import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import (
    CREATE_KEY_CONSTRAINT,
    EDIT_KEY_CONSTRAINT,
    Attachment,
    AttachmentStatus,
    Memo,
    Upload,
    User,
    Visibility,
)
from app.routers import RequestVisibility
from app.routers.assets import RenditionItem, has_print, rendition_items
from app.services import asset_paths, reclaim, storage
from app.services import frame_share as frame_share_svc
from app.services import memos as svc
from app.services import uploads as uploads_svc
from app.services.asset_paths import web_ready_uids
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


def _restore_moved(moves: list[storage.TrackedMove]) -> None:
    """Put every original a failed write moved back where it came from.

    The DB rollback returns each upload row to pending, so the bytes must return to the part file
    that row names — otherwise the row promises a file that is gone, and the client's only copy is
    destroyed. Deleting them instead is safe ONLY if the write can never be retried; a keyed create
    retries by design (MEMNG-110), and it re-reads exactly these part files.

    Reversed, because a freshly finalized master moves TWICE — upload part → originals/_unfiled/ →
    the memo folder — and undoing the last hop first is what walks the bytes back to the part path.
    A restore that fails is logged and LEFT IN PLACE: a file in the wrong directory can still be
    recovered by hand, an unlinked one cannot.

    Called INLINE rather than through `to_thread.run_sync`, for the same reason
    `_discard_poster_copies` is: the caller may be unwinding a CANCELLED request, where an await is
    not guaranteed to resume and this would simply be skipped, leaving the originals of a
    rolled-back write scattered across `_unfiled/` and a memo folder that no longer exists
    (MEMNG-145). Each move arbitrates with its own placement under a lock, so this is safe to call
    while a worker thread is still mid-rename. The AST guard in tests/test_no_blocking_io.py cannot
    see the filesystem work here — it is a method on a class imported from another module — so it
    is named here instead of hidden behind a marker."""
    for move in reversed(moves):
        unrestored = move.restore()
        if unrestored is not None:
            logger.error("memo.write.restore_failed", src=move.src, dest=unrestored)


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


async def _after_commit_seam() -> None:
    """The instant between a detaching request's commit and its after-commit reclaim.

    Production opens that gap by itself — the commit is an await, the edit route unlinks the
    uploads it consumed off the loop before the reclaim loop starts, and each reclaim awaits the
    database in turn — but only by timing, never on demand. And it is exactly the window the epoch
    fence exists for: a standalone finalize that dedups onto one of the rows this request just
    detached adopts it there, and a reclaim that did not carry its own detach instant would take
    the adopter's bytes. So the gap is also a named no-op the interleaving witnesses can hold
    open, rather than a race no test can time.
    """
    return None


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
    # What the FILE's own metadata said about when it was taken, as opposed to `captured_at`
    # (AttachmentMeta), which falls back to the browser's modification time and then to the upload
    # time and so always holds something. All three are null when the file said nothing.
    # `capture_civil` is a wall-clock reading the file recorded no zone for — only the reader knows
    # the zone to resolve it in, which is why it travels unresolved and why a date resolved from it
    # is approximate. Carried on every ref so a reader holding the memo can tell the memo's own
    # date apart from a guess without a round trip per attachment.
    capture_instant: Rfc3339Datetime | None
    capture_civil: str | None
    capture_source: str | None
    mine: bool  # the requesting user owns it (→ may delete); shared media is view-only
    hasPrint: bool  # has a print master of any kind (legacy print_path OR a rendition)
    hasLegacyPrint: bool  # has the legacy single print_path → the /print download button applies
    renditions: list[RenditionItem]
    tags: list[str] = Field(
        default=[], description="The picture's own tags, sorted by name; never its memo's tags."
    )
    # This attachment's place in the memo's saved order, 0-based; null on a row written before the
    # order was persisted (MEMNG-262). The list is ALREADY sorted by it — the field is carried so a
    # client can tell a saved order from an incidental one.
    position: int | None
    thumbnailUrl: str
    previewUrl: str
    webUrl: str
    # Whether `webUrl` has a file behind it. `webUrl` is minted for every attachment, but only a
    # video the worker has finished transcoding has a web rendition, so this is always false for
    # anything that is not a video and false for a video until its transcode is published. Read
    # off the filesystem (app/services/asset_paths.py `web_ready_uids`), one batched hop per page.
    webReady: bool
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
    created_at: Rfc3339Datetime
    updated_at: Rfc3339Datetime
    archived_at: Rfc3339Datetime | None
    tags: list[str]
    attachments: list[AttachmentRef] = []
    reactions: list[ReactionSummary] = []
    total_size: int = 0
    # The key the memo was created under (MEMNG-1036), so an outbox whose create got no answer can
    # find it on the timeline. It is the owner's private retry token: null for every other reader.
    idempotency_key: str | None = None


# Bounded and opaque: the shape gate only has to bound what the unique index stores and reject
# junk, so a client is free to use a UUID, a ULID or a random token instead of one id library.
# Shared by both retry handles — MemoCreate.idempotency_key and MemoPatch.edit_key — because the
# storage and the junk-rejection are identical; only what the token NAMES differs.
IdempotencyKey = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


# A dedicated 409 with a machine-readable `code`, NOT the 422 FastAPI already uses for a malformed
# request — a retrying client has to tell "that upload is gone, re-upload the bytes" apart from
# "this request is malformed, never send it again", and a body it cannot classify is how an outbox
# loops forever.
class UploadsUnlinkableDetail(BaseModel):
    """Why a memo write was refused: the requested uploads cannot become attachments.

    The two buckets carry different remedies: `missing_upload_uids` (never existed, or expired and
    reaped — an upload owned by another user reads as missing too) need a fresh upload;
    `incomplete_upload_uids` (fewer bytes received than the declared size) need the transfer
    resumed from HEAD."""

    code: Literal["uploads_unlinkable"]  # required, not defaulted: generated clients switch on it
    missing_upload_uids: list[str]
    incomplete_upload_uids: list[str]


class UploadsUnlinkableError(BaseModel):
    detail: UploadsUnlinkableDetail


class CreateRacedDetail(BaseModel):
    """Why a keyed create could not be resolved: concurrent writes raced it and the memo they
    raced for was deleted before it could be read back. Retrying with the same key is safe — that
    is what the key is for — which is why this is a 5xx the client may repeat and not a 4xx
    telling it to stop."""

    code: Literal["create_raced"]
    message: str


class CreateRacedError(BaseModel):
    detail: CreateRacedDetail


class EditKeyConflictDetail(BaseModel):
    """Why a keyed edit was refused: this owner already used `edit_key` on a DIFFERENT
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
    """Why a keyed edit could not be resolved: the write collided on the edit-key
    constraint, and the edit it collided with was gone by the time it could be read back — the memo
    itself was deleted, taking its recorded edits with it. Retrying with the same key is safe (that
    is what the key is for) and self-correcting: the retry finds no memo and answers 404."""

    code: Literal["edit_raced"]
    message: str


class EditRacedError(BaseModel):
    detail: EditRacedDetail


# The 404 both PATCH and DELETE already answer for a memo that is not there (MEMNG-285).
#
# Deliberately NOT the nested `code`/`message` shape every other error model in this router
# carries. Those bodies are built here, so their shape is ours to choose; this one is not — all
# three raises behind it hand FastAPI's own `HTTPException` a plain string detail, which FastAPI
# serialises as `{"detail": "<string>"}`. Adopting the house family here would declare a body no
# route returns, replacing one contract lie with another.
#
# A comment and not a docstring, because a model docstring is EXPORTED as the component's
# `description` — into openapi.json and from there into every generated client. Rationale written
# for a reader of this file does not belong in a document a client reads, and neither does a
# tracker key. The client-facing wording lives in the response `description` below, and the
# siblings above export no description at all.
class MemoNotFoundError(BaseModel):
    detail: str


MEMO_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "model": MemoNotFoundError,
        "description": "No such memo for this caller: it never existed, or it is another user's "
        "private memo, or it was deleted between this request's read and the memo lock being "
        "granted. Nothing was edited or deleted.",
    }
}


UNLINKABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "model": UploadsUnlinkableError,
        "description": "A requested upload_uid is missing, expired or incomplete. No memo is "
        "created and no attachment is linked — resolve the named uploads and retry.",
    }
}


PATCH_RESPONSES: dict[int | str, dict[str, Any]] = {
    **MEMO_NOT_FOUND_RESPONSE,
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


ATTACHMENT_UID_MESSAGES = {
    "missing": "no attachment of yours has this uid, or the files it names are gone",
    "foreign_owner": "that attachment belongs to another user",
    "other_memo": "that attachment is on a different memo; move it there or leave it out",
    "unresolvable_upload_uid": "that upload is not one this request finalizes; "
    "name it in upload_uids too",
}


def _attachment_uids_invalid(problems: list[tuple[str, str]]) -> HTTPException:
    """A 422 in FastAPI's OWN validation-error shape, for the same reasons `_video_posters_invalid`
    gives: a second body shape under one status leaves a generated client unable to decode either,
    and declaring a model for 422 would REPLACE this operation's HTTPValidationError entry and so
    describe ordinary pydantic failures as something they are not.

    `loc` names the offending uid — which is what the client has to hear, since a status alone
    cannot say which picture it lost — and `type` carries the machine-readable reason, namespaced
    `attachment_uid.<reason>`. The detail is a list because a client fixing its list one round
    trip per bad uid is a client re-sending pictures.
    """
    return HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", "attachment_uids", uid],
                "msg": ATTACHMENT_UID_MESSAGES[reason],
                "type": f"attachment_uid.{reason}",
            }
            for uid, reason in problems
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
    attachment_uids: list[str] | None = Field(
        default=None,
        description=(
            "The memo's COMPLETE picture list, in display order — the index of an entry becomes "
            "that picture's place on the memo. Omitted (null), the pictures are untouched and "
            "this stays a true partial update, exactly as `upload_uids` is; sent as an empty "
            "list, the memo keeps no pictures at all. Those two are different requests and are "
            "answered differently. An attachment the memo has and the list does not name is "
            "REMOVED. An "
            "entry is either an attachment uid or an `upload_uid` named in this same request's "
            "`upload_uids` — a picture being uploaded here has no attachment uid the client could "
            "know, because that uid is minted while this request runs. Two entries may resolve to "
            "one attachment (identical bytes dedup, and a print binds onto its screen); that is "
            "not an error, and the attachment takes the EARLIEST place its entries asked for, so "
            "the stored list can be shorter than the one sent. An entry that cannot be resolved "
            "fails the WHOLE request with 422 — never a silent skip, which on a declared list "
            "would delete the pictures the list omitted. Each rejected entry comes back as one "
            "`detail` item whose `loc` ends with the uid and whose `type` names the reason: "
            "attachment_uid.missing, attachment_uid.foreign_owner, attachment_uid.other_memo or "
            "attachment_uid.unresolvable_upload_uid."
        ),
    )
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


def _att_ref(att: Attachment, user_id: int, web_ready: AbstractSet[str]) -> AttachmentRef:
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
        capture_instant=att.capture_instant,
        capture_civil=att.capture_civil,
        capture_source=att.capture_source,
        mine=att.owner_id == user_id,
        hasPrint=has_print(att),
        hasLegacyPrint=att.print_path is not None,
        renditions=rendition_items(att),
        tags=list(att.picture_tag_names),
        position=att.position,
        **signed_tier_urls(att.uid, user_id, now=now),
        webReady=att.uid in web_ready,
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
    return [ReactionSummary(emoji=e, count=c, reacted=e in mine) for e, c in sorted(counts.items())]


def _out(memo: Memo, user_id: int, web_ready: AbstractSet[str]) -> MemoOut:
    mine = memo.owner_id == user_id
    return MemoOut(
        uid=memo.uid,
        owner=memo.owner.subject,
        mine=mine,
        content=memo.content,
        visibility=memo.visibility,
        pinned=memo.pinned,
        created_at=memo.created_at,
        updated_at=memo.updated_at,
        archived_at=memo.archived_at,
        tags=sorted(t.name for t in memo.tags),
        attachments=[_att_ref(a, user_id, web_ready) for a in memo.attachments],
        reactions=_reactions(memo, user_id),
        # Summed over the same loaded rows `attachments` is built from, so the two never disagree
        # and no extra query is spent.
        total_size=sum(a.size for a in memo.attachments),
        idempotency_key=memo.idempotency_key if mine else None,
    )


async def _render(memo: Memo, user_id: int) -> MemoOut:
    """`_out` for one memo, with its web-rendition check done off the loop first."""
    return _out(memo, user_id, await web_ready_uids(memo.attachments))


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
                "Filter to memos owned by the named household members (`subject`, as "
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
    ready = await web_ready_uids(a for m in items for a in m.attachments)
    return MemoList(
        items=[_out(m, user.id, ready) for m in items], nextCursor=next_cursor, totalCount=total
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
            return await _render(replayed, owner_id)

    async def attempt() -> Memo:
        """One whole create, from finalize to commit. Owns its own `moved` list because it may run
        twice: each try must clean up only the originals IT moved, or a retry that succeeds would
        leave the previous try's files behind with no row referencing them."""
        # Finalize-on-post: pending uploads become attachments now, then link them alongside any
        # already-finalized attachment_uids (back-compat). Abandoned composes leave only Uploads.
        # finalize moves files to disk before the commit — if this try then fails, move those
        # originals BACK, so the rolled-back upload rows still name the bytes they promise.
        moved: list[storage.TrackedMove] = []
        # A poster override is COPIED into the derived-asset tree (a separate mount, so it cannot
        # be renamed there), which leaves BOTH files alive until this transaction settles: see
        # uploads.PosterCopy for which one each outcome removes. Per-attempt like `moved`.
        poster_copies: list[uploads_svc.PosterCopy] = []
        # Part files this attempt CONSUMED without moving — a dedup hit hands back an attachment
        # that already holds these bytes. Their upload rows are deleted transactionally, so the
        # bytes may only be unlinked once the commit makes that deletion real; until then they are
        # what a rolled-back row still promises (MEMNG-142). Per-attempt, like `moved`.
        consumed: list[str] = []
        # attachment uid -> its 0-based place in `upload_uids`, so the memo's attachments are
        # linked (and numbered) in the order the composer DECLARED rather than in the order the
        # two finalize passes happened to produce them (MEMNG-262). Per-attempt, like `moved`.
        positions: dict[str, int] = {}
        # The line between "this write may still be undone" and "this memo is stored". Everything
        # the failure path does — discarding posters, moving originals back — destroys files that
        # a COMMITTED memo owns, so it must never run once this is set.
        committed = False
        try:
            # Finalizing runs INSIDE create_memo, after the memo row is inserted and locked, so
            # this write takes the same outermost lock as every other one (see create_memo). The
            # closure is the only way to sit between those two steps without the router owning the
            # insert.
            async def finalize_uploads(memo: Memo) -> list[str]:
                # The ids behind the back-compat `attachment_uids`, resolved UNLOCKED and handed to
                # pass 0 so its ONE ascending attachment acquisition covers them too (MEMNG-267
                # R3.4). `link_attachments` takes these rows a moment later; without them in pass 0
                # that is a SECOND acquisition of the attachment class in this transaction, in an
                # order nothing else shares — and R7a's sweep, which takes its terminal attachments
                # ascending, inverts against exactly that. Owner-scoped exactly as
                # `link_attachments`' own predicate is, so a uid this caller does not own resolves
                # to nothing here and is skipped there, as it always was. The read DECIDES nothing:
                # a row that disappears before the lock is simply not locked, and link_attachments
                # then finds it gone and skips it.
                named_ids = (
                    list(
                        (
                            await db.execute(
                                select(Attachment.id).where(
                                    Attachment.uid.in_(body.attachment_uids),
                                    Attachment.owner_id == owner_id,
                                )
                            )
                        ).scalars()
                    )
                    if body.attachment_uids
                    else []
                )
                # `or body.video_posters`: a mapping sent with no upload_uids at all names no video
                # of this memo, and must be answered as the mistake it is rather than dropped in
                # silence because the batch looked empty.
                # No `memo=`, and that is the difference from the PATCH path rather than an
                # omission: `memo=` is what lets a print bind onto a screen the memo ALREADY
                # carries, and a memo being created carries nothing yet — this very call is what
                # produces its first attachments. Passing the half-built memo would offer
                # `bind_print_to_sibling` a sibling set that cannot exist.
                finalized = (
                    await uploads_svc.finalize_pending(
                        db,
                        body.upload_uids,
                        owner_id,
                        moved_out=moved,
                        video_posters=body.video_posters,
                        poster_copies_out=poster_copies,
                        consumed_out=consumed,
                        positions_out=positions,
                        named_attachment_ids=named_ids,
                    )
                    if body.upload_uids or body.video_posters
                    else []
                )
                # Sorted back into the DECLARED order: `finalized` comes out in pass order
                # (screens, then promoted prints), while it is the REQUEST order that becomes each
                # attachment's saved `position`. Stable, so a uid the map does not name keeps its
                # relative place at the end.
                return sorted(finalized, key=lambda uid: positions.get(uid, len(finalized)))

            try:
                memo = await svc.create_memo(
                    db,
                    user,
                    content=body.content,
                    visibility=visibility,
                    pinned=body.pinned,
                    attachment_uids=body.attachment_uids,
                    created_at=body.created_at,
                    moved_out=moved,  # placement into the memo folder appends here → undone on fail
                    idempotency_key=key,
                    finalize=finalize_uploads,
                )
            except uploads_svc.UploadsUnlinkable as exc:
                raise _unlinkable(exc) from None
            except uploads_svc.VideoPostersInvalid as exc:
                raise _video_posters_invalid(exc) from None
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
            # Posters FIRST, and both of these synchronously. The attachments they belong to are
            # rolled back with this write, so the placed files would be orphans under uids nothing
            # references — and an `await` here is not guaranteed to resume when this handler is
            # unwinding a cancellation, so anything sequenced behind one can be skipped entirely.
            # Their part files stay untouched: that is what leaves the client's poster re-postable.
            if poster_copies:
                _discard_poster_copies(poster_copies)
            if moved:
                _restore_moved(moved)
            raise

    if key is None:
        # No key, no dedupe and no recovery: an IntegrityError here is a real fault, exactly as
        # before this feature existed.
        return await _render(await attempt(), owner_id)

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
    return await _render(memo, owner_id)


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
    return await _render(await _require_owned(db, user, uid), user.id)


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


async def _judge_picture_list(
    db: AsyncSession,
    owner_id: int,
    memo_id: int,
    entries: Sequence[str],
    upload_uids: Collection[str],
) -> tuple[list[str], dict[str, str]]:
    """Refuse every entry this list cannot resolve, and hand back the entries in declared order
    together with the attachment each already-existing one names.

    With uploads this runs at the END OF FINALIZATION'S PASS 0 — after the one ascending attachment
    acquisition, before pass 1 moves a byte. A list-only edit finalizes nothing, so it calls this
    straight after its own `acquire_attachment_set`: the same point in the same order, rows held
    and nothing moved. Either way a refused request has nothing on disk to put back. Taken
    any later, the same deterministic 422 could fire after a sibling print bind had replaced a
    committed master in place: those bytes are overwritten, no tracked move names them, and the
    upload row the rollback restores promises a part file that replacement consumed.

    An entry is an attachment uid or an `upload_uid` of this same request. An entry of the second
    kind cannot be refused here and must not be: finalization has by now proved every uid in
    `upload_uids` to be a complete, owner-scoped, locked pending row of this owner's, so what it
    becomes is a fact finalization settles later — a screen in pass 1, a print in pass 2, where it
    binds to a sibling or is promoted — not a verdict about the list. The reverse — an entry naming
    an upload this request never finalizes — is a pure set-membership question and is answered
    here.

    Everything unresolvable is collected and refused TOGETHER: the request fails whole either way,
    and a client fixing its list one uid per round trip is a client re-sending pictures. The
    lookup is owner-AGNOSTIC and takes no lock — it has to be able to say that a uid is someone
    else's, and answering that must not queue behind a stranger's write. The owner's own rows were
    covered by the acquisition, so for them this read returns what the lock returned.
    """
    wanted = {entry for entry in entries if entry not in upload_uids}
    rows: dict[str, Attachment] = {}
    if wanted:
        rows = {
            att.uid: att
            for att in (
                await db.execute(select(Attachment).where(Attachment.uid.in_(wanted)))
            ).scalars()
        }
    # A uid that is no attachment of anyone's may still be an upload of this owner's — one they
    # forgot to name in `upload_uids`. Saying so is the difference between a client adding one
    # field and a client hunting a picture it thinks the server lost.
    stranded: set[str] = set()
    unknown = wanted - rows.keys()
    if unknown:
        stranded = set(
            (
                await db.execute(
                    select(Upload.upload_uid).where(
                        Upload.upload_uid.in_(unknown), Upload.owner_id == owner_id
                    )
                )
            ).scalars()
        )
    problems: list[tuple[str, str]] = []
    bound: dict[str, str] = {}
    for entry in dict.fromkeys(entries):  # the same entry twice asks for one place, not two
        if entry in upload_uids:
            continue
        att = rows.get(entry)
        if att is None:
            problems.append((entry, "unresolvable_upload_uid" if entry in stranded else "missing"))
            continue
        if att.owner_id != owner_id:
            problems.append((entry, "foreign_owner"))
            continue
        if att.memo_id is not None and att.memo_id != memo_id:
            # The one `link_attachments` skips in silence, which is right for an append and
            # catastrophic for a declared list: the skip would answer 200 to a list one picture
            # short, having already detached every picture that list left out.
            problems.append((entry, "other_memo"))
            continue
        if att.memo_id is None and not await asset_paths.adoptable(att):
            # The owner's row, free to be linked, missing one of the files only the upload could
            # have supplied: its original, a print master, a rendition master, or, for a video
            # that has an original, its poster.
            # `link_attachments` declines to adopt it and `_apply_picture_list` turns that into
            # this same `missing`; asking here instead is answer-identical, because nothing
            # between this point and the link removes bytes a listed attachment owns — pass 1
            # replaces and places, never deletes, and consumed part files go after the commit —
            # and the row is held under the acquisition's lock until then.
            problems.append((entry, "missing"))
            continue
        bound[entry] = att.uid
    if problems:
        raise _attachment_uids_invalid(problems)
    return list(dict.fromkeys(entries)), bound


def _order_picture_list(
    entries: Sequence[str],
    upload_uids: Collection[str],
    resolved: Mapping[str, str],
    bound: Mapping[str, str],
) -> list[str]:
    """The attachment uids the judged list names, in the order it names them.

    `resolved` is what finalization made of the entries naming an upload of this request; `bound`
    is what `_judge_picture_list` made of the rest. A later entry resolving to an attachment an
    earlier one already reached collapses onto that earlier place: dedup and print binding both do
    that behind the client's back, and refusing would be asking the client to know in advance
    something the server discovers mid-request.
    """
    problems: list[tuple[str, str]] = []
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in upload_uids:
            attachment_uid = resolved.get(entry)
            if attachment_uid is None:
                # No request reaches this: `resolved` is total over `upload_uids`, and an entry
                # naming an upload this request does NOT finalize was refused before finalization
                # ran. It stays as the fence over that totality — a finalization branch that stops
                # recording what an upload became would otherwise silently shorten the list,
                # and this is where a mutation of that recording lands.
                problems.append((entry, "unresolvable_upload_uid"))
                continue
        else:
            attachment_uid = bound[entry]
        if attachment_uid not in seen:
            seen.add(attachment_uid)
            ordered.append(attachment_uid)
    if problems:
        raise _attachment_uids_invalid(problems)
    return ordered


async def _apply_picture_list(
    db: AsyncSession,
    owner_id: int,
    memo: Memo,
    ordered: Sequence[str],
    vouched: Collection[str],
    moved: list[storage.TrackedMove],
) -> tuple[list[tuple[int, str]], datetime | None]:
    """Make the memo hold exactly `ordered`, in that order; hand back what came off it and WHEN.

    What the list omits is DETACHED, never deleted here: the files go in their own transaction
    after this one commits, and a crash in the gap has to leave a row that still names them (see
    app/services/reclaim.py). The ids and uids are read out now, while this transaction still holds
    the rows: the reclaim that follows the commit deletes them, and an instance read after that
    carries only what this transaction loaded.
    """
    refused: list[str] = []
    await svc.link_attachments(
        db, owner_id, memo, list(ordered), moved_out=moved, vouched=vouched, refused_out=refused
    )
    if refused:
        # A row the list named that `adoptable` refuses: one of the non-regenerable files it
        # names is gone. `_judge_picture_list` asks this of every listed entry that already IS an
        # attachment off a memo — the only rows `link_attachments` stats either: the attachment an
        # entry this request finalizes became arrives VOUCHED, and a vouched uid is deliberately
        # never stat'd (this transaction minted that row, or deduped onto a candidate pass 0 had
        # already locked and proved adoptable — and a video finalized a moment ago has no poster
        # tier yet, so stat'ing it would refuse the upload that created it), and a row already on
        # this memo is kept, not adopted. So reaching it here means the bytes went AFTER that
        # row was locked and held, which nothing in this transaction does. It stands as the
        # write-time fence: a declared
        # list cannot let `link_attachments`'s silent skip pass, or it would answer 200 to a
        # request whose result is one picture short.
        raise _attachment_uids_invalid([(uid, "missing") for uid in refused])
    on_memo = (
        (await db.execute(select(Attachment).where(Attachment.memo_id == memo.id))).scalars().all()
    )
    rank = {uid: place for place, uid in enumerate(ordered)}
    # Renumbered from the list, not shifted: `link_attachments` appends what it linked AFTER the
    # attachments the memo already carried, which is the right rule for an append and the wrong one
    # for a list that declares where everything goes.
    for place, att in enumerate(
        sorted((a for a in on_memo if a.uid in rank), key=lambda a: rank[a.uid])
    ):
        att.position = place
    taken_off = [a for a in on_memo if a.uid not in rank]
    detached_at = svc.detach_attachments(taken_off) if taken_off else None
    pairs = [(a.id, a.uid) for a in taken_off]
    await db.flush()
    return pairs, detached_at


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
            return await _render(_replayed_or_conflict(replayed, memo_id, key), owner_id)

    async def attempt() -> Memo:
        """One whole edit, from recording the key to commit. Owns its own `moved` list: if the write
        does not land, every original it moved goes back where it came from."""
        # Same finalize-on-post pattern as create: new uploads become attachments and link to this
        # memo; if the patch rolls back, unlink the moved originals so they don't leak.
        moved: list[storage.TrackedMove] = []
        # Same reason as on create: a dedup hit's part file belongs to an upload row this edit may
        # still roll back, so it is unlinked after the commit, never before (MEMNG-142).
        consumed: list[str] = []
        # Same as on create: the declared order of the files this edit adds (MEMNG-262). They are
        # appended after the attachments the memo already carries, which keep their saved order.
        positions: dict[str, int] = {}
        # The pictures the declared list takes OFF this memo, carried past the commit as plain
        # values: each reclaim below ends a transaction of its own, and the row behind an instance
        # may be gone by the time the next one is read. The instant is the epoch each of those
        # reclaims is judged against, and nothing re-derives it.
        detached_ids: list[tuple[int, str]] = []
        detached_at: datetime | None = None
        committed = False
        try:
            # The FIRST lock this edit takes, before any upload is finalized or any attachment is
            # linked: every later lock belongs to something hanging off this memo, so they all
            # nest under it and two edits of one memo can neither deadlock nor both allocate the
            # same attachment position (see services.memos.lock_memo_for_write).
            # It re-reads as well as locks: this edit read the memo BEFORE queueing for the
            # lock, so everything it holds is the winner's before-picture, and patching that is a
            # lost update SQLAlchemy cannot see (see lock_memo_for_write). A distinct name, since
            # the enclosing handler's `memo` is what the retry loop re-enters with.
            locked = await svc.lock_memo_for_write(db, memo)
            if locked is None:
                # The memo was deleted between this request's unlocked read and the lock being
                # granted — the interleaving the memo lock exists to order (MEMNG-267 R6a).
                # Raised HERE, ahead of the edit key and ahead of every byte, so the client's key
                # stays free for a retry and the answer is the one this API already gives for a
                # memo that is not there. `_apply_or_replay` catches IntegrityError alone, so it
                # travels out untouched.
                raise HTTPException(status_code=404, detail="Memo not found")
            if key is not None:
                # Recorded BEFORE any work: the loser of a same-key race blocks on the unique
                # constraint and fails here, having moved no bytes — so it has none to move back.
                # It lands in the same transaction as the edit, so the two commit or roll back
                # together and a recorded key never outlives the edit it names.
                await svc.record_edit_key(db, locked, owner_id, key)
            fields = body.model_dump(
                exclude_unset=True, exclude={"upload_uids", "attachment_uids", "edit_key"}
            )
            edited = await svc.update_memo(db, locked, **fields)
            # The ids behind the existing uids the list names, resolved UNLOCKED and owner-scoped
            # exactly as create resolves its own, so the ONE ascending attachment acquisition
            # below covers them too. The link, the renumber and the detaches write these rows a
            # moment later; taking them THERE would be a second acquisition of the attachment
            # class in this transaction, in an order nothing else shares — and the worker's media
            # recovery, which takes its terminal attachments ascending, inverts against exactly
            # that. The read DECIDES nothing: a row that disappears before the lock is simply not
            # locked, and the resolution below finds it gone and refuses the request.
            named_ids = (
                list(
                    (
                        await db.execute(
                            select(Attachment.id).where(
                                Attachment.uid.in_(body.attachment_uids),
                                Attachment.owner_id == owner_id,
                            )
                        )
                    ).scalars()
                )
                if body.attachment_uids
                else []
            )
            finalized: list[str] = []
            # upload_uid -> the attachment it produced, deduped onto, or was bound to: the only
            # way a list can name a picture this same request is uploading.
            resolved: dict[str, str] = {}
            named_upload_uids = set(body.upload_uids or ())
            # What the judgement leaves for the placement below: the declared entries deduped, in
            # order, and the attachment each already-existing one names.
            judged: list[str] = []
            bound: dict[str, str] = {}

            async def judge_the_list() -> None:
                # Every refusal the declared list can earn, taken while the request has changed
                # nothing on disk. Its one correct moment is the end of finalization's pass 0: the
                # rows are locked and revalidated by then and nothing has moved a file yet — and
                # pass 2 REPLACES a print master in place when a print binds to a memo sibling,
                # which no rollback undoes.
                nonlocal judged, bound
                judged, bound = await _judge_picture_list(
                    db, owner_id, memo_id, body.attachment_uids or (), named_upload_uids
                )

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
                        positions_out=positions,
                        resolved_out=resolved,
                        named_attachment_ids=named_ids,
                        after_acquisition=(
                            judge_the_list if body.attachment_uids is not None else None
                        ),
                    )
                except uploads_svc.UploadsUnlinkable as exc:
                    # The edits applied above are still uncommitted: this raise leaves the session
                    # to be rolled back by get_session, so a refused edit changes nothing at all —
                    # the recorded key included, which keeps it free for the client's real retry.
                    raise _unlinkable(exc) from None
                await svc.link_attachments(
                    db,
                    owner_id,
                    edited,
                    sorted(finalized, key=lambda uid: positions.get(uid, len(finalized))),
                    moved_out=moved,
                    vouched=set(finalized),
                )
            elif body.attachment_uids is not None:
                # A list-only edit finalizes nothing, so nothing else asks for the acquisition —
                # and it still links, renumbers and detaches attachments. The rows have to be held
                # before the first of those writes, in the same ascending order as everywhere else.
                await uploads_svc.acquire_attachment_set(db, memo=edited, attachment_ids=named_ids)
                # The same judgement, at the same point in the same order: right after the
                # acquisition, before the link, the renumber or the detaches write anything.
                await judge_the_list()
            if body.attachment_uids is not None:
                # After the uploads: a uid this request is finalizing has to BE an attachment
                # before the list can place it, and `resolved` is what says which one it became.
                detached_ids, detached_at = await _apply_picture_list(
                    db,
                    owner_id,
                    edited,
                    _order_picture_list(judged, named_upload_uids, resolved, bound),
                    set(finalized),
                    moved,
                )
            # Enqueue after linking (new uploads are linked above) AND after update_memo re-parsed
            # tags, so both "added #curated on edit" and "added a new photo to a curated memo" are
            # caught. Savepoint-isolated so a feature error can't fail the memo save.
            await _enqueue_frame_shares_safe(db, edited, user)
            await db.commit()
            committed = True
            await _after_commit_seam()
            if consumed:
                await to_thread.run_sync(_unlink_all, consumed)
            # PHASE 2, exactly as the delete route does it: the edit is durable, and only now does
            # each detached row get reclaimed, in its own transaction, against the instant that
            # detached it. Anything else — the `finally` below, or a reclaim before the commit —
            # would let a file this request could not delete undo an edit the user was told was
            # saved.
            for attachment_id, attachment_uid in detached_ids:
                # The shared unit, in a task of its own: this request's session must not be what
                # holds the row, or a cancellation arriving while its files are being unlinked off
                # the loop would close that session and put the row back — adoptable, with a thread
                # still deleting its bytes. A cancellation here ends the loop (the shield re-raises
                # it); the row in flight finishes under its own lock and the shutdown awaits it.
                result = await reclaim.reclaim_row_shielded(
                    attachment_id, reclaim.Revalidate.ORPHANED, detached_at=detached_at
                )
                if result.outcome != "reclaimed":
                    # The edit IS saved, so this cannot become an error response. A reclaim that
                    # did not take the row changed nothing about it; reclaim logs the skip and the
                    # unlink failure itself, but a row that had already vanished it reports
                    # nowhere, and neither tells which memo the row came off — so the line names
                    # both uids to leave it findable.
                    logger.warning(
                        "memo.patch.attachment_not_reclaimed",
                        memo_uid=uid,
                        attachment_id=attachment_id,
                        attachment_uid=attachment_uid,
                        outcome=result.outcome,
                        failed_paths=list(result.failed_paths),
                    )
            await db.refresh(edited)
            return edited
        finally:
            # Inline, like the create path: an unwinding cancellation would skip anything behind an
            # await, leaving this edit's originals where the rollback cannot find them (MEMNG-145).
            if not committed and moved:
                _restore_moved(moved)

    if key is None:
        # No key, no dedupe and no recovery: an IntegrityError here is a real fault, exactly as
        # before this feature existed.
        return await _render(await attempt(), owner_id)

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
    return await _render(settled, owner_id)


@router.delete("/memos/{uid}", status_code=204, responses=MEMO_NOT_FOUND_RESPONSE)
async def delete_memo(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    memo = await _require_owned(db, user, uid)
    if memo.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your memo")
    # M FIRST, before this transaction touches anything else (MEMNG-267 R6). The ORM delete below
    # emits `UPDATE attachments SET memo_id=NULL` and only then `DELETE FROM memos`, so left to
    # itself this request takes the attachment rows before the memo row — the protocol's order
    # backwards, and exactly what a PATCH of the same memo (which holds M and then reaches for
    # those attachments) deadlocks against. Taking the memo lock at the head puts the delete on
    # the same M → A path as every edit, and it costs nothing: everything after it is downstream.
    locked = await svc.lock_memo_for_write(db, memo)
    if locked is None:
        # A concurrent delete of this memo committed while this one queued for the lock. It won:
        # there is no row left to delete and no attachment left to reclaim, and saying so is
        # honest where a 204 would report work this request did not do (R6a).
        raise HTTPException(status_code=404, detail="Memo not found")
    # PHASE 1: the memo goes and its attachments are left ORPHANED, on purpose. Deleting their rows
    # here would put this request one crash away from files that no row names any more; an orphan
    # row is the durable journal that survives the crash and is reconcilable afterwards
    # (app/services/reclaim.py states the principle in full).
    # The LOCKED memo, not the one read before the wait: its `attachments` were reloaded through
    # the lock, so the orphan journal names what the row actually carries now (§3.3).
    orphaned, detached_at = await svc.delete_memo(db, locked)
    await db.commit()
    await _after_commit_seam()
    # PHASE 2: now reclaim each of them properly — jobs, then the row locked and revalidated, then
    # its files, then the row. Each id gets its own transaction, so a failure part way through
    # leaves the rest as detached rows the sweep finishes once their grace elapses, rather than
    # rolling the memo delete back. Every one of them is judged against THIS delete's detach
    # instant: between the commit above and the lock below a standalone upload can dedup onto one
    # of these rows and clear the stamp, and those bytes are live media this loop must not take.
    for attachment_id in orphaned:
        # In a task of its own, holding its own session, for the reason the edit route states: a
        # cancelled request must not be able to release the row while the thread unlinking its
        # files runs on.
        result = await reclaim.reclaim_row_shielded(
            attachment_id, reclaim.Revalidate.ORPHANED, detached_at=detached_at
        )
        if result.outcome != "reclaimed":
            # The memo IS deleted — that is what the caller asked for and it has committed, so
            # this cannot become an error response. A reclaim that did not take the row changed
            # nothing about it; reclaim logs the skip and the unlink failure itself, but a row
            # that had already vanished it reports nowhere, and neither of its lines says which
            # memo delete asked for the row — which is what this one is for.
            logger.warning(
                "memo.delete.attachment_not_reclaimed",
                memo_uid=uid,
                attachment_id=attachment_id,
                outcome=result.outcome,
                failed_paths=list(result.failed_paths),
            )


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
    return await _render(memo, user.id)


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
    return await _render(memo, user.id)


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
    return await _render(memo, user.id)


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
    return await _render(memo, user.id)


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
