"""/api/events on LISTEN/NOTIFY (MEMNG-964): frame format, transactional emit, replay, scoping.

Every case runs a real hub against the real test database: the NOTIFY is Postgres's, the listener
is a real asyncpg connection, the read-back is the production query. Only the stream's snapshot
callables are the router's own (`_user_snapshot` / `_ready_meta`), bound to the owner under test.
"""

import asyncio
import json
from typing import Any

import pytest
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobKind, MediaJob
from app.routers.assets import _ready_meta, _user_snapshot, media_event_stream
from app.services import event_stream as es
from app.services.media import publish_progress
from app.worker import _fail_attachment
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

pytestmark = pytest.mark.usefixtures("db_engine")

FRAME_TIMEOUT = 5.0


def _sql(stmt: str, **params) -> Any:
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            res = c.execute(text(stmt), params)
            return res.scalar_one() if res.returns_rows else None
    finally:
        eng.dispose()


@pytest.fixture
def owners():
    a = _sql("INSERT INTO users (subject, role) VALUES ('es-owner-a','user') RETURNING id")
    b = _sql("INSERT INTO users (subject, role) VALUES ('es-owner-b','user') RETURNING id")
    try:
        yield a, b
    finally:
        _sql(
            "DELETE FROM media_jobs WHERE attachment_id IN "
            "(SELECT id FROM attachments WHERE owner_id IN (:a,:b))",
            a=a,
            b=b,
        )
        _sql("DELETE FROM attachments WHERE owner_id IN (:a,:b)", a=a, b=b)
        _sql("DELETE FROM users WHERE id IN (:a,:b)", a=a, b=b)


def _seed(uid: str, owner_id: int, status: str = "ready") -> int:
    return _sql(
        "INSERT INTO attachments (uid, owner_id, filename, mime, size, status, storage_path, "
        "width, height, blurhash) VALUES (:u,:o,'x.jpg','image/jpeg',1,"
        "CAST(:s AS attachment_status),'/x',640,480,'LKO2') RETURNING id",
        u=uid,
        o=owner_id,
        s=status,
    )


def _seed_job(aid: int, state: str = "processing") -> int:
    return _sql(
        "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct) "
        "VALUES (:a,'slow','derive',CAST(:s AS job_state),0,0) RETURNING id",
        a=aid,
        s=state,
    )


async def _write_status(uid: str, status: AttachmentStatus, *, commit: bool = True) -> None:
    async with async_session() as db:
        att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one()
        att.status = status
        await es.notify_attachment_status(db, att.uid, att.owner_id)
        if commit:
            await db.commit()
        else:
            await db.rollback()


@pytest.fixture
async def hub():
    h = es.EventHub(
        es._listener_dsn(), es.fetch_status_frames, health_interval=0.2, backoff_initial=0.05
    )
    await h.start()
    assert await h.wait_connected(FRAME_TIMEOUT)
    try:
        yield h
    finally:
        await h.stop()


def _stream(hub, owner_id: int, **kw):
    async def snap():
        return await _user_snapshot(owner_id)

    async def meta(uids):
        return await _ready_meta(owner_id, uids)

    return media_event_stream(hub, owner_id, snap, meta, **kw)


def _parse(block: str) -> dict:
    out: dict = {}
    for line in block.rstrip("\n").split("\n"):
        field, _, value = line.partition(":")
        out[field or "comment"] = value.removeprefix(" ")
    if "data" in out:
        out["data"] = json.loads(out["data"])
    return out


async def _next(gen) -> dict:
    return _parse(await asyncio.wait_for(anext(gen), FRAME_TIMEOUT))


async def _opened(gen) -> None:
    assert await asyncio.wait_for(anext(gen), FRAME_TIMEOUT) == f"retry: {es.RETRY_MS}\n\n"


# --- (a) frame format ---


async def test_frames_carry_message_type_id_retry_once_and_keepalive(hub, owners):
    a, _ = owners
    _seed("es-fmt", a, status="processing")
    gen = _stream(hub, a, keepalive=0.2)
    try:
        await _opened(gen)
        snap = await _next(gen)
        # The default type, spelled out: the shipped iOS build drops any other name, and the web
        # client listens on onmessage only.
        assert snap["event"] == "message"
        assert snap["id"] == hub.event_id(hub.head)
        assert snap["data"] == {"uid": "es-fmt", "status": "processing", "progress_pct": 0}
        idle = [await asyncio.wait_for(anext(gen), FRAME_TIMEOUT) for _ in range(2)]
        assert idle == [": keepalive\n\n", ": keepalive\n\n"]  # and no second retry:
    finally:
        await gen.aclose()


