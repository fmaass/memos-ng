"""The guest door (trip pass P2): redeem, upload sessions, completion into a batch, receipts.

Every case runs the real `ingest` app over ASGI against the test database; passes are pushed
through the real ingest-home interface first. Nothing here mocks a store or the session: the
concurrency cases hold real row locks and wait on `pg_locks` until the requests under test are
parked behind them.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings
from app.database import async_session
from app.ingest import core
from app.models import PassDevice
from sqlalchemy import select, text

from tests.ingest_support import (
    HEIC,
    JPEG,
    PNG,
    bearer,
    complete,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    pass_state,
    push,
    redeem,
    seeded_pass,
    upload,
)
from tests.test_attachments_taken import _sql


async def _waiters(n: int, budget_s: float = 10.0) -> None:
    """Until `n` backends of THIS database wait on a lock."""
    deadline = asyncio.get_running_loop().time() + budget_s
    while True:
        async with async_session() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE NOT l.granted AND a.datname = current_database()"
                    )
                )
            ).scalar_one()
        if waiting >= n:
            return
        assert asyncio.get_running_loop().time() < deadline, f"only {waiting} lock waiters"
        await asyncio.sleep(0.05)


# --- redeem ------------------------------------------------------------------------------------


async def test_redeem_enrolls_a_device_and_answers_a_token_shown_once(door, home_iface):  # noqa: F811
    uid, secret = await seeded_pass(home_iface)
    out = await redeem(door, secret, "Anna")
    assert out["device_token"].startswith("trip_dev_")
    assert out["pass"]["label"] == "Sardinia friends"
    rows = _sql("SELECT uid, token_hash, display_name, change_seq FROM pass_devices")
    assert len(rows) == 1 and rows[0][0] == out["device_id"] and rows[0][2] == "Anna"
    assert rows[0][1] == hashlib.sha256(out["device_token"].encode()).hexdigest()
    assert rows[0][3] is not None
    me = await door.get("/api/guest/me", headers=bearer(out["device_token"]))
    assert me.status_code == 200 and me.json()["device_id"] == out["device_id"]
    kinds = [r[0] for r in _sql("SELECT kind FROM pass_events WHERE ip_hash IS NOT NULL")]
    assert kinds == ["redeemed"]


async def test_an_unknown_secret_is_404_and_counted_per_address(door, home_iface):  # noqa: F811
    await seeded_pass(home_iface)
    r = await door.post(
        "/api/pass/redeem", json={"secret": "nope", "display_name": "x", "platform": "web"}
    )
    assert r.status_code == 404
    assert _sql("SELECT refusals FROM ingest_refusals") == [(1,)]
    assert _sql("SELECT count(*) FROM pass_events WHERE kind = 'refused'") == [(0,)]


@pytest.mark.parametrize("ended", ["expired", "revoked"])
async def test_an_ended_pass_is_410(door, home_iface, ended):  # noqa: F811
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    extra = (
        {"expires_at": past} if ended == "expired" else {"revoked_at": past, "revoked_version": 1}
    )
    _, secret = await seeded_pass(home_iface, **extra)
    r = await door.post(
        "/api/pass/redeem", json={"secret": secret, "display_name": "x", "platform": "web"}
    )
    assert r.status_code == 410
    assert _sql("SELECT count(*) FROM pass_devices") == [(0,)]
    assert _sql("SELECT detail FROM pass_events WHERE kind = 'refused'") == [("ended",)]


async def test_the_device_cap_is_429_and_admission_is_serialized(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface, max_devices=2)
    body = {"secret": secret, "display_name": "x", "platform": "web"}
    # Three concurrent redeems for two seats, each from its own address so the rate limit is out
    # of the way: the pass row lock admits exactly two.
    answers = await asyncio.gather(
        *(door.post("/api/pass/redeem", json=body, headers={"x": str(i)}) for i in range(3))
    )
    assert sorted(r.status_code for r in answers) == [201, 201, 429]
    assert _sql("SELECT count(*) FROM pass_devices") == [(2,)]


async def test_redeem_is_limited_per_address_and_cf_connecting_ip_counts_only_when_trusted(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    _, secret = await seeded_pass(home_iface)
    body = {"secret": secret, "display_name": "x", "platform": "web"}
    codes = [
        (
            await door.post(
                "/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"198.51.100.{i}"}
            )
        ).status_code
        for i in range(6)
    ]
    # Untrusted, the header is ignored: six attempts from one peer, the sixth refused.
    assert codes == [201] * 5 + [429]
    core.redeem_window.reset()
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", True)
    codes = [
        (
            await door.post(
                "/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"198.51.100.{i}"}
            )
        ).status_code
        for i in range(6)
    ]
    assert codes == [201] * 6


async def test_a_pass_with_more_than_the_hourly_refusals_is_suspended(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "INGEST_REFUSALS_PER_HOUR", 3)
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", True)
    _, secret = await seeded_pass(home_iface, max_devices=1)
    token = (await redeem(door, secret, **{"CF-Connecting-IP": "192.0.2.1"}))["device_token"]
    body = {"secret": secret, "display_name": "x", "platform": "web"}
    for i in range(4):  # four refusals at the cap, each from its own address
        r = await door.post(
            "/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"192.0.2.{10 + i}"}
        )
        assert r.status_code == 429
    assert _sql("SELECT suspended_at IS NOT NULL, version FROM passes") == [(True, 1)]
    # Suspended: the enrolled guest is shut out too, and a push does not lift it.
    assert (await door.get("/api/guest/me", headers=bearer(token))).status_code == 410
    uid = _sql("SELECT uid FROM passes")[0][0]
    out = await push(home_iface, pass_state(uid, secret, version=2, max_devices=1))
    assert out[0]["applied"] is True and out[0]["suspended_at"] is not None


async def test_an_address_past_the_hourly_refusals_is_shut_out_before_any_lookup(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "INGEST_REFUSALS_PER_HOUR", 2)
    monkeypatch.setattr(settings, "INGEST_REDEEM_PER_MINUTE", 100)
    _, secret = await seeded_pass(home_iface)
    for _ in range(3):
        r = await door.post(
            "/api/pass/redeem", json={"secret": "guess", "display_name": "x", "platform": "web"}
        )
        assert r.status_code == 404
    r = await door.post(
        "/api/pass/redeem", json={"secret": secret, "display_name": "x", "platform": "web"}
    )
    assert r.status_code == 429  # even the right secret, from this address, this hour


# --- upload sessions ---------------------------------------------------------------------------


async def test_upload_in_three_patches_with_a_resume_then_complete(door, home_iface, ingest_env):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads",
        json={
            "filename": "IMG_1.HEIC",
            "mime": "image/heic",
            "size": len(HEIC),
            "last_modified": 1700000000123,
        },
        headers=bearer(token),
    )
    assert r.status_code == 201
    uid = r.json()["upload_uid"]
    third = len(HEIC) // 3
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=HEIC[:third],
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 200 and r.headers["Upload-Offset"] == str(third)
    # A gap is refused and leaves the session as it was; HEAD tells the client where to resume.
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=b"xx",
        headers={**bearer(token), "Upload-Offset": str(third + 5)},
    )
    assert r.status_code == 409
    head = await door.head(f"/api/guest/uploads/{uid}", headers=bearer(token))
    assert head.headers["Upload-Offset"] == str(third)
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=HEIC[third : 2 * third],
        headers={**bearer(token), "Upload-Offset": str(third)},
    )
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=HEIC[2 * third :],
        headers={**bearer(token), "Upload-Offset": str(2 * third)},
    )
    assert r.json()["bytes_received"] == len(HEIC)
    done = await complete(door, token, [uid], caption="Day one")
    assert done.status_code == 200, done.text
    receipt = done.json()
    sha = hashlib.sha256(HEIC).hexdigest()
    assert receipt["status"] == "received" and receipt["files"] == [
        {"filename": "IMG_1.HEIC", "sha256": sha, "size": len(HEIC)}
    ]
    rows = _sql(
        "SELECT b.sha256, b.size_bytes, b.mime, b.storage_path, b.last_modified FROM ingest_blobs b"
    )
    assert rows[0][:4] == (sha, len(HEIC), "image/heic", f"blobs/{sha[:2]}/{sha}")
    assert rows[0][4] == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)
    assert (ingest_env / rows[0][3]).read_bytes() == HEIC
    assert not (ingest_env / "parts" / f"{uid}.part").exists()
    assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]
    assert _sql("SELECT uploads_count, bytes_total FROM pass_devices") == [(1, len(HEIC))]
    listed = await door.get("/api/guest/batches", headers=bearer(token))
    assert [b["batch_id"] for b in listed.json()] == [receipt["batch_id"]]


async def test_another_devices_session_answers_as_unknown(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    anna = (await redeem(door, secret, "Anna"))["device_token"]
    ben = (await redeem(door, secret, "Ben"))["device_token"]
    uid = await upload(door, anna, JPEG)
    assert (await door.head(f"/api/guest/uploads/{uid}", headers=bearer(ben))).status_code == 404
    r = await door.patch(
        f"/api/guest/uploads/{uid}", content=b"x", headers={**bearer(ben), "Upload-Offset": "0"}
    )
    assert r.status_code == 404
    assert (await complete(door, ben, [uid])).status_code == 409


async def test_a_known_hash_is_answered_already_received_before_any_byte(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    assert (await complete(door, token, [await upload(door, token, JPEG)])).status_code == 200
    sha = hashlib.sha256(JPEG).hexdigest()
    r = await door.post(
        "/api/guest/uploads",
        json={"filename": "again.jpg", "size": len(JPEG), "sha256": sha.upper()},
        headers=bearer(token),
    )
    assert r.status_code == 200 and r.json()["already_received"] is True
    assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]
    # Another pass's guest holding the same bytes gets no such answer: the check is per pass.
    _, other = await seeded_pass(home_iface)
    stranger = (await redeem(door, other))["device_token"]
    r = await door.post(
        "/api/guest/uploads",
        json={"filename": "a.jpg", "size": len(JPEG), "sha256": sha},
        headers=bearer(stranger),
    )
    assert r.status_code == 201


@pytest.mark.parametrize("name", ["a.exe", "a", "a.jpg.php", "a.gif"])
async def test_a_name_outside_the_allowlist_is_415_at_create(door, home_iface, name):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": name, "size": 10}, headers=bearer(token)
    )
    assert r.status_code == 415


@pytest.mark.parametrize(
    ("name", "data", "ok"),
    [
        ("a.jpg", JPEG, True),
        ("a.png", PNG, True),
        ("a.heic", HEIC, True),
        ("a.jpg", PNG, False),
        ("a.heic", JPEG, False),
        ("a.mov", HEIC, False),
        ("a.png", b"<?php system($_GET['x']); ?>" + b" " * 64, False),
        ("a.jpg", b"\xff\xd8", False),
    ],
)
async def test_the_bytes_must_be_what_the_name_says_or_the_completion_is_415(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    name,
    data,
    ok,
):
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uid = await upload(door, token, data, name)
    r = await complete(door, token, [uid])
    if ok:
        assert r.status_code == 200
        return
    assert r.status_code == 415
    # Discarded: the session and its part are gone, nothing was filed.
    assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]
    assert not (ingest_env / "parts" / f"{uid}.part").exists()


async def test_a_declared_hash_the_bytes_do_not_match_is_refused(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uid = await upload(door, token, JPEG, sha256="0" * 64)
    assert (await complete(door, token, [uid])).status_code == 422
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]


async def test_the_pass_byte_cap_and_the_volume_floor_are_507(door, home_iface, monkeypatch):  # noqa: F811
    _, secret = await seeded_pass(home_iface, byte_cap=len(JPEG) + 10)
    token = (await redeem(door, secret))["device_token"]
    await upload(door, token, JPEG)  # an open session reserves its declared size
    r = await door.post(
        "/api/guest/uploads", json={"filename": "b.jpg", "size": 11}, headers=bearer(token)
    )
    assert r.status_code == 507
    r = await door.post(
        "/api/guest/uploads", json={"filename": "b.jpg", "size": 10}, headers=bearer(token)
    )
    assert r.status_code == 201  # control: exactly at the cap
    monkeypatch.setattr(settings, "INGEST_MIN_FREE_BYTES", 1 << 62)
    _, roomy = await seeded_pass(home_iface)
    other = (await redeem(door, roomy))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "c.jpg", "size": 1}, headers=bearer(other)
    )
    assert r.status_code == 507


async def test_a_body_over_the_patch_bound_is_413(door, home_iface, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "INGEST_PATCH_MAX_BYTES", 100)
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": len(JPEG)}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=JPEG[:101],
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 413

    async def chunked():  # no Content-Length: bounded while it streams
        yield JPEG[:60]
        yield JPEG[60:120]

    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=chunked(),
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 413
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=JPEG[:100],
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 200 and r.json()["bytes_received"] == 100  # control


async def test_a_device_revoked_while_its_body_streams_lands_no_bytes(door, home_iface, ingest_env):  # noqa: F811
    pass_uid, secret = await seeded_pass(home_iface)
    out = await redeem(door, secret)
    token = out["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": len(JPEG)}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]

    async def body():
        yield JPEG[:100]
        revoked = await home_iface.put(
            "/ingest/device-revocations",
            json={
                "revocations": [
                    {
                        "device_id": out["device_id"],
                        "pass_uid": pass_uid,
                        "version": 2,
                        "revoked_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
        )
        assert revoked.json()["highest_version"] == 2
        yield JPEG[100:]

    r = await door.patch(
        f"/api/guest/uploads/{uid}", content=body(), headers={**bearer(token), "Upload-Offset": "0"}
    )
    assert r.status_code == 401
    assert _sql("SELECT bytes_received FROM guest_uploads") == [(0,)]
    assert (ingest_env / "parts" / f"{uid}.part").read_bytes() == b""
    assert list((ingest_env / "parts").glob("*.spool")) == []
    assert (await complete(door, token, [uid])).status_code == 401


async def test_a_completion_fenced_by_a_revocation_files_nothing(door, home_iface):  # noqa: F811
    pass_uid, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uid = await upload(door, token, JPEG)
    async with async_session() as blocker:
        await blocker.execute(text("SELECT 1 FROM passes FOR UPDATE"))
        task = asyncio.ensure_future(complete(door, token, [uid]))
        await _waiters(1)
        await blocker.execute(
            text("UPDATE passes SET revoked_at = now(), revoked_version = 2, version = 2")
        )
        await blocker.commit()
    assert (await task).status_code == 410
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]
    assert _sql("SELECT count(*) FROM guest_uploads") == [(1,)]


# --- completion joins ----------------------------------------------------------------------------


async def test_overlapping_completions_of_the_same_uploads_join_into_one_batch(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    out = await redeem(door, secret)
    token = out["device_token"]
    uids = [await upload(door, token, JPEG, "a.jpg"), await upload(door, token, HEIC, "b.heic")]
    async with async_session() as blocker:
        await blocker.execute(
            select(PassDevice).where(PassDevice.uid == out["device_id"]).with_for_update()
        )
        first = asyncio.ensure_future(complete(door, token, uids))
        second = asyncio.ensure_future(complete(door, token, list(reversed(uids))))
        await _waiters(2)
        await blocker.commit()
    a, b = await first, await second
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    assert a.json() == b.json()
    assert _sql("SELECT count(*), sum(file_count) FROM ingest_batches") == [(1, 2)]


async def test_a_replayed_completion_answers_the_committed_receipt(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uids = [await upload(door, token, JPEG)]
    first = await complete(door, token, uids)
    again = await complete(door, token, uids)
    assert again.status_code == 200 and again.json() == first.json()
    assert _sql("SELECT count(*) FROM ingest_batches") == [(1,)]


async def test_the_same_bytes_twice_in_one_batch_are_one_file(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uids = [await upload(door, token, JPEG, "a.jpg"), await upload(door, token, JPEG, "copy.jpg")]
    r = await complete(door, token, uids)
    assert r.status_code == 200 and r.json()["file_count"] == 1
    assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]


async def test_an_incomplete_upload_cannot_be_completed(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": len(JPEG)}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]
    await door.patch(
        f"/api/guest/uploads/{uid}",
        content=JPEG[:10],
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert (await complete(door, token, [uid])).status_code == 409


# --- the association file (the page itself: tests/test_ingest_guest_page.py) ----------------------


async def test_the_association_file_names_the_app_and_the_clip_for_pass_links(
    door,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    assert (await door.get("/.well-known/apple-app-site-association")).status_code == 404
    monkeypatch.setattr(settings, "APPLE_TEAM_ID", "TEAM123")
    doc = (await door.get("/.well-known/apple-app-site-association")).json()
    assert doc["applinks"]["details"][0]["appIDs"] == [f"TEAM123.{settings.IOS_BUNDLE_ID}"]
    assert doc["applinks"]["details"][0]["components"] == [{"/": "/p/*"}]
    assert doc["appclips"]["apps"] == [f"TEAM123.{settings.IOS_CLIP_BUNDLE_ID}"]
    assert "webcredentials" not in doc


# --- the volume floor, at create and at every body ---------------------------------------------


async def test_create_counts_what_open_sessions_still_owe_against_the_floor(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "INGEST_MIN_FREE_BYTES", 1000)
    monkeypatch.setattr(core, "free_bytes", lambda _root: 1100)
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": 80}, headers=bearer(token)
    )
    assert r.status_code == 201
    # 80 of the 100 bytes above the floor are already promised to the open session.
    r = await door.post(
        "/api/guest/uploads", json={"filename": "b.jpg", "size": 30}, headers=bearer(token)
    )
    assert r.status_code == 507
    r = await door.post(
        "/api/guest/uploads", json={"filename": "b.jpg", "size": 20}, headers=bearer(token)
    )
    assert r.status_code == 201  # control: exactly what is left


async def test_a_body_that_would_cross_the_floor_is_507_before_any_byte_lands(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": len(JPEG)}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]
    monkeypatch.setattr(settings, "INGEST_MIN_FREE_BYTES", 1000)
    # A 100-byte body costs its spool and then the same bytes again in the part file.
    monkeypatch.setattr(core, "free_bytes", lambda _root: 1000 + 199)
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=JPEG[:100],
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 507
    assert _sql("SELECT bytes_received FROM guest_uploads") == [(0,)]
    assert (ingest_env / "parts" / f"{uid}.part").read_bytes() == b""
    monkeypatch.setattr(core, "free_bytes", lambda _root: 1000 + 200)
    r = await door.patch(
        f"/api/guest/uploads/{uid}",
        content=JPEG[:100],
        headers={**bearer(token), "Upload-Offset": "0"},
    )
    assert r.status_code == 200  # control


# --- blob locks in one global order --------------------------------------------------------------


async def test_a_completion_takes_its_blob_locks_in_hash_order(door, home_iface, ingest_env):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    low, high = sorted([JPEG, HEIC], key=lambda b: hashlib.sha256(b).hexdigest())
    lo_sha, hi_sha = (hashlib.sha256(b).hexdigest() for b in (low, high))
    names = {
        low: "lo.jpg" if low is JPEG else "lo.heic",
        high: "hi.jpg" if high is JPEG else "hi.heic",
    }
    uids = [
        await upload(door, token, high, names[high]),
        await upload(door, token, low, names[low]),
    ]
    # Name the sessions so the rows come back high hash first: upload order is not hash order.
    for old, new in zip(uids, ["a-first", "b-second"], strict=True):
        _sql("UPDATE guest_uploads SET upload_uid = :n WHERE upload_uid = :o", {"n": new, "o": old})
        (ingest_env / "parts" / f"{old}.part").rename(ingest_env / "parts" / f"{new}.part")
    async with async_session() as holder:
        await core.lock_blob_hash(holder, hi_sha)
        task = asyncio.ensure_future(complete(door, token, ["a-first", "b-second"]))
        await _waiters(1)
        async with async_session() as probe:
            free = (
                await probe.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:h, 0))"),
                    {"h": f"blob:{lo_sha}"},
                )
            ).scalar_one()
            await probe.rollback()
        # Parked on the high hash, the completion already holds the low one.
        assert free is False
        await holder.commit()
    assert (await task).status_code == 200


# --- the declared contract, provoked (openapi-guest.json, MEMNG-1011) ---------------------------
# tests/test_openapi_guest.py maps every refusal the guest document declares to the test here that
# makes the route send it; these are the ones no behaviour test above already provoked.


async def test_every_authed_guest_route_is_410_once_its_pass_has_ended(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uid = await upload(door, token, JPEG)
    assert (await door.get("/api/guest/me", headers=bearer(token))).status_code == 200  # control
    _sql("UPDATE passes SET expires_at = now() - interval '1 minute'")
    auth = bearer(token)
    answers = {
        "me": await door.get("/api/guest/me", headers=auth),
        "create": await door.post(
            "/api/guest/uploads", json={"filename": "b.jpg", "size": 10}, headers=auth
        ),
        "offset": await door.head(f"/api/guest/uploads/{uid}", headers=auth),
        "append": await door.patch(
            f"/api/guest/uploads/{uid}", content=b"x", headers={**auth, "Upload-Offset": "0"}
        ),
        "complete": await complete(door, token, [uid]),
        "batches": await door.get("/api/guest/batches", headers=auth),
    }
    assert {name: r.status_code for name, r in answers.items()} == dict.fromkeys(answers, 410)
    assert answers["me"].json() == {"detail": "This pass is no longer open"}
    assert answers["offset"].content == b""  # a HEAD answer has no body to describe


async def test_a_declared_size_over_the_upload_limit_is_413_at_create(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    over = {"filename": "big.jpg", "size": 1024 * 1024 + 1}
    r = await door.post("/api/guest/uploads", json=over, headers=bearer(token))
    assert r.status_code == 413 and r.json() == {"detail": "File exceeds the 1 MiB limit"}
    at = {"filename": "big.jpg", "size": 1024 * 1024}
    assert (
        await door.post("/api/guest/uploads", json=at, headers=bearer(token))
    ).status_code == 201


async def test_every_429_tells_the_client_when_to_retry(door, home_iface, monkeypatch):  # noqa: F811
    _, secret = await seeded_pass(home_iface, max_devices=1)
    token = (await redeem(door, secret))["device_token"]
    body = {"secret": secret, "display_name": "x", "platform": "web"}
    r = await door.post("/api/pass/redeem", json=body)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert r.json() == {"detail": "This pass has no room for another device"}

    # The append's admission is held for real: this device's only body slot is taken first.
    monkeypatch.setattr(settings, "GUEST_BODIES_PER_DEVICE", 1)
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": len(JPEG)}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]
    device_id = _sql("SELECT id FROM pass_devices")[0][0]
    assert core.body_admission.admit(device_id)
    try:
        r = await door.patch(
            f"/api/guest/uploads/{uid}",
            content=JPEG,
            headers={**bearer(token), "Upload-Offset": "0"},
        )
    finally:
        core.body_admission.release(device_id)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(settings.UPLOAD_RETRY_AFTER_SECONDS)
    r = await door.patch(
        f"/api/guest/uploads/{uid}", content=JPEG, headers={**bearer(token), "Upload-Offset": "0"}
    )
    assert r.status_code == 200 and r.headers["Upload-Offset"] == str(len(JPEG))  # control


async def test_the_limits_keep_their_wire_bytes(door, home_iface):  # noqa: F811
    """`limits` became a named model for the contract; what a client reads must not have moved by a
    byte — the same keys, in the same order, compact, on both answers that carry it."""
    wire = (
        f'"limits":{{"max_file_bytes":{settings.MAX_UPLOAD_MIB * 1024 * 1024},'
        f'"chunk_size":{settings.UPLOAD_CHUNK_SIZE},'
        f'"max_patch_bytes":{settings.INGEST_PATCH_MAX_BYTES}}}'
    ).encode()
    _, secret = await seeded_pass(home_iface)
    r = await door.post(
        "/api/pass/redeem", json={"secret": secret, "display_name": "x", "platform": "web"}
    )
    assert r.status_code == 201 and wire in r.content
    me = await door.get("/api/guest/me", headers=bearer(r.json()["device_token"]))
    assert me.status_code == 200 and wire in me.content
