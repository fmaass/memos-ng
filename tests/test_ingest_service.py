"""The home interface (trip pass P2): pushed pass state, device revocations, pulls, transitions,
blob download and retention. Driven over ASGI with the real service token."""

import asyncio
import hashlib
import threading
from datetime import UTC, datetime

import pytest
from app.database import async_session
from app.ingest import guest as guest_module
from app.ingest import service_api
from sqlalchemy import text

from tests.ingest_support import (
    HEIC,
    JPEG,
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
from tests.test_ingest_guest import _waiters


def _revocation(device_id: str, pass_uid: str, version: int) -> dict:
    return {
        "device_id": device_id,
        "pass_uid": pass_uid,
        "version": version,
        "revoked_at": datetime.now(UTC).isoformat(),
    }


# --- pushed pass state -------------------------------------------------------------------------


async def test_a_pushed_pass_is_an_ingest_projection_with_no_home_references(home_iface):  # noqa: F811
    uid, _ = await seeded_pass(home_iface)
    rows = _sql(
        "SELECT collection_id, created_by, revoked_by, version FROM passes WHERE uid=:u", {"u": uid}
    )
    assert rows == [(None, None, None, 1)]


async def test_a_push_lands_only_when_its_version_is_higher(home_iface):  # noqa: F811
    uid, secret = "p-order", "s" * 43
    out = await push(home_iface, pass_state(uid, secret, version=2, label="two"))
    assert out[0] == {"uid": uid, "version": 2, "applied": True, "suspended_at": None}
    out = await push(home_iface, pass_state(uid, secret, version=1, label="one"))
    assert out[0]["applied"] is False and out[0]["version"] == 2
    out = await push(home_iface, pass_state(uid, secret, version=2, label="again"))
    assert out[0]["applied"] is False
    assert _sql("SELECT label FROM passes WHERE uid=:u", {"u": uid}) == [("two",)]
    out = await push(home_iface, pass_state(uid, secret, version=3, label="three"))
    assert out[0]["applied"] is True and _sql("SELECT label FROM passes") == [("three",)]


async def test_a_revoked_pass_is_never_reopened_by_a_later_push(door, home_iface):  # noqa: F811
    uid, secret = "p-rev", "r" * 43
    now = datetime.now(UTC).isoformat()
    await push(home_iface, pass_state(uid, secret, version=2, revoked_at=now, revoked_version=2))
    out = await push(home_iface, pass_state(uid, secret, version=3, label="later"))
    assert out[0]["applied"] is True
    assert _sql("SELECT revoked_version, label FROM passes") == [(2, "later")]
    r = await door.post(
        "/api/pass/redeem", json={"secret": secret, "display_name": "x", "platform": "web"}
    )
    assert r.status_code == 410


# --- device revocations --------------------------------------------------------------------------


async def test_a_device_revocation_at_the_version_already_pushed_still_applies(door, home_iface):  # noqa: F811
    uid, secret = await seeded_pass(home_iface)
    out = await redeem(door, secret)
    # Home revoked the device at pass version 2 and its pass push of v2 arrived FIRST.
    await push(home_iface, pass_state(uid, secret, version=2))
    r = await home_iface.put(
        "/ingest/device-revocations", json={"revocations": [_revocation(out["device_id"], uid, 2)]}
    )
    ack = r.json()
    assert r.status_code == 200 and ack["highest_version"] == 2 and ack["unknown"] == []
    assert [a["device_id"] for a in ack["applied"]] == [out["device_id"]]
    assert _sql("SELECT revoked_version FROM pass_devices") == [(2,)]
    assert (await door.get("/api/guest/me", headers=bearer(out["device_token"]))).status_code == 401
    # Replayed: still acknowledged, still revoked; a later pass push never clears it.
    r = await home_iface.put(
        "/ingest/device-revocations", json={"revocations": [_revocation(out["device_id"], uid, 2)]}
    )
    assert r.json()["highest_version"] == 2
    await push(home_iface, pass_state(uid, secret, version=3))
    assert _sql("SELECT revoked_version, revoked_at IS NOT NULL FROM pass_devices") == [(2, True)]


async def test_only_revocations_that_now_stand_are_acknowledged(door, home_iface):  # noqa: F811
    uid, secret = await seeded_pass(home_iface)
    other_uid, other_secret = await seeded_pass(home_iface)
    mine = await redeem(door, secret)
    theirs = await redeem(door, other_secret)
    r = await home_iface.put(
        "/ingest/device-revocations",
        json={
            "revocations": [
                _revocation(mine["device_id"], uid, 4),
                _revocation("no-such-device", uid, 9),
                _revocation(theirs["device_id"], uid, 7),  # a device of ANOTHER pass
            ]
        },
    )
    ack = r.json()
    assert [a["device_id"] for a in ack["applied"]] == [mine["device_id"]]
    assert sorted(ack["unknown"]) == sorted(["no-such-device", theirs["device_id"]])
    assert ack["highest_version"] == 4
    assert (
        await door.get("/api/guest/me", headers=bearer(theirs["device_token"]))
    ).status_code == 200


# --- pulls -------------------------------------------------------------------------------------


async def test_the_pulls_page_by_cursor_in_commit_order(door, home_iface):  # noqa: F811
    uid, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    ben = await redeem(door, secret, "Ben")
    page = (await home_iface.get("/ingest/devices", params={"since": 0, "limit": 1})).json()
    assert [d["display_name"] for d in page["devices"]] == ["Anna"]
    page = (await home_iface.get("/ingest/devices", params={"since": page["next"]})).json()
    assert [d["display_name"] for d in page["devices"]] == ["Ben"]
    cursor = page["next"]
    assert (await home_iface.get("/ingest/devices", params={"since": cursor})).json()[
        "devices"
    ] == []
    receipt = (
        await complete(
            door, anna["device_token"], [await upload(door, anna["device_token"], JPEG)], "hi"
        )
    ).json()
    # Anna's counters moved with the batch, so she comes back after the cursor.
    page = (await home_iface.get("/ingest/devices", params={"since": cursor})).json()
    assert [(d["display_name"], d["uploads_count"]) for d in page["devices"]] == [("Anna", 1)]
    batches = (await home_iface.get("/ingest/batches", params={"since": 0})).json()
    assert len(batches["batches"]) == 1
    b = batches["batches"][0]
    assert (
        b["batch_id"],
        b["pass_uid"],
        b["device_id"],
        b["display_name"],
        b["platform"],
        b["caption"],
    ) == (
        receipt["batch_id"],
        uid,
        anna["device_id"],
        "Anna",
        "web",
        "hi",
    )
    assert b["blobs"] == [
        {
            "sha256": hashlib.sha256(JPEG).hexdigest(),
            "size": len(JPEG),
            "mime": "image/jpeg",
            "filename": "a.jpg",
            "last_modified": None,
        }
    ]
    assert (await home_iface.get("/ingest/batches", params={"since": batches["next"]})).json()[
        "batches"
    ] == []
    assert ben["device_id"] != anna["device_id"]


async def test_a_blob_is_served_by_hash_with_range_to_home_only(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    await complete(door, token, [await upload(door, token, HEIC, "x.heic")])
    sha = hashlib.sha256(HEIC).hexdigest()
    full = await home_iface.get(f"/ingest/blobs/{sha}")
    assert full.status_code == 200 and full.content == HEIC
    part = await home_iface.get(f"/ingest/blobs/{sha}", headers={"Range": "bytes=4-11"})
    assert part.status_code == 206 and part.content == HEIC[4:12]
    assert (await home_iface.get(f"/ingest/blobs/{'0' * 64}")).status_code == 404
    assert (await home_iface.get(f"/ingest/blobs/{sha.upper()}")).status_code == 404
    assert (await door.get(f"/ingest/blobs/{sha}", headers=bearer(token))).status_code == 404


# --- transitions and retention -------------------------------------------------------------------


async def _batch(door, home_iface, data: bytes, name: str = "a.jpg") -> str:  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    return (await complete(door, token, [await upload(door, token, data, name)])).json()["batch_id"]


async def test_ack_and_quarantine_are_idempotent_and_quarantine_is_sticky(door, home_iface):  # noqa: F811
    batch = await _batch(door, home_iface, JPEG)
    assert (await home_iface.post(f"/ingest/batches/{batch}/ack")).json()["state"] == "acked"
    assert (await home_iface.post(f"/ingest/batches/{batch}/ack")).status_code == 200
    q = await home_iface.post(f"/ingest/batches/{batch}/quarantine")
    assert q.json()["state"] == "quarantined"
    assert (await home_iface.post(f"/ingest/batches/{batch}/ack")).status_code == 409
    assert (await home_iface.post("/ingest/batches/nope/ack")).status_code == 404


def _age(batch: str, days: int) -> None:
    _sql(
        "UPDATE ingest_batches SET acked_at = now() - make_interval(days => :d) WHERE uid = :u",
        {"d": days, "u": batch},
    )


async def test_purge_keeps_bytes_another_batch_still_names_and_never_touches_unacked(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    shared = await _batch(door, home_iface, JPEG)
    twin = await _batch(door, home_iface, JPEG)  # the same bytes, another pass's guest
    held = await _batch(door, home_iface, HEIC, "h.heic")
    blob = (
        ingest_env
        / "blobs"
        / hashlib.sha256(JPEG).hexdigest()[:2]
        / hashlib.sha256(JPEG).hexdigest()
    )
    for batch in (shared, held):
        await home_iface.post(f"/ingest/batches/{batch}/ack")
    await home_iface.post(f"/ingest/batches/{held}/quarantine")
    _age(shared, 8)
    _age(held, 30)
    _sql(
        "UPDATE ingest_batches SET completed_at = now() - interval '60 days' WHERE uid = :u",
        {"u": twin},
    )
    result = await service_api.purge()
    assert result["batches"] == 1 and result["files"] == 0
    assert sorted(r[0] for r in _sql("SELECT uid FROM ingest_batches")) == sorted([twin, held])
    assert blob.read_bytes() == JPEG  # the unacked twin still names it
    await home_iface.post(f"/ingest/batches/{twin}/ack")
    _age(twin, 6)
    assert (await service_api.purge())["batches"] == 0  # inside the window
    _age(twin, 8)
    assert (await service_api.purge()) == {"batches": 1, "sessions": 0, "files": 1}
    assert not blob.exists()
    assert _sql("SELECT uid FROM ingest_batches") == [(held,)]  # quarantined: never purged


async def test_purge_reaps_expired_upload_sessions_and_their_parts(door, home_iface, ingest_env):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": 5}, headers=bearer(token)
    )
    uid = r.json()["upload_uid"]
    keep = (
        await door.post(
            "/api/guest/uploads", json={"filename": "b.jpg", "size": 5}, headers=bearer(token)
        )
    ).json()["upload_uid"]
    _sql(
        "UPDATE guest_uploads SET expires_at = now() - interval '1 minute' WHERE upload_uid = :u",
        {"u": uid},
    )
    assert (await service_api.purge())["sessions"] == 1
    assert not (ingest_env / "parts" / f"{uid}.part").exists()
    assert (ingest_env / "parts" / f"{keep}.part").exists()


@pytest.mark.parametrize("path", ["/ingest/passes", "/ingest/device-revocations"])
async def test_a_push_without_the_service_token_changes_nothing(home_iface, path):  # noqa: F811
    home_iface.headers.pop("Authorization", None)
    body = {"passes": [pass_state("x", "y")]} if path.endswith("passes") else {"revocations": []}
    assert (await home_iface.put(path, json=body)).status_code == 401
    assert _sql("SELECT count(*) FROM passes") == [(0,)]


async def test_a_quarantine_landing_while_purge_waits_keeps_the_batch_and_its_bytes(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    batch = await _batch(door, home_iface, JPEG)
    await home_iface.post(f"/ingest/batches/{batch}/ack")
    _age(batch, 8)
    sha = hashlib.sha256(JPEG).hexdigest()
    async with async_session() as quarantine:
        # The quarantine holds the row while purge runs; it commits only once purge is parked.
        await quarantine.execute(
            text("SELECT 1 FROM ingest_batches WHERE uid = :u FOR UPDATE"), {"u": batch}
        )
        task = asyncio.ensure_future(service_api.purge())
        await _waiters(1)
        await quarantine.execute(
            text(
                "UPDATE ingest_batches SET state = 'quarantined', quarantined_at = now() "
                "WHERE uid = :u"
            ),
            {"u": batch},
        )
        await quarantine.commit()
    result = await task
    assert result["batches"] == 0 and result["files"] == 0
    assert _sql("SELECT state FROM ingest_batches") == [("quarantined",)]
    assert (ingest_env / "blobs" / sha[:2] / sha).read_bytes() == JPEG


async def test_devices_revoked_together_all_come_back_one_page_at_a_time(door, home_iface):  # noqa: F811
    uid, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    ben = await redeem(door, secret, "Ben")
    cursor = (await home_iface.get("/ingest/devices", params={"since": 0})).json()["next"]
    await home_iface.put(
        "/ingest/device-revocations",
        json={
            "revocations": [
                _revocation(anna["device_id"], uid, 2),
                _revocation(ben["device_id"], uid, 2),
            ]
        },
    )
    seen = []
    for _ in range(4):
        page = (
            await home_iface.get("/ingest/devices", params={"since": cursor, "limit": 1})
        ).json()
        seen += [d["device_id"] for d in page["devices"]]
        cursor = page["next"]
    assert sorted(seen) == sorted([anna["device_id"], ben["device_id"]])


def _parked(real):
    """Wrap a file operation so it waits in its worker thread until released."""
    entered, release = threading.Event(), threading.Event()

    def parked(*args):
        entered.set()
        release.wait(10)
        return real(*args)

    return parked, entered, release


async def _hash_lock_free(sha: str) -> bool:
    async with async_session() as probe:
        free = (
            await probe.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:h, 0))"),
                {"h": f"blob:{sha}"},
            )
        ).scalar_one()
        await probe.rollback()
    return bool(free)


