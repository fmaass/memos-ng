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


async def test_dedup_is_per_owner(adb, monkeypatch, tmp_path):
    # sha256 dedup must be scoped per-owner: owner B finalizing bytes identical to owner A's
    # must get a DISTINCT attachment, never a reference to A's row (cross-user linkage).
    from app.models import User

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    a = User(subject="dedup_a", role="user")
    b = User(subject="dedup_b", role="user")
    adb.add_all([a, b])
    await adb.flush()
    data = b"shared-bytes" * 500

    async def _finalize(owner_id, name):
        up = await svc.create_upload(
            adb, filename=name, mime="application/octet-stream", size=len(data)
        )
        await svc.append_chunk(adb, up, 0, data)
        return await svc.finalize(adb, up, owner_id=owner_id)

    att_a = await _finalize(a.id, "a.bin")
    att_b = await _finalize(b.id, "b.bin")
    assert att_b.uid != att_a.uid  # B does NOT receive A's attachment
    assert att_a.owner_id == a.id and att_b.owner_id == b.id

    att_a2 = await _finalize(a.id, "a2.bin")
    assert att_a2.uid == att_a.uid  # same owner, same bytes → still dedups to A's row


async def test_finalize_pending_skips_incomplete_and_missing(adb, monkeypatch, tmp_path):
    # finalize-on-post: only fully-received uploads become attachments; incomplete/missing skip.
    from app.models import User

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    u = User(subject="fp_user", role="user")
    adb.add(u)
    await adb.flush()
    data = b"complete-bytes" * 100
    done = await svc.create_upload(adb, filename="a.png", mime="image/png", size=len(data))
    await svc.append_chunk(adb, done, 0, data)
    incomplete = await svc.create_upload(adb, filename="b.png", mime="image/png", size=999999)
    uids = await svc.finalize_pending(
        adb, [done.upload_uid, incomplete.upload_uid, "nonexistent"], owner_id=u.id
    )
    assert len(uids) == 1  # only the complete upload finalized into an attachment


