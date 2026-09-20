from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from app import auth
from app.config import settings
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

# `/api/auth/login` and `/api/auth/callback` both carry `Depends(auth.rate_limit())`, so most of
# this file reaches a limiter path whether or not it says so — and the limiter's window is 60
# seconds of `time.monotonic()`. The whole module runs under the controlled clock; the fixture
# fails any case that pins the clock without the application ever reading it, so the cases that
# touch no clock at all are marked `real_clock` rather than left silently pinned (MEMNG-296).
pytestmark = pytest.mark.usefixtures("controlled_clock")


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


@pytest.mark.real_clock
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


@pytest.mark.real_clock
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


@pytest.mark.real_clock
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


@pytest.mark.real_clock
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


def test_rate_limit_blocks_after_threshold(controlled_clock):
    """The refusal is decided by the request COUNT; the window reopens only on an explicit advance.

    Under the controlled clock the burst cannot outlive its own window between filling the bucket
    and asking for the refusal, and the recovery below is the window being crossed on purpose
    rather than the test waiting a real minute.
    """
    from fastapi import HTTPException

    auth._RL.pop("rl-unit", None)
    dep = auth.rate_limit(limit=2, window=60)
    req = _req({}, client_host="rl-unit")
    dep(req)
    dep(req)
    with pytest.raises(HTTPException) as ei:
        dep(req)
    assert ei.value.status_code == 429
    # The pin reached the limiter: both stamps are the controlled instant, so nothing but the
    # count could have decided the 429.
    assert set(auth._RL["rl-unit"]) == {controlled_clock.monotonic}
    assert len(auth._RL["rl-unit"]) == 2

    # And the window is that clock's: one advance past it and the bucket is empty again.
    controlled_clock.advance(60)
    dep(req)
    assert auth._RL["rl-unit"] == [controlled_clock.monotonic]
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


@pytest.mark.real_clock
def test_login_next_rejects_open_redirect(client):
    # A protocol-relative or absolute next must not survive — only same-site local paths.
    from app.routers.auth import _safe_next

    assert _safe_next("/memos/abc") == "/memos/abc"
    assert _safe_next("//evil.com") == "/"
    assert _safe_next("https://evil.com") == "/"
    assert _safe_next("/\\evil.com") == "/"
    assert _safe_next(None) == "/"
    assert _safe_next("") == "/"


@pytest.mark.real_clock
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


@pytest.mark.real_clock
async def test_fetch_userinfo_no_access_token_returns_empty():
    from app import auth

    assert await auth.fetch_userinfo(None) == {}


@pytest.mark.real_clock
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


def test_session_past_absolute_expiry_is_dead(logged_in_client, controlled_clock):
    client, subject = logged_in_client
    assert client.get("/api/auth/user").status_code == 200
    eng = create_engine(SYNC_URL)
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET absolute_expiry = :t "
                "WHERE user_id = (SELECT id FROM users WHERE subject = :s)"
            ),
            # From the controlled clock, not PostgreSQL's: `absolute_expiry <= datetime.now(UTC)`
            # is evaluated in the pinned application, which no longer shares the database's
            # timeline.
            {"t": controlled_clock.now() - timedelta(minutes=1), "s": subject},
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


# --- the provider refuses the authorization code ----------------------------------------------

# RFC 7636 Appendix B's published S256 challenge — any well-formed one the login accepts will do
# here; these cases never redeem it.
_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
# The marker the refusal page carries on <html>, so a case proves that page answered rather than
# the SPA shell or a JSON detail.
_REFUSED_MARKER = "data-memosng-signin-refused"


def _token_endpoint_refuses(monkeypatch, status: int = 400):
    """Script the provider's token endpoint to answer `status` UNDER the real `exchange_code`.

    Substituting `exchange_code` with a mock that raises would only prove the route handles an
    exception the test invented; scripting the transport leaves `raise_for_status()` as the thing
    that turns the provider's answer into the failure, which is what the traceback came from.
    """

    real_client = httpx.AsyncClient  # bound BEFORE the patch; the replacement builds a real one

    def _refusing_client(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(lambda _request: httpx.Response(status)))

    monkeypatch.setattr(httpx, "AsyncClient", _refusing_client)


def _tx_cookie_deleted(response) -> bool:
    header = response.headers.get("set-cookie", "").lower()
    return auth.TX_COOKIE in header and "max-age=0" in header


def _the_refusal_event(logs: list[dict], *, status: int, lane: str) -> None:
    """The one event the refusal leaves behind, and nothing of the request in it.

    A refusal the server answers rather than raises is invisible in the log unless it says so, and
    a provider outage arrives here as the same exception a spent code does — so the status and the
    lane are what has to be readable afterwards. Everything else about the request is a secret:
    the code, the verifier and the state all travel on it.
    """
    refusals = [entry for entry in logs if entry["event"] == "oidc_code_exchange_refused"]
    assert len(refusals) == 1
    assert refusals[0]["status_code"] == status
    assert refusals[0]["lane"] == lane
    assert "already-spent" not in repr(refusals[0])


def test_a_refused_code_ends_the_web_login_on_a_readable_page(client, monkeypatch):
    """An authorization code is single-use, so a second arrival of the same callback is refused at
    the provider. The person must get a page they can act on, not a bare Internal Server Error."""
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    _token_endpoint_refuses(monkeypatch)
    login = client.get("/api/auth/login", follow_redirects=False)
    tx = login.cookies[auth.TX_COOKIE]
    client.cookies.set(auth.TX_COOKIE, tx)

    with capture_logs() as logs:
        cb = client.get(
            f"/api/auth/callback?code=already-spent&state={auth.decrypt(tx)['state']}",
            follow_redirects=False,
        )

    _the_refusal_event(logs, status=400, lane="web")
    assert cb.status_code == 400
    assert _REFUSED_MARKER in cb.text
    assert "/api/auth/login" in cb.text  # a way to start again
    assert "already-spent" not in cb.text  # nothing the caller supplied is reflected back
    # The transaction is finished either way: its cookie left behind is a state value that can only
    # produce the same refusal again.
    assert _tx_cookie_deleted(cb)


def test_a_refused_code_reaches_the_native_hand_back_page_as_an_error(client, monkeypatch):
    """An app watching an authentication session sees only the callback URL it was told to wait
    for, so a refusal has to arrive there as a parameter or its sign-on waits for ever.

    Driven with the provider unavailable rather than refusing a spent code: an outage answers the
    same exception, and the device has to be let go on that one too.
    """
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    _token_endpoint_refuses(monkeypatch, status=503)
    login = client.get(
        "/api/auth/login?"
        + urlencode({"client": "ios", "code_challenge": _CHALLENGE, "device": "AuthTestPhone"}),
        follow_redirects=False,
    )
    tx = login.cookies[auth.TX_COOKIE]
    client.cookies.set(auth.TX_COOKIE, tx)

    with capture_logs() as logs:
        cb = client.get(
            f"/api/auth/callback?code=already-spent&state={auth.decrypt(tx)['state']}",
            follow_redirects=False,
        )

    _the_refusal_event(logs, status=503, lane="native")
    assert cb.status_code == 302
    url = urlsplit(cb.headers["location"])
    assert url._replace(query="").geturl() == settings.NATIVE_CALLBACK_URL
    query = parse_qs(url.query)
    assert query["error"] == ["exchange_failed"]
    assert "code" not in query  # nothing to redeem: the login did not complete
    assert "already-spent" not in cb.headers["location"]
    assert _tx_cookie_deleted(cb)
