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
    _supervisor,
    supervised,
)

# Docker's default stop grace, and the budget the whole stop path is designed to fit inside: the
# stack compose sets no `stop_grace_period`. Not a measurement of this machine — a bound the
# signal proof fails at, so a stop that grew past what production allows is visible here.
STOP_GRACE_SECONDS = 10.0


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

    # The light class has no subprocess to park in: its Pillow work runs INLINE on the loop thread
    # (app/services/media.py:1610-1613), so a cancellation arriving during the decode is delivered
    # at the first await AFTER it — `guard_publish`. Parking exactly there puts the cancellation
    # where production would deliver it, with the decode itself real.
    light_parked = asyncio.Event()
    real_guard_publish = media.guard_publish

    async def parking_guard_publish(db, job, attachment) -> None:
        if attachment.id == aid_i:
            light_parked.set()
            await asyncio.sleep(3600)  # released only by the shutdown under test
        await real_guard_publish(db, job, attachment)

    monkeypatch.setattr(media, "guard_publish", parking_guard_publish)

    # The decision point: the real requeue write, wrapped so it can be asked what it ran for.
    requeued: list[int] = []
    real_requeue_claim = worker._requeue_claim

    async def watched_requeue_claim(db, job_id: int) -> None:
        requeued.append(job_id)
        await real_requeue_claim(db, job_id)

    monkeypatch.setattr(worker, "_requeue_claim", watched_requeue_claim)

    try:
        async with _supervisor():
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
        assert sorted(requeued) == sorted([jid_v, jid_i]), (
            f"the cancelled claims never reached the requeue write: {requeued}"
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
        marker.with_suffix(".cancelled." + str(job_class)).write_text("torn down")
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


def test_a_real_sigterm_runs_the_supervisor_teardown_and_exits_cleanly(tmp_path):
    """AC3's wiring: a real SIGTERM to a real worker process ends it through the teardown.

    The database proof above drives the shutdown directly; this one proves the signal is wired to
    that same shutdown. With no handler installed, SIGTERM's default action kills the process
    outright: no consumer is cancelled, no marker is written, and the exit status is -SIGTERM —
    which is the red this test fails with when the handler is removed.

    The stop is also bounded by STOP_GRACE_SECONDS, the grace the stack compose leaves at docker's
    default: past it docker sends SIGKILL and the claims cannot be written back at all.
    """
    marker = tmp_path / "teardown"
    env = {**os.environ, "DATA_ROOT": str(tmp_path)}
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(marker)],
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        started = time.monotonic()
        while time.monotonic() - started < 120:
            if all(
                marker.with_suffix(f".started.{job_class}").exists()
                for job_class in ("transcode", "light")
            ):
                break
            assert child.poll() is None, f"the worker died before draining: {child.communicate()}"
            time.sleep(0.05)
        else:
            raise AssertionError("the worker's consumers never reached their drain")

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
