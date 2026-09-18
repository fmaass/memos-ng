"""The supervisor: what it spawns, when it refuses to stamp, and how it tears down.

No database and no real media work — the unit under test is `worker.run()`'s own wiring, so every
collaborator it calls is stubbed and the two consumer tasks are real. Time is driven by the
supervisor's OWN passes (a counted stub it calls exactly once per pass) plus injected stall bounds,
never by a fixed sleep a loaded machine could turn into a false verdict: every wait ends on an
observed pass, with a generous `wait_for` as a failure bound rather than as the thing measured.
"""

import asyncio
import contextlib
import time

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

    async def wait_seconds_of_passes(self, seconds: float) -> None:
        """Keep watching real passes until the injected bound has demonstrably elapsed."""
        started = time.monotonic()
        await self._until(lambda: time.monotonic() - started >= seconds, give_up_after=seconds + 20)

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


async def test_the_heartbeat_stops_when_a_consumer_hangs(harness, monkeypatch):
    # The hole the old serial loop covered by accident: a consumer suspended inside an AWAIT is
    # neither done nor erroring. Deliberately not an ffmpeg hang — that path is bounded by a
    # subprocess timeout; the unbounded region is the publication tail.
    monkeypatch.setattr(settings, "WORKER_LIGHT_STALL_SECONDS", 1)  # must exceed POLL_SECONDS
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
    with capture_logs() as caps:
        async with _supervisor():
            await asyncio.wait_for(parked.wait(), 20)
            await harness.wait_for_log(caps, "worker_consumer_stalled")
            stamp = harness.stamp()
            await harness.wait_passes(3)
            assert harness.stamp() == stamp, "the beat advanced with a hung consumer"
    stalled = [c for c in caps if c["event"] == "worker_consumer_stalled"]
    assert stalled[0]["job_class"] == "light"
    assert stalled[0]["elapsed_seconds"] >= 1


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
    # the watermark, so the bound must never be reached however long the heat lasts.
    monkeypatch.setattr(settings, "WORKER_LIGHT_STALL_SECONDS", 1)
    monkeypatch.setattr(settings, "WORKER_TRANSCODE_STALL_SECONDS", 1)

    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        return "deferred"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    with capture_logs() as caps:
        async with _supervisor():
            await harness.wait_passes(1)
            stamp = harness.stamp()
            await harness.wait_seconds_of_passes(1.5)  # well past the injected bound
            assert harness.stamp() != stamp, "the beat stopped on a merely deferred worker"
    assert not [c for c in caps if c["event"] == "worker_consumer_stalled"]


async def test_cancelling_the_supervisor_tears_both_consumers_down(harness, monkeypatch):
    # Every caller that drives the loop cancels it; a consumer surviving that would go on draining
    # against patched collaborators or a closed session.
    async def drain(job_class=None) -> str:
        await asyncio.sleep(0)
        harness.drains[job_class] += 1
        return "idle"

    monkeypatch.setattr(worker, "run_media_drain", drain)
    loop_task = asyncio.create_task(worker.run())
    await harness.wait_passes(2)
    consumers = _consumer_tasks()
    assert len(consumers) == 2
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
    assert all(t.done() for t in consumers), "a consumer outlived the supervisor"
    drained = dict(harness.drains)
    for _ in range(10):  # give anything still scheduled every chance to run
        await asyncio.sleep(0)
    assert dict(harness.drains) == drained, "a drain started after the supervisor stopped"


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
