"""A deliberate worker stop must leave no `processing` row behind.

A deploy recreates the worker container, which is a SIGTERM to the worker process. Until now
whatever each consumer had in flight died with the process and its row stayed `processing` with a
claim identity no live process answers to — requeued only by the stale-claim sweep at
MEDIA_CLAIM_STALE_SECONDS (2 h), and, while it stood, withholding every LATER job of the same
attachment through `_order_guard`. Two consumers mean up to two such rows per recreate.

Two proofs, deliberately separate:

* the DATABASE one drives the shutdown the signal handler maps to (cancelling the supervisor task,
  exactly what every in-process caller does) against the REAL media path — real claim, real
  run_once, a real parked ffmpeg subprocess for the transcode class and a real Pillow decode for
  the light class — and reads both rows back from a session of their own;
* the SIGNAL one runs a real worker process, sends it a real SIGTERM, and asks that the
  supervisor's teardown ran and the process exited cleanly inside docker's stop grace. It runs in
  a subprocess on purpose: a real SIGTERM with the handler missing would otherwise kill the test
  runner, and "the handler is missing" is exactly the red-proof this test has to survive.

Nothing here waits on the clock: the transcode is released by creating a file, the light job parks
on an event, and every bound is a failure message rather than a measurement.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app import worker
from app.database import async_session
from app.services import media
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# The parked stub, the bounded wait, the pid-checked reaper, the claim read and the seeds are
# shared with the other worker proofs rather than written twice — one stub, one teardown net.
from tests.test_progress_visibility import _cleanup, _parked_cmd, _read_claim, _reap_parked, _until
from tests.test_worker_parked_transcode import (  # noqa: F401 — `supervised` is a fixture
    _seed_image,
    _seed_video,
    _started,
    supervised,
)

# Docker's default stop grace, and the budget the whole stop path is designed to fit inside: the
# stack compose sets no `stop_grace_period`. Not a measurement of this machine — a bound the
# signal proof fails at, so a stop that grew past what production allows is visible here.
STOP_GRACE_SECONDS = 10.0


@contextlib.asynccontextmanager
async def _stopped_supervisor():
    """worker.run(), stopped on the way out — with docker's grace as the DEADLINE on the stop.

    Deliberately not the shared `_supervisor()` of the other worker proofs, whose teardown await
    has no bound. Here the stop IS the thing under test, and its failure mode is a consumer that
    never ends: `run_once` returning from its cancel handler instead of re-raising leaves the
    consumer looping, so an unbounded await would hang the suite exactly where it has to go red.
    Every exit from this block therefore ends in a verdict — a completed stop, or an assertion
    naming the grace it ran past.
    """
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        task.cancel()
        try:
            # suppress OUTSIDE the bound: the expected CancelledError must not be mistaken for the
            # deadline's own, or a stop that ran out of grace would read as a clean one.
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(STOP_GRACE_SECONDS):
                    await task
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout(30):
                    await task  # never leave a supervisor draining into the next test
            raise AssertionError(
                f"the stop did not finish inside docker's {STOP_GRACE_SECONDS}s grace — a "
                "consumer that keeps draining through a cancellation never ends"
            ) from None


@pytest.fixture(autouse=True)
def _schema(db_engine):
    """The tables these proofs seed into, so this file also stands on its own."""
    return db_engine


def _seed_progress(job_id: int, pct: int) -> None:
    """One committed progress sample for a running job — the side channel the stop must clear."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO media_job_progress (job_id, progress_pct) VALUES (:j, :p)"),
            {"j": job_id, "p": pct},
        )
    eng.dispose()


def _progress_rows(job_id: int) -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM media_job_progress WHERE job_id=:j"), {"j": job_id}
        ).scalar_one()
    eng.dispose()
    return int(n)


async def _claimable_now(job_class: str) -> list[int]:
    """The job ids a FRESH worker start could claim right now, in one rolled-back transaction.

    Every claim is made by the real `claim_one` and none of them is committed, so this reads the
    queue the way the next worker would see it without changing it. It drains up to a handful of
    rows because this database is shared with the rest of the suite: what matters is whether OUR
    job is among the ones a new worker can take, not that it is first in line.
    """
    ids: list[int] = []
    async with async_session() as db:
        try:
            for _ in range(10):
                job = await worker.claim_one(db, job_class=job_class)  # pyright: ignore[reportArgumentType]
                if job is None:
                    break
                ids.append(job.id)
        finally:
            await db.rollback()
    return ids


