"""The filing's move journal and the cancel-record deadline (MEMNG-1037 #2, #3 and its follow-up).

The crash tests run the REAL filing in a subprocess and SIGKILL it, because the in-memory move list
is exactly what a hard crash loses and no in-process raise can prove otherwise."""

import asyncio
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from app.database import async_session
from app.models import Attachment, AttachmentStatus, IngestBlob, IngestMove, Pass, User
from app.services import passes as passes_svc
from app.services import storage, trip_ingest
from sqlalchemy import select, text

from tests import test_trip_ingest_puller as tp
from tests.ingest_support import JPEG
from tests.test_lock_protocol_concurrency import WAIT_TIMEOUT, Park, _lock_waiters
from tests.test_lock_protocol_concurrency import _wait_for_a_new_lock_wait as _wait_lock
from tests.test_trip_filing_lock_order import _two_part_batch

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


@pytest.mark.parametrize("found", [b"somebody else", b""])
async def test_another_writers_file_at_a_journaled_master_dest_survives_reconcile(
    world, tmp_path, found
):
    """A later part's memo folder is committed, so an owner upload can reserve the very name a
    crashed filing journaled. Empty or not, without this row's token the file is not this row's."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(b"source")
    dest.write_bytes(found)
    row = await _journal_row(tmp_path, src, dest, "master")
    await trip_ingest.reconcile_moves()
    assert (src.read_bytes(), dest.read_bytes()) == (b"source", found)
    assert tp._sql("SELECT flagged_at IS NOT NULL FROM ingest_moves WHERE id = :i", {"i": row}) == [
        (True,)
    ]


async def test_a_kill_before_the_token_flags_the_dest_and_the_part_refiles_under_the_next_name(
    world, monkeypatch
):
    """The reservation is created and the kill lands before its token, in a later part, so the name
    is in a committed memo's folder. Nothing proves that empty file is ours: it stays, its row is
    flagged, and the part must still file, because a flagged row may not wedge the batch."""
    _trip, batch_id, _memo, _picture = await _two_part_batch(world, monkeypatch)
    [(batch,)] = tp._sql("SELECT uid FROM ingest_batches WHERE id = :b", {"b": batch_id})
    staged = _verified(batch)
    assert len(staged) == 1
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", CRASH, "before_reservation_token", str(batch_id)],
        env={**os.environ, "DATA_ROOT": str(world.home_root)},
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == -9, done.stderr[-2000:]
    empty = [p for p in _files(world.home_root, "originals") if p.stat().st_size == 0]
    assert len(empty) == 1, "the kill did not land between the create and the token"
    expected = {"one.jpg": JPEG + b"1", "two.jpg": JPEG + b"2"}[empty[0].name]
    await trip_ingest.reconcile_moves()
    assert _verified(batch) == staged, "the source original is not intact in staging"
    assert empty[0].exists() and empty[0].stat().st_size == 0
    flagged = "SELECT count(*) FILTER (WHERE flagged_at IS NOT NULL), count(*) FROM ingest_moves"
    assert tp._sql(flagged) == [(1, 1)]
    assert await tp._tick(world) == "ok"
    refiled = empty[0].with_name(f"{empty[0].stem} (2){empty[0].suffix}")
    assert refiled.read_bytes() == expected, "the part did not refile under the next name"
    assert empty[0].stat().st_size == 0
    assert tp._sql(
        "SELECT count(*) FROM attachments a JOIN ingest_batches b ON b.memo_id = a.memo_id "
        "WHERE b.id = :b",
        {"b": batch_id},
    ) == [(2,)]
    assert tp._pluto_batch(batch) == [("acked", True)], "the flagged row wedged the batch"
    assert tp._sql("SELECT count(*) FROM ingest_moves") == [(1,)]
    assert await tp._tick(world) == "ok"
    assert empty[0].stat().st_size == 0 and refiled.read_bytes() == expected


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


def _cancels_then_runs(call, holder: dict, seen: dict):
    """The hop under test: cancel the task from inside it, give a cancellation that can reach the
    coroutine time to unwind it, then do the rename and say whether the task had already gone."""
    loop = asyncio.get_running_loop()

    def hop(*args, **kwargs):
        loop.call_soon_threadsafe(holder["task"].cancel)
        limit = time.monotonic() + 1
        while not holder["task"].done() and time.monotonic() < limit:
            time.sleep(0.01)
        seen["task_had_gone"] = holder["task"].done()
        try:
            return call(*args, **kwargs)
        finally:
            seen["hopped"].set()

    return hop


async def test_a_cancelled_reconcile_moves_nothing_after_it_let_the_batch_go(
    world, tmp_path, monkeypatch
):
    """anyio does not hold a native `task.cancel()` for a running thread: the coroutine unwinds and
    closes its session, so the batch lock is gone while the rename is still to come. A later
    reconcile and a refile can then finish first, and the late thread takes the new master away."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    dest.write_bytes(b"bytes")
    await _journal_row(tmp_path, src, dest)
    holder: dict = {}
    seen: dict = {"hopped": threading.Event()}
    monkeypatch.setattr(os, "replace", _cancels_then_runs(os.replace, holder, seen))
    holder["task"] = asyncio.ensure_future(trip_ingest.reconcile_moves())
    await asyncio.wait([holder["task"]], timeout=WAIT_TIMEOUT)
    assert await asyncio.to_thread(seen["hopped"].wait, WAIT_TIMEOUT)
    assert holder["task"].cancelled()
    assert seen["task_had_gone"] is False, "the rename ran after the batch lock was released"
    monkeypatch.undo()
    await trip_ingest.reconcile_moves()
    assert src.read_bytes() == b"bytes" and not dest.exists()
    assert _rows() == 0


