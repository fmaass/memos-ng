import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
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


async def _chunks(parts):
    for p in parts:
        yield p


async def test_spool_stream_accumulates_received_bytes(monkeypatch, tmp_path):
    # The received count is the RUNNING SUM over the transport's chunks — that accumulation is what
    # the size ceiling is checked against.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    spool = tmp_path / "uploads" / "acc.spool"
    chunks = [bytes([i]) * 1000 for i in range(5)]
    assert await svc.spool_stream(_chunks(chunks), spool, start=0, ceiling=10_000) == 5000
    assert spool.read_bytes() == b"".join(chunks)


async def test_append_accumulates_bytes_received_across_appends(adb, monkeypatch, tmp_path):
    # bytes_received accumulates both WITHIN an append (over its chunks) and ACROSS appends, and
    # the part file is the concatenation in order — nothing overwritten, nothing zero-filled.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    a, b = b"A" * 1000, b"B" * 700
    up = await svc.create_upload(
        adb, filename="acc.bin", mime="application/octet-stream", size=1700
    )
    await svc.append_stream(adb, up, 0, _chunks([a[:400], a[400:]]))
    assert up.bytes_received == 1000
    await svc.append_stream(adb, up, 1000, _chunks([b]))
    assert up.bytes_received == 1700
    assert svc.part_path(up.upload_uid).read_bytes() == a + b


async def test_append_rejects_accumulated_total_and_keeps_committed_bytes(
    adb, monkeypatch, tmp_path
):
    # Every individual chunk is far under the ceiling — only the accumulated total crosses it, so a
    # per-chunk check would let this through. The already-committed prefix must survive untouched.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    up = await svc.create_upload(adb, filename="c.bin", mime="application/octet-stream", size=200)
    await svc.append_stream(adb, up, 0, _chunks([b"P" * 100]))
    with pytest.raises(svc.UploadTooLarge):
        await svc.append_stream(adb, up, 100, _chunks([b"x" * 50] * 20))
    assert up.bytes_received == 100
    assert svc.part_path(up.upload_uid).read_bytes() == b"P" * 100  # committed prefix intact
    assert list((tmp_path / "uploads").glob("*.spool")) == []  # the failed request's spool is gone


async def test_append_stops_pulling_the_body_once_the_ceiling_is_passed(adb, monkeypatch, tmp_path):
    # Receive-TIME enforcement: the body generator is abandoned the moment the running total passes
    # the ceiling, so an oversize upload is never pulled through in full (let alone buffered).
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    up = await svc.create_upload(adb, filename="s.bin", mime="application/octet-stream", size=500)
    pulled = 0

    async def body():
        nonlocal pulled
        for _ in range(100):  # 10 000 bytes on offer; only the first 500 may be accepted
            pulled += 1
            yield b"z" * 100

    with pytest.raises(svc.UploadTooLarge):
        await svc.append_stream(adb, up, 0, body())
    assert pulled == 6  # 5 chunks fill the ceiling, the 6th trips it, the other 94 are never read
    assert up.bytes_received == 0


async def test_append_rejects_body_that_overshoots_once_landed_at_its_offset(
    adb, monkeypatch, tmp_path
):
    # A body can fit the session's ceiling on its own yet overshoot once landed at its offset. That
    # comparison needs the SETTLED offset, so it is taken at merge time rather than while streaming.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    up = await svc.create_upload(adb, filename="o.bin", mime="application/octet-stream", size=800)
    await svc.append_stream(adb, up, 0, _chunks([b"P" * 700]))
    with pytest.raises(svc.UploadTooLarge):  # 300 alone fits 800; landed at 700 it does not
        await svc.append_stream(adb, up, 700, _chunks([b"x" * 300]))
    assert up.bytes_received == 700
    assert svc.part_path(up.upload_uid).read_bytes() == b"P" * 700
    assert list((tmp_path / "uploads").glob("*.spool")) == []


