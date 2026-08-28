"""MEMNG-138 option 2: the step-up management grant for `/api/pats/*`.

A device PAT mints and revokes nothing but itself (ADR-0015). The capability to manage the
account's tokens comes from a FRESH Authelia authentication and nowhere else: the app runs the
ordinary browser login lane a second time with `purpose=manage`, exchanges the resulting one-shot
code for a short-lived grant, and presents that grant as `Authorization: Grant …`.

The properties under test, in the order the flow builds them:

* B1 — a handoff code carries the purpose it was minted for, and a grant is stored hashed,
  bounded by its configured TTL, and swept globally at the next mint;
* B2 — the manage lane mints a manage-purpose code, `POST /api/auth/native/manage-grant` turns it
  into a grant, and the two exchange endpoints refuse each other's codes WITHOUT burning them;
* B3 — `/api/pats/*` accepts the session cookie or a grant, refuses a plain bearer exactly as
  before, and fails closed on any ambiguous or invalid credential.

Every case drives the real flow with only the OIDC network calls mocked (the seam
`tests/conftest.py`'s `logged_in_client` already uses), so removing the behaviour under test turns
the case red rather than changing which mock answers.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from app import auth
from app.config import settings
from app.models import ManagementGrant, User
from app.services import management_grant, native_auth
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
OTHER_VERIFIER = "M25iVXpKU3puUjFaYWg3T1NDTDQtcW1ROUY5YXlwalNoc0hhakxifmZH"
DEVICE = "GrantTestPhone"


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


async def _user(adb, subject: str) -> User:
    user = User(subject=subject, role="user")
    adb.add(user)
    await adb.flush()
    return user


# --- B1: the grant store and the code's purpose ------------------------------------------------


async def test_a_grant_is_stored_only_as_a_hash(adb):
    """The plaintext exists in exactly one place — the mint's return value — for the same reason a
    PAT does: a database read must not be replayable into a live credential."""
    user = await _user(adb, "grant_hash_user")
    grant, _ = await management_grant.mint_grant(adb, user)

    row = (
        await adb.execute(select(ManagementGrant).where(ManagementGrant.user_id == user.id))
    ).scalar_one()
    assert row.hashed_grant == management_grant.hash_grant(grant)
    assert row.hashed_grant != grant
    assert grant not in (row.hashed_grant,)


async def test_a_grant_expires_after_the_configured_ttl(adb, monkeypatch):
    """The bound is read from settings at mint time, not baked in: a grant authenticates a
    management session of minutes, and that window is the whole reason it is safe to hand out."""
    monkeypatch.setattr(settings, "MANAGE_GRANT_TTL_S", 42)
    user = await _user(adb, "grant_ttl_user")
    before = datetime.now(UTC)

    _, expires_at = await management_grant.mint_grant(adb, user)

    assert timedelta(seconds=42) <= expires_at - before <= timedelta(seconds=52)
    stored = (
        await adb.execute(
            select(ManagementGrant.expires_at).where(ManagementGrant.user_id == user.id)
        )
    ).scalar_one()
    assert stored == expires_at  # the response and the row agree on the deadline


def test_the_default_grant_ttl_is_ten_minutes():
    assert settings.MANAGE_GRANT_TTL_S == 600


async def test_minting_a_grant_sweeps_every_expired_grant_not_just_the_minter_s(adb):
    """The sweep is GLOBAL, like the handoff-code sweep it is modelled on. A per-user sweep would
    leave the rows of a user who never comes back sitting there forever, so the "cannot accumulate
    unbounded" property would simply be false."""
    stale_owner = await _user(adb, "grant_sweep_stale")
    fresh_owner = await _user(adb, "grant_sweep_fresh")
    stale, _ = await management_grant.mint_grant(adb, stale_owner)
    await adb.execute(
        text(
            "UPDATE management_grants SET expires_at = now() - interval '1 hour' "
            "WHERE hashed_grant = :h"
        ),
        {"h": management_grant.hash_grant(stale)},
    )
    live, _ = await management_grant.mint_grant(adb, stale_owner)

    # A DIFFERENT user's mint is what runs the sweep — the whole point of "global".
    await management_grant.mint_grant(adb, fresh_owner)

    survivors = set(
        (await adb.execute(select(ManagementGrant.hashed_grant))).scalars().all()
    )
    assert management_grant.hash_grant(stale) not in survivors
    assert management_grant.hash_grant(live) in survivors  # the sweep took only the dead one


