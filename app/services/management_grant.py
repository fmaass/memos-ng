"""The step-up management grant: what a fresh Authelia login buys, and how long it lasts.

A device PAT mints and revokes nothing but itself (ADR-0015), so `/api/pats/*` needs a credential
that a token can never become. That credential is this grant: the app runs the ordinary browser
login lane a second time with `purpose=manage`, and the one-shot code it comes back with is
exchanged here for a short-lived secret it presents as `Authorization: Grant …`.

Only the sha256 is stored, like a PAT and like a handoff code — a database read cannot be replayed
into a live credential. The grant is deliberately MULTI-USE within its TTL (it authorises a
management session of minutes: list, create, revoke); what is single-use is the CODE that was
exchanged for it.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ManagementGrant, User

# The authorization scheme a grant is presented under. Deliberately NOT `Bearer`: a device PAT
# travels in a Bearer header, and the two credentials must never be confusable — a request that
# names the wrong scheme is refused rather than reinterpreted.
GRANT_SCHEME = "grant"


def hash_grant(grant: str) -> str:
    return hashlib.sha256(grant.encode()).hexdigest()


async def mint_grant(db: AsyncSession, user: User) -> tuple[str, datetime]:
    """Insert a fresh grant for `user`; return (plaintext, expiry). The plaintext exists only here.

    Sweeps every expired row on the way through — GLOBALLY, like `native_auth.mint_code`, not just
    this user's: a per-user sweep would leave the rows of an account that never comes back sitting
    there for good, and "the table cannot accumulate unbounded" would simply not be true. No grace
    window, unlike the code sweep: an expired grant is a 401 whether the row is still there or not,
    so keeping it buys no better diagnostic.
    """
    now = datetime.now(UTC)
    await db.execute(delete(ManagementGrant).where(ManagementGrant.expires_at < now))
    grant = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=settings.MANAGE_GRANT_TTL_S)
    db.add(ManagementGrant(hashed_grant=hash_grant(grant), user_id=user.id, expires_at=expires_at))
    await db.flush()
    return grant, expires_at


async def authenticate_grant(db: AsyncSession, grant: str) -> User | None:
    """The user a live grant speaks for — None for an unknown, expired or orphaned one alike.

    Expiry is enforced HERE rather than in the caller, for the reason `auth.authenticate_pat`
    enforces a token's: every surface that ever accepts a grant then inherits the bound from one
    place instead of remembering to re-check it.

    Nothing is stamped and nothing is consumed. A grant is a time-bounded capability, not a
    one-shot: the management session it authorises lists, creates and revokes with the one value it
    is holding, and a use counter would only end that session at an arbitrary point inside its TTL.
    """
    row = (
        await db.execute(
            select(ManagementGrant.user_id).where(
                ManagementGrant.hashed_grant == hash_grant(grant),
                ManagementGrant.expires_at > datetime.now(UTC),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