async def test_a_cancelled_part_rename_cannot_land_after_the_restore(world, monkeypatch):
    """The restore runs while the cancellation unwinds, and it now drops the journal when it is
    done: a rename still running in a thread would land after both, with no row to find it."""
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    staged = _verified(batch)
    holder: dict = {}
    seen: dict = {"hopped": threading.Event()}
    hop = _cancels_then_runs(trip_ingest._replace_sync, holder, seen)
    monkeypatch.setattr(trip_ingest, "_replace_sync", hop)
    holder["task"] = asyncio.ensure_future(trip_ingest._file_part(batch_id))
    await asyncio.wait([holder["task"]], timeout=WAIT_TIMEOUT)
    assert await asyncio.to_thread(seen["hopped"].wait, WAIT_TIMEOUT)
    assert holder["task"].cancelled()
    assert seen["task_had_gone"] is False, "the part rename ran after the filing had unwound"
    assert _verified(batch) == staged
    assert _files(world.home_root, "uploads") == []
    assert _rows() == 0


async def _failed_filing(monkeypatch, batch_id: int) -> None:
    """A filing that placed its files and then fails in process, with nothing committed."""

    def failing(real):
        async def then_fails(*a, **k):
            await real(*a, **k)
            raise RuntimeError("injected")

        return then_fails

    with monkeypatch.context() as m:
        m.setattr(passes_svc, "create_guest_memo", failing(passes_svc.create_guest_memo))
        m.setattr(passes_svc, "append_guest_files", failing(passes_svc.append_guest_files))
        with pytest.raises(RuntimeError, match="injected"):
            await trip_ingest._file_part(batch_id)


async def _stale_rows(monkeypatch, batch_id: int) -> None:
    """A filing's rows outliving its full restore, as a failed drop or an older build leaves it."""

    async def kept(self):
        return None

    with monkeypatch.context() as m:
        m.setattr(trip_ingest._MoveJournal, "drop_written", kept, raising=False)
        await _failed_filing(m, batch_id)
    assert _rows() > 0


def _last_master_dest() -> Path:
    [(dest,)] = tp._sql(
        "SELECT dest FROM ingest_moves WHERE kind = 'master' ORDER BY id DESC LIMIT 1"
    )
    return Path(dest)


def _flagged() -> int:
    return tp._sql("SELECT count(*) FROM ingest_moves WHERE flagged_at IS NOT NULL")[0][0]


def _batch_attachments(batch_id: int) -> list[tuple[str, str]]:
    return tp._sql(
        "SELECT a.filename, a.storage_path FROM attachments a "
        "JOIN ingest_batches b ON b.memo_id = a.memo_id WHERE b.id = :b ORDER BY a.id",
        {"b": batch_id},
    )


