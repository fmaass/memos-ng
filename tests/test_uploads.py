import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from app import auth
from app.config import settings
from app.csrf import canonical_origin
from app.database import engine as async_engine
from app.main import app
from app.models import Upload
from app.services import uploads as svc
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, select, text

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


def _login_as(monkeypatch, subject):
    """A SECOND authenticated client. The conftest fixture is pinned to one subject, and ownership
    is decided from the session's user — a cross-user test needs two real sessions, not two cookies
    for the same one. Safe to call after the victim's session exists: that session resolves from its
    SID cookie, so re-patching the token validator here cannot re-identify it."""
    from unittest.mock import AsyncMock

    from app import auth

    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Origin"] = canonical_origin()  # speak as a browser on this app's own origin
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"preferred_username": subject, "name": "O", "email": "o@x", "groups": []},
    )
    login = client.get("/api/auth/login", follow_redirects=False)
    tx = login.cookies[auth.TX_COOKIE]
    state = auth.decrypt(tx)["state"]
    client.cookies.set(auth.TX_COOKIE, tx)
    cb = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    client.cookies.set(auth.SID_COOKIE, cb.cookies[auth.SID_COOKIE])
    return client


def _drop_user(subject):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
    eng.dispose()


def _reap_expired_committed():
    """Run the worker's committing reap against the real DB from a sync test. Its own event loop is
    safe here: the dev/test engine uses NullPool, so a fresh (loop-bound) connection is taken per
    checkout."""
    return asyncio.run(svc.reap_expired_committed())


