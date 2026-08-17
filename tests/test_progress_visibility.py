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
