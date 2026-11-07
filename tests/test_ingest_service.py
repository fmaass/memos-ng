"""The home interface (trip pass P2): pushed pass state, device revocations, pulls, transitions,
blob download and retention. Driven over ASGI with the real service token."""

import asyncio
import hashlib
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from app.config import settings
from app.database import async_session
from app.database import engine as door_engine
from app.ingest import guest as guest_module
from app.ingest import service_api
from sqlalchemy import event, text

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


def _suspend(uid: str) -> None:
    _sql("UPDATE passes SET suspended_at = now() WHERE uid = :u", {"u": uid})


async def test_a_pushed_lift_clears_a_suspension_standing_before_it(home_iface):  # noqa: F811
    uid, secret = "p-lift", "s" * 43
    await push(home_iface, pass_state(uid, secret))
    _suspend(uid)
    out = await push(home_iface, pass_state(uid, secret, version=2, label="edited"))
    assert out[0]["applied"] is True and out[0]["suspended_at"] is not None  # an edit lifts nothing
    out = await push(home_iface, pass_state(uid, secret, version=3, unsuspend_version=3))
    assert out[0] == {"uid": uid, "version": 3, "applied": True, "suspended_at": None}
    # The dedicated kind, never the legacy `updated` + detail form, and only on the lift: the
    # `pushed` rows beside it stay `updated` (MEMNG-1180).
    assert _sql(
        "SELECT kind, version, detail FROM pass_events "
        "WHERE (kind = 'unsuspended' OR detail = 'unsuspended') "
        "AND pass_id = (SELECT id FROM passes WHERE uid = :u)",
        {"u": uid},
    ) == [("unsuspended", 3, "unsuspended")]


async def test_a_stale_push_never_undoes_a_newer_state(home_iface):  # noqa: F811
    uid, secret = "p-stale", "s" * 43
    await push(home_iface, pass_state(uid, secret))
    _suspend(uid)
    out = await push(home_iface, pass_state(uid, secret, version=2, unsuspend_version=2))
    assert out[0]["suspended_at"] is None
    _suspend(uid)  # suspended again after the lift
    # The lift delivered twice, and a push older than it, arriving now: neither lands.
    for state in (
        pass_state(uid, secret, version=2, unsuspend_version=2),
        pass_state(uid, secret, version=1, label="older"),
    ):
        out = await push(home_iface, state)
        assert out[0]["applied"] is False and out[0]["suspended_at"] is not None
    assert _sql("SELECT version, label FROM passes WHERE uid = :u", {"u": uid}) == [
        (2, "Sardinia friends")
    ]


async def test_a_suspension_after_the_lift_survives_pushes_carrying_that_lift(home_iface):  # noqa: F811
    uid, secret = "p-again", "s" * 43
    await push(home_iface, pass_state(uid, secret))
    _suspend(uid)
    await push(home_iface, pass_state(uid, secret, version=2, unsuspend_version=2))
    _suspend(uid)
    # Every later push carries the newest lift; the ingest host already holds it.
    out = await push(home_iface, pass_state(uid, secret, version=3, unsuspend_version=2))
    assert out[0]["applied"] is True and out[0]["suspended_at"] is not None
    # Only a new lift (a version above what the host holds) clears it again.
    out = await push(home_iface, pass_state(uid, secret, version=4, unsuspend_version=4))
    assert out[0]["applied"] is True and out[0]["suspended_at"] is None


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


# --- the feeds' high-water mark (MEMNG-1205) ----------------------------------------------------


def _sequence_last_value() -> tuple[int, bool]:
    return _sql("SELECT last_value, is_called FROM ingest_change_seq")[0]


@pytest.mark.parametrize(("route", "key"), [("devices", "devices"), ("batches", "batches")])
async def test_both_feeds_report_the_sequence_head_and_reading_it_draws_nothing(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    route,
    key,
):
    await _batch(door, home_iface, JPEG)
    drawn = _sql("SELECT nextval('ingest_change_seq')")[0][
        0
    ]  # above every row: not a max over them
    for since in (0, drawn):
        page = (await home_iface.get(f"/ingest/{route}", params={"since": since})).json()
        assert page["head"] == drawn
        assert set(page) == {key, "next", "head"}  # home up to 2.13.0 reads two keys by name
    assert _sequence_last_value() == (drawn, True)
    assert _sql("SELECT nextval('ingest_change_seq')")[0][0] == drawn + 1