def _attachment_owners():
    """uid -> owning subject, read straight from the DB. The authoritative answer to 'whose bytes
    are these' — the library listing also surfaces other users' PUBLIC media, so it cannot decide
    ownership."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(
            text("SELECT a.uid, u.subject FROM attachments a LEFT JOIN users u ON u.id=a.owner_id")
        ).all()
    eng.dispose()
    return {uid: subject for uid, subject in rows}


async def _owner(adb, subject):
    """A real user row for the service-level tests — an upload session is bound to its creator, so
    every create_upload needs an owner that actually exists (owner_id is an FK)."""
    from app.models import User

    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


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

        first = client.patch(f"/api/uploads/{uid}", content=c1, headers={"Upload-Offset": "0"})
        assert first.json()["bytes_received"] == 60000
        # the declared contract: every successful PATCH echoes the cursor as a header too
        assert first.headers["Upload-Offset"] == "60000"
        # interruption → client re-syncs via HEAD, then continues from the offset
        assert client.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == "60000"
        second = client.patch(f"/api/uploads/{uid}", content=c2, headers={"Upload-Offset": "60000"})
        assert second.json()["bytes_received"] == len(content)
        assert second.headers["Upload-Offset"] == str(len(content))

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
            adb, owner_id=owner_id, filename=name, mime="application/octet-stream", size=len(data)
        )
        await svc.append_chunk(adb, up, 0, data)
        return await svc.finalize(adb, up, owner_id=owner_id)

    att_a = await _finalize(a.id, "a.bin")
    att_b = await _finalize(b.id, "b.bin")
    assert att_b.uid != att_a.uid  # B does NOT receive A's attachment
    assert att_a.owner_id == a.id and att_b.owner_id == b.id

    att_a2 = await _finalize(a.id, "a2.bin")
    assert att_a2.uid == att_a.uid  # same owner, same bytes → still dedups to A's row


async def test_finalize_pending_rejects_incomplete_and_missing(adb, monkeypatch, tmp_path):
    # MEMNG-113: a batch naming an upload that is missing or not fully received is refused WHOLE,
    # naming every offender by cause. Silently reducing the batch to its complete members is what
    # turned "I posted my photo after the upload expired" into a 201 for a text-only memo.
    from app.models import User

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    u = User(subject="fp_user", role="user")
    adb.add(u)
    await adb.flush()
    data = b"complete-bytes" * 100
    done = await svc.create_upload(
        adb, owner_id=u.id, filename="a.png", mime="image/png", size=len(data)
    )
    await svc.append_chunk(adb, done, 0, data)
    incomplete = await svc.create_upload(
        adb, owner_id=u.id, filename="b.png", mime="image/png", size=999999
    )
    with pytest.raises(svc.UploadsUnlinkable) as raised:
        await svc.finalize_pending(
            adb, [done.upload_uid, incomplete.upload_uid, "nonexistent"], owner_id=u.id
        )
    assert raised.value.missing == ["nonexistent"]
    assert raised.value.incomplete == [incomplete.upload_uid]
    # the refusal costs the healthy member nothing: its session is untouched, so the caller can
    # resolve the two offenders and re-post the same batch
    assert await adb.get(Upload, done.upload_uid) is not None
    assert len(await svc.finalize_pending(adb, [done.upload_uid], owner_id=u.id)) == 1


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


def test_failed_post_puts_the_moved_original_back_where_it_came_from(
    logged_in_client, monkeypatch, tmp_path
):
    # finalize moves the file to disk before the shared commit; if the post then fails, the DB
    # rollback returns the upload row to pending — so the bytes have to return to the part file
    # that row names. Unlinking them instead leaves the row promising a file that is gone and
    # destroys what may be the client's only copy.
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
        assert (
            client.post(
                "/api/memos", json={"content": "x", "visibility": "PUBLIC", "upload_uids": [uid]}
            ).status_code
            == 500
        )
        originals = tmp_path / "originals"
        stranded = [p for p in originals.rglob("*") if p.is_file()] if originals.exists() else []
        assert stranded == []  # nothing left half-filed under a memo that does not exist
        assert svc.part_path(uid).read_bytes() == data  # ...because it went back, byte for byte

        # and the session really is intact: the very same upload finalizes cleanly on a later post
        monkeypatch.undo()
        monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
        retried = client.post(
            "/api/memos", json={"content": "x", "visibility": "PUBLIC", "upload_uids": [uid]}
        )
        assert retried.status_code == 201 and len(retried.json()["attachments"]) == 1
    finally:
        _cleanup()


def _make_heic(path):
    import pillow_heif
    from PIL import Image

    pillow_heif.from_pillow(Image.new("RGB", (96, 72), (200, 120, 40))).save(str(path))


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
    up = await svc.create_upload(
        adb, owner_id=u.id, filename="photo.heic", mime="image/heic", size=len(data)
    )
    await svc.append_chunk(adb, up, 0, data)
    att = await svc.finalize(adb, up, owner_id=u.id)

    assert att.mime == "image/heic"  # classified as image, not video/mp4
    assert att.status is AttachmentStatus.processing  # 'pending' is only for video
    job = (await adb.execute(select(MediaJob).where(MediaJob.attachment_id == att.id))).scalar_one()
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
        edited = client.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [uid]}).json()
        assert len(edited["attachments"]) == 1  # new image attached on edit
        assert client.head(f"/api/uploads/{uid}").status_code == 404  # upload consumed
    finally:
        _cleanup()


# --- MEMNG-113: a memo write naming an unusable upload is refused, whole ------------------------
# Every case below used to answer 201 with the media silently dropped, which is what let an outbox
# client delete its only copy of a photo the server never stored.


def _memo_uids(client) -> set[str]:
    return {m["uid"] for m in client.get("/api/memos").json()["items"]}


def _expect_unlinkable(response, *, missing=(), incomplete=()):
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "uploads_unlinkable",
        "missing_upload_uids": list(missing),
        "incomplete_upload_uids": list(incomplete),
    }


def test_create_memo_409s_when_an_upload_uid_never_existed(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        before = _memo_uids(client)
        posted = client.post(
            "/api/memos",
            json={"content": "hi", "visibility": "PUBLIC", "upload_uids": ["no-such-uid"]},
        )
        _expect_unlinkable(posted, missing=["no-such-uid"])
        assert _memo_uids(client) == before  # no memo at all — not even a text-only one
    finally:
        _cleanup()


def test_create_memo_409s_when_the_upload_expired_and_was_reaped(
    logged_in_client, monkeypatch, tmp_path
):
    # The ticket's actual field report: everything uploaded fine, the client posted the memo more
    # than UPLOAD_EXPIRY_HOURS later, and the reaper had already taken row and bytes.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"X" * 4000
        uid = _start(client, "late.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(
                text(
                    "UPDATE uploads SET expires_at = now() - interval '1 hour'"
                    " WHERE upload_uid = :u"
                ),
                {"u": uid},
            )
        eng.dispose()
        _reap_expired_committed()
        assert client.head(f"/api/uploads/{uid}").status_code == 404  # premise: it really is gone

        before = _memo_uids(client)
        _expect_unlinkable(
            client.post(
                "/api/memos",
                json={"content": "my only copy", "visibility": "PUBLIC", "upload_uids": [uid]},
            ),
            missing=[uid],
        )
        assert _memo_uids(client) == before
    finally:
        _cleanup()


def test_create_memo_409s_when_the_upload_is_short_of_its_declared_size(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"H" * 4000
        uid = _start(client, "half.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data[:1000], headers={"Upload-Offset": "0"})

        before = _memo_uids(client)
        _expect_unlinkable(
            client.post(
                "/api/memos",
                json={"content": "half a photo", "visibility": "PUBLIC", "upload_uids": [uid]},
            ),
            incomplete=[uid],
        )
        assert _memo_uids(client) == before
        # the session survives the refusal, so the client can resume it from HEAD and post again
        assert client.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == "1000"
    finally:
        _cleanup()


def test_create_memo_names_only_the_offending_uid_and_finalizes_nothing(
    logged_in_client, monkeypatch, tmp_path
):
    # Mixed batch: the refusal must name ONLY what is wrong, and must not half-apply — the healthy
    # upload stays a pending session (not an attachment, not consumed), so the retry can carry it.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"G" * 4000
        good = _start(client, "good.png", "image/png", len(data))
        client.patch(f"/api/uploads/{good}", content=data, headers={"Upload-Offset": "0"})

        before = _memo_uids(client)
        _expect_unlinkable(
            client.post(
                "/api/memos",
                json={"content": "2 pics", "visibility": "PUBLIC", "upload_uids": [good, "ghost"]},
            ),
            missing=["ghost"],
        )
        assert _memo_uids(client) == before
        assert not client.get("/api/attachments").json()  # nothing was finalized
        assert client.head(f"/api/uploads/{good}").status_code == 200  # still pending, not consumed

        retried = client.post(
            "/api/memos", json={"content": "2 pics", "visibility": "PUBLIC", "upload_uids": [good]}
        )
        assert retried.status_code == 201 and len(retried.json()["attachments"]) == 1
    finally:
        _cleanup()


def test_patch_memo_409s_and_leaves_the_memo_exactly_as_it_was(
    logged_in_client, monkeypatch, tmp_path
):
    # Atomicity on the edit path: the content/visibility edit is applied to the session BEFORE the
    # uploads are finalized, so a refusal must take the whole transaction down with it.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = client.post("/api/memos", json={"content": "before", "visibility": "PUBLIC"}).json()
        _expect_unlinkable(
            client.patch(
                f"/api/memos/{memo['uid']}",
                json={"content": "after", "upload_uids": ["ghost-on-edit"]},
            ),
            missing=["ghost-on-edit"],
        )
        after = client.get(f"/api/memos/{memo['uid']}").json()
        assert after["content"] == "before"  # the edit rolled back with the failed link
        assert after["attachments"] == []
    finally:
        _cleanup()


def test_uploads_require_auth(client):
    assert (
        client.post("/api/uploads", json={"filename": "x", "mime": "x", "size": 1}).status_code
        == 401
    )


async def test_the_session_scoped_reap_deletes_the_row_and_leaves_the_part(
    adb, monkeypatch, tmp_path
):
    # The part may go only once the deletion commits (MEMNG-1162), and `adb` never commits: the
    # part's removal is `reap_expired_committed`'s, pinned beside the worker wiring below.
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
    assert n == 1 and part.read_bytes() == b"orphan"
    assert (
        await adb.execute(select(Upload).where(Upload.upload_uid == "expired-uid"))
    ).first() is None


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
    spooled = await svc.spool_stream(_chunks(chunks), spool, start=0, ceiling=10_000)
    assert spooled == svc.Spooled(received=5000, cut=False)
    assert spool.read_bytes() == b"".join(chunks)


async def test_append_accumulates_bytes_received_across_appends(adb, monkeypatch, tmp_path):
    # bytes_received accumulates both WITHIN an append (over its chunks) and ACROSS appends, and
    # the part file is the concatenation in order — nothing overwritten, nothing zero-filled.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    a, b = b"A" * 1000, b"B" * 700
    o = await _owner(adb, "up_acc")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="acc.bin", mime="application/octet-stream", size=1700
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
    o = await _owner(adb, "up_ceil")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="c.bin", mime="application/octet-stream", size=200
    )
    await svc.append_stream(adb, up, 0, _chunks([b"P" * 100]))
    with pytest.raises(svc.UploadTooLarge):
        await svc.append_stream(adb, up, 100, _chunks([b"x" * 50] * 20))
    assert up.bytes_received == 100
    assert svc.part_path(up.upload_uid).read_bytes() == b"P" * 100  # committed prefix intact
    assert list((tmp_path / "uploads").glob("*.spool")) == []  # the failed request's spool is gone


async def test_gap_and_replay_answer_before_consuming_the_body(adb, monkeypatch, tmp_path):
    # Offset precedence outranks the ceiling: a gap answers 409 and a replay answers no-op WITHOUT
    # pulling a single chunk, even when the body offered would blow the session ceiling.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "up_gap")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="p.bin", mime="application/octet-stream", size=800
    )
    await svc.append_stream(adb, up, 0, _chunks([b"P" * 400]))
    pulled = 0

    async def oversize_body():
        nonlocal pulled
        for _ in range(100):  # 10 000 bytes — far past the 800-byte ceiling
            pulled += 1
            yield b"z" * 100

    with pytest.raises(ValueError):  # gap wins over the ceiling: 409, not 413
        await svc.append_stream(adb, up, 500, oversize_body())
    replay = await svc.append_stream(adb, up, 0, oversize_body())
    assert replay is not None and replay.upload is up  # replay wins too: no-op
    assert pulled == 0  # neither request read a chunk
    assert up.bytes_received == 400
    assert svc.part_path(up.upload_uid).read_bytes() == b"P" * 400


def _open_session(uid, hours=1):
    """An upload session row that expires `hours` from now (negative → already expired)."""
    return Upload(
        upload_uid=uid,
        filename="x",
        mime="x",
        declared_size=1,
        bytes_received=0,
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
    )


async def test_reaper_removes_orphaned_spools_but_spares_an_active_one(adb, monkeypatch, tmp_path):
    # A process killed mid-append leaves a .spool that nothing else would ever remove. The reaper
    # drops that debris, and must not pull the file out from under a request still streaming.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    spools = tmp_path / "uploads"
    spools.mkdir(parents=True, exist_ok=True)
    orphan = spools / "dead-uid.abc123.spool"
    orphan.write_bytes(b"half a chunk")
    stale = (datetime.now(UTC) - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS + 1)).timestamp()
    os.utime(orphan, (stale, stale))
    adb.add(_open_session("live-uid"))  # the session the active spool is being streamed into
    await adb.flush()
    active = spools / "live-uid.def456.spool"
    active.write_bytes(b"still streaming")
    part = svc.part_path("live-uid")
    part.write_bytes(b"committed")

    await svc.reap_expired(adb)

    assert not orphan.exists()  # crash debris reaped
    assert active.read_bytes() == b"still streaming"  # in-flight spool untouched
    assert part.read_bytes() == b"committed"  # a live session's committed bytes untouched


async def test_reaper_spares_an_ancient_spool_while_its_session_is_open(adb, monkeypatch, tmp_path):
    # mtime alone cannot tell a stalled upload from crash debris: a slow or wedged connection can
    # leave an in-flight spool untouched for far longer than the expiry window, and unlinking it
    # under its writer fails that request. The OPEN SESSION spares it — however ancient it looks.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    spools = tmp_path / "uploads"
    spools.mkdir(parents=True, exist_ok=True)
    adb.add(_open_session("slow-uid"))
    await adb.flush()
    stalled = spools / "slow-uid.tok123.spool"
    stalled.write_bytes(b"mid-chunk")
    ancient = (datetime.now(UTC) - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS * 10)).timestamp()
    os.utime(stalled, (ancient, ancient))

    await svc.reap_expired(adb)

    assert stalled.read_bytes() == b"mid-chunk"


async def test_reaper_reaps_a_fresh_spool_once_its_session_is_gone(adb, monkeypatch, tmp_path):
    # The other half of the same rule: a spool whose session no longer exists — or expired in this
    # very sweep — can have no writer left, so it is collected on its own merits rather than
    # lingering for another whole window. Both files here were written a moment ago.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    spools = tmp_path / "uploads"
    spools.mkdir(parents=True, exist_ok=True)
    adb.add(_open_session("expiring-uid", hours=-1))  # collected by this same reap
    await adb.flush()
    expired = spools / "expiring-uid.tok456.spool"
    expired.write_bytes(b"debris")
    unknown = spools / "no-such-uid.tok789.spool"  # never had a row at all
    unknown.write_bytes(b"debris")

    await svc.reap_expired(adb)

    assert not expired.exists() and not unknown.exists()


async def test_reaper_ages_out_a_spool_whose_name_names_no_session(adb, monkeypatch, tmp_path):
    # A name that is not '<uid>.<token>.spool' has no session to ask about (an older layout, a
    # stray file), so for those the age horizon still decides — exactly as it did before.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    spools = tmp_path / "uploads"
    spools.mkdir(parents=True, exist_ok=True)
    aged = spools / "nameless.spool"
    aged.write_bytes(b"old debris")
    stale = (datetime.now(UTC) - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS + 1)).timestamp()
    os.utime(aged, (stale, stale))
    fresh = spools / "also.nameless.too.spool"
    fresh.write_bytes(b"fresh debris")

    await svc.reap_expired(adb)

    assert not aged.exists()
    assert fresh.read_bytes() == b"fresh debris"


async def test_append_stops_pulling_the_body_once_the_ceiling_is_passed(adb, monkeypatch, tmp_path):
    # Receive-TIME enforcement: the body generator is abandoned the moment the running total passes
    # the ceiling, so an oversize upload is never pulled through in full (let alone buffered).
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "up_stop")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="s.bin", mime="application/octet-stream", size=500
    )
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
    o = await _owner(adb, "up_over")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="o.bin", mime="application/octet-stream", size=800
    )
    await svc.append_stream(adb, up, 0, _chunks([b"P" * 700]))
    with pytest.raises(svc.UploadTooLarge):  # 300 alone fits 800; landed at 700 it does not
        await svc.append_stream(adb, up, 700, _chunks([b"x" * 300]))
    assert up.bytes_received == 700
    assert svc.part_path(up.upload_uid).read_bytes() == b"P" * 700
    assert list((tmp_path / "uploads").glob("*.spool")) == []


async def test_stale_append_that_streams_successfully_is_a_replay_under_the_lock(
    adb, monkeypatch, tmp_path
):
    # Both appends start at offset 0 and both stream fine; A commits first. B's snapshot passed the
    # early exit while it was still true, so the decision that matters is the one re-taken under the
    # row lock: a replay no-op, NOT a second write at 0 that would overwrite A.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "up_stale")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="both.bin", mime="application/octet-stream", size=900
    )
    b_in_flight, a_committed = asyncio.Event(), asyncio.Event()

    async def b_body():
        yield b"B" * 100
        b_in_flight.set()
        await a_committed.wait()
        yield b"B" * 100  # stays inside the ceiling — B's BODY is fine, only its offset goes stale

    b_task = asyncio.create_task(svc.append_stream(adb, up, 0, b_body()))
    await b_in_flight.wait()
    await svc.append_stream(adb, up, 0, _chunks([b"A" * 500]))
    assert up.bytes_received == 500
    a_committed.set()
    await b_task  # completes without error: a replay is a no-op, not a failure

    assert svc.part_path(up.upload_uid).read_bytes() == b"A" * 500  # B did not overwrite A
    assert up.bytes_received == 500
    assert list((tmp_path / "uploads").glob("*.spool")) == []


async def test_failed_append_cannot_destroy_a_concurrent_appends_bytes(adb, monkeypatch, tmp_path):
    # Two appends start from the same offset. A completes and advances the session; B — still
    # holding the now-stale offset — then fails. B may discard only its OWN spool: A's bytes must
    # be byte-for-byte intact with no zero-filled region, and B's stale offset must resolve as a
    # replay no-op against the REFRESHED offset rather than overwriting A.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "up_race")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="race.bin", mime="application/octet-stream", size=900
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


def _cookie_client(client) -> AsyncClient:
    """An async client on the SAME cookie session as the `logged_in_client` TestClient, so a PATCH
    can stream an async-generator body (TestClient cannot) and no PAT bookkeeping task opens a
    connection of its own. The login flow leaves more than one cookie of that name in the jar; the
    LAST one is the live session (tests/test_lock_protocol_concurrency.py `_async_client`)."""
    sid = [c.value for c in client.cookies.jar if c.name == auth.SID_COOKIE][-1]
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": canonical_origin()},
        cookies={auth.SID_COOKIE: sid or ""},
    )


async def test_append_holds_no_connection_while_its_body_spools(
    logged_in_client, monkeypatch, tmp_path
):
    # MEMNG-621: a PATCH used to keep its lookup transaction — and so a pooled connection — open
    # for as long as the client took to send the body, so a handful of slow uploads could drain
    # the pool for every other request. Mid-body, every connection checked out must be back.
    # Counted with pool events because tests run on NullPool, which has no `checkedout()`.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    counts = {"checkout": 0, "checkin": 0}
    mid_body: dict[str, int] = {}

    def on_checkout(*_):
        counts["checkout"] += 1

    def on_checkin(*_):
        counts["checkin"] += 1

    async def body():
        yield b"a" * 100
        mid_body.update(counts)  # the route is now inside the spool, waiting for more bytes
        yield b"b" * 100

    try:
        uid = _start(client, "held.bin", "application/octet-stream", 200)
        target = async_engine.sync_engine
        event.listen(target, "checkout", on_checkout)
        event.listen(target, "checkin", on_checkin)
        try:
            async with _cookie_client(client) as ac:
                r = await ac.patch(
                    f"/api/uploads/{uid}", content=body(), headers={"Upload-Offset": "0"}
                )
        finally:
            event.remove(target, "checkout", on_checkout)
            event.remove(target, "checkin", on_checkin)
        assert r.status_code == 200, r.text
        assert r.headers["Upload-Offset"] == "200"
        assert mid_body["checkout"] >= 1  # the auth + lookup really did use a connection
        assert mid_body["checkin"] == mid_body["checkout"], mid_body
        assert svc.part_path(uid).read_bytes() == b"a" * 100 + b"b" * 100
    finally:
        _cleanup()


async def test_concurrent_http_appends_at_one_offset_land_only_the_winner(
    logged_in_client, monkeypatch, tmp_path
):
    # Without a connection held across the body, two PATCHes at one offset can both be mid-body at
    # once, each on its own DB session. The one that merges second must re-decide against the
    # offset the first COMMITTED — a replay no-op answering 200 (the route is not strict_offset, so
    # a 409 here would be a protocol change) — and must not write its bytes over the winner's.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    a_in_flight, b_in_flight, a_answered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def a_body():
        yield b"A" * 300
        a_in_flight.set()
        await b_in_flight.wait()  # both bodies are now half-sent at offset 0
        yield b"A" * 300

    async def b_body():
        yield b"B" * 200
        b_in_flight.set()
        await a_answered.wait()  # ... A merges and commits here
        yield b"B" * 200

    try:
        uid = _start(client, "race.bin", "application/octet-stream", 1000)
        async with _cookie_client(client) as ca, _cookie_client(client) as cb:
            b_task = asyncio.create_task(
                cb.patch(f"/api/uploads/{uid}", content=b_body(), headers={"Upload-Offset": "0"})
            )
            # Bounded: a server that serialized the two requests would park A on B forever.
            a = await asyncio.wait_for(
                ca.patch(f"/api/uploads/{uid}", content=a_body(), headers={"Upload-Offset": "0"}),
                timeout=30,
            )
            a_answered.set()
            b = await asyncio.wait_for(b_task, timeout=30)
        assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
        assert a.headers["Upload-Offset"] == "600"
        assert b.headers["Upload-Offset"] == "600"  # the winner's offset, not B's own 400
        assert svc.part_path(uid).read_bytes() == b"A" * 600
        assert _upload_row(uid)[0] == 600
        assert list((tmp_path / "uploads").glob("*.spool")) == []
    finally:
        _cleanup()


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

        # ...and the offset answer still wins when the body itself would blow the ceiling
        huge = b"z" * 5000  # way past the 800-byte declared size
        big_gap = client.patch(
            f"/api/uploads/{uid}", content=huge, headers={"Upload-Offset": "500"}
        )
        assert big_gap.status_code == 409  # 409, not 413
        big_replay = client.patch(
            f"/api/uploads/{uid}", content=huge, headers={"Upload-Offset": "0"}
        )
        assert big_replay.status_code == 200 and big_replay.json()["bytes_received"] == 400
        assert svc.part_path(uid).stat().st_size == 400
    finally:
        _cleanup()


def test_cross_user_head_append_finalize_are_404_and_leave_the_session_intact(
    logged_in_client, monkeypatch, tmp_path
):
    # An upload_uid is not a capability. Another authenticated user who learns one must get the
    # SAME answer as for a uid that never existed on every verb — otherwise a leaked uid lets them
    # read the offset (HEAD), corrupt the bytes (append), or finalize the victim's file into an
    # attachment stamped with THEIR owner_id.
    victim, victim_subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"V" * 4000
        uid = _start(victim, "victim.png", "image/png", len(data))
        victim.patch(f"/api/uploads/{uid}", content=data[:2000], headers={"Upload-Offset": "0"})

        attacker = _login_as(monkeypatch, "upload_thief")
        ghost = "a-uid-that-never-existed"

        # HEAD — probing an in-flight session
        head, head_ghost = (attacker.head(f"/api/uploads/{x}") for x in (uid, ghost))
        assert head.status_code == 404
        assert (head.status_code, head.text) == (head_ghost.status_code, head_ghost.text)
        assert "Upload-Offset" not in head.headers  # the victim's progress is not disclosed

        # APPEND — a well-formed continuation at the victim's real offset
        at_2000 = {"Upload-Offset": "2000"}
        patch, patch_ghost = (
            attacker.patch(f"/api/uploads/{x}", content=b"X" * 100, headers=at_2000)
            for x in (uid, ghost)
        )
        assert patch.status_code == 404
        assert (patch.status_code, patch.text) == (patch_ghost.status_code, patch_ghost.text)

        # FINALIZE — of a now fully-received session
        victim.patch(f"/api/uploads/{uid}", content=data[2000:], headers={"Upload-Offset": "2000"})
        fin, fin_ghost = (attacker.post(f"/api/uploads/{x}/finalize") for x in (uid, ghost))
        assert fin.status_code == 404
        assert (fin.status_code, fin.text) == (fin_ghost.status_code, fin_ghost.text)
        assert _attachment_owners() == {}  # nothing was finalized for anyone

        # the victim's session is untouched by all of it, and still finalizes to the victim
        assert victim.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == str(len(data))
        assert svc.part_path(uid).read_bytes() == data  # no foreign bytes were merged in
        own = victim.post(f"/api/uploads/{uid}/finalize")
        assert own.status_code == 200
        assert _attachment_owners() == {own.json()["uid"]: victim_subject}
    finally:
        _cleanup()
        _drop_user("upload_thief")


def test_cross_user_memo_create_and_edit_treat_a_foreign_upload_uid_as_unknown(
    logged_in_client, monkeypatch, tmp_path
):
    # The direct routes are not the only door: POST /api/memos and PATCH /api/memos/{uid} carry
    # upload_uids into finalize_pending, which stamps the SUBMITTING user as owner. A foreign uid
    # must be indistinguishable from an unknown one there too.
    victim, victim_subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"W" * 4000
        uid = _start(victim, "victim2.png", "image/png", len(data))
        victim.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        ghost = "a-uid-that-never-existed"

        attacker = _login_as(monkeypatch, "memo_thief")
        stolen, stolen_ghost = (
            attacker.post(
                "/api/memos",
                json={"content": "mine now", "visibility": "PUBLIC", "upload_uids": [x]},
            )
            for x in (uid, ghost)
        )
        # Both are refused (MEMNG-113) and — the invariant that matters here — refused IDENTICALLY:
        # same status, same code, same bucket. A foreign uid answering differently from one that
        # never existed would make this endpoint an oracle for other users' in-flight sessions.
        assert stolen.status_code == stolen_ghost.status_code == 409
        for response, submitted in ((stolen, uid), (stolen_ghost, ghost)):
            assert response.json()["detail"] == {
                "code": "uploads_unlinkable",
                "missing_upload_uids": [submitted],
                "incomplete_upload_uids": [],
            }

        target = attacker.post(
            "/api/memos", json={"content": "edit me", "visibility": "PUBLIC"}
        ).json()
        edited, edited_ghost = (
            attacker.patch(f"/api/memos/{target['uid']}", json={"upload_uids": [x]})
            for x in (uid, ghost)
        )
        assert edited.status_code == edited_ghost.status_code == 409
        for response, submitted in ((edited, uid), (edited_ghost, ghost)):
            assert response.json()["detail"]["missing_upload_uids"] == [submitted]
        assert attacker.get(f"/api/memos/{target['uid']}").json()["attachments"] == []
        assert _attachment_owners() == {}  # no attachment was created for the submitter

        # the victim's completed session survived both attempts and still finalizes to the victim
        assert victim.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == str(len(data))
        mine = victim.post(
            "/api/memos", json={"content": "mine", "visibility": "PUBLIC", "upload_uids": [uid]}
        ).json()
        assert len(mine["attachments"]) == 1
        assert _attachment_owners() == {mine["attachments"][0]["uid"]: victim_subject}
    finally:
        _cleanup()
        _drop_user("memo_thief")


def test_pre_ownership_upload_row_fails_closed_on_every_verb(
    logged_in_client, monkeypatch, tmp_path
):
    # A row created before the owner column carries owner_id NULL. It has no verifiable creator, so
    # it is claimable by NOBODY — every verb answers 404 and it ages out via the expiry reaper.
    # Anything else would hand the whole in-flight population to the first user who submits its uid.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    uid = "legacy-null-owner-uid"
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"N" * 4000
        part = svc.part_path(uid)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(data)  # fully received: only ownership can stop it being finalized
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO uploads (upload_uid, owner_id, filename, mime, declared_size,"
                    " bytes_received, expires_at) VALUES (:u, NULL, 'legacy.png', 'image/png',"
                    " :s, :s, now() + interval '1 hour')"
                ),
                {"u": uid, "s": len(data)},
            )
        eng.dispose()

        assert client.head(f"/api/uploads/{uid}").status_code == 404
        assert (
            client.patch(
                f"/api/uploads/{uid}", content=b"x" * 10, headers={"Upload-Offset": str(len(data))}
            ).status_code
            == 404
        )
        assert client.post(f"/api/uploads/{uid}/finalize").status_code == 404
        memo = client.post(
            "/api/memos", json={"content": "x", "visibility": "PUBLIC", "upload_uids": [uid]}
        )
        assert memo.status_code == 409  # reads as missing, exactly like a uid that never existed
        assert memo.json()["detail"]["missing_upload_uids"] == [uid]
        assert _attachment_owners() == {}  # the orphaned bytes became nobody's attachment

        eng = create_engine(SYNC_URL)
        with eng.connect() as c:  # the row itself is untouched, so the reaper can still collect it
            still_there = c.execute(
                text("SELECT bytes_received FROM uploads WHERE upload_uid = :u"), {"u": uid}
            ).scalar_one()
        eng.dispose()
        assert still_there == len(data)
    finally:
        _cleanup()


def test_deleting_a_user_nulls_their_upload_instead_of_stranding_its_bytes(
    logged_in_client, monkeypatch, tmp_path
):
    # The owner FK is ON DELETE SET NULL, not CASCADE. reap_expired unlinks a .part file only by
    # walking its uploads row (the orphan sweep matches *.spool), so cascading the row away with the
    # user would strand those bytes on disk with nothing left to find them. Nulling the owner drops
    # the row into the already-covered fail-closed state and keeps it reapable.
    other, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        departing = _login_as(monkeypatch, "departing_user")
        data = b"\x89PNG\r\n\x1a\n" + b"D" * 4000
        uid = _start(departing, "leaving.png", "image/png", len(data))
        departing.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        part = svc.part_path(uid)
        assert part.read_bytes() == data

        _drop_user("departing_user")

        # the row OUTLIVES its owner — under CASCADE it would be gone here, orphaning the part file
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            row = c.execute(
                text("SELECT owner_id, bytes_received FROM uploads WHERE upload_uid = :u"),
                {"u": uid},
            ).one()
        eng.dispose()
        assert row == (None, len(data))
        assert part.exists()

        # ...and an owner-less session is claimable by nobody: every verb still fails closed
        assert other.head(f"/api/uploads/{uid}").status_code == 404
        assert (
            other.patch(
                f"/api/uploads/{uid}", content=b"x" * 10, headers={"Upload-Offset": str(len(data))}
            ).status_code
            == 404
        )
        assert other.post(f"/api/uploads/{uid}/finalize").status_code == 404
        memo = other.post(
            "/api/memos", json={"content": "x", "visibility": "PUBLIC", "upload_uids": [uid]}
        )
        assert memo.status_code == 409  # the departed owner's row is claimable by nobody
        assert memo.json()["detail"]["missing_upload_uids"] == [uid]
        assert _attachment_owners() == {}

        # ...and the reaper still reclaims BOTH the row and the bytes once it expires
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(
                text(
                    "UPDATE uploads SET expires_at = now() - interval '1 hour'"
                    " WHERE upload_uid = :u"
                ),
                {"u": uid},
            )
        eng.dispose()
        assert _reap_expired_committed() == 1
        assert not part.exists()  # the bytes are gone, not stranded
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            left = c.execute(
                text("SELECT count(*) FROM uploads WHERE upload_uid = :u"), {"u": uid}
            ).scalar_one()
        eng.dispose()
        assert left == 0
    finally:
        _cleanup()
        _drop_user("departing_user")


@pytest.mark.anyio
async def test_worker_reap_entry_commits_the_deletions(db_engine, tmp_path, monkeypatch):
    # The worker used to reap through a session it never committed: the part file
    # died (unlinked pre-flush) but the row resurrected on rollback, so the same
    # uploads were "reaped" every cycle forever. The committed entry must leave
    # the row durably gone, visible from a fresh connection.
    monkeypatch.setattr(svc.settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        uid_owner = c.execute(
            text(
                "INSERT INTO users (subject, role) VALUES ('reap_commit_user','user') RETURNING id"
            )
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO uploads (upload_uid, filename, mime, declared_size, "
                "bytes_received, owner_id, expires_at) "
                "VALUES ('reap-commit-uid','f.bin','application/octet-stream',"
                "1,0,:o, now() - interval '1 hour')"
            ),
            {"o": uid_owner},
        )
    eng.dispose()
    try:
        reaped = await svc.reap_expired_committed()
        assert reaped >= 1
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            left = c.execute(
                text("SELECT count(*) FROM uploads WHERE upload_uid='reap-commit-uid'")
            ).scalar_one()
        eng.dispose()
        assert left == 0
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject='reap_commit_user'"))
        eng.dispose()


def test_worker_loop_uses_the_committed_reap_entry():
    # Wiring pin: the worker must call the committing wrapper, never the bare
    # session-scoped reap whose deletions die with an uncommitted session.
    from pathlib import Path

    source = Path(__file__).parent.parent.joinpath("app", "worker.py").read_text()
    assert "reap_expired_committed()" in source
    assert "reap_expired(db)" not in source


def _expired_part(uid: str, payload: bytes):
    """A COMMITTED expired session with a real part file under it."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO uploads (upload_uid, filename, mime, declared_size, bytes_received, "
                "expires_at) VALUES (:u, 'a.bin', 'application/octet-stream', :n, :n, "
                "now() - interval '1 hour')"
            ),
            {"u": uid, "n": len(payload)},
        )
    eng.dispose()
    part = svc.part_path(uid)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(payload)
    return part


