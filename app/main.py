import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from anyio import to_thread
from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from prometheus_client import REGISTRY
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from starlette.datastructures import MutableHeaders
from starlette.routing import BaseRoute

from app import auth
from app.config import settings
from app.csrf import OriginCsrfMiddleware
from app.database import async_session
from app.security_headers import CSP
from app.services import management_grant
from app.services.cursors import InvalidCursor
from app.services.digest import metrics as digest_metrics
from app.version import __version__

logger = structlog.get_logger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"
# Documents the backend serves itself, kept out of the SPA bundle on purpose: they must stay
# readable and reachable at a stable URL whether or not a frontend build exists.
STATIC_DIR = Path(__file__).parent / "static"


DATA_SUBDIRS = ("originals", "assets/thumbs", "assets/mobile", "assets/web")


def _create_data_dirs_sync(data_root: Path) -> None:
    """All four tree roots in ONE off-loop hop — the data volume is a bind-mounted share, so
    each mkdir is a network round-trip."""
    for sub in DATA_SUBDIRS:
        (data_root / sub).mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime_secrets()  # fail fast if prod is on the dev-default key
    data_root = Path(settings.DATA_ROOT)
    await to_thread.run_sync(_create_data_dirs_sync, data_root)
    # The digest metrics snapshot: a supervised task refreshes it every 60 s and the custom
    # collector below reads ONLY that, so a /metrics scrape does zero database work (ADR-0009 —
    # the constraint the pool-exhaustion fix left behind). The task is exception-isolated inside
    # `digest_metrics.refresher`; if it dies anyway the collector's own staleness check makes the
    # metrics go ABSENT rather than freeze, which is what the alerting needs to see.
    digest_refresher = asyncio.create_task(digest_metrics.refresher())
    logger.info("app_started", data_root=str(data_root), version=__version__)
    yield
    digest_refresher.cancel()
    # PAT last_used stamps run as background tasks (MEMNG-81), so shutdown can catch one in
    # flight. Give it a bounded moment to land; anything slower is cancelled deliberately here
    # rather than being torn down mid-write by the loop with the engine already going away.
    await auth.drain_pat_stamp_tasks()
    logger.info("app_stopped")


app = FastAPI(
    title="memos-ng",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
)

from app.routers import admin_digest as admin_digest_router  # noqa: E402
from app.routers import admin_export as admin_export_router  # noqa: E402
from app.routers import assets as assets_router  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.routers import compat as compat_router  # noqa: E402
from app.routers import export as export_router  # noqa: E402
from app.routers import map as map_router  # noqa: E402
from app.routers import members as members_router  # noqa: E402
from app.routers import memos as memos_router  # noqa: E402
from app.routers import native as native_router  # noqa: E402
from app.routers import pats as pats_router  # noqa: E402
from app.routers import push as push_router  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402
from app.routers import uploads as uploads_router  # noqa: E402

# Add eagerly rather than include_router(): FastAPI 0.138's include_router defers to a
# lazy _IncludedRouter wrapper that the Prometheus instrumentator's route resolver can't
# read (AttributeError on .path → 500). The routers' routes already carry their full
# paths and dependencies, so extending is equivalent. (Tests authenticate via the real
# session flow rather than dependency_overrides, since eager-added routes bake their
# handler — and thus the overrides provider — at construction time.)
app.router.routes.extend(auth_router.router.routes)
app.router.routes.extend(memos_router.router.routes)
app.router.routes.extend(pats_router.router.routes)
app.router.routes.extend(export_router.router.routes)
app.router.routes.extend(uploads_router.router.routes)
app.router.routes.extend(assets_router.router.routes)
app.router.routes.extend(push_router.router.routes)
app.router.routes.extend(settings_router.router.routes)
app.router.routes.extend(members_router.router.routes)
app.router.routes.extend(admin_digest_router.router.routes)
app.router.routes.extend(admin_export_router.router.routes)
app.router.routes.extend(map_router.router.routes)
# Compat (/api/v1/*, /file/*) must precede the /api/* fallback and the SPA catch-all.
app.router.routes.extend(compat_router.router.routes)
# Likewise the two non-/api native-login URLs (/.well-known/apple-app-site-association,
# /ios/auth/callback): the SPA catch-all below would otherwise answer both with the shell, and
# Apple refuses an association file served as HTML.
app.router.routes.extend(native_router.router.routes)

