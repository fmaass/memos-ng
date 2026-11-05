"""Two-actor lock-order probes for the trip filing path (MEMNG-1037 item 1, docs/LOCK-ORDER.md).

A filing part of an ALBUM pass meets every owner writer that locks the album's collection row or
its pass: the collection delete (C FOR UPDATE, then the live passes FOR UPDATE), an album edit
(C FOR UPDATE), an album member add or remove (A KEY SHARE, then C), and a pass edit or revoke
(P FOR UPDATE). Each pair is run both ways round: one actor is PARKED holding its locks, the other
is started and proved through `pg_stat_activity` to be waiting, and the relation it waits on is
read from `pg_locks` before the park is released. Then both must finish without SQLSTATE 40P01,
and the wait must have been on the class the global order puts first between the two (the filing
takes C before it touches P, so it waits for an album writer at the collection and for a pass
writer at the pass, and either of them waits for it the same way).

On the tree before the fix the filing linked the memo to its pass (an implicit KEY SHARE on P)
before it filed the album's member rows (C), and the collection delete takes C, then P: both
`delete` probes deadlock there, and the filer-first `add`/`remove` probes find the owner writer
never waits at all, because the filer had not reached the album row yet.
"""

import asyncio

import pytest
from app.database import async_session
from app.models import Collection, IngestBlob, Pass, User
from app.routers import memos as memos_router
from app.services import collections as coll_svc
from app.services import memos as memos_svc
from app.services import passes as passes_svc
from app.services import reclaim, trip_ingest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tests import test_trip_ingest_puller as tp
from tests.conftest import SYNC_URL
from tests.ingest_support import JPEG
from tests.test_lock_protocol_concurrency import (
    WAIT_TIMEOUT,
    Park,
    _lock_waiters,
    _wait_for_a_new_lock_wait,
)

pluto_schema = tp.pluto_schema
world = tp.world

WRITERS = ("delete", "rename", "add_member", "remove_member", "edit_pass", "revoke_pass")
# The class each pair meets at first, in the one global order (docs/LOCK-ORDER.md).
MEETS_AT = {
    "delete": "collections",
    "rename": "collections",
    "add_member": "collections",
    "remove_member": "collections",
    "edit_pass": "passes",
    "revoke_pass": "passes",
}


def _waited_relation(pid: int) -> str | None:
    """The table whose row `pid` is queued for: a row-lock waiter holds the heavyweight TUPLE lock
    of the row it waits on while it waits for the holder's transaction to end."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as c:
            return c.execute(
                text(
                    "SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE l.pid = :pid AND l.locktype = 'tuple'"
                ),
                {"pid": pid},
            ).scalar_one_or_none()
    finally:
        eng.dispose()


def _deadlocks(results: list) -> list[BaseException]:
    return [
        r
        for r in results
        if isinstance(r, BaseException)
        and getattr(getattr(r, "orig", None), "sqlstate", None) == "40P01"
    ]


async def _album_with_a_downloaded_batch(w, monkeypatch) -> tuple[tp.Trip, int, int]:
    """An album, one picture already in it (filed from a first batch), and a second batch
    recorded and downloaded but not filed. Returns the trip, the picture's attachment id and the
    second batch's ledger id."""
    trip = await tp._trip(w, kind="album")
    token = await tp._guest(w, trip)
    await tp._send(w, token, (JPEG + b"seed", "seed.jpg"))
    assert await tp._tick(w) == "ok"
    [(picture,)] = tp._sql("SELECT attachment_id FROM collection_members")
    batch = await tp._send(w, token, (JPEG, "a.jpg"), (JPEG + b"b", "b.jpg"))
    real_file_open = trip_ingest.file_open
    monkeypatch.setattr(trip_ingest, "file_open", tp._no_filing)
    assert await tp._tick(w) == "ok"
    monkeypatch.setattr(trip_ingest, "file_open", real_file_open)
    [(batch_id,)] = tp._sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list(
            (await db.execute(select(IngestBlob).where(IngestBlob.batch_id == batch_id))).scalars()
        )
    await trip_ingest.download(w.link, batch, rows, deadline=float("inf"))
    return trip, picture, batch_id