def _upload_rows(uid: str) -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM uploads WHERE upload_uid = :u"), {"u": uid}
        ).scalar_one()
    eng.dispose()
    return n


def test_a_reap_that_rolls_back_keeps_the_part_bytes(db_engine, monkeypatch, tmp_path):
    # MEMNG-1162: the sweep used to unlink inside its transaction, so a reap that rolled back
    # restored the row and lost its bytes. The row and the bytes now go together or not at all.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _cleanup()
    payload = b"the only copy" * 64
    part = _expired_part("rollback-reap-uid", payload)

    async def _reap_then_roll_back() -> int:
        from app.database import async_session

        async with async_session() as db:
            reaped = await svc.reap_expired(db)
            await db.rollback()
            return reaped

    try:
        assert asyncio.run(_reap_then_roll_back()) == 1
        assert _upload_rows("rollback-reap-uid") == 1  # the row is back...
        assert part.read_bytes() == payload  # ...and so are its bytes, every one of them
    finally:
        _cleanup()


def test_the_committed_reap_unlinks_the_part_only_once_the_deletion_committed(
    db_engine, monkeypatch, tmp_path
):
    from sqlalchemy.orm import Session

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _cleanup()
    part = _expired_part("commit-reap-uid", b"expired bytes")
    at_commit: list[bool] = []

    def _seen_at_commit(session):
        if not session.in_nested_transaction():
            at_commit.append(part.exists())

    event.listen(Session, "after_commit", _seen_at_commit)
    try:
        assert asyncio.run(svc.reap_expired_committed()) == 1
    finally:
        event.remove(Session, "after_commit", _seen_at_commit)
        _cleanup()
    assert at_commit == [True]  # still on disk when the deletion committed
    assert not part.exists()  # gone once it had
    assert _upload_rows("commit-reap-uid") == 0


