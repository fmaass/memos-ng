"""/api/events on LISTEN/NOTIFY (MEMNG-964): frame format, transactional emit, replay, scoping.

Every case runs a real hub against the real test database: the NOTIFY is Postgres's, the listener
is a real asyncpg connection, the read-back is the production query. Only the stream's snapshot
callables are the router's own (`_user_snapshot` / `_ready_meta`), bound to the owner under test.
"""

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobKind, MediaJob
from app.routers.assets import _ready_meta, _user_snapshot, media_event_stream
from app.services import event_stream as es
from app.services.media import publish_progress
from app.worker import _fail_attachment
from sqlalchemy import create_engine, select, text
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

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


# --- hardening (MEMNG-1052) ---


def _bare_hub(**kw) -> es.EventHub:
    """A hub that is never started: publish, resync and subscribe only, no listener."""
    h = es.EventHub("unused", es.fetch_status_frames, **kw)
    h.loop = asyncio.get_running_loop()
    return h


def _pending(uid: str) -> dict:
    return {"uid": uid, "status": "pending", "progress_pct": 0}


async def test_a_cursor_at_the_head_across_a_resync_is_not_replayable(owners):
    a, _ = owners
    h = _bare_hub()
    for i in range(3):
        h.publish(a, _pending(f"h{i}"))
    held = h.event_id(h.head)  # the client is fully caught up when the listener drops
    h.resync_all()
    # Whatever the outage lost belongs after `held`: an empty replay would hide it, the snapshot
    # does not.
    assert h.subscribe(a, held).replay is None
    # A frame published after the resync is above the boundary, so its id resumes normally.
    after = h.event_id(h.publish(a, _pending("h3")).seq)
    assert h.subscribe(a, after).replay == []


async def test_eviction_never_lowers_the_boundary_a_resync_raised(owners):
    a, _ = owners
    h = _bare_hub(ring_max=3)
    for i in range(3):
        h.publish(a, _pending(f"e{i}"))
    stale = h.event_id(2)  # issued before the outage
    h.resync_all()
    # The ring is at its cap, so this publish evicts seq 1: the eviction floor moves to 1, below
    # the boundary the resync set at 3, and must not make seq 2 replayable again.
    h.publish(a, _pending("e3"))
    assert h.subscribe(a, stale).replay is None


async def test_a_cursor_from_a_partly_delivered_snapshot_resnapshots(owners):
    a, _ = owners
    for uid in ("es-part-1", "es-part-2", "es-part-3"):
        _seed(uid, a, status="pending")
    h = _bare_hub()
    h.publish(a, _pending("es-part-1"))  # a head above the boundary, so it is resumable
    gen = _stream(h, a)
    try:
        await _opened(gen)
        rows = [await _next(gen) for _ in range(3)]
    finally:
        await gen.aclose()
    assert sorted(r["data"]["uid"] for r in rows) == ["es-part-1", "es-part-2", "es-part-3"]
    # The client that dropped after the first or second row holds that row's id: it must be sent
    # the snapshot again, not resumed past the rows it never saw.
    for partial in rows[:2]:
        assert h.subscribe(a, partial["id"]).replay is None
    # Only the last row says the snapshot is complete, and that id resumes from the head.
    assert rows[2]["id"] == h.event_id(h.head)
    assert h.subscribe(a, rows[2]["id"]).replay == []


async def test_a_resent_snapshot_marks_only_its_last_frame_resumable(owners):
    a, _ = owners
    for uid in ("es-rs-1", "es-rs-2"):
        _seed(uid, a, status="pending")
    h = _bare_hub()
    gen = _stream(h, a)
    try:
        await _opened(gen)
        [await _next(gen) for _ in range(2)]
        _sql("UPDATE attachments SET status='ready' WHERE uid='es-rs-1'")
        h.resync_all()
        h.publish(a, _pending("es-rs-2"))  # lifts the head above the boundary the resync set
        resent = [await _next(gen) for _ in range(2)]
    finally:
        await gen.aclose()
    # The resend is the in-flight row, then the ready frame synthesised for the finished one.
    assert [f["data"]["uid"] for f in resent] == ["es-rs-2", "es-rs-1"]
    assert resent[1]["data"]["status"] == "ready"
    assert h.subscribe(a, resent[0]["id"]).replay is None
    assert h.subscribe(a, resent[1]["id"]).replay is not None


@pytest.mark.parametrize(
    "tail",
    [
        "²",  # str.isdigit() says yes, int() says no
        "١",  # an Arabic-Indic one: int() reads it as 1
        "0" * 19 + "1",  # twenty digits
        "-1",
        " 1",
        "",
    ],
)
async def test_a_garbled_id_falls_back_to_the_snapshot(owners, tail):
    a, _ = owners
    h = _bare_hub()
    h.resync_all()  # the boundary sits at 0, as after the listener's first connect
    for i in range(2):
        h.publish(a, _pending(f"g{i}"))
    assert h.subscribe(a, f"{h.epoch}.{tail}").replay is None


