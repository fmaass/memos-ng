"""The ASGI app of the ingest roles (`uvicorn app.ingest.asgi:app`); the home role refuses it."""

from app.config import settings
from app.ingest.factory import create_app

if settings.APP_ROLE == "home":
    raise RuntimeError("app.ingest.asgi serves the ingest roles; APP_ROLE=home serves app.main:app")

app = create_app(settings.APP_ROLE)
