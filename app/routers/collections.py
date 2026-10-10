"""Collections (trips): the household-shared record, its exclusions and its member read.

Every signed-in member lists, opens and edits every collection, and the last write wins — so no
route here asks who created what. What a route SHOWS is the caller's view: the member read and
`memberCount` go through the library's own filters, the cover is serialised only when the caller
may see that attachment, and an attachment the caller may not see is referenced (as a cover or an
exclusion) exactly as if it did not exist — `_viewable`'s 404, one answer for both.
"""

import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import Attachment, Collection, User
from app.routers.assets import (
    AttachmentListItem,
    _list_item,
    _next_cursor_header,
    _viewable,
    library_page,
)
from app.routers.memos import IdempotencyKey
from app.services import collections as coll_svc
from app.services.asset_paths import web_ready_uids
from app.services.attachments import (
    collection_member_clause,
    library_filters,
    library_page_statement,
)

router = APIRouter(prefix="/api", tags=["collections"])

# Postgres' foreign_key_violation: the exclusion insert names a collection or an attachment that
# was deleted after this request looked it up.
_FK_VIOLATION = "23503"

_DETAIL = {
    "application/json": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}
}
_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No valid session cookie or bearer token.", "content": _DETAIL},
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    404: {
        "description": "No such collection, or the attachment it names does not exist or is not "
        "visible to the caller — one answer for all three. Nothing was changed.",
        "content": _DETAIL,
    },
}


def _tag_name(value: str) -> str:
    if re.fullmatch(r"\w+", value) is None:
        raise ValueError("a tag is a name as a memo's #tag carries it, without the #")
    return value


# A tag is the NAME a memo's `#tag` stores — letters, digits and `_`, case kept, no `#` — and is
# matched exactly. It is refused rather than normalised: a stripped `#` or a lowered case would
# make the stored record differ from what the client sent, and a value no memo can carry would
# silently match nothing.
CollectionTag = Annotated[
    str,
    Field(
        min_length=1,
        max_length=255,
        description="A tag name exactly as memos carry it, without the hash sign: letters, "
        "digits and underscores. Matched exactly, case included.",
    ),
    AfterValidator(_tag_name),
]
Title = Annotated[str, Field(min_length=1, max_length=255)]
SpanBound = Annotated[
    AwareDatetime,
    Field(description="An instant with its UTC offset; a value without one is refused."),
]
Zone = Annotated[str, Field(description="The IANA zone the span was picked in; display only.")]


def _no_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


class CollectionOut(BaseModel):
    uid: str
    title: str
    tag: str | None
    spanStart: Rfc3339Datetime | None
    spanEnd: Rfc3339Datetime | None
    tz: str | None
    cover: AttachmentListItem | None = Field(
        description="The cover as the library lists it, or null when there is none or the caller "
        "may not see it."
    )
    memberCount: int = Field(description="How many members the CALLER can see.")
    createdBy: str | None = Field(description="Who created it; null once that member is gone.")
    createdAt: Rfc3339Datetime
    updatedAt: Rfc3339Datetime


class CollectionCreate(BaseModel):
    title: Title
    tag: CollectionTag | None = None
    spanStart: SpanBound | None = None
    spanEnd: SpanBound | None = None
    tz: Zone | None = None
    coverAttachmentUid: str | None = None
    idempotencyKey: IdempotencyKey | None = Field(
        default=None,
        description="Optional client-generated retry token, unique per logical create. Posting it "
        "again returns the collection the first call stored, with 201 like the original, "
        "whatever the retry's other fields say. Without a key, a retry stores a second collection.",
    )


class CollectionPatch(BaseModel):
    # Absent leaves a field as it is; null clears it — except `title`, which a collection always
    # has, so an explicit null is refused by the schema.
    # The None default is never validated or published: it only marks the field absent for
    # `exclude_unset`, and the schema keeps `title` a plain non-nullable string.
    title: Title = Field(default=None, json_schema_extra=_no_default)  # type: ignore[assignment]
    tag: CollectionTag | None = None
    spanStart: SpanBound | None = None
    spanEnd: SpanBound | None = None
    tz: Zone | None = None
    coverAttachmentUid: str | None = None


# The wire's names for the service's fields; `coverAttachmentUid` is resolved separately.
_FIELDS = {
    "title": "title",
    "tag": "tag",
    "spanStart": "span_start",
    "spanEnd": "span_end",
    "tz": "tz",
}


