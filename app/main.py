from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from anyio import to_thread
from fastapi import FastAPI, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from starlette.datastructures import MutableHeaders

from app import auth
from app.config import settings
from app.csrf import OriginCsrfMiddleware
from app.database import async_session
from app.version import __version__

logger = structlog.get_logger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"


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
    logger.info("app_started", data_root=str(data_root), version=__version__)
    yield
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

from app.routers import assets as assets_router  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.routers import compat as compat_router  # noqa: E402
from app.routers import export as export_router  # noqa: E402
from app.routers import memos as memos_router  # noqa: E402
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
# Compat (/api/v1/*, /file/*) must precede the /api/* fallback and the SPA catch-all.
app.router.routes.extend(compat_router.router.routes)

# Instrument AFTER all routers are registered so route resolution stays consistent.
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


SECURITY_SCHEMES = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "memos_pat",
        "description": "A personal access token (`memos_pat_…`). Accepted on every /api/* route "
        "except /api/pats/*, which is browser-session-only.",
    },
    "sessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": auth.SID_COOKIE,
        "description": "The BFF session cookie set by the OIDC login flow.",
    },
}


def custom_openapi() -> dict[str, Any]:
    """The served schema plus the two credentials the API actually accepts.

    FastAPI derives `securitySchemes` from security dependencies, and this app authenticates in
    plain dependency functions (app/auth.py) — so the exported document, which is the ONLY contract
    the generated clients see (ADR-0009), would otherwise describe an API with no way in. The
    schemes are catalogued, not attached per operation: which credential a route takes is decided
    by its dependency, and asserting a blanket requirement here would misdescribe the public
    surfaces (/health, the /api/v1 compat reads, signed asset URLs).
    """
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


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
                headers["Content-Security-Policy"] = CSP
                if settings.is_production:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
