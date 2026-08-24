import json
from datetime import UTC, datetime

from app.config import settings
from app.main import app
from app.routers.assets import (
    _negotiated_path_sync,
    _ready_meta,
    _user_snapshot,
    media_event_stream,
)
from app.services import uploads as up_svc
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from starlette.datastructures import UploadFile  # the class the multipart parser instantiates

from tests.conftest import SYNC_URL


def _seed_attachment(
    uid,
    owner_subject,
    status="ready",
    mime="image/jpeg",
    width=None,
    height=None,
    captured_at=None,
):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        oid = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": owner_subject}
        ).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, owner_id, filename, mime, size, status, storage_path, blurhash, "
                "width, height, captured_at) "
                "VALUES (:u,:o,'x.jpg',:m,1,CAST(:st AS attachment_status),'/x',:bh,:w,:h,"
                "CAST(:cap AS timestamptz)) "
                "RETURNING id"
            ),
            {
                "u": uid,
                "o": oid,
                "m": mime,
                "st": status,
                "bh": "LKO2",
                "w": width,
                "h": height,
                "cap": captured_at,
            },
        ).scalar_one()
    eng.dispose()
    return aid


def _user_id(subject):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        uid = c.execute(text("SELECT id FROM users WHERE subject=:s"), {"s": subject}).scalar_one()
    eng.dispose()
    return uid


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
        # auth path (no sig): Cache-Control is private (never public), and switching off `public`
        # did NOT break ETag/Last-Modified/Range above.
        auth_cc = r.headers["cache-control"]
        assert auth_cc.startswith("private,") and "public" not in auth_cc

        ranged = client.get("/api/attachments/att-thumb/thumb", headers={"Range": "bytes=0-9"})
        assert ranged.status_code == 206  # partial content

        assert client.get("/api/attachments/att-thumb/web").status_code == 404  # tier file absent

        meta = client.get("/api/attachments/att-thumb").json()
        assert meta["thumbnailUrl"].split("?")[0].endswith("/thumb")
        assert "sig=" in meta["thumbnailUrl"]
        assert meta["webUrl"].split("?")[0].endswith("/web") and meta["blurhash"] == "LKO2"
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


def _stream_states(states):
    """A snapshot callable that walks `states` and then repeats the last one."""
    calls = {"i": 0}

    async def snap():
        i = min(calls["i"], len(states) - 1)
        calls["i"] += 1
        return states[i]

    return snap


async def test_sse_stream_emits_progress_then_ready():
    snap = _stream_states(
        [
            [{"uid": "a", "status": "processing", "progress_pct": 40}],
            [{"uid": "a", "status": "processing", "progress_pct": 90}],
            [],  # dropped from non-ready → finished
        ]
    )

    async def meta(uids):
        return {"a": {"width": 1200, "height": 900, "blurhash": "LKO2"}}

    events = [e async for e in media_event_stream(snap, meta, interval=0)]

    assert any('"progress_pct": 40' in e for e in events)
    ready = [json.loads(e.removeprefix("data: ")) for e in events if '"status": "ready"' in e]
    # The terminal event carries the metadata the worker just wrote: a client that only patched
    # `status` would leave the attachment at NULL dims until the next refetch.
    assert ready == [
        {
            "uid": "a",
            "status": "ready",
            "progress_pct": 100,
            "width": 1200,
            "height": 900,
            "blurhash": "LKO2",
        }
    ]


async def test_sse_ready_event_is_still_emitted_without_metadata():
    """A row whose metadata is unknown (video ffprobe found none, or the row vanished) still
    finishes — the ready transition itself must not depend on the read-back."""
    snap = _stream_states([[{"uid": "b", "status": "pending", "progress_pct": 0}], []])

    async def meta(uids):
        return {}

    events = [e async for e in media_event_stream(snap, meta, interval=0)]

    assert json.loads(events[-1].removeprefix("data: ")) == {
        "uid": "b",
        "status": "ready",
        "progress_pct": 100,
    }


