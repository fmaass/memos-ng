"""The supervisor: what it spawns, when it refuses to stamp, and how it tears down.

No database and no real media work — the unit under test is `worker.run()`'s own wiring, so every
collaborator it calls is stubbed and the two consumer tasks are real. Time is driven by the
supervisor's OWN passes (a counted stub it calls exactly once per pass) plus injected stall bounds,
never by a fixed sleep a loaded machine could turn into a false verdict: every wait ends on an
observed pass, with a generous `wait_for` as a failure bound rather than as the thing measured.
Where a proof turns on elapsed time, the supervisor's clock (`worker._now`) is injected too, so
the time under test advances on this file's word and not on the host's scheduling luck.
"""

import ast
import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from app import migrations, worker
from app.config import settings
from structlog.testing import capture_logs


class _Harness:
    """Counts supervisor passes and reads the heartbeat file the healthcheck reads."""

    def __init__(self) -> None:
        self.passes = 0
        self.pass_event = asyncio.Event()
        self.drains: dict[str, int] = {"transcode": 0, "light": 0}

    def saw_a_pass(self) -> None:
        self.passes += 1
        self.pass_event.set()

    def stamp(self) -> float | None:
        """The heartbeat's VALUE (beat() writes time.time() into it), not its mtime: a float the
        filesystem cannot round away, which is what 'the beat stopped advancing' has to compare."""
        path = worker.heartbeat_path()
        return float(path.read_text()) if path.exists() else None

    async def _until(self, done, give_up_after: float = 20.0) -> None:
        """Wake on every observed pass until `done()`. The bound is a failure, not a measurement."""
        async with asyncio.timeout(give_up_after):
            while not done():
                self.pass_event.clear()
                await self.pass_event.wait()

    async def wait_passes(self, n: int) -> None:
        target = self.passes + n
        await self._until(lambda: self.passes >= target)

    async def wait_until(self, done) -> None:
        """Watch real passes until `done()` — for a condition on injected time, never on real."""
        await self._until(done)

    async def wait_for_log(self, caps: list[dict], event: str) -> None:
        await self._until(lambda: any(c["event"] == event for c in caps))


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Every collaborator of the supervisor stubbed, on the validator's documented defaults."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    # Ambient settings are whatever an earlier test left behind; pin the eight the validator reads.
    for key, value in {
        "WORKER_TRANSCODE_SLOTS": 1,
        "WORKER_LIGHT_SLOTS": 1,
        "WORKER_TRANSCODE_CPU_COST": 3,
        "WORKER_CPU_BUDGET": 8,
        "FFMPEG_THREADS": 1,
        "WORKER_TRANSCODE_STALL_SECONDS": 3905,
        "WORKER_LIGHT_STALL_SECONDS": 900,
        "MEDIA_CLAIM_STALE_SECONDS": 7200,
    }.items():
        monkeypatch.setattr(settings, key, value)

    state = _Harness()

    async def noop(*args, **kwargs) -> int:
        return 0

    async def frame_drain() -> str:
        # Called exactly once per supervisor pass — this is the clock every wait below runs on.
        state.saw_a_pass()
        return "idle"

    monkeypatch.setattr(migrations, "wait_for_migrated_schema", noop)
    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    monkeypatch.setattr(worker, "run_frame_share_recover_safe", noop)
    monkeypatch.setattr(worker, "run_media_recover_safe", noop)
    monkeypatch.setattr(worker, "reap_expired_committed", noop)
    monkeypatch.setattr(worker, "sweep_job_progress_safe", noop)
    monkeypatch.setattr(worker, "run_commit_race_reconcile_safe", noop)
    monkeypatch.setattr(worker, "sweep_digest_safe", noop)
    # The digest gate is a database read this proof has no use for; "already in flight" is the one
    # answer that skips it without touching a session.
    monkeypatch.setattr(worker, "digest_task_in_flight", lambda: True)
    return state


@contextlib.asynccontextmanager
async def _supervisor():
    """Run worker.run() as its caller does, and cancel it the way every caller does."""
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _consumer_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("media-consumer-")]


