"""Transcode progress is visible while the job runs (MEMNG-36).

The worker claims a media job with SELECT … FOR UPDATE and holds that row lock for the whole
transcode, so anything it writes into the job row stays invisible until the job commits. Progress
goes to `media_job_progress` instead, committed by a session of its own. These tests drive the
REAL worker path — worker.run_once, its claiming transaction, media.process_slow, the progress
pump — and read what another session (the SSE snapshot) can see while that is still running.

No real ffmpeg anywhere: the transcode command is substituted with a python stub that emits
genuine `-progress` lines at a controlled pace (same technique as the timeout tests). The
subprocess machinery, the pump, the publish, the snapshot query and the SSE stream are all real;
only the codec is not.
"""

import asyncio
import contextlib
import json
import os
import secrets
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import cast

from app import worker
from app.config import settings
from app.database import async_session
from app.routers.assets import _ready_meta, _user_snapshot, media_event_stream
from app.services import media
from PIL import Image
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

STUB_STEPS = (250_000, 500_000, 750_000)  # out_time_us marks → 25 / 50 / 75 % of a 1s clip


def _progress_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv: emit real ffmpeg `-progress` lines with a pause between them, then write the
    output file. The pauses are what let another session read progress MID-run — without them the
    'transcode' would be over before anything could observe it."""
    script = (
        "import sys, time\n"
        f"for us in {STUB_STEPS!r}:\n"
        "    print(f'out_time_us={us}', flush=True)\n"
        "    time.sleep(0.25)\n"
        "open(sys.argv[1], 'w').write('ok')\n"
    )
    return [sys.executable, "-c", script, dest]


def _silent_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv for a transcode that publishes NOTHING: it runs long enough to be observed, emits
    no `-progress` lines at all, then writes its output. Anything that clears a progress row during
    such a run can only be the claim, never a new sample."""
    script = "import sys, time\ntime.sleep(0.8)\nopen(sys.argv[1], 'w').write('ok')\n"
    return [sys.executable, "-c", script, dest]


def _seed_owned_video(subject: str, storage_path: str) -> tuple[int, int, int, str]:
    """A committed user + processing video + queued slow job the worker's own session can claim."""
    uid = f"prog-{secrets.token_urlsafe(6)}"
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
            {"u": uid, "o": owner, "sp": storage_path},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, 'slow', 'queued', 0, 0) RETURNING id"
            ),
            {"a": aid},
        ).scalar_one()
    eng.dispose()
    return owner, aid, jid, uid


def _seed_job_row(
    aid: int, lane: str = "slow", state: str = "queued", progress_pct: int = 0
) -> int:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, CAST(:l AS job_lane), CAST(:s AS job_state), 0, :p) RETURNING id"
            ),
            {"a": aid, "l": lane, "s": state, "p": progress_pct},
        ).scalar_one()
    eng.dispose()
    return jid


def _read_job(jid: int) -> tuple[str, int]:
    """The job row as ANOTHER session sees it (a plain read never waits on the claim's lock)."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT state, progress_pct FROM media_jobs WHERE id=:j"), {"j": jid}
        ).one()
    eng.dispose()
    return row[0], row[1]


def _read_progress(jid: int) -> int | None:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        pct = c.execute(
            text("SELECT progress_pct FROM media_job_progress WHERE job_id=:j"), {"j": jid}
        ).scalar_one_or_none()
    eng.dispose()
    return pct


def _write_progress_row(jid: int, pct: int) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO media_job_progress (job_id, progress_pct) VALUES (:j,:p)"),
            {"j": jid, "p": pct},
        )
    eng.dispose()


def _progress_table() -> list[tuple]:
    """Every progress row with the job state behind it — the sweep is database-wide, so a count
    that disagrees has to name what else was in the table."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT p.job_id, p.progress_pct, j.state, j.lane, j.attachment_id "
                "FROM media_job_progress p LEFT JOIN media_jobs j ON j.id = p.job_id "
                "ORDER BY p.job_id"
            )
        ).all()
    eng.dispose()
    return [tuple(r) for r in rows]


def _attachment_status(aid: int) -> str:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        status = c.execute(
            text("SELECT status FROM attachments WHERE id=:a"), {"a": aid}
        ).scalar_one()
    eng.dispose()
    return status