def _seed_job(aid, lane, state, pct, live_pct=None):
    """A media job for an attachment, optionally with a committed progress sample (MEMNG-36)."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, CAST(:l AS job_lane), CAST(:s AS job_state), 0, :p) RETURNING id"
            ),
            {"a": aid, "l": lane, "s": state, "p": pct},
        ).scalar_one()
        if live_pct is not None:
            c.execute(
                text(
                    "INSERT INTO media_job_progress (job_id, progress_pct) VALUES (:j, :p)"
                ),
                {"j": jid, "p": live_pct},
            )
    eng.dispose()
    return jid


def _rm_jobs(*aids):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            c.execute(
                text(
                    "DELETE FROM media_job_progress WHERE job_id IN "
                    "(SELECT id FROM media_jobs WHERE attachment_id=:a)"
                ),
                {"a": aid},
            )
            c.execute(text("DELETE FROM media_jobs WHERE attachment_id=:a"), {"a": aid})
    eng.dispose()


async def test_snapshot_reports_the_running_job_not_the_finished_fast_lane(logged_in_client):
    """A video's fast lane finishes in seconds; its slow lane then transcodes for minutes.

    max() over the attachment's jobs reported the fast lane's 100 for that whole time, so the
    client showed 100% from the first second onwards (MEMNG-36). The snapshot must report the job
    still doing work — here the slow lane's real 37%.
    """
    _, subject = logged_in_client
    aid = _seed_attachment("prog-video", subject, status="processing", mime="video/mp4")
    try:
        _seed_job(aid, "fast", "done", 100)  # poster/thumbs: finished at 100
        _seed_job(aid, "slow", "queued", 0, live_pct=37)  # transcoding, 37% in

        rows = await _user_snapshot(_user_id(subject))

        assert rows == [{"uid": "prog-video", "status": "processing", "progress_pct": 37}]
    finally:
        _rm_jobs(aid)
        _cleanup(aid)


async def test_snapshot_falls_back_to_the_job_row_when_no_sample_exists(logged_in_client):
    """A queued job has published nothing yet: its own stored percentage is the honest answer
    (0), not the finished lane's 100 and not a missing row."""
    _, subject = logged_in_client
    aid = _seed_attachment("prog-queued", subject, status="processing", mime="video/mp4")
    try:
        _seed_job(aid, "fast", "done", 100)
        _seed_job(aid, "slow", "queued", 0)

        rows = await _user_snapshot(_user_id(subject))

        assert rows == [{"uid": "prog-queued", "status": "processing", "progress_pct": 0}]
    finally:
        _rm_jobs(aid)
        _cleanup(aid)


async def test_ready_meta_reads_back_the_owners_rows_only(logged_in_client):
    """Where the terminal event's payload comes from: the attachment row, scoped to its owner."""
    _, subject = logged_in_client
    _mk_user("meta_other")
    mine = _seed_attachment("meta-mine", subject, width=1200, height=900)
    theirs = _seed_attachment("meta-theirs", "meta_other", width=10, height=20)
    try:
        meta = await _ready_meta(_user_id(subject), ["meta-mine", "meta-theirs"])

        assert meta == {"meta-mine": {"width": 1200, "height": 900, "blurhash": "LKO2"}}
    finally:
        _cleanup(mine, theirs)
        _rm("meta_other")


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


def test_upload_print_rejects_oversize_at_receive_time(logged_in_client, monkeypatch, tmp_path):
    # The print master is read in BOUNDED chunks against a running byte count: an oversize export is
    # refused about one chunk past the cap, never after the whole file has been pulled into memory.
    # The pass-through spy on UploadFile.read (a dependency, still calling the real implementation)
    # is what makes "streamed, not buffered" observable rather than assumed.
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)
    body = b"B" * (4 * 1024 * 1024)
    reads: list[int] = []
    real_read = UploadFile.read

    async def counting_read(self, size=-1):
        chunk = await real_read(self, size)
        reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(UploadFile, "read", counting_read)
    aid = _seed_attachment("pr-big", subject, mime="image/jpeg")
    try:
        r = client.post(
            "/api/attachments/pr-big/print", files={"file": ("big.tif", body, "image/tiff")}
        )
        assert r.status_code == 413
        assert reads and max(reads) <= up_svc.STREAM_CHUNK_SIZE  # bounded reads, not one whole body
        assert sum(reads) < len(body)  # stopped at the cap instead of consuming everything
        assert client.get("/api/attachments/pr-big").json()["hasPrint"] is False
        staging = tmp_path / "uploads"
        assert not staging.exists() or list(staging.iterdir()) == []  # no staged bytes left behind
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


