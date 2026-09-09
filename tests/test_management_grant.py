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

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from app import auth
from app.config import settings
from app.csrf import canonical_origin
from app.main import app
from app.models import ManagementGrant, User
from app.services import management_grant, native_auth
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

# Every case here mints or redeems a handoff code and most of them mint a grant; both are
# time-bounded (`NATIVE_CODE_TTL_S`, `MANAGE_GRANT_TTL_S`) and neither window may be measured
# against a wall clock a stall can cross. The whole module runs under the controlled clock; a case
# that asserts something about an instant takes the fixture by name and reads it (MEMNG-296).
pytestmark = pytest.mark.usefixtures("controlled_clock")

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


async def test_a_grant_expires_after_the_configured_ttl(adb, monkeypatch, controlled_clock):
    """The bound is read from settings at mint time, not baked in: a grant authenticates a
    management session of minutes, and that window is the whole reason it is safe to hand out.

    Under the controlled clock the deadline is exact — 42 seconds past the pinned instant, not
    "42-ish" — so the assertion can no longer be satisfied by a mint that ignored the setting and
    happened to land inside a tolerance.
    """
    monkeypatch.setattr(settings, "MANAGE_GRANT_TTL_S", 42)
    user = await _user(adb, "grant_ttl_user")

    _, expires_at = await management_grant.mint_grant(adb, user)

    # Also the control that the pin reached the mint: the instant it read is this one.
    assert expires_at == controlled_clock.now() + timedelta(seconds=42)
    stored = (
        await adb.execute(
            select(ManagementGrant.expires_at).where(ManagementGrant.user_id == user.id)
        )
    ).scalar_one()
    assert stored == expires_at  # the response and the row agree on the deadline


@pytest.mark.real_clock
def test_the_default_grant_ttl_is_ten_minutes():
    assert settings.MANAGE_GRANT_TTL_S == 600