async def test_an_id_of_nineteen_ascii_digits_still_resumes(owners):
    a, _ = owners
    h = _bare_hub()
    h.publish(a, _pending("n0"))
    h.publish(a, _pending("n1"))
    replay = h.subscribe(a, f"{h.epoch}.{'0' * 18}1").replay
    assert replay is not None and [f.payload["uid"] for f in replay] == ["n1"]


async def test_an_inbox_overflow_drops_and_resyncs_every_subscriber(owners):
    a, b = owners
    release = asyncio.Event()
    busy = asyncio.Event()
    fetched: list[list[tuple[str, int]]] = []

    async def gated(keys):
        fetched.append(list(keys))
        if len(fetched) == 1:
            busy.set()
            await release.wait()  # the dispatcher is busy on the read-back while the inbox fills
        return [(owner, _pending(uid)) for uid, owner in keys]

    h = es.EventHub("unused", gated, inbox_max=2)
    h.loop = asyncio.get_running_loop()
    before = h.event_id(h.publish(a, _pending("seed")).seq)
    sub_a, sub_b = h.subscribe(a), h.subscribe(b)
    dispatch = asyncio.create_task(h._dispatch())

    def notify(uid: str, owner: int) -> None:
        h._on_notify(None, 0, es.ATTACHMENT_STATUS, json.dumps({"uid": uid, "owner_id": owner}))

    async def take(sub, n: int) -> list:
        return [await asyncio.wait_for(sub.queue.get(), FRAME_TIMEOUT) for _ in range(n)]

    try:
        notify("k0", a)
        await asyncio.wait_for(busy.wait(), FRAME_TIMEOUT)
        for uid in ("k1", "k2", "k3", "k4"):  # two fit, two do not — and nothing raises
            notify(uid, b)
        assert h._inbox.qsize() == 2
        release.set()
        got_a, got_b = await take(sub_a, 2), await take(sub_b, 3)
        await asyncio.sleep(0.05)  # room for a second resync, which must not come
    finally:
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
    assert fetched == [[("k0", a)], [("k1", b), ("k2", b)]]  # k3 and k4 were dropped

    # One resync for the whole episode, to every subscriber, so the snapshot covers k3 and k4.
    assert sub_a.queue.empty() and sub_b.queue.empty()
    assert [type(i) for i in got_a] == [es.Frame, es.Resync]
    assert sum(isinstance(i, es.Resync) for i in got_b) == 1
    assert [i.payload["uid"] for i in got_b if isinstance(i, es.Frame)] == ["k1", "k2"]
    assert h.subscribe(a, before).replay is None  # and every id before the drop is retired


# --- shutdown (MEMNG-1052 finding 5) ---


def _graceful_shutdown_seconds() -> float | None:
    """The bound docker-entrypoint.sh hands the home app's uvicorn, or None without one."""
    lines = (Path(__file__).resolve().parents[1] / "docker-entrypoint.sh").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("exec uvicorn app.main:app"))
    command = [lines[start]]
    while command[-1].endswith("\\"):
        start += 1
        command.append(lines[start])
    match = re.search(r"--timeout-graceful-shutdown[ =](\d+(?:\.\d+)?)\b", " ".join(command))
    return float(match.group(1)) if match else None


async def test_an_idle_stream_does_not_hold_a_stopping_server_past_its_bound(owners):
    a, _ = owners
    bound = _graceful_shutdown_seconds()
    # The container must stop within 5 s; uvicorn adds its own 0.1 s and the lifespan shutdown.
    assert bound is not None and bound <= 4
    h = _bare_hub()

    async def snap() -> list[dict]:
        return []

    async def meta(_uids: list[str]) -> dict[str, dict]:
        return {}

    async def events(_request):
        return StreamingResponse(
            media_event_stream(h, a, snap, meta), media_type="text/event-stream"
        )

    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/api/events", events)]),
            host="127.0.0.1",
            port=0,
            lifespan="off",
            # No logging config and no level: uvicorn would otherwise dictConfig its loggers for
            # the rest of the session, under the filters the logging-privacy tests install.
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=bound,
        )
    )
    serving = asyncio.create_task(server.serve())
    writer = None
    try:
        while not server.started:  # noqa: ASYNC110 — uvicorn exposes a flag, not an event
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/events HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        head = b""
        while b"retry:" not in head:
            head += await asyncio.wait_for(reader.read(1024), FRAME_TIMEOUT)
        assert h.subscriber_count() == 1  # the stream is open and idle: no frame is coming
        started = asyncio.get_running_loop().time()
        server.should_exit = True
        await asyncio.wait_for(asyncio.shield(serving), bound + 1.5)
        assert asyncio.get_running_loop().time() - started < 5
        assert h.subscriber_count() == 0  # the stream's own cleanup ran on the way out
    finally:
        if writer is not None:
            writer.close()
        if not serving.done():
            server.force_exit = True
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, 5)
