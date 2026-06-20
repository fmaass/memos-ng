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


def _seed_memo_with_attachment(memo_uid, att_uid, owner_subject, visibility, mime="image/jpeg"):
    """A memo (owner, visibility) + an attachment linked to it (same owner), thumb file separate."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        oid = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": owner_subject}
        ).scalar_one()
        mid = c.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, "
                "updated_at) VALUES (:u,:o,'m',CAST(:v AS visibility),false,now(),now()) "
                "RETURNING id"
            ),
            {"u": memo_uid, "o": oid, "v": visibility},
        ).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
                "storage_path, blurhash) VALUES (:u,:o,:m,'x.jpg',:mime,1,"
                "CAST('ready' AS attachment_status),'/x','LKO2') RETURNING id"
            ),
            {"u": att_uid, "o": oid, "m": mid, "mime": mime},
        ).scalar_one()
    eng.dispose()
    return mid, aid


def _mk_user(subject):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
        c.execute(text("INSERT INTO users (subject, role) VALUES (:s,'user')"), {"s": subject})
    eng.dispose()


def _rm(subject, *mids):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for mid in mids:
            c.execute(text("DELETE FROM memos WHERE id=:m"), {"m": mid})
        c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
    eng.dispose()


def test_shared_memo_media_viewable_by_other_user(logged_in_client, monkeypatch, tmp_path):
    # Household-sharing fix: a memo you can SEE (non-PRIVATE) → you can fetch its media even
    # though ANOTHER user owns the attachment. Regression for "shared images don't load".
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _mk_user("sharer")
    mid, aid = _seed_memo_with_attachment("shm-pub", "sha-pub", "sharer", "PUBLIC")
    try:
        thumb = tmp_path / "assets" / "thumbs" / "sha-pub.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")  # file present → 404 is the check
        assert client.get("/api/attachments/sha-pub/thumb").status_code == 200  # was 404 pre-fix
        assert client.get("/api/attachments/sha-pub").status_code == 200  # meta too
    finally:
        _cleanup(aid)
        _rm("sharer", mid)


def test_private_memo_media_stays_404_for_other_user(logged_in_client, monkeypatch, tmp_path):
    # The fix must NOT over-grant: another user's PRIVATE-memo media stays hidden even with the
    # tier file present (so the 404 is the visibility check, not a missing file).
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _mk_user("sharer2")
    mid, aid = _seed_memo_with_attachment("shm-priv", "sha-priv", "sharer2", "PRIVATE")
    try:
        thumb = tmp_path / "assets" / "thumbs" / "sha-priv.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")
        assert client.get("/api/attachments/sha-priv/thumb").status_code == 404  # PRIVATE+not owner
        assert client.get("/api/attachments/sha-priv").status_code == 404
    finally:
        _cleanup(aid)
        _rm("sharer2", mid)


def test_list_attachments_includes_shared_excludes_private(logged_in_client):
    # The library shows the household-viewable set: own + media on non-PRIVATE memos; another
    # user's PRIVATE-memo media and orphans stay out.
    client, _ = logged_in_client
    _mk_user("libsharer")
    pub_m, pub_a = _seed_memo_with_attachment("lib-pubm", "lib-pub", "libsharer", "PUBLIC")
    priv_m, priv_a = _seed_memo_with_attachment("lib-privm", "lib-priv", "libsharer", "PRIVATE")
    orphan = _seed_attachment("lib-orph", "libsharer")  # another user's orphan
    try:
        uids = {i["uid"] for i in client.get("/api/attachments").json()}
        assert "lib-pub" in uids  # shared (PUBLIC memo) media now in the library
        assert "lib-priv" not in uids  # another user's PRIVATE media excluded
        assert "lib-orph" not in uids  # another user's orphan excluded
    finally:
        _cleanup(pub_a, priv_a, orphan)
        _rm("libsharer", pub_m, priv_m)


def test_delete_own_attachment_removes_row_and_file(logged_in_client, monkeypatch, tmp_path):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid = _seed_attachment("del-me", subject)
    try:
        thumb = tmp_path / "assets" / "thumbs" / "del-me.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")
        assert client.delete("/api/attachments/del-me").status_code == 204
        assert client.get("/api/attachments/del-me").status_code == 404  # gone from DB
        assert not thumb.exists()  # tier file removed
    finally:
        _cleanup(aid)  # no-op if already deleted


def test_cannot_delete_another_users_shared_attachment(logged_in_client, monkeypatch, tmp_path):
    # delete is OWNER-only — even a shared (PUBLIC-memo) attachment you can VIEW is not deletable.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _mk_user("delsharer")
    mid, aid = _seed_memo_with_attachment("delm", "del-shared", "delsharer", "PUBLIC")
    try:
        assert client.delete("/api/attachments/del-shared").status_code == 404  # not owner
        assert client.get("/api/attachments/del-shared").status_code == 200  # still viewable
    finally:
        _cleanup(aid)
        _rm("delsharer", mid)


def test_upload_print_master_then_serve_and_flag(logged_in_client, monkeypatch, tmp_path):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid = _seed_attachment("pr-img", subject, mime="image/jpeg")
    try:
        assert client.get("/api/attachments/pr-img").json()["hasPrint"] is False
        r = client.post(
            "/api/attachments/pr-img/print",
            files={"file": ("p.tif", b"PRINTMASTERBYTES", "image/tiff")},
        )
        assert r.status_code == 200 and r.json()["hasPrint"] is True
        served = client.get("/api/attachments/pr-img/print")
        assert served.status_code == 200 and served.content == b"PRINTMASTERBYTES"
    finally:
        _cleanup(aid)


def test_upload_print_is_owner_only(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _mk_user("prsharer")
    mid, aid = _seed_memo_with_attachment("prm", "pr-shared", "prsharer", "PUBLIC")
    try:
        r = client.post(
            "/api/attachments/pr-shared/print",
            files={"file": ("p.tif", b"X", "image/tiff")},
        )
        assert r.status_code == 404  # can view the shared image, cannot attach a print master
    finally:
        _cleanup(aid)
        _rm("prsharer", mid)


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
