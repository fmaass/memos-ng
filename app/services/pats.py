import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PAT_PREFIX, hash_token
from app.models import Pat, User


async def create_pat(db: AsyncSession, user: User, name: str) -> tuple[Pat, str]:
    """Create a PAT; return (row, plaintext token). Only the sha256 is persisted."""
    token = PAT_PREFIX + secrets.token_urlsafe(32)
    pat = Pat(hashed_token=hash_token(token), name=name, user_id=user.id)
    db.add(pat)
    await db.flush()
    await db.refresh(pat)
    return pat, token
