"""Dump the FastAPI OpenAPI schema to openapi.json (the codegen source of truth).

The frontend client + types are generated from this — never hand-written (ADR-0009).
CI regenerates and fails on drift.
"""

import json
from pathlib import Path

from app.main import app

out = Path(__file__).resolve().parent.parent / "openapi.json"
# sort_keys=True makes the snapshot deterministic — FastAPI emits set-valued route
# methods (e.g. the /api/{path} catch-all) in arbitrary order, which would otherwise
# make the CI drift gate flaky.
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(f"wrote {out} ({out.stat().st_size} bytes)")