async def test_minting_a_grant_sweeps_every_expired_grant_not_just_the_minter_s(
    adb, controlled_clock
):
    """The sweep is GLOBAL, like the handoff-code sweep it is modelled on. A per-user sweep would
    leave the rows of a user who never comes back sitting there forever, so the "cannot accumulate
    unbounded" property would simply be false."""
    stale_owner = await _user(adb, "grant_sweep_stale")
    fresh_owner = await _user(adb, "grant_sweep_fresh")
    stale, _ = await management_grant.mint_grant(adb, stale_owner)
    await adb.execute(
        text("UPDATE management_grants SET expires_at = :t WHERE hashed_grant = :h"),
        {
            "t": controlled_clock.now() - timedelta(hours=1),
            "h": management_grant.hash_grant(stale),
        },
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


@pytest.mark.real_clock
def test_the_purpose_column_defaults_to_login_for_rows_that_predate_it(db_engine):
    """Expand-only: every historical row was a login code, and the server default is what says so
    without a backfill. Inserted WITHOUT the column, the way the pre-migration code wrote it."""
    _sql(
        "INSERT INTO users (subject, role) VALUES ('code_purpose_default', 'user') "
        "ON CONFLICT (subject) DO NOTHING"
    )
    try:
        # SQL `now()` is right here and only here: this row is read for its `purpose` column and
        # never redeemed, so no pinned code ever compares its deadline — and the case runs on the
        # real clock (`real_clock`) precisely because it touches none.
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


def test_a_manage_code_becomes_a_grant_that_is_stored_hashed(grant_oidc, controlled_clock):
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
    # The stored deadline is the pinned instant plus the configured TTL, exactly — which is both
    # the TTL assertion and the control that the pin reached the route that minted this grant.
    assert expires_at == controlled_clock.now() + timedelta(seconds=settings.MANAGE_GRANT_TTL_S)
    # The wire drops sub-second precision (`to_rfc3339`), and nothing else.
    assert datetime.fromisoformat(body["expires_at"]) == expires_at.replace(microsecond=0)
    # Burned by its redemption, and stamped with the controlled instant — the store's own record
    # that the consume ran under the pin.
    assert _code_row(code)[0][1] == controlled_clock.now()


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


def test_an_expired_manage_code_is_refused(grant_oidc, controlled_clock):
    """The deadline is a real STORED instant the running predicate reads — but computed from the
    controlled clock, never from PostgreSQL's `now()`: the two clocks are independent once the pin
    starts, and a one-second backdate against the wrong one lands in the pinned future."""
    client, subject = grant_oidc
    code = _handoff_code(client, purpose="manage")
    _sql(
        "UPDATE native_auth_codes SET expires_at = :t WHERE code_hash = :h",
        t=controlled_clock.now() - timedelta(seconds=1),
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


@pytest.mark.real_clock
def test_the_manage_grant_route_is_documented_as_anonymous():
    """It takes a code and a verifier, not a credential — the generated clients see only this
    document, and an endpoint that requires nothing has to say so."""
    from app.main import app

    schema = app.openapi()
    assert "/api/auth/native/manage-grant" in schema["paths"]
    assert "security" not in schema["paths"]["/api/auth/native/manage-grant"]["post"]


# `TestClient`'s default peer address, and therefore the limiter's bucket key for every
# request these tests make.
TESTCLIENT_HOST = "testclient"


def test_the_manage_grant_route_is_rate_limited_like_the_login_legs(grant_oidc, controlled_clock):
    """The same `rate_limit()` dependency the login legs carry, on the step-up lane.

    Defence in depth rather than a live hole — a manage code is single-use, 120-second and
    PKCE-bound — but this endpoint is anonymous and pays out the capability to mint and revoke
    this account's tokens, so it gets the parity `/api/auth/login` and `/api/auth/callback`
    already have. The limiter is a dependency, so it answers BEFORE the handler: a burst is
    refused with its own 429, while a grant bought still inside the window is untouched.

    The clock the limiter reads is pinned for the whole case, so the window cannot expire
    between filling the bucket and asking for the refusal: the outcome depends on the request
    count and nothing else.
    """
    client, _ = grant_oidc
    code = _handoff_code(client, purpose="manage")
    # The login leg that minted the code shares the caller's bucket; burst from empty.
    auth._RL.clear()

    for _ in range(19):  # one short of rate_limit()'s default limit of 20
        refused = _manage_grant(client, "not-a-code-anyone-ever-minted")
        assert refused.status_code == 400, refused.text

    # Still under the limit: a genuine redemption answers exactly as it did before the limiter.
    minted = _manage_grant(client, code)
    assert minted.status_code == 200, minted.text
    assert minted.json()["grant"]

    over = _manage_grant(client, "not-a-code-anyone-ever-minted")
    assert over.status_code == 429, over.text
    assert over.json()["detail"] == "Too many requests"

    # The pin really reached the limiter: twenty stamps, every one of them the frozen instant —
    # the refusal above was decided by the request COUNT, with a window that never advanced.
    assert set(auth._RL[TESTCLIENT_HOST]) == {controlled_clock.monotonic}
    assert len(auth._RL[TESTCLIENT_HOST]) == 20


# --- B3: the management dependency --------------------------------------------------------------


def _clean_client() -> TestClient:
    """A client with an EMPTY cookie jar — the shape a native app has. Nothing but the header it is
    handed can be answering for it, which is what every grant assertion below depends on."""
    return TestClient(app, raise_server_exceptions=False)


def _as_grant(grant: str) -> dict[str, str]:
    return {"Authorization": f"Grant {grant}"}


def _grant_for(subject: str, monkeypatch) -> str:
    """A live grant for `subject`, minted the only way one can be: a real trip through the manage
    lane, on its own clean client. The caller owns deleting the user afterwards."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"preferred_username": subject, "name": "X", "email": "x@x", "groups": []},
    )
    http = _clean_client()
    http.headers["Origin"] = canonical_origin()
    resp = _manage_grant(http, _handoff_code(http, purpose="manage"))
    assert resp.status_code == 200, resp.text
    return resp.json()["grant"]


async def test_authenticate_grant_answers_only_for_a_live_grant(adb, controlled_clock):
    """Expiry is enforced in the store, not in the caller, so every surface that ever accepts a
    grant inherits the bound from one place."""
    user = await _user(adb, "grant_authn_user")
    grant, _ = await management_grant.mint_grant(adb, user)

    got = await management_grant.authenticate_grant(adb, grant)
    assert got is not None and got.id == user.id
    assert await management_grant.authenticate_grant(adb, "not-a-grant-anyone-minted") is None

    await adb.execute(
        text("UPDATE management_grants SET expires_at = :t WHERE hashed_grant = :h"),
        {
            "t": controlled_clock.now() - timedelta(seconds=1),
            "h": management_grant.hash_grant(grant),
        },
    )
    assert await management_grant.authenticate_grant(adb, grant) is None


def test_manage_grant_requires_a_fresh_login(grant_oidc):
    """The grant is bought with an authentication and with nothing else. The schema check is the
    drift guard on that: exactly one operation in the whole API hands a grant back, so a later
    route that quietly minted one from a session or a token would turn this red."""
    import json as _json

    from app.main import app as fastapi_app

    client, subject = grant_oidc
    assert _manage_grant(client, "never-minted-by-anyone").status_code == 400
    assert _grant_count(subject) == 0

    schema = fastapi_app.openapi()
    granting = {
        f"#/components/schemas/{name}"
        for name, node in schema["components"]["schemas"].items()
        if "grant" in node.get("properties", {})
    }
    assert granting, schema["components"]["schemas"].keys()  # control: a grant shape exists at all
    minting = [
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if isinstance(operation, dict)
        and any(ref in _json.dumps(operation.get("responses", {})) for ref in granting)
    ]
    assert minting == [("POST", "/api/auth/native/manage-grant")]

    # And the lane itself really does work — without this the assertions above pass vacuously.
    grant = _grant(client)
    assert _clean_client().get("/api/pats", headers=_as_grant(grant)).status_code == 200


async def test_device_pat_cannot_obtain_a_grant(pat_client):
    """ADR-0015 from the other side: a token mints nothing but itself. It cannot buy a grant (it
    holds no code), and it cannot BE one — presenting a PAT under the Grant scheme is refused, not
    reinterpreted as the bearer it is."""
    bearer, subject, token = pat_client

    refused = await bearer.post(
        "/api/auth/native/manage-grant", json={"code": token, "code_verifier": RFC_VERIFIER}
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == "code_invalid"
    assert _grant_count(subject) == 0

    smuggled = _clean_client().get("/api/pats", headers=_as_grant(token))
    assert smuggled.status_code == 401
    assert (await bearer.get("/api/pats")).status_code == 401  # nor as the bearer it really is


def test_grant_authenticates_pat_management(grant_oidc):
    """The whole point of the phase: with a grant, the three token-management routes work for a
    caller that holds no browser session at all."""
    client, subject = grant_oidc
    headers = _as_grant(_grant(client))
    http = _clean_client()

    created = http.post("/api/pats", json={"name": "minted-under-grant"}, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["token"].startswith(auth.PAT_PREFIX)

    listed = http.get("/api/pats", headers=headers)
    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["minted-under-grant"]

    # The token it minted is a real one, not a shape that merely serialises like one.
    assert (
        _clean_client()
        .get("/api/memos", headers={"Authorization": f"Bearer {body['token']}"})
        .status_code
        == 200
    )

    assert http.delete(f"/api/pats/{body['id']}", headers=headers).status_code == 204
    assert http.get("/api/pats", headers=headers).json() == []
    assert _sql("SELECT count(*) FROM pats WHERE name = 'minted-under-grant'")[0][0] == 0


def test_grant_expires(grant_oidc, controlled_clock):
    """Three verdicts on ONE grant — usable, refused once its stored deadline is in the past,
    usable again when it is moved back — so only the stored instant can be deciding it.

    A real STORED instant decides all three, since `expires_at` is compared inside the query —
    but every one of them is computed from the controlled clock rather than PostgreSQL's `now()`,
    which the pinned code no longer shares a timeline with.
    """
    client, subject = grant_oidc
    grant = _grant(client)
    headers = _as_grant(grant)
    http = _clean_client()
    hashed = management_grant.hash_grant(grant)
    assert http.get("/api/pats", headers=headers).status_code == 200

    stored = _sql("SELECT expires_at FROM management_grants WHERE hashed_grant = :h", h=hashed)[0][
        0
    ]
    # The control that the pin reached the mint behind `_grant()`: the row's deadline is the
    # pinned instant plus the configured TTL, and the refusal below is the DB moving that stored
    # instant into the past rather than any time passing here.
    assert stored == controlled_clock.now() + timedelta(seconds=settings.MANAGE_GRANT_TTL_S)

    _sql(
        "UPDATE management_grants SET expires_at = :t WHERE hashed_grant = :h",
        t=controlled_clock.now() - timedelta(seconds=1),
        h=hashed,
    )
    assert http.get("/api/pats", headers=headers).status_code == 401

    _sql(
        "UPDATE management_grants SET expires_at = :t WHERE hashed_grant = :h",
        t=controlled_clock.now() + timedelta(hours=1),
        h=hashed,
    )
    assert http.get("/api/pats", headers=headers).status_code == 200


def test_expired_grant_is_401(grant_oidc, controlled_clock):
    """Expiry is authentication's answer, so no verb gets through and nothing happens on the way:
    the token that was there before the attempt is still there, unrevoked, afterwards."""
    client, subject = grant_oidc
    grant = _grant(client)
    headers = _as_grant(grant)
    http = _clean_client()
    existing = http.post("/api/pats", json={"name": "survives-the-expiry"}, headers=headers).json()
    _sql(
        "UPDATE management_grants SET expires_at = :t WHERE hashed_grant = :h",
        t=controlled_clock.now() - timedelta(seconds=1),
        h=management_grant.hash_grant(grant),
    )

    assert http.get("/api/pats", headers=headers).status_code == 401
    assert http.post("/api/pats", json={"name": "after"}, headers=headers).status_code == 401
    assert http.delete(f"/api/pats/{existing['id']}", headers=headers).status_code == 401

    names = [r[0] for r in _sql("SELECT name FROM pats WHERE user_id = (SELECT id FROM users "
                                "WHERE subject = :s)", s=subject)]
    assert names == ["survives-the-expiry"]


def test_grant_is_refused_on_non_management_routes(grant_oidc):
    """A grant buys token management and NOTHING else — it is not a general-purpose credential
    standing in for a session. Every route outside /api/pats/* answers it as the stranger it is."""
    client, _ = grant_oidc
    headers = _as_grant(_grant(client))
    http = _clean_client()

    assert http.get("/api/memos", headers=headers).status_code == 401
    assert http.post("/api/memos", json={"content": "x"}, headers=headers).status_code == 401
    assert http.get("/api/settings", headers=headers).status_code == 401
    assert http.get("/api/auth/user", headers=headers).status_code == 401
    assert http.get("/api/auth/native/token", headers=headers).status_code == 401
    # The control for those 401s: the same header on the surface it IS for.
    assert http.get("/api/pats", headers=headers).status_code == 200


def test_grant_is_scoped_to_its_own_user(grant_oidc, monkeypatch):
    """Keyed on the grant's OWNER, not on "some authenticated caller": a second household account's
    token is neither listed nor revocable, and comes out of the attempt still working."""
    client, _ = grant_oidc
    other = "test_grant_other_user"
    try:
        others_token = _exchange(
            client, _handoff_code(client, device="OtherPhone")
        ).json()["token"]
        others_pat_id = _sql(
            "SELECT id FROM pats WHERE hashed_token = :h", h=auth.hash_token(others_token)
        )[0][0]
        # The grant belongs to a DIFFERENT account than the token above.
        headers = _as_grant(_grant_for(other, monkeypatch))
        http = _clean_client()

        listed = http.get("/api/pats", headers=headers)
        assert listed.status_code == 200
        assert listed.json() == []  # the other account's phone is not this account's business

        assert http.delete(f"/api/pats/{others_pat_id}", headers=headers).status_code == 404
        assert _sql(
            "SELECT count(*) FROM pats WHERE hashed_token = :h", h=auth.hash_token(others_token)
        )[0][0] == 1
        assert (
            _clean_client()
            .get("/api/memos", headers={"Authorization": f"Bearer {others_token}"})
            .status_code
            == 200
        )
    finally:
        _sql("DELETE FROM users WHERE subject = :s", s=other)


async def test_pat_management_still_refuses_a_plain_bearer(pat_client):
    """The Option-5 refusal, re-pinned under the new dependency: widening /api/pats/* to a grant
    must not have widened it to the bearer that was refused there before."""
    bearer, _, _ = pat_client

    assert (await bearer.get("/api/pats")).status_code == 401
    assert (await bearer.post("/api/pats", json={"name": "successor"})).status_code == 401
    assert (await bearer.delete("/api/pats/1")).status_code == 401


def test_a_bearer_riding_a_valid_session_still_reaches_nothing(logged_in_client):
    """The other half of that refusal: the bearer is judged BEFORE the cookie is read, so a device
    token alongside a live browser session cannot reach token management by being ignored."""
    client, _ = logged_in_client
    own = client.post("/api/pats", json={"name": "laptop"})
    assert own.status_code == 201
    headers = {"Authorization": f"Bearer {own.json()['token']}"}

    assert client.get("/api/pats", headers=headers).status_code == 401
    assert client.post("/api/pats", json={"x": 1}, headers=headers).status_code == 401
    assert client.delete(f"/api/pats/{own.json()['id']}", headers=headers).status_code == 401
    assert client.delete(f"/api/pats/{own.json()['id']}").status_code == 204  # cookie alone: fine


def test_an_invalid_grant_beats_a_valid_cookie(logged_in_client):
    """THE fail-closed pin, and the mirror of `require_session_user`'s bearer rule: once a request
    states a Grant credential, that credential decides the outcome. A grant this server never
    issued must never quietly fall through to the session cookie riding alongside it — that is the
    silent downgrade the whole precedence order exists to prevent.
    """
    client, _ = logged_in_client
    headers = _as_grant("not-a-grant-this-server-ever-issued")
    assert client.get("/api/pats").status_code == 200  # the cookie on its own is fine

    assert client.get("/api/pats", headers=headers).status_code == 401
    assert client.post("/api/pats", json={"name": "smuggled"}, headers=headers).status_code == 401
    assert client.delete("/api/pats/1", headers=headers).status_code == 401
    assert client.get("/api/pats").json() == []  # and nothing was created on the way through

    # The same bad header with NO cookie is refused too — so the 401s above cannot be explained by
    # the cookie having been the only thing under test.
    assert _clean_client().get("/api/pats", headers=headers).status_code == 401


def test_a_grant_and_a_session_cookie_together_are_refused(logged_in_client, monkeypatch):
    """Two credentials naming two accounts is ambiguity, and ambiguity fails closed. Neither
    identity may silently win: a grant minted for one household account must not be able to reach
    another account's token list by riding that account's browser session (or the reverse).
    """
    client, subject = logged_in_client
    other = "test_grant_ambiguous_user"
    try:
        headers = _as_grant(_grant_for(other, monkeypatch))
        assert client.get("/api/pats").status_code == 200  # the cookie alone still works

        assert client.get("/api/pats", headers=headers).status_code == 401
        assert client.post("/api/pats", json={"name": "either"}, headers=headers).status_code == 401
        assert client.delete("/api/pats/1", headers=headers).status_code == 401
        assert _grant_count(other) == 1  # the grant itself is live; it is the pairing that is not
        assert _sql("SELECT count(*) FROM pats WHERE name = 'either'")[0][0] == 0
    finally:
        _sql("DELETE FROM users WHERE subject = :s", s=other)


@pytest.mark.real_clock
def test_the_pat_surface_publishes_exactly_the_two_credentials_it_takes():
    """The generated clients (ADR-0009) see only this document: /api/pats/* takes the session
    cookie or a grant, and a bearer must not appear there in any form."""
    from app.main import app as fastapi_app

    paths = fastapi_app.openapi()["paths"]
    pat_paths = [p for p in paths if p.startswith("/api/pats")]
    assert pat_paths
    for path in pat_paths:
        for method, operation in paths[path].items():
            if not isinstance(operation, dict) or "security" not in operation:
                continue
            assert operation["security"] == [{"sessionCookie": []}, {"grantAuth": []}], (
                method,
                path,
            )
