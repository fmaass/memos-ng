"""Home's trip-ingest puller (trip pass P3a), against the REAL ingest apps.

Home and the ingest host are two installations of one schema, so both cannot share the test
database's tables: the ingest apps (`create_app("ingest")` for the guest door, `"ingest-home"` for
the interface home pulls from) run here against a `pluto` schema of the same database, through
their own sessionmaker (see `world`), while the puller works on home's `public` tables exactly
as the worker does. The puller talks to the ingest-home app over ASGI through `Link`, a transport
that can drop the link or cut a download short — the transport is the only thing replaced.
"""

import asyncio
import gzip
import hashlib
import json
import random
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from app import database
from app.config import settings
from app.database import async_session
from app.database import engine as home_engine
from app.ingest import core
from app.ingest.factory import create_app
from app.models import Base, Collection, IngestBlob, User
from app.services import collections as coll_svc
from app.services import passes as passes_svc
from app.services import trip_ingest, variants
from app.services import uploads as uploads_svc
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.ingest_support import HEIC, JPEG, SERVICE_TOKEN, complete, redeem, upload
from tests.test_attachments_taken import _sql

TAG = "tp3trip"
OWNER = "tp3_owner"
HOME_TABLES = (
    "trip_ingest_cursor",
    "pass_sync_state",
    "pass_events",
    "ingest_blobs",
    "ingest_batches",
    "guest_uploads",
    "pass_devices",
    "passes",
    "collection_exclusions",
    "collection_members",
    "collections",
    "frame_share_jobs",
    "media_jobs",
    "attachments",
    "memos",
    "uploads",
    "tags",
)
PLUTO_TABLES = (
    "ingest_refusals",
    "pass_events",
    "ingest_blobs",
    "ingest_batches",
    "guest_uploads",
    "pass_devices",
    "passes",
)


