"""upload.progress and memo.* on /api/events (MEMNG-965).

The new kinds ride the same transport as the media-status frames (MEMNG-964): a NOTIFY in the
transaction of the write, a real asyncpg listener, the hub's fan-out and ring. They take NEW event
names, so a client that only handles `message` never sees them. Recipients:

* upload.progress — the upload's owner only, at most one frame per upload per second (coalesced in
  the hub against an injectable clock, so the test never sleeps).
* memo.created / memo.updated / memo.deleted — by the memo's visibility at emission: PRIVATE reaches
  the owner, PROTECTED/PUBLIC reaches every household member; a PATCH that narrows a memo to PRIVATE
  is a memo.deleted for the others, and a widening from PRIVATE is a memo.created for them.

The HTTP calls go through the real routes; the stream is the router's own `media_event_stream`.
"""

import asyncio
import contextlib
import json
from typing import Any

import pytest
from app.database import async_session
from app.models import Visibility
from app.routers import memos as memos_router
from app.routers.assets import _ready_meta, _user_snapshot, media_event_stream
from app.services import event_stream as es
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_collections_api import _att
from tests.test_memo_guest import _guest
from tests.test_passes_api import _trip, _wipe

pytestmark = pytest.mark.usefixtures("db_engine")

FRAME_TIMEOUT = 5.0
MEMBER = "ev965-member-b"
MIB = 1024 * 1024


def _sql(stmt: str, **params) -> Any:
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            res = c.execute(text(stmt), params)
            return res.scalar_one() if res.returns_rows else None
    finally:
        eng.dispose()


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def hub(clock):
    h = es.EventHub(
        es._listener_dsn(),
        es.fetch_status_frames,
        health_interval=0.2,
        backoff_initial=0.05,
        clock=clock,
    )
    await h.start()
    assert await h.wait_connected(FRAME_TIMEOUT)
    try:
        yield h
    finally:
        await h.stop()


@pytest.fixture
def household(logged_in_client, monkeypatch, tmp_path):
    """(client, owner id, member id): the logged-in owner and a second household member."""
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    _wipe()
    _sql("DELETE FROM uploads")
    _sql("DELETE FROM users WHERE subject=:s", s=MEMBER)
    owner = _sql("SELECT id FROM users WHERE subject=:s", s=subject)
    member = _sql("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id", s=MEMBER)
    try:
        yield client, owner, member
    finally:
        _wipe()
        _sql("DELETE FROM uploads")
        _sql("DELETE FROM users WHERE subject=:s", s=MEMBER)


def _stream(hub, user_id: int, **kw):
    async def snap():
        return await _user_snapshot(user_id)

    async def meta(uids):
        return await _ready_meta(user_id, uids)

    return media_event_stream(hub, user_id, snap, meta, **kw)


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


@contextlib.asynccontextmanager
async def _opened(hub, user_id: int):
    """A subscribed stream with its (empty) opening snapshot consumed."""
    gen = _stream(hub, user_id)
    try:
        assert await asyncio.wait_for(anext(gen), FRAME_TIMEOUT) == f"retry: {es.RETRY_MS}\n\n"
        yield gen
    finally:
        await gen.aclose()


def _create(client, visibility: str, content: str = "hello") -> str:
    r = client.post("/api/memos", json={"content": content, "visibility": visibility})
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def _patch(client, uid: str, **body) -> None:
    r = client.patch(f"/api/memos/{uid}", json=body)
    assert r.status_code == 200, r.text


def _memo(event: str, uid: str, guest: dict | None = None) -> tuple[str, dict]:
    return event, {"uid": uid, "kind": event.removeprefix("memo."), "guest": guest}


async def _got(gen) -> tuple[str, dict]:
    frame = await _next(gen)
    return frame["event"], frame["data"]


# --- memo.* recipients by visibility ---


async def test_a_protected_memo_create_reaches_the_owner_and_another_member(hub, household):
    client, owner, member = household
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        uid = _create(client, "PROTECTED")
        frame = await _next(mine)
        assert frame["event"] == "memo.created"
        assert frame["id"] == hub.event_id(hub.head)
        assert frame["data"] == {"uid": uid, "kind": "created", "guest": None}
        assert await _got(theirs) == _memo("memo.created", uid)


async def test_a_private_memo_create_reaches_only_its_owner(hub, household):
    client, owner, member = household
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        private = _create(client, "PRIVATE")
        assert await _got(mine) == _memo("memo.created", private)
        shared = _create(client, "PUBLIC")
        assert await _got(mine) == _memo("memo.created", shared)
        # Had the private create reached the member it would be in front of this one.
        assert await _got(theirs) == _memo("memo.created", shared)