def test_a_part_that_will_not_unlink_after_the_commit_is_logged_not_raised(
    db_engine, monkeypatch, tmp_path
):
    # Past the commit the session is gone for good: a part that refuses to go is a logged leak,
    # never an error that reports a committed reap as failed.
    from structlog.testing import capture_logs

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _cleanup()
    part = svc.part_path("stuck-reap-uid")
    _expired_part("stuck-reap-uid", b"x").unlink()
    part.mkdir()  # unlink() of a directory refuses (EISDIR) — a stand-in for a refusing volume
    try:
        with capture_logs() as logs:
            assert asyncio.run(svc.reap_expired_committed()) == 1
        assert _upload_rows("stuck-reap-uid") == 0
        assert any(e["event"] == "uploads.consumed_reap_failed" for e in logs)
    finally:
        part.rmdir()
        _cleanup()


def _upload_row(uid):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT bytes_received, capture_state FROM uploads WHERE upload_uid = :u"),
            {"u": uid},
        ).one()
    eng.dispose()
    return row


async def _patch_cut_short(token, uid, offset, pieces, content_length):
    """PATCH through the COMPLETE app, fed the ASGI messages uvicorn sends when the client's
    connection drops mid-body: each piece as a `more_body` request message, then `http.disconnect`
    for every later receive (uvicorn `httptools_impl.py` `receive`, :571-572). httpx's ASGI
    transport cannot say this — it has no way to end a body except by finishing it."""
    queue = [{"type": "http.request", "body": p, "more_body": True} for p in pieces]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    return await _asgi_patch(token, uid, offset, content_length, receive)