@pytest.mark.parametrize("route", ["devices", "batches"])
async def test_a_sequence_never_drawn_reports_head_zero(home_iface, route):  # noqa: F811
    """`last_value` of an unused sequence reads 1, which no cursor has ever been."""
    stood_at = _sequence_last_value()[0]
    _sql("ALTER SEQUENCE ingest_change_seq RESTART")
    try:
        assert _sequence_last_value()[1] is False
        page = (await home_iface.get(f"/ingest/{route}")).json()
        assert page["head"] == 0
        assert page["next"] == 0
    finally:
        _sql("SELECT setval('ingest_change_seq', :v)", {"v": stood_at})


@pytest.mark.parametrize(
    ("route", "table"), [("devices", "pass_devices"), ("batches", "ingest_batches")]
)
async def test_a_writer_committing_while_a_page_is_read_never_puts_next_above_head(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    route,
    table,
):
    """An honest door must not trip home's check. The writer commits just before the page's rows
    are read, so the page holds a cursor a head read any earlier could not have seen."""
    await _batch(door, home_iface, JPEG)
    rows_read = f"{table}.change_seq > "
    fired: list[int] = []

    def writer(conn, cursor, statement, parameters, context, executemany) -> None:
        if rows_read in statement and not fired:
            fired.append(1)
            _sql(f"UPDATE {table} SET change_seq = nextval('ingest_change_seq')")

    event.listen(door_engine.sync_engine, "before_cursor_execute", writer)
    try:
        page = (await home_iface.get(f"/ingest/{route}", params={"since": 0})).json()
    finally:
        event.remove(door_engine.sync_engine, "before_cursor_execute", writer)
    assert fired
    assert page["next"] > 0
    assert page["next"] <= page["head"]


async def test_the_purge_leaves_the_head_at_or_above_every_cursor_it_handed_out(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    """The purge deletes acked rows, so a max over them can honestly fall below home's cursor."""
    batch = await _batch(door, home_iface, JPEG)
    devices_at = (await home_iface.get("/ingest/devices", params={"since": 0})).json()["next"]
    batches_at = (await home_iface.get("/ingest/batches", params={"since": 0})).json()["next"]
    await home_iface.post(f"/ingest/batches/{batch}/ack")
    _age(batch, 60)
    assert (await service_api.purge())["batches"] == 1
    _sql("DELETE FROM guest_uploads")
    _sql("DELETE FROM pass_devices")
    assert _sql(
        "SELECT (SELECT max(change_seq) FROM ingest_batches), "
        "(SELECT max(change_seq) FROM pass_devices)"
    ) == [(None, None)]

    for route, key, cursor in (
        ("devices", "devices", devices_at),
        ("batches", "batches", batches_at),
    ):
        page = (await home_iface.get(f"/ingest/{route}", params={"since": cursor})).json()
        assert page[key] == []
        assert page["next"] == cursor
        assert page["head"] >= cursor


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
    assert (await service_api.purge()) == {"batches": 1, "sessions": 0, "files": 1, "strays": 0}
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


def _aged(path, hours: float):
    if not path.exists():
        path.write_bytes(b"x")
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))
    return path


