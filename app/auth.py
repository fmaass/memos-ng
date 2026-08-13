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
from sqlalchemy import or_, select, update
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


# In-process record of when each session's last_seen was last claimed for a refresh (monotonic
# seconds). Set synchronously BEFORE the DB write so a concurrent same-session burst sees it and
# skips — only one coroutine per window even attempts the write, so the first load after idle can't
# lock-convoy on the hot session row either. Per-process + non-durable is fine: the DB conditional
# UPDATE is the durable safety net, and a restart just costs one extra write per session.
_last_seen_claimed: dict[str, float] = {}


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
    if settings.SESSION_ROLLING and rolling_refresh_due(ws.last_seen, ws_now := datetime.now(UTC)):
        mono = time.monotonic()
        # Claim the write window IN MEMORY before any await: a concurrent same-session burst sees
        # the fresh marker and skips, so at most one coroutine per window hits the DB (no convoy —
        # even on first load after idle). The conditional UPDATE is the durable cross-restart gate.
        if mono - _last_seen_claimed.get(sid, 0.0) >= settings.SESSION_ROLLING_THROTTLE_S:
            _last_seen_claimed[sid] = mono
            if len(_last_seen_claimed) > 4096:  # bound the map (correctness lives in the DB WHERE)
                _last_seen_claimed.clear()
            stale_before = ws_now - timedelta(seconds=settings.SESSION_ROLLING_THROTTLE_S)
            await db.execute(
                update(WebSession)
                .where(WebSession.sid == sid, WebSession.last_seen < stale_before)
                .values(last_seen=ws_now)
            )
            await db.commit()  # expire_on_commit=False keeps objects usable
    user = (await db.execute(select(User).where(User.id == ws.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


PAT_PREFIX = "memos_pat_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# In-process record of when each PAT's last_used stamp was last claimed (monotonic seconds), the
# PAT-side twin of _last_seen_claimed above. Same reason: claimed synchronously BEFORE the write so
# a concurrent burst on one PAT (a satellite pulling a gallery's derived tiers) has exactly one
# coroutine attempt the UPDATE — the rest never queue behind that row's lock.
_pat_last_used_claimed: dict[int, float] = {}


def pat_stamp_due(last_used: datetime | None, now: datetime) -> bool:
    """True iff the PAT's informational last_used is unset or older than the throttle window and
    should be refreshed. last_used is display-only (never an authorization input), so bounded
    staleness is the right trade against per-request writes to the one hot PAT row. Pure."""
    return last_used is None or last_used < now - timedelta(
        seconds=settings.PAT_LAST_USED_THROTTLE_S
    )


def claim_pat_stamp(pat_id: int, now_mono: float) -> bool:
    """Reserve this PAT's write window in-process; True iff the caller may do the write.

    Synchronous and await-free by design — a concurrent burst that all read the same stale
    last_used is collapsed here, before anyone takes the row lock."""
    if now_mono - _pat_last_used_claimed.get(pat_id, 0.0) < settings.PAT_LAST_USED_THROTTLE_S:
        return False
    _pat_last_used_claimed[pat_id] = now_mono
    if len(_pat_last_used_claimed) > 4096:  # bound the map (correctness lives in the DB WHERE)
        _pat_last_used_claimed.clear()
    return True


async def stamp_pat_last_used(db: AsyncSession, pat: Pat) -> bool:
    """Persist the PAT's last_used, throttled. Returns True iff this call won the throttle claim
    and issued the write (a cross-process race can still make it update 0 rows — see below).

    Committed HERE, in its own short transaction, rather than left pending for the caller: the
    compat surface (routers/compat.py optional_user, and serve_file_compat's own session) never
    commits, so a pending stamp was rolled back at request end and the row never changed
    (MEMNG-31). Committing immediately also releases the row lock the UPDATE takes instead of
    holding it for the rest of the request, which is what let a PAT gallery burst serialise on the
    one PAT row and queue toward pool exhaustion. Nothing else is pending in `db` at this point —
    authentication runs before any handler work — so the commit has no other side effect."""
    now = datetime.now(UTC)
    if not pat_stamp_due(pat.last_used, now):
        return False
    if not claim_pat_stamp(pat.id, time.monotonic()):
        return False
    # The same staleness predicate in the WHERE is the durable, cross-process/restart gate: another
    # worker (or this one after a restart) that has no in-process claim still writes at most once
    # per window, and a loser of the race updates 0 rows instead of overwriting a fresh stamp.
    stale_before = now - timedelta(seconds=settings.PAT_LAST_USED_THROTTLE_S)
    await db.execute(
        update(Pat)
        .where(Pat.id == pat.id, or_(Pat.last_used.is_(None), Pat.last_used < stale_before))
        .values(last_used=now)
    )
    await db.commit()  # expire_on_commit=False keeps objects usable
    return True


async def authenticate_pat(db: AsyncSession, token: str) -> User | None:
    """Return the owning user for a valid PAT, else None. Stamps last_used at most once per
    throttle window, committing that stamp itself (see stamp_pat_last_used)."""
    pat = (
        await db.execute(select(Pat).where(Pat.hashed_token == hash_token(token)))
    ).scalar_one_or_none()
    if pat is None:
        return None
    # Read the FK BEFORE the stamp commits: a session configured to expire on commit would have to
    # lazy-refresh the attribute afterwards, which async IO cannot do from attribute access.
    user_id = pat.user_id
    await stamp_pat_last_used(db, pat)
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def require_pat_or_user(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer ") and authz[7:].startswith(PAT_PREFIX):
        user = await authenticate_pat(db, authz[7:])
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked token")
        return user  # the last_used stamp is already committed by authenticate_pat
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
