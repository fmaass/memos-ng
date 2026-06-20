import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import get_session
from app.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


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


@router.get("/login", include_in_schema=False)
async def login(request: Request, next: str = "/", _: None = Depends(auth.rate_limit())):
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
    resp = RedirectResponse(
        f"{settings.OIDC_AUTHORIZE_ENDPOINT}?{urlencode(params)}", status_code=302
    )
    resp.set_cookie(
        auth.TX_COOKIE,
        auth.encrypt({"state": state, "verifier": verifier, "next": _safe_next(next)}),
        max_age=600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
    )
    return resp


@router.get("/callback", include_in_schema=False)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: None = Depends(auth.rate_limit()),
):
    tx_cookie = request.cookies.get(auth.TX_COOKIE)
    tx = auth.decrypt(tx_cookie) if tx_cookie else None
    if not tx or not code or not state or not secrets.compare_digest(state, tx.get("state", "")):
        raise HTTPException(status_code=400, detail="Invalid OIDC state")
    tokens = await auth.exchange_code(code, tx["verifier"])
    claims = auth.validate_id_token(tokens["id_token"])
    # The id_token only carries `sub`; pull preferred_username/name/email/groups from userinfo
    # so the user is keyed by their real username, not the opaque sub UUID.
    userinfo = await auth.fetch_userinfo(tokens.get("access_token"))
    user = await auth.upsert_user(db, {**claims, **userinfo})
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
async def whoami(user: User = Depends(auth.require_user)) -> UserOut:
    return UserOut(
        subject=user.subject, display_name=user.display_name, email=user.email, role=user.role
    )


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
