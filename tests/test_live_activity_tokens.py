"""`/api/devices/live-activity-token` (MEMNG-946): the iOS app registers its ActivityKit
push-to-start token so the server can later start a Live Activity for a capture the app never saw.

One token is one device's mailbox, so it belongs to one account at a time: posting it upserts by
token, and a token posted by a different member MOVES to that member (the device switched
accounts). Removal is decided by the SESSION, not by the token — a member removes only their own
row, and a request naming someone else's token is answered exactly like one naming an unknown
token. Both routes take `require_user`, so an upload-only capture grant is refused.

Every test starts and ends with an empty table (`_empty_table`); expected values are literals.
"""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.main import app
from sqlalchemy.exc import IntegrityError

from tests.test_attachments_taken import _sql
from tests.test_uploads import _drop_user, _login_as

PATH = "/api/devices/live-activity-token"
# The revision this module's migration sits on; the round trip walks down to it by NAME, so a
# migration landing on top later cannot make `-1` silently test a different revision.
BEFORE_TOKENS_REVISION = "eeda927cec0a"
OTHER = "lat946_other"

TOKEN_A = "a1" * 32  # 64 hex chars, the length an APNs token has today
TOKEN_B = "b2" * 32


def _rows() -> list[tuple[str, str, str]]:
    """Every stored token as (token, owner subject, environment), ordered by token."""
    return [
        tuple(r)
        for r in _sql(
            "SELECT t.token, u.subject, t.environment FROM live_activity_tokens t "
            "JOIN users u ON u.id = t.user_id ORDER BY t.token"
        )
    ]


@pytest.fixture(autouse=True)
def _empty_table(db_engine):
    _sql("DELETE FROM live_activity_tokens")
    yield
    _sql("DELETE FROM live_activity_tokens")


@pytest.fixture
def other(logged_in_client, monkeypatch):
    """A SECOND member's real session, beside the conftest member's."""
    client = _login_as(monkeypatch, OTHER)
    try:
        yield client
    finally:
        _drop_user(OTHER)


# --- who may call ---------------------------------------------------------------------------------


def test_anonymous_is_401_on_both_routes(client):
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 401
    assert client.request("DELETE", PATH, json={"token": TOKEN_A}).status_code == 401
    assert _rows() == []


async def test_a_capture_grant_is_refused_and_its_parent_token_is_not(pat_client):
    """The grant is upload-only: both routes refuse it and write nothing, while the very token
    that minted it — a member's PAT bearer — registers fine."""
    bearer, subject, _ = pat_client
    minted = await bearer.post("/api/tokens/capture-grant")
    assert minted.status_code == 201, minted.text
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {minted.json()['token']}"},
    ) as grant:
        assert (await grant.post(PATH, json={"token": TOKEN_A})).status_code == 401
        assert _rows() == []
        assert (await bearer.post(PATH, json={"token": TOKEN_A})).status_code == 204
        assert (await grant.request("DELETE", PATH, json={"token": TOKEN_A})).status_code == 401
    assert _rows() == [(TOKEN_A, subject, "production")]


# --- POST -----------------------------------------------------------------------------------------


def test_post_stores_the_token_for_the_caller_as_production_by_default(logged_in_client):
    client, me = logged_in_client
    r = client.post(PATH, json={"token": TOKEN_A})
    assert r.status_code == 204, r.text
    assert r.content == b""
    assert _rows() == [(TOKEN_A, me, "production")]
    stamps = _sql("SELECT created_at, updated_at FROM live_activity_tokens")
    assert stamps[0][0] is not None and stamps[0][1] is not None


def test_post_stores_a_sandbox_token(logged_in_client):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A, "environment": "sandbox"}).status_code == 204
    assert _rows() == [(TOKEN_A, me, "sandbox")]


def test_re_posting_the_same_token_is_idempotent(logged_in_client):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    first_id = _sql("SELECT id FROM live_activity_tokens")[0][0]
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert _rows() == [(TOKEN_A, me, "production")]
    assert _sql("SELECT id FROM live_activity_tokens")[0][0] == first_id  # updated in place


def test_re_posting_with_another_environment_updates_the_one_row(logged_in_client):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A, "environment": "sandbox"}).status_code == 204
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert _rows() == [(TOKEN_A, me, "production")]


