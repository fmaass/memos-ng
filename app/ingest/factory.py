"""Build the app of one ingest role. Only the routes named here exist: no OpenAPI document, no
docs, no SPA or static mount, no /metrics, no /privacy, no API fallback, no member middleware."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from anyio import to_thread
from fastapi import APIRouter, FastAPI, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from sqlalchemy import text
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.database import async_session
from app.ingest import core, guest, service_api
from app.version import __version__

logger = structlog.get_logger(__name__)

IngestRole = Literal["ingest", "ingest-home"]

health_router = APIRouter()


class IngestHealth(BaseModel):
    status: str
    role: str
    version: str
    db: str


@health_router.get("/health", response_model=IngestHealth)
async def health(response: Response) -> IngestHealth:
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return IngestHealth(
            status="degraded", role=settings.APP_ROLE, version=__version__, db="unreachable"
        )
    return IngestHealth(status="ok", role=settings.APP_ROLE, version=__version__, db="ok")


class DoorHeaders:
    """Headers every answer from the door carries: nothing is sniffed, cached or referred on."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _make_dirs_sync() -> None:
    for sub in ("parts", "blobs"):
        (core.ingest_root() / sub).mkdir(parents=True, exist_ok=True)


async def _purge_loop() -> None:
    while True:
        try:
            await service_api.purge()
        except Exception as exc:  # the loop outlives one failed sweep; the next one retries
            logger.error("ingest.purge_failed", error=str(exc)[:200])
        await asyncio.sleep(settings.INGEST_PURGE_INTERVAL_S)


def _lifespan(role: IngestRole):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.APP_ROLE != role:
            raise RuntimeError(f"the {role} app started with APP_ROLE={settings.APP_ROLE}")
        settings.validate_runtime_secrets()
        await to_thread.run_sync(_make_dirs_sync)
        # One of the two processes sweeps: the one home talks to, which also takes the acks.
        sweeper = asyncio.create_task(_purge_loop()) if role == "ingest-home" else None
        logger.info("ingest_started", role=role, version=__version__)
        yield
        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
        logger.info("ingest_stopped", role=role)

    return lifespan


def create_app(role: IngestRole) -> FastAPI:
    app = FastAPI(
        title=f"memos-ng {role}",
        version=__version__,
        lifespan=_lifespan(role),
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        # A near-miss path is a 404, never a redirect onto an allowed one.
        redirect_slashes=False,
    )
    # Added eagerly, as app.main does (FastAPI's lazy include wrapper), so the runtime route list
    # is exactly what serves.
    app.router.routes.extend(health_router.routes)
    if role == "ingest":
        app.router.routes.extend(guest.router.routes)
        app.add_exception_handler(RequestValidationError, guest.validation_refused)
    elif role == "ingest-home":
        app.router.routes.extend(service_api.router.routes)
    else:
        raise ValueError(f"not an ingest role: {role!r}")
    app.add_middleware(DoorHeaders)
    return app
