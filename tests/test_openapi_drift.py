"""The committed openapi.json must be what the app actually serves.

`openapi.json` is the codegen source of truth (ADR-0009): the SPA's `schema.d.ts` — and, from
Phase 3, the Swift client — are generated from it, so a stale snapshot silently hands every
generated consumer a contract the server no longer honours. The repo's GitHub Actions drift job
never runs (CI is abandoned), which left this contract with no gate at all; this test is it.
"""

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

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


def _mappings(node: object, path: str = "#"):
    """Every mapping in the document, with the JSON pointer that reaches it."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _mappings(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _mappings(value, f"{path}/{index}")


def _inline_nullable_anyof(node: dict) -> bool:
    members = node.get("anyOf")
    if not isinstance(members, list) or len(members) != 2:
        return False
    if not any(m == {"type": "null"} for m in members):
        return False
    keep = next(m for m in members if m != {"type": "null"})
    return isinstance(keep, dict) and "type" in keep and "$ref" not in keep


def test_no_inline_nullable_anyof_survives_anywhere_in_the_document():
    """swift-openapi-generator drops a property whose schema contains a bare `null` member, so a
    single surviving one silently deletes a field from the generated client (MEMNG-105)."""
    from app.main import app

    offenders = [
        pointer for pointer, node in _mappings(app.openapi()) if _inline_nullable_anyof(node)
    ]
    assert offenders == [], f"{len(offenders)} nullable anyOf survived, e.g. {offenders[:3]}"


def test_a_nullable_field_reaches_the_client_as_a_type_array():
    from app.main import app

    archived_at = app.openapi()["components"]["schemas"]["MemoOut"]["properties"]["archived_at"]
    assert archived_at["type"] == ["string", "null"]
    assert archived_at["format"] == "date-time"
    assert archived_at["title"] == "Archived At"
    assert "anyOf" not in archived_at


def test_a_genuine_union_is_left_as_an_anyof():
    """FastAPI's own ValidationError.loc is `string | integer` — a union, not a nullable."""
    from app.main import app

    loc = app.openapi()["components"]["schemas"]["ValidationError"]["properties"]["loc"]["items"]
    assert loc == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_reference_and_wider_unions_are_not_rewritten():
    from app.main import collapse_nullable_anyof

    document = {
        "ref_plus_null": {
            "anyOf": [{"$ref": "#/components/schemas/Visibility"}, {"type": "null"}]
        },
        "three_arm": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
        "union": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        "already_a_type_array": {"type": ["string", "null"]},
    }
    untouched = deepcopy(document)
    collapse_nullable_anyof(document)
    assert document == untouched


def test_sibling_keywords_survive_the_collapse():
    from app.main import collapse_nullable_anyof

    node = {"anyOf": [{"type": "string", "maxLength": 64}, {"type": "null"}], "title": "Cursor"}
    collapse_nullable_anyof(node)
    assert node == {"type": ["string", "null"], "maxLength": 64, "title": "Cursor"}


def test_a_disagreeing_keyword_stops_the_schema_instead_of_dropping_one_side():
    from app.main import NullableMergeCollision, collapse_nullable_anyof

    with pytest.raises(NullableMergeCollision) as raised:
        collapse_nullable_anyof(
            {
                "properties": {
                    "q": {
                        "anyOf": [{"type": "string", "maxLength": 8}, {"type": "null"}],
                        "maxLength": 99,
                    }
                }
            }
        )
    assert "#/properties/q" in str(raised.value)
    assert "maxLength" in str(raised.value)


UPLOAD_PATH = "/api/uploads/{upload_uid}"


def test_the_chunked_upload_patch_declares_its_octet_stream_body():
    """The handler reads `request.stream()`, so FastAPI declares no requestBody and a generated
    client cannot express the chunk at all (MEMNG-109)."""
    from app.main import app

    body = app.openapi()["paths"][UPLOAD_PATH]["patch"]["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/octet-stream"]["schema"] == {
        "type": "string",
        "format": "binary",
    }


def test_both_upload_operations_model_the_upload_offset_response_header():
    from app.main import app

    paths = app.openapi()["paths"]
    for method in ("patch", "head"):
        header = paths[UPLOAD_PATH][method]["responses"]["200"]["headers"]["Upload-Offset"]
        assert header["schema"] == {"type": "integer"}
        assert header["description"]


def test_the_upload_offset_request_header_is_declared_exactly_once():
    """It already comes from the handler signature; the extra must not add a second one."""
    from app.main import app

    parameters = app.openapi()["paths"][UPLOAD_PATH]["patch"]["parameters"]
    offsets = [p for p in parameters if p["name"] == "Upload-Offset"]
    assert len(offsets) == 1
    assert offsets[0]["in"] == "header"


def test_the_offset_gap_conflict_is_part_of_the_upload_contract():
    """tests/test_uploads.py drives the real 409; this is the schema saying the same thing."""
    from app.main import app

    conflict = app.openapi()["paths"][UPLOAD_PATH]["patch"]["responses"]["409"]
    assert conflict["description"]
    schema = conflict["content"]["application/json"]["schema"]
    assert schema["properties"]["detail"] == {"type": "string"}