def test_upper_case_hex_is_stored_lower_case_and_is_the_same_token(logged_in_client):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A.upper()}).status_code == 204
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert _rows() == [(TOKEN_A, me, "production")]


def test_a_member_keeps_several_devices(logged_in_client):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert client.post(PATH, json={"token": TOKEN_B}).status_code == 204
    assert _rows() == [(TOKEN_A, me, "production"), (TOKEN_B, me, "production")]


def test_a_token_posted_by_another_member_moves_to_that_member(logged_in_client, other):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert client.post(PATH, json={"token": TOKEN_B}).status_code == 204
    assert other.post(PATH, json={"token": TOKEN_A, "environment": "sandbox"}).status_code == 204
    assert _rows() == [(TOKEN_A, OTHER, "sandbox"), (TOKEN_B, me, "production")]


# --- DELETE ---------------------------------------------------------------------------------------


def test_the_owner_deletes_their_token_and_a_second_delete_is_still_204(logged_in_client):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert client.post(PATH, json={"token": TOKEN_B}).status_code == 204
    first = client.request("DELETE", PATH, json={"token": TOKEN_A})
    assert first.status_code == 204 and first.content == b""
    assert _rows() == [(TOKEN_B, me, "production")]  # only the named token went
    assert client.request("DELETE", PATH, json={"token": TOKEN_A}).status_code == 204
    assert _rows() == [(TOKEN_B, me, "production")]


def test_delete_matches_the_token_whatever_its_hex_case(logged_in_client):
    client, _ = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert client.request("DELETE", PATH, json={"token": TOKEN_A.upper()}).status_code == 204
    assert _rows() == []


def test_another_member_cannot_delete_a_token_and_learns_nothing(logged_in_client, other):
    """The answer to someone else's token is the answer to a token nobody holds."""
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    theirs = other.request("DELETE", PATH, json={"token": TOKEN_A})
    unknown = other.request("DELETE", PATH, json={"token": TOKEN_B})
    assert (theirs.status_code, theirs.content) == (204, b"")
    assert (theirs.status_code, theirs.content) == (unknown.status_code, unknown.content)
    assert _rows() == [(TOKEN_A, me, "production")]


# --- validation -----------------------------------------------------------------------------------

BAD_TOKENS = {
    "empty": "",
    "one_short": "a" * 31,
    "one_long": "a" * 513,
    "not_hex": "g" * 64,
    "prefixed": "0x" + "a" * 62,
    "spaced": "a1 " * 21 + "a",
    "base64": "q83vEjRWeJA" * 4,
    # A valid token INSIDE a longer string: only an anchored match refuses these.
    "trailing_newline": TOKEN_A + "\n",
    "quoted": f'"{TOKEN_A}"',
}


@pytest.mark.parametrize("token", BAD_TOKENS.values(), ids=BAD_TOKENS.keys())
def test_a_malformed_token_is_422_on_both_routes_and_writes_nothing(logged_in_client, token):
    client, _ = logged_in_client
    assert client.post(PATH, json={"token": token}).status_code == 422
    assert _rows() == []
    assert client.request("DELETE", PATH, json={"token": token}).status_code == 422


@pytest.mark.parametrize("length", [32, 512])
def test_the_length_bounds_are_inclusive(logged_in_client, length):
    client, me = logged_in_client
    token = "c" * length
    assert client.post(PATH, json={"token": token}).status_code == 204
    assert _rows() == [(token, me, "production")]


@pytest.mark.parametrize(
    "body",
    [
        {"token": TOKEN_A, "environment": "development"},
        {"token": TOKEN_A, "environment": "PRODUCTION"},
        {"token": TOKEN_A, "environment": None},
        {"token": None},
        {"environment": "sandbox"},
        {},
    ],
)
def test_a_bad_body_is_422_and_writes_nothing(logged_in_client, body):
    client, _ = logged_in_client
    assert client.post(PATH, json=body).status_code == 422
    assert _rows() == []


def test_delete_without_a_token_is_422(logged_in_client):
    client, _ = logged_in_client
    assert client.request("DELETE", PATH, json={}).status_code == 422
    assert client.request("DELETE", PATH).status_code == 422


# --- the table ------------------------------------------------------------------------------------