async def _asgi_patch(token, uid, offset, content_length, receive):
    """PATCH through the COMPLETE app with the caller's own ASGI `receive`, so a test can say what
    httpx cannot: a body that drops, stalls, or trickles."""
    import json

    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {token}".encode()),
        (b"upload-offset", str(offset).encode()),
        (b"content-type", b"application/octet-stream"),
        (b"content-length", str(content_length).encode()),
    ]
    path = f"/api/uploads/{uid}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PATCH",
        "headers": headers,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }
    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


async def test_bytes_before_a_disconnect_are_kept_and_the_resume_completes(
    pat_client, monkeypatch, tmp_path, caplog
):
    import logging

    from structlog.testing import capture_logs

    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    content = os.urandom(10_000)
    start = await bearer.post(
        "/api/uploads",
        json={"filename": "cut.bin", "mime": "application/octet-stream", "size": len(content)},
    )
    uid = start.json()["upload_uid"]
    try:
        with caplog.at_level(logging.DEBUG), capture_logs() as events:
            status, out = await _patch_cut_short(
                token, uid, 0, [content[:2500], content[2500:4000]], len(content)
            )
        assert status == 200
        assert out["bytes_received"] == 4000
        head = await bearer.head(f"/api/uploads/{uid}")
        assert head.headers["Upload-Offset"] == "4000"
        assert list((tmp_path / "uploads").glob("*.spool")) == []
        partials = [e for e in events if e["event"] == "upload.partial_commit"]
        assert [(e["upload_uid"], e["offset"], e["bytes"]) for e in partials] == [(uid, 0, 4000)]
        assert not [r for r in caplog.records if r.exc_info or r.levelno >= logging.ERROR]
        assert "ResponseValidationError" not in caplog.text
        assert _upload_row(uid) == (4000, "pending")

        rest = await bearer.patch(
            f"/api/uploads/{uid}", content=content[4000:], headers={"Upload-Offset": "4000"}
        )
        assert rest.status_code == 200 and rest.json()["bytes_received"] == len(content)
        assert _upload_row(uid) == (len(content), "done")  # the completing append read capture
        assert svc.part_path(uid).read_bytes() == content
    finally:
        _cleanup()


