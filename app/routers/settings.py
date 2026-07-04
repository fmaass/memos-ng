import time
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings as app_settings
from app.database import get_session
from app.models import User, Visibility
from app.services import frame_share

router = APIRouter(prefix="/api/settings", tags=["settings"])

MAX_AVATAR_BYTES = 256 * 1024

# Per-user cooldown for the frame-test send primitive: user_id -> last-send monotonic timestamp.
# In-process (single app instance) and best-effort — enough to stop an authed user hammering the
# shared server SMTP account from the settings screen.
FRAME_TEST_COOLDOWN_S = 30
_frame_test_cooldown: dict[int, float] = {}


class SettingsOut(BaseModel):
    default_visibility: Visibility
    theme: str
    avatar: str | None
    frame_send_to: str | None
    frame_send_from: str | None


class SettingsPatch(BaseModel):
    default_visibility: Visibility | None = None
    theme: str | None = None
    avatar: str | None = None  # data:image/ URL; "" clears it
    frame_send_to: str | None = None  # …@send.auraframes.com; "" clears it
    frame_send_from: str | None = None  # the Aura member sender identity; "" clears it

    @field_validator("frame_send_from")
    @classmethod
    def _valid_email_or_blank(cls, v: str | None) -> str | None:
        # None = unchanged, "" = clear; any other value must look like an email (422 otherwise).
        if v is None or v == "" or frame_share.is_valid_email(v):
            return v
        raise ValueError("must be a valid email address")

    @field_validator("frame_send_to")
    @classmethod
    def _valid_aura_addr_or_blank(cls, v: str | None) -> str | None:
        # The frame recipient must be an Aura ingestion address — the server SMTP account may only
        # send to the frame, not an arbitrary recipient. None = unchanged, "" = clear.
        if v is None or v == "" or frame_share.is_aura_send_to(v):
            return v
        raise ValueError("must be an @send.auraframes.com address")


class FrameTestResult(BaseModel):
    ok: bool
    detail: str


def _out(user: User) -> SettingsOut:
    return SettingsOut(
        default_visibility=user.default_visibility,
        theme=user.theme,
        avatar=user.avatar,
        frame_send_to=user.frame_send_to,
        frame_send_from=user.frame_send_from,
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
    if body.frame_send_to is not None:
        user.frame_send_to = body.frame_send_to or None  # "" clears it
    if body.frame_send_from is not None:
        user.frame_send_from = body.frame_send_from or None
    await db.commit()
    return _out(user)


@router.post("/frame-test", response_model=FrameTestResult)
async def frame_test(user: Annotated[User, Depends(auth.require_user)]) -> FrameTestResult:
    """Send a bundled test photo through the real render+SMTP path, synchronously. Reports whether
    the MAIL SERVER accepted it — NOT whether it reached the frame (acceptance != delivery)."""
    if not (app_settings.SMTP_USERNAME and app_settings.SMTP_PASSWORD):
        raise HTTPException(status_code=400, detail="Frame sharing is not configured on the server")
    if not (user.frame_send_to and user.frame_send_from):
        raise HTTPException(status_code=400, detail="Set a send-to and send-from address first.")
    now = time.monotonic()
    last = _frame_test_cooldown.get(user.id)
    if last is not None and now - last < FRAME_TEST_COOLDOWN_S:
        raise HTTPException(
            status_code=429, detail="Please wait a moment before sending another test photo."
        )
    _frame_test_cooldown[user.id] = now  # record before sending → the primitive can't be hammered
    jpeg = await to_thread.run_sync(frame_share.render_test_image)
    try:
        await frame_share.send_frame_share(
            from_addr=user.frame_send_from,
            to_addr=user.frame_send_to,
            filename="memos-ng-frame-test.jpg",
            jpeg_bytes=jpeg,
        )
    except Exception as exc:  # surface the SMTP error to the UI, never raise 500
        return FrameTestResult(ok=False, detail=str(exc)[:300])
    return FrameTestResult(ok=True, detail="Accepted by the mail server — check your frame.")
