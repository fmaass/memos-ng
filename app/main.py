import asyncio
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
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
from sqlalchemy import exc as sa_exc
from sqlalchemy import text as sa_text
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import BaseRoute
from starlette.types import Receive, Scope, Send

# `_role_guard` first: it refuses an ingest role before any member module loads.
from app import _role_guard, auth  # noqa: F401
from app.config import settings
from app.csrf import OriginCsrfMiddleware
from app.database import async_session
from app.logging_privacy import configure_logging
from app.security_headers import CSP
from app.services import (
    event_stream,
    management_grant,
    reclaim,
    trip_ingest_metrics,
    upload_metrics,
    upload_queue_metrics,
)
from app.services.cursors import InvalidCursor
from app.services.digest import metrics as digest_metrics
from app.version import API_MAJOR, __version__

configure_logging()  # before the first log line: a rendered row detail cannot be taken back
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
    # The listener's socket and tasks belong to this loop; leaving them for the loop's teardown
    # logs a cancelled-task error per shutdown instead of a clean close.
    await event_stream.shutdown_hub()
    # PAT last_used stamps run as background tasks (MEMNG-81), so shutdown can catch one in
    # flight. Give it a bounded moment to land; anything slower is cancelled deliberately here
    # rather than being torn down mid-write by the loop with the engine already going away.
    await auth.drain_pat_stamp_tasks()
    # The same for a reclaim a cancelled request left behind. A cancelled request — the client
    # gone, the server stopping — is cancelled at whatever it is awaiting, and for a request in its
    # reclaim loop that is `await asyncio.shield(task)` inside `reclaim.reclaim_row_shielded`: the
    # reclaim itself runs in a task of its own, holding its own session, so the request ends there
    # and the reclaim does not. That task is what this awaits, because the alternative is the loop
    # going away with the row locked and its files half gone.
    await reclaim.drain_pending_reclaims()
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
from app.routers import collection_covers as collection_covers_router  # noqa: E402
from app.routers import collections as collections_router  # noqa: E402
from app.routers import compat as compat_router  # noqa: E402
from app.routers import export as export_router  # noqa: E402
from app.routers import map as map_router  # noqa: E402
from app.routers import members as members_router  # noqa: E402
from app.routers import memos as memos_router  # noqa: E402
from app.routers import native as native_router  # noqa: E402
from app.routers import passes as passes_router  # noqa: E402
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
app.router.routes.extend(collections_router.router.routes)
app.router.routes.extend(collection_covers_router.router.routes)
app.router.routes.extend(passes_router.router.routes)
app.router.routes.extend(push_router.router.routes)
app.router.routes.extend(settings_router.router.routes)
app.router.routes.extend(members_router.router.routes)
app.router.routes.extend(map_router.router.routes)
app.router.routes.extend(admin_digest_router.router.routes)
app.router.routes.extend(admin_export_router.router.routes)
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
# The trip pass puller's gauges, beside them on the same registry. Unlike the digest's, this
# collector reads the database on every scrape (bounded; see its module docstring for why).
trip_ingest_metrics.register_once(REGISTRY)
# Uploads and the event stream (MEMNG-962): in-process instruments where the app is the one that
# sees the event, and a per-scrape read of the tables for what the worker decides.
upload_metrics.register_once(REGISTRY)
upload_queue_metrics.register_once(REGISTRY)


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
# `{}` is the OAS spelling of "no credential also works". Only the signed asset fast path says it:
# there the HMAC signature in the URL is the credential, minted by an authenticated read
# (ADR-0009, ADR-0020). No route answers a caller that presents nothing at all (MEMNG-835).
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
    ("GET", "/file/attachments/{uid}/{filename}"): USER_SECURITY,
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


def route_security(
    route: APIRoute,
    dependency_security: Mapping[str, list[dict[str, list[str]]]] = DEPENDENCY_SECURITY,
) -> list[dict[str, list[str]]] | None:
    """The credentials this route accepts, or None when it takes none.

    Read off the route's auth dependency rather than declared alongside it, so the published
    contract cannot drift from what the route enforces (MEMNG-111). `dependency_security` is the
    member map unless a caller describes another app — the guest door's snapshot passes its own
    (scripts/gen-openapi.py), so both documents derive security the same way.
    """
    for dependency in route.dependant.dependencies:
        derived = dependency_security.get(getattr(dependency.call, "__name__", ""))
        if derived is not None:
            return derived
    for method in route.methods or ():
        inline = INLINE_AUTH_SECURITY.get((method, route.path_format))
        if inline is not None:
            return inline
    return None


