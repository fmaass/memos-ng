import asyncio
import json
import secrets
import shutil
import subprocess
import sys
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


# --- MEMNG-226: an HDR source is tone-mapped before its frame becomes a JPEG ---


@pytest.mark.parametrize("transfer", ["arib-std-b67", "smpte2084"])
def test_is_hdr_is_true_for_the_two_transfers_a_jpeg_cannot_carry(transfer):
    assert media.is_hdr(transfer) is True


@pytest.mark.parametrize(
    "transfer",
    [
        "bt709",  # the 111 SDR clips in the library
        "unknown",  # ffprobe's spelling for a stream that states nothing
        "bt2020-10",  # wide-gamut SDR: wide primaries are not an HDR transfer
        "smpte170m",
        "",
        None,  # no video stream tags at all
    ],
)
def test_is_hdr_is_false_for_everything_else(transfer):
    assert media.is_hdr(transfer) is False


def test_stream_colour_answers_four_keys_whatever_the_stream_says():
    # Absent and non-string readings both have to come back as None, so no caller has to tell
    # "the stream said nothing" apart from "ffprobe omitted the key".
    assert media.stream_colour({}) == {
        "color_transfer": None,
        "color_primaries": None,
        "color_space": None,
        "pix_fmt": None,
    }
    assert media.stream_colour({"color_transfer": 18, "pix_fmt": "yuv420p"}) == {
        "color_transfer": None,
        "color_primaries": None,
        "color_space": None,
        "pix_fmt": "yuv420p",
    }


@pytest.mark.parametrize(
    "colour",
    [
        None,  # no probe at all
        {},  # a probe that returned nothing about colour
        {"color_transfer": None},
        {"color_transfer": "bt709", "color_primaries": "bt709", "color_space": "bt709"},
        {"color_transfer": "unknown", "color_primaries": "unknown", "color_space": "unknown"},
        {"color_primaries": "bt2020", "color_space": "bt2020nc"},  # wide gamut, SDR curve
    ],
)
def test_poster_filter_for_a_non_hdr_source_is_the_frame_heuristic_alone(colour):
    assert media.poster_video_filter(colour) == "thumbnail=100"


def test_poster_filter_tone_maps_an_hlg_source_with_the_streams_own_tags():
    chain = media.poster_video_filter(
        {
            "color_transfer": "arib-std-b67",
            "color_primaries": "bt2020",
            "color_space": "bt2020nc",
            "pix_fmt": "yuv420p10le",
        }
    )
    # The frame is chosen BEFORE the conversion, so the chain is paid once, not 100 times.
    assert chain.startswith("thumbnail=100,")
    assert "zscale=tin=arib-std-b67:pin=bt2020:min=bt2020nc:t=linear:npl=100" in chain
    assert "format=gbrpf32le" in chain  # tonemap reads linear float RGB and nothing else
    assert "tonemap=tonemap=hable:desat=0" in chain
    assert chain.endswith("zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def test_poster_filter_tone_maps_a_pq_source():
    chain = media.poster_video_filter(
        {"color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc"}
    )
    assert "zscale=tin=smpte2084:pin=bt2020:min=bt2020nc:t=linear:npl=100" in chain


def test_poster_filter_falls_back_to_the_bt2020_pair_when_only_the_transfer_is_stated():
    # What a stream that carries the HLG curve and leaves the rest unsignalled must get: zimg can
    # build no path from an unspecified primary or matrix, and bt2020/bt2020nc is what every
    # HLG camera writes when it writes them at all.
    for colour in (
        {"color_transfer": "arib-std-b67"},
        {"color_transfer": "arib-std-b67", "color_primaries": "unknown", "color_space": "unknown"},
    ):
        chain = media.poster_video_filter(colour)
        assert "pin=bt2020:min=bt2020nc" in chain
        assert "unknown" not in chain


def test_poster_cmd_without_colour_is_the_command_this_repo_has_always_run():
    # Ground truth, spelled out: every SDR video in the library must keep the byte-identical argv,
    # so the tone-map cannot change a single one of the 111 bt709 posters.
    assert media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0) == [
        "ffmpeg",
        "-y",
        "-ss",
        "12.00",
        "-i",
        "/in.mov",
        "-map",
        "0:V:0",
        # A derived artifact carries no metadata: a geotagged source used to publish its
        # coordinates through this lane (tests/test_location_privacy.py).
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vf",
        "thumbnail=100",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "/out.jpg",
    ]