async def test_a_fully_restored_filing_leaves_no_rows(world, monkeypatch):
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    staged = _verified(batch)
    await _failed_filing(monkeypatch, batch_id)
    assert _verified(batch) == staged
    assert _files(world.home_root, "originals") == []
    assert _rows() == 0, "a restored filing's rows treat a vacated name as theirs"


async def test_a_cancelled_filing_still_drops_its_rows_after_a_full_restore(world, monkeypatch):
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    staged = _verified(batch)
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
    assert _rows() > 0
    filing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await filing
    assert _verified(batch) == staged
    assert _rows() == 0


async def test_a_restore_that_left_a_file_behind_keeps_the_rows_for_reconcile(world, monkeypatch):
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    staged = _verified(batch)
    real_replace, real_restore = os.replace, trip_ingest._restore

    def refused_restore(moved, parts):
        def refuses(src, dest, *a, **k):
            raise PermissionError(13, "injected", str(src))

        os.replace = refuses
        try:
            return real_restore(moved, parts)
        finally:
            os.replace = real_replace

    with monkeypatch.context() as m:
        m.setattr(trip_ingest, "_restore", refused_restore)
        await _failed_filing(m, batch_id)
    assert len(_files(world.home_root, "originals")) == 1
    assert _rows() > 0, "the only record of the stranded file went"
    await trip_ingest.reconcile_moves()
    assert _verified(batch) == staged
    assert _rows() == 0


async def test_the_journal_drop_ends_inside_its_deadline_and_leaves_the_rows(world, monkeypatch):
    """A held row makes the drop fail, not the filing hang: the failure still surfaces inside the
    deadline, nothing stays queued on the row, and the surviving rows are reconcile's to clear."""
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    staged = _verified(batch)
    holder = async_session()
    real_drop = trip_ingest._MoveJournal.drop_written

    async def held_drop(self):
        await holder.execute(text("SELECT id FROM ingest_moves FOR UPDATE"))
        await real_drop(self)

    monkeypatch.setattr(trip_ingest._MoveJournal, "drop_written", held_drop)
    try:
        async with asyncio.timeout(trip_ingest.CANCEL_RECORD_DEADLINE_S + 2):
            await _failed_filing(monkeypatch, batch_id)
    finally:
        await holder.rollback()
        await holder.close()
    waiting = tp._sql(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
        "AND datname = current_database()"
    )
    assert waiting == [(0,)]
    assert _verified(batch) == staged
    assert _rows() > 0
    await trip_ingest.reconcile_moves()
    assert _rows() == 0
    assert _verified(batch) == staged


@pytest.mark.parametrize("column", ["storage_path", "print_path"])
async def test_an_owners_committed_attachment_at_a_journaled_dest_stays_and_the_batch_files_once(
    world, monkeypatch, column
):
    """The normal case: a failed filing's stale rows, and an owner upload that took the vacated
    name before the next tick. The kept chain costs the batch one tick and nothing else: the flag
    is permanent, the earlier rows walk on the next pass, and the part refiles under the next name.
    """
    _trip, batch_id, _memo, _picture = await _two_part_batch(world, monkeypatch)
    await _stale_rows(monkeypatch, batch_id)
    dest = _last_master_dest()
    expected = {"one.jpg": JPEG + b"1", "two.jpg": JPEG + b"2"}[dest.name]
    assert not dest.exists()
    dest.write_bytes(b"the owner's print")
    async with async_session() as db:
        db.add(
            Attachment(
                uid=secrets.token_urlsafe(16),
                filename=dest.name,
                mime="image/jpeg",
                size=17,
                status=AttachmentStatus.ready,
                **{column: str(dest)},
            )
        )
        await db.commit()
    unflagged = "SELECT count(*) FROM ingest_moves WHERE flagged_at IS NULL"
    left = _rows()

    assert await tp._tick(world) == "error"
    assert dest.read_bytes() == b"the owner's print"
    assert _flagged() == 1
    assert tp._sql(unflagged) == [(left - 1,)], "the rest of the chain did not stop at the flag"
    assert len(_batch_attachments(batch_id)) == 1, "the batch filed on the tick that flagged"

    assert await tp._tick(world) == "ok"
    assert dest.read_bytes() == b"the owner's print"
    refiled = dest.with_name(f"{dest.stem} (2){dest.suffix}")
    assert refiled.read_bytes() == expected
    assert len(_batch_attachments(batch_id)) == 2
    assert _rows() == 1 and _flagged() == 1
    [(batch,)] = tp._sql("SELECT uid FROM ingest_batches WHERE id = :b", {"b": batch_id})
    assert tp._pluto_batch(batch) == [("acked", True)], "the kept chain wedged the batch"
    assert await tp._tick(world) == "ok"
    assert dest.read_bytes() == b"the owner's print" and refiled.read_bytes() == expected
    assert len(_batch_attachments(batch_id)) == 2