# Instrument AFTER all routers are registered so route resolution stays consistent.
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# The digest's own families, on the SAME default registry the instrumentator exposes — one
# /metrics endpoint, one scrape job, no listening socket in the worker (which has no HTTP server
# at all). Registration is guarded because this module is re-imported by the test suite's app
# fixtures, and prometheus_client refuses a duplicate collector by raising.
digest_metrics.register_once(REGISTRY)


SECURITY_SCHEMES = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "memos_pat",
        "description": "A personal access token (`memos_pat_…`). Accepted on every /api/* route "
        "except /api/pats/*, which takes a browser session or a management grant and never a "
        "token, and POST /api/auth/native/exchange, which is anonymous by construction — it is how "
        "a device obtains a token in the first place, so requiring one would be circular.",
    },
    "sessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": auth.SID_COOKIE,
        "description": "The BFF session cookie set by the OIDC login flow.",
    },
    "grantAuth": {
        "type": "http",
        "scheme": management_grant.GRANT_SCHEME,
        "description": "A short-lived step-up management grant (`Authorization: Grant …`), "
        "obtained from POST /api/auth/native/manage-grant with a handoff code minted by a fresh "
        "Authelia login. Accepted only on /api/pats/*, and never mintable from a token — a device "
        "PAT manages nothing but itself.",  # the ADR-0015 rule, stated as the behaviour it is
    },
}


USER_SECURITY = [{"bearerAuth": []}, {"sessionCookie": []}]
SESSION_SECURITY = [{"sessionCookie": []}]
# The native self-service lane — renew, logout, and the token read/rename pair. Each of them acts
# ON the credential presenting it, and a session cookie names no token at all, so `require_user`
# alone described the lane wider than it is enforced (MEMNG-201): the routes read their subject
# through `presenting_token_hash`, which refuses anything but a bearer.
BEARER_SECURITY = [{"bearerAuth": []}]
# Token management: the browser session as before, or a grant for a client that holds no session.
# A bearer is in neither arm and reaches these routes under no circumstances.
SESSION_OR_GRANT_SECURITY = [{"sessionCookie": []}, {"grantAuth": []}]
# `{}` is the OAS spelling of "no credential also works": these surfaces answer an anonymous
# caller with the PUBLIC-only slice (fixtures F1) and widen when a credential is presented, so
# neither a hard requirement nor silence would describe them truthfully.
OPTIONAL_PAT_SECURITY = [{"bearerAuth": []}, {}]
SIGNED_OR_USER_SECURITY = [{"bearerAuth": []}, {"sessionCookie": []}, {}]

# Keyed on the dependency NAME, and `route_security` takes the FIRST entry it recognises on a
# route — so a route that narrows a wider dependency declares the narrow one ahead of it.
DEPENDENCY_SECURITY: dict[str, list[dict[str, list[str]]]] = {
    "presenting_token_hash": BEARER_SECURITY,
    "require_user": USER_SECURITY,
    "require_pat_or_user": USER_SECURITY,
    "require_session_user": SESSION_SECURITY,
    "require_admin": SESSION_SECURITY,
    "require_session_or_grant": SESSION_OR_GRANT_SECURITY,
    "optional_user": OPTIONAL_PAT_SECURITY,
}

# These handlers authenticate INSIDE the body, in a short-lived session released before the
# response streams (an SSE stream or a media download must not pin a pool connection for its
# lifetime), so there is no dependency to read and the requirement has to be stated here.
# The signed asset fast path is the reason /attachments/{uid}/{tier} also accepts no credential:
# a valid HMAC signature was minted against the feed's own authorization (ADR-0009).
INLINE_AUTH_SECURITY: dict[tuple[str, str], list[dict[str, list[str]]]] = {
    ("GET", "/api/events"): USER_SECURITY,
    ("GET", "/api/attachments/{uid}/rendition/{variant_key}"): USER_SECURITY,
    ("GET", "/api/attachments/{uid}/{tier}"): SIGNED_OR_USER_SECURITY,
    ("GET", "/file/attachments/{uid}/{filename}"): OPTIONAL_PAT_SECURITY,
    # The basemap routes authenticate in the handler for a reason of their own: an archive
    # transfer must not hold a pool connection for its duration (app/routers/map.py).
    ("GET", "/api/map/manifest"): USER_SECURITY,
    ("GET", "/api/map/tiles/{name}"): USER_SECURITY,
    ("HEAD", "/api/map/tiles/{name}"): USER_SECURITY,
    ("GET", "/api/map/assets/{path}"): USER_SECURITY,
}

