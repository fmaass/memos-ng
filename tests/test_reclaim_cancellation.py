"""A reclaim's row lock must outlive the thread doing its unlinking — in every caller.

The unlinks run off the event loop, which is what makes the sweep's bound real. Off the loop they
are also OUTSIDE the caller's control: a direct `task.cancel()` — uvicorn's graceful-shutdown
timeout is one, and `asyncio.run`'s teardown of whatever tasks are left is another — is delivered
at the `await to_thread.run_sync(...)` immediately, while the thread it was waiting on runs on to
completion. Cancelled there, a caller that owns the session unwinds the transaction: the row's lock
is released and the row is live again, and the thread then deletes the files of whoever adopts it
next. That is the one outcome `app/services/reclaim.py` exists to prevent, arriving through the
door the offload opened.

So every caller runs the SAME unit — a coroutine that opens its own session, locks the row, unlinks
off the loop and commits — as a task it awaits through a shield, and every such task is registered
where both processes' shutdowns can await it. These four proofs are the four doors: PATCH, DELETE,
the worker's stop, and the API's lifespan shutdown.
"""

import asyncio
import concurrent.futures
import contextlib
import threading
from datetime import UTC, datetime
from pathlib import Path

from app import main, worker
from app.config import settings
from app.services import reclaim
from structlog.testing import capture_logs

from tests.test_detached_sweep import (
    GRACE,
    SEAM_TIMEOUT,
    _async_client,
    _cleanup,
    _png,
    _rows,
    _seed,
    _upload,
)
from tests.test_detached_sweep_bound import _is_locked
from tests.test_worker_supervisor import harness  # noqa: F401 — the supervisor's stubbed wiring

# Docker's stop grace, which the worker's teardown has to fit inside; used here only as a failure
# bound on a wait that ends on an observed state change.
STOP_GRACE_SECONDS = 10.0


class _Park:
    """The unlink seam, parked on its FIRST call — a stalled data volume, held open deliberately.

    It runs in the worker thread `to_thread.run_sync` hands it to, so the events are threading's.
    The real `unlink_paths` is captured at construction: the seam replaces the module attribute the
    reclaim reads, and calling it through the module again would be the seam calling itself.
    """

    def __init__(self, real) -> None:
        self.real = real
        self.entered = threading.Event()
        self.release = threading.Event()
        self.first_paths: list[Path] = []

    def __call__(self, paths: list[Path]) -> list[str]:
        if not self.entered.is_set():
            self.first_paths = list(paths)
            self.entered.set()
            self.release.wait(timeout=SEAM_TIMEOUT)
        return self.real(paths)


class _InFlight:
    """What a cancelled caller left behind: the parked seam and the two rows it detached."""

    def __init__(self, park: _Park, taken: tuple[int, Path], spared: tuple[int, Path]) -> None:
        self.park = park
        self.taken_id, self.taken_original = taken
        self.spared_id, self.spared_original = spared

    async def finish(self) -> None:
        """Let the parked unlink through and give the reclaim its moment to commit."""
        self.park.release.set()
        await _settled(self.taken_id)


def _two_pictures(client, tmp_path) -> tuple[str, dict[int, Path]]:
    """A memo carrying two real pictures. Returns its uid and each attachment id's original."""
    uids = [_upload(client, "a.png", _png(b"aaaa")), _upload(client, "b.png", _png(b"bbbb"))]
    memo = client.post(
        "/api/memos",
        json={"content": "pics", "visibility": "PRIVATE", "upload_uids": uids},
    )
    assert memo.status_code == 201, memo.text
    rows = _rows("SELECT id, storage_path FROM attachments ORDER BY id")
    assert len(rows) == 2, rows
    originals = {row[0]: Path(row[1]) for row in rows}
    assert all(path.exists() for path in originals.values())
    return memo.json()["uid"], originals


async def _wait_for(event: threading.Event, what: str) -> None:
    """Wait on a thread's event without stopping the loop the reclaim still needs."""
    assert await asyncio.to_thread(event.wait, SEAM_TIMEOUT), what