def test_poster_cmd_carries_the_tone_map_chain_only_for_an_hdr_colour():
    hlg = {
        "color_transfer": "arib-std-b67",
        "color_primaries": "bt2020",
        "color_space": "bt2020nc",
    }
    hdr_cmd = media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0, colour=hlg)
    sdr = {"color_transfer": "bt709"}
    sdr_cmd = media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0, colour=sdr)

    assert "tonemap" in hdr_cmd[hdr_cmd.index("-vf") + 1]
    # An SDR probe reaches the same argv as no probe at all.
    assert sdr_cmd == media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0)
    # ...and the filter is the ONLY thing the HDR command changes: same seek, same stream map,
    # same encoder settings, same output path.
    swapped = list(hdr_cmd)
    swapped[swapped.index("-vf") + 1] = "thumbnail=100"
    assert swapped == sdr_cmd


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


def _exception_snapshot(seen: list[BaseException | None]):
    """A capturing processor that records the exception IN FLIGHT as each entry is logged.

    `logger.exception` under capture_logs records `exc_info=True` — a flag, not the exception — so
    an assertion on the captured entry alone cannot say WHICH exception was isolated, and any
    other error raised on the way in (a call-shape TypeError, say) would satisfy it just as well.
    """

    def snapshot(logger, method_name, event_dict):
        seen.append(sys.exc_info()[1])
        return event_dict

    return snapshot


async def test_run_media_drain_isolates_and_logs_an_escaping_error(monkeypatch):
    # The loop guard: anything raising OUTSIDE run_once's own handler (claim_one, the failure
    # record's commit, the session context manager's exit) must be logged and converted to a
    # non-'processed' status, never propagated into run()'s while-loop.
    async def boom(db):
        raise RuntimeError("claim exploded")

    monkeypatch.setattr(worker, "run_once", boom)
    active: list[BaseException | None] = []
    with capture_logs(processors=[_exception_snapshot(active)]) as caps:
        assert await worker.run_media_drain() == "error"  # backs off, keeps running
    assert any(c["event"] == "media.drain_error" and c.get("exc_info") for c in caps)
    isolated = [
        exc
        for entry, exc in zip(caps, active, strict=True)
        if entry["event"] == "media.drain_error"
    ]
    # Name the exception that was isolated, not merely that one was: this is the drain's contract.
    assert len(isolated) == 1
    assert isinstance(isolated[0], RuntimeError) and str(isolated[0]) == "claim exploded"


async def test_run_media_drain_does_not_swallow_cancellation(monkeypatch):
    # Shutdown must stay clean: CancelledError/KeyboardInterrupt are BaseExceptions and the
    # guard catches Exception only, so a stopping worker still stops.
    async def cancel(db):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", cancel)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_media_drain()


async def test_failure_record_commit_error_leaves_job_for_the_stale_sweep(
    monkeypatch, tmp_path, db_engine
):
    # MEMNG-90: run_once commits the CLAIM before processing, then processing fails, then the
    # failure record's own commit fails too (a dead connection / poisoned session). run_once must
    # roll back, log, and report 'deferred' — never raise — and leave the row durable at
    # `processing` (attempts still 0) for recover_stale_media_claims to recover. Because claim_one
    # is queued-only, that stuck row is SKIPPED, not re-claimed under itself, so it never blocks the
    # queue head. (This replaces the pre-MEMNG-90 behaviour where a rollback returned the job to
    # `queued`: the claim was uncommitted then, so a rollback undid it.)
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid, jid = _seed("image/png", str(tmp_path / "x.png"))

    async def boom(db, job):
        raise RuntimeError("processing died")

    monkeypatch.setattr(media, "process_job", boom)
    try:
        async with async_session() as db:
            real_commit = db.commit
            calls = {"n": 0}

            async def flaky_commit():
                calls["n"] += 1
                if calls["n"] == 1:
                    await real_commit()  # let the CLAIM commit land — `processing` is durable
                    return
                raise RuntimeError("connection reset by peer")  # the failure record cannot commit

            monkeypatch.setattr(db, "commit", flaky_commit)
            with capture_logs() as caps:
                assert await worker.run_once(db) == "deferred"  # recovered, not raised
        assert any(c["event"] == "media_job_failure_record_error" for c in caps)
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            state, attempts = c.execute(
                text("SELECT state, attempts FROM media_jobs WHERE id=:j"), {"j": jid}
            ).one()
        eng.dispose()
        assert state == "processing" and attempts == 0  # durable claim, awaits the stale sweep
    finally:
        _cleanup(aid)