# Operations that take no credential at all. An /api/* operation that is in neither this set nor
# one of the two maps above fails tests/test_openapi_drift.py, so a new route cannot reach the
# schema with its authentication left unsaid.
PUBLIC_OPERATIONS: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/privacy"),
    ("GET", "/api/push/vapid-public-key"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/native/exchange"),
    # The code and its PKCE verifier ARE the authority here: the code was minted by a fresh
    # Authelia authentication minutes ago (MEMNG-138 option 2), so requiring a credential on top
    # would be circular in exactly the way the device exchange above is.
    ("POST", "/api/auth/native/manage-grant"),
}


def route_security(route: APIRoute) -> list[dict[str, list[str]]] | None:
    """The credentials this route accepts, or None when it takes none.

    Read off the route's auth dependency rather than declared alongside it, so the published
    contract cannot drift from what the route enforces (MEMNG-111).
    """
    for dependency in route.dependant.dependencies:
        derived = DEPENDENCY_SECURITY.get(getattr(dependency.call, "__name__", ""))
        if derived is not None:
            return derived
    for method in route.methods or ():
        inline = INLINE_AUTH_SECURITY.get((method, route.path_format))
        if inline is not None:
            return inline
    return None


def attach_route_security(schema: dict[str, Any], routes: Sequence[BaseRoute]) -> None:
    for route in routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        security = route_security(route)
        if security is None:
            continue
        operations = schema["paths"].get(route.path_format, {})
        for method in route.methods or ():
            operation = operations.get(method.lower())
            if operation is not None:
                operation["security"] = [dict(requirement) for requirement in security]


class NullableMergeCollision(Exception):
    """A nullable `anyOf` member and its parent disagree about a shared keyword."""


