import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import Row, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.config import settings
from app.database import get_session
from app.models import Pat, User
from app.services import management_grant, native_auth
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
    purpose: str | None = None,
    _: None = Depends(auth.rate_limit()),
):
    """Start the OIDC login. `client=ios` brokers the result to a native app instead of the SPA.

    The request sent to Authelia is the SAME one however this route is called — same client,
    redirect_uri and scopes, differing only in the per-request randoms. That is the whole point of
    the design (Decision 1): no second Authelia client, no new redirect URI, and therefore no
    household SSO restart. The app's own PKCE challenge, its device name and the lane it is running
    (`purpose`) are BFF state; they ride the encrypted transaction cookie and are never forwarded to
    the IdP, which knows nothing about a device — or a step-up — being involved.

    `purpose=manage` runs the same round trip for a management grant instead of a device token
    (MEMNG-138 option 2). It is a native-lane parameter: the SPA already holds a session and has no
    step-up to ask for, so naming it outside `client=ios` is refused rather than ignored, and so is
    a purpose this build does not offer — a value that silently degraded to a plain login would mint
    the wrong credential for a caller that asked for the other one.
    """
    native = client == NATIVE_CLIENT
    if purpose is not None and not native:
        raise HTTPException(status_code=422, detail="invalid_purpose")
    purpose = purpose if purpose is not None else native_auth.PURPOSE_LOGIN
    if purpose not in native_auth.PURPOSES:
        raise HTTPException(status_code=422, detail="invalid_purpose")
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
        tx |= {
            "client": NATIVE_CLIENT,
            "code_challenge": code_challenge,
            "device": device,
            "purpose": purpose,
        }
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
        # The lane is stamped on the code ROW here, read out of the transaction cookie that is
        # about to be deleted: after this response nothing is left that could say what the login
        # was for. A tx minted before this column existed carries no purpose and is a device login,
        # which is what it was — a rolling deploy lands mid-flight logins on the new code.
        handoff = await native_auth.mint_code(
            db,
            user,
            tx["code_challenge"],
            tx["device"],
            purpose=tx.get("purpose", native_auth.PURPOSE_LOGIN),
        )
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
    # The plaintext token exists in exactly one response per mint — this one — whether the mint is
    # the first exchange or a renewal, which returns this same shape by design.
    token: str
    expires_at: Rfc3339Datetime
    user: UserOut


def _native_token_response(user: User, token: str, expires_at: datetime) -> NativeExchangeOut:
    """The one shape a device ever receives a token in, so exchange and renewal cannot drift apart
    — a native client parses a renewal with the same decoder it parsed its first token with."""
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


async def _redeem_handoff_code(
    db: AsyncSession, body: NativeExchangeIn, purpose: str
) -> tuple[User, native_auth.NativeCodeClaim]:
    """Burn a `purpose` handoff code and return who it was minted for, or raise the 400 saying why.

    ONE implementation for both redemption lanes rather than two that look alike: the order here is
    the security property — burn, commit, THEN judge the verifier — and a copy of it is a copy that
    can drift. The purpose is a parameter of the atomic consume itself, so a code minted for the
    other lane matches nothing and is refused without being burned.

    Every failure is a 400 with a stable machine-readable `detail`; none of them distinguishes
    "wrong" from "not yours", and all of them are terminal — the client starts a new login rather
    than retrying, because a code that matched is burned by the attempt whatever its outcome.
    """
    try:
        claim = await native_auth.consume_code(db, body.code, purpose=purpose)
    except native_auth.NativeCodeRejected as rejected:
        raise HTTPException(status_code=400, detail=rejected.reason) from rejected
    # Committed BEFORE the verifier is judged: one attempt per code is the property that makes a
    # leaked code worthless to a guesser, and it only holds if the burn survives the 400 below.
    await db.commit()
    if not native_auth.verifier_matches(body.code_verifier, claim.code_challenge):
        raise HTTPException(status_code=400, detail="verifier_mismatch")
    user = (await db.execute(select(User).where(User.id == claim.user_id))).scalar_one_or_none()
    if user is None:  # the account was deleted between the login and this redemption
        raise HTTPException(status_code=400, detail="code_invalid")
    return user, claim


