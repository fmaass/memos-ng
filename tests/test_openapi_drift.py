"""The committed openapi.json must be what the app actually serves.

`openapi.json` is the codegen source of truth (ADR-0009): the SPA's `schema.d.ts` — and, from
Phase 3, the Swift client — are generated from it, so a stale snapshot silently hands every
generated consumer a contract the server no longer honours. The repo's GitHub Actions drift job
never runs (CI is abandoned), which left this contract with no gate at all; this test is it.
"""

import importlib.util
import json
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


def test_schema_declares_the_credentials_the_api_accepts():
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
    # The step-up grant is its OWN scheme, not a second flavour of bearer: a generated client that
    # sent it as a Bearer would be refused, and one that could not tell them apart would leak a
    # device token onto the routes a token may never reach (MEMNG-138 option 2).
    assert schemes["grantAuth"]["type"] == "http"
    assert schemes["grantAuth"]["scheme"] == "grant"
    assert schemes["grantAuth"]["scheme"] != schemes["bearerAuth"]["scheme"]


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


def test_the_committed_snapshot_publishes_the_attachment_content_digest():
    """Read off the COMMITTED file, not the live app: a generated client (SPA `schema.d.ts`, the
    Swift client) only ever sees this snapshot, and an external consumer verifying a delivered file
    by digest needs the field on both attachment shapes — the single-attachment meta and the ref
    inside a memo. Nullable, because most rows carry no digest (see AttachmentMeta.sha256)."""
    schemas = json.loads((REPO_ROOT / "openapi.json").read_text())["components"]["schemas"]
    for name in ("AttachmentMeta", "AttachmentRef"):
        sha = schemas[name]["properties"]["sha256"]
        assert sha["type"] == ["string", "null"], (name, sha)
        assert "sha256" in schemas[name]["required"], name  # present-and-null, never absent


