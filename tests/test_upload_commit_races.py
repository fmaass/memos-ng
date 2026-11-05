"""The upload creates' part cleanup against the races a failed COMMIT and a cancellation open.

POST /api/uploads and POST /api/uploads/batch make each session's part file before the row that
names it is committed, so a request that fails must remove exactly the parts of sessions that did
not commit — and must know which those are. Four ways that used to go wrong (MEMNG-1192):

1. A COMMIT that raised while PostgreSQL was still processing it: a row read run at that moment
   finds nothing, and the parts of sessions that then commit were unlinked. Only the transaction's
   own status, waited out while it is in progress, says what happened.
2. A cancellation reaching the thread hop that makes a part (or its staging link) abandoned the
   await while the thread went on, so the cleanup ran first and the file appeared after it.
3. `finish_off_the_loop` spun the event loop on an executor future cancelled directly.
4. The single create had no part cleanup at all.

The routes are real and so is PostgreSQL; only the free-space probe, a COMMIT made to raise, and a
file hop parked on a threading event stand in.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from app.config import settings
from app.services import storage
from app.services import uploads as svc
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import SYNC_URL
from tests.test_upload_batch import (
    BATCH,
    _commit_that,
    _Disk,
    _entry,
    _holds_upload,
    _loop_watchdog,
    _LoopBlocked,
    _parts,
    _rows,
    _settle,
)
from tests.test_upload_staging_dedupe import ONE, _att, _ready_memo, _sha
from tests.test_upload_staging_dedupe import _cleanup as _cleanup_all

SINGLE = "/api/uploads"


@pytest.fixture
def disk(monkeypatch, tmp_path):
    """As `tests.test_upload_batch.disk`: parts under `tmp_path`, free space stubbed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    probe = _Disk(free=1 << 40)
    monkeypatch.setattr(storage, "free_bytes", probe)
    _cleanup_all()
    try:
        yield probe
    finally:
        _cleanup_all()


