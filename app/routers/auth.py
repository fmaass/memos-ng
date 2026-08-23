import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import get_session
from app.models import Pat, User
from app.services import native_auth
from app.services import pats as pats_svc

router = APIRouter(prefix="/api/auth", tags=["auth"])

# The query value that switches /login and /callback into device-handoff mode. Anything else —
# including the parameter's absence — is the web flow, unchanged.
NATIVE_CLIENT = "ios"


def _safe_next(target: str | None) -> str:
    """Only allow a same-site local path as the post-login redirect — never an absolute
    or protocol-relative URL ('//evil.com', 'https://evil.com', '/\\evil') — so the `next`
    param can't be used as an open redirect."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


class UserOut(BaseModel):
    subject: str
    display_name: str | None
    email: str | None
    role: str
    avatar: str | None = None
    # Which credential answered this request — a native client that pasted a token needs to see
    # that the token, not a leftover browser session, is what is speaking for it.
    auth_kind: str


@router.get("/login", include_in_schema=False)
async def login(
    request: Request,
    next: str = "/",
    client: str | None = None,
    code_challenge: str | None = None,
    device: str | None = None,
    _: None = Depends(auth.rate_limit()),
):
    """Start the OIDC login. `client=ios` brokers the result to a native app instead of the SPA.

    The request sent to Authelia is the SAME one either way — same client, redirect_uri and scopes,
    differing only in the per-request randoms. That is the whole point of the design (Decision 1):
    no second Authelia client, no new redirect URI, and therefore no household SSO restart. The
    app's own PKCE challenge and its device name are BFF state; they ride the encrypted transaction
    cookie and are never forwarded to the IdP, which knows nothing about a device being involved.
    """
    native = client == NATIVE_CLIENT
    if native:
        if not native_auth.valid_code_challenge(code_challenge):
            raise HTTPException(status_code=422, detail="invalid_code_challenge")
        if not native_auth.valid_device_name(device):
            raise HTTPException(status_code=422, detail="invalid_device")
    verifier, challenge = auth.new_pkce()
    state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "scope": "openid profile email groups",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    tx = {"state": state, "verifier": verifier}
    if native:
        # `next` is a SPA route and means nothing to an app; the redirect target in this mode is
        # NATIVE_CALLBACK_URL, so accepting one here would only widen the open-redirect surface.
        tx |= {"client": NATIVE_CLIENT, "code_challenge": code_challenge, "device": device}
    else:
        tx["next"] = _safe_next(next)
    resp = RedirectResponse(
        f"{settings.OIDC_AUTHORIZE_ENDPOINT}?{urlencode(params)}", status_code=302
    )
    resp.set_cookie(
        auth.TX_COOKIE,
        auth.encrypt(tx),
        max_age=600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
    )
    return resp


def _native_redirect(**params: str) -> RedirectResponse:
    """302 back to the app's HTTPS callback, dropping the transaction cookie on the way out."""
    resp = RedirectResponse(f"{settings.NATIVE_CALLBACK_URL}?{urlencode(params)}", status_code=302)
    resp.delete_cookie(auth.TX_COOKIE)
    return resp


@router.get("/callback", include_in_schema=False)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(auth.rate_limit()),
):
    tx_cookie = request.cookies.get(auth.TX_COOKIE)
    tx = auth.decrypt(tx_cookie) if tx_cookie else None
    # State is checked before ANY branch reads the mode: whether this request is a device login at
    # all is itself read out of the transaction cookie, so an unverified callback must not be able
    # to reach the native error path (or any other) by claiming to be one.
    if not tx or not state or not secrets.compare_digest(state, tx.get("state", "")):
        raise HTTPException(status_code=400, detail="Invalid OIDC state")
    native = tx.get("client") == NATIVE_CLIENT
    if error:
        if native:
            # An app cannot read an HTTP 400 out of an authentication session — it only sees the
            # callback URL it was told to wait for — so a refused login has to come back as one.
            return _native_redirect(error=error)
        raise HTTPException(status_code=400, detail="Invalid OIDC state")
    if not code:
        raise HTTPException(status_code=400, detail="Invalid OIDC state")
    tokens = await auth.exchange_code(code, tx["verifier"])
    claims = auth.validate_id_token(tokens["id_token"])
    # The id_token only carries `sub`; pull preferred_username/name/email/groups from userinfo
    # so the user is keyed by their real username, not the opaque sub UUID.
    userinfo = await auth.fetch_userinfo(tokens.get("access_token"))
    user = await auth.upsert_user(db, {**claims, **userinfo})
    if native:
        # No web session and no `memosng_sid`: the browser that ran this login was a transient
        # authentication session owned by the app, and leaving a household-length cookie behind in
        # it would be a credential nobody is holding. The OIDC tokens are dropped with this scope —
        # the PAT the app is about to exchange for is the device's only credential (Decision 1).
        handoff = await native_auth.mint_code(db, user, tx["code_challenge"], tx["device"])
        await db.commit()
        return _native_redirect(code=handoff)
    sid = await auth.create_session(db, user, tokens)
    await db.commit()
    resp = RedirectResponse(_safe_next(tx.get("next")), status_code=302)  # defence in depth
    resp.delete_cookie(auth.TX_COOKIE)
    resp.set_cookie(
        auth.SID_COOKIE,
        sid,
        max_age=settings.SESSION_ABSOLUTE_DAYS * 86400,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
    )
    return resp


