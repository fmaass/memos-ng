import asyncio
import secrets
import shutil
import subprocess
import threading
import time

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
    src = tmp_path / "originals" / "p.heic"
    src.parent.mkdir(parents=True, exist_ok=True)
    pillow_heif.from_pillow(Image.new("RGB", (120, 90), (40, 160, 90))).save(str(src))

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


async def test_the_heavy_image_decode_does_not_freeze_the_event_loop(adb, monkeypatch, tmp_path):
    # The worker runs two consumers plus the supervisor on ONE loop, and a HEIC derive is claimed
    # by the LIGHT one: held on the loop thread, its libheif decode freezes the transcode
    # consumer's progress writes and the supervisor's heartbeat for its whole duration — seconds
    # of full-core CPU per photo, back to back through an import. What is asserted is therefore
    # the loop STILL RUNNING while the decode is in the middle of blocking, not merely finishing
    # afterwards.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = Attachment(
        uid="heic-offloop",
        filename="p.heic",
        mime="image/heic",
        size=1,
        status=AttachmentStatus.processing,
        storage_path=str(tmp_path / "p.heic"),
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued)
    adb.add(job)
    await adb.flush()

    block_s = 0.4
    loop_thread = threading.get_ident()
    decode_threads: list[int] = []
    began: list[float] = []
    finished: list[float] = []

    def _slow_decode(attachment) -> dict:
        # A libheif decode's stand-in: a blocking sleep that offers the loop no yield point,
        # exactly like the real decode + derivation.
        decode_threads.append(threading.get_ident())
        began.append(time.monotonic())
        time.sleep(block_s)
        finished.append(time.monotonic())
        return {"width": 120, "height": 90, "blurhash": "L00000"}

    monkeypatch.setattr(media, "process_image", _slow_decode)

    ticks: list[float] = []

    async def _ticker() -> None:
        # Bounded by the stub's own duration so a regression fails on the assertions below instead
        # of hanging the suite.
        deadline = time.monotonic() + block_s * 5
        while not finished and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            ticks.append(time.monotonic())

    await asyncio.gather(media.process_job(adb, job), _ticker())

    during = [t for t in ticks if began[0] < t < finished[0]]
    assert len(during) >= 3, f"the loop ran {len(during)} times while the decode blocked"
    assert decode_threads and decode_threads[0] != loop_thread  # ran on a worker thread
    assert att.status is AttachmentStatus.ready and att.blurhash == "L00000"  # ...and published


def test_progress_parser():
    assert media.parse_progress("out_time_us=500000\n", 1.0) == 50
    assert media.parse_progress("out_time_us=1000000", 1.0) == 100
    assert media.parse_progress("frame=10", 1.0) is None  # not a time line
    assert media.parse_progress("out_time_us=500000", None) is None  # unknown duration


async def test_thermal_guard_defers(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # Above the threshold, whatever it is set to. The literal that stood here (80 °C, hot only
    # while the limit was 70) tied this case to one host's number, and it broke loudly the moment
    # the limit moved — which is the cheapest kind of breakage, but avoidable.
    monkeypatch.setattr(media, "read_temp_millideg", lambda: (settings.THERMAL_MAX_C + 10) * 1000)
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