def test_worker_loop_wires_the_media_drain():
    # Wiring pin (like the frame-share recovery's): a guard the loop does not call guards
    # nothing. The media branch must go through run_media_drain, not a bare session + run_once.
    # Moved from run() to consume_media with the per-class consumer split: the drain loop now
    # lives in the consumer, so a pin left on run() would go on passing with the media queue
    # drained by nobody — hence the second half, which pins the supervisor to SPAWN the consumers.
    import inspect

    _, sep, loop = inspect.getsource(worker.consume_media).partition("while True:")
    assert sep, "worker.consume_media no longer has the drain loop this pin anchors on"
    assert "run_media_drain(" in loop
    assert "run_once(" not in loop  # never called unguarded from the loop
    supervisor = inspect.getsource(worker.run)
    assert "consume_media(" in supervisor, "the supervisor must spawn the media consumers"
    assert "create_task(" in supervisor, "…as tasks, or nothing drains while the supervisor waits"


# --- video rotation (MEMNG-40 / #37) -------------------------------------------------------
# A portrait phone clip is stored LANDSCAPE plus a rotation, so the coded width/height describe
# the wrong shape. The fixtures below are the two payload shapes ffprobe really emits, captured
# from `ffprobe -show_streams` (7.1.5) against clips written with `-display_rotation`.

DISPLAY_MATRIX_ROWS = (
    "\n00000000:            0      -65536           0"
    "\n00000001:        65536           0           0"
    "\n00000002:            0           0  1073741824\n"
)


def _matrix(rotation):
    """A Display Matrix side-data entry. The `displaymatrix` rows are carried so the fixture is
    the real payload and not a hand-trimmed one; the parser reads `rotation`, which is the angle
    ffprobe derives from those rows (counter-clockwise, hence the negative values it emits)."""
    return {
        "side_data_type": "Display Matrix",
        "displaymatrix": DISPLAY_MATRIX_ROWS,
        "rotation": rotation,
    }


def _stream(width=320, height=240, *, side_data=None, tags=None):
    """One `streams[0]` entry, trimmed to the fields the parser reads."""
    stream = {"codec_type": "video", "width": width, "height": height}
    if side_data is not None:
        stream["side_data_list"] = side_data
    if tags is not None:
        stream["tags"] = tags
    return stream


@pytest.mark.parametrize(
    ("raw", "degrees"),
    [
        (0, 0),
        (90, 90),
        (180, 180),
        (270, 270),
        (-90, 270),  # what ffprobe actually emits for a 270° clip
        (-180, 180),  # ...and for a 180° one
        (-270, 90),
        (360, 0),
        (90.0, 90),  # the angle is a float in ffprobe's JSON, not always an int
        (-89.9998, 270),  # atan2 noise still has to land on a quarter turn
        ("90", 90),  # the legacy tag arrives as a string
        (45, 0),  # not a quarter turn: no swap can express it
        # float() swallows these; round() then raises (OverflowError / ValueError). A junk
        # rotation field must read as "no rotation", never take the whole probe down with it.
        (float("inf"), 0),
        (float("-inf"), 0),
        (float("nan"), 0),
        ("inf", 0),
        ("nan", 0),
        ("", 0),
        ("north", 0),
        (None, 0),
        ([90], 0),
    ],
)
def test_probe_rotation_normalizes_every_rotation_value(raw, degrees):
    assert media.probe_rotation(_stream(side_data=[_matrix(raw)])) == degrees
    assert media.probe_rotation(_stream(tags={"rotate": raw})) == degrees


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        # nothing to rotate by → the coded dimensions ARE the displayed ones
        (_stream(), (320, 240)),
        (_stream(side_data=[], tags={"language": "und"}), (320, 240)),
        # modern source: the display matrix
        (_stream(side_data=[_matrix(0)]), (320, 240)),
        (_stream(side_data=[_matrix(90)]), (240, 320)),
        (_stream(side_data=[_matrix(180)]), (320, 240)),
        (_stream(side_data=[_matrix(270)]), (240, 320)),
        (_stream(side_data=[_matrix(-90)]), (240, 320)),
        (_stream(side_data=[_matrix(-180)]), (320, 240)),
        # legacy source: the `rotate` stream tag
        (_stream(tags={"rotate": "0"}), (320, 240)),
        (_stream(tags={"rotate": "90"}), (240, 320)),
        (_stream(tags={"rotate": "180"}), (320, 240)),
        (_stream(tags={"rotate": "270"}), (240, 320)),
        # other side data present, none of it a rotation → the tag still decides
        (_stream(side_data=[{"side_data_type": "Stereo 3D"}], tags={"rotate": "90"}), (240, 320)),
        # unprobeable stream (ffprobe found no video stream): nothing to report either way
        ({}, (None, None)),
        ({"side_data_list": [_matrix(90)]}, (None, None)),
    ],
)
def test_displayed_size_reports_the_rotated_shape(stream, expected):
    assert media.displayed_size(stream) == expected


