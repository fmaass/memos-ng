def test_settings_requires_auth(client):
    assert client.get("/api/settings").status_code == 401
    assert client.patch("/api/settings", json={"theme": "light"}).status_code == 401


def test_settings_get_defaults_then_patch(logged_in_client):
    client, _ = logged_in_client
    s = client.get("/api/settings").json()
    assert s["default_visibility"] == "PUBLIC" and s["theme"] == "dark"  # prod-parity defaults

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
