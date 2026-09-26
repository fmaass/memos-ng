"""The server must keep serving everything the shipped iOS client was generated against.

`tests/fixtures/ios-client/openapi.json` is the document the iOS client's generated code was built
from, copied verbatim from that repo (provenance in `SOURCE` beside it). A shipped build cannot be
recalled, so a change that removes, retypes or tightens anything that document names breaks a
client already on someone's phone — while `test_openapi_drift.py` stays green, because the served
schema and openapi.json move together. This test walks the snapshot and holds the SERVED schema to
it. Additions are allowed; the rules for enums and `required` are directional (see
`tests/ios_contract.py`).
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from tests.ios_contract import compare

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ios-client"
GENERATOR = REPO_ROOT / "scripts" / "gen-openapi.py"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_openapi", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot() -> dict:
    return json.loads((FIXTURE_DIR / "openapi.json").read_text())


def _served() -> dict:
    return json.loads(_generator().render())


def test_every_ios_snapshot_element_is_still_served():
    result = compare(_snapshot(), _served())
    print(
        f"compared {result.paths} paths, {result.operations} operations, "
        f"{result.parameters} parameters, {result.properties} properties"
    )
    assert result.failures == []
    # A walk that compared nothing is green for the wrong reason.
    assert result.paths > 0
    assert result.operations > 0
    assert result.parameters > 0
    assert result.properties > 0


def test_the_fixture_is_the_copy_its_source_file_names():
    source = dict(
        line.split("=", 1)
        for line in (FIXTURE_DIR / "SOURCE").read_text().splitlines()
        if line and not line.startswith("#")
    )
    digest = hashlib.sha256((FIXTURE_DIR / "openapi.json").read_bytes()).hexdigest()
    assert digest == source["sha256"], "the fixture was edited: re-copy it from the iOS client"


def test_a_property_removed_from_the_served_document_fails_naming_it():
    served = _served()
    mutated = copy.deepcopy(served)
    del mutated["components"]["schemas"]["HealthResponse"]["properties"]["status"]
    failures = compare(_snapshot(), mutated).failures
    assert "paths./health.get.responses.200 → HealthResponse.status: property removed" in failures
    assert compare(_snapshot(), served).failures == []


# --- hand-built pairs: each rule, both directions ------------------------------------------------


def _doc(paths: dict, schemas: dict | None = None) -> dict:
    return {"openapi": "3.1.0", "paths": paths, "components": {"schemas": schemas or {}}}


def _op(*, parameters=None, body=None, response=None) -> dict:
    op: dict = {"responses": {"200": {"description": "ok"}}}
    if parameters is not None:
        op["parameters"] = parameters
    if body is not None:
        op["requestBody"] = {"content": {"application/json": {"schema": body}}, "required": True}
    if response is not None:
        op["responses"]["200"]["content"] = {"application/json": {"schema": response}}
    return op


def _param(name: str, *, required: bool = False, schema: dict | None = None) -> dict:
    return {
        "name": name,
        "in": "query",
        "required": required,
        "schema": schema or {"type": "string"},
    }


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    node: dict = {"type": "object", "properties": properties}
    if required is not None:
        node["required"] = required
    return node


STR = {"type": "string"}
INT = {"type": "integer"}


def test_a_new_required_parameter_on_a_snapshot_operation_fails():
    snap = _doc({"/api/x": {"get": _op(parameters=[_param("a")])}})
    served = _doc({"/api/x": {"get": _op(parameters=[_param("a"), _param("b", required=True)])}})
    assert compare(snap, served).failures == [
        "paths./api/x.get.parameters.b(query): new required parameter"
    ]


def test_an_optional_parameter_becoming_required_fails():
    snap = _doc({"/api/x": {"get": _op(parameters=[_param("a")])}})
    served = _doc({"/api/x": {"get": _op(parameters=[_param("a", required=True)])}})
    assert compare(snap, served).failures == [
        "paths./api/x.get.parameters.a(query): parameter became required"
    ]


def test_a_new_required_request_body_property_fails():
    snap = _doc({"/api/x": {"post": _op(body=_obj({"a": STR}, ["a"]))}})
    served = _doc({"/api/x": {"post": _op(body=_obj({"a": STR, "b": STR}, ["a", "b"]))}})
    assert compare(snap, served).failures == [
        "paths./api/x.post.requestBody.b: request property became required"
    ]


def test_a_required_request_body_property_becoming_optional_passes():
    snap = _doc({"/api/x": {"post": _op(body=_obj({"a": STR}, ["a"]))}})
    served = _doc({"/api/x": {"post": _op(body=_obj({"a": STR}, []))}})
    assert compare(snap, served).failures == []


def test_an_added_operation_or_optional_property_passes():
    snap = _doc(
        {
            "/api/x": {
                "get": _op(parameters=[_param("a")], response={"$ref": "#/components/schemas/X"})
            }
        },
        {"X": _obj({"a": STR}, ["a"])},
    )
    served = _doc(
        {
            "/api/x": {
                "get": _op(
                    parameters=[_param("a"), _param("b")],
                    response={"$ref": "#/components/schemas/X"},
                ),
                "post": _op(body=_obj({"z": STR}, ["z"])),
            },
            "/api/y": {"get": _op(parameters=[_param("q", required=True)])},
        },
        {"X": _obj({"a": STR, "b": INT, "c": STR}, ["a", "c"])},
    )
    result = compare(snap, served)
    assert result.failures == []
    assert (result.paths, result.operations, result.parameters) == (1, 1, 1)


def test_request_enum_growth_passes_and_a_lost_accepted_value_fails():
    snap = _doc({"/api/x": {"get": _op(parameters=[_param("o", schema={"enum": ["a", "b"]})])}})
    grown = _doc(
        {"/api/x": {"get": _op(parameters=[_param("o", schema={"enum": ["a", "b", "c"]})])}}
    )
    lost = _doc({"/api/x": {"get": _op(parameters=[_param("o", schema={"enum": ["a"]})])}})
    assert compare(snap, grown).failures == []
    assert compare(snap, lost).failures == [
        "paths./api/x.get.parameters.o(query): enum no longer accepts ['b']"
    ]


def test_response_enum_growth_fails_and_narrowing_passes():
    snap = _doc({"/api/x": {"get": _op(response=_obj({"s": {"enum": ["a", "b"]}}))}})
    grown = _doc({"/api/x": {"get": _op(response=_obj({"s": {"enum": ["a", "b", "c"]}}))}})
    narrowed = _doc({"/api/x": {"get": _op(response=_obj({"s": {"enum": ["a"]}}))}})
    assert compare(snap, grown).failures == [
        "paths./api/x.get.responses.200.s: enum may now return ['c']"
    ]
    assert compare(snap, narrowed).failures == []


def test_a_response_enum_dropped_to_any_value_fails():
    snap = _doc({"/api/x": {"get": _op(response=_obj({"s": {"type": "string", "const": "a"}}))}})
    served = _doc({"/api/x": {"get": _op(response=_obj({"s": {"type": "string"}}))}})
    assert compare(snap, served).failures == [
        "paths./api/x.get.responses.200.s: enum may now return any value"
    ]


def test_response_required_strengthening_passes_and_weakening_fails():
    snap = _doc({"/api/x": {"get": _op(response=_obj({"a": STR, "b": STR}, ["a"]))}})
    stronger = _doc({"/api/x": {"get": _op(response=_obj({"a": STR, "b": STR}, ["a", "b"]))}})
    weaker = _doc({"/api/x": {"get": _op(response=_obj({"a": STR, "b": STR}, []))}})
    assert compare(snap, stronger).failures == []
    assert compare(snap, weaker).failures == [
        "paths./api/x.get.responses.200.a: response property no longer required"
    ]


def test_a_type_change_fails_including_a_nullable_one():
    snap = _doc({"/api/x": {"get": _op(response=_obj({"a": {"type": ["string", "null"]}}))}})
    retyped = _doc({"/api/x": {"get": _op(response=_obj({"a": {"type": "string"}}))}})
    same = _doc({"/api/x": {"get": _op(response=_obj({"a": {"type": ["null", "string"]}}))}})
    assert compare(snap, retyped).failures == [
        "paths./api/x.get.responses.200.a: type changed from ['null', 'string'] to ['string']"
    ]
    assert compare(snap, same).failures == []


def test_a_removed_path_operation_or_response_fails():
    snap = _doc({"/api/x": {"get": _op(), "post": _op()}, "/api/y": {"get": _op()}})
    served = _doc({"/api/x": {"get": {"responses": {"201": {"description": "created"}}}}})
    assert compare(snap, served).failures == [
        "paths./api/x.get.responses.200: response removed",
        "paths./api/x.post: operation removed",
        "paths./api/y: path removed",
    ]


def test_a_new_response_anyof_member_fails_and_a_new_request_one_passes():
    one = {"anyOf": [STR]}
    two = {"anyOf": [STR, INT]}
    snap = _doc({"/api/x": {"post": _op(body=_obj({"a": one}), response=_obj({"a": one}))}})
    served = _doc({"/api/x": {"post": _op(body=_obj({"a": two}), response=_obj({"a": two}))}})
    assert compare(snap, served).failures == [
        "paths./api/x.post.responses.200.a.anyOf[1]: new member the client cannot decode"
    ]


def test_a_reference_cycle_terminates():
    node = _obj({"child": {"$ref": "#/components/schemas/Node"}, "name": STR}, ["name"])
    snap = _doc(
        {"/api/x": {"get": _op(response={"$ref": "#/components/schemas/Node"})}}, {"Node": node}
    )
    served = copy.deepcopy(snap)
    assert compare(snap, served).failures == []
    del served["components"]["schemas"]["Node"]["properties"]["name"]
    assert compare(snap, served).failures == [
        "paths./api/x.get.responses.200 → Node.name: property removed"
    ]


def test_properties_named_like_schema_keywords_are_properties():
    snap = _doc({"/api/x": {"get": _op(response=_obj({"type": STR, "enum": STR, "items": INT}))}})
    served = _doc({"/api/x": {"get": _op(response=_obj({"enum": STR, "items": INT}))}})
    assert compare(snap, snap).failures == []
    assert compare(snap, served).failures == [
        "paths./api/x.get.responses.200.type: property removed"
    ]


def test_a_dangling_reference_raises_rather_than_comparing_clean():
    snap = _doc(
        {"/api/x": {"get": _op(response={"$ref": "#/components/schemas/X"})}},
        {"X": _obj({"a": STR})},
    )
    served = _doc({"/api/x": {"get": _op(response={"$ref": "#/components/schemas/Gone"})}})
    with pytest.raises(KeyError):
        compare(snap, served)


# --- composition and $ref siblings: normalised on both sides before any rule runs ---------------

VISIBILITY = {"type": "string", "enum": ["PRIVATE", "PROTECTED", "PUBLIC"]}


def test_a_required_request_property_added_through_allof_fails():
    base = _obj({"content": STR}, ["content"])
    snap = _doc(
        {"/api/x": {"post": _op(body={"$ref": "#/components/schemas/MemoCreate"})}},
        {"MemoCreate": base},
    )
    grown = {
        **base,
        "allOf": [{"required": ["new_required"], "properties": {"new_required": STR}}],
    }
    served = _doc(
        {"/api/x": {"post": _op(body={"$ref": "#/components/schemas/MemoCreate"})}},
        {"MemoCreate": grown},
    )
    assert compare(snap, served).failures == [
        "paths./api/x.post.requestBody → MemoCreate.new_required: request property became required"
    ]


def test_a_snapshot_property_inside_an_allof_member_is_still_found():
    snap = _doc(
        {"/api/x": {"get": _op(response={"allOf": [{"$ref": "#/components/schemas/Base"}]})}},
        {"Base": _obj({"a": STR, "b": INT}, ["a"])},
    )
    flattened = _doc({"/api/x": {"get": _op(response=_obj({"a": STR, "b": INT}, ["a"]))}})
    lost = _doc({"/api/x": {"get": _op(response=_obj({"a": STR}, ["a"]))}})
    assert compare(snap, flattened).failures == []
    assert compare(snap, lost).failures == ["paths./api/x.get.responses.200.b: property removed"]


def test_a_served_request_oneof_may_grow_but_not_drop_a_member():
    snap = _doc({"/api/x": {"post": _op(body={"oneOf": [_obj({"a": STR}), _obj({"b": INT})]})}})
    grown = _doc(
        {"/api/x": {"post": _op(body={"oneOf": [_obj({"a": STR}), _obj({"b": INT}), INT]})}}
    )
    dropped = _doc({"/api/x": {"post": _op(body={"oneOf": [_obj({"a": STR})]})}})
    assert compare(snap, grown).failures == []
    assert compare(snap, dropped).failures == [
        "paths./api/x.post.requestBody.oneOf[1]: member no longer served"
    ]


def test_a_requirement_hidden_in_a_served_only_oneof_fails():
    snap = _doc({"/api/x": {"post": _op(body=_obj({"a": STR, "b": STR}))}})
    served = _doc(
        {
            "/api/x": {
                "post": _op(
                    body={
                        **_obj({"a": STR, "b": STR}),
                        "oneOf": [{"required": ["a"]}, {"required": ["b"]}],
                    }
                )
            }
        }
    )
    assert compare(snap, served).failures == [
        "paths./api/x.post.requestBody: no served alternative accepts it"
    ]


def test_an_enum_beside_a_served_ref_narrows_what_is_accepted():
    snap = _doc(
        {
            "/api/x": {
                "post": _op(body=_obj({"visibility": {"$ref": "#/components/schemas/Visibility"}}))
            }
        },
        {"Visibility": VISIBILITY},
    )
    served = copy.deepcopy(snap)
    served["paths"]["/api/x"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["visibility"] = {"$ref": "#/components/schemas/Visibility", "enum": ["PRIVATE"]}
    assert compare(snap, served).failures == [
        "paths./api/x.post.requestBody.visibility → Visibility: "
        "enum no longer accepts ['PROTECTED', 'PUBLIC']"
    ]


def test_an_enum_beside_a_snapshot_ref_is_what_the_client_decodes():
    snap = _doc(
        {
            "/api/x": {
                "get": _op(
                    response=_obj(
                        {
                            "visibility": {
                                "$ref": "#/components/schemas/Visibility",
                                "enum": ["PRIVATE"],
                            }
                        }
                    )
                )
            }
        },
        {"Visibility": VISIBILITY},
    )
    served = _doc(
        {
            "/api/x": {
                "get": _op(
                    response=_obj({"visibility": {"$ref": "#/components/schemas/Visibility"}})
                )
            }
        },
        {"Visibility": VISIBILITY},
    )
    assert compare(snap, served).failures == [
        "paths./api/x.get.responses.200.visibility → Visibility: "
        "enum may now return ['PROTECTED', 'PUBLIC']"
    ]
