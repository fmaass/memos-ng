"""Dump the FastAPI OpenAPI schema to openapi.json (the codegen source of truth).

The frontend client + types are generated from this — never hand-written (ADR-0009).
CI regenerates and fails on drift.
"""

import json
from pathlib import Path

from app.main import app

out = Path(__file__).resolve().parent.parent / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
print(f"wrote {out} ({out.stat().st_size} bytes)")
