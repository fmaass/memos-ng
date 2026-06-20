import hashlib
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models import Upload
from app.services import uploads as svc
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


def _start(client, name, mime, size, mtime=1609459200000):
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": size, "last_modified": mtime},
    )
    assert r.status_code == 201
    return r.json()["upload_uid"]


_LANES_SQL = (
    "SELECT lane FROM media_jobs mj JOIN attachments a ON a.id = mj.attachment_id WHERE a.uid = :u"
)


def _lanes_for(uid):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        lanes = c.execute(text(_LANES_SQL), {"u": uid}).scalars().all()
    eng.dispose()
    return lanes


def test_resumable_upload_resumes_and_finalizes(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        content = bytes(range(256)) * 400  # 102400 bytes
        c1, c2 = content[:60000], content[60000:]
        uid = _start(client, "v.mov", "video/quicktime", len(content))

        assert (
            client.patch(f"/api/uploads/{uid}", content=c1, headers={"Upload-Offset": "0"}).json()[
                "bytes_received"
            ]
            == 60000
        )
        # interruption → client re-syncs via HEAD, then continues from the offset
        assert client.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == "60000"
        assert client.patch(
            f"/api/uploads/{uid}", content=c2, headers={"Upload-Offset": "60000"}
        ).json()["bytes_received"] == len(content)

        fin = client.post(f"/api/uploads/{uid}/finalize")
        assert fin.status_code == 200
        att = fin.json()
        assert att["size"] == len(content) and att["status"] == "pending"  # video → pending

        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            sha = c.execute(
                text("SELECT sha256 FROM attachments WHERE uid=:u"), {"u": att["uid"]}
            ).scalar_one()
        eng.dispose()
        assert sha == hashlib.sha256(content).hexdigest()  # reassembled bytes intact
        # Video enqueues ONLY the fast lane at finalize; the fast lane enqueues the slow
        # (transcode) job when it completes, so fast→slow can't race a status overwrite.
        assert _lanes_for(att["uid"]) == ["fast"]
    finally:
        _cleanup()


def test_image_finalize_is_processing_fast_lane_only(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 5000
        uid = _start(client, "p.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        att = client.post(f"/api/uploads/{uid}/finalize").json()
        assert att["status"] == "processing"  # image → processing
        assert _lanes_for(att["uid"]) == ["fast"]  # image → fast lane only
    finally:
        _cleanup()


def test_dedup_same_content_returns_existing(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"duplicate-me" * 1000
        uid1 = _start(client, "a.bin", "application/octet-stream", len(data))
        client.patch(f"/api/uploads/{uid1}", content=data, headers={"Upload-Offset": "0"})
        first = client.post(f"/api/uploads/{uid1}/finalize").json()

        uid2 = _start(client, "b.bin", "application/octet-stream", len(data))
        client.patch(f"/api/uploads/{uid2}", content=data, headers={"Upload-Offset": "0"})
        second = client.post(f"/api/uploads/{uid2}/finalize").json()

        assert second["uid"] == first["uid"]  # same sha256 → same attachment (dedup)
    finally:
        _cleanup()


def test_uploads_require_auth(client):
    assert (
        client.post("/api/uploads", json={"filename": "x", "mime": "x", "size": 1}).status_code
        == 401
    )


async def test_reaper_deletes_expired_upload_and_part(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    part = svc.part_path("expired-uid")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"orphan")
    adb.add(
        Upload(
            upload_uid="expired-uid",
            filename="x",
            mime="x",
            declared_size=6,
            bytes_received=6,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await adb.flush()
    n = await svc.reap_expired(adb)
    assert n == 1 and not part.exists()
