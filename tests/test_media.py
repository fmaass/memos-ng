import asyncio
import secrets
import time

import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import media
from PIL import Image
from sqlalchemy import create_engine, select, text
from structlog.testing import capture_logs

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


def test_poster_seek_skips_intro_and_clamps():
    from app.services.media import poster_seek_seconds

    assert poster_seek_seconds(None) == 0.0  # unknown duration → from start
    assert poster_seek_seconds(1.0) == 0.0  # very short clip → from start
    assert poster_seek_seconds(100.0) == 12.0  # ~12% in
    # short clips clamp up to the 0.5s floor (3s → 0.12*3 = 0.36, raised to 0.5)
    assert poster_seek_seconds(3.0) == 0.5


def test_poster_cmd_uses_thumbnail_filter():
    from app.services.media import poster_ffmpeg_cmd

    cmd = poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0)
    assert "thumbnail=100" in cmd  # representative-frame heuristic, not the first frame
    assert "-ss" in cmd and "12.00" in cmd  # seeks past the intro
    assert cmd[-1] == "/out.jpg" and "/in.mov" in cmd


# --- MEMNG-39: one poisoned job must never crash-loop the worker ---


def test_sanitize_error_removes_what_postgres_refuses_and_bounds():
    # The NUL byte is what Postgres rejects in a text column; a lone surrogate is what the
    # driver cannot even UTF-8 encode. Both must leave, the readable remainder must stay.
    assert worker.sanitize_error("bad\x00header\x00") == "badheader"
    cleaned = worker.sanitize_error("path \udcff broke")
    assert "\udcff" not in cleaned and cleaned.encode("utf-8")  # now encodable at all
    assert "path" in cleaned and "broke" in cleaned
    assert len(worker.sanitize_error("x" * 5000)) == 500  # column bound kept
    # Escaping happens BEFORE truncation, so an all-surrogate message still ends up bounded.
    assert len(worker.sanitize_error("\udcff" * 5000)) == 500


async def test_poisoned_job_recorded_failed_and_worker_keeps_draining(
    monkeypatch, tmp_path, db_engine
):
    # AC1 + AC2: a job whose processing raises an arbitrary exception — carrying a NUL byte
    # that Postgres refuses in text — is RECORDED as failed, and the worker goes on to drain
    # the next job. Before MEMNG-39 the NUL made the failure record's own commit raise out of
    # run_once and kill the process, which restarted onto the same row: a crash-loop.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)  # terminal on the first failure
    poison_aid, poison_jid = _seed("image/png", str(tmp_path / "poison.png"))
    good_aid, good_jid = _seed("image/png", str(tmp_path / "good.png"))

    async def _process(db, job):
        if job.id == poison_jid:
            raise RuntimeError("pyvips: bad header\x00\x00 (NUL out of a C string)")
        # The good job's processing is a dependency here, not the unit under test: what is
        # being proven is that the worker still gets to it.

    monkeypatch.setattr(media, "process_job", _process)
    try:
        # Bounded drain — never an unbounded loop in a test; stop as soon as the queue empties.
        statuses = []
        for _ in range(6):
            statuses.append(await worker.run_media_drain())
            if statuses[-1] == "idle":
                break
        assert "error" not in statuses  # no exception ever escaped run_once into the loop
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            p_state, p_error, p_attempts = c.execute(
                text("SELECT state, error, attempts FROM media_jobs WHERE id=:j"), {"j": poison_jid}
            ).one()
            g_state = c.execute(
                text("SELECT state FROM media_jobs WHERE id=:j"), {"j": good_jid}
            ).scalar_one()
            p_att_status = c.execute(
                text("SELECT status FROM attachments WHERE id=:a"), {"a": poison_aid}
            ).scalar_one()
        eng.dispose()
        assert p_state == "failed" and p_attempts == 1  # AC1: the failure is on the record
        assert p_error is not None and "\x00" not in p_error  # AC2: the NUL never reached PG
        assert "pyvips: bad header" in p_error  # ...and the message stayed diagnostic
        assert p_att_status == "failed"  # the attachment is terminal + UI-visible
        assert g_state == "done"  # the worker kept draining past the poisoned job
    finally:
        _cleanup(poison_aid, good_aid)


async def test_run_media_drain_isolates_and_logs_an_escaping_error(monkeypatch):
    # The loop guard: anything raising OUTSIDE run_once's own handler (claim_one, the failure
    # record's commit, the session context manager's exit) must be logged and converted to a
    # non-'processed' status, never propagated into run()'s while-loop.
    async def boom(db):
        raise RuntimeError("claim exploded")

    monkeypatch.setattr(worker, "run_once", boom)
    with capture_logs() as caps:
        assert await worker.run_media_drain() == "error"  # backs off, keeps running
    assert any(c["event"] == "media.drain_error" and c.get("exc_info") for c in caps)


async def test_run_media_drain_does_not_swallow_cancellation(monkeypatch):
    # Shutdown must stay clean: CancelledError/KeyboardInterrupt are BaseExceptions and the
    # guard catches Exception only, so a stopping worker still stops.
    async def cancel(db):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", cancel)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_media_drain()


async def test_failure_record_commit_error_is_recovered_not_raised(
    monkeypatch, tmp_path, db_engine
):
    # Second layer of the same defence: the failure record's own commit fails (dead connection,
    # or a session poisoned by the flush inside process_job). run_once must roll back, log and
    # report, not raise. Read back the AUTHORITATIVE row: the rollback also undoes claim_one's
    # claim — claim and outcome are one transaction — so the job returns to `queued` and the
    # ordinary claim picks it up again. Nothing is left stranded in `processing`.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid, jid = _seed("image/png", str(tmp_path / "x.png"))

    async def boom(db, job):
        raise RuntimeError("processing died")

    monkeypatch.setattr(media, "process_job", boom)
    try:
        async with async_session() as db:

            async def dead_commit():
                raise RuntimeError("connection reset by peer")

            monkeypatch.setattr(db, "commit", dead_commit)
            with capture_logs() as caps:
                assert await worker.run_once(db) == "deferred"  # recovered, not raised
        assert any(c["event"] == "media_job_failure_record_error" for c in caps)
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            state, attempts = c.execute(
                text("SELECT state, attempts FROM media_jobs WHERE id=:j"), {"j": jid}
            ).one()
        eng.dispose()
        assert state == "queued" and attempts == 0  # re-claimable; no durable `processing`
    finally:
        _cleanup(aid)


def test_worker_loop_wires_the_media_drain():
    # Wiring pin (like the frame-share recovery's): a guard the loop does not call guards
    # nothing. The media branch must go through run_media_drain, not a bare session + run_once.
    import inspect

    _, sep, loop = inspect.getsource(worker.run).partition("while True:")
    assert sep, "worker.run no longer has the drain loop this pin anchors on"
    assert "run_media_drain()" in loop
    assert "run_once(" not in loop  # never called unguarded from the loop