async def test_a_native_login_code_is_stamped_with_its_purpose(adb):
    """The transaction cookie dies at the callback, so the purpose has to live on the CODE ROW —
    otherwise the two exchange endpoints cannot tell each other's codes apart at all."""
    user = await _user(adb, "code_purpose_user")

    login_code = await native_auth.mint_code(adb, user, RFC_CHALLENGE, DEVICE)
    manage_code = await native_auth.mint_code(
        adb, user, RFC_CHALLENGE, DEVICE, purpose=native_auth.PURPOSE_MANAGE
    )

    purposes = dict(
        (
            await adb.execute(
                select(native_auth.NativeAuthCode.code_hash, native_auth.NativeAuthCode.purpose)
            )
        ).all()
    )
    assert purposes[native_auth.hash_code(login_code)] == "login"
    assert purposes[native_auth.hash_code(manage_code)] == "manage"


def test_the_purpose_column_defaults_to_login_for_rows_that_predate_it(db_engine):
    """Expand-only: every historical row was a login code, and the server default is what says so
    without a backfill. Inserted WITHOUT the column, the way the pre-migration code wrote it."""
    _sql(
        "INSERT INTO users (subject, role) VALUES ('code_purpose_default', 'user') "
        "ON CONFLICT (subject) DO NOTHING"
    )
    try:
        _sql(
            "INSERT INTO native_auth_codes (code_hash, user_id, code_challenge, device_name, "
            "expires_at) SELECT 'purpose-default-probe', id, :c, :d, now() + interval '1 minute' "
            "FROM users WHERE subject = 'code_purpose_default'",
            c=RFC_CHALLENGE,
            d=DEVICE,
        )
        purpose = _sql(
            "SELECT purpose FROM native_auth_codes WHERE code_hash = 'purpose-default-probe'"
        )[0][0]
        assert purpose == "login"
    finally:
        _sql("DELETE FROM users WHERE subject = 'code_purpose_default'")


@pytest.mark.parametrize(
    "purpose", [native_auth.PURPOSE_LOGIN, native_auth.PURPOSE_MANAGE]
)
async def test_consume_code_only_matches_its_own_purpose(adb, purpose):
    """The purpose rides in the atomic consume's WHERE, so a wrong-purpose attempt matches nothing
    — refusal and consumption cannot diverge, and the code survives to be used properly."""
    other = (
        native_auth.PURPOSE_MANAGE
        if purpose == native_auth.PURPOSE_LOGIN
        else native_auth.PURPOSE_LOGIN
    )
    user = await _user(adb, f"consume_purpose_{purpose}")
    code = await native_auth.mint_code(adb, user, RFC_CHALLENGE, DEVICE, purpose=purpose)

    with pytest.raises(native_auth.NativeCodeRejected) as rejected:
        await native_auth.consume_code(adb, code, purpose=other)
    assert rejected.value.reason == "code_invalid"

    consumed_at = (
        await adb.execute(
            select(native_auth.NativeAuthCode.consumed_at).where(
                native_auth.NativeAuthCode.code_hash == native_auth.hash_code(code)
            )
        )
    ).scalar_one()
    assert consumed_at is None  # refused, NOT burned

    claim = await native_auth.consume_code(adb, code, purpose=purpose)  # still usable
    assert claim.user_id == user.id


# --- B2: minting a grant through the login lane -------------------------------------------------


@pytest.fixture
def grant_oidc(client, db_engine, monkeypatch):
    """A CLEAN TestClient — empty cookie jar, no session ever created — with the OIDC network calls
    mocked at the same seam `logged_in_client` uses.

    Clean on purpose: the grant lane must work with no browser session anywhere near it, which is
    only worth asserting against a client that could not have been carrying one.
    """
    from unittest.mock import AsyncMock

    subject = "test_grant_user"
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"preferred_username": subject, "name": "G", "email": "g@x", "groups": []},
    )
    try:
        yield client, subject
    finally:
        # Cascades to the user's pats, native_auth_codes and management_grants.
        _sql("DELETE FROM users WHERE subject = :s", s=subject)


def _login(http, **params: str):
    """GET the login leg. Named `http` rather than `client` because `client=ios` is itself one of
    the query parameters this route takes."""
    return http.get("/api/auth/login?" + urlencode(params), follow_redirects=False)


def _handoff_code(client, *, purpose: str | None = None, device: str = DEVICE) -> str:
    """A full ios login, returning the one-shot code the app reads off the callback URL."""
    extra = {"purpose": purpose} if purpose else {}
    started = _login(client, client="ios", code_challenge=RFC_CHALLENGE, device=device, **extra)
    assert started.status_code == 302, started.text
    tx = started.cookies[auth.TX_COOKIE]
    client.cookies.set(auth.TX_COOKIE, tx)
    landed = client.get(
        "/api/auth/callback?"
        + urlencode({"code": "authelia-code", "state": auth.decrypt(tx)["state"]}),
        follow_redirects=False,
    )
    assert landed.status_code == 302, landed.text
    return parse_qs(urlsplit(landed.headers["location"]).query)["code"][0]