def _invalid(exc: coll_svc.CollectionInvalid) -> HTTPException:
    """The service's refusal in the shape every other 422 of this API has."""
    return HTTPException(
        status_code=422, detail=[{"loc": ["body"], "msg": str(exc), "type": "value_error"}]
    )


def _collection_gone() -> HTTPException:
    return HTTPException(status_code=404, detail="Collection not found")


async def _collection(db: AsyncSession, uid: str) -> Collection:
    found = await coll_svc.get_collection(db, uid)
    if found is None:
        raise _collection_gone()
    return found


async def _render(
    db: AsyncSession, user: User, collections: Sequence[Collection]
) -> list[CollectionOut]:
    """Each collection as the CALLER sees it. The covers are read in one statement under the
    library's filters — the same visibility `_viewable` applies — so a cover the caller may not see
    is simply absent from the result and renders as null."""
    user_id = user.id
    cover_ids = {c.cover_attachment_id for c in collections if c.cover_attachment_id is not None}
    covers: dict[int, AttachmentListItem] = {}
    if cover_ids:
        stmt = library_page_statement(
            [*library_filters(user, None, None), Attachment.id.in_(cover_ids)], "desc"
        )
        rows = list((await db.execute(stmt)).all())
        ready = await web_ready_uids(att for att, _, _, _ in rows)
        covers = {
            att.id: _list_item(att, user_id, memo_uid, when, list(tags), ready)
            for att, memo_uid, when, tags in rows
        }
    creator_ids = {c.created_by for c in collections if c.created_by is not None}
    creators: dict[int, str] = {}
    if creator_ids:
        found = await db.execute(select(User.id, User.subject).where(User.id.in_(creator_ids)))
        creators = {uid: subject for uid, subject in found.all()}
    out = []
    for c in collections:
        out.append(
            CollectionOut(
                uid=c.uid,
                title=c.title,
                tag=c.tag,
                spanStart=c.span_start,
                spanEnd=c.span_end,
                tz=c.tz,
                cover=covers.get(c.cover_attachment_id)
                if c.cover_attachment_id is not None
                else None,
                memberCount=await coll_svc.member_count(db, c, user),
                createdBy=creators.get(c.created_by) if c.created_by is not None else None,
                createdAt=c.created_at,
                updatedAt=c.updated_at,
            )
        )
    return out


