"""Phase 1 (native iOS client): a PAT presented as `Authorization: Bearer` authenticates every
`/api/*` route, `/api/pats/*` stays session-only, and an ambiguous credential fails closed.

Every case here drives a client whose ONLY credential is the bearer header (`pat_client`, no
cookie jar) — with the bearer branch removed from `auth.require_user` each one 401s.
"""

import json
from datetime import UTC, datetime

from app import auth
from app.config import settings
from app.main import app
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

BAD_BEARER = {"Authorization": "Bearer memos_pat_definitely_not_a_token"}


def _sql(statement: str, **params):
    """Run one committed statement against the real DB and hand back its rows (none for a write).

    A fresh engine per call, like the rest of the suite: these seeds and read-backs must be visible
    to — and see — what the app's own sessions committed, never a test transaction's private view.
    """
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(statement), params)
            return result.all() if result.returns_rows else []
    finally:
        eng.dispose()


def _seed_attachment(uid: str, subject: str, status: str = "ready", storage_path: str = "/x"):
    rows = _sql(
        "INSERT INTO attachments (uid, owner_id, filename, mime, size, status, storage_path) "
        "SELECT :u, id, 'x.jpg', 'image/jpeg', 1, CAST(:st AS attachment_status), :p "
        "FROM users WHERE subject = :s RETURNING id",
        u=uid,
        st=status,
        p=storage_path,
        s=subject,
    )
    return rows[0][0]


def _drop_attachment(aid: int):
    _sql("DELETE FROM attachments WHERE id = :a", a=aid)


# --- the write and read surfaces a native client needs -------------------------------------


async def test_bearer_drives_the_full_memo_lifecycle(pat_client):
    bearer, _, _ = pat_client
    created = await bearer.post("/api/memos", json={"content": "from the phone"})
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]

    listed = await bearer.get("/api/memos")
    assert listed.status_code == 200
    assert uid in [m["uid"] for m in listed.json()["items"]]

    patched = await bearer.patch(f"/api/memos/{uid}", json={"content": "edited on the phone"})
    assert patched.status_code == 200 and patched.json()["content"] == "edited on the phone"

    assert (await bearer.delete(f"/api/memos/{uid}")).status_code == 204
    assert (await bearer.get(f"/api/memos/{uid}")).status_code == 404


async def test_bearer_can_run_the_chunked_upload_flow(pat_client, monkeypatch, tmp_path):
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    content = b"\x89PNG\r\n\x1a\n" + b"0" * 4000
    start = await bearer.post(
        "/api/uploads",
        json={"filename": "phone.png", "mime": "image/png", "size": len(content)},
    )
    assert start.status_code == 201, start.text
    upload_uid = start.json()["upload_uid"]

    chunk = await bearer.patch(
        f"/api/uploads/{upload_uid}", content=content, headers={"Upload-Offset": "0"}
    )
    assert chunk.status_code == 200 and chunk.json()["bytes_received"] == len(content)

    finalized = await bearer.post(f"/api/uploads/{upload_uid}/finalize")
    assert finalized.status_code == 200, finalized.text
    att_uid = finalized.json()["uid"]
    try:
        assert finalized.json()["size"] == len(content)
    finally:
        _sql(
            "DELETE FROM media_jobs WHERE attachment_id IN "
            "(SELECT id FROM attachments WHERE uid = :u)",
            u=att_uid,
        )
        _sql("DELETE FROM attachments WHERE uid = :u", u=att_uid)


async def test_bearer_reads_and_writes_settings(pat_client):
    bearer, _, _ = pat_client
    assert (await bearer.get("/api/settings")).status_code == 200
    patched = await bearer.patch("/api/settings", json={"theme": "light"})
    assert patched.status_code == 200 and patched.json()["theme"] == "light"
    assert (await bearer.get("/api/settings")).json()["theme"] == "light"


async def test_bearer_reads_an_export_route(pat_client):
    bearer, _, _ = pat_client
    manifest = await bearer.get("/api/exports/photobook")
    assert manifest.status_code == 200, manifest.text
    assert "photos" in manifest.json()


async def test_bearer_opens_the_sse_stream_and_gets_a_real_frame(pat_client):
    """`/api/events` calls `auth.require_user` itself rather than declaring it as a dependency —
    this pins that the bearer reaches that call site too, and that the stream really emits."""
    bearer, subject, _ = pat_client
    aid = _seed_attachment("att-bearer-sse", subject, status="failed")
    try:
        stream = await bearer.get("/api/events")
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        frame = stream.text.split("\n\n")[0]
        assert json.loads(frame.removeprefix("data: "))["uid"] == "att-bearer-sse"
    finally:
        _drop_attachment(aid)


async def test_bearer_downloads_the_original_tier(pat_client, monkeypatch, tmp_path):
    bearer, subject, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    original = tmp_path / "orig.jpg"
    Image.new("RGB", (16, 16), (7, 7, 7)).save(original, "JPEG")
    aid = _seed_attachment("att-bearer-orig", subject, storage_path=str(original))
    try:
        got = await bearer.get("/api/attachments/att-bearer-orig/original")
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("image/")
    finally:
        _drop_attachment(aid)


async def test_auth_user_reports_the_pat_credential(pat_client):
    bearer, subject, _ = pat_client
    me = await bearer.get("/api/auth/user")
    assert me.status_code == 200
    assert me.json()["subject"] == subject and me.json()["auth_kind"] == "pat"


def test_auth_user_reports_the_session_credential(logged_in_client):
    client, subject = logged_in_client
    me = client.get("/api/auth/user")
    assert me.status_code == 200
    assert me.json()["subject"] == subject and me.json()["auth_kind"] == "session"