class _Cut(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data
        raise httpx.ReadError("the link dropped mid-file")

    async def aclose(self) -> None:
        return None


class Link(httpx.AsyncBaseTransport):
    """The wire between home and the ingest host: down entirely, down for some paths, or cutting
    the next file download after `cut_after` bytes. Everything else is the real app's answer."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.down = False
        self.down_paths: tuple[str, ...] = ()
        self.cut_after: int | None = None
        self.seen: list[tuple[str, str, str | None]] = []
        self.requests: list[httpx.Request] = []
        # A hostile ingest host: replaces the real app's answer to the requests it chooses.
        self.rewrite: Callable[[httpx.Request, httpx.Response], Awaitable[httpx.Response]] | None
        self.rewrite = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append((request.method, path, request.headers.get("range")))
        self.requests.append(request)
        if self.down or any(path.startswith(p) for p in self.down_paths):
            raise httpx.ConnectError("link down", request=request)
        response = await self.inner.handle_async_request(request)
        if self.rewrite is not None:
            response = await self.rewrite(request, response)
        if self.cut_after is not None and path.startswith("/ingest/blobs/"):
            n, self.cut_after = self.cut_after, None
            body = await response.aread()
            return httpx.Response(
                response.status_code, headers=response.headers, stream=_Cut(body[:n])
            )
        return response


@dataclass
class World:
    door: AsyncClient
    link: AsyncClient
    wire: Link
    owner_id: int
    home_root: Path


@pytest.fixture(scope="session")
def pluto_schema(db_engine):
    with db_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS pluto CASCADE"))
        conn.execute(text("CREATE SCHEMA pluto"))
    sync = create_engine(db_engine.url, connect_args={"options": "-csearch_path=pluto"})
    Base.metadata.create_all(sync)
    sync.dispose()
    yield
    with db_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS pluto CASCADE"))


def _wipe() -> None:
    for table in HOME_TABLES:
        _sql(f"DELETE FROM {table}")
    for table in PLUTO_TABLES:
        _sql(f"DELETE FROM pluto.{table}")
    _sql("DELETE FROM users WHERE subject = :s", {"s": OWNER})


@pytest_asyncio.fixture
async def world(db_engine, pluto_schema, tmp_path, monkeypatch) -> AsyncIterator[World]:
    monkeypatch.setattr(settings, "INGEST_DATA_ROOT", str(tmp_path / "pluto"))
    monkeypatch.setattr(settings, "INGEST_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(settings, "INGEST_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(settings, "INGEST_TRUST_CF_CONNECTING_IP", False)
    monkeypatch.setattr(settings, "INGEST_REDEEM_PER_MINUTE", 1000)
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path / "home"))
    core.redeem_window.reset()
    _wipe()
    pluto = create_async_engine(
        home_engine.url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": "pluto"}},
    )
    # `get_session` opens `app.database.async_session` at call time, and it is the only reader of
    # that name: the routes bake their dependencies at construction, so `dependency_overrides`
    # would not reach them, while every home module (this one included) bound home's sessionmaker
    # at import and keeps it.
    pluto_sessions = async_sessionmaker(pluto, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", pluto_sessions)
    door_app, home_app = create_app("ingest"), create_app("ingest-home")
    wire = Link(ASGITransport(app=home_app))
    async with async_session() as db:
        owner = User(subject=OWNER, display_name="Owner")
        db.add(owner)
        await db.commit()
        owner_id = owner.id
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=door_app), base_url="http://door") as door,
            AsyncClient(
                transport=wire,
                base_url="http://pluto",
                headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            ) as link,
        ):
            yield World(door, link, wire, owner_id, tmp_path / "home")
    finally:
        await pluto.dispose()
        core.redeem_window.reset()
        _wipe()


# --- helpers ------------------------------------------------------------------------------------


@dataclass
class Trip:
    pass_uid: str
    secret: str
    collection_id: int


async def _trip(w: World, *, tag: str | None = TAG, kind: str = "trip", span=False) -> Trip:
    async with async_session() as db:
        owner = await db.get(User, w.owner_id)
        assert owner is not None
        extra = {}
        if span:
            extra = {
                "span_start": datetime(2031, 3, 1, tzinfo=UTC),
                "span_end": datetime(2031, 3, 10, tzinfo=UTC),
            }
        coll, _ = await coll_svc.create_collection(
            db, owner, title="Sardinia", kind=kind, tag=tag if kind == "trip" else None, **extra
        )
        p, secret = await passes_svc.issue_pass(db, coll, owner, label="friends")
        await db.commit()
        return Trip(p.uid, secret, coll.id)


async def _guest(w: World, trip: Trip, name: str = "Anna") -> str:
    await trip_ingest.push_passes(w.link)  # the pass has to be on the ingest host to redeem
    return (await redeem(w.door, trip.secret, name))["device_token"]


async def _send(w: World, token: str, *files: tuple[bytes, str], caption=None) -> str:
    uids = [await upload(w.door, token, data, name) for data, name in files]
    r = await complete(w.door, token, uids, caption)
    assert r.status_code in (200, 201), r.text
    return r.json()["batch_id"]


async def _tick(w: World) -> str:
    return await trip_ingest.run_sync_safe(w.link, budget_s=60)


def _pluto_batch(uid: str):
    return _sql(
        "SELECT state, acked_at IS NOT NULL FROM pluto.ingest_batches WHERE uid = :u", {"u": uid}
    )


def _home_batch(uid: str):
    return _sql(
        "SELECT state, memo_id IS NOT NULL, acked_at IS NOT NULL, quarantined_at IS NOT NULL "
        "FROM ingest_batches WHERE uid = :u",
        {"u": uid},
    )


def _memos():
    return _sql("SELECT id, visibility, owner_id, pass_id, guest_caption, content FROM memos")


def _tags(memo_id: int) -> list[str]:
    return [
        r[0]
        for r in _sql(
            "SELECT t.name FROM memo_tags mt JOIN tags t ON t.id = mt.tag_id WHERE mt.memo_id = :m",
            {"m": memo_id},
        )
    ]


SHA_JPEG = hashlib.sha256(JPEG).hexdigest()


# --- the happy path -----------------------------------------------------------------------------


async def test_a_guest_batch_is_filed_exactly_like_a_member_upload_and_acked(world):
    trip = await _trip(world)
    token = await _guest(world, trip)
    batch = await _send(world, token, (JPEG, "a.jpg"), caption="Beach day")
    assert await _tick(world) == "ok"
    [(memo_id, visibility, owner_id, pass_id, caption, content)] = _memos()
    assert (visibility, owner_id, caption, content) == (
        "PROTECTED",
        world.owner_id,
        "Beach day",
        f"#{TAG}",
    )
    assert pass_id == _sql("SELECT id FROM passes WHERE uid = :u", {"u": trip.pass_uid})[0][0]
    assert _tags(memo_id) == [TAG]
    [(name, device, sha, path, filename)] = _sql(
        "SELECT contributor_name, contributed_by_device_id, sha256, storage_path, filename "
        "FROM attachments WHERE memo_id = :m",
        {"m": memo_id},
    )
    assert (name, sha, filename) == ("Anna", SHA_JPEG, "a.jpg")
    assert device == _sql("SELECT id FROM pass_devices")[0][0]
    original = Path(path)
    assert original.is_relative_to(world.home_root / "originals")
    assert original.read_bytes() == JPEG
    assert _sql("SELECT count(*) FROM media_jobs")[0][0] >= 1  # the derive work is queued
    assert _pluto_batch(batch) == [("acked", True)]
    assert _home_batch(batch) == [("acked", True, True, False)]
    assert not trip_ingest.staging_dir(batch).exists()


async def test_an_album_pass_files_member_rows_instead_of_a_tag(world):
    trip = await _trip(world, kind="album")
    token = await _guest(world, trip)
    await _send(world, token, (JPEG, "a.jpg"))
    assert await _tick(world) == "ok"
    [(memo_id, *_rest, content)] = _memos()
    assert content == "" and _tags(memo_id) == []
    assert _sql("SELECT collection_id FROM collection_members") == [(trip.collection_id,)]


async def test_a_hashtag_in_the_caption_or_the_name_joins_no_other_trip(world):
    trip = await _trip(world)
    token = await _guest(world, trip, name="#other_trip Anna")
    await _send(world, token, (JPEG, "a.jpg"), caption="#other_trip with #friends")
    assert await _tick(world) == "ok"
    [(memo_id, *_rest, caption, content)] = _memos()
    assert content == f"#{TAG}" and _tags(memo_id) == [TAG]
    assert caption == "#other_trip with #friends"
    assert _sql("SELECT contributor_name FROM attachments") == [("#other_trip Anna",)]
    assert _sql("SELECT count(*) FROM tags WHERE name = 'other_trip'") == [(0,)]


# --- idempotency --------------------------------------------------------------------------------


async def test_a_crash_between_the_filing_commit_and_the_ack_files_once_and_acks_later(
    world, monkeypatch
):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))

    async def crash() -> None:
        raise RuntimeError("the worker died here")

    seam = trip_ingest._after_filing_commit
    monkeypatch.setattr(trip_ingest, "_after_filing_commit", crash)
    assert await _tick(world) == "error"
    assert _home_batch(batch) == [("complete", True, False, False)]
    assert _pluto_batch(batch) == [("complete", False)]
    monkeypatch.setattr(trip_ingest, "_after_filing_commit", seam)  # the restarted worker
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1
    assert _pluto_batch(batch) == [("acked", True)]


async def test_a_replay_after_the_owner_deleted_the_memo_never_refiles(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    assert await _tick(world) == "ok"
    _sql("DELETE FROM memos")
    # The feed re-read from the start, and the ingest host forgetting the ack.
    _sql("UPDATE trip_ingest_cursor SET batches_since = 0, devices_since = 0")
    _sql("UPDATE pluto.ingest_batches SET state = 'complete', acked_at = NULL")
    _sql("UPDATE ingest_batches SET state = 'complete', acked_at = NULL")
    assert await _tick(world) == "ok"
    assert _memos() == []
    assert _home_batch(batch) == [("acked", False, True, False)]  # memo_id nulled, state kept
    assert _pluto_batch(batch) == [("acked", True)]


async def test_a_double_pull_files_one_memo(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    assert await _tick(world) == "ok"
    _sql("UPDATE trip_ingest_cursor SET batches_since = 0, devices_since = 0")
    assert await _tick(world) == "ok"
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1
    assert _sql("SELECT count(*) FROM ingest_batches WHERE uid = :u", {"u": batch}) == [(1,)]


async def test_two_filings_of_one_batch_racing_make_one_memo(world, monkeypatch):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    monkeypatch.setattr(trip_ingest, "file_open", _no_filing)
    assert await _tick(world) == "ok"  # recorded and downloaded nothing yet
    [(batch_id,)] = _sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list((await db.execute(select(IngestBlob))).scalars())
    await trip_ingest.download(world.link, batch, rows, deadline=float("inf"))
    results = await asyncio.gather(
        trip_ingest.file_batch(batch_id), trip_ingest.file_batch(batch_id), return_exceptions=True
    )
    assert sorted(results, key=repr) == [False, True], results  # no exception either
    assert len(_memos()) == 1


async def _no_filing(client, deadline, errors) -> int:
    return 0


async def test_a_second_tick_is_refused_while_one_holds_the_lock(world):
    async with home_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": trip_ingest.SYNC_LOCK_KEY}
            )
            assert await _tick(world) == "busy"
    assert await _tick(world) == "ok"


# --- hostile manifests --------------------------------------------------------------------------


HOSTILE = {
    "oversize": "UPDATE pluto.ingest_blobs SET size_bytes = :cap + 1",
    "streams_past_its_size": "UPDATE pluto.ingest_blobs SET size_bytes = size_bytes - 10",
    "dotdot_filename": "UPDATE pluto.ingest_blobs SET filename = '../../etc/a.jpg'",
    "dotdot_device": "UPDATE pluto.pass_devices SET uid = '../dev'",
    "non_hex_sha": "UPDATE pluto.ingest_blobs SET sha256 = repeat('Z', 64)",
    "mime_disagrees": "UPDATE pluto.ingest_blobs SET mime = 'image/png'",
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
async def test_a_hostile_manifest_is_quarantined_never_acked(world, case):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    _sql(HOSTILE[case], {"cap": settings.MAX_UPLOAD_MIB * 1024 * 1024})
    assert await _tick(world) == "ok"
    assert _memos() == []
    assert _pluto_batch(batch) == [("quarantined", False)]
    assert _home_batch(batch) == [("quarantined", False, False, True)]
    assert _sql("SELECT count(*) FROM pass_events WHERE kind = 'refused'") == [(1,)]
    assert _sql("SELECT count(*) FROM uploads") == [(0,)]
    assert not trip_ingest.staging_dir(batch).exists()
    # Sticky: the next tick acknowledges nothing.
    assert await _tick(world) == "ok"
    assert _pluto_batch(batch) == [("quarantined", False)]
    assert not any(p.endswith("/ack") for _, p, _ in world.wire.seen)


async def test_a_device_of_another_pass_is_a_mismatch_and_quarantined(world):
    mine, theirs = await _trip(world), await _trip(world, tag="tp3other")
    await _guest(world, mine)
    batch = await _send(world, await _guest(world, theirs, name="Bea"), (JPEG, "a.jpg"))
    # The manifest now claims the batch for the other pass while its device stays where it is.
    _sql(
        "UPDATE pluto.ingest_batches SET pass_id = (SELECT id FROM pluto.passes WHERE uid = :p)",
        {"p": mine.pass_uid},
    )
    assert await _tick(world) == "ok"
    assert _memos() == []
    assert _pluto_batch(batch) == [("quarantined", False)]


async def test_a_batch_of_a_pass_home_never_issued_is_quarantined_with_no_ledger_row(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    _sql("UPDATE pluto.passes SET uid = 'not-a-home-pass'")
    assert await _tick(world) == "ok"
    assert _pluto_batch(batch) == [("quarantined", False)]
    assert _home_batch(batch) == [] and _memos() == []


def test_a_declared_size_is_bounded_on_both_sides():
    cap = settings.MAX_UPLOAD_MIB * 1024 * 1024
    entry = {"sha256": SHA_JPEG, "mime": "image/jpeg", "filename": "a.jpg"}
    assert trip_ingest.validate_blob({**entry, "size": cap}).size == cap
    assert trip_ingest.validate_blob({**entry, "size": 1}).size == 1
    for size in (0, -1, cap + 1, True, "5"):
        with pytest.raises(trip_ingest.Hostile):
            trip_ingest.validate_blob({**entry, "size": size})


def test_a_batch_needs_between_one_and_the_maximum_number_of_files():
    base = {"batch_id": "b", "pass_uid": "p", "device_id": "d", "caption": None}
    with pytest.raises(trip_ingest.Hostile):
        trip_ingest.validate_batch({**base, "blobs": []})
    entry = {"sha256": SHA_JPEG, "mime": "image/jpeg", "filename": "a.jpg", "size": 3}
    assert len(trip_ingest.validate_batch({**base, "blobs": [entry]}).blobs) == 1
    with pytest.raises(trip_ingest.Hostile):  # the same file twice
        trip_ingest.validate_batch({**base, "blobs": [entry, entry]})


def test_the_memo_key_is_bounded_and_namespaced():
    key = trip_ingest.idempotency_key("A" * 64)
    assert key.startswith("trip:") and len(key) <= 64
    assert key != trip_ingest.idempotency_key("A" * 63)


# --- the link -----------------------------------------------------------------------------------


async def test_a_link_down_is_retried_and_quarantines_nothing(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    world.wire.down = True
    assert await _tick(world) == "link_down"
    assert _sql("SELECT last_error LIKE 'link:%' FROM trip_ingest_cursor") == [(True,)]
    assert _pluto_batch(batch) == [("complete", False)]
    assert _home_batch(batch) == []
    world.wire.down = False
    assert await _tick(world) == "ok"
    assert _pluto_batch(batch) == [("acked", True)]
    assert len(_memos()) == 1
    assert _sql("SELECT last_error, last_sync_at IS NOT NULL FROM trip_ingest_cursor") == [
        (None, True)
    ]


async def test_a_quarantine_the_link_dropped_is_retried_until_it_lands(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    _sql("UPDATE pluto.ingest_blobs SET sha256 = repeat('Z', 64)")
    world.wire.down_paths = (f"/ingest/batches/{batch}",)  # the feed is up, transitions not
    assert await _tick(world) == "link_down"
    assert _home_batch(batch) == [("quarantined", False, False, False)]  # owed, persisted
    assert _pluto_batch(batch) == [("complete", False)]
    world.wire.down_paths = ()
    assert await _tick(world) == "ok"
    assert _home_batch(batch) == [("quarantined", False, False, True)]
    assert _pluto_batch(batch) == [("quarantined", False)]


async def test_a_cut_download_resumes_by_range(world):
    trip = await _trip(world)
    data = HEIC + bytes(range(256)) * 40
    batch = await _send(world, await _guest(world, trip), (data, "h.heic"))
    world.wire.cut_after = 300
    assert await _tick(world) == "link_down"
    partial = trip_ingest.staging_dir(batch) / f"{hashlib.sha256(data).hexdigest()}.part"
    assert partial.stat().st_size == 300
    world.wire.seen.clear()
    assert await _tick(world) == "ok"
    ranged = [r for m, p, r in world.wire.seen if p.startswith("/ingest/blobs/")]
    assert ranged == ["bytes=300-"]
    [(sha,)] = _sql("SELECT sha256 FROM attachments")
    assert sha == hashlib.sha256(data).hexdigest()
    assert _pluto_batch(batch) == [("acked", True)]


# --- revocation ---------------------------------------------------------------------------------


async def _revoke(trip: Trip, owner_id: int) -> None:
    async with async_session() as db:
        p = await passes_svc.get_pass(db, trip.pass_uid)
        owner = await db.get(User, owner_id)
        assert p is not None and owner is not None
        await passes_svc.revoke_pass(db, p, owner)
        await db.commit()


async def test_a_revocation_made_while_the_link_is_down_lands_on_reconnect_and_batches_drain(
    world,
):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    assert await trip_ingest.pull_devices(world.link) == 1  # home's mirror holds the device
    await _revoke(trip, world.owner_id)
    world.wire.down = True
    assert await _tick(world) == "link_down"
    assert await _tick(world) == "link_down"  # retried every tick, not sent once
    assert _sql("SELECT revoked_at IS NULL FROM pluto.passes") == [(True,)]
    world.wire.down = False
    assert await _tick(world) == "ok"
    assert _sql("SELECT revoked_at IS NOT NULL FROM pluto.passes") == [(True,)]
    assert _sql("SELECT revoked_at IS NOT NULL FROM pluto.pass_devices") == [(True,)]
    assert _sql("SELECT revocation_acked_version = revoked_version FROM pass_devices") == [(True,)]
    assert _sql("SELECT push_urgent FROM pass_sync_state") == [(False,)]
    # The owner moderates: a batch received before the revocation is still filed.
    assert len(_memos()) == 1 and _pluto_batch(batch) == [("acked", True)]


async def test_a_pull_never_clears_a_home_revocation(world):
    trip = await _trip(world)
    await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    await trip_ingest.pull_devices(world.link)
    await _revoke(trip, world.owner_id)
    # The ingest host keeps showing the device live and moves its cursor.
    world.wire.down_paths = ("/ingest/device-revocations",)
    _sql("UPDATE pluto.pass_devices SET change_seq = change_seq + 1000, uploads_count = 7")
    assert await _tick(world) == "link_down"
    _sql("UPDATE trip_ingest_cursor SET devices_since = 0")
    await trip_ingest.pull_devices(world.link)
    assert _sql(
        "SELECT revoked_at IS NOT NULL, revocation_acked_version IS NULL, uploads_count "
        "FROM pass_devices"
    ) == [(True, True, 7)]


async def test_an_unknown_answer_keeps_the_revocation_owed(world):
    trip = await _trip(world)
    await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    await trip_ingest.pull_devices(world.link)
    await _revoke(trip, world.owner_id)
    _sql("UPDATE pluto.pass_devices SET uid = 'renamed-on-pluto'")
    await trip_ingest.push_passes(world.link)
    assert await trip_ingest.push_device_revocations(world.link) == 0
    assert _sql("SELECT revocation_acked_version FROM pass_devices") == [(None,)]


async def test_a_pass_revoked_mid_pull_still_files_what_was_received(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    await trip_ingest.push_passes(world.link)
    await trip_ingest.pull_devices(world.link)
    await trip_ingest.pull_batches(world.link, [])  # recorded, not yet filed
    await _revoke(trip, world.owner_id)
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1 and _pluto_batch(batch) == [("acked", True)]


# --- paging -------------------------------------------------------------------------------------


async def test_quarantine_and_cursors_page_across_more_than_one_page(world, monkeypatch):
    monkeypatch.setattr(trip_ingest, "BATCH_PAGE", 2)
    monkeypatch.setattr(trip_ingest, "DEVICE_PAGE", 2)
    trip = await _trip(world)
    batches = []
    for i, name in enumerate(("Anna", "Bea", "Cleo", "Dina", "Ella")):
        token = await _guest(world, trip, name=name)
        batches.append(await _send(world, token, (JPEG + bytes([i]) * 7, f"{i}.jpg")))
    _sql(
        "UPDATE pluto.ingest_blobs SET sha256 = repeat('Z', 64) WHERE batch_id = "
        "(SELECT id FROM pluto.ingest_batches WHERE uid = :u)",
        {"u": batches[2]},
    )
    assert await _tick(world) == "ok"
    assert len(_memos()) == 4
    states = {b: _pluto_batch(b)[0][0] for b in batches}
    assert states == {b: ("quarantined" if b == batches[2] else "acked") for b in batches}
    assert _sql("SELECT count(*) FROM pass_devices") == [(5,)]
    top = _sql("SELECT max(change_seq) FROM pluto.ingest_batches")[0][0]
    assert _sql("SELECT batches_since FROM trip_ingest_cursor") == [(top,)]
    # Acked and quarantined rows stay in the feed: reading it again changes nothing.
    _sql("UPDATE trip_ingest_cursor SET batches_since = 0")
    world.wire.seen.clear()
    assert await _tick(world) == "ok"
    assert len(_memos()) == 4
    assert not any(p.endswith(("/ack", "/quarantine")) for _, p, _ in world.wire.seen)


# --- the trip's tag at filing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change", ["UPDATE collections SET tag = NULL", "UPDATE collections SET tag = 'two words'"]
)
async def test_a_trip_whose_tag_is_gone_or_unusable_holds_the_batch(world, change):
    trip = await _trip(world, span=True)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    _sql(change)
    assert await _tick(world) == "ok"
    assert _memos() == []
    assert _home_batch(batch) == [("open", False, False, False)]
    assert _pluto_batch(batch) == [("complete", False)]
    assert _sql("SELECT last_error LIKE '%held%' FROM pass_sync_state") == [(True,)]
    _sql("UPDATE collections SET tag = :t", {"t": TAG})
    assert await _tick(world) == "ok"
    [(memo_id, *_rest)] = _memos()
    assert _tags(memo_id) == [TAG] and _pluto_batch(batch) == [("acked", True)]


async def test_a_renamed_tag_is_the_one_the_memo_carries(world):
    trip = await _trip(world)
    await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    async with async_session() as db:
        coll = await db.get(Collection, trip.collection_id)
        assert coll is not None
        await coll_svc.update_collection(db, coll, {"tag": "tp3renamed"})
        await db.commit()
    assert await _tick(world) == "ok"
    [(memo_id, *_rest, content)] = _memos()
    assert content == "#tp3renamed" and _tags(memo_id) == ["tp3renamed"]


# --- configuration ------------------------------------------------------------------------------


def test_the_token_is_read_from_its_file_without_the_newline(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("s3cret-token\n")
    monkeypatch.setattr(settings, "TRIP_INGEST_URL", "http://127.0.0.1:8091")
    monkeypatch.setattr(settings, "TRIP_INGEST_TOKEN_FILE", str(token_file))
    assert trip_ingest.enabled()
    assert trip_ingest.read_token() == "s3cret-token"
    assert "TRIP_INGEST_TOKEN_FILE" not in repr(settings)


async def test_the_puller_is_off_until_configured(monkeypatch):
    monkeypatch.setattr(settings, "TRIP_INGEST_URL", "")
    monkeypatch.setattr(settings, "TRIP_INGEST_TOKEN_FILE", "")
    assert not trip_ingest.enabled()
    assert await trip_ingest.run_sync_safe() == "disabled"


async def test_a_filing_that_rolls_back_walks_every_file_back_to_staging(world, monkeypatch):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    real = passes_svc.create_guest_memo

    async def placed_then_failed(*args, **kwargs):
        await real(*args, **kwargs)  # the original is in the memo folder by now
        raise RuntimeError("the filing failed after placement")

    monkeypatch.setattr(passes_svc, "create_guest_memo", placed_then_failed)
    assert await _tick(world) == "ok"  # the batch's own failure, recorded on its pass
    assert _sql("SELECT last_error LIKE '%RuntimeError%' FROM pass_sync_state") == [(True,)]
    staged = trip_ingest.staging_dir(batch) / SHA_JPEG
    assert staged.read_bytes() == JPEG
    originals = world.home_root / "originals"
    assert not originals.exists() or not any(p.is_file() for p in originals.rglob("*"))
    assert _sql("SELECT count(*) FROM uploads") == [(0,)] and _memos() == []
    assert _home_batch(batch) == [("open", False, False, False)]
    monkeypatch.setattr(passes_svc, "create_guest_memo", real)
    world.wire.seen.clear()
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1
    assert not any(p.startswith("/ingest/blobs/") for _, p, _ in world.wire.seen)  # no re-download


async def test_bytes_that_do_not_hash_to_their_name_are_quarantined(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    forged = bytearray(JPEG)
    forged[-1] ^= 0xFF  # same length, same type, other bytes
    core.blob_path(SHA_JPEG).write_bytes(bytes(forged))
    assert await _tick(world) == "ok"
    assert _memos() == []
    assert _pluto_batch(batch) == [("quarantined", False)]
    assert _sql("SELECT detail LIKE '%hash%' FROM pass_events WHERE kind = 'refused'") == [(True,)]


# --- fold round 1: a less trusted ingest host, long names, large batches, fresh identities -------


class _Body(httpx.AsyncByteStream):
    """An answer body as a transport delivers it: not yet read."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data

    async def aclose(self) -> None:
        return None


class _Endless(httpx.AsyncByteStream):
    """A body far larger than any answer home accepts, counting how much of it was pulled."""

    def __init__(self, chunks: int) -> None:
        self.chunks, self.pulled = chunks, 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.chunks):
            self.pulled += 1
            yield b" " * 1024

    async def aclose(self) -> None:
        return None