async def _quiesced(att_ids) -> None:
    """Give the loop back to whatever still holds one of these rows, until nothing does.

    The teardown below deletes these rows from a connection of its own, and it is SYNCHRONOUS: run
    while a reclaim still holds a row it would wait on a lock only the event loop it is blocking
    can release. So the release of the seam is always followed by this.
    """
    for _ in range(400):
        held = [i for i in att_ids if await asyncio.to_thread(_is_locked, i)]
        if not held:
            return
        await asyncio.sleep(0.05)


async def _settled(att_id: int) -> None:
    """Wait until the row is gone — the shielded reclaim's own commit, on its own schedule."""
    for _ in range(400):
        if _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []:
            return
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _cancelled_mid_unlink(client, monkeypatch, tmp_path, request_call):
    """Run `request_call(async_client, memo_uid)` and cancel it while its first reclaim's unlink is
    parked. Yields with the seam STILL HELD: the caller is gone, the thread is not.

    On the way out the seam is released and the reclaim is given its moment to finish, so each test
    reads the same two things back: what the cancelled caller's row ended as, and what the row it
    never reached kept.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    memo_uid, originals = _two_pictures(client, tmp_path)
    park = _Park(reclaim.unlink_paths)
    monkeypatch.setattr(reclaim, "unlink_paths", park)
    try:
        async with _async_client(client) as caller:
            request = asyncio.create_task(request_call(caller, memo_uid))
            await _wait_for(park.entered, "the unlink seam was never reached")
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(request, timeout=SEAM_TIMEOUT)
            assert request.cancelled(), "the request was not cancelled where this proof needs it"
            taken = [i for i, path in originals.items() if path in park.first_paths]
            assert len(taken) == 1, f"the seam's first call named {taken} of {originals}"
            spared = [i for i in originals if i != taken[0]]
            yield _InFlight(
                park, (taken[0], originals[taken[0]]), (spared[0], originals[spared[0]])
            )
    finally:
        park.release.set()
        await _quiesced(list(originals))
        _cleanup()


def _reclaimed(inflight: _InFlight) -> None:
    """The row the cancelled caller had already locked is gone, files and all — and the row it
    never reached is untouched, because the cancellation ENDED the loop instead of being swallowed
    by it."""
    assert _rows("SELECT id FROM attachments WHERE id=:a", a=inflight.taken_id) == [], (
        "the cancellation unwound the reclaim's transaction — the row is live again while its "
        "files were deleted by a thread that ran on"
    )
    assert not inflight.taken_original.exists()
    assert _rows(
        "SELECT id FROM attachments WHERE id=:a AND memo_id IS NULL AND detached_at IS NOT NULL",
        a=inflight.spared_id,
    ) == [(inflight.spared_id,)], "the cancelled loop went on to the next row"
    assert inflight.spared_original.exists(), "the loop took bytes it was cancelled before reaching"


async def test_a_cancelled_patch_keeps_the_lock_until_its_unlink_ends(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """PATCH's after-commit loop, cancelled mid-unlink.

    The lock probe is the whole proof: it asks for the row `FOR UPDATE` from a session of its own
    and fails to get it, which is exactly what a finalize trying to adopt this row in that instant
    would do. Unshielded, the request's session closes on the cancellation and the row is free —
    free while the thread deleting its files is still running.
    """
    client, _ = logged_in_client

    async def patch(async_client, memo_uid):
        return await async_client.patch(f"/api/memos/{memo_uid}", json={"attachment_uids": []})

    async with _cancelled_mid_unlink(client, monkeypatch, tmp_path, patch) as inflight:
        assert await asyncio.to_thread(_is_locked, inflight.taken_id), (
            "the row was adoptable while the thread unlinking its files ran on"
        )
        await inflight.finish()
        _reclaimed(inflight)


async def test_a_cancelled_delete_keeps_the_lock_until_its_unlink_ends(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """The same experiment on the other caller: a memo delete orphans every attachment it carried
    and reclaims them after its commit, so it owes the adopter the same lock."""
    client, _ = logged_in_client

    async def delete(async_client, memo_uid):
        return await async_client.delete(f"/api/memos/{memo_uid}")

    async with _cancelled_mid_unlink(client, monkeypatch, tmp_path, delete) as inflight:
        assert await asyncio.to_thread(_is_locked, inflight.taken_id), (
            "the row was adoptable while the thread unlinking its files ran on"
        )
        await inflight.finish()
        _reclaimed(inflight)


async def test_the_worker_stop_waits_for_a_reclaim_its_sweep_abandoned(
    db_engine,
    harness,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
):
    """The worker's stop, with a reclaim in flight that nothing is awaiting any more.

    The sweep's own bound cancels the pass, and the row already in flight finishes on its own — in
    a task the supervisor's teardown has to await, because what follows the teardown is
    `asyncio.run` cancelling whatever is left, mid-unlink and mid-transaction.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    async def no_second_pass() -> int:
        return 0

    # The supervisor runs its hourly block on its first pass, and a SECOND sweep would simply queue
    # behind the lock the one under test is holding — 45 s of waiting that proves nothing. The pass
    # this proof is about is the one already in flight below.
    monkeypatch.setattr(worker, "sweep_detached_attachments_safe", no_second_pass)
    att_id, original = _seed(tmp_path, "stopwait", detached_at=datetime.now(UTC) - 2 * GRACE)
    park = _Park(reclaim.unlink_paths)
    try:
        sweep = asyncio.create_task(worker.sweep_detached_attachments(unlink=park))
        await _wait_for(park.entered, "the sweep never reached the unlink seam")
        sweep.cancel()  # what the bound's expiry does to the pass
        with contextlib.suppress(asyncio.CancelledError):
            await sweep

        with capture_logs() as logs:
            supervisor = asyncio.create_task(worker.run())
            await harness.wait_passes(2)
            supervisor.cancel()
            await asyncio.sleep(0.5)
            assert not supervisor.done(), "the stop did not wait for the reclaim in flight"
            park.release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(supervisor, timeout=STOP_GRACE_SECONDS)

        assert any(entry["event"] == "reclaim_drain_waiting" for entry in logs), (
            "the stop waited on nothing it named"
        )
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert not original.exists()
    finally:
        park.release.set()
        await _quiesced([att_id])
        _cleanup()


