from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import User

router = APIRouter(prefix="/api", tags=["members"])


class MemberOut(BaseModel):
    """One household member, in the field shapes `/api/auth/user` already uses for the caller.

    A deliberate subset of `UserOut`: `email` and `role` are the account's own business and no
    client needs them to draw a filter row, so they are not published about OTHER people here.
    `avatar` is whatever the settings feature stores — a `data:image/…` URL (nothing else is
    accepted, capped at 256 KB) or, for an account carried over from old memos, the path string
    that came with it — and null for a member who never set one. It is
    passed through verbatim rather than normalised, so the value a client renders for a member is
    the same one `/api/auth/user` hands that member for themselves.
    """

    subject: str
    display_name: str | None
    avatar: str | None = None


@router.get("/members", response_model=list[MemberOut])
async def list_members(
    _: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[MemberOut]:
    """Every member of the household, for the client-side "whose memos?" filter.

    ASSUMPTION, and the reason this route needs no scoping argument: this is a single-household
    instance — every row in `users` is a member of the one household, so membership needs no
    column to express it. A multi-tenant deployment would have to add one and filter here; nothing
    in the schema records tenancy today.

    Everyone who can authenticate sees the whole roster, and it deliberately does NOT depend on
    what the caller can read: a member who has posted nothing, or only PRIVATE memos, still appears
    — a filter that silently omitted them would make an empty result indistinguishable from a
    member who does not exist, and the roster leaks nothing beyond a display name the household
    already sees on every shared memo.

    Ordered by (display_name, subject) so the list is stable across calls and across pages of any
    client that caches it; `subject` alone breaks the tie for the members with no display name,
    whom Postgres would otherwise return in whatever order it liked.
    """
    rows = (
        await db.execute(select(User).order_by(User.display_name.asc(), User.subject.asc()))
    ).scalars()
    return [
        MemberOut(subject=u.subject, display_name=u.display_name, avatar=u.avatar) for u in rows
    ]