async def test_a_deliberate_stop_returns_both_consumers_claims_to_the_queue(
    supervised,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
):
    """AC1/AC3: the stop the SIGTERM handler performs requeues what BOTH consumers hold.

    One transcode-class job parked inside a real ffmpeg stub and one light-class job parked at the
    first await after its Pillow decode — the two rows a `--force-recreate` strands today. The
    supervisor is then ended the way its signal handler ends it, and each row is read back from a
    session of its own: `queued`, unclaimed, attempts untouched, and claimable by a fresh
    `claim_one` at once (the Outcome: the next worker start is not blocked, and `_order_guard` no
    longer withholds that attachment's later jobs).

    The end state alone would not prove it: a partial implementation that dropped the claim some
    other way reads the same. So the REAL requeue write is wrapped (not replaced) and asked to
    name the job ids it ran for — the decision point, asserted directly.
    """
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off (dev host)
    gate = tmp_path / "release-the-transcode"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _parked_cmd(gate))
    subject = "shutdown_owner"
    aid_v, jid_v, uid_v = _seed_video(subject, tmp_path)
    aid_i, jid_i, uid_i = _seed_image(subject, tmp_path)

    # The light class has no subprocess to park in: its Pillow work runs in a thread that cannot be
    # interrupted and is waited out, so a cancellation arriving during the decode is delivered at
    # the first await AFTER it — `guard_publish`. Parking exactly there puts the cancellation where
    # production would deliver it, with the decode itself real.
    light_parked = asyncio.Event()
    real_guard_publish = media.guard_publish

    async def parking_guard_publish(db, job, attachment) -> None:
        if attachment.id == aid_i:
            light_parked.set()
            await asyncio.sleep(3600)  # released only by the shutdown under test
        await real_guard_publish(db, job, attachment)

    monkeypatch.setattr(media, "guard_publish", parking_guard_publish)

    # The decision point: the real conditional write, wrapped so it can be asked what it ran for
    # and how many rows it actually changed.
    requeued: list[tuple[int, int]] = []
    real_conditional = worker._requeue_claim_conditional

    async def watched_conditional(job_id: int, claim: str) -> int:
        rows = await real_conditional(job_id, claim)
        requeued.append((job_id, rows))
        return rows

    monkeypatch.setattr(worker, "_requeue_claim_conditional", watched_conditional)

    try:
        async with _stopped_supervisor():
            await _until(lambda: _started(tmp_path, uid_v).exists(), "the transcode to start")
            async with asyncio.timeout(60):
                await light_parked.wait()
            # A progress sample from this attempt, so the cancel path's side-channel cleanup is
            # asserted on a row that really exists.
            _seed_progress(jid_v, 42)
            state_v, attempts_v, claimed_v = _read_claim(jid_v)
            state_i, attempts_i, claimed_i = _read_claim(jid_i)
            assert (state_v, state_i) == ("processing", "processing"), (state_v, state_i)
            assert claimed_v is not None and claimed_v.endswith("/transcode"), claimed_v
            assert claimed_i is not None and claimed_i.endswith("/light"), claimed_i
        # Leaving the context cancelled run() and awaited it, which is what the signal handler
        # does: past this line the teardown has cancelled AND awaited both consumers.
        after_v = _read_claim(jid_v)
        after_i = _read_claim(jid_i)
        assert after_v == ("queued", attempts_v, None), (
            f"the transcode claim was stranded: {after_v}"
        )
        assert after_i == ("queued", attempts_i, None), f"the light claim was stranded: {after_i}"
        assert sorted(requeued) == sorted([(jid_v, 1), (jid_i, 1)]), (
            f"the cancelled claims never reached the requeue write, or changed no row: {requeued}"
        )
        assert _progress_rows(jid_v) == 0, "the stopped attempt left its progress sample behind"
        assert jid_v in await _claimable_now("transcode"), (
            "a fresh worker start cannot claim the requeued transcode at once"
        )
        assert jid_i in await _claimable_now("light"), (
            "a fresh worker start cannot claim the requeued light job at once"
        )
    finally:
        gate.write_text("go")  # whatever the body did, no stub stays parked
        _reap_parked(tmp_path)
        _cleanup(subject, aid_v, aid_i)