@router.get("/collections", response_model=list[CollectionOut], responses=_UNAUTHORIZED)
async def list_collections(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[CollectionOut]:
    """Every collection of the household, the latest span first and span-less ones last, each
    with the caller's member count and cover."""
    return await _render(db, user, await coll_svc.list_collections(db))


@router.post("/collections", response_model=CollectionOut, status_code=201, responses=_NOT_FOUND)
async def create_collection(
    body: CollectionCreate,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CollectionOut:
    """Store a collection: it needs a tag, or both span bounds, or all three. A cover the caller
    may not see is a 404. With `idempotencyKey`, a replay answers 201 with the stored record."""
    user_id = user.id
    if body.idempotencyKey is not None:
        # The replay FIRST, before the cover is resolved: a retry whose response was lost may name
        # a cover reclaimed since, and its answer is still the record the first call stored.
        replayed = await coll_svc.get_by_idempotency_key(db, user_id, body.idempotencyKey)
        if replayed is not None:
            return (await _render(db, user, [replayed]))[0]
    cover_id = None
    if body.coverAttachmentUid is not None:
        try:
            cover_id = (await _viewable(db, user, body.coverAttachmentUid)).id
        except HTTPException:
            # Between the missed lookup and here a same-key create may have won and its cover been
            # reclaimed since: the winner is still this retry's answer, so ask once more.
            if body.idempotencyKey is not None:
                won = await coll_svc.get_by_idempotency_key(db, user_id, body.idempotencyKey)
                if won is not None:
                    return (await _render(db, user, [won]))[0]
            raise
    try:
        created, _ = await coll_svc.create_collection(
            db,
            user,
            title=body.title,
            tag=body.tag,
            span_start=body.spanStart,
            span_end=body.spanEnd,
            tz=body.tz,
            cover_attachment_id=cover_id,
            idempotency_key=body.idempotencyKey,
        )
    except coll_svc.CollectionInvalid as exc:
        raise _invalid(exc) from exc
    except coll_svc.CoverGone as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    await db.commit()
    return (await _render(db, user, [created]))[0]


@router.get("/collections/{uid}", response_model=CollectionOut, responses=_NOT_FOUND)
async def get_collection(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CollectionOut:
    """One collection, with the caller's member count and cover."""
    return (await _render(db, user, [await _collection(db, uid)]))[0]


@router.patch("/collections/{uid}", response_model=CollectionOut, responses=_NOT_FOUND)
async def update_collection(
    uid: str,
    body: CollectionPatch,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CollectionOut:
    """Change the named fields: an absent field stays, null clears it (`title` excepted). The
    resulting record must still be a valid collection (422 otherwise, nothing changed). Any member
    may edit any collection; the last write wins. A cover the caller may not see is a 404."""
    collection = await _collection(db, uid)
    sent = body.model_dump(exclude_unset=True)
    changes: dict[str, object] = {_FIELDS[k]: v for k, v in sent.items() if k in _FIELDS}
    if "coverAttachmentUid" in sent:
        cover_uid = sent["coverAttachmentUid"]
        changes["cover_attachment_id"] = (
            None if cover_uid is None else (await _viewable(db, user, cover_uid)).id
        )
    try:
        updated = await coll_svc.update_collection(db, collection, changes)
    except coll_svc.CollectionInvalid as exc:
        raise _invalid(exc) from exc
    except coll_svc.CollectionGone as exc:
        raise _collection_gone() from exc
    except coll_svc.CoverGone as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    await db.commit()
    return (await _render(db, user, [updated]))[0]


@router.delete("/collections/{uid}", status_code=204, responses=_NOT_FOUND)
async def delete_collection(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete a collection and its exclusions. The media and the memos are untouched."""
    await coll_svc.delete_collection(db, await _collection(db, uid))
    await db.commit()


@router.put("/collections/{uid}/exclusions/{attachment_uid}", status_code=204, responses=_NOT_FOUND)
async def exclude_attachment(
    uid: str,
    attachment_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Take one attachment out of this collection's members. Idempotent: excluding it again
    changes nothing and answers 204 again."""
    collection = await _collection(db, uid)
    attachment = await _viewable(db, user, attachment_uid)
    try:
        await coll_svc.exclude(db, collection, attachment)
        await db.commit()
    except IntegrityError as exc:
        # Only a vanished parent is the caller's 404; any other integrity failure is not.
        if getattr(exc.orig, "sqlstate", None) != _FK_VIOLATION:
            raise
        await db.rollback()
        raise HTTPException(status_code=404, detail="Collection or attachment not found") from exc


@router.delete(
    "/collections/{uid}/exclusions/{attachment_uid}", status_code=204, responses=_NOT_FOUND
)
async def include_attachment(
    uid: str,
    attachment_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Put an excluded attachment back. Idempotent: one that is not excluded stays a member."""
    collection = await _collection(db, uid)
    attachment = await _viewable(db, user, attachment_uid)
    await coll_svc.include(db, collection, attachment)
    await db.commit()


# The member read answers 422 in two shapes, the library's own: a query parameter that fails
# validation gets the array, and a cursor the server cannot decode or of the other order's kind
# gets the global cursor handler's string (app/main.py). Both are published so neither surprises a
# generated client.
_MEMBER_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND,
    422: {
        "description": "A query parameter failed validation (the `detail` array), or `cursor` "
        "could not be decoded or belongs to the other order kind (the `detail` string).",
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {
                            "type": "object",
                            "properties": {"detail": {"type": "string"}},
                            "required": ["detail"],
                        },
                    ]
                }
            }
        },
    },
}


@router.get(
    "/collections/{uid}/attachments",
    response_model=list[AttachmentListItem],
    responses=_MEMBER_READ_RESPONSES,
    openapi_extra={"responses": {"200": {"headers": _next_cursor_header()}}},
)
async def list_members(
    uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    type: Literal["image", "video"] | None = None,
    limit: int = 200,
    cursor: str | None = None,
    order: Literal["desc", "asc", "taken_desc", "taken_asc"] = "taken_desc",
) -> list[AttachmentListItem]:
    """The collection's members as the caller may see them — its tag's media and its span's,
    minus its exclusions — in exactly the shape, order and paging of `GET /api/attachments`.
    `type` narrows to images or videos. When more rows follow, the `X-Next-Cursor` header carries
    the token to resend as `?cursor=` with the same `type`, `limit` and `order`."""
    collection = await _collection(db, uid)
    filters = [*library_filters(user, type, None), collection_member_clause(collection)]
    return await library_page(db, user, response, filters, order, limit, cursor)