async def test_a_service_answer_is_read_no_further_than_its_cap(world, monkeypatch):
    monkeypatch.setattr(trip_ingest, "MAX_RESPONSE_BYTES", 4096)
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    endless = _Endless(10_000)

    async def huge_page(request, response):
        if request.url.path == "/ingest/batches":
            return httpx.Response(200, stream=endless)
        return response

    world.wire.rewrite = huge_page
    assert await _tick(world) == "link_down"
    assert endless.pulled <= 6  # four KiB and the chunk that crossed it, not ten MiB
    assert _home_batch(batch) == [] and _pluto_batch(batch) == [("complete", False)]
    assert _sql("SELECT batches_since FROM trip_ingest_cursor") == [(0,)]
    assert {r.headers.get("accept-encoding") for r in world.wire.requests} == {"identity"}


async def test_a_compressed_file_is_refused_before_it_is_inflated(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))

    async def gzipped(request, response):
        if request.url.path.startswith("/ingest/blobs/"):
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Body(bomb))
        return response

    world.wire.rewrite = gzipped
    assert await _tick(world) == "ok"
    blob_gets = [r for r in world.wire.requests if r.url.path.startswith("/ingest/blobs/")]
    assert [r.headers.get("accept-encoding") for r in blob_gets] == ["identity"]
    assert _memos() == [] and _pluto_batch(batch) == [("quarantined", False)]
    assert _sql("SELECT detail LIKE '%encoded%' FROM pass_events WHERE kind = 'refused'") == [
        (True,)
    ]