@router.get("/user", response_model=UserOut)
async def whoami(request: Request, user: User = Depends(auth.require_user)) -> UserOut:
    return UserOut(
        subject=user.subject,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        avatar=user.avatar,
        auth_kind=request.state.auth_kind,
    )


class NativeExchangeIn(BaseModel):
    code: str = Field(min_length=1, max_length=512)
    code_verifier: str = Field(min_length=43, max_length=128)


class NativeExchangeOut(BaseModel):
    # The plaintext token exists in exactly one response in this application's lifetime: this one.
    token: str
    expires_at: datetime
    user: UserOut


@router.post("/native/exchange", response_model=NativeExchangeOut)
async def native_exchange(
    body: NativeExchangeIn, db: AsyncSession = Depends(get_session)
) -> NativeExchangeOut:
    """Trade a handoff code plus its PKCE verifier for a device PAT.

    Anonymous by construction — this is how a device gets its first credential, so requiring one
    would be circular. It is also cookie-free, which is what keeps CSRF out of the picture: the
    request carries no ambient authority a browser could be tricked into attaching.

    Every failure is a 400 with a stable machine-readable `detail`; none of them distinguishes
    "wrong" from "not yours", and all of them are terminal — the client starts a new login rather
    than retrying, because a code is burned by the attempt whatever its outcome.
    """
    try:
        claim = await native_auth.consume_code(db, body.code)
    except native_auth.NativeCodeRejected as rejected:
        raise HTTPException(status_code=400, detail=rejected.reason) from rejected
    # Committed BEFORE the verifier is judged: one attempt per code is the property that makes a
    # leaked code worthless to a guesser, and it only holds if the burn survives the 400 below.
    await db.commit()
    if not native_auth.verifier_matches(body.code_verifier, claim.code_challenge):
        raise HTTPException(status_code=400, detail="verifier_mismatch")
    user = (await db.execute(select(User).where(User.id == claim.user_id))).scalar_one_or_none()
    if user is None:  # the account was deleted between the login and this exchange
        raise HTTPException(status_code=400, detail="code_invalid")
    # Bounded, unlike a PAT minted from the SPA with no expiry: a device token lives on hardware
    # that gets lost, and nobody revokes what they have not noticed is gone.
    expires_at = datetime.now(UTC) + timedelta(days=settings.NATIVE_TOKEN_DAYS)
    _, token = await pats_svc.create_pat(db, user, claim.device_name, expires_at=expires_at)
    await db.commit()
    return NativeExchangeOut(
        token=token,
        expires_at=expires_at,
        user=UserOut(
            subject=user.subject,
            display_name=user.display_name,
            email=user.email,
            role=user.role,
            avatar=user.avatar,
            auth_kind="pat",
        ),
    )


@router.post("/native/logout", status_code=204)
async def native_logout(
    request: Request,
    user: User = Depends(auth.require_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Revoke the presenting device token, and only that one.

    A bearer may revoke ITSELF and nothing else (Decision 5): the rest of token management stays
    behind a browser session, so a leaked device token can neither mint successors that outlive its
    revocation nor take another device offline. A session cookie is refused here for the same
    reason from the other side — the SPA revokes through `/api/pats/{id}`, which knows which token
    it is talking about.
    """
    if getattr(request.state, "auth_kind", None) != "pat":
        raise HTTPException(status_code=401, detail="Token authentication required")
    credential = auth.bearer_credential(request)
    if credential is None:  # unreachable while auth_kind == "pat"; refuse rather than delete blind
        raise HTTPException(status_code=401, detail="Token authentication required")
    await db.execute(
        delete(Pat).where(Pat.hashed_token == auth.hash_token(credential), Pat.user_id == user.id)
    )
    await db.commit()


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_session)) -> JSONResponse:
    # Kill the local session AND hand back an RP-initiated logout URL (id_token_hint +
    # post_logout_redirect_uri) so the SPA can end the Authelia session too (plan T1.2).
    id_token = None
    sid = request.cookies.get(auth.SID_COOKIE)
    if sid:
        ws = await auth.load_session(db, sid)
        if ws:
            tokens = auth.decrypt(ws.encrypted_tokens) or {}
            id_token = tokens.get("id_token")
            await db.delete(ws)
            await db.commit()
    params = {"post_logout_redirect_uri": settings.OIDC_POST_LOGOUT_REDIRECT_URI}
    if id_token:
        params["id_token_hint"] = id_token
    logout_url = f"{settings.OIDC_END_SESSION_ENDPOINT}?{urlencode(params)}"
    resp = JSONResponse({"ok": True, "logout_url": logout_url})
    resp.delete_cookie(auth.SID_COOKIE)
    return resp
