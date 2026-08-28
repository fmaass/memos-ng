import asyncio
import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import structlog
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session, get_session
from app.models import Pat, User, WebSession
from app.services import management_grant

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


def _scheme_credential(request: Request, scheme: str) -> str | None:
    """The credential this request states under `scheme` (lowercase), else None.

    None means "this request makes no claim in that scheme at all" — an empty or whitespace-only
    credential is still a claim, and returns "" so it is judged (and refused) as one rather than
    silently falling through to the cookie. A header in some other scheme states no claim here.
    """
    presented, _, credential = request.headers.get("Authorization", "").partition(" ")
    if presented.lower() != scheme:
        return None
    return credential.strip()


def bearer_credential(request: Request) -> str | None:
    """The credential of an `Authorization: Bearer …` header, else None.

    None means "this request makes no bearer claim at all" — an empty or whitespace-only bearer
    credential is still a claim, and returns "" so it is judged (and refused) as one rather than
    silently falling through to the cookie. Any other scheme (Basic, Negotiate) is not a bearer
    claim and leaves the caller on the cookie path.
    """
    return _scheme_credential(request, "bearer")


def grant_credential(request: Request) -> str | None:
    """The credential of an `Authorization: Grant …` header, else None.

    A DISTINCT scheme from Bearer, deliberately: a device PAT travels in a Bearer header, and the
    two must never be confusable in either direction — a PAT presented as a Grant states a grant
    claim and is refused as one (it is not a grant), and a grant presented as a Bearer states a
    bearer claim and is refused as one (it is not a token). Neither is reinterpreted as the other.
    """
    return _scheme_credential(request, management_grant.GRANT_SCHEME)


async def require_session_user(
    request: Request, db: AsyncSession = Depends(get_session)
) -> User:
    """Authenticate the browser session, and ONLY that.

    A request carrying a bearer header is refused here before the cookie is read (Decision 6):
    on the routes that mint and revoke tokens, a device token riding alongside a valid session
    cookie must not be able to reach token management by having its header ignored.
    """
    if bearer_credential(request) is not None:
        raise HTTPException(status_code=401, detail="Session authentication required")
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
        prev_claim = _last_seen_claimed.get(sid)  # None = never claimed; see claim_pat_stamp
        if prev_claim is None or mono - prev_claim >= settings.SESSION_ROLLING_THROTTLE_S:
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
    request.state.auth_kind = "session"
    return user


async def require_session_or_grant(
    request: Request, db: AsyncSession = Depends(get_session)
) -> User:
    """Authenticate the browser session OR a step-up management grant, and nothing else.

    This is what `/api/pats/*` takes (MEMNG-138 option 2). The session cookie keeps the semantics it
    had — a browser reaches token management exactly as before — and a native client, which holds
    no session at all, reaches it with a grant it bought minutes ago with a fresh Authelia
    authentication. A device PAT still reaches it with neither (ADR-0015).

    The precedence is fail-closed at every step, and the order below IS the rule:

    * A BEARER claim is refused first, before anything else is read — `require_session_user`'s
      Decision-6 rule, unchanged: a device token riding alongside a valid session cookie must not
      reach token management by having its header quietly ignored.
    * A request that states no Grant claim is the ordinary cookie path, untouched.
    * A Grant claim decides the outcome, and it decides it BEFORE any cookie is consulted: a grant
      this server never issued, or one that has expired, is a 401 even with a valid session cookie
      riding along. Falling through to the cookie there is the silent downgrade to a
      differently-bound credential that this ordering exists to prevent.
    * A Grant claim presented TOGETHER with a session cookie is ambiguous — two credentials that
      can name two different accounts — and ambiguity fails closed rather than picking a winner.
      Nothing legitimate does this: the app that holds a grant has no cookie jar, and the SPA that
      holds a cookie never mints a grant.
    """
    if bearer_credential(request) is not None:
        raise HTTPException(status_code=401, detail="Session or grant authentication required")
    credential = grant_credential(request)
    if credential is None:
        return await require_session_user(request, db)
    if SID_COOKIE in request.cookies:
        raise HTTPException(status_code=401, detail="Ambiguous credentials")
    user = await management_grant.authenticate_grant(db, credential)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired management grant")
    request.state.auth_kind = "grant"
    return user