def _seed_owner(subject: str) -> int:
    """Just the user row, for the proofs that need one image and no video."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
        owner = c.execute(
            text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
            {"s": subject},
        ).scalar_one()
    eng.dispose()
    return int(owner)


async def test_a_stop_during_the_push_after_the_done_commit_leaves_the_finished_job_alone(
    supervised,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
):
    """The requeue is CONDITIONAL, so a stop that lands past the outcome cannot resurrect a job.

    `_notify_terminal_safe` runs after `state = done` has been committed, and a cancellation
    delivered in there reaches the same handler as one delivered mid-derivation. An unconditional
    requeue would put a finished job back on the queue to be derived a second time — for a photo
    whose owner has already been told it is ready.

    Asserted at the decision point as well as on the row: the write must run and report that it
    changed NOTHING, which is what distinguishes "left alone on purpose" from "never asked".
    """
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    subject = "shutdown_done_owner"
    _seed_owner(subject)
    aid, jid, _uid = _seed_image(subject, tmp_path)

    notifying = asyncio.Event()

    async def parking_notify(attachment_id: int) -> None:
        if attachment_id == aid:
            notifying.set()
            await asyncio.sleep(3600)  # the stop lands here, past the done commit

    monkeypatch.setattr(worker, "_notify_terminal_safe", parking_notify)

    decisions: list[tuple[int, int]] = []
    real_conditional = worker._requeue_claim_conditional

    async def watched_conditional(job_id: int, claim: str) -> int:
        rows = await real_conditional(job_id, claim)
        decisions.append((job_id, rows))
        return rows

    monkeypatch.setattr(worker, "_requeue_claim_conditional", watched_conditional)

    try:
        async with _stopped_supervisor():
            async with asyncio.timeout(60):
                await notifying.wait()
            assert _read_claim(jid)[0] == "done", "the job had not finished, so this proves nothing"
        state, attempts, claimed = _read_claim(jid)
        assert state == "done", f"a finished job was resurrected by the stop: {state}"
        assert attempts == 0, attempts
        assert claimed is not None, "the done row lost its claim identity"
        assert decisions == [(jid, 0)], (
            f"the cancel path did not decide against requeueing the finished row: {decisions}"
        )
    finally:
        _cleanup(subject, aid)


async def test_a_stop_in_the_claim_time_progress_clear_still_requeues_inside_the_grace(
    supervised,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
):
    """The window between the claim commit and the work itself is covered too — and it is bounded.

    `media.clear_progress` runs after the claim is durable and before anything is derived. A
    cancellation there used to reach no handler at all, so the row stayed `processing`. The same
    stub keeps hanging when the stop path clears the row on the way out, which is what makes this
    also a proof of the bound: the whole stop still completes well inside docker's grace.
    """
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    subject = "shutdown_clear_owner"
    _seed_owner(subject)
    aid, jid, _uid = _seed_image(subject, tmp_path)

    clearing = asyncio.Event()
    real_clear_progress = media.clear_progress

    async def hanging_clear_progress(job_id: int) -> None:
        if job_id == jid:
            clearing.set()
            await asyncio.sleep(3600)  # never answers — a lock wait that outlives the stop
        await real_clear_progress(job_id)

    monkeypatch.setattr(media, "clear_progress", hanging_clear_progress)

    # Driven by hand, like the hung-write proof below: docker's grace is the BOUND, so a stop that
    # spends it on best-effort cleanup fails as an assertion instead of hanging the suite.
    supervisor = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(60):
            await clearing.wait()
        assert _read_claim(jid)[0] == "processing", _read_claim(jid)
        started = time.monotonic()
        supervisor.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(STOP_GRACE_SECONDS):
                    await supervisor
        except TimeoutError:
            raise AssertionError(
                f"the stop hung on best-effort cleanup past docker's {STOP_GRACE_SECONDS}s grace"
            ) from None
        elapsed = time.monotonic() - started
        assert _read_claim(jid) == ("queued", 0, None), (
            f"a stop in the claim-time cleanup stranded the row: {_read_claim(jid)}"
        )
        assert jid in await _claimable_now("light"), "the requeued job is not claimable at once"
        assert elapsed < STOP_GRACE_SECONDS, (
            f"the stop waited {elapsed:.1f}s on best-effort cleanup, past docker's grace"
        )
    finally:
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            async with asyncio.timeout(30):
                await supervisor
        _cleanup(subject, aid)


async def test_a_requeue_write_that_never_answers_still_ends_the_consumer_cancelled(
    supervised,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
):
    """The bound holds even when the database does not answer, and the stop stays a cancellation.

    The write is given its own session and abandoned at the bound, so nothing it does afterwards
    can reach the session the consumer is closing behind it. What must NOT happen: the cancelled
    consumer ending as an ordinary error (`run_media_drain` would swallow it and the consumer
    would keep draining while the supervisor waits for it to end). So the consumer task's own
    verdict is read: cancelled, not finished.
    """
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    subject = "shutdown_hang_owner"
    _seed_owner(subject)
    aid, jid, _uid = _seed_image(subject, tmp_path)

    publishing = asyncio.Event()
    real_guard_publish = media.guard_publish

    async def parking_guard_publish(db, job, attachment) -> None:
        if attachment.id == aid:
            publishing.set()
            await asyncio.sleep(3600)
        await real_guard_publish(db, job, attachment)

    async def hanging_conditional(job_id: int, claim: str) -> int:
        await asyncio.sleep(3600)  # a database that never answers
        return 0

    monkeypatch.setattr(media, "guard_publish", parking_guard_publish)
    monkeypatch.setattr(worker, "_requeue_claim_conditional", hanging_conditional)

    # The supervisor is driven by hand here rather than through the helper above: the stop itself is
    # what is under test, so it is given docker's grace as a BOUND and a stop that never finishes
    # has to fail as an assertion rather than hang the suite.
    supervisor = asyncio.create_task(worker.run())
    consumer: asyncio.Task | None = None
    try:
        async with asyncio.timeout(60):
            await publishing.wait()
        consumer = next(t for t in asyncio.all_tasks() if t.get_name() == "media-consumer-light")
        started = time.monotonic()
        supervisor.cancel()
        try:
            # suppress OUTSIDE the bound: the expected CancelledError must not be mistaken for
            # the timeout's own, or a stop that ran out of grace would read as a clean one.
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(STOP_GRACE_SECONDS):
                    await supervisor
        except TimeoutError:
            raise AssertionError(
                f"a requeue write that never answers held the stop past docker's "
                f"{STOP_GRACE_SECONDS}s grace"
            ) from None
        elapsed = time.monotonic() - started
        assert consumer is not None and consumer.cancelled(), (
            "the light consumer did not end as a cancellation, so the stop became an error path"
        )
        assert elapsed >= worker.CANCEL_REQUEUE_TIMEOUT_SECONDS, (
            f"the stop did not even wait for the bound ({elapsed:.1f}s)"
        )
        assert elapsed < STOP_GRACE_SECONDS, f"the hung write held the stop for {elapsed:.1f}s"
        assert _read_claim(jid)[0] == "processing", (
            "the write was supposed to hang, so the row must be left for the stale-claim sweep"
        )
    finally:
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            async with asyncio.timeout(30):
                await supervisor
        _cleanup(subject, aid)


# A worker process with no database and no media work: every collaborator the supervisor calls is
# replaced in the child itself, and each consumer's drain parks until the shutdown cancels it, so
# what this proves is the SIGNAL path and nothing else.
_CHILD = """
import asyncio, sys
from pathlib import Path
from app import migrations, worker