class _Clock:
    """The supervisor's time source, under this file's control: it moves when a test says so."""

    def __init__(self, at: float = 1_000_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


class _CountingBeat:
    """`beat()` as a counter. The heartbeat FILE cannot answer these proofs: it holds one float,
    so two stamps in the same pass are indistinguishable in it, and 'did the second stamp fire'
    is exactly the question."""

    def __init__(self) -> None:
        self.count = 0
        self._stamped = asyncio.Event()

    def __call__(self) -> None:
        self.count += 1
        self._stamped.set()

    async def wait_for(self, n: int, give_up_after: float = 20.0) -> None:
        """Wake on stamps until `n` more have landed.

        The supervisor's own clock (a pass of the frame-share drain) cannot drive the bound proofs:
        the step under test is the one that never returns, so the pass never completes and waiting
        on passes would deadlock on the very hang being tested. Stamps are the right clock for
        "the heartbeat kept going" anyway — it is what the healthcheck reads. The bound is a
        failure, not a measurement: a regression must fail here rather than hang.
        """
        target = self.count + n
        async with asyncio.timeout(give_up_after):
            while self.count < target:
                self._stamped.clear()
                await self._stamped.wait()


async def _stamps_per_pass(
    harness, monkeypatch, bring_about: Callable[[], Awaitable[None]]
) -> tuple[list[int], int]:
    """Run the supervisor and let `bring_about()` happen INSIDE the awaited frame-share drain.

    That await is what separates a pass's two stamps, so it is the window in which a consumer can
    die, go quiet or fail systematically while the pass is already under way. Returns the stamp
    count observed at the entry of every pass, and the index of the pass that was armed.
    """
    beats = _CountingBeat()
    monkeypatch.setattr(worker, "beat", beats)
    at_pass_entry: list[int] = []
    armed = -1

    async def frame_drain() -> str:
        nonlocal armed
        harness.saw_a_pass()
        at_pass_entry.append(beats.count)
        if len(at_pass_entry) == 3 and armed < 0:
            armed = len(at_pass_entry) - 1
            async with asyncio.timeout(20):
                await bring_about()
        return "idle"

    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    async with _supervisor():
        await harness.wait_passes(5)
    return at_pass_entry, armed


def _assert_the_second_stamp_was_withheld(stamps: list[int], armed: int, condition: str) -> None:
    assert armed > 0, "the drain was never armed, so nothing was proved"
    assert stamps[armed] - stamps[armed - 1] == 2, (
        "the supervisor was not stamping twice a pass before the condition arose, so withholding "
        "the second stamp afterwards would prove nothing"
    )
    assert stamps[armed + 1] == stamps[armed], (
        f"the supervisor stamped after {condition} during the drain it was awaiting"
    )


async def test_the_heartbeat_stops_when_a_consumer_dies(harness, monkeypatch):
    # A dead consumer is a class nothing drains. The supervisor is still perfectly healthy itself,
    # which is exactly why it must not keep saying so.
    died = asyncio.Event()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        if job_class == "transcode":
            died.set()
            raise RuntimeError("the transcode consumer died")
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    with capture_logs() as caps:
        async with _supervisor():
            await asyncio.wait_for(died.wait(), 20)
            await harness.wait_passes(2)  # the supervisor has now seen the exited task
            stamp = harness.stamp()
            await harness.wait_passes(3)
            assert harness.stamp() == stamp, "the beat advanced with a dead consumer"
    exited = [c for c in caps if c["event"] == "worker_consumer_exited"]
    assert exited and exited[0]["job_class"] == "transcode"
    # WHY the cause is on the FIRST line: until the supervisor is torn down, nothing else retrieves
    # that exception, so a worker that keeps running (or restarts) would otherwise leave the reason
    # a whole class stopped draining nowhere in the log.
    assert isinstance(exited[0].get("exc_info"), RuntimeError), (
        f"the first exit line named no cause: {exited[0]}"
    )
    assert all("exc_info" not in c for c in exited[1:]), (
        "every pass after the first repeated the traceback instead of the short line"
    )


async def test_a_consumer_that_dies_during_the_drain_is_not_stamped_alive(harness, monkeypatch):
    # The pass's verdict is taken before an await of unbounded length. A consumer that dies inside
    # that await is dead for the rest of the pass, and a stamp is a promise that it is not.
    let_it_die = asyncio.Event()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        if job_class == "transcode":
            await let_it_die.wait()
            raise RuntimeError("the transcode consumer died mid-pass")
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)

    async def kill_it() -> None:
        let_it_die.set()
        await asyncio.wait(_consumer_tasks(), return_when=asyncio.FIRST_COMPLETED)

    stamps, armed = await _stamps_per_pass(harness, monkeypatch, kill_it)
    _assert_the_second_stamp_was_withheld(stamps, armed, "a consumer exited")