def test_display_matrix_wins_over_a_disagreeing_rotate_tag():
    # Both sources present and CONTRADICTING each other. ffmpeg resolves to the matrix and
    # autorotates the transcode by it, so the reported shape has to follow the matrix too —
    # picking the tag here would describe a shape the poster and web tier do not have.
    upright = _stream(side_data=[_matrix(0)], tags={"rotate": "90"})
    assert media.probe_rotation(upright) == 0
    assert media.displayed_size(upright) == (320, 240)

    rotated = _stream(side_data=[_matrix(90)], tags={"rotate": "0"})
    assert media.probe_rotation(rotated) == 90
    assert media.displayed_size(rotated) == (240, 320)


def _write_clip(path, rotation=None, size="320x240"):
    """A 1s clip, optionally stamped with a display matrix (stream-copied, so the frames
    stay landscape and only the rotation metadata differs from the control)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    src = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration=1:size={size}:rate=5",
        "-pix_fmt",
        "yuv420p",
    ]
    if rotation is None:
        subprocess.run(
            ["ffmpeg", "-y", *src, str(path)], capture_output=True, check=True, timeout=60
        )
        return
    base = path.with_name(f"base-{path.name}")
    subprocess.run(["ffmpeg", "-y", *src, str(base)], capture_output=True, check=True, timeout=60)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-display_rotation",
            str(rotation),
            "-i",
            str(base),
            "-c",
            "copy",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )


def _raw_side_data(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return json.loads(out)["streams"][0].get("side_data_list") or []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_ffprobe_reports_displayed_dims_for_a_real_rotated_clip(tmp_path):
    # AC1 end-to-end against real ffmpeg: a clip whose FRAMES are 320x240 but which carries a
    # 90° display matrix must probe as 240x320 — the shape a player shows and the shape the
    # autorotated poster/web tier are written in.
    rotated = tmp_path / "portrait.mp4"
    _write_clip(rotated, rotation=90)
    if not any("rotation" in sd for sd in _raw_side_data(rotated)):
        pytest.skip("this ffmpeg cannot stamp a display matrix (-display_rotation)")

    assert media._ffprobe(str(rotated))["width"] == 240
    assert media._ffprobe(str(rotated))["height"] == 320

    control = tmp_path / "landscape.mp4"  # same frames, no rotation → unswapped
    _write_clip(control)
    assert (media._ffprobe(str(control))["width"], media._ffprobe(str(control))["height"]) == (
        320,
        240,
    )


def _write_hlg_clip(path, size="320x240", duration=2):
    """A 10-bit clip TAGGED as HLG/BT.2020, the way a phone writes one.

    The tags are stamped with `setparams`, not with the encoder's `-color_trc/-color_primaries`
    options: on this ffmpeg (7.1.5) those are overwritten by the filtergraph's own frame
    properties, and the file comes out with `color_transfer` unset — which would make this
    fixture an SDR clip wearing an HDR name and the test below vacuous. `setparams` sets the
    properties on the frames themselves, so the encoder and the muxer carry them through.
    Nothing about the PIXELS changes, which is the point: the plain and the tone-mapped
    extraction below start from identical picture data and differ only by the filter chain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={duration}:size={size}:rate=5",
            "-vf",
            "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p10le",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_ffprobe_carries_the_colour_tags_of_a_real_clip(tmp_path):
    hlg = tmp_path / "hlg.mp4"
    _write_hlg_clip(hlg)
    probed = media._ffprobe(str(hlg))
    if probed["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")

    assert probed["color_primaries"] == "bt2020"
    assert probed["color_space"] == "bt2020nc"
    assert probed["pix_fmt"] == "yuv420p10le"
    assert media.is_hdr(probed["color_transfer"]) is True

    # The SDR control: an untagged clip must answer None for the tags it does not carry rather
    # than raising or inventing one, and must stay out of the tone-mapping branch.
    plain = tmp_path / "sdr.mp4"
    _write_clip(plain)
    sdr = media._ffprobe(str(plain))
    assert sdr["color_transfer"] is None
    assert media.is_hdr(sdr["color_transfer"]) is False
    assert media.poster_video_filter(sdr) == "thumbnail=100"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_a_real_hlg_clip_gets_a_tone_mapped_poster(tmp_path):
    # End-to-end proof that the chain is not just spelled but APPLIED: the same frame of the same
    # clip, extracted twice, once with the probe's colour and once without. Both commands must
    # succeed and produce the source's dimensions; the pictures must differ, which they can only
    # do because the tone-map ran.
    src = tmp_path / "hlg.mp4"
    _write_hlg_clip(src)
    probed = media._ffprobe(str(src))
    if probed["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")

    plain, toned = tmp_path / "plain.jpg", tmp_path / "toned.jpg"
    for dest, colour in ((plain, None), (toned, probed)):
        subprocess.run(
            media.poster_ffmpeg_cmd(str(src), str(dest), probed["duration"], colour=colour),
            capture_output=True,
            check=True,
            timeout=60,
        )

    assert plain.exists() and toned.exists()
    with Image.open(toned) as image:
        assert image.size == (probed["width"], probed["height"]) == (320, 240)
    assert toned.read_bytes() != plain.read_bytes()