async def test_the_api_shutdown_waits_for_a_reclaim_a_cancelled_request_left(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """The API process's door, the one uvicorn's graceful-shutdown timeout comes through: it
    cancels the request task, and the lifespan shutdown that follows must not let the loop go away
    while the reclaim that cancellation left behind is still holding a row."""
    client, _ = logged_in_client

    async def patch(async_client, memo_uid):
        return await async_client.patch(f"/api/memos/{memo_uid}", json={"attachment_uids": []})

    async def shutdown() -> None:
        started = main.lifespan(main.app)
        await started.__aenter__()
        await started.__aexit__(None, None, None)

    async with _cancelled_mid_unlink(client, monkeypatch, tmp_path, patch) as inflight:
        stopping = asyncio.create_task(shutdown())
        await asyncio.sleep(0.5)
        assert not stopping.done(), "the lifespan shutdown did not wait for the reclaim in flight"
        await inflight.finish()
        await asyncio.wait_for(stopping, timeout=STOP_GRACE_SECONDS)
        _reclaimed(inflight)


async def test_a_reclaim_cancelled_directly_still_finishes_its_unlink_and_commits(
    db_engine, monkeypatch, tmp_path
):
    """The other door, the one no shield covers: the reclaim TASK cancelled, not its caller.

    `asyncio.run`'s teardown (the worker) and the loop's own (the API) cancel whatever tasks are
    left, and a reclaim the drain gave up on is exactly such a task. The shield is no answer here
    — it protects the caller's await, and this cancellation is delivered inside the task itself,
    at the `await` on its unlink thread. So the unit holds it: the thread is awaited to its return
    and the transaction is finished before the cancellation is allowed through. Until then the row
    stays locked — which is what an adopter reaching for it in that instant asks about.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att_id, original = _seed(
        tmp_path, "cancelled-direct", detached_at=datetime.now(UTC) - 2 * GRACE
    )
    park = _Park(reclaim.unlink_paths)
    monkeypatch.setattr(reclaim, "unlink_paths", park)
    try:
        task = asyncio.create_task(
            reclaim.reclaim_row(
                att_id, reclaim.Revalidate.DETACHED, detached_before=datetime.now(UTC)
            )
        )
        await _wait_for(park.entered, "the reclaim never reached the unlink seam")
        task.cancel()
        await asyncio.sleep(0.5)
        assert not task.done(), "the cancellation unwound the reclaim while its thread ran on"
        assert await asyncio.to_thread(_is_locked, att_id), (
            "the row was adoptable while the thread unlinking its files ran on"
        )
        park.release.set()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=STOP_GRACE_SECONDS)
        assert task.cancelled(), "the reclaim swallowed a cancellation its caller has to see"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [], (
            "the cancellation was let through before the transaction was finished"
        )
        assert not original.exists()
    finally:
        park.release.set()
        await _quiesced([att_id])
        _cleanup()


class _RecordingExecutor(concurrent.futures.ThreadPoolExecutor):
    """The loop's default executor, keeping the future each offload runs on.

    `_unlink_off_the_loop` keeps that future to itself, and the case below has to cancel in the one
    instant that only ITS state defines: the offload over, the reclaim not yet resumed. The unlink
    is picked out by the callable — `asyncio.to_thread` submits through here too.
    """

    def __init__(self) -> None:
        super().__init__(max_workers=4, thread_name_prefix="recording")
        self.submitted: list[tuple[object, concurrent.futures.Future]] = []

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        future = super().submit(fn, *args, **kwargs)
        self.submitted.append((fn, future))
        return future

    def future_for(self, fn) -> concurrent.futures.Future:
        mine = [future for called, future in self.submitted if called is fn]
        assert len(mine) == 1, f"{len(mine)} offloads of {fn} of {self.submitted}"
        return mine[0]


async def test_a_reclaim_cancelled_after_its_unlink_finished_still_commits(
    db_engine, monkeypatch, tmp_path
):
    """The cancellation that arrives once the offload is OVER and before the reclaim resumed.

    A finished future is not a cancelled one. Nothing cancels this one — it is deliberately not a
    task — so the only way it is ever done at a cancellation is that the thread RETURNED, with its
    answer sitting in it and the files already gone. Read there as "the offload ended cancelled",
    that instant loses everything the offload was for: the reclaim unwinds, and the rollback puts
    back a row naming files that no longer exist.

    The instant is BUILT, not waited for. The loop is parked on a threading event across the
    thread's return, so the future's own completion and the cancel are both queued on it before it
    turns again — and a queue is ordered: the reclaim wakes cancelled with its answer already there.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att_id, original = _seed(
        tmp_path, "cancelled-after-unlink", detached_at=datetime.now(UTC) - 2 * GRACE
    )
    park = _Park(reclaim.unlink_paths)
    monkeypatch.setattr(reclaim, "unlink_paths", park)
    loop = asyncio.get_running_loop()
    executor = _RecordingExecutor()
    monkeypatch.setattr(loop, "_default_executor", executor)
    try:
        task = asyncio.create_task(
            reclaim.reclaim_row(
                att_id, reclaim.Revalidate.DETACHED, detached_before=datetime.now(UTC)
            )
        )
        await _wait_for(park.entered, "the reclaim never reached the unlink seam")
        armed = threading.Event()

        def _cancel_behind_the_answer(_) -> None:
            """On the unlink's own thread, after the future's own callback queued the answer."""
            loop.call_soon_threadsafe(task.cancel)
            armed.set()

        executor.future_for(park).add_done_callback(_cancel_behind_the_answer)
        park.release.set()
        # Parked on purpose, and the only place in this file the loop is: it must not turn until
        # the answer and the cancellation are both on its queue, in that order.
        assert armed.wait(SEAM_TIMEOUT), "the unlink thread never returned"
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=STOP_GRACE_SECONDS)
        assert task.cancelled(), "the reclaim swallowed a cancellation its caller has to see"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [], (
            "a finished offload was taken for a cancelled one: the transaction unwound and the "
            "row is back, naming files the thread had already deleted"
        )
        assert not original.exists()
    finally:
        park.release.set()
        await _quiesced([att_id])
        _cleanup()
        executor.shutdown(wait=True)


