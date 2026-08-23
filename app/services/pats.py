import secrets
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PAT_PREFIX, hash_token
from app.models import Pat, User


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