def attach_route_security(
    schema: dict[str, Any],
    routes: Sequence[BaseRoute],
    dependency_security: Mapping[str, list[dict[str, list[str]]]] = DEPENDENCY_SECURITY,
) -> None:
    for route in routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        security = route_security(route, dependency_security)
        if security is None:
            continue
        operations = schema["paths"].get(route.path_format, {})
        for method in route.methods or ():
            operation = operations.get(method.lower())
            if operation is not None:
                operation["security"] = [dict(requirement) for requirement in security]


class NullableMergeCollision(Exception):
    """A nullable `anyOf` member and its parent disagree about a shared keyword."""


class NullableRefNotInlinable(Exception):
    """A `$ref` arm of a nullable `anyOf` cannot be restated as a nullable schema."""


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
    if isinstance(merged.get("enum"), list) and None not in merged["enum"]:
        # An instance must satisfy `type` AND `enum`: without null among the members the field
        # would admit no null at all, whatever its type array says (MEMNG-785).
        merged["enum"] = [*merged["enum"], None]
    return merged


def _resolve_local_ref(root: Any, ref: Any, path: str) -> dict[str, Any]:
    """The schema a document-local `$ref` names, or a refusal naming the pointer that held it."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise NullableRefNotInlinable(
            f"{path}: {ref!r} is not a reference this document can resolve on its own"
        )
    target: Any = root
    for token in ref[2:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        target = target.get(key) if isinstance(target, dict) else None
        if target is None:
            raise NullableRefNotInlinable(f"{path}: {ref!r} does not resolve inside the document")
    if not isinstance(target, dict):
        raise NullableRefNotInlinable(
            f"{path}: {ref!r} names a {type(target).__name__}, not a schema"
        )
    return target


def _inline_nullable_ref(
    node: dict[str, Any], keep: dict[str, Any], root: Any, path: str, inlining: frozenset[str]
) -> dict[str, Any]:
    """`{$ref: X} | null` restated as a copy of X whose type carries `"null"` as well.

    A `$ref` is the whole schema wherever it appears, so there is nowhere to put the second type
    without copying X out; X itself stays in `components`, because other properties reference it
    and most of them are not nullable. Its own nested references are left as references — only the
    one arm being made nullable is resolved.
    """
    ref = keep["$ref"]
    extra = sorted(set(keep) - {"$ref"})
    if extra:
        raise NullableRefNotInlinable(
            f"{path}: the reference arm carries {extra} beside its $ref; which side of a shared "
            "keyword should win is not ours to guess"
        )
    if ref in inlining:
        raise NullableRefNotInlinable(
            f"{path}: {ref!r} is nullable within itself — inlining it would never terminate"
        )
    target = _resolve_local_ref(root, ref, path)
    if not isinstance(target.get("type"), str):
        raise NullableRefNotInlinable(
            f'{path}: {ref!r} has no single `type` to extend with "null" '
            f"(type={target.get('type')!r})"
        )
    return _merge_nullable(deepcopy(target), node, path)


def collapse_nullable_anyof(
    node: Any, path: str = "#", root: Any = None, inlining: frozenset[str] = frozenset()
) -> None:
    """Rewrite `anyOf: [{type: X}, {type: "null"}]` into `type: [X, "null"]` in place.

    Pydantic spells an optional field the first way; swift-openapi-generator has no support for a
    bare `"null"` schema and DROPS the whole property rather than failing, so `MemoOut.archived_at`
    and friends simply did not exist in the generated Swift types. The two spellings say the same
    thing in JSON Schema 2020-12, so emitting the type array here retires the iOS repo's
    vendoring-time rewrite and keeps every generated client on one contract (ADR-0009, MEMNG-105).

    A two-arm `anyOf: [{$ref: …}, {type: "null"}]` — how pydantic spells an optional model field —
    goes the same way, through `_inline_nullable_ref`: the generator drops such a property just as
    readily, and `MapManifest.planet`/`.detail` were the last two left in the document. A wider
    union is not a nullable at all and is left alone. A keyword the retained member and its parent
    disagree about, and a reference that cannot be given a type array, both raise: a served
    contract that differs from the model in a way nobody chose is worse than a loud stop.

    `root` is the document the references resolve against; it threads down the walk so a nested
    call can resolve one, and `inlining` carries the references already expanded above this node.
    """
    if root is None:
        root = node
    if isinstance(node, dict):
        members = node.get("anyOf")
        if isinstance(members, list) and len(members) == 2 and any(map(_is_null_schema, members)):
            keep = next(m for m in members if not _is_null_schema(m))
            if isinstance(keep, dict) and "$ref" in keep:
                merged = _inline_nullable_ref(node, keep, root, path, inlining)
                inlining = inlining | {keep["$ref"]}
                node.clear()
                node.update(merged)
            elif isinstance(keep, dict) and isinstance(keep.get("type"), str):
                merged = _merge_nullable(keep, node, path)
                node.clear()
                node.update(merged)
        for key, value in list(node.items()):
            collapse_nullable_anyof(value, f"{path}/{key}", root, inlining)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            collapse_nullable_anyof(value, f"{path}/{index}", root, inlining)


POOL_RETRY_AFTER_SECONDS = 5
DATABASE_BUSY = "DatabaseBusy"
DATABASE_BUSY_HEAD = "DatabaseBusyHead"
_OPERATION_METHODS = frozenset({"get", "put", "post", "patch", "delete", "head", "options"})


def _database_busy_response(*, body: bool) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": "The server could not get a database connection in time. Resend the "
        "request after Retry-After seconds.",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before sending the request again.",
                "schema": {"type": "integer"},
            }
        },
    }
    if body:
        response["content"] = {
            "application/json": {
                "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}
            }
        }
    return response


def attach_database_busy_responses(schema: dict[str, Any]) -> None:
    """Publish the pool-exhaustion 503 (`_pool_exhausted` below) on every operation.

    Any route that touches the database can answer it, so it is declared here once rather than per
    route. An operation that already declares a 503 of its own keeps it untouched: the typed memo
    create/edit 503s are part of the vendored native contract, and neither replacing their schema
    nor widening it to a union is an additive change. HEAD operations get a body-less variant,
    because a HEAD answer carries no body to describe (the reason the offset HEAD declares none).
    """
    responses = schema.setdefault("components", {}).setdefault("responses", {})
    responses[DATABASE_BUSY] = _database_busy_response(body=True)
    responses[DATABASE_BUSY_HEAD] = _database_busy_response(body=False)
    for item in schema["paths"].values():
        for method, operation in item.items():
            if method not in _OPERATION_METHODS:
                continue
            name = DATABASE_BUSY_HEAD if method == "head" else DATABASE_BUSY
            operation.setdefault("responses", {}).setdefault(
                "503", {"$ref": f"#/components/responses/{name}"}
            )


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
        attach_database_busy_responses(schema)
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


# A request that waited DB_POOL_TIMEOUT for a pooled connection and got none is overload, not a
# fault of that request: answer it with a status and a pause a client can act on instead of a bare
# 500, and log it, because exhaustion is a hold-time bug to find, not a wait to tune.
@app.exception_handler(sa_exc.TimeoutError)
async def _pool_exhausted(request: Request, exc: sa_exc.TimeoutError) -> JSONResponse:
    logger.warning("db.pool_exhausted", path=request.url.path)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database busy; retry later"},
        headers={"Retry-After": str(POOL_RETRY_AFTER_SECONDS)},
    )


# Order matters: `add_middleware` inserts at the OUTSIDE of the stack, so the last one added runs
# first. SecurityHeaders must stay outermost — it wraps `send`, and a CSRF rejection generated
# inside it still gets the security headers.
app.add_middleware(OriginCsrfMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str | None = None
    apiMajor: int  # app/version.py says why this is not derived from `version`


@app.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    try:
        async with async_session() as session:
            await session.execute(sa_text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return HealthResponse(
            status="degraded", version=__version__, db="unreachable", apiMajor=API_MAJOR
        )
    return HealthResponse(status="ok", version=__version__, db="ok", apiMajor=API_MAJOR)


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


def resolve_spa_file(path: str, root: Path = FRONTEND_DIR) -> Path:
    """Map a SPA request to a real file under dist, else index.html.

    Uses Path.is_relative_to (NOT a lexical startswith, which would let a
    sibling like dist-secret/ escape the served directory) so the result can
    never point outside FRONTEND_DIR.

    Blocking (resolve + is_file touch the disk) and deliberately kept sync: callers on the
    event loop run it through to_thread.run_sync.
    """
    dist = root.resolve()
    candidate = (root / path).resolve()
    if candidate.is_relative_to(dist) and candidate.is_file():
        return candidate
    return root / "index.html"


# Everything the Vite build puts under dist/assets is content-hashed — the default
# `assets/[name]-[hash].ext` plus the explicitly hashed map worker (frontend/vite.config.ts) — so
# such a URL can never change meaning and the client may keep it without ever revalidating. That
# round-trip is what a phone on the VPN pays before a lazy route (/places, ~1 MB) can even start.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
# The shell's names are stable instead, so it has to be revalidated: an index.html kept across a
# deploy names chunks that are gone. `no-cache` is "revalidate", not "don't store". The
# revalidation costs the full couple of KB: FileResponse sends an ETag but evaluates no
# If-None-Match (unlike StaticFiles above), so the catch-all always answers 200. Cheap enough
# against a round-trip the client pays anyway, and not worth hand-rolling validator comparison
# into the highest-fan-out route in the app.
SHELL_CACHE_CONTROL = "no-cache"
# Served by the ingest role only (app/ingest/guest.py); the home catch-all refuses them.
GUEST_PATH_PREFIXES = ("p/", "guest-assets/")


class HashedAssetFiles(StaticFiles):
    """StaticFiles for the content-hashed tier: immutable, never ranged.

    Ranges are refused because the compressor wraps this mount: FileResponse would cut the slice
    out of the identity file and the compressor would then gzip it, so `Content-Range` and the
    strong ETag would describe a representation the client never received — a browser resuming an
    interrupted download would splice that onto its partial entry and reassemble garbage. A server
    may always ignore Range (RFC 9110 §14.2) and the full 200 replaces the partial entry; nothing
    under assets/ (JS, CSS, fonts) is large enough or seekable enough to want ranges anyway. The
    tiers that DO serve ranges — media, the basemap archives — are their own routes, uncompressed.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Dropped before StaticFiles builds the response: FileResponse reads them straight off the
        # scope, so this is the only place that decides for both the response and its body.
        scope = dict(scope)
        scope["headers"] = [
            (name, value) for name, value in scope["headers"] if name not in (b"range", b"if-range")
        ]
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # On the response rather than at the mount so both also ride the 304, which is what a
        # revalidating client gets for the rest of the year.
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        if response.status_code == 304:
            # The compressor adds Vary downstream of here and never sees a body to compress on a
            # 304, so the negotiation dimension the stored 200 carries has to be restated
            # (RFC 9110 §15.4.5).
            response.headers["Vary"] = "Accept-Encoding"
        else:
            response.headers["Accept-Ranges"] = "none"
        return response