def _cleanup(subject: str | None = None, *aids: int) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            c.execute(
                text(
                    "DELETE FROM media_job_progress WHERE job_id IN "
                    "(SELECT id FROM media_jobs WHERE attachment_id=:a)"
                ),
                {"a": aid},
            )
            c.execute(text("DELETE FROM media_jobs WHERE attachment_id=:a"), {"a": aid})
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
        if subject is not None:
            c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
    eng.dispose()


async def test_sse_reports_real_progress_from_committed_worker_transitions(monkeypatch, tmp_path):
    """End to end, against the real worker: /api/events' stream must show the transcode moving.

    The SSE layer polls _user_snapshot from its own session while worker.run_once processes the job.
    Before MEMNG-36 the percentages existed only inside the worker's own transaction, so every
    event carried the value the row had when the job was claimed — the client watched a
    frozen number until the job finished. The mid-run assertions here (a percentage strictly
    between 0 and 100 seen while the job row's OWN progress_pct is still 0) pin that the live number
    comes from the committed media_job_progress side channel, not the row. Since MEMNG-90 the claim
    is committed before processing, so the row reads `processing` (not `queued`) during the run —
    its progress_pct is still 0, which is the point.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 60)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _progress_cmd)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off (dev host)
    monkeypatch.setattr(media, "PROGRESS_PUBLISH_SECONDS", 0.0)  # publish every stub sample
    src = tmp_path / "originals" / "v.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"stub source")
    subject = "progress_owner"
    owner, aid, jid, uid = _seed_owned_video(subject, str(src))
    # The video's fast lane, already finished at 100 — the real shape of a transcoding video, and
    # the lane whose 100 used to be reported (max across the attachment's jobs) for the whole
    # transcode below.
    _seed_job_row(aid, lane="fast", state="done", progress_pct=100)

    async def drain() -> str:
        async with async_session() as db:
            return await worker.run_once(db)

    async def snap() -> list[dict]:
        return await _user_snapshot(owner)

    async def meta(uids: list[str]) -> dict[str, dict]:
        return await _ready_meta(owner, uids)

    try:
        job = asyncio.create_task(drain())
        events: list[dict] = []
        job_rows: list[tuple[str, int]] = []
        async with asyncio.timeout(90):  # a stuck stream must fail the test, not hang the suite
            async for chunk in media_event_stream(snap, meta, interval=0.05):
                event = json.loads(chunk.removeprefix("data: "))
                events.append(event)
                if 0 < event["progress_pct"] < 100 and not job.done():
                    # Read the job row from a THIRD session at the moment the stream reports
                    # movement: it reads `processing` at progress_pct 0 there — the claim IS
                    # committed (MEMNG-90) but the live percentage is written to the separate
                    # media_job_progress table, so the number the stream just reported cannot
                    # have come from the job row.
                    job_rows.append(_read_job(jid))
        assert await job == "processed"

        moving = [e["progress_pct"] for e in events if 0 < e["progress_pct"] < 100]
        assert moving, f"stream never reported progress between 0 and 100: {events}"
        assert set(moving) <= {25, 50, 75}  # the stub's real marks, not a synthesised number
        assert job_rows and all(row == ("processing", 0) for row in job_rows), job_rows
        assert events[-1] == {
            "uid": uid,
            "status": "ready",
            "progress_pct": 100,
            "width": None,
            "height": None,
            "blurhash": None,
        }
        assert _attachment_status(aid) == "ready"
        assert _read_progress(jid) is None  # the side channel is cleared when the job stops
    finally:
        _cleanup(subject, aid)


async def test_the_reported_percentage_never_goes_backwards(monkeypatch, tmp_path):
    """The whole sequence a client sees must be monotonic, and 100 must mean ready.

    The transcode's last sample lands during processing; the outcome (attachment ready + job done)
    commits a moment later. Clearing the progress row inside that window (as media.process_slow used
    to) made the snapshot fall back to the job row's committed 0 while the attachment still read
    `processing` — the tile ran up to 75%, snapped back to 0%, and only then went ready. The
    window is widened deterministically here by delaying the real guard_publish, which sits
    between the last sample and the commit; without the delay this race is a coin toss to
    observe, and a flaky test proves nothing.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 60)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _progress_cmd)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    monkeypatch.setattr(media, "PROGRESS_PUBLISH_SECONDS", 0.0)
    real_guard = media.guard_publish

    async def _slow_guard(db, job, attachment):
        await asyncio.sleep(0.5)  # a real await point between the last sample and the commit
        await real_guard(db, job, attachment)

    monkeypatch.setattr(media, "guard_publish", _slow_guard)
    src = tmp_path / "originals" / "v.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"stub source")
    subject = "monotonic_owner"
    owner, aid, jid, uid = _seed_owned_video(subject, str(src))

    async def drain() -> str:
        async with async_session() as db:
            return await worker.run_once(db)

    async def snap() -> list[dict]:
        return await _user_snapshot(owner)

    async def meta(uids: list[str]) -> dict[str, dict]:
        return await _ready_meta(owner, uids)

    try:
        job = asyncio.create_task(drain())
        events: list[dict] = []
        async with asyncio.timeout(90):
            async for chunk in media_event_stream(snap, meta, interval=0.05):
                events.append(json.loads(chunk.removeprefix("data: ")))
        assert await job == "processed"

        seen = [(e["status"], e["progress_pct"]) for e in events]
        pcts = [e["progress_pct"] for e in events]
        assert pcts == sorted(pcts), f"the reported percentage went backwards: {seen}"
        # 100 is the finish line, not a state the tile sits in while work continues.
        assert [s for s, p in seen if p == 100 and s != "ready"] == [], seen
        assert seen[-1] == ("ready", 100)
        assert _attachment_status(aid) == "ready"
        assert _read_progress(jid) is None  # cleared after the commit, not before it
    finally:
        _cleanup(subject, aid)


