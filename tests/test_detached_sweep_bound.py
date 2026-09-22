"""The sweep's 45 s bound, held to what a bound has to mean.

Unlinking an attachment's files is blocking filesystem work on a network volume, and the sweep runs
inside the supervisor's event loop. Run there, one stalled `unlink` stops the loop itself: the
timeout that is supposed to end the sweep cannot fire, the heartbeat cannot be stamped, and the
media consumers stop draining — so the bound bounds nothing. Off the loop the bound is real, and
these tests hold the sweep to the three things that then have to be true together:

* the loop keeps beating while a reclaim's filesystem work runs,
* a row whose work is in flight when the bound expires keeps its lock until that work ends, so no
  cancellation can land between the lock and the unlink and leave the files gone with the row back,
* and an expired sweep takes no further row.
"""

import asyncio
import threading
import time
from datetime import UTC, datetime

import structlog
from app import worker
from app.config import settings
from app.services import reclaim
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from tests.conftest import SYNC_URL
from tests.test_detached_sweep import GRACE, _cleanup, _rows, _seed


def _is_locked(att_id: int) -> bool:
    """Is this row's lock held by somebody else right now? Probed from a connection of its own."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as conn:
            conn.execute(text("SET lock_timeout = '250ms'"))
            try:
                conn.execute(
                    text("SELECT id FROM attachments WHERE id = :a FOR UPDATE"), {"a": att_id}
                )
                return False
            except OperationalError:
                return True
            finally:
                conn.rollback()
    finally:
        eng.dispose()


async def test_the_loop_keeps_beating_while_a_reclaim_unlinks(db_engine, monkeypatch, tmp_path):
    """The seam counts the loop's beats across its own block: on the loop, there are none."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    beats = 0
    observed: list[int] = []

    async def ticker() -> None:
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.005)

    def blocking_unlink(paths):
        at_entry = beats
        time.sleep(0.3)
        observed.append(beats - at_entry)
        return reclaim.unlink_paths(paths)

    try:
        att_id, original = _seed(tmp_path, "offloop", detached_at=datetime.now(UTC) - 2 * GRACE)
        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.01)
        try:
            assert await worker.sweep_detached_attachments(unlink=blocking_unlink) == 1
        finally:
            tick.cancel()

        assert observed, "the unlink seam was never reached"
        assert observed[0] >= 5, f"the loop beat {observed[0]} times while the unlink ran"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert not original.exists()
    finally:
        _cleanup()


async def test_a_row_in_flight_when_the_bound_expires_keeps_its_lock(
    db_engine, monkeypatch, tmp_path
):
    """The bound expires mid-unlink: the row stays locked until its filesystem work ends, and the
    reclaim finishes — files gone AND row gone, never the half of it a cancellation would leave."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setitem(worker.HOUSEKEEPING_BOUNDS, "detached_attachment_sweep", 1)
    entered = threading.Event()
    release = threading.Event()
    locked_at_expiry: list[bool] = []

    def blocking_unlink(paths):
        entered.set()
        release.wait(timeout=10.0)
        return reclaim.unlink_paths(paths)

    try:
        att_id, original = _seed(tmp_path, "inflight", detached_at=datetime.now(UTC) - 2 * GRACE)

        def outlive_the_bound() -> None:
            if not entered.wait(timeout=10.0):
                release.set()
                return
            time.sleep(1.6)  # past the 1 s bound patched above
            locked_at_expiry.append(_is_locked(att_id))
            release.set()

        watcher = threading.Thread(target=outlive_the_bound)
        watcher.start()
        try:
            swept = await worker._bounded(
                "detached_attachment_sweep",
                worker.sweep_detached_attachments(unlink=blocking_unlink),
                -1,
            )
        finally:
            release.set()
            watcher.join(timeout=15.0)

        assert swept == -1, "the bound did not expire while the row was in flight"
        assert locked_at_expiry == [True], "the row's lock was gone when the bound expired"

        for _ in range(200):
            if _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []:
                break
            await asyncio.sleep(0.05)
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert not original.exists()
        assert not _is_locked(att_id)
    finally:
        release.set()
        _cleanup()


async def test_an_expired_sweep_takes_no_further_row(db_engine, monkeypatch, tmp_path):
    """The deadline is checked BETWEEN rows, so the backlog a spent pass leaves is untouched — it
    is the next hour's work, not this pass's, and nothing was taken half-way."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setitem(worker.HOUSEKEEPING_BOUNDS, "detached_attachment_sweep", 1)
    calls: list[int] = []

    def slow_unlink(paths):
        calls.append(len(paths))
        time.sleep(1.2)  # the first row alone spends the whole bound
        return reclaim.unlink_paths(paths)

    try:
        detached = datetime.now(UTC) - 2 * GRACE
        first, first_file = _seed(tmp_path, "spent_a", detached_at=detached)
        second, second_file = _seed(tmp_path, "spent_b", detached_at=detached)

        assert await worker.sweep_detached_attachments(unlink=slow_unlink) == 1

        assert len(calls) == 1, f"the spent pass took {len(calls)} rows"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=first) == []
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=second) == [(second,)]
        assert not first_file.exists()
        assert second_file.exists()
    finally:
        _cleanup()


async def test_the_count_is_logged_when_the_sweep_raises(db_engine, monkeypatch):
    """The deploy read-back reads `detached_attachments_swept` to tell a pass that found nothing
    from a worker that never reached its hourly block, so the error path has to log it too."""

    async def explode(**kwargs):
        raise RuntimeError("data volume unreachable")

    monkeypatch.setattr(worker, "sweep_detached_attachments", explode)
    with structlog.testing.capture_logs() as logs:
        assert await worker.sweep_detached_attachments_safe() == 0

    events = [entry["event"] for entry in logs]
    assert "detached_attachments.sweep_error" in events
    counted = [entry for entry in logs if entry["event"] == "detached_attachments_swept"]
    assert counted == [{"event": "detached_attachments_swept", "count": 0, "log_level": "info"}]
