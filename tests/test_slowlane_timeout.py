"""Slow-lane transcode timeout (MEMNG-38).

No real ffmpeg anywhere in this module: the transcode command is substituted with a shell stub
that hangs (or finishes instantly), so the real subprocess/timeout/kill machinery is exercised
without depending on ffmpeg actually wedging. The injected ceiling keeps every test ~1s.
"""

import asyncio
import contextlib
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import media
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

# Effectively forever: a stub grandchild must never be able to pass an "is it gone?" assertion by
# exiting on its own while the ceiling and the poll windows elapse. Bounded runtime comes from the
# injected ceiling and the reaper fixture below, never from the stub outliving the test by luck.
HANG_SECONDS = 86_400


@pytest.fixture(autouse=True)
def _reap_stub_children(tmp_path):
    """Teardown net: kill any stub grandchild still alive (a failed assertion, or a regression,
    must not leave a day-long orphan behind). Only ever signals a pid that still looks like the
    stub — a recycled pid belongs to a stranger."""
    yield
    for marker in tmp_path.rglob("*.child"):
        try:
            pid = int(marker.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError):
            continue
        if b"sleep" in cmdline:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def _hang_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv: write a partial output file, spawn a CHILD that hangs ~forever, then wait on it.

    The grandchild is the point — only signalling the process GROUP reaches it, so a stopper that
    kills the direct child alone leaves it running. Its pid is written next to the output so the
    test can assert the whole tree is gone.
    """
    return [
        "sh",
        "-c",
        f'printf partial > "$1"; sleep {HANG_SECONDS} & echo $! > "$1.child"; wait',
        "sh",
        dest,
    ]


def _orphan_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv where the LEADER exits immediately but leaves a descendant hanging ~forever and
    still holding the inherited stdout pipe (no `wait`).

    This is the case a leader-keyed cleanup misses entirely: proc.returncode is set long before the
    ceiling fires, so a "kill only if the leader is still running" guard skips the sweep and the
    descendant survives.
    """
    return [
        "sh",
        "-c",
        f'printf partial > "$1"; sleep {HANG_SECONDS} & echo $! > "$1.child"',
        "sh",
        dest,
    ]


def _quick_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv for a transcode that finishes immediately with a valid (tiny) output."""
    return ["sh", "-c", 'printf ok > "$1"', "sh", dest]


def _proc_state(pid: int) -> str | None:
    """The Linux /proc state letter for a pid ('R'/'S'/'Z'/…), or None if it does not exist."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rpartition(")")[2].split()[0]


