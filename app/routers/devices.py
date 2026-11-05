"""Per-device registrations the native client makes on behalf of its signed-in member.

`/api/devices/live-activity-token` (MEMNG-946) stores the iOS app's ActivityKit push-to-start token
so the server can later start a Live Activity for a capture the app never saw. Modelled on the
web-push subscription (app/routers/push.py): the token is one device's mailbox, unique across the
table, so a POST upserts by token and a device that switched accounts moves its row to the new
account. Ownership is decided by the SESSION (`require_user`), never by the token — so an
upload-only capture grant is refused like on every other route, and DELETE removes a row only
when it belongs to the caller while answering 204 either way.

DELETE takes the token in a JSON body, as the web-push DELETE does, rather than a query parameter:
a URL lands in the proxy's and the server's access logs, and this token is what lets a sender
start an activity on the device. The generated Swift client models a DELETE body like any other
operation's.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.database import get_session
from app.models import LiveActivityToken, User

router = APIRouter(prefix="/api/devices", tags=["devices"])

# Hex in either case is accepted and folded to lower case, so one device cannot register the same
# token twice under two spellings. 32..512 characters bounds a token whose length APNs does not
# promise (64 today) without admitting an unbounded string.
PushToStartToken = Annotated[
    str,
    StringConstraints(min_length=32, max_length=512, pattern=r"^[0-9a-fA-F]+$", to_lower=True),
    Field(description="The ActivityKit push-to-start token, hex-encoded; stored in lower case."),
]


class LiveActivityTokenIn(BaseModel):
    token: PushToStartToken
    environment: Literal["production", "sandbox"] = Field(
        default="production",
        description="The APNs environment the token was issued for.",
    )


class LiveActivityTokenRef(BaseModel):
    token: PushToStartToken


async def upsert_live_activity_token(
    db: AsyncSession, user_id: int, token: str, environment: str
) -> None:
    """One statement, so two first registrations of one token racing each other settle on the
    unique index as one row instead of one of them failing on it."""
    stmt = insert(LiveActivityToken).values(user_id=user_id, token=token, environment=environment)
    stmt = stmt.on_conflict_do_update(
        index_elements=[LiveActivityToken.token],
        set_={
            "user_id": stmt.excluded.user_id,
            "environment": stmt.excluded.environment,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()


@router.post("/live-activity-token", status_code=204)
async def register_live_activity_token(
    body: LiveActivityTokenIn,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Register this device's push-to-start token for the signed-in member.

    Idempotent: posting the same token again updates its one row. A token another member posted
    moves to the caller, since the device has switched accounts."""
    await upsert_live_activity_token(db, user.id, body.token, body.environment)
    return Response(status_code=204)


@router.delete("/live-activity-token", status_code=204)
async def unregister_live_activity_token(
    body: LiveActivityTokenRef,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Remove this device's push-to-start token if the signed-in member holds it.

    Always 204: a token that is unknown, already removed, or held by another member is left as it
    is and answered the same way."""
    await db.execute(
        delete(LiveActivityToken).where(
            LiveActivityToken.token == body.token, LiveActivityToken.user_id == user.id
        )
    )
    await db.commit()
    return Response(status_code=204)
