from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import User, Visibility

router = APIRouter(prefix="/api/settings", tags=["settings"])

MAX_AVATAR_BYTES = 256 * 1024


class SettingsOut(BaseModel):
    default_visibility: Visibility
    theme: str
    avatar: str | None


class SettingsPatch(BaseModel):
    default_visibility: Visibility | None = None
    theme: str | None = None
    avatar: str | None = None  # data:image/ URL; "" clears it


def _out(user: User) -> SettingsOut:
    return SettingsOut(
        default_visibility=user.default_visibility,
        theme=user.theme,
        avatar=user.avatar,
    )


@router.get("", response_model=SettingsOut)
async def get_settings(user: Annotated[User, Depends(auth.require_user)]) -> SettingsOut:
    return _out(user)


@router.patch("", response_model=SettingsOut)
async def update_settings(
    body: SettingsPatch,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SettingsOut:
    if body.default_visibility is not None:
        user.default_visibility = body.default_visibility
    if body.theme is not None:
        user.theme = body.theme
    if body.avatar is not None:
        if body.avatar and (
            not body.avatar.startswith("data:image/") or len(body.avatar) > MAX_AVATAR_BYTES
        ):
            raise HTTPException(
                status_code=400, detail="avatar must be a data:image/ URL under 256 KB"
            )
        user.avatar = body.avatar or None  # empty string clears it
    await db.commit()
    return _out(user)