async def test_narrowing_to_private_is_a_delete_for_the_others_and_widening_a_create(
    hub, household
):
    client, owner, member = household
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        uid = _create(client, "PROTECTED")
        assert await _got(mine) == _memo("memo.created", uid)
        assert await _got(theirs) == _memo("memo.created", uid)

        _patch(client, uid, visibility="PRIVATE")
        assert await _got(mine) == _memo("memo.updated", uid)
        assert await _got(theirs) == _memo("memo.deleted", uid)

        _patch(client, uid, content="edited while private")  # the member hears nothing of it
        assert await _got(mine) == _memo("memo.updated", uid)

        _patch(client, uid, visibility="PUBLIC")
        assert await _got(mine) == _memo("memo.updated", uid)
        assert await _got(theirs) == _memo("memo.created", uid)

        _patch(client, uid, content="edited while public")
        assert await _got(mine) == _memo("memo.updated", uid)
        assert await _got(theirs) == _memo("memo.updated", uid)


async def test_a_delete_reaches_the_audience_the_memo_had(hub, household):
    client, owner, member = household
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        private = _create(client, "PRIVATE")
        shared = _create(client, "PROTECTED")
        assert await _got(mine) == _memo("memo.created", private)
        assert await _got(mine) == _memo("memo.created", shared)
        assert await _got(theirs) == _memo("memo.created", shared)

        assert client.delete(f"/api/memos/{private}").status_code == 204
        assert client.delete(f"/api/memos/{shared}").status_code == 204
        assert await _got(mine) == _memo("memo.deleted", private)
        assert await _got(mine) == _memo("memo.deleted", shared)
        # The private delete went to the owner alone: the member's next frame is the shared one.
        assert await _got(theirs) == _memo("memo.deleted", shared)


async def test_a_rolled_back_memo_write_emits_nothing(hub, household, monkeypatch):
    client, owner, member = household
    issued: list[str] = []
    real = es.notify_memo_changed

    async def spy(db, change, *args, **kwargs):
        issued.append(change)
        await real(db, change, *args, **kwargs)

    async def boom(*_a, **_k):
        raise RuntimeError("the write fails after its NOTIFY was issued")

    monkeypatch.setattr(es, "notify_memo_changed", spy)
    shares = memos_router._enqueue_frame_shares_safe
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        monkeypatch.setattr(memos_router, "_enqueue_frame_shares_safe", boom)
        failed = client.post("/api/memos", json={"content": "lost", "visibility": "PROTECTED"})
        assert failed.status_code == 500
        assert issued == ["created"]  # the NOTIFY ran inside the transaction that rolled back
        assert _sql("SELECT count(*) FROM memos WHERE content='lost'") == 0
        monkeypatch.setattr(memos_router, "_enqueue_frame_shares_safe", shares)
        uid = _create(client, "PROTECTED", content="kept")
        assert await _got(mine) == _memo("memo.created", uid)
        assert await _got(theirs) == _memo("memo.created", uid)


async def test_a_guest_memo_frame_carries_the_guest_flag(hub, household):
    client, owner, member = household
    _att("ev965-g1", "test_session_user")
    # The filing announces its own memo.created (MEMNG-1181), asynchronously after its commit: wait
    # for the hub to fan it out, so a stream opened below cannot subscribe in time to hear it.
    filed = hub.subscribe(owner)
    memo_uid, pass_uid = await _guest(_trip(client), "test_session_user", ["ev965-g1"], name="Anna")
    assert (await asyncio.wait_for(filed.queue.get(), FRAME_TIMEOUT)).event == "memo.created"
    hub.unsubscribe(filed)
    guest = {"contributor_name": "Anna", "pass_uid": pass_uid}
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        _patch(client, memo_uid, content="a caption from the owner")
        assert await _got(mine) == _memo("memo.updated", memo_uid, guest)
        assert await _got(theirs) == _memo("memo.updated", memo_uid, guest)
        assert client.delete(f"/api/memos/{memo_uid}").status_code == 204
        assert await _got(mine) == _memo("memo.deleted", memo_uid, guest)


async def test_a_trip_ingest_guest_memo_announces_its_creation_to_the_household(hub, household):
    """`passes.create_guest_memo` is how trip ingest files a guest batch; the memo it makes is
    PROTECTED, so every member hears it created, with the guest flag (MEMNG-1181)."""
    client, owner, member = household
    _att("ev1181-g1", "test_session_user")
    coll = _trip(client)
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        memo_uid, pass_uid = await _guest(coll, "test_session_user", ["ev1181-g1"], name="Anna")
        guest = {"contributor_name": "Anna", "pass_uid": pass_uid}
        assert await _got(mine) == _memo("memo.created", memo_uid, guest)
        assert await _got(theirs) == _memo("memo.created", memo_uid, guest)