PAT_PREFIX = "memos_pat_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# In-process record of when each PAT's last_used stamp was last claimed (monotonic seconds), the
# PAT-side twin of _last_seen_claimed above. Same reason: claimed synchronously BEFORE the write so
# a concurrent burst on one PAT (a satellite pulling a gallery's derived tiers) has exactly one
# coroutine attempt the UPDATE — the rest never queue behind that row's lock.
_pat_last_used_claimed: dict[int, float] = {}


logger = structlog.get_logger(__name__)


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
    # A missing entry must read as "never claimed", not "claimed at monotonic zero":
    # time.monotonic() counts from an arbitrary epoch (often boot), so a 0.0 default
    # silently refused every first claim during the first window after a fresh boot -
    # which is exactly what a just-started CI runner or a just-deployed container is.
    prev = _pat_last_used_claimed.get(pat_id)
    if prev is not None and now_mono - prev < settings.PAT_LAST_USED_THROTTLE_S:
        return False
    _pat_last_used_claimed[pat_id] = now_mono
    if len(_pat_last_used_claimed) > 4096:  # bound the map (correctness lives in the DB WHERE)
        _pat_last_used_claimed.clear()
    return True


async def stamp_pat_last_used(pat_id: int, now: datetime) -> None:
    """Persist the PAT's last_used. Only ever run as the body of a scheduled task — the claim is
    already taken by schedule_pat_last_used_stamp, so this is the write and nothing else.

    Runs in its OWN session, never the request's: the compat surface never commits its request
    session, so a stamp left pending there was rolled back at request end and the row never
    changed (MEMNG-31). A dedicated session also means the commit releases the row lock at once
    instead of holding it for the rest of the request (the PAT gallery-burst convoy), injects no
    commit boundary into caller state, and — because a failed stamp dies with its own session —
    can never expire or poison the objects authentication has already loaded."""
    # The same staleness predicate in the WHERE is the durable, cross-process/restart gate: another
    # worker (or this one after a restart) that has no in-process claim still writes at most once
    # per window, and a loser of the race updates 0 rows instead of overwriting a fresh stamp.
    stale_before = now - timedelta(seconds=settings.PAT_LAST_USED_THROTTLE_S)
    async with async_session() as sdb:
        await sdb.execute(
            update(Pat)
            .where(Pat.id == pat_id, or_(Pat.last_used.is_(None), Pat.last_used < stale_before))
            .values(last_used=now)
        )
        await sdb.commit()


# Strong references to the in-flight stamp tasks. The event loop only keeps a WEAK reference to a
# task, so a fire-and-forget one that nobody holds can be garbage-collected mid-write; this set is
# that owner, and the done callback empties it again so it cannot grow without bound.
_stamp_tasks: set[asyncio.Task[None]] = set()


def _stamp_task_done(task: asyncio.Task[None]) -> None:
    """Drop the strong reference and surface the task's failure — nothing awaits it, so an
    exception left in a scheduled task would otherwise vanish (or resurface as an
    "exception was never retrieved" warning at GC time, detached from the request)."""
    _stamp_tasks.discard(task)
    if task.cancelled():  # shutdown drain cancelled it; the row simply stays stale
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("pat_last_used_stamp_failed", exc_info=exc)


def schedule_pat_last_used_stamp(
    pat_id: int, last_used: datetime | None
) -> asyncio.Task[None] | None:
    """Claim this PAT's write window and hand the DB write to a background task.

    Returns the task iff this caller won the claim (None = nothing to do), which is also the seam
    tests await instead of sleeping. Synchronous and await-free up to the create_task, so a
    concurrent burst is still collapsed by claim_pat_stamp before anyone reaches the row.
    """
    now = datetime.now(UTC)
    if not pat_stamp_due(last_used, now):
        return None
    if not claim_pat_stamp(pat_id, time.monotonic()):
        return None
    task = asyncio.create_task(stamp_pat_last_used(pat_id, now))
    _stamp_tasks.add(task)
    task.add_done_callback(_stamp_task_done)
    return task