@pytest.mark.parametrize(
    "name",
    ["a" * 230 + ".jpg", "b" * 496 + ".jpg", "é" * 200 + ".jpg"],
    ids=["past_name_max_with_the_prefix", "the_door_s_own_cap", "multibyte"],
)
async def test_a_long_but_valid_filename_is_filed(world, name):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, name))
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1
    assert _sql("SELECT filename FROM attachments") == [(name,)]
    assert _pluto_batch(batch) == [("acked", True)]


async def test_a_batch_that_cannot_be_filed_does_not_hold_back_the_next(world, monkeypatch):
    trip = await _trip(world)
    token = await _guest(world, trip)
    stuck = await _send(world, token, (JPEG, "a.jpg"), caption="stuck")
    after = await _send(world, token, (HEIC, "h.heic"), caption="after")
    real = passes_svc.create_guest_memo

    async def refuses_the_first(db, p, *, caption, **kwargs):
        if caption == "stuck":
            raise OSError(36, "File name too long")
        return await real(db, p, caption=caption, **kwargs)

    monkeypatch.setattr(passes_svc, "create_guest_memo", refuses_the_first)
    assert await _tick(world) == "ok"
    assert _home_batch(stuck) == [("open", False, False, False)]
    assert _home_batch(after) == [("acked", True, True, False)]
    assert _pluto_batch(stuck) == [("complete", False)]


