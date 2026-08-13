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
import json
import secrets
import sys
from collections import deque
from typing import cast

from app import worker
from app.config import settings
from app.database import async_session
from app.routers.assets import _ready_meta, _user_snapshot, media_event_stream
from app.services import media
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

    The SSE layer polls _user_snapshot from its own session while worker.run_once holds the claim.
    Before MEMNG-36 the percentages existed only inside that uncommitted transaction, so every
    event carried the value the row had when the job was claimed — the client watched a
    frozen number until the job finished. The mid-run assertions here (a percentage strictly
    between 0 and 100 seen while the job row is STILL uncommitted) are what pins that.
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
        uncommitted: list[tuple[str, int]] = []
        async with asyncio.timeout(90):  # a stuck stream must fail the test, not hang the suite
            async for chunk in media_event_stream(snap, meta, interval=0.05):
                event = json.loads(chunk.removeprefix("data: "))
                events.append(event)
                if 0 < event["progress_pct"] < 100 and not job.done():
                    # Read the job row from a THIRD session at the moment the stream reports
                    # movement: it is still `queued` at 0 there, because the worker's claim and
                    # its progress writes are uncommitted. So the number the stream just
                    # reported cannot have come from that row.
                    uncommitted.append(_read_job(jid))
        assert await job == "processed"

        moving = [e["progress_pct"] for e in events if 0 < e["progress_pct"] < 100]
        assert moving, f"stream never reported progress between 0 and 100: {events}"
        assert set(moving) <= {25, 50, 75}  # the stub's real marks, not a synthesised number
        assert uncommitted and all(row == ("queued", 0) for row in uncommitted), uncommitted
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


async def test_sweep_removes_only_progress_rows_whose_job_is_gone(monkeypatch, tmp_path):
    """The backstop for a worker killed mid-transcode: its row cannot be cleared by anyone else,
    so the sweep collects rows whose job no longer exists — and nothing else."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    subject = "sweep_owner"
    _, aid, live_jid, _ = _seed_owned_video(subject, str(tmp_path / "v.mp4"))
    orphan_jid = _seed_job_row(aid)
    try:
        _write_progress_row(live_jid, 30)
        _write_progress_row(orphan_jid, 60)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:  # the job vanishes (deleted attachment / discarded job) …
            c.execute(text("DELETE FROM media_jobs WHERE id=:j"), {"j": orphan_jid})
        eng.dispose()

        assert await media.sweep_orphan_progress() == 1

        assert _read_progress(orphan_jid) is None
        assert _read_progress(live_jid) == 30  # … the running job's sample is untouched
    finally:
        _cleanup(subject, aid)


async def test_worker_sweep_isolates_its_errors(monkeypatch):
    """A sweep failure must never propagate into the worker loop — media processing does not stop
    because a telemetry table could not be tidied."""

    async def _explode() -> int:
        raise RuntimeError("simulated sweep failure")

    monkeypatch.setattr(media, "sweep_orphan_progress", _explode)

    assert await worker.sweep_job_progress_safe() == 0
