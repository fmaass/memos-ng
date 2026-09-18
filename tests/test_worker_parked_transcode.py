"""The supervisor while ONE transcode is parked for a long time, against the real queue.

`tests/test_worker_supervisor.py` proves the beat gate with every collaborator stubbed. These
proofs keep the media path REAL — the claim, run_once, media.process_slow, a genuine ffmpeg
subprocess (a python stub parked on a gate file) — and ask the three questions a stubbed drain
cannot: does the beat keep going while a transcode legitimately takes a long time, is it the
class's stall BOUND that decides that, and does the thermal guard hold each consumer back at its
own job start rather than the worker as a whole.

Nothing here waits on the clock: the transcode is released by creating a file, every wait ends on
observed state or on an observed supervisor pass, and the timeouts are failure bounds. Several
suites share this machine.
"""

import asyncio
import contextlib
import secrets
import time
from pathlib import Path

import pytest
from app import migrations, worker
from app.config import settings
from app.services import media
from PIL import Image
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

# The parked-transcode stub, the bounded wait, the pid-checked reaper and the claim read are shared
# with the AC1 proof rather than written twice — one stub, one teardown net.
from tests.test_progress_visibility import _cleanup, _parked_cmd, _read_claim, _reap_parked, _until

# How long the transcode is held parked in the bound proof. It has to outlast the bound the
# red-proof injects (1 s) — nothing else about it is a measurement.
PARK_SECONDS = 1.5


class _Harness:
    """Counts supervisor passes and reads the heartbeat file the healthcheck reads."""

    def __init__(self) -> None:
        self.passes = 0
        self.pass_event = asyncio.Event()

    def saw_a_pass(self) -> None:
        self.passes += 1
        self.pass_event.set()

    def stamp(self) -> float | None:
        """The heartbeat's VALUE (beat() writes time.time() into it), not its mtime: a float the
        filesystem cannot round away, which is what 'the beat stopped advancing' has to compare."""
        path = worker.heartbeat_path()
        return float(path.read_text()) if path.exists() else None

    async def _until_pass(self, done, give_up_after: float) -> None:
        async with asyncio.timeout(give_up_after):
            while not done():
                self.pass_event.clear()
                await self.pass_event.wait()

    async def stamps_seen(self, n: int, give_up_after: float = 30.0) -> set[float]:
        """Distinct heartbeat values observed over real supervisor passes. A beat that has stopped
        produces the same value forever, so this waits out its bound and fails there."""
        seen: set[float] = set()

        def collected() -> bool:
            value = self.stamp()
            if value is not None:
                seen.add(value)
            return len(seen) >= n

        try:
            await self._until_pass(collected, give_up_after)
        except TimeoutError:
            raise AssertionError(
                f"only {len(seen)} distinct heartbeat stamp(s) in {give_up_after}s"
            ) from None
        return seen

    async def watch_for(self, seconds: float) -> None:
        """Keep watching real passes until `seconds` have demonstrably elapsed."""
        started = time.monotonic()
        await self._until_pass(lambda: time.monotonic() - started >= seconds, seconds + 30)