async def test_a_consumer_that_crosses_its_bound_during_the_drain_is_not_stamped_alive(
    harness, monkeypatch
):
    # Same window, the stall condition: the consumer was within its bound when the pass began and
    # is past it by the time the pass ends.
    clock = _Clock()
    monkeypatch.setattr(worker, "_now", clock)
    monkeypatch.setattr(settings, "WORKER_LIGHT_STALL_SECONDS", 60)
    parked = asyncio.Event()
    never_set = asyncio.Event()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        if job_class == "light":
            parked.set()
            await never_set.wait()
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)

    async def let_the_bound_pass() -> None:
        await parked.wait()
        clock.advance(600)  # ten bounds' silence from the light consumer, none from the other

    stamps, armed = await _stamps_per_pass(harness, monkeypatch, let_the_bound_pass)
    _assert_the_second_stamp_was_withheld(stamps, armed, "a consumer crossed its stall bound")


async def test_a_consumer_that_reaches_its_error_streak_during_the_drain_is_not_stamped_alive(
    harness, monkeypatch
):
    # Same window, the systemic-error condition: the streak reaches the threshold while the
    # supervisor is awaiting the frame-share drain.
    monkeypatch.setattr(settings, "WORKER_UNHEALTHY_ERROR_STREAK", 2)
    erroring = asyncio.Event()
    recorded = asyncio.Event()
    errors = 0

    async def drain(job_class=None) -> str:
        nonlocal errors
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        if job_class == "light" and erroring.is_set():
            errors += 1
            if errors > 2:
                # A THIRD error call means the consumer recorded the second one: the streak is in.
                recorded.set()
            return "error"
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)

    async def let_the_streak_build() -> None:
        erroring.set()
        await recorded.wait()

    stamps, armed = await _stamps_per_pass(harness, monkeypatch, let_the_streak_build)
    _assert_the_second_stamp_was_withheld(stamps, armed, "a consumer reached its error streak")


async def test_the_heartbeat_stops_when_a_consumer_hangs(harness, monkeypatch):
    # The hole the old serial loop covered by accident: a consumer suspended inside an AWAIT is
    # neither done nor erroring. Deliberately not an ffmpeg hang — that path is bounded by a
    # subprocess timeout; the unbounded region is the publication tail.
    clock = _Clock()
    monkeypatch.setattr(worker, "_now", clock)
    monkeypatch.setattr(settings, "WORKER_LIGHT_STALL_SECONDS", 1)
    parked = asyncio.Event()
    never_set = asyncio.Event()
    moved = asyncio.Event()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        if job_class == "light":
            parked.set()
            await never_set.wait()
        return "idle"

    async def frame_drain() -> str:
        harness.saw_a_pass()
        if parked.is_set() and not moved.is_set():
            moved.set()
            clock.advance(1000)  # the hang is now a thousand bounds old, without waiting one out
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    with capture_logs() as caps:
        async with _supervisor():
            await asyncio.wait_for(parked.wait(), 20)
            await harness.wait_for_log(caps, "worker_consumer_stalled")
            stamp = harness.stamp()
            await harness.wait_passes(3)
            assert harness.stamp() == stamp, "the beat advanced with a hung consumer"
    stalled = [c for c in caps if c["event"] == "worker_consumer_stalled"]
    assert stalled[0]["job_class"] == "light"
    assert stalled[0]["elapsed_seconds"] >= 1000


