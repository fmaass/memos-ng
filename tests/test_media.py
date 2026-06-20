import asyncio
import secrets
import time

from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import media
from PIL import Image
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL


def _make_png(path, size=(1200, 900)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 80, 200)).save(path, "PNG")


def _seed(mime, storage_path, status="processing", lane="fast"):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, filename, mime, size, status, storage_path) "
                "VALUES (:u,'x',:m,1,CAST(:s AS attachment_status),:sp) RETURNING id"
            ),
            {"u": secrets.token_urlsafe(8), "m": mime, "s": status, "sp": storage_path},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                "VALUES (:a, CAST(:l AS job_lane), CAST('queued' AS job_state), 0, 0) RETURNING id"
            ),
            {"a": aid, "l": lane},
        ).scalar_one()
    eng.dispose()
    return aid, jid


def _cleanup(*aids):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})  # cascades jobs
    eng.dispose()


async def test_fast_lane_image_produces_tiers(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    orig = tmp_path / "originals" / "2024" / "01" / "x.png"
    _make_png(orig)
    att = Attachment(
        uid="img1",
        filename="x.png",
        mime="image/png",
        size=orig.stat().st_size,
        status=AttachmentStatus.processing,
        storage_path=str(orig),
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=JobLane.fast, state=JobState.queued)
    adb.add(job)
    await adb.flush()

    await media.process_job(adb, job)

    assert (tmp_path / "assets" / "thumbs" / "img1.jpg").exists()
    assert (tmp_path / "assets" / "mobile" / "img1.jpg").exists()
    assert att.width == 1200 and att.height == 900
    assert att.blurhash and att.status is AttachmentStatus.ready  # image done after fast lane


async def test_video_fast_lane_enqueues_slow_after_poster(adb, monkeypatch, tmp_path):
    # The fast lane must enqueue the slow (transcode) job ONLY after it finishes, so the
    # two never race a status overwrite under concurrent workers. ffmpeg is stubbed out.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        media,
        "process_video_poster",
        lambda att: {"width": 1920, "height": 1080, "duration": 4.0, "blurhash": "LKO2"},
    )
    att = Attachment(
        uid="vid1",
        filename="v.mov",
        mime="video/quicktime",
        size=1,
        status=AttachmentStatus.pending,
        storage_path=str(tmp_path / "v.mov"),
    )
    adb.add(att)
    await adb.flush()
    fast = MediaJob(attachment_id=att.id, lane=JobLane.fast, state=JobState.queued)
    adb.add(fast)
    await adb.flush()

    await media.process_job(adb, fast)

    assert att.status is AttachmentStatus.processing  # video not done after fast lane
    lanes = (
        (await adb.execute(select(MediaJob.lane).where(MediaJob.attachment_id == att.id)))
        .scalars()
        .all()
    )
    assert JobLane.slow in lanes  # slow transcode now queued, by the fast lane


def test_missing_original_retries_then_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid, jid = _seed("image/png", str(tmp_path / "does-not-exist.png"))
    try:

        async def _go():
            async with async_session() as db:
                return await worker.run_once(db)

        # Fails on each attempt; retries (deferred) until MEDIA_MAX_ATTEMPTS → terminal failed.
        for _ in range(settings.MEDIA_MAX_ATTEMPTS):
            asyncio.run(_go())
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            state, attempts = c.execute(
                text("SELECT state, attempts FROM media_jobs WHERE id=:j"), {"j": jid}
            ).one()
        eng.dispose()
        assert state == "failed" and attempts == settings.MEDIA_MAX_ATTEMPTS  # worker survived
    finally:
        _cleanup(aid)


def test_skip_locked_no_double_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    a1, j1 = _seed("image/png", "p1")
    a2, j2 = _seed("image/png", "p2")
    try:

        async def _go():
            async with async_session() as db_a, async_session() as db_b:
                job_a = await worker.claim_one(db_a)  # locks the lower id
                job_b = await worker.claim_one(db_b)  # must SKIP the locked row
                return job_a.id, job_b.id

        ida, idb = asyncio.run(_go())
        assert ida != idb and {ida, idb} == {j1, j2}  # no double-claim
    finally:
        _cleanup(a1, a2)


def test_heartbeat_written(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    worker.beat()
    p = worker.heartbeat_path()
    assert p.exists() and time.time() - float(p.read_text()) < 5


def test_worker_defers_everything_when_too_hot(monkeypatch, tmp_path):
    # Whole-worker thermal gate: when too hot, even a fast-lane image job is NOT processed.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _make_png(tmp_path / "hot.png")
    aid, jid = _seed("image/png", str(tmp_path / "hot.png"))
    monkeypatch.setattr(media, "too_hot", lambda: True)
    try:

        async def _go():
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "deferred"
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            state = c.execute(
                text("SELECT state FROM media_jobs WHERE id=:j"), {"j": jid}
            ).scalar_one()
        eng.dispose()
        assert state == "queued"  # nothing claimed/processed while hot
    finally:
        _cleanup(aid)