async def test_a_guest_name_reaches_the_frame_as_its_column_stores_it(hub, household):
    """The guest flag is read back from the row, so a name with quotes and a hashtag rides the
    NOTIFY intact and a long one arrives cut to the column's bound (MEMNG-1181)."""
    from app.services.passes import CONTRIBUTOR_NAME_MAX

    client, owner, member = household
    _att("ev1181-q1", "test_session_user")
    coll = _trip(client)
    name = 'Ann "#beach" O\'Neil \\ ' + "x" * CONTRIBUTOR_NAME_MAX
    async with _opened(hub, member) as theirs:
        memo_uid, pass_uid = await _guest(coll, "test_session_user", ["ev1181-q1"], name=name)
        stored = name[:CONTRIBUTOR_NAME_MAX]
        assert _sql("SELECT contributor_name FROM attachments WHERE uid='ev1181-q1'") == stored
        assert await _got(theirs) == _memo(
            "memo.created", memo_uid, {"contributor_name": stored, "pass_uid": pass_uid}
        )


async def test_a_guest_filing_that_rolls_back_announces_nothing(hub, household, monkeypatch):
    """The NOTIFY is issued inside the filing transaction, so a filing that does not commit is
    never heard; the next one that does is (MEMNG-1181)."""
    from app.models import Collection, User
    from app.services import passes as pass_svc
    from sqlalchemy import select

    client, owner, member = household
    _att("ev1181-lost", "test_session_user")
    _att("ev1181-kept", "test_session_user")
    coll = _trip(client)
    issued: list[tuple[str, dict | None]] = []
    real = es.notify_memo_changed

    async def spy(db, change, *args, **kwargs):
        issued.append((change, kwargs.get("guest")))
        await real(db, change, *args, **kwargs)

    monkeypatch.setattr(es, "notify_memo_changed", spy)
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.subject == "test_session_user"))
            ).scalar_one()
            collection = (
                await db.execute(select(Collection).where(Collection.uid == coll))
            ).scalar_one()
            pass_row, _ = await pass_svc.issue_pass(db, collection, user, label="lost")

            async def finalize(_memo) -> list[str]:
                return ["ev1181-lost"]

            await pass_svc.create_guest_memo(
                db,
                pass_row,
                caption=None,
                contributor_name="Ben",
                idempotency_key="trip:lost",
                finalize=finalize,
            )
            await db.rollback()
        assert [change for change, _ in issued] == ["created"]
        assert issued[0][1] is not None and issued[0][1]["contributor_name"] == "Ben"
        assert _sql("SELECT count(*) FROM memos WHERE pass_id IS NOT NULL") == 0
        memo_uid, pass_uid = await _guest(coll, "test_session_user", ["ev1181-kept"], name="Anna")
        guest = {"contributor_name": "Anna", "pass_uid": pass_uid}
        assert await _got(mine) == _memo("memo.created", memo_uid, guest)
        assert await _got(theirs) == _memo("memo.created", memo_uid, guest)


def test_memo_change_frames_per_transition():
    p, r, u = Visibility.PRIVATE, Visibility.PROTECTED, Visibility.PUBLIC
    o, x, h = es.Audience.OWNER, es.Audience.OTHERS, es.Audience.HOUSEHOLD
    assert es.memo_change_frames("created", None, p) == [("memo.created", o)]
    assert es.memo_change_frames("created", None, r) == [("memo.created", h)]
    assert es.memo_change_frames("created", None, u) == [("memo.created", h)]
    assert es.memo_change_frames("deleted", p, None) == [("memo.deleted", o)]
    assert es.memo_change_frames("deleted", r, None) == [("memo.deleted", h)]
    assert es.memo_change_frames("deleted", u, None) == [("memo.deleted", h)]
    assert es.memo_change_frames("updated", p, p) == [("memo.updated", o)]
    assert es.memo_change_frames("updated", r, u) == [("memo.updated", h)]
    assert es.memo_change_frames("updated", u, u) == [("memo.updated", h)]
    assert es.memo_change_frames("updated", r, p) == [("memo.updated", o), ("memo.deleted", x)]
    assert es.memo_change_frames("updated", u, p) == [("memo.updated", o), ("memo.deleted", x)]
    assert es.memo_change_frames("updated", p, r) == [("memo.updated", o), ("memo.created", x)]
    assert es.memo_change_frames("updated", p, u) == [("memo.updated", o), ("memo.created", x)]


# --- the ring: replay per user, and the new kinds stay out of the status tracking ---


