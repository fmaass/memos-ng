from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import get_session
from app.models import PushSubscription, User

router = APIRouter(prefix="/api/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: PushKeys


class VapidKeyOut(BaseModel):
    key: str


@router.get("/vapid-public-key", response_model=VapidKeyOut)
async def vapid_public_key() -> VapidKeyOut:
    """The VAPID public key the service worker needs to subscribe. Public by definition."""
    return VapidKeyOut(key=settings.VAPID_PUBLIC_KEY)


@router.post("/subscribe", status_code=204)
async def subscribe(
    body: SubscriptionIn,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    # Upsert by endpoint: a browser re-subscribes (rotating keys, possibly under a new
    # user) to the same endpoint — update in place rather than create a duplicate.
    existing = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            PushSubscription(
                user_id=user.id,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
            )
        )
    else:
        existing.user_id = user.id
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
    await db.commit()
    return Response(status_code=204)


@router.delete("/subscribe", status_code=204)
async def unsubscribe(
    body: SubscriptionIn,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    sub = (
        await db.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == body.endpoint,
                PushSubscription.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if sub is not None:
        await db.delete(sub)
        await db.commit()
    return Response(status_code=204)
