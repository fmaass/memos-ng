"""Dump the FastAPI OpenAPI schema to openapi.json (the codegen source of truth).

The frontend client + types are generated from this — never hand-written (ADR-0009).

What catches a stale snapshot is `tests/test_openapi_drift.py`, which renders through `render()`
below and compares: the repo's GitHub Actions drift job is dormant (there is no CI — MEMNG-1 was
cancelled), so the local gate is the only gate. Keep the serialisation in `render()` alone, or the
test starts comparing against a copy of it instead of against the generator.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    """Exactly what openapi.json must contain, trailing newline included.

    sort_keys=True makes the snapshot deterministic — FastAPI emits set-valued route
    methods (e.g. the /api/{path} catch-all) in arbitrary order, which would otherwise
    make the drift gate flaky. Imported inside the function so loading this module is
    side-effect-free for the drift test.
    """
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUT.write_text(render())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
