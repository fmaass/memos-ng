"""Chosen covers for the DERIVED collections — tags, months and years (MEMNG-853).

Trips and albums are stored records and carry their cover on `/api/collections`. A tag, a month or
a year collection is computed from the library and has no record, so its cover is kept here, keyed
by `kind` and `key`, for the whole household: any signed-in member may choose or clear one, and the
last write wins. Choosing asks only that the caller may SEE the picture (`_viewable`, the rule the
collection routes use); the listing shows each reader only the covers whose picture that reader may
see, so a deleted or hidden picture drops out and the client falls back to its default cover.
"""

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import tag_name
from app.database import get_session
from app.models import User
from app.routers.assets import _viewable
from app.services import collection_covers as covers_svc

router = APIRouter(prefix="/api", tags=["collection-covers"])

CoverKind = Literal["tag", "month", "year"]

_KEY_DOC = (
    "The collection's key: a tag name exactly as memos carry it (letters, digits and underscores, "
    "without the hash sign), a month as four-digit year, hyphen and two-digit month (2031-03), or "
    "a year as four digits (2031)."
)
_REFUSALS = (
    "An unknown kind, or a key that is not a tag name, a month or a year as that kind needs it, "
    "answers 422 and changes nothing."
)

_DETAIL = {
    "application/json": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}
}
_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No valid session cookie or bearer token.", "content": _DETAIL},
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    404: {
        "description": "The attachment does not exist or is not visible to the caller — one "
        "answer for both. Nothing was changed.",
        "content": _DETAIL,
    },
}

_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_YEAR = re.compile(r"[0-9]{4}")


class CollectionCoverOut(BaseModel):
    kind: CoverKind
    key: str = Field(description=_KEY_DOC)
    attachmentUid: str = Field(description="The cover picture.")


class CollectionCoverSet(BaseModel):
    attachmentUid: str = Field(
        min_length=1, description="A picture the caller may see; any member's, not only theirs."
    )


def _checked_key(kind: str, key: str) -> str:
    """`key` unchanged when it names a collection of `kind`, else the 422 every other path
    parameter of this API answers with."""
    try:
        if kind == "tag":
            if len(key) > 255:
                raise ValueError("a tag name is at most 255 characters")
            tag_name(key)
        elif kind == "month":
            if _MONTH.fullmatch(key) is None:
                raise ValueError("a month is YYYY-MM")
        elif _YEAR.fullmatch(key) is None:
            raise ValueError("a year is YYYY")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["path", "key"], "msg": str(exc), "type": "value_error"}],
        ) from exc
    return key


@router.get("/collection-covers", response_model=list[CollectionCoverOut], responses=_UNAUTHORIZED)
async def list_collection_covers(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[CollectionCoverOut]:
    """Every chosen cover of a tag, month or year collection whose picture the caller may see,
    ordered by kind then key. A cover whose picture was deleted, or that the caller may not see,
    is simply absent — the client shows its default cover for that collection."""
    return [
        CollectionCoverOut.model_validate({"kind": kind, "key": key, "attachmentUid": uid})
        for kind, key, uid in await covers_svc.list_covers(db, user)
    ]


@router.put(
    "/collection-covers/{kind}/{key}",
    response_model=CollectionCoverOut,
    responses=_NOT_FOUND,
    description="Choose the cover of one tag, month or year collection for the whole household, "
    "replacing any earlier choice; the last write wins. Any signed-in member may choose any "
    "picture they can see. " + _REFUSALS + " A picture that does not exist or that the caller "
    "may not see answers 404 and changes nothing.",
)
async def set_collection_cover(
    kind: CoverKind,
    key: Annotated[str, Path(description=_KEY_DOC)],
    body: CollectionCoverSet,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CollectionCoverOut:
    key = _checked_key(kind, key)
    attachment = await _viewable(db, user, body.attachmentUid)
    try:
        await covers_svc.set_cover(db, kind, key, attachment)
    except covers_svc.CoverGone as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    await db.commit()
    return CollectionCoverOut(kind=kind, key=key, attachmentUid=body.attachmentUid)


@router.delete(
    "/collection-covers/{kind}/{key}",
    status_code=204,
    responses=_UNAUTHORIZED,
    description="Clear the chosen cover of one tag, month or year collection, so every client "
    "shows its default again. Idempotent: clearing a cover that is not set answers 204 too. "
    + _REFUSALS,
)
async def clear_collection_cover(
    kind: CoverKind,
    key: Annotated[str, Path(description=_KEY_DOC)],
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await covers_svc.clear_cover(db, kind, _checked_key(kind, key))
    await db.commit()