@router.post("/native/exchange", response_model=NativeExchangeOut)
async def native_exchange(
    body: NativeExchangeIn, db: AsyncSession = Depends(get_session)
) -> NativeExchangeOut:
    """Trade a handoff code plus its PKCE verifier for a device PAT.

    Anonymous by construction — this is how a device gets its first credential, so requiring one
    would be circular. It is also cookie-free, which is what keeps CSRF out of the picture: the
    request carries no ambient authority a browser could be tricked into attaching.

    Only a code minted for the login lane is redeemable here. A management code presented to this
    route is refused
    like any unknown one (and left unburned): the two lanes ask Authelia for the same thing but buy
    very different credentials, and a manage code spent here would hand a year-long device token to
    a flow that asked for ten minutes of token management.
    """
    user, claim = await _redeem_handoff_code(db, body, native_auth.PURPOSE_LOGIN)
    # Bounded, unlike a PAT minted from the SPA with no expiry: a device token lives on hardware
    # that gets lost, and nobody revokes what they have not noticed is gone.
    expires_at = datetime.now(UTC) + timedelta(days=settings.NATIVE_TOKEN_DAYS)
    _, token = await pats_svc.create_pat(db, user, claim.device_name, expires_at=expires_at)
    await db.commit()
    return _native_token_response(user, token, expires_at)


class ManageGrantOut(BaseModel):
    """A step-up grant, returned once and stored only as a hash — like a PAT's plaintext.

    `expires_at` is part of the contract rather than a client-side guess: the app has to know when
    the management screen it is holding this for goes cold, and the server's deadline is the only
    one that decides anything.
    """

    grant: str
    expires_at: Rfc3339Datetime


@router.post("/native/manage-grant", response_model=ManageGrantOut)
async def native_manage_grant(
    body: NativeExchangeIn, db: AsyncSession = Depends(get_session)
) -> ManageGrantOut:
    """Trade a MANAGEMENT handoff code plus its PKCE verifier for a short-lived management grant.

    This is the only way a grant comes into existence: the code was
    minted by a fresh Authelia authentication minutes ago, and nothing a device already holds can
    produce one — a PAT cannot, and neither can an existing browser session. That is the whole
    point of a step-up: the capability to mint and revoke this account's tokens is bought with an
    authentication, never with a token.

    Anonymous by construction, exactly like `/native/exchange`: the code and the verifier ARE the
    authority, and the request carries no ambient authority a browser could be tricked into
    attaching. A code minted for the login lane is refused here, and that refusal does not burn
    it: the purpose is
    part of the atomic match, so a code minted for the other lane matches nothing and stays
    redeemable on the route it was minted for.
    """
    # Why the refusal cannot burn the code: the purpose is a parameter of the atomic consume inside
    # `_redeem_handoff_code`, so a code minted for the login lane never matches here.
    user, _ = await _redeem_handoff_code(db, body, native_auth.PURPOSE_MANAGE)
    grant, expires_at = await management_grant.mint_grant(db, user)
    await db.commit()
    return ManageGrantOut(grant=grant, expires_at=expires_at)


def presenting_token_hash(
    request: Request,
    _user: User = Depends(auth.require_user),
) -> str:
    """The sha256 of the bearer this request is speaking with — or a 401.

    The one seam every self-service route reads its subject through. Each of them acts on the
    credential that presented itself, so a session cookie — which names no token at all — is
    refused here rather than four times over (Decision 5/6).

    A DEPENDENCY rather than a call inside the handler, for two halves of one reason. It nests
    `require_user`, whose `auth_kind` stamp is the thing this reads, so running after it is
    structural instead of a sentence in a docstring; and it is the name DEPENDENCY_SECURITY reads,
    so the exported contract narrows to bearer-only with the enforcement rather than beside it
    (MEMNG-201). Declare it AHEAD of `require_user` on the route — the schema pass takes the first
    auth dependency it recognises. On its own it authenticates nothing.
    """
    if getattr(request.state, "auth_kind", None) != "pat":
        raise HTTPException(status_code=401, detail="Token authentication required")
    credential = auth.bearer_credential(request)
    if credential is None:  # unreachable while auth_kind == "pat"; refuse rather than act blind
        raise HTTPException(status_code=401, detail="Token authentication required")
    return auth.hash_token(credential)


