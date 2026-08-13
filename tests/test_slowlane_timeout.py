"""Slow-lane transcode timeout (MEMNG-38).

No real ffmpeg anywhere in this module: the transcode command is substituted with a shell stub
that hangs (or finishes instantly), so the real subprocess/timeout/kill machinery is exercised
without depending on ffmpeg actually wedging. The injected ceiling keeps every test ~1s.
"""

import asyncio
import secrets
import time
from pathlib import Path

import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import media
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

HANG_SECONDS = 30  # long enough to outlive the injected ceiling, short enough for a mutation run


def _hang_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv: write a partial output file, spawn a CHILD that hangs, then wait on it.

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


def _quick_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv for a transcode that finishes immediately with a valid (tiny) output."""
    return ["sh", "-c", 'printf ok > "$1"', "sh", dest]


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
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


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
    try:

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
