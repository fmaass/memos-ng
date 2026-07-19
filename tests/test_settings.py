def test_settings_requires_auth(client):
    assert client.get("/api/settings").status_code == 401
    assert client.patch("/api/settings", json={"theme": "light"}).status_code == 401


def test_settings_get_defaults_then_patch(logged_in_client):
    client, _ = logged_in_client
    s = client.get("/api/settings").json()
    assert s["default_visibility"] == "PUBLIC" and s["theme"] == "dark"  # prod-parity defaults
    # The per-user Aura frame settings were removed (delivery is now a system-level album upload) —
    # the settings payload must no longer expose them.
    assert "frame_send_to" not in s and "frame_send_from" not in s

    r = client.patch("/api/settings", json={"default_visibility": "PRIVATE", "theme": "light"})
    assert r.status_code == 200
    s2 = client.get("/api/settings").json()
    assert s2["default_visibility"] == "PRIVATE" and s2["theme"] == "light"  # persisted


def test_memo_create_uses_per_user_default_visibility(logged_in_client):
    """Regression fix: a memo posted without an explicit visibility takes the user's default
    (PUBLIC, matching prod) — NOT the old hard-coded PRIVATE."""
    client, _ = logged_in_client
    m = client.post("/api/memos", json={"content": "no explicit visibility"}).json()
    assert m["visibility"] == "PUBLIC"  # was PRIVATE before the fix

    client.patch("/api/settings", json={"default_visibility": "PRIVATE"})
    m2 = client.post("/api/memos", json={"content": "now private by default"}).json()
    assert m2["visibility"] == "PRIVATE"  # honors the changed default

    # an explicit visibility still wins
    m3 = client.post("/api/memos", json={"content": "explicit", "visibility": "PROTECTED"}).json()
    assert m3["visibility"] == "PROTECTED"


def test_avatar_set_validation_and_clear(logged_in_client):
    client, _ = logged_in_client
    # rejects a non-data:image URL
    assert client.patch("/api/settings", json={"avatar": "http://x/a.png"}).status_code == 400

    img = "data:image/png;base64,iVBORw0KGgo="
    r = client.patch("/api/settings", json={"avatar": img})
    assert r.status_code == 200 and r.json()["avatar"] == img
    assert client.get("/api/auth/user").json()["avatar"] == img  # surfaced on the user

    assert client.patch("/api/settings", json={"avatar": ""}).json()["avatar"] is None  # cleared


def test_frame_test_endpoint_removed(logged_in_client):
    # The SMTP frame-test endpoint was removed with the frame-send path; it must 404 now.
    client, _ = logged_in_client
    assert client.post("/api/settings/frame-test").status_code == 404