# --- token management stays out of a token's reach (Decision 5) ------------------------------


async def test_pat_management_refuses_a_bearer(pat_client):
    bearer, _, _ = pat_client
    assert (await bearer.get("/api/pats")).status_code == 401
    assert (await bearer.post("/api/pats", json={"name": "successor"})).status_code == 401
    assert (await bearer.delete("/api/pats/1")).status_code == 401


def test_pat_management_refuses_a_bearer_riding_a_valid_session(logged_in_client):
    """The header is refused BEFORE the cookie is read: a device token alongside a live browser
    session must not reach token management by having its header quietly ignored."""
    client, _ = logged_in_client
    own = client.post("/api/pats", json={"name": "laptop"})
    assert own.status_code == 201  # the same client, same cookie, WITHOUT the header: allowed
    pat_id = own.json()["id"]
    headers = {"Authorization": f"Bearer {own.json()['token']}"}

    assert client.get("/api/pats", headers=headers).status_code == 401
    assert client.post("/api/pats", json={"name": "successor"}, headers=headers).status_code == 401
    assert client.delete(f"/api/pats/{pat_id}", headers=headers).status_code == 401
    assert client.delete(f"/api/pats/{pat_id}").status_code == 204  # cookie alone still works


# --- fail-closed precedence ------------------------------------------------------------------


def test_an_invalid_bearer_beats_a_valid_cookie(logged_in_client):
    client, _ = logged_in_client
    assert client.get("/api/memos").status_code == 200  # the cookie on its own is fine
    assert client.get("/api/memos", headers=BAD_BEARER).status_code == 401


def test_an_expired_pat_is_refused(logged_in_client):
    client, _ = logged_in_client
    token = client.post("/api/pats", json={"name": "expired"}).json()["token"]
    anon = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {token}"}
    assert anon.get("/api/memos", headers=headers).status_code == 200  # valid until it isn't
    _sql(
        "UPDATE pats SET expires_at = now() - interval '1 minute' WHERE hashed_token = :h",
        h=auth.hash_token(token),
    )
    assert anon.get("/api/memos", headers=headers).status_code == 401


def test_an_unknown_authorization_scheme_falls_through_to_the_cookie(logged_in_client):
    """`Basic …` states no bearer claim, so it must neither authenticate nor poison the cookie
    path — the same request is 401 without a session and 200 with one."""
    basic = {"Authorization": "Basic dXNlcjpwYXNz"}
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/api/memos", headers=basic).status_code == 401

    client, _ = logged_in_client
    assert client.get("/api/memos", headers=basic).status_code == 200


async def test_a_bearer_post_without_cookie_or_origin_passes_csrf(pat_client):
    """The CSRF middleware guards ambient cookie authority; a cookieless native client sends no
    Origin at all and must not be answered with a 403."""
    bearer, _, _ = pat_client
    assert "origin" not in {k.lower() for k in bearer.headers}
    assert "cookie" not in {k.lower() for k in bearer.headers}
    assert not bearer.cookies

    created = await bearer.post("/api/memos", json={"content": "no origin, no cookie"})
    assert created.status_code == 201, created.text
    await bearer.delete(f"/api/memos/{created.json()['uid']}")


def test_bearer_carrying_no_credential_is_refused_not_ignored(logged_in_client):
    client, _ = logged_in_client
    assert client.get("/api/memos", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api/memos", headers={"Authorization": "Bearer"}).status_code == 401


def test_expired_pat_is_anonymous_on_the_compat_surface(logged_in_client):
    """The compat surface downgrades an unusable token to anonymous (PUBLIC-only, fixture F1)
    rather than 401ing — expiry must not change that shape, only what counts as usable."""
    client, _ = logged_in_client
    token = client.post("/api/pats", json={"name": "compat"}).json()["token"]
    _sql(
        "UPDATE pats SET expires_at = now() - interval '1 minute' WHERE hashed_token = :h",
        h=auth.hash_token(token),
    )
    anon = TestClient(app, raise_server_exceptions=False)
    assert (
        anon.get("/api/v1/memos", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )


def test_pat_expiry_is_enforced_on_the_boundary_not_before(logged_in_client, monkeypatch):
    """A PAT is valid up to its expiry instant and refused from it on — pinned through
    authenticate_pat, so the same boundary holds on every bearer surface."""
    client, _ = logged_in_client
    token = client.post("/api/pats", json={"name": "boundary", "expires_in_days": 1}).json()[
        "token"
    ]
    anon = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {token}"}
    assert anon.get("/api/memos", headers=headers).status_code == 200

    expires_at = _sql(
        "SELECT expires_at FROM pats WHERE hashed_token = :h", h=auth.hash_token(token)
    )[0][0]
    assert expires_at is not None
    delta = expires_at - datetime.now(UTC)
    assert 23.5 * 3600 < delta.total_seconds() <= 24 * 3600


def test_rejection_logging_never_carries_the_token(logged_in_client):
    """The one place this app logs request headers is the CSRF rejection (app/csrf.py), which
    names origin and referer only. A bearer credential must never reach a log line — logs are
    shipped and kept, and a PAT is a bearer credential: whoever reads the line holds the account.
    """
    client, _ = logged_in_client
    token = client.post("/api/pats", json={"name": "logged"}).json()["token"]
    with capture_logs() as logs:
        rejected = client.post(
            "/api/memos",
            json={"content": "forged"},
            headers={
                "Origin": "https://sibling.discomarder.live",
                "Authorization": f"Bearer {token}",
            },
        )
    assert rejected.status_code == 403
    assert [e for e in logs if e.get("event") == "csrf_rejected"]  # the line really was emitted
    assert not [e for e in logs if token in json.dumps(e, default=str)]