async def test_one_consumers_error_streak_stops_the_beat_the_others_success_cannot_reset_it(
    harness, monkeypatch
):
    # Each consumer carries its OWN _DrainHealth. Sharing one would let the healthy class keep
    # resetting the failing class's streak, which is the honesty _DrainHealth exists for.
    monkeypatch.setattr(settings, "WORKER_UNHEALTHY_ERROR_STREAK", 2)

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        return "error" if job_class == "light" else "processed"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    with capture_logs() as caps:
        async with _supervisor():
            await harness.wait_for_log(caps, "worker_unhealthy_error_streak")
            stamp = harness.stamp()
            transcodes = harness.drains["transcode"]
            await harness.wait_passes(3)
            assert harness.stamp() == stamp, "the beat advanced with a systemically failing class"
            assert harness.drains["transcode"] > transcodes, (
                "the healthy consumer stopped draining, so its success never had the chance to "
                "reset the other's streak and this proof would be vacuous"
            )
    unhealthy = [c for c in caps if c["event"] == "worker_unhealthy_error_streak"]
    assert unhealthy[0]["job_class"] == "light"
    assert unhealthy[0]["streak"] >= 2


async def test_a_thermally_deferred_pass_is_not_a_stall(harness, monkeypatch):
    # A worker held off by the thermal gate is idle, not stuck: every pass returns and re-stamps
    # the watermark, so the bound must never be reached however long the heat lasts. "However long"
    # is injected rather than waited out — waiting a real bound out made this test a measurement of
    # the host's scheduler, which fails on a loaded machine with nothing wrong.
    clock = _Clock()
    monkeypatch.setattr(worker, "_now", clock)
    bound = 1000
    monkeypatch.setattr(settings, "WORKER_LIGHT_STALL_SECONDS", bound)
    monkeypatch.setattr(settings, "WORKER_TRANSCODE_STALL_SECONDS", bound)

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        return "deferred"

    seen = dict(harness.drains)

    async def frame_drain() -> str:
        harness.saw_a_pass()
        # The heat only advances in steps BOTH consumers have been heard within, and a step is a
        # quarter of the bound: the claim under test is that a deferred pass re-stamps, so a jump
        # taken while a consumer happened to be descheduled would be measuring the scheduler again.
        if all(harness.drains[c] > seen[c] for c in seen):
            seen.update(harness.drains)
            clock.advance(bound // 4)
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    started = clock()
    with capture_logs() as caps:
        async with _supervisor():
            await harness.wait_passes(1)
            stamp = harness.stamp()
            await harness.wait_until(lambda: clock() - started > 3 * bound)  # three bounds of heat
            assert harness.stamp() != stamp, "the beat stopped on a merely deferred worker"
    assert not [c for c in caps if c["event"] == "worker_consumer_stalled"]


async def test_cancelling_the_supervisor_tears_both_consumers_down(harness, monkeypatch):
    # Every caller that drives the loop cancels it; a consumer surviving that would go on draining
    # against patched collaborators or a closed session. Finishing a cancellation is deliberately
    # NOT synchronous here — each consumer needs one more loop turn after its CancelledError — so
    # the proof can tell a teardown that AWAITS its consumers from one that merely asks them to go.
    both_parked = asyncio.Event()
    never_set = asyncio.Event()
    finished: set[str] = set()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        if all(harness.drains.values()):
            both_parked.set()
        try:
            await never_set.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # one more turn of the loop before this consumer is finished
            finished.add(job_class)
            raise
        return "idle"  # unreachable: this consumer only ever leaves by cancellation

    monkeypatch.setattr(worker, "run_media_drain", drain)
    loop_task = asyncio.create_task(worker.run())
    await asyncio.wait_for(both_parked.wait(), 20)
    await harness.wait_passes(2)
    consumers = _consumer_tasks()
    assert len(consumers) == 2
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
    # Nothing yields between the supervisor's cancellation completing and this line: a teardown
    # that only REQUESTED the cancellations leaves both finalisers still pending here.
    assert finished == {"transcode", "light"}, "the supervisor stopped before its consumers had"
    assert all(t.done() for t in consumers), "a consumer outlived the supervisor"
    drained = dict(harness.drains)
    for _ in range(10):  # give anything still scheduled every chance to run
        await asyncio.sleep(0)
    assert dict(harness.drains) == drained, "a drain started after the supervisor stopped"


# --- the total bound on every step the supervisor awaits ------------------------------------
#
# The defect these cover: a housekeeping step with no total bound is time between two heartbeat
# stamps, and the worker healthcheck recreates a container whose heartbeat is older than its bar —
# so one sweep that never returns gets a healthy worker, mid-job consumers and all, killed. Each
# proof parks one step forever and asks the only question that matters: did the stamp keep coming?
#
# A tiny bound, not a slept one: nothing here waits an interval out, the step under test simply
# never returns, and the bound is what has to notice. Real seconds are unavoidable for these —
# `asyncio.timeout` runs on the event loop's clock, not on the supervisor's injectable `_now` —
# so the number is kept at a twentieth of a second and every wait below is itself bounded.
_TINY_BOUND = 0.05


def _bounds_with(monkeypatch, **overrides: float) -> None:
    monkeypatch.setattr(worker, "HOUSEKEEPING_BOUNDS", {**worker.HOUSEKEEPING_BOUNDS, **overrides})


def _idle_media_drain(harness, monkeypatch) -> None:
    """Both consumers alive and draining nothing — the baseline every bound proof needs, because a
    supervisor with a dead consumer stops stamping for a reason that has nothing to do with bounds.
    """

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)


