from app.config import settings
from app.services import frame_share


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


def test_avatar_set_validation_and_clear(logged_in_client):
    client, _ = logged_in_client
    # rejects a non-data:image URL
    assert client.patch("/api/settings", json={"avatar": "http://x/a.png"}).status_code == 400

    img = "data:image/png;base64,iVBORw0KGgo="
    r = client.patch("/api/settings", json={"avatar": img})
    assert r.status_code == 200 and r.json()["avatar"] == img
    assert client.get("/api/auth/user").json()["avatar"] == img  # surfaced on the user

    assert client.patch("/api/settings", json={"avatar": ""}).json()["avatar"] is None  # cleared


def test_frame_settings_roundtrip_and_validation(logged_in_client):
    client, _ = logged_in_client
    s = client.get("/api/settings").json()
    assert s["frame_send_to"] is None and s["frame_send_from"] is None  # unset by default

    r = client.patch(
        "/api/settings",
        json={"frame_send_to": "f@send.auraframes.com", "frame_send_from": "me@me.com"},
    )
    assert r.status_code == 200
    s2 = client.get("/api/settings").json()
    assert s2["frame_send_to"] == "f@send.auraframes.com" and s2["frame_send_from"] == "me@me.com"

    # invalid email → 422 (pydantic field validator), value unchanged
    assert client.patch("/api/settings", json={"frame_send_to": "not-an-email"}).status_code == 422
    assert client.get("/api/settings").json()["frame_send_to"] == "f@send.auraframes.com"

    # "" clears
    assert client.patch("/api/settings", json={"frame_send_to": ""}).json()["frame_send_to"] is None


def test_frame_send_to_must_be_aura_address(logged_in_client):
    client, _ = logged_in_client
    # A shaped but non-Aura recipient is rejected — the server SMTP account may only send to the
    # frame's ingestion domain, not an arbitrary address.
    assert client.patch("/api/settings", json={"frame_send_to": "me@icloud.com"}).status_code == 422
    # frame_send_from stays shape-only (Aura membership enforces it in practice).
    r = client.patch("/api/settings", json={"frame_send_from": "me@icloud.com"})
    assert r.status_code == 200


def test_frame_test_requires_server_config(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    assert client.post("/api/settings/frame-test").status_code == 400


def test_frame_test_requires_user_addresses(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "SMTP_USERNAME", "u@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")
    # user has not set send-to/from yet
    assert client.post("/api/settings/frame-test").status_code == 400


def test_frame_test_success_reports_acceptance_not_delivery(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "SMTP_USERNAME", "u@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")
    client.patch(
        "/api/settings",
        json={"frame_send_to": "f@send.auraframes.com", "frame_send_from": "me@me.com"},
    )

    sent = []

    async def fake_send(msg, **kw):
        sent.append(msg)
        return ({}, "250 ok")

    monkeypatch.setattr(frame_share.aiosmtplib, "send", fake_send)
    r = client.post("/api/settings/frame-test").json()
    assert r["ok"] is True and "mail server" in r["detail"] and "frame" in r["detail"]
    assert "delivered" not in r["detail"].lower()  # must not claim frame delivery
    assert len(sent) == 1


def test_frame_test_cooldown_returns_429_on_rapid_reuse(logged_in_client, monkeypatch):
    # frame-test is an authenticated send primitive through the server SMTP account. A per-user
    # cooldown stops it being hammered: the first call sends, an immediate second is 429.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "SMTP_USERNAME", "u@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")
    client.patch(
        "/api/settings",
        json={"frame_send_to": "f@send.auraframes.com", "frame_send_from": "me@me.com"},
    )
    sent = []

    async def fake_send(msg, **kw):
        sent.append(msg)
        return ({}, "250 ok")

    monkeypatch.setattr(frame_share.aiosmtplib, "send", fake_send)
    first = client.post("/api/settings/frame-test")
    assert first.status_code == 200 and first.json()["ok"] is True
    second = client.post("/api/settings/frame-test")
    assert second.status_code == 429
    assert len(sent) == 1  # the throttled call never reached SMTP


def test_frame_test_smtp_error_is_reported_not_500(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "SMTP_USERNAME", "u@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")
    client.patch(
        "/api/settings",
        json={"frame_send_to": "f@send.auraframes.com", "frame_send_from": "me@me.com"},
    )

    async def boom(msg, **kw):
        raise frame_share.aiosmtplib.SMTPAuthenticationError(535, "bad creds")

    monkeypatch.setattr(frame_share.aiosmtplib, "send", boom)
    r = client.post("/api/settings/frame-test")
    assert r.status_code == 200 and r.json()["ok"] is False and "535" in r.json()["detail"]