# --- HMAC-signed asset URLs: the DB-free serve fast path ---
import time as _time  # noqa: E402

from app.services.asset_urls import sign_asset_url  # noqa: E402


def test_signed_thumb_serves_without_auth_or_db(client, monkeypatch, tmp_path):
    # The load-bearing guard: a signed derived-tier URL serves with NO auth and NO DB session.
    # If the byte path ever takes a DB session again (the missing-thumbnails bug), this goes red.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "ASSET_URL_SECRET", "test-secret")
    thumb = tmp_path / "assets" / "thumbs" / "sig1.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), (1, 2, 3)).save(thumb, "JPEG")

    from app.routers import assets as assets_mod

    def _boom(*a, **k):
        raise AssertionError("signed serve must not open a DB session")

    monkeypatch.setattr(assets_mod, "async_session", _boom)

    url = sign_asset_url("sig1", "thumb", 7, now=_time.time())
    r = client.get(url)  # `client` is unauthenticated (no session cookie)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    # Scoped media must be `private` (never `public`) and bounded to the signature's remaining
    # life, so a shared cache can't replay it and a revoked tier can't outlive exp in cache.
    cc = r.headers["cache-control"]
    assert cc.startswith("private,")
    assert "immutable" in cc and "public" not in cc
    max_age = int(cc.split("max-age=")[1].split(",")[0])
    assert 0 < max_age <= settings.ASSET_URL_TTL


def test_signed_serve_rejects_tampered_and_expired(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "ASSET_URL_SECRET", "test-secret")
    thumb = tmp_path / "assets" / "thumbs" / "sig2.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), (1, 2, 3)).save(thumb, "JPEG")

    good = sign_asset_url("sig2", "thumb", 7, now=_time.time())
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert client.get(tampered).status_code == 403  # bad signature
    expired = sign_asset_url("sig2", "thumb", 7, now=1000)  # exp long in the past
    assert client.get(expired).status_code == 403