async def drain_pat_stamp_tasks(grace_s: float = 5.0) -> None:
    """Await the in-flight stamp tasks; cancel whatever is still running after `grace_s`.

    Called on lifespan shutdown so a stamp in flight either lands or dies deliberately: left
    alone, the loop teardown cancels it mid-write anyway, but noisily (a pending task destroyed
    with the engine already disposed). The bound matters because last_used is display-only
    bookkeeping — it must never hold a shutdown (or a test) open waiting for the DB. It is a
    grace period, not an error timeout: expiry cancels, and the caller is never raised into.
    """
    pending = tuple(_stamp_tasks)
    if not pending:
        return
    _, still_running = await asyncio.wait(pending, timeout=grace_s)
    for task in still_running:
        task.cancel()
    if still_running:
        # Awaited, not just cancelled: this returns only once each one has actually finished
        # unwinding, so the caller can rely on the queue being empty afterwards.
        await asyncio.gather(*still_running, return_exceptions=True)


async def authenticate_pat(db: AsyncSession, token: str) -> User | None:
    """Return the owning user for a valid, unexpired PAT, else None. Schedules the last_used stamp
    (at most once per throttle window) as a background task; the request never waits for it."""
    pat = (
        await db.execute(select(Pat).where(Pat.hashed_token == hash_token(token)))
    ).scalar_one_or_none()
    if pat is None:
        return None
    # Expiry is enforced HERE rather than in the callers so every bearer surface inherits it from
    # one place — the compat surface (which downgrades an unusable token to anonymous/PUBLIC-only)
    # as much as the routes that 401. An expired token is also not worth a last_used stamp.
    if pat.expires_at is not None and pat.expires_at <= datetime.now(UTC):
        return None
    # Read the FK BEFORE the stamp commits: a session configured to expire on commit would have to
    # lazy-refresh the attribute afterwards, which async IO cannot do from attribute access.
    user_id = pat.user_id
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    # Scheduled, never awaited here (MEMNG-81). The stamp needs a SECOND pooled connection while
    # this request still holds its own, so awaiting it made the request block on that second
    # checkout while holding the first — hold-and-wait, the cycle that lets a saturated pool
    # deadlock on itself once enough simultaneously-due PATs pile up. Handing the write to a task
    # breaks the cycle: this request returns and RELEASES its connection while the task waits for
    # a slot, so the holder is never the waiter. The claim is taken synchronously inside the
    # scheduler (before create_task), so a burst still collapses to one attempt, and the task
    # logs its own failure — a stamp can neither turn a valid token into a 500 nor expire the
    # user object loaded above. The guard below covers the one way SCHEDULING itself can fail:
    # create_task on a loop that is already shutting down (a request still in flight while the
    # app stops) raises, and that must not be the thing that 401/500s a valid token either.
    try:
        schedule_pat_last_used_stamp(pat.id, pat.last_used)
    except Exception:
        logger.warning("pat_last_used_stamp_failed", exc_info=True)
    return user


async def require_user(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    """Authenticate a request by bearer PAT first, else by session cookie.

    Bearer-first and fail-closed (Decision 6): once a request states a bearer credential, that
    credential decides the outcome — an unknown, revoked or expired one is a 401 even when a valid
    session cookie rides along, because ambiguous credentials must not silently downgrade to the
    weaker-bound one. A header in another scheme states no bearer claim and leaves the cookie path
    exactly as it was.
    """
    credential = bearer_credential(request)
    if credential is None:
        return await require_session_user(request, db)
    user = await authenticate_pat(db, credential)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    request.state.auth_kind = "pat"
    return user  # the last_used stamp runs off the request path (see authenticate_pat)


async def require_pat_or_user(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    """Alias of require_user, kept so the routes widened to PAT auth before this change keep the
    dependency name they were reviewed under. Now that every route accepts a bearer it carries no
    meaning of its own; the call sites collapse onto require_user in a later cleanup."""
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
