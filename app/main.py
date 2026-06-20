from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from app.config import settings
from app.database import async_session
from app.version import __version__

logger = structlog.get_logger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_root = Path(settings.DATA_ROOT)
    for sub in ("originals", "assets/thumbs", "assets/mobile", "assets/web"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    logger.info("app_started", data_root=str(data_root), version=__version__)
    yield
    logger.info("app_stopped")


app = FastAPI(
    title="memos-ng",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


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
    return HealthResponse(status="ok", version=__version__)


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
        return FileResponse(resolve_spa_file(path))
else:
    logger.warning("frontend_not_built", path=str(FRONTEND_DIR))

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa_fallback(path: str):
        return JSONResponse(
            {"error": "Frontend not built. Run 'npm run build' in frontend/."},
            status_code=503,
        )