async def _renew_rejection(db: AsyncSession, hashed_token: str) -> HTTPException:
    """Why the conditional rotate matched nothing — read back, never inferred.

    The token authenticated moments ago, so its absence here means it stopped existing in between:
    a concurrent renewal took it (exactly the race the single statement exists to settle) or the
    owner revoked it. Either way the client is holding a dead credential and logs in again.

    `id` is selected only so an absent row stays distinguishable from a present one whose expiry
    is NULL — the difference between "gone" and "never renewable", which is the whole verdict.
    """
    row = (
        await db.execute(select(Pat.id, Pat.expires_at).where(Pat.hashed_token == hashed_token))
    ).one_or_none()
    if row is not None and row.expires_at is None:
        return HTTPException(status_code=400, detail="token_not_renewable")
    return HTTPException(status_code=401, detail="Invalid or revoked token")


# One rule governs this whole self-service block, and the exported descriptions state it as
# behaviour rather than pointing at where it was decided (Decision 5, ADR-0015): a bearer may act
# on nothing but the token it presents — renew it, revoke it, read it — while token management
# proper (minting, naming or revoking ANOTHER device's token) takes what `require_session_or_grant`
# accepts — a browser session or a step-up management grant — and never a bearer.
@router.post("/native/renew", response_model=NativeExchangeOut)
async def native_renew(
    hashed_token: str = Depends(presenting_token_hash),
    user: User = Depends(auth.require_user),
    db: AsyncSession = Depends(get_session),
) -> NativeExchangeOut:
    """Rotate the presenting device token: mint a successor and revoke the presenter, atomically.

    A device token is deliberately bounded — it lives on hardware that gets lost, and nobody
    revokes what they have not noticed is gone — which without this route means every phone set up
    a year ago signs out one morning with no warning and no way back except a full login. Renewal
    refreshes that lifetime from USE instead of removing the bound.

    It ROTATES rather than extends: the successor is a new secret and the presenter is gone in the
    same statement, so a token copied out of a backup or a log stops working at the owner's next
    renewal instead of living the full year twice over.

    Restricted to a token that carries an expiry. Nothing marks a device token as one — carrying
    an `expires_at` is the only thing that distinguishes it from a token the SPA minted, and only
    imperfectly — and only a token that expires can silently sign out, so an unbounded PAT is
    refused
    (`token_not_renewable`) rather than rotated: token management proper takes a browser session or
    a step-up management grant, never the token being presented, and this route reaches no further
    than the class it exists for.

    Bearer-only, like `native/logout`: the presenting token is the subject of the request, and a
    session cookie names no token at all.
    """
    now = datetime.now(UTC)
    # ONE statement, for the reason `native_auth.consume_code` is one: reading the row, judging it
    # renewable and then deleting it would let two concurrent renewals both find a live token and
    # both mint a successor — leaving one of them orphaned on a device that thinks it is signed in.
    # The conditional DELETE ... RETURNING makes the database the arbiter: the second writer blocks
    # on the row lock, re-evaluates against the committed version and matches nothing. Expiry is in
    # the WHERE as well, so the rotate is fail-closed however it is reached.
    revoked = (
        await db.execute(
            delete(Pat)
            .where(
                Pat.hashed_token == hashed_token,
                Pat.user_id == user.id,
                Pat.expires_at.is_not(None),
                Pat.expires_at > now,
            )
            .returning(Pat.name)
        )
    ).one_or_none()
    if revoked is None:
        raise await _renew_rejection(db, hashed_token)
    # The device name rides across unchanged: the successor is the same phone in the token list,
    # not a new entry the owner has to recognise.
    expires_at = now + timedelta(days=settings.NATIVE_TOKEN_DAYS)
    _, token = await pats_svc.create_pat(db, user, revoked.name, expires_at=expires_at)
    await db.commit()
    return _native_token_response(user, token, expires_at)