# --- (b) transactional emit ---


async def test_a_committed_status_write_reaches_the_subscriber_and_a_rolled_back_one_does_not(
    hub, owners
):
    a, _ = owners
    rolled = _seed("es-rolled", a)
    jr = _seed_job(rolled)
    committed = _seed("es-committed", a)
    jc = _seed_job(committed)
    gen = _stream(hub, a)
    try:
        await _opened(gen)
        async with async_session() as db:
            job = (await db.execute(select(MediaJob).where(MediaJob.id == jr))).scalar_one()
            assert job.kind == JobKind.derive
            await _fail_attachment(db, job)
            await db.rollback()
        async with async_session() as db:
            job = (await db.execute(select(MediaJob).where(MediaJob.id == jc))).scalar_one()
            await _fail_attachment(db, job)
            await db.commit()
        frame = await _next(gen)
        # The rolled-back write was issued FIRST; had its NOTIFY escaped the rollback it would
        # be the frame in front of this one.
        assert frame["data"] == {"uid": "es-committed", "status": "failed", "progress_pct": 0}
        assert frame["event"] == "message"
        assert frame["id"] == hub.event_id(hub.head)
        assert _sql("SELECT status::text FROM attachments WHERE id=:i", i=rolled) == "ready"
    finally:
        await gen.aclose()


async def test_a_progress_sample_reaches_the_subscriber(hub, owners):
    a, _ = owners
    aid = _seed("es-progress", a, status="processing")
    jid = _seed_job(aid)
    gen = _stream(hub, a)
    try:
        await _opened(gen)
        assert (await _next(gen))["data"]["progress_pct"] == 0  # the snapshot
        await publish_progress(jid, 40)
        frame = await _next(gen)
        assert frame["data"] == {"uid": "es-progress", "status": "processing", "progress_pct": 40}
    finally:
        await gen.aclose()


# --- (c) replay ---


async def test_last_event_id_replays_the_missed_frames_in_order(hub, owners):
    a, _ = owners
    for uid in ("es-r1", "es-r2", "es-r3", "es-r4"):
        _seed(uid, a)
    first = _stream(hub, a)
    await _opened(first)
    await _write_status("es-r1", AttachmentStatus.processing)
    seen = await _next(first)
    await first.aclose()  # the client goes away
    witness = hub.subscribe(a)  # proves the three frames were published before the reconnect
    for uid in ("es-r2", "es-r3", "es-r4"):
        await _write_status(uid, AttachmentStatus.processing)
    for _ in range(3):
        await asyncio.wait_for(witness.queue.get(), FRAME_TIMEOUT)
    hub.unsubscribe(witness)

    again = _stream(hub, a, last_event_id=seen["id"])
    try:
        await _opened(again)
        replayed = [await _next(again) for _ in range(3)]
        assert [f["data"]["uid"] for f in replayed] == ["es-r2", "es-r3", "es-r4"]
        base = int(seen["id"].rpartition(".")[2])
        assert [f["id"] for f in replayed] == [hub.event_id(base + i) for i in (1, 2, 3)]
    finally:
        await again.aclose()


@pytest.mark.parametrize("last_event_id", ["nonsense", "0deadbeef.1", "0deadbeef.0", None])
async def test_an_unknown_or_foreign_id_starts_from_the_snapshot(hub, owners, last_event_id):
    a, _ = owners
    _seed("es-snap", a, status="pending")
    # Frames a replay from seq 0 or 1 would send, so only the epoch check keeps them out.
    for _ in range(2):
        hub.publish(a, {"uid": "es-ring", "status": "pending", "progress_pct": 0})
    gen = _stream(hub, a, last_event_id=last_event_id)
    try:
        await _opened(gen)
        frame = await _next(gen)
        assert frame["data"] == {"uid": "es-snap", "status": "pending", "progress_pct": 0}
    finally:
        await gen.aclose()


