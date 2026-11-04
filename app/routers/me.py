"""`/api/me` — the caller's own account preferences (MEMNG-972).

`default_visibility` is the same `users.default_visibility` column `/api/settings` writes; the two
routes are views of one row. `notify_on_household_posts` is the opt-in MEMNG-967 reads to decide who
gets a push when another household member posts a memo.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import User, Visibility
from app.routers import RequestFlag, RequestVisibility

router = APIRouter(prefix="/api/me", tags=["me"])


class MeOut(BaseModel):
    default_visibility: Visibility
    notify_on_household_posts: bool


class MePatch(BaseModel):
    """Both optional: an absent field leaves the preference as it is; a JSON null is a 422."""

    default_visibility: RequestVisibility = None
    notify_on_household_posts: RequestFlag = None


def _out(user: User) -> MeOut:
    return MeOut(
        default_visibility=user.default_visibility,
        notify_on_household_posts=user.notify_on_household_posts,
    )


@router.get("", response_model=MeOut)
async def get_me(user: Annotated[User, Depends(auth.require_user)]) -> MeOut:
    return _out(user)


@router.patch("", response_model=MeOut)
async def update_me(
    body: MePatch,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MeOut:
    if body.default_visibility is not None:
        user.default_visibility = body.default_visibility
    if body.notify_on_household_posts is not None:
        user.notify_on_household_posts = body.notify_on_household_posts
    await db.commit()
    return _out(user)
