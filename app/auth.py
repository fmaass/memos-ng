import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models import Pat, User, WebSession

TX_COOKIE = "memosng_oidc_tx"
SID_COOKIE = "memosng_sid"


def _fernet() -> Fernet:
    return Fernet(settings.SESSION_SECRET.encode())


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt(token: str) -> dict | None:
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, ValueError):
        return None


def new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# --- OIDC network calls (mocked in tests; never use the internal authelia URL) ---


async def exchange_code(code: str, verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "client_id": settings.OIDC_CLIENT_ID,
        "client_secret": settings.OIDC_CLIENT_SECRET,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.OIDC_TOKEN_ENDPOINT, data=data)
        resp.raise_for_status()
        return resp.json()


_jwk_client: jwt.PyJWKClient | None = None


def _jwks() -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(settings.OIDC_JWKS_URI)
    return _jwk_client


def validate_id_token(id_token: str) -> dict:
    """Validate the id_token signature against JWKS + audience/issuer. Raises on failure."""
    signing_key = _jwks().get_signing_key_from_jwt(id_token).key
    return jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=settings.OIDC_CLIENT_ID,
        issuer=settings.OIDC_ISSUER,
    )


# --- user + session persistence ---


async def fetch_userinfo(access_token: str | None) -> dict:
    """Authelia returns preferred_username/name/email/groups from the userinfo endpoint, not
    the (minimal, sub-only) id_token. Returns {} if there's no access token."""
    if not access_token:
        return {}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            settings.OIDC_USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json()


async def upsert_user(db: AsyncSession, claims: dict) -> User:
    subject = claims.get("preferred_username") or claims.get("sub")
    if not subject:
        raise HTTPException(status_code=400, detail="id_token missing subject")
    user = (await db.execute(select(User).where(User.subject == subject))).scalar_one_or_none()
    role = "admin" if "admins" in (claims.get("groups") or []) else "user"
    if user is None:
        user = User(
            subject=subject,
            display_name=claims.get("name"),
            email=claims.get("email"),
            role=role,
        )
        db.add(user)
    else:
        user.display_name = claims.get("name") or user.display_name
        user.email = claims.get("email") or user.email
        user.role = role
    await db.flush()
    return user


async def create_session(db: AsyncSession, user: User, tokens: dict) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        WebSession(
            sid=sid,
            user_id=user.id,
            encrypted_tokens=encrypt(tokens),
            issued_at=now,
            last_seen=now,
            absolute_expiry=now + timedelta(days=settings.SESSION_ABSOLUTE_DAYS),
        )
    )
    await db.flush()
    return sid


async def load_session(db: AsyncSession, sid: str) -> WebSession | None:
    ws = (await db.execute(select(WebSession).where(WebSession.sid == sid))).scalar_one_or_none()
    if ws is None or ws.absolute_expiry <= datetime.now(UTC):
        return None
    return ws


def rolling_refresh_due(last_seen: datetime, now: datetime) -> bool:
    """True iff the session's informational last_seen is older than the rolling-throttle window and
    should be refreshed. Throttling avoids a per-request UPDATE+COMMIT to the one hot session row,
    which lock-convoys the front page's concurrent fan-out (measured 60-90x). Pure + unit-tested."""
    return last_seen < now - timedelta(seconds=settings.SESSION_ROLLING_THROTTLE_S)


async def require_user(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    sid = request.cookies.get(SID_COOKIE)
    if not sid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ws = await load_session(db, sid)
    if ws is None:
        raise HTTPException(status_code=401, detail="Session invalid or expired")
    if settings.SESSION_ROLLING:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=settings.SESSION_ROLLING_THROTTLE_S)
        # Refresh last_seen at most once per throttle window. The Python gate skips the write (and
        # its COMMIT) on the hot path; the WHERE re-checks staleness under the row lock so a
        # concurrent burst on the same session does ONE write, not N — no per-request lock-convoy.
        if rolling_refresh_due(ws.last_seen, now):
            await db.execute(
                update(WebSession)
                .where(WebSession.sid == sid, WebSession.last_seen < stale_before)
                .values(last_seen=now)
            )
            await db.commit()  # expire_on_commit=False keeps objects usable
    user = (await db.execute(select(User).where(User.id == ws.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


PAT_PREFIX = "memos_pat_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def authenticate_pat(db: AsyncSession, token: str) -> User | None:
    """Return the owning user for a valid PAT (stamping last_used), else None. No commit."""
    pat = (
        await db.execute(select(Pat).where(Pat.hashed_token == hash_token(token)))
    ).scalar_one_or_none()
    if pat is None:
        return None
    pat.last_used = datetime.now(UTC)
    await db.flush()
    return (await db.execute(select(User).where(User.id == pat.user_id))).scalar_one_or_none()


async def require_pat_or_user(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer ") and authz[7:].startswith(PAT_PREFIX):
        user = await authenticate_pat(db, authz[7:])
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked token")
        await db.commit()
        return user
    return await require_user(request, db)


# --- simple per-IP fixed-window rate limit for login/callback ---

_RL: dict[str, list[float]] = {}


def rate_limit(limit: int = 20, window: float = 60.0):
    def dep(request: Request) -> None:
        ip = request.client.host if request.client else "anon"
        now = time.monotonic()
        bucket = [t for t in _RL.get(ip, []) if now - t < window]
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests")
        bucket.append(now)
        _RL[ip] = bucket

    return dep
