"""The guest door (trip pass P2): redeem, upload sessions, completion into a batch, receipts.

Every case runs the real `ingest` app over ASGI against the test database; passes are pushed
through the real ingest-home interface first. Nothing here mocks a store or the session: the
concurrency cases hold real row locks and wait on `pg_locks` until the requests under test are
parked behind them.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings
from app.database import async_session
from app.ingest import core, guest, service_api
from app.ingest.factory import create_app
from app.models import PassDevice
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

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
from tests.test_lock_protocol_concurrency import Park
from tests.test_uploads import _IDLE, _stalling_receive, _until_silent, _within_the_bound


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


async def test_a_lifted_pass_counts_only_the_refusals_after_the_lift(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "INGEST_REFUSALS_PER_HOUR", 3)
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", True)
    uid, secret = await seeded_pass(home_iface, max_devices=1)
    token = (await redeem(door, secret, **{"CF-Connecting-IP": "192.0.2.1"}))["device_token"]
    body = {"secret": secret, "display_name": "x", "platform": "web"}

    async def refused(first: int, n: int) -> None:
        for i in range(first, first + n):  # each from its own address, the pass full
            r = await door.post(
                "/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"192.0.2.{i}"}
            )
            assert r.status_code == 429

    await refused(10, 4)
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)]
    out = await push(
        home_iface, pass_state(uid, secret, version=2, max_devices=1, unsuspend_version=2)
    )
    assert out[0]["suspended_at"] is None
    assert (await door.get("/api/guest/me", headers=bearer(token))).status_code == 200
    # The four refusals of the last hour were the suspension's; the lift starts the count over.
    await refused(20, 3)
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)]
    await refused(30, 1)
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)]


async def test_a_lift_in_the_legacy_form_still_spends_the_refusals_before_it(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    """MEMNG-1180: this host now writes the `unsuspended` kind, but a lift an older one recorded
    as `updated` + `core.LIFTED` stays in the table and must still start the count over."""
    _, _, body = await _a_suspended_pass(door, home_iface, monkeypatch)
    _sql(
        "WITH lifted AS (UPDATE passes SET suspended_at = NULL RETURNING id, version) "
        "INSERT INTO pass_events (pass_id, kind, version, detail) "
        "SELECT id, 'updated', version, :d FROM lifted",
        {"d": core.LIFTED},
    )
    await _refused(door, body, 20, 3)
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)], "a spent refusal counted"
    await _refused(door, body, 30, 1)
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)]


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


LAST_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z, the last instant a datetime holds
FIRST_MS = -62_135_596_800_000  # 0001-01-01T00:00:00Z


@pytest.mark.parametrize("ms", [LAST_MS + 1, 10**20, FIRST_MS - 1])
async def test_a_last_modified_no_datetime_holds_is_422_and_leaves_no_part_file(
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    ms,
):
    # Answers, not raises: a crash in the door must show here as the 500 a guest would get.
    async with AsyncClient(
        transport=ASGITransport(app=create_app("ingest"), raise_app_exceptions=False),
        base_url="http://door",
    ) as answering:
        _, secret = await seeded_pass(home_iface)
        token = (await redeem(answering, secret))["device_token"]
        body = {"filename": "a.jpg", "size": 5, "last_modified": ms}
        r = await answering.post("/api/guest/uploads", json=body, headers=bearer(token))
        assert r.status_code == 422, r.text
        assert sorted(ingest_env.rglob("*.part")) == []
        assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]
        # Control: each edge itself is a time, and is kept.
        for edge in (LAST_MS, FIRST_MS):
            body["last_modified"] = edge
            r = await answering.post("/api/guest/uploads", json=body, headers=bearer(token))
            assert r.status_code == 201, r.text
        assert len(sorted(ingest_env.rglob("*.part"))) == 2
        assert _sql("SELECT count(*) FROM guest_uploads WHERE client_mtime IS NOT NULL") == [(2,)]


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


async def test_a_revoked_device_is_shut_out_while_its_sibling_keeps_uploading(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    pass_uid, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    ben = await redeem(door, secret, "Ben")
    sessions = {}
    for out in (anna, ben):
        r = await door.post(
            "/api/guest/uploads",
            json={"filename": "a.jpg", "size": len(JPEG)},
            headers=bearer(out["device_token"]),
        )
        assert r.status_code == 201, r.text
        sessions[out["device_id"]] = r.json()["upload_uid"]
    revoked = await home_iface.put(
        "/ingest/device-revocations",
        json={
            "revocations": [
                {
                    "device_id": anna["device_id"],
                    "pass_uid": pass_uid,
                    "version": 2,
                    "revoked_at": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert [a["device_id"] for a in revoked.json()["applied"]] == [anna["device_id"]]
    # The revoked device is refused before a byte lands.
    r = await door.patch(
        f"/api/guest/uploads/{sessions[anna['device_id']]}",
        content=JPEG,
        headers={**bearer(anna["device_token"]), "Upload-Offset": "0"},
    )
    assert r.status_code == 401
    assert (ingest_env / "parts" / f"{sessions[anna['device_id']]}.part").read_bytes() == b""
    # Its sibling on the same pass keeps uploading, and its bytes land.
    ben_uid = sessions[ben["device_id"]]
    r = await door.patch(
        f"/api/guest/uploads/{ben_uid}",
        content=JPEG,
        headers={**bearer(ben["device_token"]), "Upload-Offset": "0"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["bytes_received"] == len(JPEG)
    assert (ingest_env / "parts" / f"{ben_uid}.part").read_bytes() == JPEG
    done = await complete(door, ben["device_token"], [ben_uid])
    assert done.status_code == 200, done.text
    sha = hashlib.sha256(JPEG).hexdigest()
    assert (ingest_env / "blobs" / sha[:2] / sha).read_bytes() == JPEG
    anna_uid = sessions[anna["device_id"]]
    assert (await complete(door, anna["device_token"], [anna_uid])).status_code == 401
    assert _sql(
        "SELECT display_name, revoked_at IS NOT NULL, revoked_version "
        "FROM pass_devices ORDER BY display_name"
    ) == [("Anna", True, 2), ("Ben", False, None)]


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
    # Excludes first: Apple takes the first component that matches.
    assert doc["applinks"]["details"][0]["components"] == [
        {"/": "/p/", "exclude": True},
        {"/": "/p/*/*", "exclude": True},
        {"/": "/p/joined", "exclude": True},
        {"/": "/p/*"},
    ]
    assert doc["appclips"]["apps"] == [f"TEAM123.{settings.IOS_CLIP_BUNDLE_ID}"]
    assert "webcredentials" not in doc


def _apple_opens_app(components: list[dict], path: str) -> bool:
    """Apple's documented matching of an applinks `components` list against a URL path: the
    components are tried in order and the first whose `/` pattern matches the whole path decides
    (`exclude: true` hands the URL to the browser); no match leaves it with the browser. In a
    pattern `*` matches any run of characters, the empty run and `/` included, and `?` matches
    exactly one character."""
    for component in components:
        pattern = "".join(
            ".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in component["/"]
        )
        if re.fullmatch(pattern, path, flags=re.DOTALL):
            return not component.get("exclude", False)
    return False


def _check_the_yardstick() -> None:
    # The yardstick itself, against the semantics it claims: `*` spans `/` and the empty run,
    # `?` is exactly one character, and the first matching component wins. Checked inside the
    # served-document test below rather than as a test of its own, so every assertion here guards
    # a verdict that test reaches about the route's real answer.
    assert _apple_opens_app([{"/": "/p/*"}], "/p/a/b")
    assert _apple_opens_app([{"/": "/p/*"}], "/p/")
    assert _apple_opens_app([{"/": "/p/?"}], "/p/a")
    assert not _apple_opens_app([{"/": "/p/?"}], "/p/ab")
    assert not _apple_opens_app([{"/": "/p/?"}], "/p/")
    assert not _apple_opens_app([{"/": "/p/*", "exclude": True}, {"/": "/p/*"}], "/p/a")
    assert _apple_opens_app([{"/": "/p/*"}, {"/": "/p/*", "exclude": True}], "/p/a")
    assert not _apple_opens_app([], "/p/a")
    # Every other character is literal: a `.` in a pattern is not a regex wildcard.
    assert _apple_opens_app([{"/": "/a.b"}], "/a.b")
    assert not _apple_opens_app([{"/": "/a.b"}], "/axb")


@pytest.mark.parametrize(
    ("path", "opens_app"),
    [
        ("/p/abc", True),  # a real pass link
        ("/p/", False),  # the empty secret
        ("/p/x/extra", False),  # a second segment
        ("/p/x/", False),  # a trailing slash: `/p/*/*` with an empty last segment
        ("/p/joined", False),  # the guest page after a redeem, not a pass
        ("/", False),
        ("/other", False),
    ],
)
async def test_only_a_single_segment_pass_path_opens_the_app(
    door,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
    path,
    opens_app,
):
    monkeypatch.setattr(settings, "APPLE_TEAM_ID", "TEAM123")
    doc = (await door.get("/.well-known/apple-app-site-association")).json()
    components = doc["applinks"]["details"][0]["components"]
    _check_the_yardstick()
    assert _apple_opens_app(components, path) is opens_app


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
        "rename": await door.patch(
            "/api/guest/device", json={"display_name": "Anna B"}, headers=auth
        ),
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


async def _door_patch(token: str, uid: str, offset: int, content_length: int, receive):
    """A guest PATCH through the complete door app with the caller's own ASGI `receive`, so a test
    can send what httpx cannot: a body that stalls on an open connection."""
    path = f"/api/guest/uploads/{uid}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PATCH",
        "headers": [
            (b"host", b"door"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"upload-offset", str(offset).encode()),
            (b"content-type", b"application/offset+octet-stream"),
            (b"content-length", str(content_length).encode()),
        ],
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "server": ("door", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }
    sent = []

    async def send(message):
        sent.append(message)

    await create_app("ingest")(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


@pytest.mark.parametrize("sent", ["half", "nothing"])
async def test_an_idle_guest_body_ends_at_the_bound_and_frees_the_device_slot(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
    sent,
):
    """MEMNG-1158: the guest append holds one of its device's body slots while the body streams.
    A body that goes silent on an open connection is cut at the idle bound like the account
    route's — bytes kept, 200 with the offset reached — and the slot is free again, instead of
    held until Traefik's read timeout. `tests/mutations/guest_body_idle_unbounded.patch` removes
    the bound."""
    monkeypatch.setattr(settings, "UPLOAD_BODY_IDLE_SECONDS", _IDLE)
    # One slot per device, so a slot the stalled body kept would answer the next PATCH 429.
    monkeypatch.setattr(settings, "GUEST_BODIES_PER_DEVICE", 1)
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": len(JPEG)}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]
    # A body silent from its first byte is the boundary: nothing to keep, and the slot still freed.
    half = len(JPEG) // 2 if sent == "half" else 0
    pieces = [JPEG[:half]] if half else []
    silent = asyncio.Event()
    stalled = asyncio.create_task(
        _door_patch(token, uid, 0, len(JPEG), _stalling_receive(pieces, silent))
    )
    try:
        await _until_silent(stalled, silent)
        # Not vacuous: while the stalled body holds the device's one slot, a second body is refused.
        early = await door.patch(
            f"/api/guest/uploads/{uid}",
            content=JPEG,
            headers={**bearer(token), "Upload-Offset": "0"},
        )
        assert early.status_code == 429

        elapsed = await _within_the_bound(stalled)
        assert elapsed >= _IDLE * 0.5  # ended by the bound, not by something earlier
        status, out = stalled.result()
        assert (status, out["bytes_received"]) == (200, half)
        assert _sql("SELECT bytes_received FROM guest_uploads") == [(half,)]

        # The slot is free: the same device's next PATCH is admitted and completes the file.
        rest = await door.patch(
            f"/api/guest/uploads/{uid}",
            content=JPEG[half:],
            headers={**bearer(token), "Upload-Offset": str(half)},
        )
        assert rest.status_code == 200, rest.text
        assert rest.json()["bytes_received"] == len(JPEG)
    finally:
        if not stalled.done():
            stalled.cancel()
        await asyncio.gather(stalled, return_exceptions=True)


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


async def test_a_refusal_stamped_after_the_lift_but_committed_before_it_stays_spent(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    """MEMNG-1082 #1: `at` is a transaction-start now(), so it can invert against the lift. Order by
    event id: the pre-lift refusals stay spent whatever they are stamped, and later ones count."""
    monkeypatch.setattr(settings, "INGEST_REFUSALS_PER_HOUR", 3)
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", True)
    uid, secret = await seeded_pass(home_iface, max_devices=1)
    await redeem(door, secret, **{"CF-Connecting-IP": "192.0.2.1"})
    body = {"secret": secret, "display_name": "x", "platform": "web"}

    async def refused(first: int, n: int) -> None:
        for i in range(first, first + n):
            r = await door.post(
                "/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"192.0.2.{i}"}
            )
            assert r.status_code == 429

    await refused(10, 4)
    await push(home_iface, pass_state(uid, secret, version=2, max_devices=1, unsuspend_version=2))
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)]
    _sql("UPDATE pass_events SET at = now() + interval '1 minute' WHERE kind = 'refused'")
    await refused(20, 3)
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)], "spent refusals counted"
    await refused(30, 1)
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)]


async def _a_suspended_pass(door, home_iface, monkeypatch) -> tuple[str, str, dict]:  # noqa: F811
    monkeypatch.setattr(settings, "INGEST_REFUSALS_PER_HOUR", 3)
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", True)
    uid, secret = await seeded_pass(home_iface, max_devices=1)
    await redeem(door, secret, **{"CF-Connecting-IP": "192.0.2.1"})
    body = {"secret": secret, "display_name": "x", "platform": "web"}
    for i in range(10, 14):
        await door.post("/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"192.0.2.{i}"})
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)]
    return uid, secret, body


async def _refused(door, body, first: int, n: int) -> None:  # noqa: F811
    for i in range(first, first + n):
        r = await door.post(
            "/api/pass/redeem", json=body, headers={"CF-Connecting-IP": f"192.0.2.{i}"}
        )
        assert r.status_code == 429, r.text


async def test_a_refusal_holding_the_pass_lock_commits_before_the_lift_and_stays_spent(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    """MEMNG-1082 #1: parked with its event written and uncommitted, a refusal holds the pass row;
    the lift beside it waits, so its event takes the later id and the refusal is spent."""
    uid, secret, body = await _a_suspended_pass(door, home_iface, monkeypatch)
    park, real = Park(), guest.record_refusal

    async def parked(*args, **kwargs) -> None:
        await real(*args, **kwargs)
        if park.claim():
            await park.hold()

    monkeypatch.setattr(guest, "record_refusal", parked)
    refusal = asyncio.create_task(
        door.post("/api/pass/redeem", json=body, headers={"CF-Connecting-IP": "192.0.2.20"})
    )
    await asyncio.wait_for(park.entered.wait(), timeout=30)
    lift = asyncio.create_task(
        push(home_iface, pass_state(uid, secret, version=2, max_devices=1, unsuspend_version=2))
    )
    await _waiters(1)
    assert not lift.done(), "the lift did not wait for the refusal's pass lock"
    park.release.set()
    await refusal
    await lift
    park.fired()
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)]
    assert _sql(
        "SELECT (SELECT max(id) FROM pass_events WHERE kind = 'refused')"
        " < (SELECT max(id) FROM pass_events WHERE detail = :d)",
        {"d": core.LIFTED},
    ) == [(True,)]
    await _refused(door, body, 30, 3)
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)], "a spent refusal counted"
    await _refused(door, body, 40, 1)
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)]


async def test_a_refusal_waiting_on_the_lift_commits_after_it_and_counts(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    """MEMNG-1082 #1: the other order. The lift holds the pass row; the refusal queues behind it,
    so its id is later and it counts toward the next suspension."""
    uid, secret, body = await _a_suspended_pass(door, home_iface, monkeypatch)
    park, real = Park(), service_api.apply_pass_state

    async def parked(*args, **kwargs):
        result = await real(*args, **kwargs)
        if park.claim():
            await park.hold()
        return result

    monkeypatch.setattr(service_api, "apply_pass_state", parked)
    lift = asyncio.create_task(
        push(home_iface, pass_state(uid, secret, version=2, max_devices=1, unsuspend_version=2))
    )
    await asyncio.wait_for(park.entered.wait(), timeout=30)
    refusal = asyncio.create_task(
        door.post("/api/pass/redeem", json=body, headers={"CF-Connecting-IP": "192.0.2.20"})
    )
    await _waiters(1)
    assert not refusal.done(), "the refusal did not wait for the lift's pass lock"
    park.release.set()
    await lift
    assert (await refusal).status_code == 429
    park.fired()
    await _refused(door, body, 30, 2)
    assert _sql("SELECT suspended_at IS NULL FROM passes") == [(True,)]
    await _refused(door, body, 40, 1)
    assert _sql("SELECT suspended_at IS NOT NULL FROM passes") == [(True,)], "it did not count"


async def test_a_create_that_fails_at_the_flush_leaves_no_part(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    """MEMNG-1082 #6: the part comes after everything that can raise short of the commit. Made
    first, a failed flush leaves a part with no row to find it by. The refusal is the database's."""
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    _sql(
        "CREATE FUNCTION r213_refuse_upload() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'refused by the test'; END $$"
    )
    _sql(
        "CREATE TRIGGER r213_refuse_upload BEFORE INSERT ON guest_uploads "
        "FOR EACH ROW EXECUTE FUNCTION r213_refuse_upload()"
    )
    try:
        with pytest.raises(DBAPIError, match="refused by the test"):
            await door.post(
                "/api/guest/uploads", json={"filename": "a.jpg", "size": 5}, headers=bearer(token)
            )
    finally:
        _sql("DROP TRIGGER r213_refuse_upload ON guest_uploads")
        _sql("DROP FUNCTION r213_refuse_upload()")
    parts = ingest_env / "parts"
    assert not parts.exists() or list(parts.iterdir()) == []