async def test_a_row_surviving_a_rollback_leaves_the_part_that_build_refiled(world, monkeypatch):
    """A row survives, 2.12.0 (which neither reads nor deletes the table) refiles the part under the
    same name and commits it, and the roll-forward's reconcile finds the row: the file is the
    committed attachment's, so it stays, the row is flagged, and nothing is filed again."""
    _trip, batch_id, _memo, _picture = await _two_part_batch(world, monkeypatch)
    await _stale_rows(monkeypatch, batch_id)
    dest = _last_master_dest()
    expected = {"one.jpg": JPEG + b"1", "two.jpg": JPEG + b"2"}[dest.name]
    async with async_session() as db:
        survivors = [
            {c: getattr(r, c) for c in ("id", "batch_id", "src", "dest", "kind")}
            for r in (await db.execute(select(IngestMove).order_by(IngestMove.id))).scalars()
        ]
    async with async_session() as db:
        await db.execute(text("DELETE FROM ingest_moves"))
        await db.commit()
    assert await trip_ingest._file_part(batch_id) is True
    assert (dest.name, str(dest)) in _batch_attachments(batch_id), "not refiled under that name"
    async with async_session() as db:
        db.add_all(IngestMove(**row) for row in survivors)
        await db.commit()

    with pytest.raises(trip_ingest.ReconcileIncomplete):
        await trip_ingest.reconcile_moves()
    assert dest.read_bytes() == expected, "the committed original was taken away"
    assert _flagged() == 1

    assert await tp._tick(world) == "ok"
    assert dest.read_bytes() == expected
    assert len(_batch_attachments(batch_id)) == 2, "a filed part was filed again"
    assert _rows() == 1 and _flagged() == 1
    [(batch,)] = tp._sql("SELECT uid FROM ingest_batches WHERE id = :b", {"b": batch_id})
    assert tp._pluto_batch(batch) == [("acked", True)]


async def test_a_committed_dest_stops_the_chain_and_the_next_pass_brings_the_bytes_back(
    world, tmp_path
):
    """The chain matters when the bytes never reached the taken name: they sit one hop back. The
    flag stops this pass, and only the flagged row is out of the walk from then on."""
    verified, part, memo = (tmp_path / n for n in ("v", "p", "m"))
    part.write_bytes(b"the guest's bytes")
    memo.write_bytes(b"the owner's print")
    await _journal_row(tmp_path, verified, part, "part")
    await _journal_row(tmp_path, part, memo, "master")
    async with async_session() as db:
        db.add(
            Attachment(
                uid=secrets.token_urlsafe(16),
                filename="m",
                mime="image/jpeg",
                size=17,
                status=AttachmentStatus.ready,
                storage_path=str(memo),
            )
        )
        await db.commit()
    with pytest.raises(trip_ingest.ReconcileIncomplete):
        await trip_ingest.reconcile_moves()
    assert part.read_bytes() == b"the guest's bytes" and not verified.exists()
    assert memo.read_bytes() == b"the owner's print"
    assert (_rows(), _flagged()) == (2, 1)
    await trip_ingest.reconcile_moves()
    assert verified.read_bytes() == b"the guest's bytes" and not part.exists()
    assert memo.read_bytes() == b"the owner's print"
    assert (_rows(), _flagged()) == (1, 1)