async def test_a_reclaim_never_reports_the_dead_attempts_progress(monkeypatch, tmp_path):
    """A killed worker clears nothing: its claim rolls back and the job returns to `queued` still
    carrying the percentage the dead attempt reached. The next claim must drop that row before the
    new attempt publishes anything — otherwise the snapshot reports a transcode as (say) 60% done
    from the instant it restarts at 0.

    The stub emits no progress at all, so the only thing that can clear the row while this attempt
    runs is the claim itself.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 60)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _silent_cmd)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    src = tmp_path / "originals" / "v.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"stub source")
    subject = "reclaim_owner"
    owner, aid, jid, _ = _seed_owned_video(subject, str(src))
    _write_progress_row(jid, 60)  # what the killed attempt left behind

    async def drain() -> str:
        async with async_session() as db:
            return await worker.run_once(db)

    try:
        assert _read_progress(jid) == 60  # the stale row is really there before the claim
        job = asyncio.create_task(drain())
        cleared_while_running = False
        stale_snapshots: list[int] = []
        async with asyncio.timeout(90):
            while not job.done():
                pct = _read_progress(jid)
                if pct is None:
                    cleared_while_running = True
                    break
                stale_snapshots.append(pct)
                await asyncio.sleep(0.05)
        assert await job == "processed"

        assert cleared_while_running, (
            f"the dead attempt's row survived into the new one: {stale_snapshots}"
        )
        assert _read_progress(jid) is None
    finally:
        _cleanup(subject, aid)


async def test_publish_progress_commits_so_another_session_can_read_it(monkeypatch, tmp_path):
    """The publish is its own transaction: visible immediately, and repeatable (upsert)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    subject = "publish_owner"
    _, aid, jid, _ = _seed_owned_video(subject, str(tmp_path / "v.mp4"))
    try:
        await media.publish_progress(jid, 42)
        assert _read_progress(jid) == 42  # fresh connection, no waiting

        await media.publish_progress(jid, 71)
        assert _read_progress(jid) == 71  # second sample updates the same row, never a duplicate

        await media.clear_progress(jid)
        assert _read_progress(jid) is None
    finally:
        _cleanup(subject, aid)


async def test_a_failing_progress_write_never_reaches_the_transcode(monkeypatch):
    """Progress is telemetry. If the write blows up (DB gone, pool exhausted), the transcode it
    describes must still run to completion — so publish/clear swallow and log, never raise."""

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated progress outage")

    monkeypatch.setattr(media, "async_session", _explode)
    with capture_logs() as logs:
        await media.publish_progress(1234567, 50)  # must not raise
        await media.clear_progress(1234567)  # must not raise

    events = [entry["event"] for entry in logs]
    assert "job_progress_publish_failed" in events  # swallowed, but never silent
    assert "job_progress_clear_failed" in events


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line

        return gen()