@pytest.fixture
def supervised(monkeypatch, tmp_path):
    """worker.run() with every collaborator stubbed EXCEPT the media path (the same harness idiom
    as tests/test_worker_supervisor.py), so the consumers drain the real queue.

    Every setting the supervisor reads is pinned to its SHIPPED default rather than to a literal or
    to whatever an earlier test left on the singleton: the bound proof below is only worth
    something if the bound is the one production runs with.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)
    defaults = type(settings).model_fields
    for key in (
        "WORKER_TRANSCODE_SLOTS",
        "WORKER_LIGHT_SLOTS",
        "WORKER_TRANSCODE_CPU_COST",
        "WORKER_CPU_BUDGET",
        "FFMPEG_THREADS",
        "WORKER_TRANSCODE_STALL_SECONDS",
        "WORKER_LIGHT_STALL_SECONDS",
        "MEDIA_CLAIM_STALE_SECONDS",
        "WORKER_UNHEALTHY_ERROR_STREAK",
        "MEDIA_MAX_ATTEMPTS",
        "THERMAL_MAX_C",
    ):
        monkeypatch.setattr(settings, key, defaults[key].default)
    # The park is not a timeout test: the transcode ceiling must not fire while the gate is closed.
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 300)

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
    monkeypatch.setattr(worker, "digest_task_in_flight", lambda: True)
    return state


@contextlib.asynccontextmanager
async def _supervisor():
    """Run worker.run() as its caller does, and cancel it the way every caller does — which is also
    what tears both consumers (and with them any claim) down."""
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _seed_video(subject: str, root: Path) -> tuple[int, int, str]:
    """A committed user + processing video + queued slow job: the transcode class to claim."""
    uid = f"parked-{secrets.token_urlsafe(6)}"
    src = root / "originals" / f"{uid}.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"stub source")
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
        owner = c.execute(
            text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
            {"s": subject},
        ).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, owner_id, filename, mime, size, status, storage_path, duration) "
                "VALUES (:u,:o,'v.mp4','video/mp4',1,'processing',:sp,1.0) RETURNING id"
            ),
            {"u": uid, "o": owner, "sp": str(src)},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, 'slow', 'queued', 0, 0) RETURNING id"
            ),
            {"a": aid},
        ).scalar_one()
    eng.dispose()
    return aid, jid, uid


def _seed_image(subject: str, root: Path) -> tuple[int, int, str]:
    """A real image + queued fast job for the SAME owner: the light class, committed on demand."""
    uid = f"light-{secrets.token_urlsafe(6)}"
    src = root / "originals" / f"{uid}.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (900, 600), (30, 90, 160)).save(src, "PNG")
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": subject}
        ).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, owner_id, filename, mime, size, status, storage_path) "
                "VALUES (:u,:o,'i.png','image/png',:sz,'processing',:sp) RETURNING id"
            ),
            {"u": uid, "o": owner, "sz": src.stat().st_size, "sp": str(src)},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, 'fast', 'queued', 0, 0) RETURNING id"
            ),
            {"a": aid},
        ).scalar_one()
    eng.dispose()
    return aid, jid, uid


def _started(root: Path, uid: str) -> Path:
    """The marker the parked stub writes as its first act — the transcode has really begun."""
    return root / "assets" / "web" / f"{uid}.tmp.mp4.started"


async def test_the_beat_keeps_stamping_while_a_transcode_is_parked(
    supervised, monkeypatch, tmp_path
):
    """Heartbeat honesty: a worker busy with a long transcode is alive and must keep saying so.

    The old shape stamped the beat only after the media drain returned, so a legitimate hour-long
    transcode looked exactly like a dead worker and Docker restarted it mid-job. The supervisor
    owns the stamp now, and this asks for the stamp to move MORE THAN ONCE while the job is
    demonstrably still `processing` — one stamp could be the one it wrote on the way in.
    """
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off (dev host)
    gate = tmp_path / "release-the-transcode"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _parked_cmd(gate))
    subject = "parked_beat_owner"
    aid, jid, uid = _seed_video(subject, tmp_path)
    try:
        async with _supervisor():
            await _until(lambda: _started(tmp_path, uid).exists(), "the transcode to start")
            stamps = await supervised.stamps_seen(2)
            # Read AFTER the stamps: a job still processing here was processing throughout, since
            # a job state only moves forward.
            assert _read_claim(jid)[0] == "processing", (
                "the transcode ended before the beat was observed, so this proves nothing"
            )
            assert len(stamps) >= 2

            gate.write_text("go")
            await _until(lambda: _read_claim(jid)[0] == "done", "the released transcode to finish")
    finally:
        gate.write_text("go")  # whatever the body did, no stub stays parked
        _reap_parked(tmp_path)
        _cleanup(subject, aid)


async def test_a_long_transcode_inside_its_bound_keeps_the_beat(supervised, monkeypatch, tmp_path):
    """…and it is the class's stall BOUND that says so, not an unconditional stamp.

    The transcode consumer stamps its watermark on the two sides of the drain, so during a parked
    transcode the supervisor watches the elapsed time grow against WORKER_TRANSCODE_STALL_SECONDS
    (pinned to the shipped default by the fixture). Inside the bound: the beat advances and no
    stall is logged. The red-proof puts the bound below the park and this turns red, which is what
    makes the assertion a statement about the bound rather than a constant.
    """
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    gate = tmp_path / "release-the-transcode"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _parked_cmd(gate))
    subject = "parked_bound_owner"
    aid, jid, uid = _seed_video(subject, tmp_path)
    try:
        with capture_logs() as caps:
            async with _supervisor():
                await _until(lambda: _started(tmp_path, uid).exists(), "the transcode to start")
                await supervised.watch_for(PARK_SECONDS)
                # Both samples are taken PAST the injected red-proof bound, so a beat that stopped
                # when the bound elapsed cannot satisfy them with stamps it wrote before it.
                before = supervised.stamp()
                await supervised.stamps_seen(2, give_up_after=10.0)  # one is the value just read
                after = supervised.stamp()
                state = _read_claim(jid)[0]
                gate.write_text("go")
                await _until(lambda: _read_claim(jid)[0] == "done", "the transcode to finish")
        assert state == "processing", "the park ended early, so the window proves nothing"
        assert before is not None and after is not None
        assert after > before, "the beat stopped during a transcode well inside its bound"
        stalled = [c for c in caps if c["event"] == "worker_consumer_stalled"]
        assert not stalled, stalled
    finally:
        gate.write_text("go")
        _reap_parked(tmp_path)
        _cleanup(subject, aid)


async def test_the_thermal_gate_is_taken_at_every_job_start_by_every_consumer(
    supervised, monkeypatch, tmp_path
):
    """Heat that arrives mid-transcode stops the NEXT job of either class, not the running one.

    One gate per consumer per job start is what keeps the image work — which has no inner thermal
    check of its own — from becoming a thumbnail storm on a host that is already too hot, while the
    transcode that was started cool is allowed to finish rather than being thrown away.
    """
    hot = (settings.THERMAL_MAX_C + 5) * 1000  # derived from the limit, never a literal
    cool = (settings.THERMAL_MAX_C - 30) * 1000
    sensor = {"millideg": cool}
    monkeypatch.setattr(media, "read_temp_millideg", lambda: sensor["millideg"])
    gate = tmp_path / "release-the-transcode"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _parked_cmd(gate))
    subject = "parked_thermal_owner"
    aid_v, jid_v, uid_v = _seed_video(subject, tmp_path)
    aid_i: int | None = None
    try:
        with capture_logs() as caps:
            async with _supervisor():
                await _until(lambda: _started(tmp_path, uid_v).exists(), "the transcode to start")
                sensor["millideg"] = hot

                def defers() -> list[dict]:
                    return [c for c in caps if c["event"] == "worker_thermal_defer"]

                # Seeded only after a defer that POST-DATES the heat. Seeding on the flip itself
                # left a window: a consumer already past its gate with a cool reading, suspended on
                # an await, claimed the image the instant this (blocking) seed committed — a ~1-in-5
                # flake with nothing wrong in the worker. A logged defer proves that pass is over,
                # and the sensor reads hot for every pass after it. While the transcode is parked
                # the light consumer is the only one calling run_once, so every defer here is its
                # own.
                await _until(
                    lambda: defers(),
                    "the light consumer to read the new heat",
                    give_up_after=30.0,  # a gate that never fires must not cost the full bound
                )
                aid_i, jid_i, uid_i = _seed_image(subject, tmp_path)

                await _until(
                    lambda: len(defers()) >= 3 or _read_claim(jid_i)[0] != "queued",
                    "the light consumer to defer its job while the host is too hot",
                )
                state_i, attempts_i, claimed_i = _read_claim(jid_i)
                assert state_i == "queued", f"the light job was {state_i} while the host was hot"
                assert attempts_i == 0, "a thermal defer counted as an attempt"
                assert claimed_i is None, claimed_i
                assert len(defers()) >= 3
                assert defers()[0]["max_c"] == settings.THERMAL_MAX_C
                assert defers()[0]["temp_c"] == round(hot / 1000, 1)
                # The image's tiers do not exist either — the defer is before the work, not after.
                assert not (tmp_path / "assets" / "thumbs" / f"{uid_i}.jpg").exists()

                gate.write_text("go")
                await _until(
                    lambda: _read_claim(jid_v)[0] == "done",
                    "the already-running transcode to finish despite the heat",
                )
                sensor["millideg"] = cool
                await _until(
                    lambda: _read_claim(jid_i)[0] == "done", "the light job to drain once cool"
                )
                assert _read_claim(jid_i)[1] == 0, "the deferred job was charged an attempt"
                assert (tmp_path / "assets" / "thumbs" / f"{uid_i}.jpg").exists()
    finally:
        gate.write_text("go")
        _reap_parked(tmp_path)
        _cleanup(subject, aid_v, *([aid_i] if aid_i is not None else []))


async def test_a_queued_video_is_not_started_while_the_host_is_too_hot(
    supervised, monkeypatch, tmp_path
):
    """The other half of "every consumer": the TRANSCODE consumer's own job start is gated too.

    The test above holds a light job back while a transcode that started cool runs on. This one
    offers the hot host the heavy job — the class that produces the heat — and asks that nothing
    starts: no ffmpeg process, no `processing` row, no attempt charged. Cooling is then the only
    thing that changes, and the same video runs, which is what makes the refusal a gate rather
    than a stuck queue.
    """
    hot = (settings.THERMAL_MAX_C + 5) * 1000  # derived from the limit, never a literal
    cool = (settings.THERMAL_MAX_C - 30) * 1000
    sensor = {"millideg": hot}  # hot BEFORE the worker starts: the video is never offered cool
    monkeypatch.setattr(media, "read_temp_millideg", lambda: sensor["millideg"])
    gate = tmp_path / "release-the-transcode"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _parked_cmd(gate))
    subject = "hot_transcode_owner"
    aid_v, jid_v, uid_v = _seed_video(subject, tmp_path)
    try:
        with capture_logs() as caps:
            async with _supervisor():

                def defers() -> list[dict]:
                    return [c for c in caps if c["event"] == "worker_thermal_defer"]

                # Either end is a verdict: three real defers means the consumers kept asking and
                # kept being refused; a started marker means the gate let the video through.
                await _until(
                    lambda: len(defers()) >= 3 or _started(tmp_path, uid_v).exists(),
                    "the transcode consumer to defer the queued video while the host is too hot",
                    give_up_after=30.0,  # a gate that never fires must not cost the full bound
                )
                state_v, attempts_v, claimed_v = _read_claim(jid_v)
                assert not _started(tmp_path, uid_v).exists(), (
                    "ffmpeg started on a host that was already too hot"
                )
                assert state_v == "queued", f"the video was {state_v} while the host was hot"
                assert attempts_v == 0, "a thermal defer counted as an attempt"
                assert claimed_v is None, claimed_v
                assert defers()[0]["max_c"] == settings.THERMAL_MAX_C
                assert defers()[0]["temp_c"] == round(hot / 1000, 1)

                sensor["millideg"] = cool  # the ONLY thing that changes
                await _until(
                    lambda: (
                        _started(tmp_path, uid_v).exists() and _read_claim(jid_v)[0] == "processing"
                    ),
                    "the queued video to start once the host reads cool",
                )
                gate.write_text("go")
                await _until(
                    lambda: _read_claim(jid_v)[0] == "done", "the released transcode to finish"
                )
                assert _read_claim(jid_v)[1] == 0, "the deferred job was charged an attempt"
    finally:
        gate.write_text("go")
        _reap_parked(tmp_path)
        _cleanup(subject, aid_v)