def _manage_grant(client, code: str, verifier: str = RFC_VERIFIER):
    return client.post(
        "/api/auth/native/manage-grant", json={"code": code, "code_verifier": verifier}
    )


def _exchange(client, code: str, verifier: str = RFC_VERIFIER):
    return client.post(
        "/api/auth/native/exchange", json={"code": code, "code_verifier": verifier}
    )


def _grant(client) -> str:
    """A live grant, minted the only way one can be: a fresh login through the manage lane."""
    resp = _manage_grant(client, _handoff_code(client, purpose="manage"))
    assert resp.status_code == 200, resp.text
    return resp.json()["grant"]


def _code_row(code: str):
    return _sql(
        "SELECT purpose, consumed_at FROM native_auth_codes WHERE code_hash = :h",
        h=native_auth.hash_code(code),
    )


def _grant_count(subject: str) -> int:
    return _sql(
        "SELECT count(*) FROM management_grants g JOIN users u ON u.id = g.user_id "
        "WHERE u.subject = :s",
        s=subject,
    )[0][0]


def _session_count(subject: str) -> int:
    return _sql(
        "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.subject = :s",
        s=subject,
    )[0][0]


def test_the_manage_lane_mints_a_manage_purpose_code_and_leaves_no_session(grant_oidc):
    """The round trip is byte-identical to the device-login one for Authelia's half — no second
    client, no new redirect URI, no household SSO restart — and the lane it was for survives the
    callback on the code row rather than in the cookie that is deleted there."""
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")

    assert _code_row(code) == [("manage", None)]
    assert auth.SID_COOKIE not in client.cookies
    assert _session_count(subject) == 0
    # The control for that zero: the SAME client and mocks in web mode do create one.
    web = _login(client, next="/")
    client.cookies.set(auth.TX_COOKIE, web.cookies[auth.TX_COOKIE])
    client.get(
        "/api/auth/callback?"
        + urlencode(
            {"code": "authelia-code", "state": auth.decrypt(web.cookies[auth.TX_COOKIE])["state"]}
        ),
        follow_redirects=False,
    )
    assert _session_count(subject) == 1


def test_the_management_purpose_never_reaches_the_idp(grant_oidc):
    """Decision 1 again: the purpose is BFF state carried in the encrypted transaction cookie. If
    it reached Authelia the flow would need a second client and a config change to go with it."""
    client, _ = grant_oidc
    web = _login(client, next="/")
    client.cookies.clear()
    manage = _login(
        client, client="ios", code_challenge=RFC_CHALLENGE, device=DEVICE, purpose="manage"
    )

    web_q = parse_qs(urlsplit(web.headers["location"]).query)
    manage_q = parse_qs(urlsplit(manage.headers["location"]).query)
    assert set(manage_q) == set(web_q)
    for key in set(web_q) - {"state", "code_challenge", "nonce"}:
        assert manage_q[key] == web_q[key], key
    assert "manage" not in manage.headers["location"]


def test_a_manage_code_becomes_a_grant_that_is_stored_hashed(grant_oidc):
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")

    resp = _manage_grant(client, code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"grant", "expires_at"}
    grant = body["grant"]

    rows = _sql(
        "SELECT g.hashed_grant, g.expires_at FROM management_grants g JOIN users u "
        "ON u.id = g.user_id WHERE u.subject = :s",
        s=subject,
    )
    assert len(rows) == 1
    hashed, expires_at = rows[0]
    assert hashed == management_grant.hash_grant(grant)
    assert hashed != grant  # the plaintext is returned once and stored nowhere
    assert abs(datetime.fromisoformat(body["expires_at"]) - expires_at) < timedelta(seconds=5)
    ttl = expires_at - datetime.now(UTC)
    assert timedelta(seconds=settings.MANAGE_GRANT_TTL_S - 60) < ttl <= timedelta(
        seconds=settings.MANAGE_GRANT_TTL_S
    )
    assert _code_row(code)[0][1] is not None  # the code is burned by its redemption


def test_a_login_code_cannot_be_redeemed_for_a_grant(grant_oidc):
    """Purpose isolation, direction one — and the refusal must not COST the owner the code: the
    purpose rides in the atomic consume's WHERE, so a wrong-lane attempt matches nothing rather
    than burning a row it then refuses to honour."""
    client, subject = grant_oidc
    code = _handoff_code(client)  # a plain device-login code

    refused = _manage_grant(client, code)
    assert refused.status_code == 400
    assert refused.json()["detail"] == "code_invalid"
    assert _grant_count(subject) == 0
    assert _code_row(code) == [("login", None)]  # refused, NOT burned

    minted = _exchange(client, code)  # and still worth exactly what it was minted for
    assert minted.status_code == 200, minted.text
    assert minted.json()["token"].startswith(auth.PAT_PREFIX)