class _FakeProc:
    """Stand-in for the ffmpeg process — a dependency of the pump, not the unit under test (which
    is the pump's publish cadence)."""

    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = 0

    async def wait(self) -> int:
        return 0


async def test_progress_publishing_is_throttled_to_the_configured_cadence(monkeypatch):
    """ffmpeg emits a progress block per frame. One committed round-trip per frame would be a
    write storm against a Postgres shared with production, so the pump publishes at most once per
    PROGRESS_PUBLISH_SECONDS — every line still being parsed and tailed."""
    lines = [f"out_time_us={n}\n".encode() for n in (100_000, 200_000, 300_000, 400_000, 500_000)]
    published: list[int] = []

    async def _spy(job_id: int, pct: int) -> None:
        published.append(pct)

    monkeypatch.setattr(media, "publish_progress", _spy)
    monkeypatch.setattr(media, "PROGRESS_PUBLISH_SECONDS", 2.0)
    tail: deque[str] = deque(maxlen=12)
    proc = cast(asyncio.subprocess.Process, _FakeProc(list(lines)))
    await media._pump_progress(1, proc, tail, 1.0)
    assert published == [10]  # five samples in an instant → one committed write
    assert len(tail) == 5  # …and the ffmpeg tail still saw every line

    published.clear()
    monkeypatch.setattr(media, "PROGRESS_PUBLISH_SECONDS", 0.0)
    proc = cast(asyncio.subprocess.Process, _FakeProc(list(lines)))
    await media._pump_progress(1, proc, deque(maxlen=12), 1.0)
    assert published == [10, 20, 30, 40, 50]  # the throttle is the only thing dropping samples


async def test_sweep_removes_rows_no_running_job_stands_behind(monkeypatch, tmp_path):
    """The backstop for a worker killed outright — it clears nothing, at either seam.

    Both shapes it can leave are stale: a row whose job is GONE (deleted with its attachment) and
    a row whose job has FINISHED between its outcome commit and the post-commit clear. A row whose
    job may still be running is spared: from any other session that job reads `queued`, because
    its claim is uncommitted.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # The sweep is database-wide, and its RETURN VALUE is asserted exactly below — so start from a
    # drained side channel rather than counting whatever residue an earlier test left behind.
    await media.sweep_stale_progress()
    subject = "sweep_owner"
    _, aid, running_jid, _ = _seed_owned_video(subject, str(tmp_path / "v.mp4"))
    orphan_jid = _seed_job_row(aid)
    finished_jid = _seed_job_row(aid, state="done", progress_pct=100)
    try:
        _write_progress_row(running_jid, 30)
        _write_progress_row(orphan_jid, 60)
        _write_progress_row(finished_jid, 90)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:  # the job vanishes (deleted attachment / discarded job) …
            c.execute(text("DELETE FROM media_jobs WHERE id=:j"), {"j": orphan_jid})
        eng.dispose()

        assert await media.sweep_stale_progress() == 2, _progress_table()

        assert _read_progress(orphan_jid) is None
        assert _read_progress(finished_jid) is None
        assert _read_progress(running_jid) == 30  # … the running job's sample is untouched
    finally:
        _cleanup(subject, aid)


async def test_worker_sweep_isolates_its_errors(monkeypatch):
    """A sweep failure must never propagate into the worker loop — media processing does not stop
    because a telemetry table could not be tidied."""

    async def _explode() -> int:
        raise RuntimeError("simulated sweep failure")

    monkeypatch.setattr(media, "sweep_stale_progress", _explode)

    assert await worker.sweep_job_progress_safe() == 0


# --- head-of-line blocking: one parked transcode must not hold the light class up --------------


def _parked_cmd(gate: Path):
    """Stub argv factory for a transcode that PARKS until the test opens a gate FILE.

    A file gate, not a sleep: the test releases the transcode at a moment it chooses, and nothing
    here depends on how loaded the machine is. The pid is written next to the output so the
    teardown can prove no parked stub outlived its test.
    """
    script = (
        "import os, sys, time\n"
        "open(sys.argv[1] + '.started', 'w').write(str(os.getpid()))\n"
        "while not os.path.exists(sys.argv[2]):\n"
        "    time.sleep(0.02)\n"
        "open(sys.argv[1], 'w').write('ok')\n"
    )

    def cmd(src: str, dest: str, threads: int) -> list[str]:
        return [sys.executable, "-c", script, dest, str(gate)]

    return cmd


def _parked_pids(root: Path) -> list[int]:
    pids = []
    for marker in root.rglob("*.started"):
        try:
            pids.append(int(marker.read_text().strip()))
        except (OSError, ValueError):
            continue
    return pids


def _parked_alive(root: Path) -> list[int]:
    """Parked stubs that are STILL RUNNING, each confirmed by its own argv so a recycled pid can
    never be mistaken for a live transcode. 'While the transcode is processing' is a claim about a
    real process, and this is what reads it."""
    live = []
    for pid in _parked_pids(root):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue  # exited between listing and reading
        if b"sys.argv[1] + '.started'" in argv:
            live.append(pid)
    return live


def _reap_parked(root: Path) -> None:
    """Teardown net: a stub still parked when its test ends is killed, identified by its OWN argv
    so a recycled pid can never be the victim."""
    deadline = time.monotonic() + 10
    for pid in _parked_pids(root):
        while time.monotonic() < deadline:
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                break  # gone, or reaped by the event loop
            if b"sys.argv[1] + '.started'" not in argv:
                break  # some other process now owns this pid
            time.sleep(0.05)
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


async def _until(done, what: str, give_up_after: float = 90.0) -> None:
    """Wait on OBSERVED state, never on a clock: poll until `done()` holds. The bound is a failure
    message, not a measurement — several suites share this machine and a fixed sleep would be a
    verdict about its load rather than about the worker."""
    try:
        # noqa ASYNC110: the thing being waited on is a row another SESSION commits and a file a
        # SUBPROCESS writes — neither can set an asyncio.Event in this loop's process.
        async with asyncio.timeout(give_up_after):
            while not done():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError(f"timed out waiting for {what}") from None


def _read_claim(jid: int) -> tuple[str, int, str | None]:
    """State, attempts and the claim identity as ANOTHER session sees them."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT state, attempts, claimed_by FROM media_jobs WHERE id=:j"), {"j": jid}
        ).one()
    eng.dispose()
    return row[0], row[1], row[2]