# SPA static serving (frontend/dist is produced by the Vite build stage in Docker).
def mount_spa(target: FastAPI, frontend_dir: Path) -> None:
    """Register the SPA tiers on `target`: /assets, then the shell catch-all (order matters).

    Takes the app and the directory rather than reading the module globals so the tiers can be
    stood up over a fixture tree: frontend/dist is a build output and exists only in the image,
    so in a checkout the branch below is the only one that ever runs.
    """
    if not frontend_dir.is_dir():
        logger.warning("frontend_not_built", path=str(frontend_dir))

        @target.get("/{path:path}", include_in_schema=False)
        async def serve_spa_fallback(path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run 'npm run build' in frontend/."},
                status_code=503,
            )

        return

    # The compressor wraps the mounted app ALONE — never app-wide, which would also reach the
    # media tiers, the Range-served basemap archives and the /api/events stream. Text here
    # compresses ~4x, and the map chunk is the one asset a phone waits on.
    target.mount(
        "/assets",
        GZipMiddleware(HashedAssetFiles(directory=frontend_dir / "assets")),
        name="assets",
    )

    @target.get("/{path:path}", include_in_schema=False)
    async def serve_spa(path: str):
        # A pass link and the guest page's files belong to the ingest door alone: the shell must
        # not answer them (it would hand a pass URL to the member app) and neither may the tree.
        if path == "guest-assets" or path.startswith(GUEST_PATH_PREFIXES):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        # Highest-fan-out route in the app (every non-/api URL), and its resolve()+is_file()
        # hit the bind-mounted volume — off the event loop.
        return FileResponse(
            await to_thread.run_sync(resolve_spa_file, path, frontend_dir),
            headers={"Cache-Control": SHELL_CACHE_CONTROL},
        )


mount_spa(app, FRONTEND_DIR)
