from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import User, Visibility

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    default_visibility: Visibility
    theme: str


class SettingsPatch(BaseModel):
    default_visibility: Visibility | None = None
    theme: str | None = None


def _out(user: User) -> SettingsOut:
    return SettingsOut(default_visibility=user.default_visibility, theme=user.theme)


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
    await db.commit()
    return _out(user)
