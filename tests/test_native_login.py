"""Phase 2 (native iOS client): browser-brokered device login.

The properties under test, in the order the flow exercises them:

* the request memos-ng sends Authelia is the SAME one the web flow sends — the "no second OIDC
  client, no household SSO restart" premise the whole design rests on;
* a device login leaves NO web session behind, and hands back a one-shot code instead;
* that code becomes a PAT exactly once, only for the holder of the PKCE verifier, and only if it
  is neither expired nor already burned;
* a device token may revoke itself and nothing else;
* that token renews itself on use — rotating rather than extending, and exactly once
  under a concurrent pair — so a year-old install never silently signs out;
* the two anonymous URLs Apple and the authentication session need beat the SPA catch-all.

Every case drives the real flow with only the OIDC network calls mocked (the seam `conftest`'s
`logged_in_client` already uses), so removing the behaviour under test turns the case red rather
than changing which mock answers.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from app import auth
from app.config import settings
from app.main import FRONTEND_DIR, app
from app.services import native_auth
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# RFC 7636 Appendix B's published S256 test vector. Hard-coded rather than computed with
# `s256_challenge`, so a broken implementation of it cannot also produce the expected value.
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
OTHER_VERIFIER = "M25iVXpKU3puUjFaYWg3T1NDTDQtcW1ROUY5YXlwalNoc0hhakxifmZH"

DEVICE = "NativeTestPhone"
# The SPA shell's root element (frontend/index.html) — its ABSENCE is how the precedence tests
# prove a route answered instead of the catch-all.
SPA_ROOT_MARKER = 'id="app"'
# The per-request randoms an authorize URL is allowed to differ in. `nonce` is listed because the
# comparison must stay honest if one is ever added; today's login emits none.
PER_REQUEST_PARAMS = {"state", "code_challenge", "nonce"}


def _sql(statement: str, **params):
    """One committed statement against the real DB, rows back (none for a write).

    A fresh engine per call, like the rest of the suite: these read-backs must see what the app's
    own sessions committed, never a test transaction's private view.
    """
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(statement), params)
            return result.all() if result.returns_rows else []
    finally:
        eng.dispose()


def _session_count(subject: str) -> int:
    return _sql(
        "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.subject = :s",
        s=subject,
    )[0][0]


@pytest.fixture
def native_oidc(client, db_engine, monkeypatch):
    """A CLEAN TestClient — empty cookie jar, no session ever created — with the OIDC network
    calls mocked at the same seam `logged_in_client` uses.

    Clean on purpose: the "no web session" assertions are only worth anything against a client
    that could not have been carrying one from a previous login.
    """
    from unittest.mock import AsyncMock

    subject = "test_native_user"
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"preferred_username": subject, "name": "N", "email": "n@x", "groups": []},
    )
    try:
        yield client, subject
    finally:
        # Cascades to the user's pats and native_auth_codes.
        _sql("DELETE FROM users WHERE subject = :s", s=subject)


def _login_url(**params: str) -> str:
    return "/api/auth/login?" + urlencode(params)


def _start_native_login(client, *, challenge: str = RFC_CHALLENGE, device: str = DEVICE, **extra):
    """Run the ios-mode login leg; return (response, state) with the tx cookie in the jar."""
    resp = client.get(
        _login_url(client="ios", code_challenge=challenge, device=device, **extra),
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    tx = resp.cookies[auth.TX_COOKIE]
    client.cookies.set(auth.TX_COOKIE, tx)
    return resp, auth.decrypt(tx)["state"]


def _callback(client, **params: str):
    return client.get("/api/auth/callback?" + urlencode(params), follow_redirects=False)


def _handoff_code(client, *, device: str = DEVICE, challenge: str = RFC_CHALLENGE) -> str:
    """A full ios login, returning the one-shot code the app would read off the callback URL."""
    _, state = _start_native_login(client, challenge=challenge, device=device)
    cb = _callback(client, code="authelia-code", state=state)
    assert cb.status_code == 302, cb.text
    return parse_qs(urlsplit(cb.headers["location"]).query)["code"][0]


def _exchange(client, code: str, verifier: str = RFC_VERIFIER):
    return client.post(
        "/api/auth/native/exchange", json={"code": code, "code_verifier": verifier}
    )


# --- the IdP request is untouched -----------------------------------------------------------


def test_ios_login_sends_authelia_exactly_the_web_flow_s_authorize_request(client):
    """The "Authelia untouched" proof in code (Decision 1): same endpoint, same parameter set,
    same values everywhere except the per-request randoms — and neither the app's own PKCE
    challenge nor its device name is forwarded to the IdP, which never learns a device is involved.
    """
    web = client.get("/api/auth/login", follow_redirects=False)
    client.cookies.clear()
    ios = client.get(
        _login_url(client="ios", code_challenge=RFC_CHALLENGE, device=DEVICE),
        follow_redirects=False,
    )
    assert web.status_code == ios.status_code == 302

    web_url, ios_url = urlsplit(web.headers["location"]), urlsplit(ios.headers["location"])
    assert (ios_url.scheme, ios_url.netloc, ios_url.path) == (
        web_url.scheme,
        web_url.netloc,
        web_url.path,
    )
    web_q, ios_q = parse_qs(web_url.query), parse_qs(ios_url.query)
    assert set(ios_q) == set(web_q)
    shared = set(web_q) - PER_REQUEST_PARAMS
    # Guards the loop below against passing vacuously if the parameter set ever shrinks.
    assert shared == {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "code_challenge_method",
    }
    for key in shared:
        assert ios_q[key] == web_q[key], key

    assert ios_q["code_challenge_method"] == ["S256"]
    assert ios_q["code_challenge"] != [RFC_CHALLENGE]  # the BFF's own challenge, not the app's
    assert RFC_CHALLENGE not in ios.headers["location"]
    assert DEVICE not in ios.headers["location"]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"code_challenge": "a" * 42, "device": DEVICE}, id="challenge-too-short"),
        pytest.param({"code_challenge": "a" * 129, "device": DEVICE}, id="challenge-too-long"),
        pytest.param({"code_challenge": "a" * 42 + "+", "device": DEVICE}, id="challenge-plus"),
        pytest.param({"code_challenge": "a" * 42 + "=", "device": DEVICE}, id="challenge-padding"),
        pytest.param({"code_challenge": "a" * 42 + " ", "device": DEVICE}, id="challenge-space"),
        pytest.param({"device": DEVICE}, id="challenge-missing"),
        pytest.param({"code_challenge": RFC_CHALLENGE, "device": "d" * 65}, id="device-too-long"),
        pytest.param({"code_challenge": RFC_CHALLENGE, "device": ""}, id="device-empty"),
        pytest.param({"code_challenge": RFC_CHALLENGE}, id="device-missing"),
    ],
)
def test_a_malformed_native_login_request_is_refused(client, params):
    assert client.get(_login_url(client="ios", **params), follow_redirects=False).status_code == 422


def test_a_well_formed_native_login_request_is_accepted(client):
    """The positive control for the parametrised refusals above — without it they would all pass
    against a route that refused everything."""
    resp = client.get(
        _login_url(client="ios", code_challenge=RFC_CHALLENGE, device="d" * 64),
        follow_redirects=False,
    )
    assert resp.status_code == 302


# --- the callback hands back a code instead of a session -------------------------------------


def test_native_callback_returns_a_code_and_leaves_no_web_session(native_oidc):
    client, subject = native_oidc
    _, state = _start_native_login(client)
    cb = _callback(client, code="authelia-code", state=state)

    assert cb.status_code == 302
    location = cb.headers["location"]
    assert location.startswith(settings.NATIVE_CALLBACK_URL + "?")
    assert parse_qs(urlsplit(location).query)["code"][0]
    assert auth.SID_COOKIE not in cb.cookies
    assert auth.SID_COOKIE not in client.cookies
    assert _session_count(subject) == 0  # see the web-mode test below for this query's control


def test_web_mode_still_creates_a_session_and_honours_next(native_oidc):
    """The control for both native assertions above: the SAME client, the SAME mocked OIDC calls
    and the SAME session query, differing only in `client=ios` — so a zero session count and an
    absent `next` in the native tests can only be the mode, not the harness.
    """
    client, subject = native_oidc
    login = client.get("/api/auth/login?next=/explore", follow_redirects=False)
    tx = login.cookies[auth.TX_COOKIE]
    client.cookies.set(auth.TX_COOKIE, tx)
    cb = _callback(client, code="authelia-code", state=auth.decrypt(tx)["state"])

    assert cb.status_code == 302
    assert cb.headers["location"] == "/explore"
    assert auth.SID_COOKIE in cb.cookies
    assert _session_count(subject) == 1


def test_next_is_ignored_in_native_mode(native_oidc):
    client, _ = native_oidc
    login, state = _start_native_login(client, next="/explore")

    assert "next" not in auth.decrypt(login.cookies[auth.TX_COOKIE])
    cb = _callback(client, code="authelia-code", state=state)
    assert cb.headers["location"].startswith(settings.NATIVE_CALLBACK_URL + "?")
    assert "explore" not in cb.headers["location"]


def test_an_idp_error_comes_back_through_the_native_callback(native_oidc):
    """An app watching an authentication session sees only the callback URL it was told to wait
    for — never an HTTP status — so a refused login has to arrive as one."""
    client, subject = native_oidc
    _, state = _start_native_login(client)
    cb = _callback(client, error="access_denied", state=state)

    assert cb.status_code == 302
    url = urlsplit(cb.headers["location"])
    assert url._replace(query="").geturl() == settings.NATIVE_CALLBACK_URL
    query = parse_qs(url.query)
    assert query["error"] == ["access_denied"]
    assert "code" not in query
    assert _session_count(subject) == 0


def test_an_error_callback_with_a_bad_or_missing_state_is_refused(native_oidc):
    """The mode is read out of the transaction cookie, so an unverified callback must not be able
    to reach the native error path by claiming to be one — it fails exactly like the web flow."""
    client, _ = native_oidc
    _start_native_login(client)

    mismatched = _callback(client, error="access_denied", state="not-the-state")
    assert mismatched.status_code == 400
    assert settings.NATIVE_CALLBACK_URL not in mismatched.text

    client.cookies.clear()
    absent = _callback(client, error="access_denied")
    assert absent.status_code == 400
    assert settings.NATIVE_CALLBACK_URL not in absent.text


# --- exchanging the code for a device token ---------------------------------------------------


def test_exchange_mints_a_device_token_that_works(native_oidc):
    client, subject = native_oidc
    code = _handoff_code(client)

    resp = _exchange(client, code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["subject"] == subject
    assert body["user"]["auth_kind"] == "pat"
    token = body["token"]

    # A separate client with an empty cookie jar: only the token can be answering here.
    anon = TestClient(app, raise_server_exceptions=False)
    listed = anon.get("/api/memos", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200

    name, expires_at = _sql(
        "SELECT name, expires_at FROM pats WHERE hashed_token = :h", h=auth.hash_token(token)
    )[0]
    assert name == DEVICE  # the device display name propagates verbatim
    assert expires_at is not None
    lifetime = expires_at - datetime.now(UTC)
    assert timedelta(days=364) < lifetime <= timedelta(days=365)
    assert abs(datetime.fromisoformat(body["expires_at"]) - expires_at) < timedelta(seconds=5)


def test_a_wrong_verifier_is_refused_and_still_burns_the_code(native_oidc):
    """One attempt per code, whatever its outcome: the burn is committed BEFORE the verifier is
    judged, so a mismatch cannot be retried until it succeeds.
    """
    client, _ = native_oidc
    code = _handoff_code(client)

    bad = _exchange(client, code, verifier=OTHER_VERIFIER)
    assert bad.status_code == 400
    assert bad.json()["detail"] == "verifier_mismatch"

    consumed_at = _sql(
        "SELECT consumed_at FROM native_auth_codes WHERE code_hash = :h",
        h=native_auth.hash_code(code),
    )[0][0]
    assert consumed_at is not None

    retry = _exchange(client, code)  # now with the RIGHT verifier
    assert retry.status_code == 400
    assert retry.json()["detail"] == "code_consumed"
    assert _sql("SELECT count(*) FROM pats WHERE name = :n", n=DEVICE)[0][0] == 0


def test_a_code_can_only_be_exchanged_once(native_oidc):
    client, _ = native_oidc
    code = _handoff_code(client)

    assert _exchange(client, code).status_code == 200
    second = _exchange(client, code)
    assert second.status_code == 400
    assert second.json()["detail"] == "code_consumed"
    assert _sql("SELECT count(*) FROM pats WHERE name = :n", n=DEVICE)[0][0] == 1


def test_an_expired_code_is_refused_as_expired(native_oidc):
    """Backdated in PostgreSQL rather than with a frozen Python clock: `expires_at > now()` is
    evaluated by the database, so only a real stored instant tests the real predicate."""
    client, _ = native_oidc
    code = _handoff_code(client)
    _sql(
        "UPDATE native_auth_codes SET expires_at = now() - interval '1 second' "
        "WHERE code_hash = :h",
        h=native_auth.hash_code(code),
    )

    resp = _exchange(client, code)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_expired"
    assert _sql("SELECT count(*) FROM pats WHERE name = :n", n=DEVICE)[0][0] == 0


def test_a_swept_code_is_indistinguishable_from_one_that_never_existed(native_oidc):
    """Past the one-hour grace the row is gone, so the better diagnostic goes with it — both
    answers are terminal for the client, and keeping rows forever to tell them apart would be an
    unbounded table for a distinction nobody acts on."""
    client, _ = native_oidc
    code = _handoff_code(client)
    code_hash = native_auth.hash_code(code)
    _sql(
        "UPDATE native_auth_codes SET expires_at = now() - interval '2 hours' WHERE code_hash = :h",
        h=code_hash,
    )
    assert _exchange(client, code).json()["detail"] == "code_expired"  # still classifiable

    _handoff_code(client, device="SweepTrigger")  # the next mint runs the sweep
    survivors = _sql("SELECT count(*) FROM native_auth_codes WHERE code_hash = :h", h=code_hash)
    assert survivors[0][0] == 0

    resp = _exchange(client, code)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_invalid"


def test_an_unknown_code_is_refused(client, db_engine):
    resp = _exchange(client, "not-a-code-anyone-ever-minted")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_invalid"


async def test_two_concurrent_exchanges_mint_exactly_one_token(native_oidc):
    """The single-statement consume is what makes this deterministic: read-then-update would let
    both requests see an unconsumed row and both mint a token."""
    client, _ = native_oidc
    device = "ConcurrentRacePhone"
    code = _handoff_code(client, device=device)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        first, second = await asyncio.gather(
            ac.post(
                "/api/auth/native/exchange", json={"code": code, "code_verifier": RFC_VERIFIER}
            ),
            ac.post(
                "/api/auth/native/exchange", json={"code": code, "code_verifier": RFC_VERIFIER}
            ),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 400]
    loser = first if first.status_code == 400 else second
    assert loser.json()["detail"] == "code_consumed"
    assert _sql("SELECT count(*) FROM pats WHERE name = :n", n=device)[0][0] == 1


# --- a device token may revoke itself, and nothing else ---------------------------------------


def test_native_logout_revokes_only_the_presenting_token(native_oidc):
    client, _ = native_oidc
    first = _exchange(client, _handoff_code(client, device="PhoneOne")).json()["token"]
    second = _exchange(client, _handoff_code(client, device="PhoneTwo")).json()["token"]

    anon = TestClient(app, raise_server_exceptions=False)
    revoked = anon.post(
        "/api/auth/native/logout", headers={"Authorization": f"Bearer {first}"}
    )
    assert revoked.status_code == 204

    assert _sql("SELECT count(*) FROM pats WHERE hashed_token = :h", h=auth.hash_token(first))[0][
        0
    ] == 0
    assert _sql("SELECT count(*) FROM pats WHERE hashed_token = :h", h=auth.hash_token(second))[0][
        0
    ] == 1
    assert (
        anon.get("/api/memos", headers={"Authorization": f"Bearer {first}"}).status_code == 401
    )
    assert (
        anon.get("/api/memos", headers={"Authorization": f"Bearer {second}"}).status_code == 200
    )


def test_native_logout_refuses_a_browser_session(logged_in_client):
    """Session-authenticated callers revoke through `/api/pats/{id}`, which names the token it is
    revoking; this route deletes whatever credential presented itself, so a cookie must not reach
    it (Decision 5/6)."""
    client, _ = logged_in_client
    minted = client.post("/api/pats", json={"name": "survivor"})
    assert minted.status_code == 201

    assert client.post("/api/auth/native/logout").status_code == 401
    assert _sql("SELECT count(*) FROM pats WHERE name = 'survivor'")[0][0] == 1


def test_native_logout_refuses_an_unauthenticated_caller(client, db_engine):
    assert client.post("/api/auth/native/logout").status_code == 401


# --- a device token renews itself on use ------------------------------------------------------


def _renew(token: str):
    """Renew the way the app does: a clean client whose only credential is the bearer."""
    anon = TestClient(app, raise_server_exceptions=False)
    return anon.post("/api/auth/native/renew", headers={"Authorization": f"Bearer {token}"})


def test_renew_rotates_the_token_and_refreshes_its_lifetime(native_oidc):
    """The point of the endpoint: a phone that keeps using its token never reaches the expiry
    that would sign it out — and the token it presented is dead the moment it is renewed.
    """
    client, subject = native_oidc
    device = "RenewingPhone"
    old = _exchange(client, _handoff_code(client, device=device)).json()["token"]
    # Backdated to a day out so a successor that merely COPIED the old expiry would fail the
    # lifetime assertion below instead of passing on the mint's own year.
    _sql(
        "UPDATE pats SET expires_at = now() + interval '1 day' WHERE hashed_token = :h",
        h=auth.hash_token(old),
    )

    resp = _renew(old)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new = body["token"]
    assert new != old
    assert new.startswith(auth.PAT_PREFIX)
    assert body["user"]["subject"] == subject
    assert body["user"]["auth_kind"] == "pat"

    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/api/memos", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    assert anon.get("/api/memos", headers={"Authorization": f"Bearer {old}"}).status_code == 401

    rows = _sql("SELECT hashed_token, expires_at FROM pats WHERE name = :n", n=device)
    assert len(rows) == 1  # rotated, never accumulated
    hashed_token, expires_at = rows[0]
    assert hashed_token == auth.hash_token(new)
    lifetime = expires_at - datetime.now(UTC)
    assert timedelta(days=364) < lifetime <= timedelta(days=365)
    assert abs(datetime.fromisoformat(body["expires_at"]) - expires_at) < timedelta(seconds=5)


def test_an_expired_token_is_refused_before_the_renew_logic_runs(native_oidc):
    """Expiry is authentication's answer, not renewal's — so the expired row is still sitting
    there afterwards, unrotated, with no successor beside it.

    Backdated in PostgreSQL rather than with a frozen clock: only a real stored instant tests the
    predicate the running code evaluates.
    """
    client, _ = native_oidc
    device = "ExpiredPhone"
    token = _exchange(client, _handoff_code(client, device=device)).json()["token"]
    _sql(
        "UPDATE pats SET expires_at = now() - interval '1 second' WHERE hashed_token = :h",
        h=auth.hash_token(token),
    )

    resp = _renew(token)
    assert resp.status_code == 401
    assert [r[0] for r in _sql("SELECT hashed_token FROM pats WHERE name = :n", n=device)] == [
        auth.hash_token(token)
    ]


def test_a_revoked_token_cannot_renew_itself_back_to_life(native_oidc):
    client, _ = native_oidc
    device = "RevokedPhone"
    token = _exchange(client, _handoff_code(client, device=device)).json()["token"]
    anon = TestClient(app, raise_server_exceptions=False)
    revoked = anon.post("/api/auth/native/logout", headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 204

    assert _renew(token).status_code == 401
    assert _sql("SELECT count(*) FROM pats WHERE name = :n", n=device)[0][0] == 0


async def test_two_concurrent_renews_mint_exactly_one_successor(native_oidc):
    """The single-statement rotate is what makes this deterministic: read-then-delete would let
    both requests see a renewable row, mint two successors and leave one of them orphaned.
    """
    client, _ = native_oidc
    device = "RacingPhone"
    token = _exchange(client, _handoff_code(client, device=device)).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        first, second = await asyncio.gather(
            ac.post("/api/auth/native/renew", headers=headers),
            ac.post("/api/auth/native/renew", headers=headers),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 401]
    winner = first if first.status_code == 200 else second
    assert [r[0] for r in _sql("SELECT hashed_token FROM pats WHERE name = :n", n=device)] == [
        auth.hash_token(winner.json()["token"])
    ]


async def test_a_token_that_never_expires_is_not_renewable(pat_client):
    """Renewal exists so a BOUNDED token does not silently sign out; a PAT with no expiry has
    nothing to renew, and the table cannot tell a device token from an SPA-created one by anything
    else — so the unbounded class is refused in-band rather than rotated by a bearer (Decision 5).
    """
    bearer, _, token = pat_client

    resp = await bearer.post("/api/auth/native/renew")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "token_not_renewable"
    assert _sql("SELECT count(*) FROM pats WHERE hashed_token = :h", h=auth.hash_token(token))[0][
        0
    ] == 1
    assert (await bearer.get("/api/memos")).status_code == 200  # refused, not revoked


def test_renew_refuses_a_browser_session(logged_in_client):
    """A cookie names no token, and a session must not be able to mint a device successor at all:
    the SPA creates tokens through `/api/pats`, which says which one it means (Decision 5/6).
    """
    client, _ = logged_in_client
    minted = client.post("/api/pats", json={"name": "session-minted", "expires_in_days": 30})
    assert minted.status_code == 201

    assert client.post("/api/auth/native/renew").status_code == 401
    assert _sql("SELECT count(*) FROM pats WHERE name = 'session-minted'")[0][0] == 1


def test_renew_refuses_an_unauthenticated_caller(client, db_engine):
    assert client.post("/api/auth/native/renew").status_code == 401


# --- the two anonymous URLs, and their precedence over the SPA catch-all -----------------------


def test_the_native_callback_page_beats_the_spa_catch_all(client):
    resp = client.get("/ios/auth/callback")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "data-memosng-native-callback" in resp.text
    assert SPA_ROOT_MARKER not in resp.text
    # The control for that absence: the marker really is what the SPA shell is identified by.
    assert SPA_ROOT_MARKER in (FRONTEND_DIR.parent / "index.html").read_text()


def test_aasa_is_404_while_no_app_claims_this_domain(client):
    assert settings.APPLE_TEAM_ID is None
    resp = client.get("/.well-known/apple-app-site-association")

    assert resp.status_code == 404
    assert SPA_ROOT_MARKER not in resp.text  # never the SPA shell, even when there is nothing here


def test_aasa_claims_the_domain_for_the_configured_app(client, monkeypatch):
    monkeypatch.setattr(settings, "APPLE_TEAM_ID", "ABCDE12345")
    monkeypatch.setattr(settings, "IOS_BUNDLE_ID", "live.discomarder.memosng")
    resp = client.get("/.well-known/apple-app-site-association")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {
        "webcredentials": {"apps": ["ABCDE12345.live.discomarder.memosng"]}
    }


def test_aasa_emits_applinks_when_the_switch_asks_for_it(client, monkeypatch):
    monkeypatch.setattr(settings, "APPLE_TEAM_ID", "ABCDE12345")
    monkeypatch.setattr(settings, "IOS_BUNDLE_ID", "live.discomarder.memosng")
    monkeypatch.setattr(settings, "AASA_KEYS", ["webcredentials", "applinks"])
    body = client.get("/.well-known/apple-app-site-association").json()

    assert set(body) == {"webcredentials", "applinks"}
    assert body["applinks"]["details"] == [
        {
            "appIDs": ["ABCDE12345.live.discomarder.memosng"],
            "components": [{"/": "/ios/auth/callback*"}],
        }
    ]


def test_the_exchange_is_documented_as_anonymous():
    """The generated clients (ADR-0009) see only this document, and an endpoint that takes no
    credential has to say so — nothing else in the schema would reveal it."""
    schema = app.openapi()

    assert "/api/auth/native/exchange" in schema["paths"]
    assert "/api/auth/native/logout" in schema["paths"]
    description = schema["components"]["securitySchemes"]["bearerAuth"]["description"]
    assert "/api/auth/native/exchange" in description
    assert "anonymous" in description


# --- the pure pieces ---------------------------------------------------------------------------


def test_s256_challenge_matches_the_published_test_vector():
    assert native_auth.s256_challenge(RFC_VERIFIER) == RFC_CHALLENGE


@pytest.mark.parametrize(
    "value,expected",
    [
        (RFC_CHALLENGE, True),
        ("a" * 43, True),
        ("a" * 128, True),
        ("-_" * 30, True),
        ("a" * 42, False),
        ("a" * 129, False),
        ("a" * 42 + "+", False),
        ("a" * 42 + "/", False),
        ("a" * 42 + "=", False),
        ("a" * 21 + "\n" + "a" * 21, False),
        ("", False),
        (None, False),
    ],
)
def test_valid_code_challenge(value, expected):
    assert native_auth.valid_code_challenge(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [("iPhone", True), ("d" * 64, True), ("d" * 65, False), ("", False), (None, False)],
)
def test_valid_device_name(value, expected):
    assert native_auth.valid_device_name(value) is expected


def test_aasa_document_emits_only_the_requested_sections():
    assert native_auth.aasa_document("T", "b", []) == {}
    assert native_auth.aasa_document("T", "b", ["webcredentials"]) == {
        "webcredentials": {"apps": ["T.b"]}
    }
    assert set(native_auth.aasa_document("T", "b", ["applinks"])) == {"applinks"}


def test_verifier_matches_only_its_own_challenge():
    assert native_auth.verifier_matches(RFC_VERIFIER, RFC_CHALLENGE) is True
    assert native_auth.verifier_matches(OTHER_VERIFIER, RFC_CHALLENGE) is False