def _timeouts(caps) -> list[dict]:
    return [c for c in caps if c["event"] == "worker_housekeeping_timeout"]


def _assert_consumers_unharmed() -> None:
    tasks = _consumer_tasks()
    assert len(tasks) == 2, f"a bound's expiry cost a consumer: {len(tasks)} still running"
    assert all(not t.done() for t in tasks), "a bound's expiry ended a consumer"


async def test_a_frame_share_drain_that_never_returns_keeps_the_heartbeat_going(
    harness, monkeypatch
):
    # Category 1: the per-turn step, awaited between the pass's two stamps.
    beats = _CountingBeat()
    monkeypatch.setattr(worker, "beat", beats)
    _bounds_with(monkeypatch, frame_share_drain=_TINY_BOUND)
    _idle_media_drain(harness, monkeypatch)
    parked = asyncio.Event()
    never_set = asyncio.Event()

    async def frame_drain() -> str:
        parked.set()
        await never_set.wait()
        return "idle"  # unreachable: this drain only ever leaves by its bound

    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    with capture_logs() as caps:
        async with _supervisor() as task:
            await asyncio.wait_for(parked.wait(), 20)
            await beats.wait_for(4)  # two passes' worth of stamps AFTER the hang began
            assert not task.done(), "the supervisor ended over a step that merely took too long"
            _assert_consumers_unharmed()
    timed_out = _timeouts(caps)
    assert timed_out, "the bound expired without saying which step it was"
    assert timed_out[0]["step"] == "frame_share_drain"
    assert timed_out[0]["bound_seconds"] == _TINY_BOUND


async def test_a_digest_gate_read_that_never_returns_keeps_the_heartbeat_going(
    harness, monkeypatch
):
    # Category 2: the gate read. Cheap by construction, which is exactly why an unbounded one is
    # a surprise — it means the database is unreachable, and then it can hang for as long as the
    # connection takes to fail.
    beats = _CountingBeat()
    monkeypatch.setattr(worker, "beat", beats)
    _bounds_with(monkeypatch, adopt_manual_digest=_TINY_BOUND)
    _idle_media_drain(harness, monkeypatch)
    monkeypatch.setattr(worker, "digest_task_in_flight", lambda: False)
    parked = asyncio.Event()
    never_set = asyncio.Event()

    async def adopt() -> int | None:
        parked.set()
        await never_set.wait()
        return None  # unreachable

    async def not_due() -> bool:
        return False

    monkeypatch.setattr(worker, "adopt_manual_digest", adopt)
    monkeypatch.setattr(worker, "digest_is_due", not_due)
    with capture_logs() as caps:
        async with _supervisor() as task:
            await asyncio.wait_for(parked.wait(), 20)
            await beats.wait_for(4)
            assert not task.done()
            _assert_consumers_unharmed()
    timed_out = _timeouts(caps)
    assert timed_out and timed_out[0]["step"] == "adopt_manual_digest"