async def _write(kind: str, w, trip: tp.Trip, picture: int) -> None:
    """One owner writer, in its own transaction, committed."""
    async with async_session() as db:
        owner = await db.get(User, w.owner_id)
        collection = await db.get(Collection, trip.collection_id)
        p = (await db.execute(select(Pass).where(Pass.uid == trip.pass_uid))).scalar_one()
        assert owner is not None and collection is not None
        if kind == "delete":
            await coll_svc.delete_collection(db, collection, owner)
        elif kind == "rename":
            await coll_svc.update_collection(db, collection, {"title": "Renamed"})
        elif kind == "add_member":
            await coll_svc.add_member(db, trip.collection_id, picture, added_by=owner.id)
        elif kind == "remove_member":
            await coll_svc.remove_member(db, trip.collection_id, picture)
        elif kind == "edit_pass":
            await passes_svc.update_pass(db, p, owner, {"label": "edited"})
        else:
            await passes_svc.revoke_pass(db, p, owner)
        await db.commit()


def _park_writer(monkeypatch, kind: str, park: Park) -> None:
    """Park the owner writer the instant it holds its first lock (for a member write, both of its
    locks: the picture's KEY SHARE and the album row), before it writes anything."""

    def before(module, name: str) -> None:
        real = getattr(module, name)

        async def parked(*args, **kwargs):
            if park.claim():
                await park.hold()
            return await real(*args, **kwargs)

        monkeypatch.setattr(module, name, parked)

    if kind == "delete":
        before(passes_svc, "revoke_collection_passes")  # C held FOR UPDATE, P not yet asked
    elif kind in ("add_member", "remove_member"):
        before(coll_svc, "_touch")
    elif kind in ("edit_pass", "revoke_pass"):
        before(passes_svc, "_event")  # P held FOR UPDATE by `_lock_pass`
    else:
        real = AsyncSession.execute

        async def parking(self, statement, *args, **kwargs):
            result = await real(self, statement, *args, **kwargs)
            sql = " ".join(str(statement).split())
            if (
                sql.startswith("SELECT collections.")
                and sql.endswith("FOR UPDATE")
                and park.claim()
            ):
                await park.hold()
            return result

        monkeypatch.setattr(AsyncSession, "execute", parking)


def _park_filer(monkeypatch, park: Park) -> None:
    """Park the filing part the instant its guest memo exists: everything the part locks is held
    then, except the batch ledger marks it writes last."""
    real = passes_svc.create_guest_memo

    async def parked(*args, **kwargs):
        memo = await real(*args, **kwargs)
        if park.claim():
            await park.hold()
        return memo

    monkeypatch.setattr(passes_svc, "create_guest_memo", parked)


@pytest.mark.parametrize("filer_first", [True, False], ids=["filer_parked", "writer_parked"])
@pytest.mark.parametrize("kind", WRITERS)
async def test_a_filing_part_and_an_owner_writer_meet_in_one_order(
    world, monkeypatch, kind, filer_first
):
    trip, picture, batch_id = await _album_with_a_downloaded_batch(world, monkeypatch)
    if kind == "add_member":
        async with async_session() as db:
            assert await coll_svc.remove_member(db, trip.collection_id, picture)
            await db.commit()
    park = Park()
    if filer_first:
        _park_filer(monkeypatch, park)
        first = asyncio.ensure_future(trip_ingest._file_part(batch_id))
    else:
        _park_writer(monkeypatch, kind, park)
        first = asyncio.ensure_future(_write(kind, world, trip, picture))
    await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
    before = set(await asyncio.to_thread(_lock_waiters))
    second = asyncio.ensure_future(
        _write(kind, world, trip, picture) if filer_first else trip_ingest._file_part(batch_id)
    )
    try:
        pid = await _wait_for_a_new_lock_wait(before)
        relation = await asyncio.to_thread(_waited_relation, pid)
    finally:
        park.release.set()
        results = list(await asyncio.gather(first, second, return_exceptions=True))
    park.fired()
    assert _deadlocks(results) == [], results
    assert relation == MEETS_AT[kind], f"waited on {relation}, not {MEETS_AT[kind]}"
    filer = results[0] if filer_first else results[1]
    if kind == "delete" and not filer_first:
        # The album went first: the part finds it gone and files nothing, its files kept staged.
        assert isinstance(filer, coll_svc.CollectionGone), filer
        assert tp._sql("SELECT count(*) FROM ingest_blobs WHERE filed_at IS NOT NULL") == [(1,)]
    else:
        assert filer is True, filer
        assert not isinstance(results[1] if filer_first else results[0], BaseException), results


