"""The ONE route that serves a stored coordinate.

Deliberately parameterless: the client clusters on the device off the whole body (plan decision
D8), so there is no bbox or zoom argument to get wrong, and no argument means no way to ask this
route a question whose answer differs from the feed's.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import Attachment, User
from app.routers.memos import AttachmentRef, _att_ref
from app.services import map_markers

router = APIRouter(prefix="/api", tags=["map"])


class MapMarker(AttachmentRef):
    """One located attachment: the feed's ref, plus where and when.

    The coordinate lives on THIS model alone. `AttachmentRef` — which every memo body carries —
    deliberately does not grow one, so a reader of `/api/memos` learns nothing about where a
    photo was taken.
    """

    memo_uid: str
    latitude: float
    longitude: float
    taken_at: Rfc3339Datetime


class MapMarkerList(BaseModel):
    markers: list[MapMarker]


def _marker(att: Attachment, memo_uid: str, taken_at, user_id: int) -> MapMarker:
    assert att.latitude is not None and att.longitude is not None  # noqa: S101 — the query's filter
    return MapMarker(
        **_att_ref(att, user_id).model_dump(),
        memo_uid=memo_uid,
        latitude=att.latitude,
        longitude=att.longitude,
        taken_at=taken_at,
    )


@router.get("/map/markers", response_model=MapMarkerList)
async def list_markers(
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> MapMarkerList:
    """Every located photo the caller may see, oldest first."""
    rows = await map_markers.located_markers(db, user)
    return MapMarkerList(
        markers=[_marker(att, memo_uid, when, user.id) for att, memo_uid, when in rows]
    )
