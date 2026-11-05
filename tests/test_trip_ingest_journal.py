"""The filing's move journal and the cancel-record deadline (MEMNG-1037 #2, #3 and its follow-up).

The crash tests run the REAL filing in a subprocess and SIGKILL it, because the in-memory move list
is exactly what a hard crash loses and no in-process raise can prove otherwise."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app.database import async_session
from app.models import IngestBlob, IngestMove, Pass, User
from app.services import passes as passes_svc
from app.services import storage, trip_ingest
from sqlalchemy import select, text

from tests import test_trip_ingest_puller as tp
from tests.ingest_support import JPEG
from tests.test_lock_protocol_concurrency import WAIT_TIMEOUT, Park, _lock_waiters
from tests.test_lock_protocol_concurrency import _wait_for_a_new_lock_wait as _wait_lock

pluto_schema = tp.pluto_schema
world = tp.world

CRASH = """
import asyncio, os, signal, sys
from app.services import passes, storage, trip_ingest
def die(*a, **k):
    os.kill(os.getpid(), signal.SIGKILL)
async def adie(*a, **k):
    die()
point, batch_id = sys.argv[1], int(sys.argv[2])
if point == "before_journaling":
    trip_ingest._MoveJournal.record_many = adie
elif point == "before_part_rename":
    trip_ingest._replace_sync = die
elif point == "before_reservation_token":
    real_write = os.write
    def write(fd, data):
        if bytes(data).startswith(storage.RESERVATION_PREFIX):
            die()
        return real_write(fd, data)
    os.write = write
elif point == "before_master_rename":
    storage._before_journaled_rename = die
else:
    real = passes.create_guest_memo
    async def then_die(*a, **k):
        await real(*a, **k)
        die()
    passes.create_guest_memo = then_die