# --- The memo and attachment deletes (MEMNG-1037's follow-up). Every writer the plan's grep finds:
# `delete(Memo` / `delete(Attachment` / `db.delete(` on either, in app/. Each writes a foreign key
# of H through ON DELETE SET NULL: `ingest_batches.memo_id` or `ingest_blobs.attachment_id`.
DELETES = ("delete_memo", "reclaim_identity", "reclaim_orphaned", "reclaim_sync")


async def _two_part_batch(w, monkeypatch) -> tuple[tp.Trip, int, str, int]:
    """A batch of two parts with its first part filed: a real memo, `batch.memo_id` set, the second
    part still to come. Returns the trip, the batch id, the memo uid and the first picture's id."""
    trip = await tp._trip(w)
    token = await tp._guest(w, trip)
    monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 1)
    batch = await tp._send(w, token, (JPEG + b"1", "one.jpg"), (JPEG + b"2", "two.jpg"))
    real_file_open = trip_ingest.file_open
    monkeypatch.setattr(trip_ingest, "file_open", tp._no_filing)
    assert await tp._tick(w) == "ok"
    monkeypatch.setattr(trip_ingest, "file_open", real_file_open)
    [(batch_id,)] = tp._sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list(
            (await db.execute(select(IngestBlob).where(IngestBlob.batch_id == batch_id))).scalars()
        )
    await trip_ingest.download(w.link, batch, rows, deadline=float("inf"))
    assert await trip_ingest._file_part(batch_id) is False
    [(memo_uid, picture)] = tp._sql(
        "SELECT m.uid, min(a.id) FROM memos m JOIN attachments a ON a.memo_id = m.id "
        "JOIN ingest_batches b ON b.memo_id = m.id WHERE b.id = :b GROUP BY m.uid",
        {"b": batch_id},
    )
    return trip, batch_id, memo_uid, picture


async def _delete(kind: str, w, memo_uid: str, picture: int) -> None:
    if kind == "delete_memo":
        async with async_session() as db:
            owner = await db.get(User, w.owner_id)
            assert owner is not None
            await memos_router.delete_memo(memo_uid, owner, db)
    elif kind == "reclaim_identity":
        result = await reclaim.reclaim_row(picture, reclaim.Revalidate.IDENTITY)
        assert result.outcome == "reclaimed", result
    elif kind == "reclaim_orphaned":
        [(detached_at,)] = tp._sql(
            "SELECT detached_at FROM attachments WHERE id = :a", {"a": picture}
        )
        result = await reclaim.reclaim_row(
            picture, reclaim.Revalidate.ORPHANED, detached_at=detached_at
        )
        assert result.outcome == "reclaimed", result
    else:

        def run() -> None:
            eng = create_engine(SYNC_URL)
            try:
                with Session(eng) as session:
                    outcome = reclaim.reclaim_attachment_sync(
                        session, picture, reclaim.Revalidate.IDENTITY
                    ).outcome
                    assert outcome == "reclaimed", outcome
            finally:
                eng.dispose()

        await asyncio.to_thread(run)


def _park_delete(monkeypatch, kind: str, park: Park) -> None:
    """Park the delete holding its first locks and before it writes: the memo delete holds M (and,
    once fixed, H before it), a reclaim holds its J and A rows."""
    if kind == "delete_memo":
        real = memos_svc.delete_memo

        async def parked(*args, **kwargs):
            if park.claim():
                await park.hold()
            return await real(*args, **kwargs)

        monkeypatch.setattr(memos_svc, "delete_memo", parked)
        return
    if kind != "reclaim_sync":
        real_unlink = reclaim._unlink_off_the_loop

        async def parked_unlink(*args, **kwargs):
            if park.claim():
                await park.hold()
            return await real_unlink(*args, **kwargs)

        monkeypatch.setattr(reclaim, "_unlink_off_the_loop", parked_unlink)
        return
    real_paths = reclaim.all_asset_paths
    loop = asyncio.get_running_loop()

    def parking(att):
        if park.claim():
            asyncio.run_coroutine_threadsafe(park.hold(), loop).result()
        return real_paths(att)

    monkeypatch.setattr(reclaim, "all_asset_paths", parking)