@router.post("/native/logout", status_code=204)
async def native_logout(
    hashed_token: str = Depends(presenting_token_hash),
    user: User = Depends(auth.require_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Revoke the presenting device token, and only that one.

    A bearer may revoke ITSELF and nothing else: the rest of token management takes a browser
    session or a step-up management grant obtained through the native login, never a token, so a
    leaked device token can neither mint successors that outlive its revocation nor take another
    device offline. A session cookie is refused here for the same reason from the other side — the
    SPA revokes through `/api/pats/{pat_id}`, which knows which token it is talking about.
    """
    await db.execute(
        delete(Pat).where(Pat.hashed_token == hashed_token, Pat.user_id == user.id)
    )
    await db.commit()


class NativeTokenOut(BaseModel):
    """What a device may learn about the credential it is already holding.

    No `id`, no stored hash, and never the plaintext: this row is the answer to "how long does
    this token have, and when did I last use it", not a second copy of the secret. Withholding the
    id also keeps the shape honest: a device acts on no token but the one it presents, so there is
    no sibling here to act on and no handle to act with.
    """

    name: str
    created_at: Rfc3339Datetime
    last_used: Rfc3339Datetime | None
    expires_at: Rfc3339Datetime | None


class NativeTokenRename(BaseModel):
    # The bounds `/api/pats` mints under (PatCreate.name), deliberately identical: one rule for a
    # token's name whichever surface set it, and `pats.name` is 255 wide either way.
    name: str = Field(min_length=1, max_length=255)


# Selected once and shared by both verbs, so the read and the rename cannot answer with different
# shapes — the same reason `_native_token_response` exists for exchange and renewal.
_SELF_ROW = (Pat.name, Pat.created_at, Pat.last_used, Pat.expires_at)

_SelfRow = Row[tuple[str, datetime, datetime | None, datetime | None]]


def _self_row_response(row: _SelfRow) -> NativeTokenOut:
    return NativeTokenOut(
        name=row.name,
        created_at=row.created_at,
        last_used=row.last_used,
        expires_at=row.expires_at,
    )


@router.get("/native/token", response_model=NativeTokenOut)
async def native_token(
    hashed_token: str = Depends(presenting_token_hash),
    user: User = Depends(auth.require_user),
    db: AsyncSession = Depends(get_session),
) -> NativeTokenOut:
    """The presenting device token's own row, and only that one.

    A device knows its expiry from the mint response and nowhere else, which means an app that was
    reinstalled — or simply restarted after a year — cannot tell the owner how long this phone has
    left or when it last spoke. This is the read that closes that, and it reaches no further than
    the credential making the request: a leaked token learns nothing it does not already hold, so
    the route carries no security decision at all.

    Bearer-only, like `native/logout` and `native/renew`. Token management proper takes a browser
    session or a step-up management grant — `/api/pats` is where a credential that names another
    token lives; a bearer manages nothing but itself.
    """
    row = (
        await db.execute(
            select(*_SELF_ROW).where(Pat.hashed_token == hashed_token, Pat.user_id == user.id)
        )
    ).one_or_none()
    if row is None:  # revoked between authentication and this read; report it as the 401 it is
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    return _self_row_response(row)


@router.patch("/native/token", response_model=NativeTokenOut)
async def rename_native_token(
    body: NativeTokenRename,
    hashed_token: str = Depends(presenting_token_hash),
    user: User = Depends(auth.require_user),
    db: AsyncSession = Depends(get_session),
) -> NativeTokenOut:
    """Rename the presenting device token — the only field of it a device may change.

    The name is what the owner reads in the token list, and today it is fixed at the mint from the
    device name the app sent; a phone that was renamed, or handed on, keeps the old label forever.

    Scoped to the presenting row by the same WHERE the read uses, in ONE statement whose RETURNING
    is the response: nothing here can widen to a sibling, and a row that stopped existing between
    authentication and the write matches nothing rather than renaming the wrong token.
    """
    row = (
        await db.execute(
            update(Pat)
            .where(Pat.hashed_token == hashed_token, Pat.user_id == user.id)
            .values(name=body.name)
            .returning(*_SELF_ROW)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    await db.commit()
    return _self_row_response(row)


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
