"""The committed openapi.json must be what the app actually serves.

`openapi.json` is the codegen source of truth (ADR-0009): the SPA's `schema.d.ts` — and, from
Phase 3, the Swift client — are generated from it, so a stale snapshot silently hands every
generated consumer a contract the server no longer honours. The repo's GitHub Actions drift job
never runs (CI is abandoned), which left this contract with no gate at all; this test is it.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen-openapi.py"


def _generator():
    """Load `scripts/gen-openapi.py` by path — its hyphen makes it un-importable by name, and
    rendering through the generator itself is the point: a copy of the serialisation here could
    drift from the script and the test would still be green."""
    spec = importlib.util.spec_from_file_location("gen_openapi", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_openapi_matches_the_served_schema():
    generator = _generator()
    assert (REPO_ROOT / "openapi.json").read_text() == generator.render(), (
        "openapi.json is stale — run `uv run python scripts/gen-openapi.py` and "
        "`cd frontend && npm run gen:api`, then commit both."
    )


def test_generator_writes_where_the_frontend_reads_from():
    assert _generator().OUT == REPO_ROOT / "openapi.json"


def test_schema_declares_the_two_credentials_the_api_accepts():
    """FastAPI derives nothing here — authentication lives in plain dependencies — so the exported
    contract only mentions a credential because app.main.custom_openapi puts it there."""
    from app import auth
    from app.main import app

    schemes = app.openapi()["components"]["securitySchemes"]
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"
    assert schemes["sessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": auth.SID_COOKIE,
        "description": schemes["sessionCookie"]["description"],
    }


def test_custom_openapi_still_describes_the_routes():
    """The override rebuilds the document from app.routes; a rebuild that lost the routes would
    still produce a valid — and useless — schema, and the drift test alone would not notice."""
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/memos" in paths and "/api/pats" in paths and "/api/auth/user" in paths
