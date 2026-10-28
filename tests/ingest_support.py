"""Fixtures and helpers for the ingest-role suites (trip pass P2).

The two ingest apps are built with `create_app(role)` and driven over ASGI, against the same test
database as the rest of the suite; every case starts from empty pass/ingest tables. Pass state is
seeded the way home seeds it — through `PUT /ingest/passes` on the ingest-home app — so the push
path is exercised by every case that needs a pass.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import settings
from app.ingest import core
from app.ingest.factory import create_app
from httpx import ASGITransport, AsyncClient

from tests.test_attachments_taken import _sql

SERVICE_TOKEN = "t" * 86
INGEST_TABLES = (
    "ingest_refusals",
    "pass_sync_state",
    "pass_events",
    "ingest_blobs",
    "ingest_batches",
    "guest_uploads",
    "pass_devices",
    "passes",
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(256)) * 4
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 1000
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 500


def wipe() -> None:
    for table in INGEST_TABLES:
        _sql(f"DELETE FROM {table}")


@pytest.fixture
def ingest_env(db_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_DATA_ROOT", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "INGEST_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(settings, "INGEST_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", False)
    core.redeem_window.reset()
    wipe()
    try:
        yield tmp_path / "ingest"
    finally:
        core.redeem_window.reset()
        wipe()


@pytest_asyncio.fixture
async def door(ingest_env):
    async with AsyncClient(
        transport=ASGITransport(app=create_app("ingest")), base_url="http://door"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def home_iface(ingest_env):
    async with AsyncClient(
        transport=ASGITransport(app=create_app("ingest-home")),
        base_url="http://ingest-home",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    ) as client:
        yield client


def pass_state(uid: str, secret: str, **overrides) -> dict:
    state = {
        "uid": uid,
        "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
        "label": "Sardinia friends",
        "scope": "contribute",
        "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        "max_devices": 30,
        "byte_cap": 50 * 1024**3,
        "version": 1,
        "revoked_at": None,
        "revoked_version": None,
    }
    state.update(overrides)
    return state


async def push(home_iface: AsyncClient, *states: dict) -> list[dict]:
    r = await home_iface.put("/ingest/passes", json={"passes": list(states)})
    assert r.status_code == 200, r.text
    return r.json()


async def seeded_pass(home_iface: AsyncClient, **overrides) -> tuple[str, str]:
    uid, secret = secrets.token_urlsafe(8), secrets.token_urlsafe(32)
    await push(home_iface, pass_state(uid, secret, **overrides))
    return uid, secret


async def redeem(door: AsyncClient, secret: str, name: str = "Anna", **headers) -> dict:
    r = await door.post(
        "/api/pass/redeem",
        json={"secret": secret, "display_name": name, "platform": "web"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def upload(door: AsyncClient, token: str, data: bytes, name: str = "a.jpg", **extra) -> str:
    r = await door.post(
        "/api/guest/uploads",
        json={"filename": name, "mime": "image/jpeg", "size": len(data), **extra},
        headers=bearer(token),
    )
    assert r.status_code == 201, r.text
    uid = r.json()["upload_uid"]
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=data,
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 200, r.text
    return uid


async def complete(door: AsyncClient, token: str, uids: list[str], caption: str | None = None):
    return await door.post(
        "/api/guest/memos", json={"upload_uids": uids, "caption": caption}, headers=bearer(token)
    )
