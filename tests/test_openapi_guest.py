"""The committed openapi-guest.json must be what the guest door actually serves (MEMNG-1011).

The ingest role is built with `openapi_url=None` — the public door publishes no schema — so the
native guest mode has no contract to generate a client from unless one is committed here. These
tests make that snapshot a gated artefact like openapi.json: rendered by the same generator,
compared byte for byte, and pinned to exactly the guest API so a new guest route cannot reach the
door without reaching the contract.
"""

import importlib
import importlib.util
import inspect
import json
import re
from pathlib import Path

import pytest
from app.ingest.factory import create_app
from fastapi import FastAPI
from fastapi.routing import APIRoute

from tests.ingest_support import (
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen-openapi.py"
COMMITTED = REPO_ROOT / "openapi-guest.json"

GUEST_OPERATIONS = {
    ("POST", "/api/pass/redeem"),
    ("GET", "/api/guest/me"),
    ("POST", "/api/guest/uploads"),
    ("HEAD", "/api/guest/uploads/{upload_uid}"),
    ("PATCH", "/api/guest/uploads/{upload_uid}"),
    ("POST", "/api/guest/memos"),
    ("GET", "/api/guest/batches"),
}
_METHODS = {"get", "put", "post", "patch", "delete", "head", "options"}


def _generator():
    """Loaded by path for the reason tests/test_openapi_drift.py gives: the hyphenated script is
    un-importable by name, and rendering through it is what keeps the test honest."""
    spec = importlib.util.spec_from_file_location("gen_openapi", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(schema: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method in _METHODS
    }


def test_committed_guest_schema_matches_the_door():
    assert COMMITTED.read_text() == _generator().render_guest(), (
        "openapi-guest.json is stale — run `uv run python scripts/gen-openapi.py` and commit it."
    )


def test_guest_generator_writes_beside_openapi_json():
    generator = _generator()
    assert generator.GUEST_OUT == COMMITTED
    assert generator.GUEST_OUT.parent == generator.OUT.parent


def test_guest_schema_describes_exactly_the_guest_api():
    operations = _operations(json.loads(COMMITTED.read_text()))
    assert operations == GUEST_OPERATIONS
    paths = {path for _, path in operations}
    # Negative controls, each a route that does exist in some role: the home-interface API, a
    # member route, the door's own health probe, and the door's non-API pages.
    assert not any(path.startswith("/ingest") for path in paths)
    assert "/api/memos" not in paths and "/health" not in paths
    assert not any(path.startswith(("/p/", "/guest-assets", "/.well-known")) for path in paths)


def test_every_guest_api_route_the_door_serves_is_in_the_contract():
    """Read off the live ingest app, so a guest route added to the router but filtered out of the
    snapshot — or the reverse — fails here even if someone updates GUEST_OPERATIONS alone."""
    served = {
        (method, route.path)
        for route in create_app("ingest").routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
        for method in route.methods
    }
    assert served == GUEST_OPERATIONS
    assert _operations(_generator().guest_schema()) == served


def test_the_door_itself_still_serves_no_schema():
    app = create_app("ingest")
    assert app.openapi_url is None
    assert all(getattr(route, "path", "") != "/openapi.json" for route in app.routes)


def test_every_operation_has_an_operation_id_and_named_bodies():
    """swift-openapi-generator names each client method by operationId and each type by its
    component name; an anonymous inline object becomes an unnamed nested type nobody can use."""
    schema = json.loads(COMMITTED.read_text())
    ids = []
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in _METHODS:
                continue
            assert operation.get("operationId"), (method, path)
            ids.append(operation["operationId"])
            bodies = [operation.get("requestBody", {})] + list(operation["responses"].values())
            for body in bodies:
                for media in body.get("content", {}).values():
                    shape = media["schema"]
                    shape = shape.get("items", shape) if shape.get("type") == "array" else shape
                    assert "properties" not in shape, (method, path, shape)
    assert len(ids) == len(set(ids))
    assert {"RedeemIn", "RedeemOut", "MeOut", "Receipt"} <= set(schema["components"]["schemas"])


def test_guest_schema_version_follows_openapi_json():
    from app.version import __version__

    guest = json.loads(COMMITTED.read_text())["info"]["version"]
    home = json.loads((REPO_ROOT / "openapi.json").read_text())["info"]["version"]
    assert guest == home == __version__


def test_optional_guest_fields_reach_the_client_as_type_arrays():
    """The home schema's nullable rewrite (app.main.collapse_nullable_anyof) applies here too: the
    Swift generator drops a property spelled `anyOf: [X, {type: null}]` without a word."""
    schema = json.loads(COMMITTED.read_text())
    text = json.dumps(schema)
    assert '{"type": "null"}' not in text
    caption = schema["components"]["schemas"]["Receipt"]["properties"]["caption"]
    assert caption["type"] == ["string", "null"]
    sha256 = schema["components"]["schemas"]["GuestUploadIn"]["properties"]["sha256"]
    assert sha256["type"] == ["string", "null"] and "pattern" in sha256


# --- what each operation declares (fold round: the native client pins only what is declared) ----

OPERATION_IDS = {
    ("POST", "/api/pass/redeem"): "guestRedeem",
    ("GET", "/api/guest/me"): "guestMe",
    ("POST", "/api/guest/uploads"): "guestCreateUpload",
    ("HEAD", "/api/guest/uploads/{upload_uid}"): "guestUploadOffset",
    ("PATCH", "/api/guest/uploads/{upload_uid}"): "guestAppendUpload",
    ("POST", "/api/guest/memos"): "guestComplete",
    ("GET", "/api/guest/batches"): "guestBatches",
}

# Every refusal the document declares → the test that makes the route really send it. 422s that
# FastAPI declares for request validation are not listed; the completion's own 422 is. A
# parametrized test names the case that refuses by its index in the parametrize list (`[3]`).
_G = "tests/test_ingest_guest.py::"
_R = "tests/test_ingest_roles.py::"
_FOREIGN_401 = _R + "test_member_and_foreign_credentials_are_refused_on_every_guest_route"
_ENDED_410 = _G + "test_every_authed_guest_route_is_410_once_its_pass_has_ended"
_UPLOAD = "/api/guest/uploads/{upload_uid}"
PROVOKED = {
    ("POST", "/api/pass/redeem", 404): _G + "test_an_unknown_secret_is_404_and_counted_per_address",
    ("POST", "/api/pass/redeem", 410): _G + "test_an_ended_pass_is_410[0]",
    ("POST", "/api/pass/redeem", 429): _G + "test_every_429_tells_the_client_when_to_retry",
    ("GET", "/api/guest/me", 401): _FOREIGN_401,
    ("GET", "/api/guest/me", 410): _ENDED_410,
    ("POST", "/api/guest/uploads", 401): _FOREIGN_401,
    ("POST", "/api/guest/uploads", 410): _ENDED_410,
    ("POST", "/api/guest/uploads", 413): _G
    + "test_a_declared_size_over_the_upload_limit_is_413_at_create",
    ("POST", "/api/guest/uploads", 415): _G
    + "test_a_name_outside_the_allowlist_is_415_at_create[0]",
    ("POST", "/api/guest/uploads", 507): _G + "test_the_pass_byte_cap_and_the_volume_floor_are_507",
    ("POST", "/api/guest/uploads", 429): "tests/test_ingest_guest_caps.py::"
    + "test_the_pass_files_per_day_cap_admits_one_below_and_refuses_at_the_limit",
    ("HEAD", _UPLOAD, 401): _FOREIGN_401,
    ("HEAD", _UPLOAD, 404): _G + "test_another_devices_session_answers_as_unknown",
    ("HEAD", _UPLOAD, 410): _ENDED_410,
    ("PATCH", _UPLOAD, 401): _FOREIGN_401,
    ("PATCH", _UPLOAD, 404): _G + "test_another_devices_session_answers_as_unknown",
    ("PATCH", _UPLOAD, 409): _G + "test_upload_in_three_patches_with_a_resume_then_complete",
    ("PATCH", _UPLOAD, 410): _ENDED_410,
    ("PATCH", _UPLOAD, 413): _G + "test_a_body_over_the_patch_bound_is_413",
    ("PATCH", _UPLOAD, 429): _G + "test_every_429_tells_the_client_when_to_retry",
    ("PATCH", _UPLOAD, 507): _G
    + "test_a_body_that_would_cross_the_floor_is_507_before_any_byte_lands",
    ("POST", "/api/guest/memos", 401): _FOREIGN_401,
    ("POST", "/api/guest/memos", 409): _G + "test_an_incomplete_upload_cannot_be_completed",
    ("POST", "/api/guest/memos", 410): _ENDED_410,
    ("POST", "/api/guest/memos", 415): _G
    + "test_the_bytes_must_be_what_the_name_says_or_the_completion_is_415[3]",
    ("POST", "/api/guest/memos", 422): _G
    + "test_a_declared_hash_the_bytes_do_not_match_is_refused",
    ("GET", "/api/guest/batches", 401): _FOREIGN_401,
    ("GET", "/api/guest/batches", 410): _ENDED_410,
}
GUEST_ERROR = {"$ref": "#/components/schemas/GuestError"}
VALIDATION = {"$ref": "#/components/schemas/HTTPValidationError"}


def _document() -> dict:
    return json.loads(COMMITTED.read_text())


def _each_operation(schema: dict):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in _METHODS:
                yield method.upper(), path, operation


def _is_framework_422(status: str, response: dict) -> bool:
    schema = response.get("content", {}).get("application/json", {}).get("schema")
    return status == "422" and schema == VALIDATION


def test_operation_ids_are_the_short_explicit_names():
    assert {(m, p): op["operationId"] for m, p, op in _each_operation(_document())} == OPERATION_IDS


def test_every_declared_refusal_is_mapped_to_a_provoking_test():
    declared = {
        (method, path, int(status))
        for method, path, operation in _each_operation(_document())
        for status, response in operation["responses"].items()
        if int(status) >= 400 and not _is_framework_422(status, response)
    }
    assert declared == set(PROVOKED)


def _resolve(node: str):
    """`tests/x.py::name[i]` → the test function and the parametrize values of its case `i`."""
    file, _, name = node.partition("::")
    name, _, index = name.partition("[")
    function = getattr(importlib.import_module(file.removesuffix(".py").replace("/", ".")), name)
    marks = [m for m in getattr(function, "pytestmark", []) if m.name == "parametrize"]
    if not marks:
        assert not index, f"{node} names a case of an unparametrized test"
        return function, {}
    assert len(marks) == 1 and index, f"{node} must name one case of its single parametrize"
    names, cases = marks[0].args[0], marks[0].args[1]
    names = [n.strip() for n in names.split(",")] if isinstance(names, str) else list(names)
    case = cases[int(index.removesuffix("]"))]
    values = case.values if hasattr(case, "values") else case if len(names) > 1 else (case,)
    return function, dict(zip(names, values, strict=True))


@pytest.mark.parametrize("node", sorted(set(PROVOKED.values())), ids=lambda n: n.split("::")[1])
async def test_each_mapped_test_really_draws_the_refusals_it_is_mapped_to(
    node,
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    """The mapped test is RUN here, under a recorder of what the guest door actually answered — so
    a test gutted to `pass`, or a mapping pointed at a test that never draws that refusal, fails
    naming the (method, path, status) it no longer provokes. The route comes from the one Starlette
    matched (`scope["route"]`): a router-level 404 for an unknown path answers no declared route."""
    answered: set[tuple[str, str, int]] = set()
    serve = FastAPI.__call__

    async def recording(app, scope, receive, send):
        status = 0

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await serve(app, scope, receive, capture)
        route = scope.get("route")
        if app.title == "memos-ng ingest" and isinstance(route, APIRoute):
            answered.add((scope["method"], route.path, status))

    monkeypatch.setattr(FastAPI, "__call__", recording)
    function, params = _resolve(node)
    fixtures = {"door": door, "home_iface": home_iface, "ingest_env": ingest_env}
    arguments = {**fixtures, "monkeypatch": monkeypatch, **params}
    await function(**{name: arguments[name] for name in inspect.signature(function).parameters})
    claimed = {refusal for refusal, mapped in PROVOKED.items() if mapped == node}
    assert claimed - answered == set(), f"{node} did not draw these declared refusals"


def test_refusals_carry_the_body_the_door_sends():
    """An HTTPException renders `{"detail": "<text>"}`; a HEAD answer carries no body at all."""
    for method, path, operation in _each_operation(_document()):
        for status, response in operation["responses"].items():
            if int(status) < 400 or _is_framework_422(status, response):
                continue
            assert response["description"], (method, path, status)
            if method == "HEAD":
                assert "content" not in response, (method, path, status)
            elif (method, path, status) == ("POST", "/api/guest/memos", "422"):
                schema = response["content"]["application/json"]["schema"]
                assert schema == {"anyOf": [VALIDATION, GUEST_ERROR]}
            else:
                assert response["content"] == {"application/json": {"schema": GUEST_ERROR}}
    error = _document()["components"]["schemas"]["GuestError"]
    assert error["properties"] == {"detail": {"title": "Detail", "type": "string"}}
    assert error["required"] == ["detail"]


def test_every_429_declares_retry_after_and_every_offset_answer_declares_upload_offset():
    integer = {"type": "integer"}
    document = _document()
    limited = [
        op["responses"]["429"] for *_, op in _each_operation(document) if "429" in op["responses"]
    ]
    assert len(limited) == 3  # redeem, create (file caps) and append
    for response in limited:
        assert response["headers"]["Retry-After"]["schema"] == integer
    upload = document["paths"][_UPLOAD]
    for method in ("head", "patch"):
        assert upload[method]["responses"]["200"]["headers"]["Upload-Offset"]["schema"] == integer
    assert "content" not in upload["head"]["responses"]["200"]


def test_the_append_declares_its_raw_body_and_offset_header():
    patch = _document()["paths"][_UPLOAD]["patch"]
    assert patch["requestBody"] == {
        "required": True,
        "content": {
            "application/offset+octet-stream": {"schema": {"type": "string", "format": "binary"}}
        },
    }
    (offset,) = [p for p in patch["parameters"] if p["in"] == "header"]
    assert offset["name"] == "Upload-Offset" and offset["schema"]["type"] == "integer"


def test_the_already_received_answer_is_declared_beside_the_created_one():
    responses = _document()["paths"]["/api/guest/uploads"]["post"]["responses"]
    out = {"$ref": "#/components/schemas/GuestUploadOut"}
    assert responses["200"]["content"]["application/json"]["schema"] == out
    assert responses["201"]["content"]["application/json"]["schema"] == out


def test_security_is_the_guest_bearer_exactly_where_the_route_requires_a_guest():
    """Read off each live route's dependency, so a new authed route that loses its requirement —
    or the redeem route gaining one — fails here."""
    from app.ingest.guest import GUEST_TOKEN_PREFIX, require_guest

    document = _document()
    scheme = document["components"]["securitySchemes"]["guestBearer"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    assert GUEST_TOKEN_PREFIX in scheme["description"]
    assert set(document["components"]["securitySchemes"]) == {"guestBearer"}
    authed = {
        (method, route.path)
        for route in create_app("ingest").routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
        if any(d.call is require_guest for d in route.dependant.dependencies)
        for method in route.methods
    }
    assert authed == GUEST_OPERATIONS - {("POST", "/api/pass/redeem")}
    for method, path, operation in _each_operation(document):
        expected = [{"guestBearer": []}] if (method, path) in authed else None
        assert operation.get("security") == expected, (method, path)


def test_limits_is_a_named_model_with_the_keys_the_web_client_reads():
    schemas = _document()["components"]["schemas"]
    ref = {"$ref": "#/components/schemas/GuestLimits"}
    assert schemas["MeOut"]["properties"]["limits"] == ref
    assert schemas["RedeemOut"]["properties"]["limits"] == ref
    keys = ["max_file_bytes", "chunk_size", "max_patch_bytes"]  # frontend/src/guest/api.ts Limits
    assert schemas["GuestLimits"]["required"] == keys
    assert set(schemas["GuestLimits"]["properties"]) == set(keys)


def test_every_reference_resolves():
    document = _document()
    text = json.dumps(document)
    refs = set(re.findall(r'"\$ref": "#/components/(\w+)/(\w+)"', text))
    assert refs, "positive control: the document does reference components"
    for section, name in refs:
        assert name in document["components"][section], (section, name)
