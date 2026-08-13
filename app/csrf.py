"""Request-origin CSRF defence for cookie-authenticated mutations.

The BFF session rides an httpOnly `memosng_sid` cookie set with SameSite=Lax. SameSite is
SITE-scoped, not origin-scoped: every household app on `*.discomarder.live` is the SAME site, so a
page served by a sibling subdomain can make the browser attach this session cookie to a request
aimed at memos-ng — SameSite refuses nothing there. What such a request cannot fake is the
browser-set `Origin` (or, where a navigation omits it, the `Referer`): page script cannot forge
either. Comparing them against this deployment's own origin is therefore the check that separates
the SPA's own mutations from a sibling's forgery.

Enforcement lives in ONE ASGI middleware wired in `app.main` rather than in a per-route dependency:
routes are registered by `app.router.routes.extend(...)`, so a route added later cannot forget to
opt in (and `FastAPI(dependencies=...)` would not reach those eagerly-extended routes at all).
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import SID_COOKIE
from app.config import settings

logger = structlog.get_logger(__name__)

# Methods that carry no side effect by contract, and which browsers may issue cross-site without
# script involvement anyway. Everything else (POST/PUT/PATCH/DELETE and any future verb) is guarded.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def origin_of(url: str) -> str:
    """`scheme://host[:port]`, lowercased, of an absolute URL — `''` if `url` is not absolute.

    Lowercasing only normalises the comparison (scheme and host are case-insensitive); it can never
    turn a foreign origin into this one.
    """
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def canonical_origin() -> str:
    """This deployment's own public origin, derived from `OIDC_REDIRECT_URI`.

    That setting is the URL the IdP sends the browser back to, so it is by construction the origin
    browsers use to reach this app — and it cannot silently drift, because a wrong value breaks
    login outright. No new env var is introduced for the same fact.
    """
    return origin_of(settings.OIDC_REDIRECT_URI)


def stated_origin(origin: str | None, referer: str | None) -> str:
    """The origin the browser states for a request: `Origin`, else the origin of `Referer`.

    `Origin: null` is an OPAQUE origin (sandboxed iframe, some privacy modes) and is never this
    app's, so it counts as *not stated* — it must fail the comparison, not fall through to a
    `Referer` the same context could still be sending.
    """
    if origin:
        return "" if origin.strip().lower() == "null" else origin_of(origin)
    return origin_of(referer or "")


def csrf_reject_reason(
    method: str, headers: Mapping[str, str], cookies: Mapping[str, str]
) -> str | None:
    """Why this request must be refused, or `None` to let it through. Pure — no I/O, no DB."""
    if method.upper() in SAFE_METHODS:
        return None
    if SID_COOKIE not in cookies:
        # No ambient authority to abuse: PAT/Bearer satellites and curl carry no session cookie, so
        # nothing here can be riding a browser's stored credentials. (A PAT arriving *together with*
        # a session cookie is deliberately NOT exempted: routes that depend on `require_user` ignore
        # `Authorization` entirely and authenticate from the cookie, so honouring the header would
        # hand any header-setting caller a way around this check.)
        return None
    expected = canonical_origin()
    stated = stated_origin(headers.get("origin"), headers.get("referer"))
    if not stated:
        return "Missing Origin/Referer on a cookie-authenticated request"
    if stated != expected:
        # Fails closed if `expected` is somehow empty (misconfigured redirect URI): an unverifiable
        # origin must not become a matching one.
        return f"Cross-origin request from {stated} refused"
    return None


class OriginCsrfMiddleware:
    """Refuse cookie-authenticated mutations that do not state this app's own origin.

    Pure-ASGI, deliberately NOT `BaseHTTPMiddleware` — for the same reason as
    `SecurityHeadersMiddleware`: that wrapper buffers the request and breaks endpoints reading the
    raw body (it hung the chunked upload PATCH). Only request headers are inspected here; `receive`
    is passed through untouched, so streaming uploads are unaffected. The rejection happens before
    the app is called at all, so no route side effect can run.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        reason = csrf_reject_reason(request.method, request.headers, request.cookies)
        if reason is None:
            await self.app(scope, receive, send)
            return
        logger.warning(
            "csrf_rejected",
            method=request.method,
            path=request.url.path,
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
        )
        response = JSONResponse({"detail": reason}, status_code=403)
        await response(scope, receive, send)
