import hashlib

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