async def test_an_hourly_sweep_that_never_returns_keeps_the_heartbeat_going(harness, monkeypatch):
    # Category 3: the hourly block — and `digest_sweep` in particular, whose hosted-media half
    # deletes from a remote under per-operation timeouts and no total one. The cadence is collapsed
    # to every pass so the proof does not wait an hour out.
    beats = _CountingBeat()
    monkeypatch.setattr(worker, "beat", beats)
    monkeypatch.setattr(worker, "REAP_INTERVAL_SECONDS", 0)
    _bounds_with(monkeypatch, digest_sweep=_TINY_BOUND)
    _idle_media_drain(harness, monkeypatch)
    parked = asyncio.Event()
    never_set = asyncio.Event()

    async def sweep() -> None:
        parked.set()
        await never_set.wait()

    monkeypatch.setattr(worker, "sweep_digest_safe", sweep)
    with capture_logs() as caps:
        async with _supervisor() as task:
            await asyncio.wait_for(parked.wait(), 20)
            await beats.wait_for(4)
            assert not task.done()
            _assert_consumers_unharmed()
    timed_out = _timeouts(caps)
    assert timed_out and timed_out[0]["step"] == "digest_sweep"


async def test_a_pre_loop_recover_that_never_returns_still_reaches_the_loop(harness, monkeypatch):
    # Category 4: the startup sweeps. The sharpest case — they run AFTER the first stamp and BEFORE
    # the loop and the consumers exist, so until they return the heartbeat holds exactly one value
    # and there is nothing else in the process that could refresh it.
    beats = _CountingBeat()
    monkeypatch.setattr(worker, "beat", beats)
    _bounds_with(monkeypatch, media_recover=_TINY_BOUND)
    _idle_media_drain(harness, monkeypatch)
    parked = asyncio.Event()
    never_set = asyncio.Event()
    calls = 0

    async def recover() -> int:
        # Only the FIRST call — the pre-loop one — hangs, so the single timeout line below can
        # only have come from before the loop and not from the hourly copy of the same step.
        nonlocal calls
        calls += 1
        if calls == 1:
            parked.set()
            await never_set.wait()
        return 0

    monkeypatch.setattr(worker, "run_media_recover_safe", recover)
    with capture_logs() as caps:
        async with _supervisor() as task:
            await asyncio.wait_for(parked.wait(), 20)
            assert beats.count == 1, "the startup stamp is the only one there can be by now"
            await beats.wait_for(3)
            assert not task.done()
            _assert_consumers_unharmed()
    timed_out = _timeouts(caps)
    assert len(timed_out) == 1, f"a step other than the pre-loop recover timed out: {timed_out}"
    assert timed_out[0]["step"] == "media_recover"


async def test_a_stop_during_a_bounded_step_still_ends_the_supervisor(harness, monkeypatch):
    # The hazard the bounds introduce: `asyncio.timeout` ends its inner await with a CancelledError
    # and converts it to TimeoutError on the way out — and the deliberate stop IS a cancellation of
    # this same task. A bound that swallowed it would leave the worker running through its stop
    # grace until SIGKILL, with both consumers' claims stranded.
    #
    # The bound here is LONG on purpose: if the supervisor ends, it is the stop that ended it. The
    # stop is driven by cancelling the supervisor task, which is exactly what the SIGTERM handler
    # does (`_install_stop_handlers`); the flag it also sets decides only whether run() returns or
    # re-raises, not whether the cancellation gets through.
    _bounds_with(monkeypatch, frame_share_drain=600)
    parked = asyncio.Event()
    never_set = asyncio.Event()
    finished: set[str] = set()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        try:
            await never_set.wait()
        except asyncio.CancelledError:
            finished.add(job_class)
            raise
        return "idle"  # unreachable

    async def frame_drain() -> str:
        parked.set()
        await never_set.wait()
        return "idle"  # unreachable

    monkeypatch.setattr(worker, "run_media_drain", drain)
    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    with capture_logs() as caps:
        loop_task = asyncio.create_task(worker.run())
        await asyncio.wait_for(parked.wait(), 20)
        consumers = _consumer_tasks()
        assert len(consumers) == 2
        loop_task.cancel()
        # `wait`, not `wait_for`: a bound that swallowed the stop leaves the supervisor running, and
        # wait_for would answer that by cancelling it AGAIN and waiting on a task that never ends —
        # a hang where this proof needs a failure. This observes and then reads the outcome off the
        # task itself.
        _, pending = await asyncio.wait([loop_task], timeout=20)
        assert not pending, "the stop never ended the supervisor: the bound swallowed it"
        with pytest.raises(asyncio.CancelledError):
            loop_task.result()
    assert finished == {"transcode", "light"}, "the stop did not reach both consumers' teardown"
    assert all(t.done() for t in consumers), "a consumer outlived the stop"
    assert not _timeouts(caps), "the stop was logged as a step that took too long"