async def _held_for(sha: str, seconds: float = 0.5) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await _hash_lock_free(sha):
            return False
        await asyncio.sleep(0.05)
    return True


async def test_a_cancelled_purge_keeps_the_hash_lock_until_its_unlink_returns(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    batch = await _batch(door, home_iface, JPEG)
    await home_iface.post(f"/ingest/batches/{batch}/ack")
    _age(batch, 8)
    sha = hashlib.sha256(JPEG).hexdigest()
    parked, entered, release = _parked(service_api._unlink_sync)
    monkeypatch.setattr(service_api, "_unlink_sync", parked)
    sweep = asyncio.ensure_future(service_api.purge())
    assert await asyncio.to_thread(entered.wait, 10)
    sweep.cancel()  # what the lifespan does at shutdown
    # The unlink thread is still running: a completion must not get the hash meanwhile.
    assert await _held_for(sha)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await sweep
    assert await _hash_lock_free(sha)
    # After it, the same bytes completed again are present AND named.
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    assert (await complete(door, token, [await upload(door, token, JPEG)])).status_code == 200
    got = await home_iface.get(f"/ingest/blobs/{sha}")
    assert got.status_code == 200 and got.content == JPEG


async def test_a_cancelled_completion_keeps_the_hash_lock_until_its_link_returns(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    uid = await upload(door, token, JPEG)
    sha = hashlib.sha256(JPEG).hexdigest()
    real_link = guest_module._link_blob_sync
    parked, entered, release = _parked(real_link)
    monkeypatch.setattr(guest_module, "_link_blob_sync", parked)
    task = asyncio.ensure_future(complete(door, token, [uid]))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    assert await _held_for(sha)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _hash_lock_free(sha)
    # Rolled back whole: no batch, the session still there to retry, and the retry files it.
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]
    monkeypatch.setattr(guest_module, "_link_blob_sync", real_link)
    assert (await complete(door, token, [uid])).status_code == 200


async def test_a_bulk_push_locks_its_passes_in_uid_order(home_iface):  # noqa: F811
    await push(home_iface, pass_state("pass-a", "a" * 43), pass_state("pass-b", "b" * 43))
    async with async_session() as holder:
        await holder.execute(text("SELECT 1 FROM passes WHERE uid = 'pass-b' FOR UPDATE"))
        task = asyncio.ensure_future(
            push(
                home_iface,
                pass_state("pass-b", "b" * 43, version=2),
                pass_state("pass-a", "a" * 43, version=2),
            )
        )
        await _waiters(1)
        async with async_session() as probe:
            got = (
                await probe.execute(
                    text("SELECT 1 FROM passes WHERE uid = 'pass-a' FOR UPDATE SKIP LOCKED")
                )
            ).all()
            await probe.rollback()
        # Parked on pass-b, the push already holds pass-a: every bulk push meets in one order.
        assert got == []
        await holder.commit()
    out = await task
    assert [(r["uid"], r["applied"]) for r in out] == [("pass-b", True), ("pass-a", True)]