async def test_a_batch_too_large_for_one_tick_files_in_parts_across_ticks(world, monkeypatch):
    monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 2)
    trip = await _trip(world)
    files = [(JPEG + bytes([i]), f"p{i}.jpg") for i in range(5)]
    batch = await _send(world, await _guest(world, trip), *files)
    # A clock that runs out after every download and every filed part: the batch's total work is
    # five ticks of downloading and three of filing, and each tick may do one step of it.
    now = [0.0]
    monkeypatch.setattr(trip_ingest, "time", SimpleNamespace(monotonic=lambda: now[0]))
    real_fetch, real_part, real_sniff = (
        trip_ingest._fetch,
        trip_ingest._file_part,
        core.sniff_matches,
    )
    sniffed: list[str] = []

    async def fetch(client, blob, dest, deadline):
        try:
            return await real_fetch(client, blob, dest, deadline)
        finally:
            now[0] += 1000

    async def part(batch_id):
        try:
            return await real_part(batch_id)
        finally:
            now[0] += 1000

    def sniff(path, filename):
        sniffed.append(filename)
        return real_sniff(path, filename)

    monkeypatch.setattr(trip_ingest, "_fetch", fetch)
    monkeypatch.setattr(trip_ingest, "_file_part", part)
    monkeypatch.setattr(core, "sniff_matches", sniff)
    statuses = [await _tick(world) for _ in range(7)]
    assert statuses == ["partial"] * 6 + ["ok"]
    gets = [p for _, p, _ in world.wire.seen if p.startswith("/ingest/blobs/")]
    assert sorted(gets) == sorted(
        f"/ingest/blobs/{hashlib.sha256(d).hexdigest()}" for d, _ in files
    )
    assert sorted(sniffed) == sorted(n for _, n in files)  # each file verified exactly once
    assert len(_memos()) == 3  # parts of 2, 2 and 1 files
    assert _sql("SELECT count(*) FROM ingest_blobs WHERE filed_at IS NULL") == [(0,)]
    assert _sql("SELECT count(DISTINCT memo_id) FROM attachments") == [(3,)]
    assert _home_batch(batch) == [("acked", True, True, False)]
    assert _pluto_batch(batch) == [("acked", True)]


async def test_a_part_left_unfiled_keeps_the_batch_open_and_unacked(world, monkeypatch):
    monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 2)
    trip = await _trip(world)
    files = [(JPEG, "a.jpg"), (HEIC, "a - print.heic"), (JPEG + b"x", "b.jpg")]
    batch = await _send(world, await _guest(world, trip), *files)
    real = trip_ingest._file_part
    calls = [0]

    async def one_part_per_tick(batch_id):
        calls[0] += 1
        if calls[0] > 1:
            raise trip_ingest.OutOfTime
        return await real(batch_id)

    monkeypatch.setattr(trip_ingest, "_file_part", one_part_per_tick)
    statuses, snapshots = [], []
    for _ in range(2):
        calls[0] = 0
        statuses.append(await _tick(world))
        filed = _sql("SELECT filename FROM ingest_blobs WHERE filed_at IS NOT NULL")
        snapshots.append({n for (n,) in filed})
        if statuses[-1] == "partial":
            assert _home_batch(batch) == [("open", True, False, False)]
            assert _pluto_batch(batch) == [("complete", False)]
    # Two parts whichever comes first (the manifest's order is the ingest host's): the screen
    # and its print fit one part together, so neither is filed alone.
    assert statuses == ["partial", "ok"]
    assert snapshots[0] in ({"a.jpg", "a - print.heic"}, {"b.jpg"})
    assert snapshots[1] == {n for _, n in files}
    assert _pluto_batch(batch) == [("acked", True)]


async def test_a_guest_file_never_adopts_an_unlinked_attachment_of_the_owner(world):
    async with async_session() as db:
        up = await uploads_svc.create_upload(
            db, owner_id=world.owner_id, filename="mine.jpg", mime="image/jpeg", size=len(JPEG)
        )
        part = uploads_svc.part_path(up.upload_uid)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(JPEG)
        up.bytes_received = len(JPEG)
        await uploads_svc.finalize_pending(db, [up.upload_uid], world.owner_id)
        await db.commit()
    [(mine, mine_path)] = _sql("SELECT id, storage_path FROM attachments")
    trip = await _trip(world)
    await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    assert await _tick(world) == "ok"
    rows = _sql("SELECT id, memo_id IS NULL, storage_path FROM attachments ORDER BY id")
    assert rows[0] == (mine, True, mine_path)  # the owner's row: untouched, still unlinked
    assert len(rows) == 2 and rows[1][0] != mine and rows[1][1] is False
    assert Path(rows[1][2]).read_bytes() == JPEG and Path(mine_path).read_bytes() == JPEG


async def test_a_hostile_value_is_never_echoed_into_errors_or_logs(world, capsys):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    reflected = "Bearer reflected-s3cret"

    async def reflect(request, response):
        if request.url.path == "/ingest/batches":
            page = json.loads(await response.aread())
            page["batches"][0]["blobs"][0]["size"] = reflected
            return httpx.Response(200, stream=_Body(json.dumps(page).encode()))
        return response

    world.wire.rewrite = reflect
    assert await _tick(world) == "ok"
    assert _pluto_batch(batch) == [("quarantined", False)]
    stored = _sql(
        "SELECT detail FROM pass_events UNION ALL SELECT last_error FROM pass_sync_state "
        "UNION ALL SELECT last_error FROM trip_ingest_cursor"
    )
    assert stored and not any("s3cret" in (v or "") for (v,) in stored)
    assert "s3cret" not in capsys.readouterr().out


async def test_two_filings_racing_over_a_batch_of_parts_file_each_part_once(world, monkeypatch):
    monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 1)
    trip = await _trip(world)
    files = [(JPEG + bytes([i]), f"p{i}.jpg") for i in range(3)]
    batch = await _send(world, await _guest(world, trip), *files)
    monkeypatch.setattr(trip_ingest, "file_open", _no_filing)
    assert await _tick(world) == "ok"
    [(batch_id,)] = _sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list((await db.execute(select(IngestBlob))).scalars())
    await trip_ingest.download(world.link, batch, rows, deadline=float("inf"))
    results = await asyncio.gather(
        trip_ingest.file_batch(batch_id), trip_ingest.file_batch(batch_id), return_exceptions=True
    )
    assert sorted(results, key=repr) == [False, True], results
    assert len(_memos()) == 3
    assert _sql("SELECT count(*), count(DISTINCT attachment_id) FROM ingest_blobs") == [(3, 3)]


# --- filing parts: no part outgrows its bound, a variant group stays whole while it fits -------

MIB = 1024 * 1024


def _blobs(*shape: tuple[str, int]) -> list[IngestBlob]:
    return [
        IngestBlob(filename=name, size_bytes=size, sha256=f"{i:064x}")
        for i, (name, size) in enumerate(shape)
    ]


def _variant_group(base: str, count: int, size: int) -> list[tuple[str, int]]:
    return [(f"{base}.jpg", size)] + [(f"{base} - print (v{i}).jpg", size) for i in range(1, count)]


def _assert_parts(parts: list[list[IngestBlob]], blobs: list[IngestBlob]) -> None:
    files, cap = trip_ingest.FILE_PART_FILES, trip_ingest.FILE_PART_BYTES
    for part in parts:
        assert part, "an empty part"
        if len(part) > 1:  # only a part of exactly one file may outgrow the byte bound
            assert len(part) <= files, [b.filename for b in part]
            assert sum(b.size_bytes for b in part) <= cap, [b.filename for b in part]
    flat = [b for part in parts for b in part]
    assert len({id(b) for b in flat}) == len(flat) == len(blobs)  # no file twice, none lost
    groups: dict[str, list[IngestBlob]] = {}
    for b in blobs:
        groups.setdefault(variants.parse_variant(b.filename).base.casefold(), []).append(b)
    # Groups in the order they first appear, each group's files in manifest order.
    assert [id(b) for b in flat] == [id(b) for g in groups.values() for b in g]
    where = {id(b): i for i, part in enumerate(parts) for b in part}
    for g in groups.values():
        fits = len(g) == 1 or (len(g) <= files and sum(b.size_bytes for b in g) <= cap)
        if fits:
            assert len({where[id(b)] for b in g}) == 1, [b.filename for b in g]


def test_a_variant_group_with_more_files_than_a_part_is_cut_into_bounded_parts():
    blobs = _blobs(*_variant_group("photo", 60, 1000))
    parts = trip_ingest._filing_parts(blobs)
    _assert_parts(parts, blobs)
    assert [len(p) for p in parts] == [25, 25, 10]