async def test_a_stop_arriving_as_the_bound_expires_still_ends_the_supervisor(harness, monkeypatch):
    # The same hazard at its worst moment, which the test above cannot reach: there the bound is
    # long and only the stop is ever in flight, here BOTH cancellations are outstanding at the same
    # `asyncio.timeout` exit. That exit converts only while `uncancel()` brings the cancelling count
    # back to what it was on entry, and this is the one arrangement that exercises the other side of
    # that accounting — a `_bounded` catching CancelledError beside TimeoutError, or a timeout that
    # converted regardless of the census, would read the stop as its own expiry, answer `on_timeout`
    # and run the worker on through its stop grace until SIGKILL with both claims stranded.
    #
    # The two are made to coincide by the step itself, never by a sleep the host could reorder: the
    # bound cuts the step's await, and the step holds that cancellation inside its own handler until
    # the test has delivered the stop, so the stop is always part of the same unwind.
    _bounds_with(monkeypatch, frame_share_drain=_TINY_BOUND)
    cut_by_the_bound = asyncio.Event()
    stop_delivered = asyncio.Event()
    never_set = asyncio.Event()
    finished: set[str] = set()

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        try:
            await never_set.wait()
        except asyncio.CancelledError:
            finished.add(job_class)
            raise
        return "idle"  # unreachable

    async def frame_drain() -> str:
        try:
            await never_set.wait()
        except asyncio.CancelledError:
            cut_by_the_bound.set()
            await stop_delivered.wait()  # the stop lands HERE, inside the expiry's own unwind
            raise
        return "idle"  # unreachable

    monkeypatch.setattr(worker, "run_media_drain", drain)
    monkeypatch.setattr(worker, "run_frame_share_drain", frame_drain)
    with capture_logs() as caps:
        loop_task = asyncio.create_task(worker.run())
        await asyncio.wait_for(cut_by_the_bound.wait(), 20)
        consumers = _consumer_tasks()
        assert len(consumers) == 2
        loop_task.cancel()  # the SIGTERM handler's move, made while the bound is already expiring
        stop_delivered.set()
        # `wait`, not `wait_for`, for the reason given in the test above: a swallowed stop must
        # fail here rather than hang on a second cancellation of a supervisor that ignores them.
        _, pending = await asyncio.wait([loop_task], timeout=20)
        assert not pending, "the stop never ended the supervisor: the expiring bound swallowed it"
        with pytest.raises(asyncio.CancelledError):
            loop_task.result()
    assert finished == {"transcode", "light"}, "the stop did not reach both consumers' teardown"
    assert all(t.done() for t in consumers), "a consumer outlived the stop"
    assert not _timeouts(caps), "the stop was logged as a step that took too long"