def _park_second_part(monkeypatch, park: Park, *, holding_memo: bool) -> None:
    """Park a later filing part holding H and not yet M, or after it has taken M and its A set."""
    real = passes_svc.append_guest_files

    async def parked(*args, **kwargs):
        if not holding_memo and park.claim():
            await park.hold()
        memo = await real(*args, **kwargs)
        if holding_memo and park.claim():
            await park.hold()
        return memo

    monkeypatch.setattr(passes_svc, "append_guest_files", parked)


@pytest.mark.parametrize("delete_first", [False, True], ids=["filer_parked", "delete_parked"])
@pytest.mark.parametrize("holding_memo", [False, True], ids=["before_memo", "holding_memo"])
async def test_a_memo_delete_and_a_later_filing_part_meet_at_the_batch(
    world, monkeypatch, holding_memo, delete_first
):
    """The cycle the gate named: filing H -> M, the memo delete M -> H through SET NULL."""
    _trip, batch_id, memo_uid, picture = await _two_part_batch(world, monkeypatch)
    park = Park()
    if delete_first:
        _park_delete(monkeypatch, "delete_memo", park)
        first = asyncio.ensure_future(_delete("delete_memo", world, memo_uid, picture))
    else:
        _park_second_part(monkeypatch, park, holding_memo=holding_memo)
        first = asyncio.ensure_future(trip_ingest._file_part(batch_id))
    await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
    before = set(await asyncio.to_thread(_lock_waiters))
    second = asyncio.ensure_future(
        trip_ingest._file_part(batch_id)
        if delete_first
        else _delete("delete_memo", world, memo_uid, picture)
    )
    try:
        pid = await _wait_for_a_new_lock_wait(before)
        relation = await asyncio.to_thread(_waited_relation, pid)
    finally:
        park.release.set()
        results = list(await asyncio.gather(first, second, return_exceptions=True))
    park.fired()
    assert _deadlocks(results) == [], results
    assert relation == "ingest_batches", f"waited on {relation}: M taken before H"
    assert not any(isinstance(r, BaseException) for r in results), results


@pytest.mark.parametrize("kind", DELETES[1:])
@pytest.mark.parametrize("delete_first", [False, True], ids=["filer_parked", "delete_parked"])
async def test_an_attachment_reclaim_beside_a_later_filing_part_never_deadlocks(
    world, monkeypatch, kind, delete_first
):
    """A reclaim holds J -> A and writes `ingest_blobs.attachment_id` of an EARLIER part, a row a
    filing part never locks, so the two have no cycle. The parked side releases only once the
    other has finished or queued; either way both end, with no 40P01."""
    _trip, batch_id, memo_uid, picture = await _two_part_batch(world, monkeypatch)
    if kind == "reclaim_orphaned":
        tp._sql(
            "UPDATE attachments SET memo_id = NULL, detached_at = now() WHERE id = :a RETURNING id",
            {"a": picture},
        )
    park = Park()
    if delete_first:
        _park_delete(monkeypatch, kind, park)
        first = asyncio.ensure_future(_delete(kind, world, memo_uid, picture))
    else:
        _park_second_part(monkeypatch, park, holding_memo=True)
        first = asyncio.ensure_future(trip_ingest._file_part(batch_id))
    await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
    second = asyncio.ensure_future(
        trip_ingest._file_part(batch_id)
        if delete_first
        else _delete(kind, world, memo_uid, picture)
    )
    await asyncio.wait([second], timeout=2.0)
    park.release.set()
    results = list(await asyncio.gather(first, second, return_exceptions=True))
    park.fired()
    assert _deadlocks(results) == [], results
    assert not any(isinstance(r, BaseException) for r in results), results