asyncio.run(trip_ingest._file_part(batch_id))
"""


async def _downloaded(w, monkeypatch, *files: tuple[bytes, str]) -> tuple[str, int]:
    trip = await tp._trip(w)
    token = await tp._guest(w, trip)
    batch = await tp._send(w, token, *files)
    real = trip_ingest.file_open
    monkeypatch.setattr(trip_ingest, "file_open", tp._no_filing)
    assert await tp._tick(w) == "ok"
    monkeypatch.setattr(trip_ingest, "file_open", real)
    [(batch_id,)] = tp._sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list(
            (await db.execute(select(IngestBlob).where(IngestBlob.batch_id == batch_id))).scalars()
        )
    await trip_ingest.download(w.link, batch, rows, deadline=float("inf"))
    return batch, batch_id


def _files(root: Path, sub: str) -> list[Path]:
    """Files under `sub`, the batch staging area aside: that is where the sources belong."""
    staging = trip_ingest.staging_dir("x").parent
    return [p for p in (root / sub).rglob("*") if p.is_file() and not p.is_relative_to(staging)]


def _rows() -> int:
    return tp._sql("SELECT count(*) FROM ingest_moves")[0][0]


def _verified(batch: str) -> dict[str, int]:
    return {
        p.name: p.stat().st_size
        for p in trip_ingest.staging_dir(batch).rglob("*")
        if p.is_file() and p.stat().st_size > 0
    }


@pytest.mark.parametrize(
    "point",
    [
        "before_journaling",
        "before_part_rename",
        "before_reservation_token",
        "before_master_rename",
        "after_placement",
    ],
)
async def test_a_killed_filing_is_walked_back_and_refiles_clean(world, monkeypatch, point):
    batch, batch_id = await _downloaded(
        world, monkeypatch, (JPEG + b"1", "a.jpg"), (JPEG + b"2", "b.jpg")
    )
    staged = _verified(batch)
    assert len(staged) == 2
    env = {**os.environ, "DATA_ROOT": str(world.home_root)}
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", CRASH, point, str(batch_id)],
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == -9, done.stderr[-2000:]
    if point == "before_journaling":
        assert _rows() == 0
        assert _files(world.home_root, "uploads") == [], "a placeholder preceded its record"
    else:
        assert _rows() > 0
    if point == "before_reservation_token":
        empty = [p for p in _files(world.home_root, "originals") if p.stat().st_size == 0]
        assert len(empty) == 1, "the kill did not land between the create and the token"
    await trip_ingest.reconcile_moves()
    assert _rows() == 0
    assert _verified(batch) == staged, "the source bytes are not back in staging"
    assert _files(world.home_root, "uploads") == []
    assert _files(world.home_root, "originals") == []
    assert await tp._tick(world) == "ok"
    assert [p for p in _files(world.home_root, "originals") if p.stat().st_size == 0] == []
    assert len(_files(world.home_root, "originals")) == 2
    assert tp._sql("SELECT count(*) FROM attachments WHERE memo_id IS NOT NULL")[0][0] == 2


async def _journal_row(tmp: Path, src: Path, dest: Path, kind: str = "master") -> int:
    return await trip_ingest._MoveJournal(999).record(str(src), str(dest), kind)


async def test_both_present_overwrites_nothing_and_keeps_the_row(world, tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(b"source")
    dest.write_bytes(b"somebody else")
    row = await _journal_row(tmp_path, src, dest)
    await trip_ingest.reconcile_moves()
    assert (src.read_bytes(), dest.read_bytes()) == (b"source", b"somebody else")
    assert tp._sql("SELECT flagged_at IS NOT NULL FROM ingest_moves WHERE id = :i", {"i": row}) == [
        (True,)
    ]
    src.unlink()  # a flagged row is never walked again, even once its source has gone
    await trip_ingest.reconcile_moves()
    assert dest.read_bytes() == b"somebody else"


async def test_a_placeholder_of_another_row_is_not_this_rows_to_remove(world, tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(b"source")
    row = await _journal_row(tmp_path, src, dest)
    dest.write_bytes(storage.reservation_token(row + 1))
    await trip_ingest.reconcile_moves()
    assert dest.exists(), "another writer's reservation was deleted"
    assert src.exists()


async def test_reverse_order_restores_a_whole_chain_to_verified(world, tmp_path):
    verified, part, unfiled, memo = (tmp_path / n for n in ("v", "p", "u", "m"))
    memo.write_bytes(b"bytes")
    for src, dest, kind in (
        (verified, part, "part"),
        (part, unfiled, "master"),
        (unfiled, memo, "master"),
    ):
        await _journal_row(tmp_path, src, dest, kind)
    await trip_ingest.reconcile_moves()
    assert verified.read_bytes() == b"bytes"
    assert not part.exists() and not unfiled.exists() and not memo.exists()
    assert _rows() == 0


async def test_a_failed_hop_keeps_its_chain_and_a_retry_restores_it_to_verified(
    world, tmp_path, monkeypatch
):
    """A hop that will not move back stops its batch: the earlier rows depend on it, and walked now
    they read "neither present" and are dropped, stranding the original with no journal."""
    verified, part, unfiled, memo = (tmp_path / n for n in ("v", "p", "u", "m"))
    memo.write_bytes(b"bytes")
    for src, dest, kind in (
        (verified, part, "part"),
        (part, unfiled, "master"),
        (unfiled, memo, "master"),
    ):
        await _journal_row(tmp_path, src, dest, kind)
    real = os.replace

    def refuses_the_memo_hop(src, dest, *a, **k):
        if Path(src) == memo:
            raise PermissionError(13, "injected", str(src))
        return real(src, dest, *a, **k)

    monkeypatch.setattr(os, "replace", refuses_the_memo_hop)
    with pytest.raises(trip_ingest.ReconcileIncomplete):
        await trip_ingest.reconcile_moves()
    assert memo.read_bytes() == b"bytes"
    assert _rows() == 3, "a dependent row went while its hop stood"
    monkeypatch.setattr(os, "replace", real)
    await trip_ingest.reconcile_moves()
    assert verified.read_bytes() == b"bytes"
    assert not part.exists() and not unfiled.exists() and not memo.exists()
    assert _rows() == 0


async def test_an_empty_file_at_a_journaled_dest_goes_only_while_the_source_stands(world, tmp_path):
    """A master is never 0 bytes, and a source present means the rename never ran: the empty file is
    the reservation a kill caught before its token. With the source gone it is what the row moved,
    so it walks back rather than disappear."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(b"source")
    dest.write_bytes(b"")
    await _journal_row(tmp_path, src, dest)
    await trip_ingest.reconcile_moves()
    assert src.read_bytes() == b"source" and not dest.exists()
    assert _rows() == 0
    gone, kept = tmp_path / "gone", tmp_path / "kept"
    kept.write_bytes(b"")
    await _journal_row(tmp_path, gone, kept)
    await trip_ingest.reconcile_moves()
    assert gone.exists() and not kept.exists()
    assert _rows() == 0