marker = Path(sys.argv[1])

async def noop(*args, **kwargs):
    return 0

async def frame_drain():
    return "idle"

async def parked_drain(job_class=None):
    marker.with_suffix(".started." + str(job_class)).write_text("in the drain")
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        # Stands in for the cancel path's requeue: it takes real time, and the process must still
        # be alive to finish it — which is what a second signal arriving here would take away.
        marker.with_suffix(".cancelled." + str(job_class)).write_text("torn down")
        await asyncio.sleep(1.0)
        marker.with_suffix(".finished." + str(job_class)).write_text("write completed")
        raise
    return "idle"

worker.POLL_SECONDS = 0.05
migrations.wait_for_migrated_schema = noop
worker.run_frame_share_drain = frame_drain
worker.run_frame_share_recover_safe = noop
worker.run_media_recover_safe = noop
worker.reap_expired_committed = noop
worker.sweep_job_progress_safe = noop
worker.run_commit_race_reconcile_safe = noop
worker.sweep_digest_safe = noop
worker.digest_task_in_flight = lambda: True
worker.run_media_drain = parked_drain
worker.main()
"""


def _start_worker_child(tmp_path: Path) -> tuple[subprocess.Popen[str], Path]:
    """A real worker process, parked in both consumers' drains, ready to be signalled."""
    marker = tmp_path / "teardown"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(marker)],
        env={**os.environ, "DATA_ROOT": str(tmp_path)},
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if all(
            marker.with_suffix(f".started.{job_class}").exists()
            for job_class in ("transcode", "light")
        ):
            return child, marker
        assert child.poll() is None, f"the worker died before draining: {child.communicate()}"
        time.sleep(0.05)
    child.kill()
    raise AssertionError("the worker's consumers never reached their drain")


