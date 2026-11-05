"""The upload-only capture grant (MEMNG-938): what the share extension presents instead of a token.

Every case speaks through the real HTTP layer with a client whose cookie jar is EMPTY, so the only
credential a request carries is the one the case put in its Authorization header — a grant that
"worked" because a session cookie rode along would prove nothing.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from app import auth
from app.config import settings
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

MINT = "/api/tokens/capture-grant"


@pytest.fixture(autouse=True)
async def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    yield
    await auth.drain_pat_stamp_tasks()
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


def _sql(statement: str, **params):
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(statement), params)
            return result.fetchall() if result.returns_rows else []
    finally:
        eng.dispose()


def _bearer(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _mint(bearer: AsyncClient) -> dict:
    r = await bearer.post(MINT)
    assert r.status_code == 201, r.text
    return r.json()


async def _start(client: AsyncClient, size: int = 5):
    return await client.post(
        "/api/uploads",
        json={"filename": "shot.jpg", "mime": "image/jpeg", "size": size},
    )


def _grant_rows(parent_token: str) -> list:
    return _sql(
        "SELECT g.id, g.name, g.expires_at FROM pats g JOIN pats p ON g.parent_pat_id = p.id "
        "WHERE p.hashed_token = :h",
        h=auth.hash_token(parent_token),
    )


# --- minting and listing ------------------------------------------------------------------------


async def test_mint_returns_a_24h_grant_listed_under_manage_tokens(pat_client, logged_in_client):
    bearer, _, _ = pat_client
    session, _ = logged_in_client
    before = datetime.now(UTC)
    grant = await _mint(bearer)

    assert grant["token"].startswith("memos_cap_")
    assert grant["name"] == "Capture grant, phone"
    expires = datetime.fromisoformat(grant["expires_at"])
    assert before + timedelta(hours=24) - timedelta(minutes=1) <= expires
    assert expires <= datetime.now(UTC) + timedelta(hours=24)

    listed = session.get("/api/pats")
    assert listed.status_code == 200
    rows = [p for p in listed.json() if p["id"] == grant["id"]]
    assert [(p["name"], p["expires_at"] is not None) for p in rows] == [
        ("Capture grant, phone", True)
    ]


async def test_the_grant_is_stored_under_its_own_hash_never_a_pats(pat_client):
    """A database read cannot be replayed, and a code path that hashes like a PAT never finds it."""
    bearer, _, _ = pat_client
    grant = await _mint(bearer)
    as_a_pat = auth.hash_token(grant["token"])
    assert _sql("SELECT id FROM pats WHERE hashed_token = :h", h=as_a_pat) == []
    stored = _sql("SELECT hashed_token FROM pats WHERE id = :i", i=grant["id"])
    assert len(stored) == 1
    assert grant["token"] not in stored[0].hashed_token


async def test_a_grant_never_outlives_its_parent_token(pat_client):
    bearer, _, token = pat_client
    soon = datetime.now(UTC) + timedelta(hours=2)
    parent = auth.hash_token(token)
    _sql("UPDATE pats SET expires_at = :e WHERE hashed_token = :h", e=soon, h=parent)
    grant = await _mint(bearer)
    assert datetime.fromisoformat(grant["expires_at"]) <= soon


async def test_a_session_cookie_cannot_mint(logged_in_client):
    session, _ = logged_in_client
    assert session.post(MINT).status_code == 401


async def test_an_anonymous_request_cannot_mint(client):
    assert client.post(MINT).status_code == 401


# --- what the grant may do ----------------------------------------------------------------------


async def test_the_grant_opens_appends_and_reads_the_offset_as_its_owner(pat_client):
    bearer, subject, _ = pat_client
    grant = await _mint(bearer)
    async with _bearer(grant["token"]) as g:
        started = await _start(g)
        assert started.status_code == 201, started.text
        uid = started.json()["upload_uid"]
        patched = await g.patch(
            f"/api/uploads/{uid}",
            content=b"abcde",
            headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["bytes_received"] == 5
        head = await g.head(f"/api/uploads/{uid}")
        assert head.status_code == 200
        assert head.headers["Upload-Offset"] == "5"
    owner = _sql(
        "SELECT u.subject FROM uploads up JOIN users u ON u.id = up.owner_id "
        "WHERE up.upload_uid = :u",
        u=uid,
    )
    assert [r.subject for r in owner] == [subject]


async def test_the_parent_token_still_reaches_everything_it_did(pat_client):
    bearer, _, _ = pat_client
    await _mint(bearer)
    assert (await bearer.get("/api/memos")).status_code == 200
    assert (await _start(bearer)).status_code == 201


def _refused_routes(uid: str, grant_id: int) -> list[tuple[str, str, dict]]:
    return [
        ("GET", "/api/memos", {}),
        ("POST", "/api/memos", {"json": {"content": "from a grant", "visibility": "PUBLIC"}}),
        ("GET", "/api/pats", {}),
        ("POST", "/api/pats", {"json": {"name": "escalate"}}),
        ("GET", "/api/uploads/limits", {}),
        ("GET", f"/api/uploads/{uid}/capture", {}),
        ("POST", f"/api/uploads/{uid}/finalize", {"json": {}}),
        ("POST", MINT, {}),
        ("DELETE", f"{MINT}/{grant_id}", {}),
        ("POST", "/api/auth/native/renew", {}),
        ("GET", "/api/auth/native/token", {}),
        ("POST", "/api/auth/native/logout", {}),
        ("GET", "/api/auth/user", {}),
        ("GET", "/api/v1/memos", {}),
        ("GET", "/api/events", {}),
    ]


async def test_every_other_route_refuses_the_grant_with_401(pat_client):
    bearer, _, _ = pat_client
    grant = await _mint(bearer)
    uid = (await _start(bearer)).json()["upload_uid"]
    answers = {}
    async with _bearer(grant["token"]) as g:
        for method, path, kwargs in _refused_routes(uid, grant["id"]):
            answers[(method, path)] = (await g.request(method, path, **kwargs)).status_code
    assert {k: v for k, v in answers.items() if v != 401} == {}
    # Nothing it was refused on happened anyway: no memo, and the grant (and its parent) live on.
    assert _sql("SELECT id FROM memos WHERE content = 'from a grant'") == []
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 201
    assert (await bearer.get("/api/memos")).status_code == 200


async def test_the_grant_withdraws_a_session_of_its_owner(pat_client):
    """DELETE on an upload session (MEMNG-953) is part of the upload protocol a share extension
    speaks, so the grant reaches it as its owner — here a session the parent token opened — and
    the withdrawn session then refuses its finalize."""
    bearer, _, _ = pat_client
    grant = await _mint(bearer)
    uid = (await _start(bearer)).json()["upload_uid"]
    async with _bearer(grant["token"]) as g:
        assert (await g.delete(f"/api/uploads/{uid}")).status_code == 204
    rows = _sql("SELECT withdrawn_at FROM uploads WHERE upload_uid = :u", u=uid)
    assert len(rows) == 1 and rows[0].withdrawn_at is not None
    fin = await bearer.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 409, fin.text
    assert fin.json()["detail"]["code"] == "upload_withdrawn"


async def test_a_grant_in_the_grant_scheme_is_not_a_management_grant(pat_client):
    bearer, _, _ = pat_client
    grant = await _mint(bearer)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Grant {grant['token']}"},
    ) as g:
        assert (await g.get("/api/pats")).status_code == 401


async def test_a_grant_shaped_row_is_refused_as_a_token_whatever_its_hash(pat_client, adb):
    """The PAT path refuses a capture grant by what the ROW is, not only by how it was hashed: a
    child row stored under a plain token hash still authenticates nothing as a token."""
    bearer, _, token = pat_client
    parent = _sql("SELECT id, user_id FROM pats WHERE hashed_token = :h", h=auth.hash_token(token))
    forged = "memos_pat_forged-child"
    _sql(
        "INSERT INTO pats (hashed_token, name, user_id, parent_pat_id) VALUES (:h, 'x', :u, :p)",
        h=auth.hash_token(forged),
        u=parent[0].user_id,
        p=parent[0].id,
    )
    assert await auth.authenticate_pat(adb, forged) is None
    async with _bearer(forged) as f:
        assert (await f.get("/api/memos")).status_code == 401


# --- lifetime, revocation, rotation ---------------------------------------------------------------


async def test_an_expired_grant_is_refused(pat_client):
    bearer, _, _ = pat_client
    grant = await _mint(bearer)
    _sql(
        "UPDATE pats SET expires_at = :e WHERE id = :i",
        e=datetime.now(UTC) - timedelta(seconds=1),
        i=grant["id"],
    )
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 401


async def test_a_grant_dies_with_an_expired_parent_at_use_time(pat_client):
    bearer, _, token = pat_client
    grant = await _mint(bearer)
    _sql(
        "UPDATE pats SET expires_at = :e WHERE hashed_token = :h",
        e=datetime.now(UTC) - timedelta(seconds=1),
        h=auth.hash_token(token),
    )
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 401


async def test_revoking_the_parent_in_manage_tokens_revokes_the_grant(pat_client, logged_in_client):
    bearer, _, token = pat_client
    session, _ = logged_in_client
    grant = await _mint(bearer)
    parent_id = _sql("SELECT id FROM pats WHERE hashed_token = :h", h=auth.hash_token(token))[0].id
    assert session.delete(f"/api/pats/{parent_id}").status_code == 204
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 401
    assert grant["id"] not in [p["id"] for p in session.get("/api/pats").json()]


async def test_native_logout_of_the_parent_revokes_the_grant(pat_client, logged_in_client):
    bearer, _, _ = pat_client
    session, _ = logged_in_client
    grant = await _mint(bearer)
    assert (await bearer.post("/api/auth/native/logout")).status_code == 204
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 401
    assert grant["id"] not in [p["id"] for p in session.get("/api/pats").json()]


async def test_renewing_the_parent_retires_its_grant(logged_in_client):
    session, _ = logged_in_client
    device = session.post("/api/pats", json={"name": "phone", "expires_in_days": 30}).json()
    async with _bearer(device["token"]) as d:
        grant = await _mint(d)
        assert (await d.post("/api/auth/native/renew")).status_code == 200
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 401


async def test_revoking_the_grant_through_its_own_route(pat_client, logged_in_client):
    bearer, _, _ = pat_client
    session, _ = logged_in_client
    grant = await _mint(bearer)
    assert (await bearer.delete(f"{MINT}/{grant['id']}")).status_code == 204
    async with _bearer(grant["token"]) as g:
        assert (await _start(g)).status_code == 401
    assert grant["id"] not in [p["id"] for p in session.get("/api/pats").json()]
    assert (await bearer.delete(f"{MINT}/{grant['id']}")).status_code == 404


async def test_the_revoke_route_reaches_no_ordinary_token(pat_client, logged_in_client):
    bearer, _, _ = pat_client
    session, _ = logged_in_client
    other = session.post("/api/pats", json={"name": "other device"}).json()
    assert (await bearer.delete(f"{MINT}/{other['id']}")).status_code == 404
    async with _bearer(other["token"]) as o:
        assert (await o.get("/api/memos")).status_code == 200


async def test_the_revoke_route_reaches_no_other_devices_grant(pat_client, logged_in_client):
    bearer, _, _ = pat_client
    session, _ = logged_in_client
    other = session.post("/api/pats", json={"name": "other device"}).json()
    async with _bearer(other["token"]) as o:
        theirs = await _mint(o)
    assert (await bearer.delete(f"{MINT}/{theirs['id']}")).status_code == 404
    async with _bearer(theirs["token"]) as g:
        assert (await _start(g)).status_code == 201


async def test_a_new_mint_rotates_the_previous_grant(pat_client):
    bearer, _, token = pat_client
    first = await _mint(bearer)
    second = await _mint(bearer)
    assert first["token"] != second["token"]
    async with _bearer(first["token"]) as g:
        assert (await _start(g)).status_code == 401
    async with _bearer(second["token"]) as g:
        assert (await _start(g)).status_code == 201
    assert [r.id for r in _grant_rows(token)] == [second["id"]]


async def test_two_devices_each_keep_their_own_grant(pat_client, logged_in_client):
    bearer, _, _ = pat_client
    session, _ = logged_in_client
    other = session.post("/api/pats", json={"name": "other device"}).json()
    mine = await _mint(bearer)
    async with _bearer(other["token"]) as o:
        await _mint(o)
    async with _bearer(mine["token"]) as g:
        assert (await _start(g)).status_code == 201


async def test_concurrent_mints_leave_exactly_one_live_grant(pat_client):
    bearer, _, token = pat_client
    answers = await asyncio.gather(*(bearer.post(MINT) for _ in range(4)))
    assert sorted(a.status_code for a in answers) == [201, 201, 201, 201]
    live = []
    for answer in answers:
        async with _bearer(answer.json()["token"]) as g:
            if (await _start(g)).status_code == 201:
                live.append(answer.json()["id"])
    rows = _grant_rows(token)
    assert len(rows) == 1
    assert live == [rows[0].id]


def test_hash_domains_differ():
    probe = "memos_cap_probe"
    assert auth.hash_capture_grant(probe) != auth.hash_token(probe)
    assert auth.hash_token(probe) == hashlib.sha256(probe.encode()).hexdigest()


def test_capture_grant_lifetime_is_a_day():
    assert settings.CAPTURE_GRANT_TTL_S == 24 * 3600