def test_a_variant_group_with_more_bytes_than_a_part_is_cut_into_bounded_parts():
    blobs = _blobs(*_variant_group("photo", 5, 100 * MIB))
    parts = trip_ingest._filing_parts(blobs)
    _assert_parts(parts, blobs)
    assert [len(p) for p in parts] == [2, 2, 1]


def test_a_variant_group_that_fits_a_part_stays_whole():
    blobs = _blobs(*[(f"single{i}.jpg", 10) for i in range(20)], *_variant_group("photo", 10, 10))
    parts = trip_ingest._filing_parts(blobs)
    _assert_parts(parts, blobs)
    # The group fits a part of its own but not beside the twenty singles: it moves whole.
    assert [len(p) for p in parts] == [20, 10]
    assert parts[1] == blobs[20:]


def test_a_file_larger_than_a_part_is_a_part_of_its_own():
    blobs = _blobs(("a.jpg", 10), ("big.jpg", 300 * MIB), ("c.jpg", 10))
    assert trip_ingest._filing_parts(blobs) == [[blobs[0]], [blobs[1]], [blobs[2]]]
    grouped = _blobs(("p.jpg", 10), ("p - print.jpg", 300 * MIB), ("p - print (b).jpg", 10))
    parts = trip_ingest._filing_parts(grouped)
    _assert_parts(parts, grouped)
    assert parts == [[grouped[0]], [grouped[1]], [grouped[2]]]


@pytest.mark.parametrize("seed", range(40))
def test_every_part_of_any_batch_shape_stays_within_its_bound(seed):
    rng = random.Random(seed)
    sizes = (1, 10 * MIB, 100 * MIB, 255 * MIB, 256 * MIB, 300 * MIB)
    names = []
    for i in range(rng.randint(1, 120)):
        base = f"shot{rng.randint(0, rng.choice((1, 3, 30)))}"
        names.append((base if rng.random() < 0.3 else f"{base} - print (v{i})") + ".jpg")
    blobs = _blobs(*[(n, rng.choice(sizes)) for n in names])
    _assert_parts(trip_ingest._filing_parts(blobs), blobs)


def _assert_prints_bind_only_beside_their_screen(files: int) -> None:
    """Each file filed once: a print in the screen's part binds to it as a hidden rendition, a
    print in another part is a visible picture of its own (which part the screen lands in follows
    the ingest host's manifest order, so the count is read from the ledger, not assumed)."""
    [(beside,)] = _sql(
        "SELECT count(*) FROM ingest_blobs b JOIN ingest_blobs s "
        "ON s.filename LIKE '%.jpg' AND s.filename NOT LIKE '% - print%' "
        "AND b.filed_at = s.filed_at AND b.id <> s.id"
    )
    [(atts,)] = _sql("SELECT count(*) FROM attachments")
    [(prints,)] = _sql("SELECT count(*) FROM attachment_renditions")
    assert (atts, prints) == (files - beside, beside)


@pytest.mark.parametrize("bound", ["files", "bytes"])
async def test_an_oversized_variant_group_cancelled_mid_part_still_completes(
    world, monkeypatch, bound
):
    """The gate's finding: a same-base group larger than a part was one part, so a tick's bound
    cancelled it every time and the batch never completed. Here any attempt that moves more than
    a part's bound is cancelled mid-part (as the worker's bound would), and so is the very first
    attempt, right after its first file was moved into place."""
    shots = [
        (JPEG + bytes([i]), "photo.jpg" if i == 0 else f"photo - print (v{i}).jpg")
        for i in range(7)
    ]
    if bound == "files":
        monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 3)
    else:
        monkeypatch.setattr(trip_ingest, "FILE_PART_BYTES", 3 * (len(JPEG) + 1))
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), *shots)
    real_part, real_create = trip_ingest._file_part, uploads_svc.create_upload
    attempt = {"n": 0, "files": 0, "bytes": 0}
    stalled = asyncio.Event()

    async def part(batch_id):
        attempt.update(n=attempt["n"] + 1, files=0, bytes=0)
        return await real_part(batch_id)

    async def create_upload(db, **kw):
        over = attempt["files"] + 1 > trip_ingest.FILE_PART_FILES or (
            attempt["bytes"] + kw["size"] > trip_ingest.FILE_PART_BYTES
        )
        if attempt["files"] >= 1 and (attempt["n"] == 1 or over):
            stalled.set()
            await asyncio.Event().wait()  # the tick's bound runs out here, files already moved
        attempt.update(files=attempt["files"] + 1, bytes=attempt["bytes"] + kw["size"])
        return await real_create(db, **kw)

    monkeypatch.setattr(trip_ingest, "_file_part", part)
    monkeypatch.setattr(uploads_svc, "create_upload", create_upload)
    statuses: list[str] = []
    for _ in range(6):
        stalled.clear()
        tick, stall = asyncio.create_task(_tick(world)), asyncio.create_task(stalled.wait())
        done, _ = await asyncio.wait({tick, stall}, return_when=asyncio.FIRST_COMPLETED)
        if tick in done:
            stall.cancel()
            statuses.append(tick.result())
            if statuses[-1] == "ok":
                break
            continue
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
        statuses.append("cancelled")
        # Every file of the cancelled part is back in staging, none left as an upload part.
        for (sha,) in _sql("SELECT sha256 FROM ingest_blobs WHERE filed_at IS NULL"):
            staged = trip_ingest.staging_dir(batch) / sha
            assert hashlib.sha256(staged.read_bytes()).hexdigest() == sha
        assert not list((world.home_root / "uploads").glob("*.part"))
    assert statuses == ["cancelled", "ok"], statuses
    parts = _sql("SELECT count(*), sum(size_bytes) FROM ingest_blobs GROUP BY filed_at")
    assert sum(n for n, _ in parts) == len(shots)
    assert all(
        n <= trip_ingest.FILE_PART_FILES and size <= trip_ingest.FILE_PART_BYTES
        for n, size in parts
    ), parts
    assert _sql(
        "SELECT count(*) FROM ingest_blobs WHERE filed_at IS NULL OR attachment_id IS NULL"
    ) == [(0,)]
    assert len(_memos()) == len(parts) == 3
    # Each file filed once: a visible picture, or a print bound to the screen of its part.
    _assert_prints_bind_only_beside_their_screen(len(shots))
    gets = [p for _, p, _ in world.wire.seen if p.startswith("/ingest/blobs/")]
    assert sorted(gets) == sorted(
        f"/ingest/blobs/{hashlib.sha256(d).hexdigest()}" for d, _ in shots
    )
    assert _home_batch(batch) == [("acked", True, True, False)]
    assert _pluto_batch(batch) == [("acked", True)]


def test_a_variant_group_is_one_group_whatever_the_case_of_its_names():
    blobs = _blobs(("Photo.jpg", 10), ("x.jpg", 10), ("PHOTO - Print.jpg", 10))
    parts = trip_ingest._filing_parts(blobs)
    _assert_parts(parts, blobs)
    assert parts == [[blobs[0], blobs[2], blobs[1]]]


async def test_two_filings_racing_over_a_cut_variant_group_file_each_file_once(world, monkeypatch):
    monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 2)
    trip = await _trip(world)
    shots = [(JPEG + bytes([i]), "p.jpg" if i == 0 else f"p - print (v{i}).jpg") for i in range(5)]
    batch = await _send(world, await _guest(world, trip), *shots)
    monkeypatch.setattr(trip_ingest, "file_open", _no_filing)
    assert await _tick(world) == "ok"
    [(batch_id,)] = _sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list((await db.execute(select(IngestBlob))).scalars())
    await trip_ingest.download(world.link, batch, rows, deadline=float("inf"))
    results = await asyncio.gather(
        trip_ingest.file_batch(batch_id), trip_ingest.file_batch(batch_id), return_exceptions=True
    )
    assert sorted(results, key=repr) == [False, True], results
    assert len(_memos()) == 3  # parts of 2, 2 and 1 files
    assert _sql("SELECT count(*) FROM ingest_blobs GROUP BY filed_at ORDER BY 1") == [
        (1,),
        (2,),
        (2,),
    ]
    _assert_prints_bind_only_beside_their_screen(len(shots))