async def test_the_worker_process_exit_commits_a_reclaim_the_drain_gave_up_on(
    db_engine, monkeypatch, tmp_path
):
    """The whole worker teardown, run for real: a reclaim still in flight when the drain's grace
    runs out, and then `asyncio.run` returning.

    `asyncio.run` cancels whatever tasks are left and gathers them; the loop is still turning
    while it does. So the reclaim the drain abandoned takes that cancellation, finishes the unlink
    it started and commits — inside that gather, before the loop closes. Read back after the
    thread running `asyncio.run` has joined, the row can only have been committed before it
    returned: there is no loop left to do it afterwards.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att_id, original = _seed(tmp_path, "drain-expired", detached_at=datetime.now(UTC) - 2 * GRACE)
    park = _Park(reclaim.unlink_paths)

    def worker_process() -> None:
        async def stopping() -> None:
            sweep = asyncio.create_task(worker.sweep_detached_attachments(unlink=park))
            assert await asyncio.to_thread(park.entered.wait, SEAM_TIMEOUT)
            sweep.cancel()  # what the pass's own bound does, with a row in flight
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
            # The drain's grace, expired: the worker stops WAITING and returns to `asyncio.run`.
            await reclaim.drain_pending_reclaims(grace_s=0.0)

        asyncio.run(stopping())

    try:
        exiting = asyncio.create_task(asyncio.to_thread(worker_process))
        await _wait_for(park.entered, "the sweep never reached the unlink seam")
        await asyncio.sleep(0.5)  # the drain expires and the teardown's cancellation lands
        park.release.set()
        await asyncio.wait_for(exiting, timeout=STOP_GRACE_SECONDS)
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [], (
            "the process exit unwound the reclaim: the row is live again and its files are gone"
        )
        assert not original.exists()
    finally:
        park.release.set()
        await _quiesced([att_id])
        _cleanup()


async def test_a_cancelled_asset_delete_keeps_the_lock_until_its_unlink_ends(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """The fourth caller: `DELETE /api/attachments/{uid}`, which owes an adopter the same lock.

    It deletes one named attachment off a memo that keeps the other, so the row it never touched is
    the control: still on its memo, files intact.
    """
    client, _ = logged_in_client

    async def delete_attachment(async_client, memo_uid):
        uid = _rows("SELECT uid FROM attachments ORDER BY id")[0][0]
        return await async_client.delete(f"/api/attachments/{uid}")

    async with _cancelled_mid_unlink(client, monkeypatch, tmp_path, delete_attachment) as inflight:
        assert await asyncio.to_thread(_is_locked, inflight.taken_id), (
            "the row was adoptable while the thread unlinking its files ran on"
        )
        await inflight.finish()
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=inflight.taken_id) == [], (
            "the cancellation unwound the reclaim's transaction — the row is live again while its "
            "files were deleted by a thread that ran on"
        )
        assert not inflight.taken_original.exists()
        assert _rows(
            "SELECT id FROM attachments WHERE id=:a AND memo_id IS NOT NULL", a=inflight.spared_id
        ) == [(inflight.spared_id,)], "the request took a row it was never asked about"
        assert inflight.spared_original.exists()