@pytest.mark.parametrize(
    ("step", "collaborator"),
    [
        ("frame_share_recover", "run_frame_share_recover_safe"),
        ("media_recover", "run_media_recover_safe"),
    ],
)
async def test_an_hourly_recover_that_never_returns_keeps_the_heartbeat_going(
    harness, monkeypatch, step: str, collaborator: str
):
    # Category 4's other half, and the gap the declared-bounds test below cannot close: both
    # recover steps are wrapped TWICE — once before the loop, once on the reap cadence — so the SET
    # of bounded names stays complete even after an hourly wrapper is dropped. Only behaviour tells
    # the two sites apart, so each hourly one is proved here: a recover that never returns costs a
    # logged bound and a cycle of lateness, never the heartbeat.
    beats = _CountingBeat()
    monkeypatch.setattr(worker, "beat", beats)
    monkeypatch.setattr(worker, "REAP_INTERVAL_SECONDS", 0)  # the hourly block, every pass
    _bounds_with(monkeypatch, **{step: _TINY_BOUND})
    _idle_media_drain(harness, monkeypatch)
    parked = asyncio.Event()
    never_set = asyncio.Event()
    calls = 0

    async def recover() -> int:
        # The FIRST call — the pre-loop one — returns, so nothing below can be satisfied by the
        # wrapper that sits before the loop; only the hourly copies hang.
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0
        parked.set()
        await never_set.wait()
        return 0  # unreachable

    monkeypatch.setattr(worker, collaborator, recover)
    with capture_logs() as caps:
        async with _supervisor() as task:
            await asyncio.wait_for(parked.wait(), 20)
            try:
                await beats.wait_for(4)  # two passes' worth of stamps AFTER the hang began
            except TimeoutError:
                # An unbounded hourly step does not announce itself — the loop simply stops with
                # it — so the absence of stamps is what this proof has to name.
                pytest.fail(f"the hourly {step} is not bounded: the heartbeat stopped with it")
            assert not task.done()
            _assert_consumers_unharmed()
    timed_out = _timeouts(caps)
    assert timed_out, "the hourly bound expired without saying which step it was"
    assert {c["step"] for c in timed_out} == {step}, f"a step other than {step} timed out"


def test_every_bounded_step_is_declared_and_the_budget_stays_under_half_the_bar():
    # Two ways the budget could quietly stop being true, both cheap to pin: a step bounded by a
    # name nobody declared (a KeyError inside the supervisor, i.e. a dead worker), a declared bound
    # nobody applies (a number that lies about the budget) — and the sum drifting up until a single
    # round of housekeeping can outlast the healthcheck bar it is supposed to sit well inside.
    applied = _bounded_step_names()
    declared = set(worker.HOUSEKEEPING_BOUNDS)
    assert applied == declared, (
        f"bounded but undeclared: {sorted(applied - declared)}; declared but never applied: "
        f"{sorted(declared - applied)}"
    )
    total = worker.HOUSEKEEPING_BOUND_TOTAL_SECONDS
    assert total == sum(worker.HOUSEKEEPING_BOUNDS.values())
    assert total < worker.HEARTBEAT_STALE_SECONDS / 2, (
        f"the housekeeping bounds now sum to {total}s, at or past half the "
        f"{worker.HEARTBEAT_STALE_SECONDS}s healthcheck bar — one round of housekeeping can "
        "outlive the heartbeat the healthcheck reads"
    )


def _bounded_step_names() -> set[str]:
    """Every step name `run()` actually passes to `_bounded`, read out of the source.

    From the source and not from a call count: the question is which names the supervisor will use
    at runtime, and only the file can answer that without running an hour of worker.
    """
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_bounded"
        ):
            step = node.args[0]
            assert isinstance(step, ast.Constant) and isinstance(step.value, str), (
                f"a bound is applied under a non-literal step name at line {node.lineno}, so "
                "neither this test nor a reader can tell which step it covers"
            )
            names.add(step.value)
    assert names, "no bounded step found at all — this test would pass vacuously"
    return names


async def test_a_refused_configuration_never_stamps_and_never_spawns(harness, monkeypatch):
    # Startup order: the validator runs before the first beat, or a worker that cannot run at all
    # would look alive for a whole healthcheck bar and never restart.
    monkeypatch.setattr(settings, "WORKER_LIGHT_SLOTS", 0)

    async def drain(job_class=None) -> str:
        harness.drains[job_class] += 1
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    with pytest.raises(ValueError, match="WORKER_LIGHT_SLOTS"):
        await worker.run()
    assert not worker.heartbeat_path().exists(), "a refused configuration stamped a heartbeat"
    assert sum(harness.drains.values()) == 0
    assert not _consumer_tasks()