async def _until(predicate: Callable[[], bool], within_s: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + within_s
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never came true"
        await asyncio.sleep(0.02)


def _post(bearer, route: str, entries: list[dict]):
    if route == "batch":
        return bearer.post(BATCH, json={"entries": entries})
    (entry,) = entries
    return bearer.post(SINGLE, json=entry)


def _part_names(data_root: Path) -> set[str]:
    return {p.name for p in _parts(data_root)}


# --- 1. a COMMIT still running when the outcome is read ---------------------------------------

HOLD_KEY = 1192


@contextlib.contextmanager
def _commit_held_open() -> Iterator[Callable[[], None]]:
    """Make every COMMIT that inserted an upload row WAIT, server side, until released.

    A deferred constraint trigger takes an advisory lock this test holds, so PostgreSQL has the
    COMMIT and is still processing it — the state a connection drop can leave a request in, with
    no answer on its way back. Yields the release."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE FUNCTION memng1192_hold() RETURNS trigger LANGUAGE plpgsql AS "
                f"$$ BEGIN PERFORM pg_advisory_xact_lock({HOLD_KEY}); RETURN NULL; END $$"
            )
        )
        c.execute(
            text(
                "CREATE CONSTRAINT TRIGGER memng1192_hold AFTER INSERT ON uploads "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION memng1192_hold()"
            )
        )
    holder = eng.connect().execution_options(isolation_level="AUTOCOMMIT")
    holder.execute(text(f"SELECT pg_advisory_lock({HOLD_KEY})"))
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            holder.execute(text(f"SELECT pg_advisory_unlock({HOLD_KEY})"))

    try:
        yield release
    finally:
        release()
        holder.close()
        with eng.begin() as c:
            c.execute(text("DROP TRIGGER IF EXISTS memng1192_hold ON uploads"))
            c.execute(text("DROP FUNCTION IF EXISTS memng1192_hold()"))
        eng.dispose()


def _commit_waiting() -> bool:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        waiting = c.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND objid = :key AND NOT granted"
            ),
            {"key": HOLD_KEY},
        ).scalar_one()
    eng.dispose()
    return waiting > 0


@pytest.mark.parametrize("route", ["batch", "single"])
async def test_a_commit_that_completes_after_the_outcome_read_keeps_its_parts(
    pat_client, disk, tmp_path, monkeypatch, route
):
    bearer, _, _ = pat_client
    real = AsyncSession.commit
    in_flight: list[asyncio.Task] = []

    async def commit(self):
        if not _holds_upload(self.sync_session):
            return await real(self)
        # COMMIT goes to the server on the request's own connection, which then drops before the
        # answer comes back: the socket closes under it, with no cancel request sent.
        driver = (await (await self.connection()).get_raw_connection()).driver_connection
        in_flight.append(asyncio.create_task(driver.execute("COMMIT")))
        await _until(_commit_waiting)
        driver.terminate()
        raise ConnectionResetError("lost while PostgreSQL was still processing COMMIT")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    entries = [_entry("a.jpg"), _entry("b.jpg")] if route == "batch" else [_entry("a.jpg")]
    with _commit_held_open() as release:
        request = asyncio.create_task(_post(bearer, route, entries))
        try:
            # Long enough for an outcome read to run, answer and be acted on; well inside the
            # read's own bound, so a request that waits for the transaction is still waiting.
            await asyncio.wait({request}, timeout=1.0)
        finally:
            release()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(request, timeout=10)
        await _until(lambda: len(_rows()) == len(entries))  # the COMMIT completed
        await asyncio.wait(in_flight, timeout=5)
        for task in in_flight:
            if task.done() and not task.cancelled():
                task.exception()  # retrieved: the dropped connection's error is expected
        await _settle()

    rows = _rows()
    assert len(rows) == len(entries)
    assert _part_names(tmp_path) == {f"{row[0]}.part" for row in rows}, (
        "a session that committed lost its part"
    )


# --- 2. a cancellation while the part (or its staging link) is being made ----------------------


@pytest.mark.parametrize("hop", ["part", "link"])
@pytest.mark.parametrize("route", ["batch", "single"])
async def test_a_request_cancelled_while_its_part_is_made_leaves_no_orphan(
    logged_in_client, pat_client, disk, tmp_path, monkeypatch, route, hop
):
    client, _ = logged_in_client
    bearer, _, _ = pat_client
    entry = _entry("a.jpg")
    original: Path | None = None
    if hop == "link":
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        entry = _entry("same.png", len(ONE), sha256=_sha(ONE))
    target = "_create_part_sync" if hop == "part" else "_link_part_sync"
    real = getattr(svc, target)
    reached, release, made = threading.Event(), threading.Event(), threading.Event()

    def parked(*args):
        reached.set()
        release.wait(10)
        try:
            return real(*args)
        finally:
            made.set()

    monkeypatch.setattr(svc, target, parked)
    request = asyncio.create_task(_post(bearer, route, [entry]))
    try:
        await _until(reached.is_set)
        request.cancel()
        # Every chance for a cleanup that did not wait for the thread to finish first.
        await asyncio.wait({request}, timeout=0.5)
    finally:
        release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=10)
    await _until(made.is_set)
    await _settle()

    assert request.cancelled(), "the request swallowed a cancellation its caller has to see"
    assert _rows() == []
    assert _parts(tmp_path) == [], "the part was made after the cleanup had run"
    if original is not None:
        assert original.read_bytes() == ONE  # only the staged name went


# --- 3. a thread future cancelled directly -----------------------------------------------------


async def test_a_directly_cancelled_thread_future_is_raised_instead_of_spun_on(monkeypatch):
    """Awaiting a cancelled future raises at once without yielding, so a loop that only
    remembers the cancellation spins forever and blocks the event loop."""
    loop = asyncio.get_running_loop()
    futures: list[asyncio.Future] = []
    real = loop.run_in_executor

    def observed(executor, fn, *args):
        future = real(executor, fn, *args)
        futures.append(future)
        return future

    monkeypatch.setattr(loop, "run_in_executor", observed)
    release = threading.Event()
    caller = asyncio.create_task(svc.finish_off_the_loop(release.wait, 10))
    outcome: BaseException | None = None
    try:
        await _until(lambda: bool(futures))
        with _loop_watchdog(3):
            futures[0].cancel()
            try:
                await asyncio.wait_for(caller, timeout=5)
            except BaseException as exc:
                outcome = exc
    finally:
        release.set()

    assert not isinstance(outcome, _LoopBlocked), outcome
    assert isinstance(outcome, asyncio.CancelledError), outcome


# --- 4. the single create's part cleanup -------------------------------------------------------


@pytest.mark.parametrize("hit", [False, True])
def test_a_single_create_whose_commit_never_landed_leaves_no_part(
    logged_in_client, disk, tmp_path, monkeypatch, hit
):
    client, _ = logged_in_client
    entry = _entry("a.jpg")
    original: Path | None = None
    if hit:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        entry = _entry("same.png", len(ONE), sha256=_sha(ONE))
    _commit_that(monkeypatch, lands=False, then=ConnectionResetError("lost before COMMIT"))

    r = client.post(SINGLE, json=entry)

    assert r.status_code == 500
    assert _rows() == []
    assert _parts(tmp_path) == [], "the single create left a part no session names"
    if original is not None:
        assert original.read_bytes() == ONE


def test_a_single_create_whose_commit_landed_keeps_its_part(
    logged_in_client, disk, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    _commit_that(monkeypatch, lands=True, then=ConnectionResetError("lost after COMMIT"))

    r = client.post(SINGLE, json=_entry("a.jpg"))

    assert r.status_code == 500
    (row,) = _rows()
    assert _part_names(tmp_path) == {f"{row[0]}.part"}


def test_a_single_create_refused_for_space_leaves_nothing(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client
    disk.free = 5

    r = client.post(SINGLE, json=_entry("a.jpg", size=100))

    assert r.status_code == 507
    assert _rows() == []
    assert _parts(tmp_path) == []


# --- the outcome read's edges --------------------------------------------------------------------


@pytest.mark.parametrize("ends", ["commit", "rollback"])
async def test_the_outcome_is_waited_for_while_the_transaction_is_in_progress(disk, ends):
    eng = create_engine(SYNC_URL)
    other = eng.connect()
    try:
        xid = int(other.execute(text("SELECT pg_current_xact_id()::text")).scalar_one())
        async with svc.async_session() as holder:  # holds nothing: closing it ends nothing
            started = asyncio.get_running_loop().time()
            read = asyncio.create_task(svc.commit_landed(xid, holder))
            await asyncio.sleep(0.3)
            assert not read.done(), "an in-progress transaction was answered for"
            other.commit() if ends == "commit" else other.rollback()
            landed, cancelled = await asyncio.wait_for(read, timeout=5)
    finally:
        other.close()
        eng.dispose()

    assert asyncio.get_running_loop().time() - started >= 0.3
    assert landed is (ends == "commit")
    assert cancelled is None


async def test_a_commit_still_running_past_the_bound_unlinks_nothing(
    pat_client, disk, tmp_path, monkeypatch
):
    bearer, _, _ = pat_client
    real = AsyncSession.commit
    in_flight: list[asyncio.Task] = []

    async def commit(self):
        if not _holds_upload(self.sync_session):
            return await real(self)
        driver = (await (await self.connection()).get_raw_connection()).driver_connection
        in_flight.append(asyncio.create_task(driver.execute("COMMIT")))
        await _until(_commit_waiting)
        driver.terminate()
        raise ConnectionResetError("lost while PostgreSQL was still processing COMMIT")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(svc, "COMMIT_OUTCOME_TIMEOUT_S", 0.3)
    with _commit_held_open() as release:
        request = asyncio.create_task(
            bearer.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})
        )
        try:
            # The request gives up on the outcome while the COMMIT is still held.
            await asyncio.wait({request}, timeout=5)
            assert request.done(), "the outcome read outlived its bound"
            assert _rows() == []
            assert len(_parts(tmp_path)) == 2, "an outcome still in progress was taken as aborted"
        finally:
            release()
        with contextlib.suppress(Exception):  # the COMMIT's own error, or the close's
            await asyncio.wait_for(request, timeout=10)
        await _until(lambda: len(_rows()) == 2)  # and it did commit after all
        await asyncio.wait(in_flight, timeout=5)
        for task in in_flight:
            if task.done() and not task.cancelled():
                task.exception()
        await _settle()

    assert _part_names(tmp_path) == {f"{row[0]}.part" for row in _rows()}


def test_a_failed_transaction_id_read_is_a_failure_before_the_commit(
    logged_in_client, disk, tmp_path, monkeypatch
):
    """The id is read before the COMMIT is sent; when that read fails, no COMMIT ever is, so the
    transaction can only roll back and every part goes."""
    client, _ = logged_in_client
    real = AsyncSession.execute
    commits: list[AsyncSession] = []

    async def execute(self, statement, *args, **kwargs):
        if "pg_current_xact_id" in str(statement):
            raise ConnectionResetError("lost before COMMIT")
        return await real(self, statement, *args, **kwargs)

    real_commit = AsyncSession.commit

    async def commit(self):
        if _holds_upload(self.sync_session):
            commits.append(self)
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "execute", execute)
    monkeypatch.setattr(AsyncSession, "commit", commit)

    r = client.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})

    assert r.status_code == 500
    assert commits == []
    assert _rows() == []
    assert _parts(tmp_path) == []