def test_a_body_longer_than_chunk_size_is_accepted_whole(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_CHUNK_SIZE", 1000)
    try:
        content = os.urandom(7_500)
        uid = _start(client, "long.bin", "application/octet-stream", len(content))
        r = client.patch(f"/api/uploads/{uid}", content=content, headers={"Upload-Offset": "0"})
        assert r.status_code == 200
        assert r.json()["chunk_size"] == 1000
        assert r.json()["bytes_received"] == len(content)
        assert client.head(f"/api/uploads/{uid}").headers["Upload-Offset"] == str(len(content))

        # The length above is a pin of what the route already accepts; the served contract is what
        # tells a client it may send it, so the words are asserted too.
        served = client.get("/openapi.json").json()
        patch = served["paths"]["/api/uploads/{upload_uid}"]["patch"]
        assert "any length from 1 byte to the bytes remaining" in patch["description"]
        assert "Bytes received before a disconnect are kept" in patch["description"]
        fields = served["components"]["schemas"]["UploadOut"]["properties"]
        assert (
            "any length from 1 byte to the bytes remaining" in fields["chunk_size"]["description"]
        )
        assert "the offset the next PATCH must start at" in fields["bytes_received"]["description"]
    finally:
        _cleanup()


async def test_a_disconnect_before_any_byte_changes_nothing_and_takes_no_lock(
    pat_client, monkeypatch, tmp_path
):
    import threading

    import psycopg2
    from structlog.testing import capture_logs

    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    content = os.urandom(6_000)
    start = await bearer.post(
        "/api/uploads",
        json={"filename": "zero.bin", "mime": "application/octet-stream", "size": len(content)},
    )
    uid = start.json()["upload_uid"]
    try:
        first = await bearer.patch(
            f"/api/uploads/{uid}", content=content[:3000], headers={"Upload-Offset": "0"}
        )
        assert first.json()["bytes_received"] == 3000
        # Another connection holds the row lock and lets go of it only after 5 s: an append that
        # asked for the lock would answer only after that release, so the answer must come first.
        holder = psycopg2.connect(SYNC_URL.replace("postgresql+psycopg2", "postgresql"))
        released = threading.Event()

        def release():
            holder.rollback()
            released.set()

        with holder.cursor() as cur:
            cur.execute("SELECT 1 FROM uploads WHERE upload_uid = %s FOR UPDATE", (uid,))
        timer = threading.Timer(5, release)
        timer.start()
        try:
            with capture_logs() as events:
                status, out = await _patch_cut_short(token, uid, 3000, [], len(content) - 3000)
            answered_under_the_lock = not released.is_set()
        finally:
            timer.join()
            holder.close()
        assert answered_under_the_lock
        assert status == 200
        assert out["bytes_received"] == 3000
        assert [e for e in events if e["event"] == "upload.partial_commit"] == []
        assert list((tmp_path / "uploads").glob("*.spool")) == []
        assert _upload_row(uid) == (3000, "pending")
        assert svc.part_path(uid).read_bytes() == content[:3000]
    finally:
        _cleanup()


async def test_a_cut_body_whose_offset_a_concurrent_append_moved_is_discarded(
    pat_client, monkeypatch, tmp_path
):
    from app.database import async_session
    from sqlalchemy import select
    from structlog.testing import capture_logs

    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    content = os.urandom(10_000)
    start = await bearer.post(
        "/api/uploads",
        json={"filename": "race.bin", "mime": "application/octet-stream", "size": len(content)},
    )
    uid = start.json()["upload_uid"]
    real_spool = svc.spool_stream
    raced = []

    async def spool_then_race(*args, **kwargs):
        # The cut body is fully spooled; before its append asks for the row lock, another append
        # of the same range lands and commits through its own session. That append spools through
        # this same function, so only the first call races.
        spooled = await real_spool(*args, **kwargs)
        if not raced:
            raced.append(True)
            async with async_session() as other:
                row = (
                    await other.execute(select(Upload).where(Upload.upload_uid == uid))
                ).scalar_one()
                await svc.append_chunk(other, row, 0, content[:3000])
                await other.commit()
        return spooled

    monkeypatch.setattr(svc, "spool_stream", spool_then_race)
    try:
        with capture_logs() as events:
            status, out = await _patch_cut_short(
                token, uid, 0, [content[:2500], content[2500:4000]], len(content)
            )
        assert raced == [True]
        assert status == 200
        assert out["bytes_received"] == 3000
        assert [e for e in events if e["event"] == "upload.partial_commit"] == []
        assert list((tmp_path / "uploads").glob("*.spool")) == []
        assert _upload_row(uid) == (3000, "pending")
        assert svc.part_path(uid).read_bytes() == content[:3000]
    finally:
        _cleanup()


async def test_spool_stream_keeps_a_cut_body_only_for_a_caller_that_asked(monkeypatch, tmp_path):
    from starlette.requests import ClientDisconnect

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    parts = [b"a" * 700, b"b" * 300]

    async def cut():
        for p in parts:
            yield p
        raise ClientDisconnect()

    kept = tmp_path / "uploads" / "kept.spool"
    spooled = await svc.spool_stream(cut(), kept, start=0, ceiling=10_000, keep_cut=True)
    assert spooled == svc.Spooled(received=1000, cut=True)
    assert kept.read_bytes() == b"".join(parts)

    dropped = tmp_path / "uploads" / "dropped.spool"
    with pytest.raises(ClientDisconnect):
        await svc.spool_stream(cut(), dropped, start=0, ceiling=10_000)
    assert not dropped.exists()


# ---------------------------------------------------------------------------------------------
# The store: strict offsets at both decision points; the sent millisecond kept exactly
# ---------------------------------------------------------------------------------------------


async def test_strict_offset_refuses_on_the_snapshot_without_consuming_the_body(
    adb, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "store_strict_snapshot")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="s.bin", mime="application/octet-stream", size=900
    )
    await svc.append_stream(adb, up, 0, _chunks([b"A" * 300]))
    pulled = False

    async def body():
        nonlocal pulled
        pulled = True
        yield b"Z" * 100

    for stale in (0, 299, 301):
        with pytest.raises(ValueError):
            await svc.append_stream(adb, up, stale, body(), strict_offset=True)
    assert not pulled  # decided on the snapshot, before the body was read
    assert up.bytes_received == 300
    assert svc.part_path(up.upload_uid).read_bytes() == b"A" * 300

    # the default keeps the /api/uploads contract: a replay is a no-op, not a refusal
    replay = await svc.append_stream(adb, up, 0, body())
    assert replay is not None and replay.upload.bytes_received == 300