def _wait_for(marker: Path, give_up_after: float = 60.0) -> bool:
    deadline = time.monotonic() + give_up_after
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(0.02)
    return False


def test_a_real_sigterm_runs_the_supervisor_teardown_and_exits_cleanly(tmp_path):
    """AC3's wiring: a real SIGTERM to a real worker process ends it through the teardown.

    The database proofs above drive the shutdown directly; this one proves the signal is wired to
    that same shutdown. With no handler installed, SIGTERM's default action kills the process
    outright: no consumer is cancelled, no marker is written, and the exit status is -SIGTERM —
    which is the red this test fails with when the handler is removed.

    The stop is also bounded by STOP_GRACE_SECONDS, the grace the stack compose leaves at docker's
    default: past it docker sends SIGKILL and the claims cannot be written back at all.
    """
    child, marker = _start_worker_child(tmp_path)
    try:
        sent = time.monotonic()
        child.send_signal(signal.SIGTERM)
        try:
            output = child.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            child.kill()
            raise AssertionError("SIGTERM did not stop the worker at all") from None
        elapsed = time.monotonic() - sent
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)

    for job_class in ("transcode", "light"):
        assert marker.with_suffix(f".cancelled.{job_class}").exists(), (
            f"SIGTERM did not reach the {job_class} consumer's teardown; worker said: {output}"
        )
    assert child.returncode == 0, (
        f"a signalled stop must end the process cleanly, not by signal "
        f"(returncode {child.returncode}); worker said: {output}"
    )
    assert elapsed < STOP_GRACE_SECONDS, (
        f"the stop took {elapsed:.1f}s, past docker's {STOP_GRACE_SECONDS}s grace"
    )


def test_a_second_sigterm_during_the_teardown_does_not_kill_the_write(tmp_path):
    """An impatient operator (or docker) signalling twice must not cut the requeue in half.

    The handlers stay installed until the teardown has finished, and a repeat signal is a no-op
    while a stop is already running. Give them back before the consumers are awaited instead, and
    the seconds each consumer spends writing its claim back are seconds in which SIGTERM's default
    action is armed again — the process dies mid-write, which is the state this whole path exists
    to avoid. The child's drain writes its completion marker AFTER a delay on the cancel path, so
    "the write still completed" is observable rather than inferred.
    """
    child, marker = _start_worker_child(tmp_path)
    try:
        sent = time.monotonic()
        child.send_signal(signal.SIGTERM)
        assert _wait_for(marker.with_suffix(".cancelled.transcode")), (
            "the first SIGTERM never reached the teardown"
        )
        child.send_signal(signal.SIGTERM)  # while the consumers are still unwinding
        try:
            output = child.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            child.kill()
            raise AssertionError("the second SIGTERM left the worker hanging") from None
        elapsed = time.monotonic() - sent
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)

    for job_class in ("transcode", "light"):
        assert marker.with_suffix(f".finished.{job_class}").exists(), (
            f"the second SIGTERM cut the {job_class} consumer's write short; worker said: {output}"
        )
    assert child.returncode == 0, (
        f"the second signal killed the process instead of being a no-op "
        f"(returncode {child.returncode}); worker said: {output}"
    )
    assert elapsed < STOP_GRACE_SECONDS, (
        f"the stop took {elapsed:.1f}s, past docker's {STOP_GRACE_SECONDS}s grace"
    )
