"""Imported first by `app.main`, the household app: an ingest role must never build it.

The ingest roles serve `app.ingest.asgi:app` and never import a member router (trip pass P2). A
process started with `APP_ROLE=ingest` against `app.main:app` by mistake would otherwise serve every
member route on the public door, so the import itself refuses.
"""

from app.config import settings

if settings.APP_ROLE != "home":
    raise RuntimeError(
        f"app.main is the home app; APP_ROLE={settings.APP_ROLE} serves app.ingest.asgi:app"
    )