async def test_strict_offset_refuses_under_the_row_lock_when_the_snapshot_went_stale(
    db_engine, monkeypatch, tmp_path
):
    # B's snapshot is true when B starts (offset 0), and B's body stays inside the ceiling, so the
    # only thing that can refuse it is the decision re-taken under the row lock after A committed.
    # Two real sessions, not the `adb` fixture's one transaction: A's append is COMMITTED before B
    # asks for the lock, as it would be between two requests.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner_id = c.execute(
            text("INSERT INTO users (subject, role) VALUES ('strict_lock','user') RETURNING id")
        ).scalar_one()
    eng.dispose()
    try:
        from app.database import async_session

        async with async_session() as s0:
            up = await svc.create_upload(
                s0, owner_id=owner_id, filename="l.bin", mime="application/octet-stream", size=900
            )
            await s0.commit()
            uid = up.upload_uid

        async def fetch(session):
            return (
                await session.execute(select(Upload).where(Upload.upload_uid == uid))
            ).scalar_one()

        b_in_flight, a_committed = asyncio.Event(), asyncio.Event()

        async def b_body():
            yield b"B" * 100
            b_in_flight.set()
            await a_committed.wait()
            yield b"B" * 100

        async with async_session() as sa, async_session() as sb:
            b_snapshot = await fetch(sb)
            b_task = asyncio.create_task(
                svc.append_stream(sb, b_snapshot, 0, b_body(), strict_offset=True)
            )
            await b_in_flight.wait()
            await svc.append_stream(sa, await fetch(sa), 0, _chunks([b"A" * 500]))
            await sa.commit()
            a_committed.set()
            with pytest.raises(ValueError):
                await b_task
            await sb.rollback()

        assert _upload_row(uid).bytes_received == 500
        assert svc.part_path(uid).read_bytes() == b"A" * 500
        assert list((tmp_path / "uploads").glob("*.spool")) == []
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM uploads WHERE owner_id = :o"), {"o": owner_id})
            c.execute(text("DELETE FROM users WHERE id = :o"), {"o": owner_id})
        eng.dispose()


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        (1001, datetime(1970, 1, 1, 0, 0, 1, 1000, tzinfo=UTC)),
        (1700000000123, datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)),
        (253402300799999, datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)),
        (None, None),
        (0, None),
    ],
)
async def test_create_upload_stores_the_sent_millisecond_exactly(
    adb, monkeypatch, tmp_path, sent, stored
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "store_mtime")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="m.bin", mime="x/y", size=1, last_modified=sent
    )
    reread = (
        await adb.execute(
            select(Upload)
            .where(Upload.upload_uid == up.upload_uid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reread.client_mtime == stored


def test_upload_limits_follow_the_settings(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 7)
    monkeypatch.setattr(settings, "UPLOAD_WIFI_RECOMMENDED_MIB", 3)
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 5)
    r = client.get("/api/uploads/limits")
    assert r.status_code == 200
    assert r.json() == {
        "max_bytes": 7 * 1024 * 1024,
        "wifi_recommended_above_bytes": 3 * 1024 * 1024,
        "bodies_per_user": 5,
    }


def test_upload_limits_max_bytes_is_the_largest_size_a_session_accepts(
    logged_in_client, monkeypatch
):
    # The client marks a file too large from this number alone, so it must sit exactly on the
    # boundary start_upload enforces — one byte either way strands a file or wastes an upload.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)
    max_bytes = client.get("/api/uploads/limits").json()["max_bytes"]
    try:
        at = client.post(
            "/api/uploads", json={"filename": "a.mov", "mime": "video/mp4", "size": max_bytes}
        )
        assert at.status_code == 201
        over = client.post(
            "/api/uploads", json={"filename": "b.mov", "mime": "video/mp4", "size": max_bytes + 1}
        )
        assert over.status_code == 413
    finally:
        _cleanup()


def test_upload_limits_require_auth(client):
    assert client.get("/api/uploads/limits").status_code == 401


def test_upload_limits_route_precedes_every_upload_uid_route():
    # Starlette matches in declaration order, so a single-segment `/{upload_uid}` route declared
    # first would take `limits` as an upload uid.
    paths = [getattr(route, "path", None) for route in app.routes]
    limits = paths.index("/api/uploads/limits")
    parametric = [i for i, p in enumerate(paths) if p and p.startswith("/api/uploads/{upload_uid}")]
    assert parametric and all(limits < i for i in parametric)


# --- MEMNG-934: an idle body gives its admission slot back -----------------------------------
# A body that stops sending holds its request, and with it one of the account's body-admission
# permits, until something ends it. Without a server-side bound that something is Traefik's 300 s
# read timeout, and three dead bodies answer every retry from the account 429 for five minutes.
# These probes run with a 0.5 s bound; `tests/mutations/body_idle_unbounded.patch` removes it.

_IDLE = 0.5
# How long past the bound a stalled PATCH may take to end: generous for a loaded test host, and
# still far below anything an unbounded body would ever reach.
_IDLE_MARGIN = 2.0


def _stalling_receive(pieces):
    """ASGI `receive` for a client that sends `pieces` and then goes silent on an open
    connection: no more bytes, and no `http.disconnect` either — exactly what a dead mobile radio
    looks like from the server until a read timeout fires."""
    queue = [{"type": "http.request", "body": p, "more_body": True} for p in pieces]

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.Event().wait()  # never set
        raise AssertionError("unreachable")

    return receive


async def _within_the_bound(task):
    """Wait for a stalled PATCH for at most the bound plus the margin; answer the elapsed time.
    A task still running then is the defect itself, and is cancelled by the caller's cleanup."""
    started = asyncio.get_running_loop().time()
    done, _ = await asyncio.wait({task}, timeout=_IDLE + _IDLE_MARGIN)
    elapsed = asyncio.get_running_loop().time() - started
    assert task in done, f"a stalled PATCH still held its request {elapsed:.1f}s after it went idle"
    return elapsed


async def _settle(task):
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def _start_bearer(bearer, content, name="idle.bin"):
    start = await bearer.post(
        "/api/uploads",
        json={"filename": name, "mime": "application/octet-stream", "size": len(content)},
    )
    assert start.status_code == 201, start.text
    return start.json()["upload_uid"]


async def test_an_idle_body_ends_at_the_bound_and_frees_its_admission_slot(
    pat_client, monkeypatch, tmp_path
):
    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODY_IDLE_SECONDS", _IDLE)
    # One permit per account, so a permit the stalled body kept would answer the next PATCH 429.
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 1)
    content = os.urandom(10_000)
    uid = await _start_bearer(bearer, content)
    stalled = None
    try:
        stalled = asyncio.create_task(
            _asgi_patch(
                token, uid, 0, len(content), _stalling_receive([content[:2500], content[2500:4000]])
            )
        )
        # Not vacuous: while the stalled body holds the account's one permit, a retry is refused.
        await asyncio.sleep(_IDLE / 5)
        early = await bearer.patch(
            f"/api/uploads/{uid}", content=content, headers={"Upload-Offset": "0"}
        )
        assert early.status_code == 429

        elapsed = await _within_the_bound(stalled)
        assert elapsed >= _IDLE * 0.5  # ended by the bound, not by something earlier
        # The answer of the existing cut path: 200 with the offset the bytes reached.
        status, out = stalled.result()
        assert (status, out["bytes_received"]) == (200, 4000)

        retry = await bearer.patch(
            f"/api/uploads/{uid}", content=content[4000:], headers={"Upload-Offset": "4000"}
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["bytes_received"] == len(content)
    finally:
        await _settle(stalled)
        _cleanup()


async def test_bytes_before_an_idle_stall_are_kept_and_the_resume_completes(
    pat_client, monkeypatch, tmp_path
):
    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODY_IDLE_SECONDS", _IDLE)
    content = b"\x89PNG\r\n\x1a\n" + os.urandom(12_000)
    start = await bearer.post(
        "/api/uploads", json={"filename": "idle.png", "mime": "image/png", "size": len(content)}
    )
    uid = start.json()["upload_uid"]
    stalled = None
    try:
        stalled = asyncio.create_task(
            _asgi_patch(token, uid, 0, len(content), _stalling_receive([content[:5000]]))
        )
        await _within_the_bound(stalled)
        head = await bearer.head(f"/api/uploads/{uid}")
        assert head.headers["Upload-Offset"] == "5000"
        assert _upload_row(uid) == (5000, "pending")
        assert list((tmp_path / "uploads").glob("*.spool")) == []

        rest = await bearer.patch(
            f"/api/uploads/{uid}", content=content[5000:], headers={"Upload-Offset": "5000"}
        )
        assert rest.status_code == 200 and rest.json()["bytes_received"] == len(content)
        assert _upload_row(uid) == (len(content), "done")
        memo = await bearer.post(
            "/api/memos",
            json={
                "content": "resumed after a stall",
                "visibility": "PRIVATE",
                "upload_uids": [uid],
            },
        )
        assert memo.status_code in (200, 201), memo.text
        [att] = memo.json()["attachments"]
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            digest = c.execute(
                text("SELECT sha256 FROM attachments WHERE uid = :u"), {"u": att["uid"]}
            ).scalar_one()
        eng.dispose()
        assert digest == hashlib.sha256(content).hexdigest()
    finally:
        await _settle(stalled)
        _cleanup()


async def test_a_slow_body_that_keeps_moving_is_not_cut(pat_client, monkeypatch, tmp_path):
    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODY_IDLE_SECONDS", _IDLE)
    content = os.urandom(10_000)
    uid = await _start_bearer(bearer, content, "slow.bin")
    pieces = [content[i : i + 2000] for i in range(0, len(content), 2000)]
    queue = [
        {"type": "http.request", "body": p, "more_body": i < len(pieces) - 1}
        for i, p in enumerate(pieces)
    ]

    async def trickle():
        # Every gap is well inside the bound; together they are three times it.
        await asyncio.sleep(_IDLE * 0.6)
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    try:
        started = asyncio.get_running_loop().time()
        status, out = await _asgi_patch(token, uid, 0, len(content), trickle)
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed > _IDLE * 2  # the body as a whole outlasted the bound
        assert (status, out["bytes_received"]) == (200, len(content))
        assert _upload_row(uid) == (len(content), "done")
        assert svc.part_path(uid).read_bytes() == content
    finally:
        _cleanup()


async def test_a_body_idle_from_its_first_byte_changes_nothing_and_frees_its_slot(
    pat_client, monkeypatch, tmp_path
):
    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODY_IDLE_SECONDS", _IDLE)
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 1)
    content = os.urandom(3_000)
    uid = await _start_bearer(bearer, content, "silent.bin")
    stalled = None
    try:
        stalled = asyncio.create_task(
            _asgi_patch(token, uid, 0, len(content), _stalling_receive([]))
        )
        await _within_the_bound(stalled)
        status, out = stalled.result()
        assert (status, out["bytes_received"]) == (200, 0)
        assert _upload_row(uid) == (0, "pending")
        whole = await bearer.patch(
            f"/api/uploads/{uid}", content=content, headers={"Upload-Offset": "0"}
        )
        assert whole.status_code == 200 and whole.json()["bytes_received"] == len(content)
    finally:
        await _settle(stalled)
        _cleanup()


async def test_a_retry_that_overtakes_a_stalled_body_wins_and_the_cut_lands_nothing(
    pat_client, monkeypatch, tmp_path
):
    # The client gives up on the stalled request and resends the whole body on a new connection
    # while the old one is still open: two permits, two writers at offset 0. The retry completes
    # first; the stalled body's cut then finds its range already covered and merges nothing.
    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODY_IDLE_SECONDS", _IDLE)
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 2)
    content = os.urandom(10_000)
    uid = await _start_bearer(bearer, content, "race.bin")
    stale = os.urandom(4000)  # different bytes, so a cut that DID land would corrupt the file
    stalled = None
    try:
        stalled = asyncio.create_task(
            _asgi_patch(token, uid, 0, len(content), _stalling_receive([stale]))
        )
        await asyncio.sleep(_IDLE / 5)
        retry = await bearer.patch(
            f"/api/uploads/{uid}", content=content, headers={"Upload-Offset": "0"}
        )
        assert retry.status_code == 200 and retry.json()["bytes_received"] == len(content)
        await _within_the_bound(stalled)
        status, out = stalled.result()
        assert (status, out["bytes_received"]) == (200, len(content))
        assert svc.part_path(uid).read_bytes() == content
        assert list((tmp_path / "uploads").glob("*.spool")) == []
    finally:
        await _settle(stalled)
        _cleanup()