def _seed_head_of_line_videos(subject: str, root: Path) -> dict[str, int | str]:
    """The owner and TWO slow videos, ids ascending — the queue the photo will arrive behind.

    Two, not one: with a single transcode slot they cannot both run, so anything claimed by queue
    position has at least one transcode ahead of it for the whole test.
    """
    ids: dict[str, int | str] = {}
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
        owner = c.execute(
            text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
            {"s": subject},
        ).scalar_one()
        for name in ("a", "b"):
            src = root / "originals" / f"v{name}.mp4"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(b"stub source")
            uid = f"prog-v{name}-{secrets.token_urlsafe(6)}"
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
            ids[f"aid_{name}"], ids[f"jid_{name}"], ids[f"uid_{name}"] = aid, jid, uid
    eng.dispose()
    return ids


def _seed_late_photo(subject: str, root: Path) -> dict[str, int | str]:
    """The photo of the acceptance criterion, committed only when the caller says so.

    Inserted after the videos it must overtake, it gets a later attachment id, a later job id and a
    later created_at — so it is an UPLOAD THAT ARRIVED DURING the transcode, which is the thing the
    criterion is about. Seeded up front it would prove only that a photo already in the queue can
    be drained first.
    """
    uid_img = f"prog-img-{secrets.token_urlsafe(6)}"
    img = root / "originals" / f"{uid_img}.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (900, 600), (30, 90, 160)).save(img, "PNG")
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": subject}
        ).scalar_one()
        aid_c = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, owner_id, filename, mime, size, status, storage_path) "
                "VALUES (:u,:o,'i.png','image/png',:sz,'processing',:sp) RETURNING id"
            ),
            {"u": uid_img, "o": owner, "sz": img.stat().st_size, "sp": str(img)},
        ).scalar_one()
        jid_c = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, 'fast', 'queued', 0, 0) RETURNING id"
            ),
            {"a": aid_c},
        ).scalar_one()
    eng.dispose()
    return {"aid_c": aid_c, "jid_c": jid_c, "uid_c": uid_img}