async def test_purge_sweeps_stray_parts_and_spools_but_never_a_live_session_or_a_blob(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    """A killed process leaves spools, and a part can outlive its row; those go once older than the
    upload horizon. A live session's files stay however old they look (a stalled upload), a file
    younger than the horizon stays (a create commits its row after making the part), and the
    finalized blobs are another directory the sweep never reads."""
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]

    async def opened(name: str) -> str:
        r = await door.post(
            "/api/guest/uploads", json={"filename": name, "size": 5}, headers=bearer(token)
        )
        return r.json()["upload_uid"]

    parts = ingest_env / "parts"
    live = await opened("live.jpg")
    gone = await opened("gone.jpg")
    _sql(
        "UPDATE guest_uploads SET expires_at = now() - interval '1 minute' WHERE upload_uid = :u",
        {"u": gone},
    )
    live_part = _aged(parts / f"{live}.part", 48)
    live_spool = _aged(parts / f"{live}.tok.spool", 48)
    gone_spool = _aged(parts / f"{gone}.tok.spool", 0)  # its row expired: the horizon is the row's
    stray_part = _aged(parts / "orphan.part", 25)
    stray_spool = _aged(parts / "orphan.tok.spool", 25)
    fresh_part = _aged(parts / "creating.part", 1)
    await _batch(door, home_iface, JPEG)
    sha = hashlib.sha256(JPEG).hexdigest()
    blob = _aged(ingest_env / "blobs" / sha[:2] / sha, 48)  # finalized, named by an unacked batch
    result = await service_api.purge()
    assert result["sessions"] == 1 and result["strays"] == 3
    assert live_part.exists() and live_spool.exists() and fresh_part.exists()
    assert not gone_spool.exists() and not (parts / f"{gone}.part").exists()
    assert not stray_part.exists() and not stray_spool.exists()
    assert blob.read_bytes() == JPEG


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


async def test_a_sweep_beside_a_real_uncommitted_create_leaves_its_part(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    """MEMNG-1082 #6: a create makes its part and only then commits, so the sweep can meet a file
    whose row it cannot see. The create here is genuinely open: its transaction holds the row."""
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    made, release = threading.Event(), threading.Event()
    real = guest_module._part_create_sync

    def parked(path) -> None:
        real(path)
        made.set()
        assert release.wait(30), "the test never released the create"

    monkeypatch.setattr(guest_module, "_part_create_sync", parked)
    create = asyncio.create_task(
        door.post(
            "/api/guest/uploads", json={"filename": "a.jpg", "size": 5}, headers=bearer(token)
        )
    )
    try:
        assert await asyncio.to_thread(made.wait, 30), "the create never reached its part"
        assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)], "the row is not uncommitted"
        result = await asyncio.wait_for(service_api.purge(), timeout=30)
        assert result["strays"] == 0
        parts = list((ingest_env / "parts").iterdir())
        assert len(parts) == 1
    finally:
        release.set()
    answer = await create
    assert answer.status_code == 201, answer.text
    assert parts[0].name == f"{answer.json()['upload_uid']}.part" and parts[0].exists()


async def test_a_file_exactly_at_the_upload_horizon_stays_and_a_second_older_goes(
    ingest_env,  # noqa: F811
):
    """MEMNG-1082 #6: the 24 h limit is `older than`, so a file stamped on it is still young."""
    at = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
    horizon = int((at - timedelta(hours=settings.UPLOAD_EXPIRY_HOURS)).timestamp())
    parts = ingest_env / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    on_it, older = parts / "onit.part", parts / "older.part"
    for path, stamp in ((on_it, horizon), (older, horizon - 1)):
        path.write_bytes(b"x")
        os.utime(path, (stamp, stamp))
    result = await service_api.purge(at)
    assert result["strays"] == 1
    assert on_it.exists() and not older.exists()


async def test_a_file_the_sweep_cannot_remove_leaves_a_trace_and_the_rest_still_go(
    ingest_env,  # noqa: F811
):
    """MEMNG-1082 #6: a real OSError (unlinking a directory), not a stubbed one."""
    parts = ingest_env / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    stuck = parts / "stuck.part"
    stuck.mkdir()
    os.utime(stuck, (time.time() - 48 * 3600, time.time() - 48 * 3600))
    stray = _aged(parts / "stray.part", 48)
    with structlog.testing.capture_logs() as logs:
        result = await service_api.purge()
    assert result["strays"] == 1
    assert stuck.exists() and not stray.exists()
    skipped = [e for e in logs if e["event"] == "ingest.sweep_skipped"]
    assert [e["path"] for e in skipped] == [str(stuck)]
    assert skipped[0]["error"] in ("IsADirectoryError", "PermissionError")
