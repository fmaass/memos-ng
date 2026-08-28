import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from app import auth
from app.models import Pat, User
from app.routers.pats import PatOut
from app.services import pats as svc
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request


def _req(headers: dict) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("test", 0),
            "query_string": b"",
        }
    )


async def _user(adb, subject="alice"):
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def test_create_stores_only_sha256(adb):
    u = await _user(adb)
    pat, token = await svc.create_pat(adb, u, "cli")
    assert token.startswith("memos_pat_")
    # Only the hash is persisted; the plaintext appears nowhere in the row.
    assert pat.hashed_token == hashlib.sha256(token.encode()).hexdigest()
    assert pat.hashed_token != token
    stored = (await adb.execute(select(Pat).where(Pat.id == pat.id))).scalar_one()
    assert token not in (stored.hashed_token, stored.name)


async def test_authenticate_pat_roundtrip_and_unknown(adb):
    u = await _user(adb, "bob")
    _, token = await svc.create_pat(adb, u, "cli")
    got = await auth.authenticate_pat(adb, token)
    assert got is not None and got.id == u.id
    assert await auth.authenticate_pat(adb, "memos_pat_bogus") is None
    # The last_used stamp is scheduled, not awaited (MEMNG-81) — collect it here so it cannot
    # outlive this test's event loop. Its UPDATE matches nothing: the PAT above lives in a
    # transaction that is rolled back.
    await auth.drain_pat_stamp_tasks()


async def test_require_pat_or_user_rejects_unknown_token(adb):
    with pytest.raises(HTTPException) as ei:
        await auth.require_pat_or_user(_req({"Authorization": "Bearer memos_pat_nope"}), adb)
    assert ei.value.status_code == 401


def test_patout_never_exposes_token():
    assert "token" not in PatOut.model_fields


def test_pat_http_create_list_delete(logged_in_client):
    client, _ = logged_in_client
    created = client.post("/api/pats", json={"name": "laptop"})
    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith("memos_pat_") and body["name"] == "laptop"

    listed = client.get("/api/pats").json()
    assert len(listed) == 1 and "token" not in listed[0]  # never leaks the token

    assert client.delete(f"/api/pats/{body['id']}").status_code == 204
    assert client.get("/api/pats").json() == []


def test_pat_create_rejects_empty_name(logged_in_client):
    client, _ = logged_in_client
    assert client.post("/api/pats", json={"name": ""}).status_code == 422  # name min_length=1


def test_pat_create_rejects_an_out_of_range_lifetime(logged_in_client):
    client, _ = logged_in_client
    # 0 is a token that is already dead; 366 is past the one-year cap a lost device is bounded to.
    assert client.post("/api/pats", json={"name": "x", "expires_in_days": 0}).status_code == 422
    assert client.post("/api/pats", json={"name": "x", "expires_in_days": 366}).status_code == 422
    assert client.post("/api/pats", json={"name": "x", "expires_in_days": -1}).status_code == 422


def test_pat_lifetime_becomes_an_absolute_expiry(logged_in_client):
    client, _ = logged_in_client
    before = datetime.now(UTC)
    created = client.post("/api/pats", json={"name": "device", "expires_in_days": 30})
    assert created.status_code == 201
    expires_at = datetime.fromisoformat(created.json()["expires_at"])
    # 30 days from the moment of minting, not a duration the client has to re-derive. The lower
    # bound allows one second because the API publishes second precision (MEMNG-106,
    # app/api_types.py): the stored deadline keeps its microseconds and is what expiry is judged
    # against, while the RENDERED one is floored, so a client can see up to a second less than it
    # was granted — never more, which is the safe direction for a deadline.
    assert (
        timedelta(days=30, seconds=-1) <= expires_at - before <= timedelta(days=30, seconds=60)
    )
    listed = client.get("/api/pats").json()
    assert datetime.fromisoformat(listed[0]["expires_at"]) == expires_at


def test_pat_without_a_lifetime_never_expires(logged_in_client):
    client, _ = logged_in_client
    created = client.post("/api/pats", json={"name": "forever"})
    assert created.status_code == 201 and created.json()["expires_at"] is None
    assert client.get("/api/pats").json()[0]["expires_at"] is None


async def test_authenticate_pat_refuses_an_expired_token(adb):
    u = await _user(adb, "carol")
    _, token = await svc.create_pat(
        adb, u, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert await auth.authenticate_pat(adb, token) is None
    _, live = await svc.create_pat(
        adb, u, "live", expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    got = await auth.authenticate_pat(adb, live)
    assert got is not None and got.id == u.id
    await auth.drain_pat_stamp_tasks()