def _start_consumers(*classes: str) -> list[asyncio.Task[None]]:
    """The real consumers, one per class, each with its OWN health and watermark — exactly what
    worker.run() spawns, driven here against the real database."""
    return [
        asyncio.create_task(
            worker.consume_media(
                cast(worker.JobClass, job_class),
                worker._DrainHealth(settings.WORKER_UNHEALTHY_ERROR_STREAK),
                worker._Watermark(),
            ),
            name=f"media-consumer-{job_class}",
        )
        for job_class in classes
    ]


async def _stop(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_parked_transcode_never_blocks_the_light_class_behind_it(monkeypatch, tmp_path):
    """AC1: a photo uploaded WHILE a transcode runs reaches `done` without waiting for it.

    The order is the proof, so the test enforces it: two videos are queued, video A is claimed and
    its stub really parks, and only THEN is the photo inserted — later attachment id, later job id,
    later created_at than both videos. The photo is therefore an arrival during the transcode, and
    reaching `done` while A's ffmpeg process is still parked cannot come from queue position. Video
    B stays queued throughout: one transcode slot, one transcode.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 300)  # the park is not a timeout test
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off (dev host)
    gate = tmp_path / "release-the-transcode"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _parked_cmd(gate))
    subject = "head_of_line_owner"
    ids = _seed_head_of_line_videos(subject, tmp_path)
    jid_a, jid_b = cast(int, ids["jid_a"]), cast(int, ids["jid_b"])
    photo: dict[str, int | str] = {}

    tasks = _start_consumers("transcode", "light")
    try:
        await _until(
            lambda: (
                (
                    (tmp_path / "assets" / "web" / f"{ids['uid_a']}.tmp.mp4.started").exists()
                    and _read_claim(jid_a)[0] == "processing"
                )
                # …or the queue has already gone wrong: video B claimed means both consumers took
                # transcodes, which the assertions below then name precisely instead of timing out.
                or _read_claim(jid_b)[0] != "queued"
            ),
            "video A's transcode to start and hold the slot",
        )

        photo = _seed_late_photo(subject, tmp_path)  # the upload lands mid-transcode
        jid_c = cast(int, photo["jid_c"])
        tiers = [tmp_path / "assets" / t / f"{photo['uid_c']}.jpg" for t in ("thumbs", "mobile")]
        await _until(
            lambda: _read_claim(jid_c)[0] == "done" or _read_claim(jid_b)[0] != "queued",
            f"the photo uploaded during the transcode (media_jobs id {jid_c}) to reach done",
        )

        # Taken while the gate is STILL CLOSED: video A cannot have finished, so every state below
        # is the state of a queue with a transcode in flight.
        state_a, attempts_a, claimed_a = _read_claim(jid_a)
        state_b, _, _ = _read_claim(jid_b)
        state_c, attempts_c, claimed_c = _read_claim(jid_c)
        assert state_a == "processing", state_a
        assert _parked_alive(tmp_path), "no transcode process was still running when the photo done"
        assert state_b == "queued", (  # AC3: at most one ffmpeg transcode at a time
            f"video B is {state_b}: a second transcode started while the first held the slot"
        )
        assert (state_c, attempts_c) == ("done", 0), (state_c, attempts_c)
        assert attempts_a == 0, attempts_a  # neither job was retried into its result
        assert all(tier.exists() for tier in tiers), [str(t) for t in tiers]
        assert claimed_c is not None and claimed_c.endswith("/light"), claimed_c
        assert claimed_a is not None and claimed_a.endswith("/transcode"), claimed_a

        gate.write_text("go")  # release video A; B is claimed next and runs the same stub
        await _until(lambda: _read_claim(jid_a)[0] == "done", "video A to finish")
        await _until(lambda: _read_claim(jid_b)[0] == "done", "video B to be claimed and finish")
        assert cast(str, _read_claim(jid_b)[2]).endswith("/transcode")
    finally:
        gate.write_text("go")  # whatever the body did, no stub stays parked
        await _stop(tasks)
        _reap_parked(tmp_path)
        seeded = [cast(int, ids["aid_a"]), cast(int, ids["aid_b"])]
        if photo:  # the photo may never have been seeded (an early failure above)
            seeded.append(cast(int, photo["aid_c"]))
        _cleanup(subject, *seeded)