async def _then_silent(*chunks):
    for chunk in chunks:
        yield chunk
    await asyncio.Event().wait()  # never set


async def test_idle_bounded_passes_a_whole_body_through_unchanged():
    got = [c async for c in svc.idle_bounded(_chunks([b"ab", b"", b"cd"]), _IDLE)]
    assert got == [b"ab", b"", b"cd"]


async def test_idle_bounded_cuts_the_first_wait_that_outlasts_the_bound():
    got = []
    started = asyncio.get_running_loop().time()
    with pytest.raises(svc.BodyIdle) as cut:
        async for chunk in svc.idle_bounded(_then_silent(b"ab", b"cd"), _IDLE):
            got.append(chunk)
    elapsed = asyncio.get_running_loop().time() - started
    assert got == [b"ab", b"cd"]
    assert _IDLE * 0.5 <= elapsed < _IDLE + _IDLE_MARGIN
    # The disconnect path is what keeps a cut body's bytes; the cut must be one it recognises.
    from starlette.requests import ClientDisconnect

    assert isinstance(cut.value, ClientDisconnect)


async def test_idle_bounded_lets_every_other_outcome_of_a_wait_through_unchanged():
    # Only the bound's own expiry is a cut. The stream's own errors — a real disconnect, an I/O
    # error, even a TimeoutError of its own — and a cancellation from outside keep their identity,
    # so none of them is quietly turned into "keep the bytes and answer 200".
    for raised in (OSError("socket"), TimeoutError("stream's own")):

        async def failing(exc=raised):
            yield b"ab"
            raise exc

        with pytest.raises(type(raised)) as seen:
            async for _ in svc.idle_bounded(failing(), 10):
                pass
        assert not isinstance(seen.value, svc.BodyIdle)

    async def consume():
        async for _ in svc.idle_bounded(_then_silent(b"ab"), 10):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_two_committed_reaps_racing_for_one_session_unlink_and_count_it_once(
    db_engine, monkeypatch, tmp_path
):
    # The post-commit unlink takes its list from the set each sweep CLAIMED under the row lock, so
    # the sweep that queued behind the other finds the row gone and has nothing to unlink or count.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _cleanup()
    part = _expired_part("raced-reap-uid", b"once")

    async def _race() -> list[int]:
        return sorted(
            await asyncio.gather(svc.reap_expired_committed(), svc.reap_expired_committed())
        )

    try:
        assert asyncio.run(_race()) == [0, 1]
        assert not part.exists()
        assert _upload_rows("raced-reap-uid") == 0
    finally:
        _cleanup()