# --- the P3a final audit: cancellation windows and a hostile ingest host -------------------------


async def _two_batches(w: World) -> tuple[str, str]:
    """Two one-file batches of one guest, in feed order: the first is the one a test turns hostile,
    the second must still file behind it."""
    trip = await _trip(w)
    token = await _guest(w, trip)
    return await _send(w, token, (JPEG, "a.jpg")), await _send(w, token, (JPEG + b"\x01", "b.jpg"))


def _json_answer(page: object) -> httpx.Response:
    """A rewritten service answer as the wire delivers one: an unread body."""
    body = json.dumps(page).encode()
    return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Body(body))


def _no_upload_parts(w: World) -> bool:
    return not list((w.home_root / "uploads").glob("*.part"))


async def test_a_cancel_landing_after_the_filing_commit_keeps_the_committed_originals(
    world, monkeypatch
):
    """PostgreSQL has committed the filing, but the cancellation lands before `commit()` returns.
    The memo, its attachments and their filed marks are durable, so their originals must stay where
    the rows say they are; walking them back to staging would lose them for good."""
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    real_memo, real_commit = passes_svc.create_guest_memo, AsyncSession.commit
    armed: dict[str, asyncio.Task | None] = {"task": None}
    parked = asyncio.Event()

    async def create_guest_memo(*args, **kwargs):
        memo = await real_memo(*args, **kwargs)
        armed["task"] = asyncio.current_task()  # the next commit of this task is the filing's
        return memo

    async def commit(self):
        await real_commit(self)
        if armed["task"] is not None and armed["task"] is asyncio.current_task():
            armed["task"] = None
            parked.set()
            await asyncio.Event().wait()  # committed on the server, not yet returned

    monkeypatch.setattr(passes_svc, "create_guest_memo", create_guest_memo)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    tick = asyncio.create_task(_tick(world))
    await asyncio.wait_for(parked.wait(), 30)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    [(path, sha)] = _sql("SELECT storage_path, sha256 FROM attachments")
    assert Path(path).read_bytes() == JPEG and sha == SHA_JPEG
    assert _sql("SELECT count(*) FROM ingest_blobs WHERE filed_at IS NOT NULL") == [(1,)]
    monkeypatch.setattr(passes_svc, "create_guest_memo", real_memo)
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1 and Path(path).read_bytes() == JPEG
    assert _home_batch(batch) == [("acked", True, True, False)]


async def test_a_cancel_before_the_staging_rename_leaves_the_verified_file_intact(
    world, monkeypatch
):
    """`create_upload` has made the upload's empty part file and the cancellation lands before the
    verified file is renamed over it: the empty file must not be walked back over the verified
    bytes, and no empty part file may be left behind."""
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    real = trip_ingest.to_thread
    parked = asyncio.Event()

    async def run_sync(func, *args, **kwargs):
        if func is trip_ingest._replace_sync and args[1].parent.name == "uploads":
            parked.set()
            await asyncio.Event().wait()  # the rename into the upload has not run
        return await real.run_sync(func, *args, **kwargs)

    monkeypatch.setattr(trip_ingest, "to_thread", SimpleNamespace(run_sync=run_sync))
    tick = asyncio.create_task(_tick(world))
    await asyncio.wait_for(parked.wait(), 30)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    staged = trip_ingest.staging_dir(batch) / SHA_JPEG
    assert staged.read_bytes() == JPEG
    assert _no_upload_parts(world)
    monkeypatch.setattr(trip_ingest, "to_thread", real)
    world.wire.seen.clear()
    assert await _tick(world) == "ok"
    assert len(_memos()) == 1 and _home_batch(batch) == [("acked", True, True, False)]
    assert not any(p.startswith("/ingest/blobs/") for _, p, _ in world.wire.seen)  # no re-download


async def test_a_declared_size_one_byte_too_large_is_quarantined_and_the_queue_moves_on(world):
    """Valid bytes that hash to their name, but a manifest size one byte larger: the complete
    answer ends short. That is the manifest's fault, not the link's, so it is quarantined at once
    instead of being resumed (416) and restarted on every tick with the batches behind it stuck."""
    first, second = await _two_batches(world)
    _sql(
        "UPDATE pluto.ingest_blobs SET size_bytes = size_bytes + 1 WHERE sha256 = :s",
        {"s": SHA_JPEG},
    )
    assert await _tick(world) == "ok"
    assert _pluto_batch(first) == [("quarantined", False)]
    assert _home_batch(first) == [("quarantined", False, False, True)]
    assert _sql("SELECT detail LIKE '%declared size%' FROM pass_events WHERE kind = 'refused'") == [
        (True,)
    ]
    assert _home_batch(second) == [("acked", True, True, False)]
    assert not trip_ingest.staging_dir(first).exists()


HOSTILE_ENTRY = {
    "device_id_list": ("device_id", []),
    "pass_uid_list": ("pass_uid", []),
    "device_id_nul": ("device_id", "dev\x00ice"),
    "pass_uid_nul": ("pass_uid", "pa\x00ss"),
    "caption_nul": ("caption", "be\x00ach"),
}


@pytest.mark.parametrize("case", sorted(HOSTILE_ENTRY))
async def test_a_malformed_feed_entry_is_quarantined_and_the_rest_of_its_page_files(world, case):
    field, value = HOSTILE_ENTRY[case]
    first, second = await _two_batches(world)

    async def poisoned(request, response):
        if request.url.path != "/ingest/batches":
            return response
        page = json.loads(await response.aread())
        for entry in page["batches"]:
            if entry["batch_id"] == first:
                entry[field] = value
        return _json_answer(page)

    world.wire.rewrite = poisoned
    assert await _tick(world) == "ok"
    assert _pluto_batch(first) == [("quarantined", False)]
    assert _home_batch(first) in ([], [("quarantined", False, False, True)])
    assert _home_batch(second) == [("acked", True, True, False)]
    assert len(_memos()) == 1


HOSTILE_DEVICE = {
    "display_name_nul": ("display_name", "An\x00na"),
    "uploads_count_past_int32": ("uploads_count", 2**31),
    "bytes_total_past_int64": ("bytes_total", 2**63),
    "last_seen_past_year_9999_in_utc": ("last_seen", "9999-12-31T23:00:00-05:00"),
}


@pytest.mark.parametrize("case", sorted(HOSTILE_DEVICE))
async def test_a_device_field_the_columns_cannot_hold_is_refused_not_a_page_abort(world, case):
    """Each value passed `_valid_device` and failed only in PostgreSQL, aborting the page's
    transaction and every page after it. The device is refused instead (so its batch is refused
    as one of an unknown device) and the other guest's batch files."""
    field, value = HOSTILE_DEVICE[case]
    trip = await _trip(world)
    first = await _send(world, await _guest(world, trip, "Anna"), (JPEG, "a.jpg"))
    second = await _send(world, await _guest(world, trip, "Bea"), (JPEG + b"\x01", "b.jpg"))

    async def poisoned(request, response):
        if request.url.path != "/ingest/devices":
            return response
        page = json.loads(await response.aread())
        for entry in page["devices"]:
            if entry["display_name"] == "Anna":
                entry[field] = value
        return _json_answer(page)

    world.wire.rewrite = poisoned
    assert await _tick(world) == "ok"
    assert _sql("SELECT display_name FROM pass_devices") == [("Bea",)]
    assert _home_batch(first) == [("quarantined", False, False, True)]
    assert _home_batch(second) == [("acked", True, True, False)]


async def test_a_last_modified_at_the_end_of_year_9999_files_and_leaves_no_part_file(world):
    """`9999-12-31T23:59:59.999999Z` is a valid timestamp, but the float conversion rounded it
    into year 10000: `create_upload` raised after it had made the part file, on every tick."""
    first, second = await _two_batches(world)
    _sql(
        "UPDATE pluto.ingest_blobs SET last_modified = '9999-12-31T23:59:59.999999Z' "
        "WHERE sha256 = :s",
        {"s": SHA_JPEG},
    )
    assert await _tick(world) == "ok"
    assert _home_batch(first) == [("acked", True, True, False)]
    assert _home_batch(second) == [("acked", True, True, False)]
    assert _no_upload_parts(world)
    assert _sql("SELECT count(*) FROM pass_sync_state WHERE last_error IS NOT NULL") == [(0,)]