async def test_an_id_older_than_the_ring_starts_from_the_snapshot(owners):
    a, _ = owners
    _seed("es-expired", a, status="pending")
    h = es.EventHub("unused", es.fetch_status_frames, ring_max=2)  # never started: publish only
    h.loop = asyncio.get_running_loop()
    old = h.event_id(h.publish(a, {"uid": "x", "status": "pending", "progress_pct": 0}).seq)
    for _ in range(3):
        h.publish(a, {"uid": "y", "status": "pending", "progress_pct": 0})
    gen = _stream(h, a, last_event_id=old)
    try:
        await _opened(gen)
        assert (await _next(gen))["data"]["uid"] == "es-expired"
    finally:
        await gen.aclose()


# --- (d) ownership ---


async def test_another_users_attachment_never_reaches_a_subscriber(hub, owners):
    a, b = owners
    _seed("es-theirs", b)
    _seed("es-forged", b)
    _seed("es-mine", a)
    gen = _stream(hub, a)
    try:
        await _opened(gen)
        await _write_status("es-theirs", AttachmentStatus.processing)
        # A payload that names A as owner of B's row: routing trusts the row, not the claim.
        _sql(
            "SELECT pg_notify('attachment_status', :p)",
            p=json.dumps({"uid": "es-forged", "owner_id": a}),
        )
        await _write_status("es-mine", AttachmentStatus.processing)
        frame = await _next(gen)
        assert frame["data"]["uid"] == "es-mine"
    finally:
        await gen.aclose()


# --- listener reconnect, overflow ---


async def test_a_dropped_listener_reconnects_and_resends_state(hub, owners):
    a, _ = owners
    _seed("es-drop", a, status="processing")
    gen = _stream(hub, a)
    try:
        await _opened(gen)
        assert (await _next(gen))["data"]["status"] == "processing"
        pid = hub.listener_pid
        _sql("SELECT pg_terminate_backend(:p)", p=pid)
        # Finishes while nobody is listening: no NOTIFY, and no frame unless the reconnect resyncs.
        _sql("UPDATE attachments SET status='ready' WHERE uid='es-drop'")
        frame = await _next(gen)
        assert frame["data"] == {
            "uid": "es-drop",
            "status": "ready",
            "progress_pct": 100,
            "width": 640,
            "height": 480,
            "blurhash": "LKO2",
        }
        assert hub.connects == 2 and hub.listener_pid != pid
    finally:
        await gen.aclose()


async def test_an_overflowing_subscriber_is_closed_without_holding_up_the_others(owners):
    a, _ = owners
    h = es.EventHub("unused", es.fetch_status_frames, queue_max=2)
    h.loop = asyncio.get_running_loop()
    slow = _stream(h, a)
    await _opened(slow)
    fast = h.subscribe(a)
    got = []
    for i in range(5):
        h.publish(a, {"uid": f"o{i}", "status": "pending", "progress_pct": 0})
        got.append(fast.queue.get_nowait())
    assert [f.payload["uid"] for f in got] == ["o0", "o1", "o2", "o3", "o4"]
    assert h.subscriber_count() == 1  # dropped at the overflow, before it ever reads again
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(slow), FRAME_TIMEOUT)
    assert h.subscriber_count() == 1


async def test_a_failed_read_back_resends_state_instead_of_dropping_the_frame(owners):
    a, _ = owners
    _seed("es-fetchfail", a, status="pending")
    calls = {"n": 0}

    async def flaky(keys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("read-back failed")
        return await es.fetch_status_frames(keys)

    h = es.EventHub(es._listener_dsn(), flaky, health_interval=0.2)
    await h.start()
    assert await h.wait_connected(FRAME_TIMEOUT)
    gen = _stream(h, a)
    try:
        await _opened(gen)
        assert (await _next(gen))["data"]["status"] == "pending"  # the opening snapshot
        await _write_status("es-fetchfail", AttachmentStatus.processing)
        frame = await _next(gen)
        assert frame["data"] == {"uid": "es-fetchfail", "status": "processing", "progress_pct": 0}
        assert calls["n"] == 1  # the frame came from the resent snapshot, not a second read-back
    finally:
        await gen.aclose()
        await h.stop()