def test_deleting_the_member_takes_their_tokens_with_them(logged_in_client, other):
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    assert other.post(PATH, json={"token": TOKEN_B}).status_code == 204
    _drop_user(OTHER)
    assert _rows() == [(TOKEN_A, me, "production")]


def test_the_database_refuses_an_unknown_environment_and_a_duplicate_token(logged_in_client):
    """The CHECK and the UNIQUE are the last line behind the router's 422 and upsert."""
    client, me = logged_in_client
    assert client.post(PATH, json={"token": TOKEN_A}).status_code == 204
    user_id = _sql("SELECT id FROM users WHERE subject = :s", {"s": me})[0][0]
    insert = "INSERT INTO live_activity_tokens (user_id, token, environment) VALUES (:u, :t, :e)"
    with pytest.raises(IntegrityError):
        _sql(insert, {"u": user_id, "t": TOKEN_B, "e": "staging"})
    with pytest.raises(IntegrityError):
        _sql(insert, {"u": user_id, "t": TOKEN_A, "e": "sandbox"})
    with pytest.raises(IntegrityError):
        _sql(insert, {"u": user_id, "t": "NOT-HEX" * 8, "e": "sandbox"})
    _sql(
        "INSERT INTO live_activity_tokens (user_id, token) VALUES (:u, :t)",
        {"u": user_id, "t": TOKEN_B},
    )
    assert _rows() == [(TOKEN_A, me, "production"), (TOKEN_B, me, "production")]


# --- races ----------------------------------------------------------------------------------------


async def test_two_concurrent_first_posts_of_one_token_leave_one_row(pat_client, monkeypatch):
    """Both requests are held at the write until both have reached it, so both write a token that
    did not exist yet: the unique token settles it as one row and neither request sees the other's
    insert as an error."""
    from app.routers import devices

    bearer, subject, _ = pat_client
    real = devices.upsert_live_activity_token
    both_arrived = asyncio.Barrier(2)
    entered: list[int] = []

    async def held(*args, **kwargs):
        entered.append(1)
        async with asyncio.timeout(10):
            await both_arrived.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(devices, "upsert_live_activity_token", held)
    results = await asyncio.gather(
        bearer.post(PATH, json={"token": TOKEN_A}),
        bearer.post(PATH, json={"token": TOKEN_A, "environment": "sandbox"}),
    )
    assert [r.status_code for r in results] == [204, 204], [r.text for r in results]
    assert len(entered) == 2  # both really overlapped before the write
    rows = _rows()
    assert len(rows) == 1 and rows[0][:2] == (TOKEN_A, subject)


# --- the schema -----------------------------------------------------------------------------------


def test_openapi_describes_both_operations_with_a_json_body():
    doc = app.openapi()
    item = doc["paths"][PATH]
    assert set(item) == {"post", "delete"}
    for method in ("post", "delete"):
        assert set(item[method]["responses"]) >= {"204", "422"}
        assert item[method]["requestBody"]["required"] is True
        assert "application/json" in item[method]["requestBody"]["content"]
    post_schema = doc["components"]["schemas"]["LiveActivityTokenIn"]
    assert post_schema["required"] == ["token"]
    assert post_schema["properties"]["environment"]["enum"] == ["production", "sandbox"]
    assert "anyOf" not in post_schema["properties"]["environment"]  # no null arm for Swift
    assert doc["components"]["schemas"]["LiveActivityTokenRef"]["required"] == ["token"]


# --- the migration --------------------------------------------------------------------------------


def _table_exists() -> bool:
    return bool(
        _sql(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='live_activity_tokens'"
        )
    )


def test_the_migration_round_trips(db_engine):
    """Down to the revision below it removes exactly the table; up again restores it, empty and
    with its cascade, and leaves every other table's rows where they were."""
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    users_before = _sql("SELECT count(*) FROM users")[0][0]
    assert _table_exists()  # control: head carries it
    try:
        command.downgrade(cfg, BEFORE_TOKENS_REVISION)
        assert not _table_exists()
    finally:
        command.upgrade(cfg, "head")
    assert _table_exists()
    assert _sql("SELECT count(*) FROM live_activity_tokens")[0][0] == 0
    assert _sql("SELECT count(*) FROM users")[0][0] == users_before
    on_delete = _sql(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid='live_activity_tokens'::regclass AND contype='f'"
    )
    assert [r[0] for r in on_delete] == ["c"]  # ON DELETE CASCADE from users
