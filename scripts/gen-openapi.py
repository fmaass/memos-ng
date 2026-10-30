"""Dump the FastAPI OpenAPI schemas to openapi.json and openapi-guest.json (the codegen sources).

The frontend client + types are generated from openapi.json — never hand-written (ADR-0009). The
native guest mode is generated from openapi-guest.json: the guest door (the `ingest` role, built
by `app.ingest.factory.create_app`) serves no schema at all (`openapi_url=None`), so this snapshot
is the only place its contract exists (MEMNG-1011).

What catches a stale snapshot is `tests/test_openapi_drift.py` (openapi.json) and
`tests/test_openapi_guest.py` (openapi-guest.json), which render through `render()` and
`render_guest()` below and compare: the repo's GitHub Actions drift job is dormant (there is no
CI — MEMNG-1 was cancelled), so the local gate is the only gate. Keep the serialisation in
`_serialise()` alone, or a test starts comparing against a copy of it instead of against the
generator.
"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "openapi.json"
GUEST_OUT = ROOT / "openapi-guest.json"


def _serialise(schema: dict[str, Any]) -> str:
    """sort_keys=True makes a snapshot deterministic — FastAPI emits set-valued route methods (e.g.
    the /api/{path} catch-all) in arbitrary order, which would otherwise make the drift gate flaky.
    """
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def render() -> str:
    """Exactly what openapi.json must contain, trailing newline included.

    Imported inside the function so loading this module is side-effect-free for the drift test.
    """
    from app.main import app

    return _serialise(app.openapi())


def guest_schema() -> dict[str, Any]:
    """The guest door's API, built off-line from the very app the `ingest` role serves.

    In-process, as tests/test_ingest_roles.py builds it: `create_app(role)` takes the role as an
    argument and `APP_ROLE` is only checked in its lifespan, which never runs here — and
    `app._role_guard` only stops an ingest *process* from importing app.main, which this home-role
    process must import anyway for the nullable rewrite below.

    Only `/api/*` is the contract: the door's /health, pass page, guest assets and AASA file are
    for browsers and Apple, not for a generated client. Security is derived from each route's auth
    dependency exactly as the home schema's is, from the door's own scheme and map. The home
    schema's pool-exhaustion 503 is left out: only the home app installs the handler that sends it.
    """
    from app.ingest import guest
    from app.ingest.factory import create_app
    from app.main import attach_route_security, collapse_nullable_anyof
    from fastapi.openapi.utils import get_openapi
    from fastapi.routing import APIRoute

    door = create_app("ingest")
    routes = [r for r in door.routes if isinstance(r, APIRoute) and r.path.startswith("/api/")]
    schema = get_openapi(title=door.title, version=door.version, routes=routes)
    schema.setdefault("components", {})["securitySchemes"] = deepcopy(guest.GUEST_SECURITY_SCHEMES)
    collapse_nullable_anyof(schema)
    attach_route_security(schema, routes, guest.GUEST_DEPENDENCY_SECURITY)
    return schema


def render_guest() -> str:
    """Exactly what openapi-guest.json must contain, trailing newline included."""
    return _serialise(guest_schema())


def main() -> None:
    for out, text in ((OUT, render()), (GUEST_OUT, render_guest())):
        out.write_text(text)
        print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