@pytest.mark.parametrize(
    "field", ["last_modified", "completed_at"], ids=["last_modified", "completed_at"]
)
def test_a_timestamp_past_year_9999_in_utc_is_hostile(field):
    blob = {"sha256": SHA_JPEG, "size": len(JPEG), "mime": "image/jpeg", "filename": "a.jpg"}
    raw = {"batch_id": "b1", "pass_uid": "p1", "device_id": "d1", "blobs": [blob]}
    late = "9999-12-31T23:00:00-05:00"  # 10000-01-01T04:00Z: no datetime holds it
    if field == "last_modified":
        blob["last_modified"] = late
    else:
        raw["completed_at"] = late
    with pytest.raises(trip_ingest.Hostile):
        trip_ingest.validate_batch(raw)


async def test_create_upload_makes_no_part_file_when_it_raises(world):
    async with async_session() as db:
        with pytest.raises(OverflowError):
            await uploads_svc.create_upload(
                db,
                owner_id=world.owner_id,
                filename="a.jpg",
                mime="image/jpeg",
                size=10,
                last_modified=2**62,  # no datetime is that many milliseconds past 1970
            )
        await db.rollback()
    assert _no_upload_parts(world)


def _device(**over) -> dict:
    raw = {
        "device_id": "d1",
        "pass_uid": "p1",
        "display_name": "Anna",
        "platform": "web",
        "uploads_count": 0,
        "bytes_total": 0,
        "first_seen": "2031-03-01T10:00:00Z",
        "last_seen": None,
    }
    return raw | over


def test_a_device_count_at_its_column_maximum_is_kept_and_one_past_is_refused():
    top32, top64 = trip_ingest.INT32_MAX, trip_ingest.INT64_MAX
    assert (top32, top64) == (2_147_483_647, 9_223_372_036_854_775_807)
    kept = trip_ingest._valid_device(_device(uploads_count=top32, bytes_total=top64))
    assert kept is not None and (kept["uploads_count"], kept["bytes_total"]) == (top32, top64)
    assert trip_ingest._valid_device(_device(uploads_count=top32 + 1)) is None
    assert trip_ingest._valid_device(_device(bytes_total=top64 + 1)) is None
    assert trip_ingest._valid_device(_device(uploads_count=-1)) is None


def test_a_name_spelling_out_a_backslash_escape_is_text_not_a_nul():
    kept = trip_ingest._valid_device(_device(display_name="An\\x00na"))
    assert kept is not None and kept["display_name"] == "An\\x00na"
    assert trip_ingest._valid_device(_device(display_name="An\x00na")) is None
    assert trip_ingest._valid_device(_device(display_name="An\ud800na")) is None


async def test_create_upload_still_makes_the_part_file_it_hands_out(world):
    async with async_session() as db:
        up = await uploads_svc.create_upload(
            db, owner_id=world.owner_id, filename="a.jpg", mime="image/jpeg", size=10
        )
        await db.rollback()
    assert uploads_svc.part_path(up.upload_uid).read_bytes() == b""


async def test_a_cancelled_download_keeps_the_tick_lock_until_its_write_returns(world, monkeypatch):
    """A worker thread cannot be cancelled. A cancellation landing while it appends to a staging
    file must not unwind the tick (and release its advisory lock) around it: the next tick would
    then write the same partial file while this thread still does."""
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    real = trip_ingest._append_hashed_sync
    entered, release = threading.Event(), threading.Event()

    def parked(*args):
        entered.set()
        release.wait(30)
        real(*args)

    monkeypatch.setattr(trip_ingest, "_append_hashed_sync", parked)
    tick = asyncio.create_task(_tick(world))
    assert await asyncio.to_thread(entered.wait, 30)
    tick.cancel()
    try:
        done, _ = await asyncio.wait({tick}, timeout=1)
        assert not done, "the cancelled tick returned while its writer thread was still running"
        free = _sql("SELECT pg_try_advisory_xact_lock(:k)", {"k": trip_ingest.SYNC_LOCK_KEY})
        assert free == [(False,)]
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await tick
    partial = trip_ingest.staging_dir(batch) / f"{SHA_JPEG}.part"
    assert partial.read_bytes() == JPEG  # the write the thread was in landed whole


# --- security gate p1: zoneless timestamps, cursor range, hashed answer ids ---------------------

YEAR_ONE = {
    "naive": ("0001-01-01T00:00:00", datetime(1, 1, 1, tzinfo=UTC)),
    "utc": ("0001-01-01T00:00:00Z", datetime(1, 1, 1, tzinfo=UTC)),
    "east_of_utc": ("0001-01-01T00:00:00+01:00", None),  # year 0 in UTC: refused
}


@pytest.mark.parametrize("edge", sorted(YEAR_ONE))
@pytest.mark.parametrize("field", ["first_seen", "completed_at", "last_modified"])
def test_a_year_one_timestamp_is_read_as_utc_or_refused_never_an_escaping_error(field, edge):
    value, expected = YEAR_ONE[edge]
    if field == "first_seen":
        kept = trip_ingest._valid_device(_device(first_seen=value))
        assert (kept["first_seen"] if kept else None) == expected
        return
    blob = {"sha256": SHA_JPEG, "size": len(JPEG), "mime": "image/jpeg", "filename": "a.jpg"}
    raw = {"batch_id": "b1", "pass_uid": "p1", "device_id": "d1", "blobs": [blob]}
    (blob if field == "last_modified" else raw)[field] = value
    if expected is None:
        with pytest.raises(trip_ingest.Hostile):
            trip_ingest.validate_batch(raw)
        return
    batch = trip_ingest.validate_batch(raw)
    got = batch.blobs[0].last_modified if field == "last_modified" else batch.completed_at
    assert got == expected and got.tzinfo is not None


async def test_a_zoneless_year_one_first_seen_does_not_stall_the_device_feed(world):
    trip = await _trip(world)
    batch = await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))

    async def year_one(request, response):
        if request.url.path != "/ingest/devices":
            return response
        page = json.loads(await response.aread())
        for entry in page["devices"]:
            entry["first_seen"] = "0001-01-01"
        return _json_answer(page)

    world.wire.rewrite = year_one
    assert await _tick(world) == "ok"
    assert _home_batch(batch) == [("acked", True, True, False)]


@pytest.mark.parametrize("feed", ["devices", "batches"])
async def test_a_cursor_past_bigint_is_a_malformed_page_and_bigint_max_is_kept(world, feed):
    trip = await _trip(world)
    await _send(world, await _guest(world, trip), (JPEG, "a.jpg"))
    nxt = {"value": 2**63}

    async def far(request, response):
        if request.url.path != f"/ingest/{feed}":
            return response
        page = json.loads(await response.aread())
        if page[feed]:
            page["next"] = nxt["value"]
        return _json_answer(page)

    world.wire.rewrite = far
    column = f"{feed}_since"
    assert await _tick(world) == "link_down"
    assert _sql(f"SELECT {column} FROM trip_ingest_cursor") == [(0,)]
    nxt["value"] = 2**63 - 1
    assert await _tick(world) == "ok"
    assert _sql(f"SELECT {column} FROM trip_ingest_cursor") == [(2**63 - 1,)]


@pytest.mark.parametrize("push", ["passes", "device-revocations"])
async def test_an_unhashable_id_in_a_push_answer_is_ignored_not_a_tick_error(world, push):
    trip = await _trip(world)
    await _guest(world, trip)
    assert await _tick(world) == "ok"  # the device is mirrored, the pass acknowledged
    _sql("UPDATE pass_devices SET revoked_at = now(), revoked_version = 1")
    _sql("UPDATE pass_sync_state SET acked_version = 0")

    async def unhashable(request, response):
        if request.url.path != f"/ingest/{push}":
            return response
        await response.aread()
        if push == "passes":
            return _json_answer([{"uid": [], "version": 1}])
        return _json_answer({"applied": [{"device_id": []}], "unknown": []})

    world.wire.rewrite = unhashable
    assert await _tick(world) == "ok"