async def _owner_attachment(path: str) -> None:
    async with async_session() as db:
        db.add(
            Attachment(
                uid=secrets.token_urlsafe(16),
                filename=Path(path).name,
                mime="image/jpeg",
                size=17,
                status=AttachmentStatus.ready,
                storage_path=path,
            )
        )
        await db.commit()


async def test_a_committed_dest_with_no_file_there_still_flags_and_touches_nothing(world, tmp_path):
    """Boundary: the attachment's file is gone. The name is still somebody else's, so the row does
    not go quietly, and the bytes at its source stay where they are."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(b"source")
    await _journal_row(tmp_path, src, dest)
    await _owner_attachment(str(dest))
    with pytest.raises(trip_ingest.ReconcileIncomplete):
        await trip_ingest.reconcile_moves()
    assert src.read_bytes() == b"source" and not dest.exists()
    assert (_rows(), _flagged()) == (1, 1)


async def test_an_attachment_at_a_longer_path_does_not_claim_the_dest(world, tmp_path):
    """The comparison is exact: a name that merely starts with this dest is another file."""
    src, dest = tmp_path / "src", tmp_path / "a.jpg"
    dest.write_bytes(b"bytes")
    await _journal_row(tmp_path, src, dest)
    await _owner_attachment(f"{dest}.print")
    await trip_ingest.reconcile_moves()
    assert src.read_bytes() == b"bytes" and not dest.exists()
    assert _rows() == 0


async def test_a_failed_drop_keeps_the_rows_and_the_filings_own_error(world, monkeypatch):
    batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    staged = _verified(batch)
    real_restore = trip_ingest._restore
    real_session = trip_ingest.async_session

    def no_session():
        raise OSError("injected: the database went away")

    def restore_then_lose_the_database(moved, parts):
        monkeypatch.setattr(trip_ingest, "async_session", no_session)
        return real_restore(moved, parts)

    monkeypatch.setattr(trip_ingest, "_restore", restore_then_lose_the_database)
    await _failed_filing(monkeypatch, batch_id)  # still RuntimeError, not the drop's OSError
    monkeypatch.setattr(trip_ingest, "async_session", real_session)
    assert _rows() > 0
    await trip_ingest.reconcile_moves()
    assert _rows() == 0
    assert _verified(batch) == staged


async def test_the_drop_takes_only_what_this_filing_wrote(world, monkeypatch, tmp_path):
    """Not by batch: a row of this batch from before the filing, one of another batch and a flagged
    one all describe something this filing's restore knows nothing about."""
    _batch, batch_id = await _downloaded(world, monkeypatch, (JPEG + b"1", "a.jpg"))
    earlier = await trip_ingest._MoveJournal(batch_id).record(
        str(tmp_path / "s1"), str(tmp_path / "d1"), "master"
    )
    other = await _journal_row(tmp_path, tmp_path / "s2", tmp_path / "d2")
    flagged = await trip_ingest._MoveJournal(batch_id).record(
        str(tmp_path / "s3"), str(tmp_path / "d3"), "master"
    )
    async with async_session() as db:
        await db.execute(
            text("UPDATE ingest_moves SET flagged_at = now() WHERE id = :i"), {"i": flagged}
        )
        await db.commit()
    await _failed_filing(monkeypatch, batch_id)
    assert sorted(r[0] for r in tp._sql("SELECT id FROM ingest_moves")) == sorted(
        [earlier, other, flagged]
    )


async def test_a_row_dropped_beside_a_walk_does_not_fail_the_reconcile(
    world, tmp_path, monkeypatch
):
    """Two writers: a cancelled tick's shielded drop can outlive the tick and meet the next tick's
    reconcile on the same row. Both mean the row is finished, so neither may raise."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    dest.write_bytes(b"bytes")
    row = await _journal_row(tmp_path, src, dest)
    real_walk = trip_ingest._walk_back_sync

    def walk_beside_a_drop(*args):
        outcome = real_walk(*args)
        tp._sql("DELETE FROM ingest_moves WHERE id = :i", {"i": row})
        assert _rows() == 0
        return outcome

    monkeypatch.setattr(trip_ingest, "_walk_back_sync", walk_beside_a_drop)
    await trip_ingest.reconcile_moves()
    assert src.read_bytes() == b"bytes" and _rows() == 0
