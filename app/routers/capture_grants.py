"""Upload-only capture grants (MEMNG-938): what a device's share extension uploads with.

The extension has no access to the device token on purpose, so the app mints this narrower
credential on every foreground and leaves it in the shared container. It is accepted on the three
upload-protocol operations (`auth.require_upload_user`) and refused everywhere else.

Both routes are bearer-only, through `presenting_token_hash`: a device acts on its own grant and on
nothing else, the same rule as the rest of the native self-service lane (Decision 5, ADR-0015). A
grant does not let a token outlive its own revocation — it dies with its parent — which is why a
token may mint one when it may not mint a successor token.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import Pat, User
from app.routers.auth import presenting_token_hash
from app.services import pats as svc

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class CaptureGrantOut(BaseModel):
    # The row's id in Manage Tokens (`GET /api/pats`), and the handle the revoke route takes.
    id: int
    name: str
    created_at: Rfc3339Datetime
    expires_at: Rfc3339Datetime
    # The plaintext grant, returned exactly once and never stored. Presented as a Bearer, and only
    # on POST /api/uploads, PATCH and HEAD /api/uploads/{upload_uid}.
    token: str


@router.post("/capture-grant", response_model=CaptureGrantOut, status_code=201)
async def mint_capture_grant(
    hashed_token: Annotated[str, Depends(presenting_token_hash)],
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CaptureGrantOut:
    """Mint the presenting device token's capture grant: upload-only, 24 h, acts as its owner.

    Each mint ROTATES: the grant this token minted before is revoked in the same transaction, so a
    device holds one live grant however often it foregrounds, and concurrent mints leave exactly
    one live (the last to commit). The grant expires after 24 hours or with its token, whichever
    is first, and is revoked with its token — by logout, renewal or Manage Tokens.
    """
    minted = await svc.mint_capture_grant(db, user, hashed_token)
    if minted is None:  # the token stopped existing between authentication and the lock
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    row, grant, expires_at = minted
    await db.commit()
    return CaptureGrantOut(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        expires_at=expires_at,
        token=grant,
    )


@router.delete("/capture-grant/{grant_id}", status_code=204)
async def revoke_capture_grant(
    grant_id: int,
    hashed_token: Annotated[str, Depends(presenting_token_hash)],
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Revoke the capture grant the presenting device token minted, and only that.

    404 for any other id: an ordinary token, or a grant another device minted, is not this route's
    to touch — token management proper is `/api/pats`.
    """
    parent = select(Pat.id).where(
        Pat.hashed_token == hashed_token,
        Pat.user_id == user.id,
        Pat.parent_pat_id.is_(None),
    )
    revoked = (
        await db.execute(
            delete(Pat)
            .where(
                Pat.id == grant_id,
                Pat.user_id == user.id,
                Pat.parent_pat_id.in_(parent.scalar_subquery()),
            )
            .returning(Pat.id)
        )
    ).one_or_none()
    if revoked is None:
        raise HTTPException(status_code=404, detail="Capture grant not found")
    await db.commit()
