import asyncio
import secrets
import shutil
import subprocess

import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import media
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def _make_video(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x120:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )


def _codec(path) -> str:
    return subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _seed_slow(storage_path, duration=1.0):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, filename, mime, size, status, storage_path, duration) "
                "VALUES (:u,'v.mp4','video/mp4',1,'processing',:sp,:d) RETURNING id"
            ),
            {"u": secrets.token_urlsafe(8), "sp": storage_path, "d": duration},
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


def _cleanup(*aids):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


async def test_slow_transcode_produces_h264(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    orig = tmp_path / "originals" / "v.mp4"
    _make_video(orig)
    att = Attachment(
        uid="vid1",
        filename="v.mp4",
        mime="video/mp4",
        size=orig.stat().st_size,
        status=AttachmentStatus.processing,
        storage_path=str(orig),
        duration=1.0,
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued)
    adb.add(job)
    await adb.flush()

    await media.process_job(adb, job)

    web = tmp_path / "assets" / "web" / "vid1.mp4"
    assert web.exists()
    assert _codec(web) == "h264"
    assert att.status is AttachmentStatus.ready and job.progress_pct == 100


async def test_slow_lane_decodes_heic_image(adb, monkeypatch, tmp_path):
    # A HEIC image job on the slow lane is decoded into tiers (on Merkur), not transcoded.
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    pillow_heif.register_heif_opener()
    src = tmp_path / "originals" / "p.heic"
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 90), (40, 160, 90)).save(str(src), format="HEIF")

    att = Attachment(
        uid="heic1",
        filename="p.heic",
        mime="image/heic",
        size=src.stat().st_size,
        status=AttachmentStatus.processing,
        storage_path=str(src),
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued)
    adb.add(job)
    await adb.flush()

    await media.process_job(adb, job)

    assert (tmp_path / "assets" / "thumbs" / "heic1.jpg").exists()
    assert (tmp_path / "assets" / "mobile" / "heic1.jpg").exists()
    assert att.status is AttachmentStatus.ready
    assert att.width == 120 and att.height == 90 and att.blurhash


def test_progress_parser():
    assert media.parse_progress("out_time_us=500000\n", 1.0) == 50
    assert media.parse_progress("out_time_us=1000000", 1.0) == 100
    assert media.parse_progress("frame=10", 1.0) is None  # not a time line
    assert media.parse_progress("out_time_us=500000", None) is None  # unknown duration


async def test_thermal_guard_defers(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "read_temp_millideg", lambda: 80_000)  # 80°C
    att = Attachment(
        uid="hot1",
        filename="v.mp4",
        mime="video/mp4",
        size=1,
        status=AttachmentStatus.processing,
        storage_path=str(tmp_path / "x.mp4"),
        duration=1.0,
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued)
    adb.add(job)
    await adb.flush()
    with pytest.raises(media.ThermalDefer):
        await media.process_job(adb, job)  # too hot → defer, no transcode


def test_retry_then_terminal_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid, jid = _seed_slow(
        str(tmp_path / "missing.mp4")
    )  # missing original → ffprobe/transcode fails
    try:

        async def _go():
            async with async_session() as db:
                return await worker.run_once(db)

        results = [asyncio.run(_go()) for _ in range(settings.MEDIA_MAX_ATTEMPTS)]
        assert results[:-1] == ["deferred"] * (settings.MEDIA_MAX_ATTEMPTS - 1)  # retried
        assert results[-1] == "processed"  # terminal attempt
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            jstate, attempts = c.execute(
                text("SELECT state, attempts FROM media_jobs WHERE id=:j"), {"j": jid}
            ).one()
            astatus = c.execute(
                text("SELECT status FROM attachments WHERE id=:a"), {"a": aid}
            ).scalar_one()
        eng.dispose()
        assert jstate == "failed" and attempts == settings.MEDIA_MAX_ATTEMPTS
        assert astatus == "failed"  # terminal + visible, never silent
    finally:
        _cleanup(aid)
