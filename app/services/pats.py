import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CAPTURE_GRANT_PREFIX, PAT_PREFIX, hash_capture_grant, hash_token
from app.config import settings
from app.models import Pat, User

# What Manage Tokens lists a capture grant as. The device's own token carries the device name; the
# grant is that device's share extension, and one entry per device is all the list ever holds.
CAPTURE_GRANT_NAME = "Capture grant, phone"


async def create_pat(
    db: AsyncSession, user: User, name: str, expires_at: datetime | None = None
) -> tuple[Pat, str]:
    """Create a PAT; return (row, plaintext token). Only the sha256 is persisted.

    `expires_at` defaults to None — a token that never expires, the only kind that existed before
    device tokens, so callers that do not ask for a lifetime keep the old behaviour.
    """
    token = PAT_PREFIX + secrets.token_urlsafe(32)
    pat = Pat(hashed_token=hash_token(token), name=name, user_id=user.id, expires_at=expires_at)
    db.add(pat)
    await db.flush()
    await db.refresh(pat)
    return pat, token


async def mint_capture_grant(
    db: AsyncSession, user: User, parent_hashed_token: str
) -> tuple[Pat, str, datetime] | None:
    """Mint the presenting token's upload-only capture grant, retiring the one it held before.

    Returns (row, plaintext, expiry), or None when the parent is not a live ordinary token of
    `user` any more (revoked or renewed between authentication and here).

    ONE live grant per parent, and the parent's row lock is what makes that hold under concurrency:
    a second mint for the same token blocks on `FOR UPDATE` until the first commits, then its
    DELETE (a fresh snapshot per statement) sees the first one's grant and retires it. Last mint
    wins; every mint answers with a grant, but only the newest one authenticates. The unique index
    on `parent_pat_id` is the backstop should anything ever insert without taking the lock.

    The grant never outlives its parent: its expiry is the earlier of a day and the parent's own.
    """
    now = datetime.now(UTC)
    parent = (
        await db.execute(
            select(Pat)
            .where(
                Pat.hashed_token == parent_hashed_token,
                Pat.user_id == user.id,
                Pat.parent_pat_id.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if parent is None or (parent.expires_at is not None and parent.expires_at <= now):
        return None
    await db.execute(delete(Pat).where(Pat.parent_pat_id == parent.id))
    expires_at = now + timedelta(seconds=settings.CAPTURE_GRANT_TTL_S)
    if parent.expires_at is not None:
        expires_at = min(expires_at, parent.expires_at)
    grant = CAPTURE_GRANT_PREFIX + secrets.token_urlsafe(32)
    row = Pat(
        hashed_token=hash_capture_grant(grant),
        name=CAPTURE_GRANT_NAME,
        user_id=user.id,
        expires_at=expires_at,
        parent_pat_id=parent.id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row, grant, expires_at
