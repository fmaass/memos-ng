from app.config import settings
from app.routers.assets import media_event_stream
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL


def _seed_attachment(uid, owner_subject, status="ready", mime="image/jpeg"):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        oid = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": owner_subject}
        ).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, owner_id, filename, mime, size, status, storage_path, blurhash) "
                "VALUES (:u,:o,'x.jpg',:m,1,CAST(:st AS attachment_status),'/x',:bh) RETURNING id"
            ),
            {"u": uid, "o": oid, "m": mime, "st": status, "bh": "LKO2"},
        ).scalar_one()
    eng.dispose()
    return aid


def _cleanup(*aids):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


def test_serve_thumb_with_range_etag_lastmodified(logged_in_client, monkeypatch, tmp_path):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid = _seed_attachment("att-thumb", subject)
    try:
        thumb = tmp_path / "assets" / "thumbs" / "att-thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100), (10, 20, 30)).save(thumb, "JPEG")

        r = client.get("/api/attachments/att-thumb/thumb")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert "last-modified" in r.headers and "etag" in r.headers
        assert r.headers.get("accept-ranges") == "bytes"

        ranged = client.get("/api/attachments/att-thumb/thumb", headers={"Range": "bytes=0-9"})
        assert ranged.status_code == 206  # partial content

        assert client.get("/api/attachments/att-thumb/web").status_code == 404  # tier file absent

        meta = client.get("/api/attachments/att-thumb").json()
        assert meta["thumbnailUrl"].endswith("/thumb")
        assert meta["webUrl"].endswith("/web") and meta["blurhash"] == "LKO2"
    finally:
        _cleanup(aid)


def test_other_users_attachment_is_404(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # Seed an attachment owned by a DIFFERENT user.
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject='other_owner'"))
        c.execute(text("INSERT INTO users (subject, role) VALUES ('other_owner','user')"))
    eng.dispose()
    aid = _seed_attachment("att-other", "other_owner")
    try:
        # Seed the actual tier file so the 404 can ONLY come from the owner check, not from a
        # missing file — without this the test would pass even if owner-scoping were removed
        # (a removed check returns 200 and serves another user's file).
        thumb = tmp_path / "assets" / "thumbs" / "att-other.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")
        assert client.get("/api/attachments/att-other/thumb").status_code == 404  # not owner
    finally:
        _cleanup(aid)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject='other_owner'"))
        eng.dispose()


def test_assets_require_auth(client):
    assert client.get("/api/attachments/x/thumb").status_code == 401
    assert client.get("/api/events").status_code == 401


async def test_sse_stream_emits_progress_then_ready():
    states = [
        [{"uid": "a", "status": "processing", "progress_pct": 40}],
        [{"uid": "a", "status": "processing", "progress_pct": 90}],
        [],  # dropped from non-ready → finished
    ]
    calls = {"i": 0}

    async def snap():
        i = min(calls["i"], len(states) - 1)
        calls["i"] += 1
        return states[i]

    events = [e async for e in media_event_stream(snap, interval=0)]
    assert any('"progress_pct": 40' in e for e in events)
    assert any('"status": "ready"' in e for e in events)  # synthetic ready on completion


def test_list_attachments_requires_auth(client):
    assert client.get("/api/attachments").status_code == 401


def test_list_attachments_owner_scoped_and_type_filter(logged_in_client):
    client, subject = logged_in_client
    img = _seed_attachment("la-img", subject, mime="image/jpeg")
    vid = _seed_attachment("la-vid", subject, mime="video/mp4")
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject='other_la'"))
        c.execute(text("INSERT INTO users (subject, role) VALUES ('other_la','user')"))
    eng.dispose()
    other = _seed_attachment("la-other", "other_la", mime="image/png")
    try:
        items = client.get("/api/attachments").json()
        uids = {i["uid"] for i in items}
        assert {"la-img", "la-vid"} <= uids
        assert "la-other" not in uids  # owner-scoped: another user's media is excluded

        imgs = {i["uid"] for i in client.get("/api/attachments?type=image").json()}
        assert imgs == {"la-img"}  # type filter (owner's image only)
        vids = {i["uid"] for i in client.get("/api/attachments?type=video").json()}
        assert vids == {"la-vid"}

        # unlinked media is flagged orphan
        assert all(i["orphan"] for i in items if i["uid"] in ("la-img", "la-vid"))
    finally:
        _cleanup(img, vid, other)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject='other_la'"))
        eng.dispose()