async def test_replay_hands_each_user_only_the_frames_addressed_to_them():
    hub = es.EventHub("unused", es.fetch_status_frames)  # never started: publish only
    a, b = 101, 202
    start = hub.event_id(
        hub.publish(a, {"uid": "seed", "status": "pending", "progress_pct": 0}).seq
    )
    hub.publish(a, {"uid": "m1"}, event="memo.updated", audience=es.Audience.OWNER)
    hub.publish(a, {"uid": "m1"}, event="memo.deleted", audience=es.Audience.OTHERS)
    hub.publish(a, {"uid": "m2"}, event="memo.created", audience=es.Audience.HOUSEHOLD)
    hub.publish(a, {"upload_uid": "u1"}, event="upload.progress")

    def seen(user: int) -> list[tuple[str, str]]:
        replay = hub.subscribe(user, start).replay
        assert replay is not None
        return [(f.event, f.payload.get("uid") or f.payload["upload_uid"]) for f in replay]

    assert seen(a) == [("memo.updated", "m1"), ("memo.created", "m2"), ("upload.progress", "u1")]
    assert seen(b) == [("memo.deleted", "m1"), ("memo.created", "m2")]


async def test_a_memo_frame_is_named_and_never_tracked_as_an_attachment(hub, household):
    client, owner, _ = household
    async with _opened(hub, owner) as mine:
        uid = _create(client, "PROTECTED")
        assert await _got(mine) == _memo("memo.created", uid)
        # A resync resends the snapshot; a memo uid taken for an in-flight attachment would come
        # back as a synthesised `ready` frame here. The snapshot is empty, so the next thing on
        # the stream must be the next real frame.
        hub.resync_all()
        second = _create(client, "PRIVATE")
        frame = await _next(mine)
        assert (frame["event"], frame["data"]["uid"]) == ("memo.created", second)


# --- upload.progress ---


async def _progress(owner: int, upload_uid: str, received: int, size: int) -> None:
    async with async_session() as db:
        await es.notify_upload_progress(db, upload_uid, owner, received, size)
        await db.commit()


async def test_many_small_appends_within_a_second_make_one_progress_frame(hub, household, clock):
    _, owner, _ = household
    size = 400 * MIB
    async with _opened(hub, owner) as mine:
        for i in range(1, 401):  # 400 committed 1 MiB tails, all inside one second of the clock
            await _progress(owner, "up-400", i * MIB, size)
        frame = await _next(mine)
        assert frame["event"] == "upload.progress"
        assert frame["data"] == {
            "upload_uid": "up-400",
            "bytes_received": MIB,
            "declared_size": size,
        }
        clock.now += 0.999  # still inside the second: dropped
        await _progress(owner, "up-400", size - 1, size)
        clock.now += 0.001  # a second after the first frame: the next one goes out
        await _progress(owner, "up-400", size, size)
        frame = await _next(mine)
        # Had any of the 399 in-between appends (or the one at 0.999 s) gone out, it would be
        # this frame instead.
        assert frame["data"]["bytes_received"] == size


async def test_coalescing_is_per_upload(hub, household):
    _, owner, _ = household
    async with _opened(hub, owner) as mine:
        await _progress(owner, "up-a", 1, 10)
        await _progress(owner, "up-b", 2, 10)
        assert (await _next(mine))["data"]["upload_uid"] == "up-a"
        assert (await _next(mine))["data"]["upload_uid"] == "up-b"


async def test_upload_progress_never_reaches_another_user(hub, household):
    _, owner, member = household
    async with _opened(hub, member) as theirs:
        await _progress(owner, "up-owner", 5, 10)
        await _progress(member, "up-member", 7, 10)
        frame = await _next(theirs)
        assert frame["data"]["upload_uid"] == "up-member"


async def test_a_committed_append_announces_progress_and_finalize_the_attachment(hub, household):
    client, owner, member = household
    content = bytes(range(256)) * 400
    r = client.post(
        "/api/uploads",
        json={"filename": "v.mov", "mime": "video/quicktime", "size": len(content)},
    )
    assert r.status_code == 201, r.text
    upload_uid = r.json()["upload_uid"]
    async with _opened(hub, owner) as mine, _opened(hub, member) as theirs:
        gap = client.patch(
            f"/api/uploads/{upload_uid}", content=b"x", headers={"Upload-Offset": "9"}
        )
        assert gap.status_code == 409  # a refused append commits nothing and announces nothing
        sent = client.patch(
            f"/api/uploads/{upload_uid}", content=content, headers={"Upload-Offset": "0"}
        )
        assert sent.status_code == 200, sent.text
        frame = await _next(mine)
        assert frame["event"] == "upload.progress"
        assert frame["data"] == {
            "upload_uid": upload_uid,
            "bytes_received": len(content),
            "declared_size": len(content),
        }
        fin = client.post(f"/api/uploads/{upload_uid}/finalize")
        assert fin.status_code == 200, fin.text
        frame = await _next(mine)
        assert frame["event"] == "message"
        assert frame["data"] == {"uid": fin.json()["uid"], "status": "pending", "progress_pct": 0}
        # Neither reached the member: their first frame is their own upload's.
        await _progress(member, "up-member", 1, 2)
        assert (await _next(theirs))["data"]["upload_uid"] == "up-member"