async def test_failed_append_cannot_destroy_a_concurrent_appends_bytes(adb, monkeypatch, tmp_path):
    # Two appends start from the same offset. A completes and advances the session; B — still
    # holding the now-stale offset — then fails. B may discard only its OWN spool: A's bytes must
    # be byte-for-byte intact with no zero-filled region, and B's stale offset must resolve as a
    # replay no-op against the REFRESHED offset rather than overwriting A.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    up = await svc.create_upload(
        adb, filename="race.bin", mime="application/octet-stream", size=900
    )
    b_in_flight, a_committed = asyncio.Event(), asyncio.Event()

    async def b_body():
        yield b"B" * 100  # B is mid-stream, built on offset 0 just like A
        b_in_flight.set()
        await a_committed.wait()  # ... A finishes and advances the session here
        yield b"B" * 900  # B then blows the ceiling and unwinds

    b_task = asyncio.create_task(svc.append_stream(adb, up, 0, b_body()))
    await b_in_flight.wait()
    await svc.append_stream(adb, up, 0, _chunks([b"A" * 500]))
    assert up.bytes_received == 500
    a_committed.set()
    with pytest.raises(svc.UploadTooLarge):
        await b_task

    part = svc.part_path(up.upload_uid)
    assert part.read_bytes() == b"A" * 500  # not truncated, not overwritten, no zero region
    assert up.bytes_received == 500
    assert list((tmp_path / "uploads").glob("*.spool")) == []

    # a later append still carrying the stale offset is a replay no-op, not a second write at 0
    await svc.append_stream(adb, up, 0, _chunks([b"C" * 500]))
    assert up.bytes_received == 500 and part.read_bytes() == b"A" * 500


def test_append_rejects_body_over_declared_size(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _start(client, "d.bin", "application/octet-stream", 1000)
        over = client.patch(
            f"/api/uploads/{uid}", content=b"x" * 1500, headers={"Upload-Offset": "0"}
        )
        assert over.status_code == 413
        assert client.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == "0"
        assert svc.part_path(uid).stat().st_size == 0  # rejected bytes were never kept
        # the session survives the rejection: a correctly sized body still completes it
        ok = client.patch(
            f"/api/uploads/{uid}", content=b"x" * 1000, headers={"Upload-Offset": "0"}
        )
        assert ok.status_code == 200 and ok.json()["bytes_received"] == 1000
        assert client.post(f"/api/uploads/{uid}/finalize").status_code == 200
    finally:
        _cleanup()


def test_append_rejects_when_cap_drops_below_declared_size(logged_in_client, monkeypatch, tmp_path):
    # MAX_UPLOAD_MIB is enforced at RECEIVE time, not only at session create — a cap that is lower
    # than an already-declared size still stops the bytes at the door.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        size = 3 * 1024 * 1024
        uid = _start(client, "big.bin", "application/octet-stream", size)
        monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)
        over = client.patch(
            f"/api/uploads/{uid}", content=b"x" * size, headers={"Upload-Offset": "0"}
        )
        assert over.status_code == 413
        assert "1 MiB" in over.json()["detail"]  # the server cap, not the declared size, tripped
        assert svc.part_path(uid).stat().st_size == 0
    finally:
        _cleanup()


def test_append_offset_gap_and_replay_still_behave(logged_in_client, monkeypatch, tmp_path):
    # The offset branches are now decided BEFORE the body is received, so re-assert them: a forward
    # gap is a 409 and a replay of an already-received range stays a no-op (not a second append).
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"y" * 400
        uid = _start(client, "g.bin", "application/octet-stream", 800)
        first = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        assert first.json()["bytes_received"] == 400
        gap = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "500"})
        assert gap.status_code == 409
        replay = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        assert replay.status_code == 200 and replay.json()["bytes_received"] == 400
        assert svc.part_path(uid).stat().st_size == 400  # replay wrote nothing a second time
    finally:
        _cleanup()