def test_create_memo_finalizes_pending_uploads(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"M" * 4000
        uid = _start(client, "p.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        memo = client.post(
            "/api/memos",
            json={"content": "with a pic", "visibility": "PUBLIC", "upload_uids": [uid]},
        ).json()
        assert len(memo["attachments"]) == 1  # finalized + linked at post time
        assert client.head(f"/api/uploads/{uid}").status_code == 404  # upload consumed
    finally:
        _cleanup()


def test_abandoned_upload_leaves_no_attachment(logged_in_client, monkeypatch, tmp_path):
    # The whole point of finalize-on-post: an un-posted upload never becomes an orphan attachment.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"O" * 4000
        uid = _start(client, "orphan.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        # post a memo WITHOUT referencing the upload (the user abandoned that media)
        client.post("/api/memos", json={"content": "no pics", "visibility": "PUBLIC"})
        assert not client.get("/api/attachments").json()  # no attachment was created
        assert client.head(f"/api/uploads/{uid}").status_code == 200  # upload remains
    finally:
        _cleanup()


def test_failed_post_cleans_up_moved_original(logged_in_client, monkeypatch, tmp_path):
    # finalize moves the file to disk before the shared commit; if the post then fails, the
    # compensating cleanup must unlink it (else it leaks with no DB row — the review's MED finding).
    from app.services import memos as memo_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    async def boom(*args, **kwargs):
        raise RuntimeError("post failed after finalize")

    try:
        data = b"\x89PNG\r\n\x1a\n" + b"L" * 4000
        uid = _start(client, "leak.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        monkeypatch.setattr(memo_svc, "create_memo", boom)  # fail AFTER finalize_pending moved it
        # raise_server_exceptions=False → the failure surfaces as a 500 (the finally still runs).
        assert client.post(
            "/api/memos", json={"content": "x", "visibility": "PUBLIC", "upload_uids": [uid]}
        ).status_code == 500
        originals = tmp_path / "originals"
        leaked = [p for p in originals.rglob("*") if p.is_file()] if originals.exists() else []
        assert leaked == []  # moved original was unlinked on the failed post
    finally:
        _cleanup()


def _make_heic(path):
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    Image.new("RGB", (96, 72), (200, 120, 40)).save(str(path), format="HEIF")


def test_detect_heif_mime_per_category():
    # The classification heuristic that fixes the HEIC-as-MP4 trap — one case per input category.
    from app.services.uploads import detect_heif_mime

    def ftyp(major, *compat):
        body = b"ftyp" + major + b"\x00\x00\x00\x00" + b"".join(compat)
        return (4 + len(body)).to_bytes(4, "big") + body

    assert detect_heif_mime(ftyp(b"heic", b"mif1")) == "image/heic"  # iPhone .heic
    assert detect_heif_mime(ftyp(b"heix")) == "image/heic"
    assert detect_heif_mime(ftyp(b"mif1")) == "image/heic"  # generic HEIF
    assert detect_heif_mime(ftyp(b"avif", b"mif1")) == "image/avif"  # AVIF
    assert detect_heif_mime(ftyp(b"isom", b"mp41")) is None  # MP4 video
    assert detect_heif_mime(ftyp(b"qt  ")) is None  # MOV video
    assert detect_heif_mime(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x00\x00") is None  # JPEG
    assert detect_heif_mime(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d") is None  # PNG
    assert detect_heif_mime(b"\x00\x00\x00\x0cfty") is None  # truncated / not ftyp
    assert detect_heif_mime(b"ab") is None  # too short


def test_sniff_mime_corrects_heic_misdetected_as_video(tmp_path):
    import puremagic
    from app.services.uploads import _sniff_mime

    p = tmp_path / "photo.heic"
    _make_heic(p)
    # baseline: the content sniffer alone mislabels HEIC as video (the bug we're guarding against)
    assert puremagic.from_file(str(p), mime=True).startswith("video/")
    # _sniff_mime corrects it via the ISOBMFF brand → an image, even if the client declared wrong
    assert _sniff_mime(p, "video/mp4") == "image/heic"


async def test_heic_finalize_routes_to_slow_lane(adb, monkeypatch, tmp_path):
    # A HEIC upload must be classified as an image (not video) and routed to the slow lane so its
    # heavy decode runs on the Merkur worker.
    from app.models import AttachmentStatus, JobLane, MediaJob, User
    from sqlalchemy import select

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    u = User(subject="heic_user", role="user")
    adb.add(u)
    await adb.flush()
    src = tmp_path / "src.heic"
    _make_heic(src)
    data = src.read_bytes()
    up = await svc.create_upload(adb, filename="photo.heic", mime="image/heic", size=len(data))
    await svc.append_chunk(adb, up, 0, data)
    att = await svc.finalize(adb, up, owner_id=u.id)

    assert att.mime == "image/heic"  # classified as image, not video/mp4
    assert att.status is AttachmentStatus.processing  # 'pending' is only for video
    job = (
        await adb.execute(select(MediaJob).where(MediaJob.attachment_id == att.id))
    ).scalar_one()
    assert job.lane is JobLane.slow  # heavy decode offloaded to Merkur


def test_edit_memo_attaches_new_upload(logged_in_client, monkeypatch, tmp_path):
    # Editing a memo can add a new image: upload it, then PATCH the memo with its upload_uid.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = client.post(
            "/api/memos", json={"content": "no pics yet", "visibility": "PUBLIC"}
        ).json()
        assert len(memo["attachments"]) == 0
        data = b"\x89PNG\r\n\x1a\n" + b"E" * 4000
        uid = _start(client, "added.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        edited = client.patch(
            f"/api/memos/{memo['uid']}", json={"upload_uids": [uid]}
        ).json()
        assert len(edited["attachments"]) == 1  # new image attached on edit
        assert client.head(f"/api/uploads/{uid}").status_code == 404  # upload consumed
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


def test_upload_rejects_oversize(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)  # 1 MiB cap for the test
    over = client.post(
        "/api/uploads", json={"filename": "big.mov", "mime": "video/mp4", "size": 2 * 1024 * 1024}
    )
    assert over.status_code == 413  # exceeds the cap
    ok = client.post(
        "/api/uploads", json={"filename": "ok.jpg", "mime": "image/jpeg", "size": 500 * 1024}
    )
    assert ok.status_code == 201  # within the cap
