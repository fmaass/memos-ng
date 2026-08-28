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

import pytest
from app.config import settings
from app.models import ManagementGrant, User
from app.services import management_grant, native_auth
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
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