def _is_null_schema(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "null" and len(node) == 1


def _merge_nullable(keep: dict[str, Any], parent: dict[str, Any], path: str) -> dict[str, Any]:
    rest = {k: v for k, v in parent.items() if k != "anyOf"}
    for key in sorted(set(keep) & set(rest)):
        if keep[key] != rest[key]:
            raise NullableMergeCollision(
                f"{path}: '{key}' differs between the anyOf member ({keep[key]!r}) and its "
                f"parent ({rest[key]!r}); merging would silently drop one"
            )
    merged = dict(keep)
    merged.update(rest)
    merged["type"] = [keep["type"], "null"]
    return merged


def collapse_nullable_anyof(node: Any, path: str = "#") -> None:
    """Rewrite `anyOf: [{type: X}, {type: "null"}]` into `type: [X, "null"]` in place.

    Pydantic spells an optional field the first way; swift-openapi-generator has no support for a
    bare `"null"` schema and DROPS the whole property rather than failing, so `MemoOut.archived_at`
    and friends simply did not exist in the generated Swift types. The two spellings say the same
    thing in JSON Schema 2020-12, so emitting the type array here retires the iOS repo's
    vendoring-time rewrite and keeps every generated client on one contract (ADR-0009, MEMNG-105).

    Only that exact two-arm inline shape is touched: `anyOf: [{$ref: …}, {type: "null"}]` cannot
    become a type array (a reference plus null needs a different fix) and a wider union is not a
    nullable at all. A keyword the retained member and its parent disagree about raises, because a
    served contract that differs from the model in a way nobody chose is worse than a loud stop.
    """
    if isinstance(node, dict):
        members = node.get("anyOf")
        if isinstance(members, list) and len(members) == 2 and any(map(_is_null_schema, members)):
            keep = next(m for m in members if not _is_null_schema(m))
            if isinstance(keep, dict) and isinstance(keep.get("type"), str) and "$ref" not in keep:
                merged = _merge_nullable(keep, node, path)
                node.clear()
                node.update(merged)
        for key, value in list(node.items()):
            collapse_nullable_anyof(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            collapse_nullable_anyof(value, f"{path}/{index}")


def custom_openapi() -> dict[str, Any]:
    """The served schema, the two credentials the API accepts, and where each one is required.

    FastAPI derives `securitySchemes` from security dependencies, and this app authenticates in
    plain dependency functions (app/auth.py) — so the exported document, which is the ONLY contract
    the generated clients see (ADR-0009), would otherwise describe an API with no way in. Each
    operation's requirement is derived from the auth dependency it actually uses rather than
    asserted in bulk, because a blanket requirement would misdescribe the surfaces that serve an
    anonymous caller (/health, the /api/v1 compat reads, signed asset URLs).
    """
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
        collapse_nullable_anyof(schema)
        attach_route_security(schema, app.routes)
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


class SecurityHeadersMiddleware:
    """Pure-ASGI response-header middleware. Deliberately NOT BaseHTTPMiddleware
    (@app.middleware('http')): that wrapper buffers the request and breaks endpoints reading the
    raw body — it hung the chunked upload PATCH (await request.body()) with a CancelledError and
    stalled upload progress at 0%. This only wraps `send` to inject headers; `receive` (the request
    body stream) passes through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                # setdefault, not assignment: a route that has to serve a DERIVED policy of
                # its own sets it on its response (app/routers/native.py widens script-src by one
                # hash), and overwriting it here would silently put that page back under a policy
                # its own markup cannot satisfy. Everything else gets the app-wide policy.
                headers.setdefault("Content-Security-Policy", CSP)
                if settings.is_production:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_with_headers)


# One translation point for every paged list (memo feed, media library, /api/v1 pageToken):
# the decoders live in the service layer and stay HTTP-free, raising `InvalidCursor`, and this
# turns it into the 422 a paging client can act on. A handler rather than a try/except per route
# so that a list added later cannot reintroduce the 500 by forgetting to guard its own decode.
@app.exception_handler(InvalidCursor)
async def _invalid_cursor(request: Request, exc: InvalidCursor) -> JSONResponse:
    logger.info("invalid_cursor", path=request.url.path, reason=str(exc))
    return JSONResponse(status_code=422, content={"detail": "Invalid cursor"})


# Order matters: `add_middleware` inserts at the OUTSIDE of the stack, so the last one added runs
# first. SecurityHeaders must stay outermost — it wraps `send`, and a CSRF rejection generated
# inside it still gets the security headers.
app.add_middleware(OriginCsrfMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str | None = None


@app.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    try:
        async with async_session() as session:
            await session.execute(sa_text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return HealthResponse(status="degraded", version=__version__, db="unreachable")
    return HealthResponse(status="ok", version=__version__, db="ok")


PRIVACY_PAGE = STATIC_DIR / "privacy-policy.html"
# `public`, deliberately unlike every media response's `private` (app/routers/assets.py): this is
# one published document, identical for every reader and addressed to readers who have not signed
# in, so a shared cache holding it leaks nothing. An hour keeps a correction from being stuck
# behind a long max-age.
PRIVACY_CACHE_CONTROL = "public, max-age=3600"


# Declared before the SPA catch-all below, which would otherwise answer it with the shell. It is
# named in PUBLIC_OPERATIONS as well: the schema pass reads each route's requirement off its auth
# dependency, and a route that has none has to say so out loud (tests/test_openapi_drift.py).
@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> FileResponse:
    """The privacy policy, served to anyone — no credential, like /health and the association file.

    A policy behind a login is not a published policy: App Store review, and any reader deciding
    whether to sign in at all, fetch this URL anonymously.
    """
    return FileResponse(
        PRIVACY_PAGE,
        media_type="text/html",
        headers={"Cache-Control": PRIVACY_CACHE_CONTROL},
    )


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
async def api_fallback(path: str):
    """Catch-all for unmatched /api/* routes — JSON 404 instead of SPA HTML.

    Excluded from the OpenAPI schema: it is an internal 404 helper, not a real
    endpoint, and a single multi-method route makes FastAPI emit non-deterministic
    operationIds (which would flake the CI drift gate).
    """
    return JSONResponse({"detail": f"/api/{path} not found"}, status_code=404)


def resolve_spa_file(path: str) -> Path:
    """Map a SPA request to a real file under dist, else index.html.

    Uses Path.is_relative_to (NOT a lexical startswith, which would let a
    sibling like dist-secret/ escape the served directory) so the result can
    never point outside FRONTEND_DIR.

    Blocking (resolve + is_file touch the disk) and deliberately kept sync: callers on the
    event loop run it through to_thread.run_sync.
    """
    dist = FRONTEND_DIR.resolve()
    candidate = (FRONTEND_DIR / path).resolve()
    if candidate.is_relative_to(dist) and candidate.is_file():
        return candidate
    return FRONTEND_DIR / "index.html"


# SPA static serving (frontend/dist is produced by the Vite build stage in Docker).
if FRONTEND_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa(path: str):
        # Highest-fan-out route in the app (every non-/api URL), and its resolve()+is_file()
        # hit the bind-mounted volume — off the event loop.
        return FileResponse(await to_thread.run_sync(resolve_spa_file, path))
else:
    logger.warning("frontend_not_built", path=str(FRONTEND_DIR))

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa_fallback(path: str):
        return JSONResponse(
            {"error": "Frontend not built. Run 'npm run build' in frontend/."},
            status_code=503,
        )