def _await_state(pid: int, state: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _proc_state(pid) == state:
            return True
        time.sleep(0.02)
    return False


class _FakeLeader:
    """Stand-in for an already-exited, already-reaped leader — a dependency of _stop_transcode,
    not the unit under test (which is the group probe/escalation logic itself)."""

    def __init__(self, pid: int, returncode: int = 0):
        self.pid = pid
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


def _alive(pid: int) -> bool:
    """True while the pid exists and is not a reaped-pending zombie (Linux /proc)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    """Signal delivery + reaping are asynchronous — poll briefly instead of racing them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _spy_on_subprocess(monkeypatch) -> list:
    """Capture the Process objects media.process_slow starts (to assert they were reaped)."""
    started: list = []
    real = asyncio.create_subprocess_exec

    async def _spy(*args, **kwargs):
        proc = await real(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", _spy)
    return started


async def _seed_attachment_job(adb, uid: str, storage_path: Path) -> tuple[Attachment, MediaJob]:
    att = Attachment(
        uid=uid,
        filename="v.mp4",
        mime="video/mp4",
        size=1,
        status=AttachmentStatus.processing,
        storage_path=str(storage_path),
        duration=1.0,  # set → process_slow never shells out to ffprobe
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued)
    adb.add(job)
    await adb.flush()
    return att, job


def _seed_slow_row(storage_path: str) -> tuple[int, int]:
    """Seed a committed attachment + queued slow job the worker's own session can claim."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, filename, mime, size, status, storage_path, duration) "
                "VALUES (:u,'v.mp4','video/mp4',1,'processing',:sp,1.0) RETURNING id"
            ),
            {"u": secrets.token_urlsafe(8), "sp": storage_path},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, 'slow', 'queued', 0, 0) RETURNING id"
            ),
            {"a": aid},
        ).scalar_one()
    eng.dispose()
    return aid, jid


def _cleanup(*aids: int) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            # media_jobs has no FK to attachments since MEMNG-41 (an ON DELETE CASCADE would make
            # every delete wait on a running job's row lock), so the seeded job rows are this
            # helper's to remove — otherwise they outlive the test as orphans.
            c.execute(text("DELETE FROM media_jobs WHERE attachment_id=:a"), {"a": aid})
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


def _spawn_zombie() -> "subprocess.Popen[bytes]":
    """A group whose ONLY member is a zombie, built the way production gets one.

    The child exits at once and nothing reaps it: it is a plain Popen (never wait()ed until the
    test's own teardown), so asyncio's child watcher never sees it — the same situation as the
    container, where the worker runs as PID 1, adopts the orphans it killed and does not reap them.
    start_new_session makes the child its own group leader, so pgid == pid.
    """
    zombie = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
    assert _await_state(zombie.pid, "Z"), "test setup: child never became a zombie"
    return zombie


def test_group_probe_reads_a_zombie_member_as_gone():
    # A killpg(pgid, 0) probe counts a zombie as a live member, which would keep the stopper
    # escalating through both grace periods on every timed-out job. A zombie can never react to a
    # signal and holds nothing open, so the probe must read this group as GONE.
    zombie = _spawn_zombie()
    try:
        assert media._group_alive(os.getpgid(zombie.pid)) is False
    finally:
        zombie.wait()  # the test owns this child — reap it here, nowhere in production code


def test_group_probe_reads_a_live_member_as_alive():
    live = subprocess.Popen(["sleep", str(HANG_SECONDS)], start_new_session=True)
    try:
        assert media._group_alive(os.getpgid(live.pid)) is True
    finally:
        live.kill()
        live.wait()


async def test_stop_transcode_settles_on_a_zombie_only_group():
    # A group whose only member is a zombie is DONE: nothing left to signal, nothing left holding
    # the temp file. The stopper must settle immediately and stay silent — logging
    # transcode_group_unreapable here would fire on every timed-out job in production.
    zombie = _spawn_zombie()
    try:
        leader = cast(asyncio.subprocess.Process, _FakeLeader(zombie.pid))
        with capture_logs() as logs:
            await media._stop_transcode(os.getpgid(zombie.pid), leader)
        assert [entry for entry in logs if entry.get("event") == "transcode_group_unreapable"] == []
    finally:
        zombie.wait()


async def test_hung_transcode_is_killed_at_the_ceiling(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 1)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _hang_cmd)
    started = _spy_on_subprocess(monkeypatch)
    att, job = await _seed_attachment_job(adb, "hang1", tmp_path / "originals" / "v.mp4")

    with pytest.raises(RuntimeError, match="slow-lane transcode timed out after 1s"):
        await media.process_job(adb, job)

    tmp = tmp_path / "assets" / "web" / "hang1.tmp.mp4"
    assert not tmp.exists()  # the partial is removed, not left to look like a finished tier
    assert not (tmp_path / "assets" / "web" / "hang1.mp4").exists()  # never published
    assert att.status is AttachmentStatus.processing  # the WORKER marks it failed, not this layer

    assert len(started) == 1
    assert started[0].returncode is not None  # actually reaped — no zombie holding the slot
    child_pid = int(Path(f"{tmp}.child").read_text().strip())
    assert _wait_gone(child_pid), f"grandchild {child_pid} survived the kill (group not signalled)"


async def test_descendant_outliving_the_leader_is_killed(adb, monkeypatch, tmp_path):
    # The leader exits at once; its descendant hangs ~forever holding stdout, so the pump never
    # sees EOF and the ceiling fires with proc.returncode ALREADY SET. Cleanup keyed on the leader
    # would skip the sweep here and leak the descendant — the sweep is keyed on the GROUP.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 1)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _orphan_cmd)
    started = _spy_on_subprocess(monkeypatch)
    att, job = await _seed_attachment_job(adb, "orphan1", tmp_path / "originals" / "v.mp4")

    with capture_logs() as logs:
        with pytest.raises(RuntimeError, match="slow-lane transcode timed out after 1s"):
            await media.process_job(adb, job)

    # Regression pin: the sweep must SETTLE. A probe that counts zombies (killpg) keeps escalating
    # and logs this on every timed-out job once the worker runs as PID 1 and adopts the orphans.
    assert [entry for entry in logs if entry.get("event") == "transcode_group_unreapable"] == []
    tmp = tmp_path / "assets" / "web" / "orphan1.tmp.mp4"
    assert not tmp.exists()
    assert started[0].returncode is not None  # leader exited on its own and was reaped
    child_pid = int(Path(f"{tmp}.child").read_text().strip())
    assert _wait_gone(child_pid), f"descendant {child_pid} outlived the leader and was not swept"


def test_worker_marks_a_hung_transcode_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 1)
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)  # terminal on the first failure
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _hang_cmd)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off (dev host)
    src = tmp_path / "originals" / "v.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not really a video")
    aid, jid = _seed_slow_row(str(src))
    tally = (
        "SELECT coalesce(sum(count), 0) FROM media_job_failure_tallies "
        "WHERE lane = 'transcode' AND reason = 'timeout'"
    )
    try:
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            timeouts_before = c.execute(text(tally)).scalar_one()
        eng.dispose()

        async def _go():
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"  # terminal attempt, worker survived

        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            state, error = c.execute(
                text("SELECT state, error FROM media_jobs WHERE id=:j"), {"j": jid}
            ).one()
            status = c.execute(
                text("SELECT status FROM attachments WHERE id=:a"), {"a": aid}
            ).scalar_one()
        eng.dispose()
        assert state == "failed"
        assert error is not None and "slow-lane transcode timed out after 1s" in error
        assert status == "failed"  # terminal + UI-visible, never a silently stuck job
        # ...and published as what it was: a transcode-consumer timeout (MEMNG-1046).
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            assert c.execute(text(tally)).scalar_one() - timeouts_before == 1
        eng.dispose()
    finally:
        _cleanup(aid)


async def test_quick_transcode_is_unaffected_by_the_ceiling(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 30)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _quick_cmd)
    att, job = await _seed_attachment_job(adb, "quick1", tmp_path / "originals" / "v.mp4")

    await media.process_job(adb, job)

    web = tmp_path / "assets" / "web" / "quick1.mp4"
    assert web.read_bytes() == b"ok"  # tmp renamed into place, output intact
    assert not (tmp_path / "assets" / "web" / "quick1.tmp.mp4").exists()
    assert att.status is AttachmentStatus.ready and job.progress_pct == 100
