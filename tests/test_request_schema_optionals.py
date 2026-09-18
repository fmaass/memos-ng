"""The request-side optional enums must reach the schema WITHOUT a null branch.

Apple's OpenAPI generator cannot express `anyOf: [$ref, {type: null}]` as a usable typed
optional, which blocks every iOS write path (PLAN_2026-08-23_native-ios-client.md, Phase 4a).
`SkipJsonSchema[None]` keeps the Python default (`None` → "the caller said nothing") while the
emitted schema is a plain `$ref` the field simply does not require.

`SkipJsonSchema` is a schema-only annotation: on its own it would still ACCEPT an explicit JSON
`null` the schema no longer describes. `RequestVisibility` (app/routers/__init__.py) pairs it with
a `BeforeValidator` so the server matches its own contract — omitting the field is the only way to
say "unset", and an explicit `null` is a 422. That is the one deliberate behaviour change here, and
it is safe because no caller sends one: the SPA builds every one of these payloads from a concrete
value (`CaptureEditor.vue:145`, `MemoCard.vue:95`, `MemoFeed.vue:80`, `SettingsView.vue:41`, and
`memoFilter.ts:91` for the query side). Omitting the field means what it always meant — every
handler treats unset as "leave it alone": `create_memo`'s `body.visibility or
user.default_visibility`, `update_memo`'s `fields.get(key) is not None`, `update_settings`'
`is not None`, `list_memos`' `if visibility:`.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _schema() -> dict:
    return json.loads((REPO_ROOT / "openapi.json").read_text())


def _query_param(schema: dict, path: str, name: str) -> dict:
    params = schema["paths"][path]["get"]["parameters"]
    matches = [p for p in params if p["name"] == name]
    assert len(matches) == 1, f"{path} has {len(matches)} `{name}` query params"
    return matches[0]


def _has_null_branch(node: dict) -> bool:
    return any(branch.get("type") == "null" for branch in node.get("anyOf", []))


BODY_FIELDS = [
    ("MemoCreate", "visibility"),
    ("MemoPatch", "visibility"),
    ("SettingsPatch", "default_visibility"),
]
QUERY_PARAMS = [("/api/memos", "visibility"), ("/api/export", "visibility")]


@pytest.mark.parametrize(("model", "field"), BODY_FIELDS)
def test_request_body_enum_field_carries_no_null_branch(model, field):
    props = _schema()["components"]["schemas"][model]["properties"]
    assert field in props, f"{model}.{field} is gone from the schema — this test walked nothing"
    node = props[field]
    assert not _has_null_branch(node), f"{model}.{field} still emits an anyOf null branch: {node}"
    assert node["$ref"] == "#/components/schemas/Visibility"
    assert field not in _schema()["components"]["schemas"][model].get("required", [])


@pytest.mark.parametrize(("path", "name"), QUERY_PARAMS)
def test_query_enum_param_carries_no_null_branch(path, name):
    param = _query_param(_schema(), path, name)
    assert not _has_null_branch(param["schema"]), (
        f"{path}?{name} still emits an anyOf null branch: {param['schema']}"
    )
    assert param["schema"]["$ref"] == "#/components/schemas/Visibility"
    assert param["required"] is False


def test_the_null_detector_actually_detects_a_null_branch():
    """Guards the assertions above: a `_has_null_branch` that always returned False would make
    every test in this file vacuous."""
    assert _has_null_branch({"anyOf": [{"$ref": "#/x"}, {"type": "null"}]})
    assert not _has_null_branch({"$ref": "#/x"})


def test_memo_create_takes_absent_and_explicit_visibility_and_rejects_null(logged_in_client):
    client, _ = logged_in_client
    client.patch("/api/settings", json={"default_visibility": "PUBLIC"})

    absent = client.post("/api/memos", json={"content": "absent visibility"})
    assert absent.status_code == 201 and absent.json()["visibility"] == "PUBLIC"

    explicit = client.post("/api/memos", json={"content": "explicit", "visibility": "PROTECTED"})
    assert explicit.status_code == 201 and explicit.json()["visibility"] == "PROTECTED"

    nulled = client.post("/api/memos", json={"content": "null", "visibility": None})
    assert nulled.status_code == 422


def test_memo_patch_takes_absent_and_explicit_visibility_and_rejects_null(logged_in_client):
    client, _ = logged_in_client
    uid = client.post("/api/memos", json={"content": "patch me", "visibility": "PRIVATE"}).json()[
        "uid"
    ]

    absent = client.patch(f"/api/memos/{uid}", json={"content": "content only"})
    assert absent.status_code == 200
    assert absent.json()["visibility"] == "PRIVATE"  # untouched

    explicit = client.patch(f"/api/memos/{uid}", json={"visibility": "PUBLIC"})
    assert explicit.status_code == 200 and explicit.json()["visibility"] == "PUBLIC"

    nulled = client.patch(f"/api/memos/{uid}", json={"visibility": None, "content": "n"})
    assert nulled.status_code == 422
    after = client.get(f"/api/memos/{uid}").json()
    assert after["visibility"] == "PUBLIC" and after["content"] != "n"  # whole patch rejected


def test_settings_patch_takes_absent_and_explicit_visibility_and_rejects_null(logged_in_client):
    client, _ = logged_in_client
    client.patch("/api/settings", json={"default_visibility": "PROTECTED"})

    absent = client.patch("/api/settings", json={"theme": "light"})
    assert absent.status_code == 200 and absent.json()["default_visibility"] == "PROTECTED"

    explicit = client.patch("/api/settings", json={"default_visibility": "PRIVATE"})
    assert explicit.status_code == 200 and explicit.json()["default_visibility"] == "PRIVATE"

    nulled = client.patch("/api/settings", json={"default_visibility": None, "theme": "dark"})
    assert nulled.status_code == 422
    after = client.get("/api/settings").json()
    assert after["default_visibility"] == "PRIVATE" and after["theme"] == "light"  # nothing applied


def test_list_and_export_still_filter_with_and_without_the_visibility_param(logged_in_client):
    client, _ = logged_in_client
    client.post("/api/memos", json={"content": "pub one", "visibility": "PUBLIC"})
    client.post("/api/memos", json={"content": "priv one", "visibility": "PRIVATE"})

    absent = client.get("/api/memos")
    assert absent.status_code == 200
    contents = {m["content"] for m in absent.json()["items"]}
    assert {"pub one", "priv one"} <= contents

    filtered = client.get("/api/memos?visibility=PUBLIC")
    assert filtered.status_code == 200
    vis = {m["visibility"] for m in filtered.json()["items"]}
    assert vis == {"PUBLIC"}

    assert client.get("/api/export").status_code == 200
    exported = client.get("/api/export?visibility=PUBLIC")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