def test_a_genuine_union_is_left_as_an_anyof():
    """FastAPI's own ValidationError.loc is `string | integer` — a union, not a nullable."""
    from app.main import app

    loc = app.openapi()["components"]["schemas"]["ValidationError"]["properties"]["loc"]["items"]
    assert loc == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_reference_and_wider_unions_are_not_rewritten():
    from app.main import collapse_nullable_anyof

    document = {
        "ref_plus_null": {"anyOf": [{"$ref": "#/components/schemas/Visibility"}, {"type": "null"}]},
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


def test_the_offset_head_declares_no_response_body():
    """Read off the COMMITTED file, because a generated client only ever sees this snapshot: a
    HEAD answer carries no body, so an operation that declares one leaves the generated call
    waiting for bytes that never arrive and the published operation is unusable as declared
    (MEMNG-198). The `Upload-Offset` header is the entire answer, and it stays declared."""
    ok = json.loads((REPO_ROOT / "openapi.json").read_text())["paths"][UPLOAD_PATH]["head"][
        "responses"
    ]["200"]
    assert "content" not in ok, ok
    assert ok["headers"]["Upload-Offset"]["schema"] == {"type": "integer"}


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


def _schema_operations():
    """Every operation the document publishes, as (method, path, operation)."""
    from app.main import app
    from fastapi.routing import APIRoute

    document = app.openapi()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        for method in route.methods:
            operation = document["paths"].get(route.path_format, {}).get(method.lower())
            if operation is not None:
                yield method, route.path_format, operation


def test_every_published_operation_states_its_credential_or_is_a_declared_public_one():
    """The drift guard for MEMNG-111: a route added later either carries an auth dependency the
    schema pass understands, or its author has to say out loud that it is public."""
    from app.main import PUBLIC_OPERATIONS

    unstated = [
        (method, path)
        for method, path, operation in _schema_operations()
        if "security" not in operation and (method, path) not in PUBLIC_OPERATIONS
    ]
    assert unstated == [], (
        f"{unstated} reached the schema with no security requirement — give the route its auth "
        "dependency, add it to INLINE_AUTH_SECURITY if it authenticates in the handler, or "
        "declare it public in PUBLIC_OPERATIONS"
    )


def test_the_public_allow_list_names_only_live_operations():
    from app.main import PUBLIC_OPERATIONS

    published = {(method, path) for method, path, _ in _schema_operations()}
    assert PUBLIC_OPERATIONS <= published, PUBLIC_OPERATIONS - published


def test_a_declared_public_operation_carries_no_requirement():
    from app.main import PUBLIC_OPERATIONS

    for method, path, operation in _schema_operations():
        if (method, path) in PUBLIC_OPERATIONS:
            assert "security" not in operation, (method, path)


def test_the_pat_surface_takes_a_session_or_a_grant_and_never_a_bearer():
    """A PAT must not be able to mint or revoke PATs — /api/pats/* takes require_session_or_grant,
    whose two arms are the browser session and a step-up management grant. `bearerAuth` appearing
    here in any form would be the contract announcing exactly what ADR-0015 forbids."""
    pat_operations = [
        (method, path, operation)
        for method, path, operation in _schema_operations()
        if path.startswith("/api/pats")
    ]
    assert pat_operations
    for method, path, operation in pat_operations:
        assert operation["security"] == [{"sessionCookie": []}, {"grantAuth": []}], (method, path)


# The native self-service lane: every operation whose subject is the credential presenting it.
NATIVE_SELF_SERVICE = {
    ("post", "/api/auth/native/renew"),
    ("post", "/api/auth/native/logout"),
    ("get", "/api/auth/native/token"),
    ("patch", "/api/auth/native/token"),
}


def test_the_native_self_service_lane_publishes_a_bearer_and_nothing_else():
    """Read off the COMMITTED file, the only thing a generated client sees. Each of these acts ON
    the token that presented it, so `presenting_token_hash` refuses a session cookie — which names
    no token at all — and a contract that published one anyway would be wider than the lane it
    describes, sending every generated client at a credential the routes reject (MEMNG-201)."""
    document = json.loads((REPO_ROOT / "openapi.json").read_text())
    for method, path in sorted(NATIVE_SELF_SERVICE):
        assert document["paths"][path][method]["security"] == [{"bearerAuth": []}], (method, path)


def test_the_bearer_only_lane_is_exactly_the_routes_that_read_the_presenting_token():
    """The set above is written by hand, so a fifth route joining the lane later would leave it
    over-published with nothing to say so. This reads the lane back off the routes instead: it is
    the ones that take `presenting_token_hash` as a dependency, and the two must agree."""
    from app.main import app
    from app.routers.auth import presenting_token_hash
    from fastapi.routing import APIRoute

    reading_the_presenter = {
        (method.lower(), route.path_format)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if any(d.call is presenting_token_hash for d in route.dependant.dependencies)
    }
    assert reading_the_presenter == NATIVE_SELF_SERVICE


def test_the_narrower_credential_is_the_one_the_lane_publishes():
    """`route_security` returns the FIRST auth dependency it recognises, so the bearer-only one has
    to be declared ahead of `require_user` on each of these routes; declared after it, the lane
    would quietly publish the session cookie again. This is that ordering, stated as a test."""
    from app.main import app
    from fastapi.routing import APIRoute

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = {(method.lower(), route.path_format) for method in route.methods}
        if not methods & NATIVE_SELF_SERVICE:
            continue
        names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
        assert names.index("presenting_token_hash") < names.index("require_user"), route.path_format


def test_a_bearer_route_states_both_credentials():
    from app.main import app

    assert app.openapi()["paths"]["/api/memos"]["get"]["security"] == [
        {"bearerAuth": []},
        {"sessionCookie": []},
    ]


def test_a_handler_that_authenticates_inline_is_not_published_as_public():
    """/api/events opens its own session inside the handler, so nothing in the route signature
    says it authenticates — the schema must say it anyway."""
    from app.main import app

    assert app.openapi()["paths"]["/api/events"]["get"]["security"] == [
        {"bearerAuth": []},
        {"sessionCookie": []},
    ]


def test_the_compat_reads_publish_their_optional_credential():
    from app.main import app

    assert app.openapi()["paths"]["/api/v1/memos"]["get"]["security"] == [{"bearerAuth": []}, {}]


def test_a_vanished_memo_is_a_declared_404_on_both_patch_and_delete():
    """MEMNG-285. Both operations answer 404 for a memo that is not there on THREE paths —
    `_require_owned` (never existed, or not the caller's) and the two MEMNG-267 lock-loss raises —
    and FastAPI infers a response from none of them, because none is a return type. The runtime
    answers are driven by tests/test_lock_protocol_concurrency.py
    (`test_a_patch_that_loses_its_memo_answers_404_not_500`,
    `test_a_delete_that_loses_its_memo_answers_404_not_500`); this is the schema saying the same
    thing. The body shape is asserted, not just the status: these raise FastAPI's own
    `HTTPException`, so `detail` is a plain string, and declaring this router's nested
    `code`/`message` family here would swap one contract lie for another."""
    from app.main import app

    document = app.openapi()
    operations = document["paths"]["/api/memos/{uid}"]
    for method in ("patch", "delete"):
        not_found = operations[method]["responses"]["404"]
        assert not_found["description"], method
        ref = not_found["content"]["application/json"]["schema"]["$ref"]
        model = document["components"]["schemas"][ref.removeprefix("#/components/schemas/")]
        assert model["required"] == ["detail"], method
        # `type: string`, not a `$ref` to a nested detail object: the plain shape is the assertion.
        assert model["properties"]["detail"]["type"] == "string", method