def test_a_manage_code_cannot_be_exchanged_for_a_device_token(grant_oidc):
    """Purpose isolation, direction two — the one that matters most: a manage code redeemed at the
    device-token endpoint would hand a year-long PAT to a flow that asked for ten minutes."""
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")

    refused = _exchange(client, code)
    assert refused.status_code == 400
    assert refused.json()["detail"] == "code_invalid"
    assert _sql("SELECT count(*) FROM pats WHERE name = :n", n=DEVICE)[0][0] == 0
    assert _code_row(code) == [("manage", None)]  # refused, NOT burned

    granted = _manage_grant(client, code)  # still redeemable in its own lane
    assert granted.status_code == 200, granted.text
    assert _grant_count(subject) == 1


def test_a_manage_code_can_only_be_redeemed_once(grant_oidc):
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")

    assert _manage_grant(client, code).status_code == 200
    second = _manage_grant(client, code)
    assert second.status_code == 400
    assert second.json()["detail"] == "code_consumed"
    assert _grant_count(subject) == 1


def test_a_wrong_verifier_is_refused_and_still_burns_the_manage_code(grant_oidc):
    """One attempt per code whatever its outcome: the burn is committed BEFORE the verifier is
    judged, so a guesser cannot retry a code it holds until it lands the verifier it does not."""
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")

    bad = _manage_grant(client, code, verifier=OTHER_VERIFIER)
    assert bad.status_code == 400
    assert bad.json()["detail"] == "verifier_mismatch"
    assert _code_row(code)[0][1] is not None
    assert _grant_count(subject) == 0

    retry = _manage_grant(client, code)  # now with the RIGHT verifier
    assert retry.status_code == 400
    assert retry.json()["detail"] == "code_consumed"
    assert _grant_count(subject) == 0


def test_an_expired_manage_code_is_refused(grant_oidc):
    """Backdated in PostgreSQL rather than with a frozen Python clock: `expires_at > now()` is
    evaluated by the database, so only a real stored instant tests the real predicate."""
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")
    _sql(
        "UPDATE native_auth_codes SET expires_at = now() - interval '1 second' "
        "WHERE code_hash = :h",
        h=native_auth.hash_code(code),
    )

    resp = _manage_grant(client, code)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_expired"
    assert _grant_count(subject) == 0


def test_an_unknown_code_buys_no_grant(client, db_engine):
    resp = _manage_grant(client, "not-a-code-anyone-ever-minted")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_invalid"


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"purpose": "manage"}, id="manage-on-the-web-lane"),
        pytest.param({"purpose": "login"}, id="login-purpose-on-the-web-lane"),
        pytest.param(
            {"client": "ios", "code_challenge": RFC_CHALLENGE, "device": DEVICE, "purpose": "root"},
            id="unknown-purpose",
        ),
        pytest.param(
            {"client": "ios", "code_challenge": RFC_CHALLENGE, "device": DEVICE, "purpose": ""},
            id="empty-purpose",
        ),
    ],
)
def test_a_purpose_the_lane_does_not_offer_is_refused(client, db_engine, params):
    """`purpose` is a native-lane parameter: the web login has no management mode to ask for, and
    an unknown value must be refused rather than silently treated as a plain login."""
    assert _login(client, **params).status_code == 422


def test_the_native_lanes_the_login_route_does_offer_are_accepted(client, db_engine):
    """The positive control for the refusals above — without it they would all pass against a
    route that had simply started refusing everything."""
    for purpose in ("login", "manage"):
        resp = _login(
            client, client="ios", code_challenge=RFC_CHALLENGE, device=DEVICE, purpose=purpose
        )
        assert resp.status_code == 302, purpose
        assert auth.decrypt(resp.cookies[auth.TX_COOKIE])["purpose"] == purpose
    assert _login(client, next="/").status_code == 302  # and the web lane is untouched


def test_the_manage_grant_route_is_documented_as_anonymous():
    """It takes a code and a verifier, not a credential — the generated clients see only this
    document, and an endpoint that requires nothing has to say so."""
    from app.main import app

    schema = app.openapi()
    assert "/api/auth/native/manage-grant" in schema["paths"]
    assert "security" not in schema["paths"]["/api/auth/native/manage-grant"]["post"]
