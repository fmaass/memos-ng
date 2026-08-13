from unittest.mock import AsyncMock

import jwt
import pytest
from app import auth
from app.config import settings
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mock_jwks(monkeypatch, public_key):
    class _Key:
        key = public_key

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(auth, "_jwk_client", _Client())


# --- protected-by-default + state validation (no DB writes) ---


def test_user_endpoint_401_without_cookie(client):
    r = client.get("/api/auth/user")
    assert r.status_code == 401
    assert "detail" in r.json()


def test_callback_rejects_missing_or_mismatched_state(client):
    # No tx cookie at all → 400 before any token exchange.
    r = client.get("/api/auth/callback?code=abc&state=whatever", follow_redirects=False)
    assert r.status_code == 400


def test_login_redirects_to_authelia_with_pkce(client):
    r = client.get("/api/auth/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(settings.OIDC_AUTHORIZE_ENDPOINT)
    assert "code_challenge_method=S256" in loc
    setc = r.headers.get("set-cookie", "").lower()
    assert auth.TX_COOKIE in setc and "httponly" in setc


# --- id_token signature validation (real RS256) ---


def test_validate_id_token_accepts_correct_key(monkeypatch):
    priv = _rsa()
    _mock_jwks(monkeypatch, priv.public_key())
    token = jwt.encode(
        {"preferred_username": "u", "aud": settings.OIDC_CLIENT_ID, "iss": settings.OIDC_ISSUER},
        priv,
        algorithm="RS256",
    )
    claims = auth.validate_id_token(token)
    assert claims["preferred_username"] == "u"


def test_validate_id_token_rejects_wrong_key(monkeypatch):
    signer, attacker = _rsa(), _rsa()
    _mock_jwks(monkeypatch, attacker.public_key())  # JWKS returns a DIFFERENT key
    token = jwt.encode(
        {"preferred_username": "u", "aud": settings.OIDC_CLIENT_ID, "iss": settings.OIDC_ISSUER},
        signer,
        algorithm="RS256",
    )
    with pytest.raises(jwt.InvalidSignatureError):
        auth.validate_id_token(token)


# --- full BFF session lifecycle (OIDC network calls mocked) ---


def test_callback_creates_session_then_user_then_logout(client, db_engine, monkeypatch):
    monkeypatch.setattr(
        settings, "COOKIE_SECURE", False
    )  # so the test client keeps cookies over http
    monkeypatch.setattr(
        auth,
        "exchange_code",
        AsyncMock(return_value={"id_token": "x", "access_token": "a", "refresh_token": "r"}),
    )
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"sub": "uuid-testuser"},
    )
    monkeypatch.setattr(
        auth,
        "fetch_userinfo",
        AsyncMock(
            return_value={
                "preferred_username": "testuser_auth",
                "name": "Test",
                "email": "t@x",
                "groups": [],
            }
        ),
    )
    try:
        login = client.get("/api/auth/login?next=/home", follow_redirects=False)
        tx = login.cookies[auth.TX_COOKIE]
        state = auth.decrypt(tx)["state"]
        client.cookies.set(auth.TX_COOKIE, tx)

        cb = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert cb.status_code == 302
        setc = cb.headers.get("set-cookie", "").lower()
        assert "httponly" in setc and "samesite=lax" in setc
        sid = cb.cookies[auth.SID_COOKIE]
        assert sid

        client.cookies.set(auth.SID_COOKIE, sid)
        me = client.get("/api/auth/user")
        assert me.status_code == 200 and me.json()["subject"] == "testuser_auth"

        client.cookies.set(auth.SID_COOKIE, sid)
        assert client.post("/api/auth/logout").status_code == 200

        client.cookies.set(auth.SID_COOKIE, sid)
        assert client.get("/api/auth/user").status_code == 401  # session deleted
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject = 'testuser_auth'"))
        eng.dispose()


def test_security_headers_present(client):
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def _req(headers: dict, client_host: str = "rl-test") -> "object":
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (client_host, 0),
            "query_string": b"",
        }
    )