# --- stream selection (MEMNG-40 micro-round) -----------------------------------------------
# The probe and BOTH ffmpeg lanes have to measure and render the same stream. ffmpeg's default
# selection takes the highest-RESOLUTION video stream, which is neither the first one nor the
# one a `v:0` probe measures — so the two disagreed on any multi-stream or cover-art file.


def _vstream(index, width, height, attached_pic=None):
    stream = {"index": index, "codec_type": "video", "width": width, "height": height}
    if attached_pic is not None:
        stream["disposition"] = {"attached_pic": attached_pic}
    return stream


def test_select_video_stream_skips_an_attached_picture():
    # v:0 IS the cover art here (a music file's album art is a 1-frame video stream). Measuring
    # it would report the artwork's shape for the clip sitting next to it.
    cover = _vstream(0, 64, 64, attached_pic=1)
    clip = _vstream(1, 320, 240, attached_pic=0)
    assert media.select_video_stream([cover, clip]) is clip
    assert media.displayed_size(media.select_video_stream([cover, clip])) == (320, 240)


def test_select_video_stream_takes_the_first_not_the_biggest():
    # The alignment invariant. ffmpeg's DEFAULT selection would take the 640x480 stream — that
    # skew is exactly what `-map 0:V:0` removes, so the probe must pick first-by-index like it.
    first = _vstream(0, 320, 240, attached_pic=0)
    bigger = _vstream(1, 640, 480, attached_pic=0)
    assert media.select_video_stream([first, bigger]) is first


def test_select_video_stream_treats_a_missing_disposition_as_not_attached():
    plain = _vstream(0, 320, 240)  # no `disposition` key at all, as ffmpeg reads a missing flag
    assert media.select_video_stream([plain]) is plain


@pytest.mark.parametrize(
    "streams",
    [
        [],
        [_vstream(0, 64, 64, attached_pic=1)],
        [None],
        [_vstream(0, 64, 64, attached_pic=1), None],
    ],
)
def test_select_video_stream_reports_nothing_renderable(streams):
    # The same answer ffmpeg gives: with no non-attached-pic video stream, `-map 0:V:0` matches
    # nothing and the command fails outright, so there are no dimensions to report either.
    assert media.select_video_stream(streams) == {}
    assert media.displayed_size(media.select_video_stream(streams)) == (None, None)


def test_probe_asks_for_every_video_stream():
    cmd = media.probe_cmd("/in.mov")
    # `v`, never `v:0` — the attached-picture exclusion cannot be expressed as an index.
    assert cmd[cmd.index("-select_streams") + 1] == "v"
    assert "-show_streams" in cmd  # the only form that carries side_data_list AND tags
    assert cmd[-1] == "/in.mov"


def test_both_ffmpeg_lanes_pin_the_probed_stream():
    poster = media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0)
    slow = media.slow_ffmpeg_cmd("/in.mov", "/out.mp4", 1)
    for cmd in (poster, slow):
        # directly after the input, and the same specifier select_video_stream mirrors
        i = cmd.index("-i")
        assert cmd[i + 2 : i + 4] == ["-map", media.VIDEO_STREAM_MAP] == ["-map", "0:V:0"]
    # Naming any -map turns ffmpeg's automatic selection OFF, so the audio has to be re-mapped
    # or every transcoded clip loses its sound.
    assert "0:a:0?" in slow
    # ...and the `?` is not decoration: the bare form is fatal on a source with no audio track,
    # which is most screen recordings and every lavfi fixture in this suite.
    assert "0:a:0" not in slow
    assert not any(a.startswith("0:a") for a in poster)  # a JPEG has no audio to map