async def test_reconcile_waits_out_a_filing_still_running(world, monkeypatch):
    _batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    park = Park()
    real = passes_svc.create_guest_memo

    async def parked(*args, **kwargs):
        memo = await real(*args, **kwargs)
        if park.claim():
            await park.hold()
        return memo

    monkeypatch.setattr(passes_svc, "create_guest_memo", parked)
    filing = asyncio.ensure_future(trip_ingest._file_part(batch_id))
    await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
    # Rows committed from their own session while the filing holds the batch: no self-block.
    assert _rows() > 0
    before = set(await asyncio.to_thread(_lock_waiters))
    reconcile = asyncio.ensure_future(trip_ingest.reconcile_moves())
    await _wait_lock(before)
    assert not reconcile.done()
    park.release.set()
    assert await filing is True
    await reconcile
    assert _rows() == 0
    # The waiting reconcile read the journal AFTER the filing committed, so it moved nothing back.
    assert len(_files(world.home_root, "originals")) == 1


async def test_a_taken_name_re_journals_under_the_next_one(world, tmp_path):
    src, folder = tmp_path / "src", tmp_path / "memo"
    src.write_bytes(b"new")
    folder.mkdir()
    (folder / "a.jpg").write_bytes(b"old")
    moves = trip_ingest._JournaledMoves(trip_ingest._MoveJournal(998))
    track = storage.TrackedMove(src=str(src))
    moves.append(track)
    placed = await storage.place_master(src, folder, "a.jpg", track)
    assert placed.name == "a (2).jpg" and placed.read_bytes() == b"new"
    assert (folder / "a.jpg").read_bytes() == b"old"
    assert tp._sql("SELECT dest FROM ingest_moves") == [(str(placed),)]


async def test_a_refusal_records_the_version_that_admitted_the_batch(world, monkeypatch):
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    [(admitted, pass_id)] = tp._sql(
        "SELECT pass_version, pass_id FROM ingest_batches WHERE id = :b", {"b": batch_id}
    )
    assert admitted is not None
    async with async_session() as db:
        owner = await db.get(User, world.owner_id)
        p = await db.get(Pass, pass_id)
        assert owner is not None and p is not None
        await passes_svc.update_pass(db, p, owner, {"label": "edited"})
        await db.commit()
    assert tp._sql("SELECT version FROM passes WHERE id = :p", {"p": pass_id})[0][0] > admitted
    await trip_ingest._refuse_open(batch_id, "bad bytes")
    assert tp._sql(
        "SELECT version FROM pass_events WHERE pass_id = :p AND kind = 'refused'", {"p": pass_id}
    ) == [(admitted,)]


async def test_the_cancel_record_ends_inside_its_deadline_against_a_held_row(world):
    await tp._tick(world)
    async with async_session() as holder:
        await holder.execute(text("SELECT id FROM trip_ingest_cursor FOR UPDATE"))
        started = time.monotonic()
        task = asyncio.ensure_future(trip_ingest._record_cancelled())
        await asyncio.shield(task)
        elapsed = time.monotonic() - started
        assert task.done() and not task.cancelled()
        assert elapsed < trip_ingest.CANCEL_RECORD_DEADLINE_S + 0.5, elapsed
        await holder.rollback()
    # Nothing still queued on the row: the database task ended with the deadline.
    waiting = tp._sql(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
        "AND datname = current_database()"
    )
    assert waiting == [(0,)]
    started = time.monotonic()
    await trip_ingest._record_cancelled()
    print(f"cancel-record uncontended: {(time.monotonic() - started) * 1000:.1f} ms")
    assert tp._sql("SELECT last_error FROM trip_ingest_cursor") == [(trip_ingest.TICK_CANCELLED,)]


async def test_a_full_part_files_inside_the_housekeeping_bound(world, monkeypatch):
    files = tuple((JPEG + str(i).encode(), f"f{i}.jpg") for i in range(25))
    _batch, batch_id = await _downloaded(world, monkeypatch, *files)
    started = time.monotonic()
    assert await trip_ingest._file_part(batch_id) is True
    elapsed = time.monotonic() - started
    print(f"25-file part with journal: {elapsed:.2f} s")
    assert elapsed < 14
    assert await asyncio.to_thread(_rows) == 0
    async with async_session() as db:
        assert (await db.execute(select(IngestMove))).first() is None