def test_rate_limit_blocks_after_threshold():
    from fastapi import HTTPException

    auth._RL.pop("rl-unit", None)
    dep = auth.rate_limit(limit=2, window=60)
    req = _req({}, client_host="rl-unit")
    dep(req)
    dep(req)
    with pytest.raises(HTTPException) as ei:
        dep(req)
    assert ei.value.status_code == 429
    auth._RL.pop("rl-unit", None)


def test_logout_returns_rp_initiated_url(logged_in_client):
    client, _ = logged_in_client
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["logout_url"].startswith(settings.OIDC_END_SESSION_ENDPOINT)
    assert "post_logout_redirect_uri=" in body["logout_url"]
    assert "id_token_hint=" in body["logout_url"]


def test_login_next_rejects_open_redirect(client):
    # A protocol-relative or absolute next must not survive — only same-site local paths.
    from app.routers.auth import _safe_next

    assert _safe_next("/memos/abc") == "/memos/abc"
    assert _safe_next("//evil.com") == "/"
    assert _safe_next("https://evil.com") == "/"
    assert _safe_next("/\\evil.com") == "/"
    assert _safe_next(None) == "/"
    assert _safe_next("") == "/"


async def test_userinfo_preferred_username_keys_user_not_sub(adb):
    # id_token carries only the opaque sub UUID; userinfo supplies preferred_username.
    # The user must be keyed by the real username, not the UUID (the live bug).
    from app import auth

    claims = {"sub": "b65154b9-0f1f-46a5-9dbf-6ef2aaf6334a"}
    userinfo = {
        "preferred_username": "fabian",
        "name": "Fabian",
        "email": "f@x",
        "groups": ["admins"],
    }
    user = await auth.upsert_user(adb, {**claims, **userinfo})
    assert user.subject == "fabian"  # NOT the sub UUID
    assert user.display_name == "Fabian" and user.role == "admin"


async def test_fetch_userinfo_no_access_token_returns_empty():
    from app import auth

    assert await auth.fetch_userinfo(None) == {}


def test_rolling_refresh_due_throttle_boundary():
    # last_seen is refreshed at most once per throttle window — so the front page's concurrent
    # authenticated fan-out doesn't UPDATE+COMMIT the one hot session row on every request.
    from datetime import UTC, datetime, timedelta

    from app.auth import rolling_refresh_due

    now = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
    w = settings.SESSION_ROLLING_THROTTLE_S
    assert rolling_refresh_due(now - timedelta(seconds=w + 1), now) is True  # past window → refresh
    assert rolling_refresh_due(now - timedelta(seconds=w - 1), now) is False  # within → skip
    assert rolling_refresh_due(now, now) is False  # just written → skip


# --- mutation-killing pins: absolute expiry + present-but-wrong OIDC state ---


def test_session_past_absolute_expiry_is_dead(logged_in_client):
    client, subject = logged_in_client
    assert client.get("/api/auth/user").status_code == 200
    eng = create_engine(SYNC_URL)
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET absolute_expiry = now() - interval '1 minute' "
                "WHERE user_id = (SELECT id FROM users WHERE subject = :s)"
            ),
            {"s": subject},
        )
    eng.dispose()
    assert client.get("/api/auth/user").status_code == 401


def test_callback_rejects_a_present_but_wrong_state(client, monkeypatch):
    # The OIDC exchange is mocked to SUCCEED: if the state comparison were deleted,
    # this callback would mint a session and answer 302 — the 400 rides on the
    # comparison alone, which is what makes deleting it turn the suite red.
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {
            "preferred_username": "state_pin_user",
            "name": "S",
            "email": "s@x",
            "groups": [],
        },
    )
    monkeypatch.setattr(auth, "fetch_userinfo", AsyncMock(return_value={}))
    login = client.get("/api/auth/login", follow_redirects=False)
    client.cookies.set(auth.TX_COOKIE, login.cookies[auth.TX_COOKIE])
    cb = client.get("/api/auth/callback?code=abc&state=not-the-real-state", follow_redirects=False)
    assert cb.status_code == 400
    assert cb.json()["detail"] == "Invalid OIDC state"