def test_unsigned_original_still_requires_auth(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # original is unsigned (low fan-out, needs the DB row) → the auth path; no session → 401.
    assert client.get("/api/attachments/whatever/original").status_code == 401


def test_pat_can_download_original(logged_in_client, monkeypatch, tmp_path):
    # A headless API client (e.g. the slideshow generator) with a PAT can fetch the original
    # tier — the auth path now accepts PAT-or-session (require_pat_or_user).
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    pat = client.post("/api/pats", json={"name": "slideshow"}).json()["token"]
    aid = _seed_attachment("att-pat-orig", subject)
    try:
        orig = tmp_path / "orig.jpg"
        Image.new("RGB", (40, 40), (9, 9, 9)).save(orig, "JPEG")
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("UPDATE attachments SET storage_path=:p WHERE uid='att-pat-orig'"),
                      {"p": str(orig)})
        eng.dispose()

        anon = TestClient(app, raise_server_exceptions=False)  # no session cookie, PAT only
        r = anon.get("/api/attachments/att-pat-orig/original",
                     headers={"Authorization": f"Bearer {pat}"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        assert anon.get("/api/attachments/att-pat-orig/original").status_code == 401  # no auth
    finally:
        _cleanup(aid)


def test_pat_can_read_attachment_meta(logged_in_client):
    # Same headless client, same attachment, one level less sensitive: the metadata route accepts
    # the PAT too (require_pat_or_user), so a generator that already downloads the bytes can read
    # the capture timestamp instead of guessing it from upload time.
    client, subject = logged_in_client
    pat = client.post("/api/pats", json={"name": "slideshow"}).json()["token"]
    aid = _seed_attachment("att-pat-meta", subject, captured_at="2026-03-04T05:06:07+00:00")
    try:
        anon = TestClient(app, raise_server_exceptions=False)  # no session cookie, PAT only
        r = anon.get("/api/attachments/att-pat-meta", headers={"Authorization": f"Bearer {pat}"})
        assert r.status_code == 200
        body = r.json()
        assert body["uid"] == "att-pat-meta"
        # the field this widening exists for — present AND carrying the seeded instant
        assert "captured_at" in body
        assert datetime.fromisoformat(body["captured_at"]) == datetime(
            2026, 3, 4, 5, 6, 7, tzinfo=UTC
        )
    finally:
        _cleanup(aid)


def test_attachment_meta_without_auth_is_401(logged_in_client):
    # Widening to PAT-or-session must not open the route to no credential at all.
    client, subject = logged_in_client
    aid = _seed_attachment("att-meta-anon", subject)
    try:
        anon = TestClient(app, raise_server_exceptions=False)
        assert anon.get("/api/attachments/att-meta-anon").status_code == 401  # no cookie, no PAT
        # a Bearer token that is not a valid PAT is no better than none
        bad = anon.get(
            "/api/attachments/att-meta-anon",
            headers={"Authorization": "Bearer memos_pat_nope"},
        )
        assert bad.status_code == 401
    finally:
        _cleanup(aid)


def test_attachment_meta_response_fields_unchanged(logged_in_client):
    # The auth change is auth-only: the AttachmentMeta payload keeps exactly the fields it had
    # (pinned here so neither a dropped nor an added field can ride along unnoticed).
    client, subject = logged_in_client
    aid = _seed_attachment("att-meta-shape", subject, captured_at="2026-03-04T05:06:07+00:00")
    try:
        body = client.get("/api/attachments/att-meta-shape").json()
        assert set(body) == {
            "uid",
            "mime",
            "size",
            "status",
            "width",
            "height",
            "duration",
            "blurhash",
            "captured_at",
            "mine",
            "hasPrint",
            "hasLegacyPrint",
            "hasPoster",  # added deliberately with the video poster override
            "renditions",
            "downloadUrl",
            "printUrl",
            "thumbnailUrl",
            "previewUrl",
            "webUrl",
            "posterUrl",
        }
        # and a PAT caller is served the identical field set, not a reduced projection
        pat = client.post("/api/pats", json={"name": "slideshow-shape"}).json()["token"]
        anon = TestClient(app, raise_server_exceptions=False)
        pat_body = anon.get(
            "/api/attachments/att-meta-shape", headers={"Authorization": f"Bearer {pat}"}
        ).json()
        assert set(pat_body) == set(body)
    finally:
        _cleanup(aid)


# --- _negotiated_path_sync: the webp/original choice + existence check that MEMNG-42 batched into
# --- one off-loop hop. Tested directly as a pure function (project convention), because the
# --- refactor moved the 404 decision out of _serve_file's control flow and into this helper.


def test_negotiated_path_prefers_an_existing_webp(tmp_path):
    jpeg, webp = tmp_path / "a.jpg", tmp_path / "a.webp"
    jpeg.write_bytes(b"j")
    webp.write_bytes(b"w")
    assert _negotiated_path_sync(jpeg, "image/jpeg", True) == (webp, "image/webp")


def test_negotiated_path_falls_back_to_jpeg_when_webp_absent(tmp_path):
    jpeg = tmp_path / "a.jpg"
    jpeg.write_bytes(b"j")
    assert _negotiated_path_sync(jpeg, "image/jpeg", True) == (jpeg, "image/jpeg")


def test_negotiated_path_ignores_webp_when_not_negotiated(tmp_path):
    jpeg, webp = tmp_path / "a.jpg", tmp_path / "a.webp"
    jpeg.write_bytes(b"j")
    webp.write_bytes(b"w")
    assert _negotiated_path_sync(jpeg, "image/jpeg", False) == (jpeg, "image/jpeg")


def test_negotiated_path_is_none_when_nothing_exists(tmp_path):
    """None is what _serve_file turns into its 404 'Asset not generated yet'."""
    missing = tmp_path / "gone.jpg"
    assert _negotiated_path_sync(missing, "image/jpeg", False) is None
    assert _negotiated_path_sync(missing, "image/jpeg", True) is None


def test_negotiated_path_404s_when_only_the_webp_is_missing_and_original_gone(tmp_path):
    """A webp-negotiating client still 404s if neither tier was generated."""
    only_webp = tmp_path / "b.webp"
    only_webp.write_bytes(b"w")
    assert _negotiated_path_sync(tmp_path / "b.jpg", "image/jpeg", True) == (
        only_webp,
        "image/webp",
    )
    assert _negotiated_path_sync(tmp_path / "c.jpg", "image/jpeg", True) is None