@pytest.mark.parametrize("threads", [1, 4])
def test_ffmpeg_threads_bounds_the_decoder_as_well_as_the_encoder(threads):
    # ffmpeg options are positional: before `-i` they configure the INPUT (the decoder), after it
    # the OUTPUT (libx264). Measured 2026-09-27 on a 1080p120 clip, the output option alone left
    # the decoder on auto threads and the "one-thread" transcode peaked at 3.8 cores.
    cmd = media.slow_ffmpeg_cmd("/in.mov", "/out.mp4", threads)
    i = cmd.index("-i")
    assert cmd[i + 1] == "/in.mov"
    before = [cmd[k + 1] for k, a in enumerate(cmd[:i]) if a == "-threads"]
    after = [cmd[k + 1] for k, a in enumerate(cmd) if a == "-threads" and k > i]
    assert before == [str(threads)]
    assert after == [str(threads)]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_probe_and_transcode_agree_on_a_multi_stream_container(tmp_path):
    # End-to-end proof of the skew and its fix: a container whose FIRST video stream is the
    # smaller one. Unpinned, ffmpeg renders the 640x480 stream while the probe measured the
    # 320x240 one, and the stored dimensions describe a picture the web tier does not contain.
    small, big = tmp_path / "small.mp4", tmp_path / "big.mp4"
    _write_clip(small)
    _write_clip(big, size="640x480")
    both = tmp_path / "both.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(small), "-i", str(big), "-map", "0:v", "-map", "1:v"]
        + ["-c", "copy", str(both)],
        capture_output=True,
        check=True,
        timeout=60,
    )

    probed = media._ffprobe(str(both))
    assert (probed["width"], probed["height"]) == (320, 240)  # the first stream, not the biggest

    web = tmp_path / "web.mp4"
    subprocess.run(
        media.slow_ffmpeg_cmd(str(both), str(web), 1), capture_output=True, check=True, timeout=120
    )
    rendered = media._ffprobe(str(web))
    assert (rendered["width"], rendered["height"]) == (probed["width"], probed["height"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_slow_lane_keeps_audio_and_survives_a_silent_source(tmp_path):
    # The `-map 0:a:0?` half of the pair, both ways round: audio must still reach the web tier
    # (naming a -map disables ffmpeg's automatic audio selection), and a source with no audio
    # at all must still transcode (that is what the trailing `?` buys).
    def _codec_types(path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0"]
            + [str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        return sorted(out.split())

    with_audio = tmp_path / "av.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=5"]
        + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", str(with_audio)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    out_av = tmp_path / "av-web.mp4"
    subprocess.run(
        media.slow_ffmpeg_cmd(str(with_audio), str(out_av), 1),
        capture_output=True,
        check=True,
        timeout=120,
    )
    assert _codec_types(out_av) == ["audio", "video"]

    silent = tmp_path / "silent.mp4"
    _write_clip(silent)
    out_silent = tmp_path / "silent-web.mp4"
    subprocess.run(  # check=True: a bare `-map 0:a:0` exits 234 here
        media.slow_ffmpeg_cmd(str(silent), str(out_silent), 1),
        capture_output=True,
        check=True,
        timeout=120,
    )
    assert _codec_types(out_silent) == ["video"]


# --- MEMNG-90: durable-claim transaction lifetime + stale-claim recovery + healthcheck honesty ---


def _seed_processing(age_seconds, attempts=0):
    """A media_jobs row already in `processing`, with updated_at set `age_seconds` in the past — the
    shape a worker that committed its claim (MEMNG-90) then died mid-job leaves behind."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, filename, mime, size, status, storage_path) "
                "VALUES (:u,'x','image/png',1,CAST('processing' AS attachment_status),:sp) "
                "RETURNING id"
            ),
            {"u": secrets.token_urlsafe(8), "sp": "/tmp/x"},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs "
                "(attachment_id, lane, state, attempts, progress_pct, updated_at) VALUES "
                "(:a, CAST('fast' AS job_lane), CAST('processing' AS job_state), :at, 0, "
                "now() - (:age * interval '1 second')) RETURNING id"
            ),
            {"a": aid, "at": attempts, "age": age_seconds},
        ).scalar_one()
    eng.dispose()
    return aid, jid


def _cleanup_jobs(*jids):
    # media_jobs has no FK to attachments (MEMNG-41), so _cleanup does not remove them.
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for jid in jids:
            c.execute(text("DELETE FROM media_jobs WHERE id=:j"), {"j": jid})
    eng.dispose()


def _job_row(jid):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT state, attempts FROM media_jobs WHERE id=:j"), {"j": jid}
        ).one()
    eng.dispose()
    return row


def test_run_once_commits_the_claim_before_processing(monkeypatch, tmp_path, db_engine):
    # MEMNG-90: the claim (state=processing) must be COMMITTED before process_job runs, so no DB
    # transaction is held across the (up to an hour) transcode. Proven from a SEPARATE connection:
    # at the instant process_job is entered, the row already reads `processing`. (Old worker: the
    # claim was uncommitted, so another session read `queued` until the whole job committed.)
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid, jid = _seed("image/png", str(tmp_path / "x.png"))
    seen = {}

    async def _peek(db, job):
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            seen["state"] = c.execute(
                text("SELECT state FROM media_jobs WHERE id=:j"), {"j": jid}
            ).scalar_one()
        eng.dispose()
        att = (
            await db.execute(select(Attachment).where(Attachment.id == job.attachment_id))
        ).scalar_one()
        att.status = AttachmentStatus.ready  # a normal terminal write so run_once finishes clean
        await db.flush()

    monkeypatch.setattr(media, "process_job", _peek)
    try:

        async def _go():
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"
        assert seen["state"] == "processing"  # committed & visible to another session BEFORE work
        assert _job_row(jid)[0] == "done"
    finally:
        _cleanup(aid)
        _cleanup_jobs(jid)


def test_result_commits_when_processing_outlasts_idle_in_transaction_timeout(
    monkeypatch, tmp_path, db_engine
):
    # The MEMNG-90 incident, reproduced: a job whose processing outlasts the DB's
    # idle_in_transaction_session_timeout. With the claim committed and the result written in a
    # fresh transaction, no session sits idle-in-transaction across the work, so the result still
    # commits. (The old single-transaction worker held the claim's txn open across the transcode;
    # Postgres killed the connection at the timeout and the result write raised.)
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid, jid = _seed("image/png", str(tmp_path / "x.png"))

    async def _slow(db, job):
        await asyncio.sleep(2.5)  # longer than the 1 s idle timeout set below
        att = (
            await db.execute(select(Attachment).where(Attachment.id == job.attachment_id))
        ).scalar_one()
        att.status = AttachmentStatus.ready
        await db.flush()

    monkeypatch.setattr(media, "process_job", _slow)
    try:

        async def _go():
            async with async_session() as db:
                # The timeout has to be set on the connection that makes the claim, so this SET and
                # the claim share one transaction (the test engine is NullPool: a commit here would
                # close the connection and take the setting with it, and the test could no longer
                # fail). That transaction is therefore idle between its statements, and at 150 ms
                # a loaded host's scheduling gap alone got the connection killed before the worker
                # had done anything. 1 s leaves that gap room; the work outlasts it 2.5 to 1.
                await db.execute(text("SET idle_in_transaction_session_timeout = '1s'"))
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"
        assert _job_row(jid)[0] == "done"
    finally:
        _cleanup(aid)
        _cleanup_jobs(jid)


async def test_recover_stale_media_claims_requeues_old_but_leaves_fresh(
    monkeypatch, tmp_path, db_engine
):
    # A `processing` row older than the horizon has no live worker behind it → requeue (one attempt
    # counted). A FRESH `processing` row is a live transcode → MUST be left alone, or the sweep
    # would double-transcode a running job. This is the horizon-above-the-transcode-cap invariant.
    monkeypatch.setattr(settings, "MEDIA_CLAIM_STALE_SECONDS", 100)
    stale_aid, stale_jid = _seed_processing(age_seconds=200, attempts=0)
    fresh_aid, fresh_jid = _seed_processing(age_seconds=5, attempts=0)
    try:
        async with async_session() as db:
            await worker.recover_stale_media_claims(db)
            await db.commit()
        assert _job_row(stale_jid) == ("queued", 1)  # requeued, one attempt counted
        assert _job_row(fresh_jid) == ("processing", 0)  # a live job is never yanked
    finally:
        _cleanup(stale_aid, fresh_aid)
        _cleanup_jobs(stale_jid, fresh_jid)


async def test_recover_stale_media_claims_fails_terminally_at_the_attempt_ceiling(
    monkeypatch, tmp_path, db_engine
):
    # A stale `processing` job on its last life goes to the visible `failed` state (never a silent
    # drop), and — being a `derive` job — marks its attachment failed too.
    monkeypatch.setattr(settings, "MEDIA_CLAIM_STALE_SECONDS", 100)
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 3)
    aid, jid = _seed_processing(age_seconds=200, attempts=2)  # +1 → terminal
    try:
        async with async_session() as db:
            await worker.recover_stale_media_claims(db)
            await db.commit()
        assert _job_row(jid) == ("failed", 3)
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            status = c.execute(
                text("SELECT status FROM attachments WHERE id=:a"), {"a": aid}
            ).scalar_one()
        eng.dispose()
        assert status == "failed"
    finally:
        _cleanup(aid)
        _cleanup_jobs(jid)


def test_drain_health_stops_heartbeat_after_an_error_streak():
    # MEMNG-90 healthcheck honesty: only run_media_drain()=='error' (an escape from run_once)
    # counts; any other outcome — including a legitimately idle queue — resets, so an idle worker
    # never looks unhealthy. Consecutive hard errors past the threshold suppress the heartbeat.
    h = worker._DrainHealth(threshold=3)
    assert h.healthy
    h.record("error")
    h.record("error")
    assert h.healthy  # 2 < 3
    h.record("error")
    assert not h.healthy  # 3 → unhealthy, the loop stops stamping
    h.record("processed")
    assert h.healthy  # any non-error resets the streak
    for _ in range(5):
        h.record("error")
    assert not h.healthy
    h.record("idle")
    assert h.healthy  # an empty queue is not a failure


# --- MEMNG-361: the stale sweep hands recovered rows back to the CLASS-ROUTED consumers ---


def _seed_stale(*, mime, lane, storage_path, age_seconds, duration=None, kind="derive"):
    """A `processing` row aged `age_seconds` into the past, like `_seed_processing`, but with the
    mime/lane/kind the CLASS predicate reads — a stale claim of one class is otherwise
    indistinguishable from a stale claim of the other, and the whole point here is that the sweep
    returns each one to the consumer that can drain it."""
    uid = f"stale-{secrets.token_urlsafe(6)}"
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, filename, mime, size, status, storage_path, duration) VALUES "
                "(:u,'x',:m,1,CAST('processing' AS attachment_status),:sp,:d) RETURNING id"
            ),
            {"u": uid, "m": mime, "sp": str(storage_path), "d": duration},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs "
                "(attachment_id, lane, kind, state, attempts, progress_pct, claimed_by, "
                "updated_at) VALUES (:a, CAST(:l AS job_lane), :k, "
                "CAST('processing' AS job_state), 0, 0, 'dead-worker', "
                "now() - (:age * interval '1 second')) RETURNING id"
            ),
            {"a": aid, "l": lane, "k": kind, "age": age_seconds},
        ).scalar_one()
    eng.dispose()
    return uid, aid, jid


def _stale_ledger_cmd(ledger, hold):
    """A REAL transcode argv that records the run before producing the output, so "finished exactly
    once" is read off work that happened rather than off a terminal row (which a job run twice
    reaches just the same)."""

    def cmd(src, dest, threads):
        return [
            "sh",
            "-c",
            'printf "%s\\n" "$(basename "$2")" >> "$1"; sleep "$3"; printf transcoded > "$2"',
            "sh",
            str(ledger),
            dest,
            str(hold),
        ]

    return cmd


async def _drain_to_idle(job_class, budget=100):
    for _ in range(budget):
        if await worker.run_media_drain(job_class) == "idle":
            return
    raise AssertionError(f"{job_class} never drained to idle within {budget} passes")


def test_the_stale_sweep_returns_a_row_of_each_class_to_its_own_consumer(
    monkeypatch, tmp_path, db_engine
):
    """Both classes' stale claims are recovered, and BOTH consumers then finish them.

    The sweep is class-blind by design (it selects on state and age, never on a class predicate),
    so the risk it has to be held to is the other side: a recovered row must land back in the lane
    and kind its own consumer claims. One stale row per class plus a FRESH `processing` row, which
    is a live worker's claim and must be left exactly where it is.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "WORKER_LANES", "fast,slow")
    monkeypatch.setattr(settings, "MEDIA_CLAIM_STALE_SECONDS", 100)
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 120)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard open
    ledger = tmp_path / "stale-transcodes.log"
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _stale_ledger_cmd(ledger, 0.1))

    clip = tmp_path / "originals" / "stale.mov"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really a video")
    photo = tmp_path / "originals" / "stale.png"
    _make_png(photo)
    fresh_photo = tmp_path / "originals" / "fresh.png"
    _make_png(fresh_photo)

    video_uid, video_aid, video_jid = _seed_stale(
        mime="video/mp4", lane="slow", storage_path=clip, age_seconds=200, duration=2.0
    )
    light_uid, light_aid, light_jid = _seed_stale(
        mime="image/png", lane="fast", storage_path=photo, age_seconds=200
    )
    _, fresh_aid, fresh_jid = _seed_stale(
        mime="image/png", lane="fast", storage_path=fresh_photo, age_seconds=5
    )
    try:

        async def _go():
            async with async_session() as db:
                recovered = await worker.recover_stale_media_claims(db)
                await db.commit()
            await _drain_to_idle("transcode")
            await _drain_to_idle("light")
            return recovered

        assert asyncio.run(_go()) == 2  # the fresh claim is not stale and is not touched

        assert _job_row(video_jid) == ("done", 1)  # recovery costs the one attempt, and no more
        assert _job_row(light_jid) == ("done", 1)
        assert _job_row(fresh_jid) == ("processing", 0)  # a live worker still owns it
        assert ledger.read_text().split() == [f"{video_uid}.tmp.mp4"]  # ran once, not twice
        assert (tmp_path / "assets" / "web" / f"{video_uid}.mp4").exists()
        assert (tmp_path / "assets" / "thumbs" / f"{light_uid}.jpg").exists()
    finally:
        _cleanup(video_aid, light_aid, fresh_aid)
        _cleanup_jobs(video_jid, light_jid, fresh_jid)
