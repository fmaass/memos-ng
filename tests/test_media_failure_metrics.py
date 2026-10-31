"""Media-job failure reasons and the expired-session count (MEMNG-1046).

Both are counted by the WORKER, which serves no /metrics, so each is a durable tally the worker
bumps in the transaction that does the work and the app reads per scrape:
`memosng_media_jobs_failed_total{lane,reason}` and
`memosng_upload_sessions_total{result="expired"}`.
"""

import asyncio
import errno
import secrets
import subprocess
from io import BytesIO
from pathlib import Path
from typing import get_args

import pytest
from alembic import command
from alembic.config import Config
from app import worker
from app.config import settings
from app.database import async_session
from app.models import MEDIA_JOB_CLASSES, FailureReason
from app.services import media, sibling, tallies
from app.services import upload_queue_metrics as qm
from app.services import uploads as svc
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL
from tests.test_trip_ingest_metrics import _absent, _scrape
from tests.test_upload_metrics import _labelled

BEFORE_TALLIES_REVISION = "f9af5db6b98c"


def _sql(stmt: str, params: dict | None = None):
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(stmt), params or {})
            return result.all() if result.returns_rows else None
    finally:
        eng.dispose()


def _failures(lane: str, reason: str) -> int:
    rows = _sql(
        "SELECT count FROM media_job_failure_tallies WHERE lane = :l AND reason = :r",
        {"l": lane, "r": reason},
    )
    return int(rows[0][0]) if rows else 0


def _expired() -> int:
    rows = _sql("SELECT count FROM upload_session_tallies WHERE result = 'expired'")
    return int(rows[0][0]) if rows else 0


# --- classify_failure: one case per reason value ------------------------------------------------


def _truncated_png() -> OSError:
    buf = BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, "PNG")
    try:
        with Image.open(BytesIO(buf.getvalue()[:60])) as img:
            img.load()
    except OSError as exc:
        return exc
    raise AssertionError("test setup: a truncated PNG decoded")


def _unidentified() -> OSError:
    try:
        Image.open(BytesIO(b"not an image at all"))
    except OSError as exc:
        return exc
    raise AssertionError("test setup: garbage was identified as an image")


def _missing_file() -> OSError:
    try:
        Path("/nonexistent/memng-1046/original.heic").read_bytes()
    except OSError as exc:
        return exc
    raise AssertionError("test setup: the missing file exists")


CASES = [
    pytest.param(_unidentified(), FailureReason.decode_error, id="decode-unidentified"),
    pytest.param(_truncated_png(), FailureReason.decode_error, id="decode-truncated"),
    pytest.param(
        Image.DecompressionBombError("too many pixels"),
        FailureReason.decode_error,
        id="decode-bomb",
    ),
    pytest.param(_missing_file(), FailureReason.source_missing, id="source-missing-file"),
    pytest.param(
        media.SourceMissing("attachment 1 has no source file for a print rendition"),
        FailureReason.source_missing,
        id="source-missing-print",
    ),
    pytest.param(
        OSError(errno.ENOSPC, "No space left on device"),
        FailureReason.storage_error,
        id="storage-enospc",
    ),
    pytest.param(
        PermissionError(errno.EACCES, "Permission denied"),
        FailureReason.storage_error,
        id="storage-eacces",
    ),
    pytest.param(
        media.TranscodeFailed("ffmpeg exited 1: moov atom not found"),
        FailureReason.ffmpeg_failed,
        id="ffmpeg-transcode",
    ),
    pytest.param(
        subprocess.CalledProcessError(1, ["ffprobe"]),
        FailureReason.ffmpeg_failed,
        id="ffmpeg-probe",
    ),
    pytest.param(
        media.TranscodeTimeout("slow-lane transcode timed out after 3600s"),
        FailureReason.timeout,
        id="timeout-transcode",
    ),
    pytest.param(
        subprocess.TimeoutExpired(["ffprobe"], 30), FailureReason.timeout, id="timeout-probe"
    ),
    pytest.param(TimeoutError(), FailureReason.timeout, id="timeout-plain"),
    pytest.param(MemoryError(), FailureReason.out_of_memory, id="out-of-memory"),
    pytest.param(
        sibling.SiblingUnavailable("status 503"),
        FailureReason.sibling_unavailable,
        id="sibling-unavailable",
    ),
    pytest.param(
        RuntimeError("printready is image-only: attachment 1 is not an image"),
        FailureReason.other,
        id="other-runtime",
    ),
    pytest.param(KeyError("width"), FailureReason.other, id="other-unknown"),
    # The type decides, never the message: an untyped error that READS like a timeout is not one.
    pytest.param(
        RuntimeError("slow-lane transcode timed out after 1s"),
        FailureReason.other,
        id="other-message-mimic",
    ),
]


@pytest.mark.parametrize(("exc", "reason"), CASES)
def test_classify_failure_maps_each_exception_to_its_reason(exc, reason):
    assert worker.classify_failure(exc) is reason


def test_every_reason_value_has_a_case():
    # stale_claim has no exception: the stale-claim sweep sets it (its own test below).
    covered = {p.values[1] for p in CASES} | {FailureReason.stale_claim}
    assert covered == set(FailureReason)


def test_the_published_lanes_are_the_worker_consumer_classes():
    assert get_args(worker.JobClass) == MEDIA_JOB_CLASSES


def test_the_typed_transcode_errors_stay_runtime_errors():
    # Callers and tests that match RuntimeError keep matching: the types only add a name.
    for cls in (media.TranscodeTimeout, media.TranscodeFailed, media.SourceMissing):
        assert issubclass(cls, RuntimeError)


# --- the worker records the reason when it marks a job failed -----------------------------------


def _seed(mime: str, lane: str, storage_path: str, *, attempts: int = 0, kind: str = "derive"):
    aid = _sql(
        "INSERT INTO attachments (uid, filename, mime, size, status, storage_path, duration) "
        "VALUES (:u, 'x', :m, 1, 'processing', :sp, 1.0) RETURNING id",
        {"u": f"f1046-{secrets.token_urlsafe(6)}", "m": mime, "sp": storage_path},
    )[0][0]
    jid = _sql(
        "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct) "
        "VALUES (:a, CAST(:l AS job_lane), :k, 'queued', :at, 0) RETURNING id",
        {"a": aid, "l": lane, "k": kind, "at": attempts},
    )[0][0]
    return aid, jid


def _cleanup(*aids: int) -> None:
    for aid in aids:
        _sql("DELETE FROM media_jobs WHERE attachment_id = :a", {"a": aid})
        _sql("DELETE FROM attachments WHERE id = :a", {"a": aid})


def _run_once():
    async def _go():
        async with async_session() as db:
            return await worker.run_once(db)

    return asyncio.run(_go())


@pytest.fixture
def quiet_worker(monkeypatch, tmp_path, db_engine):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off
    _sql("DELETE FROM media_jobs")
    yield tmp_path
    _sql("DELETE FROM media_jobs")


def test_a_terminal_light_failure_is_tallied_under_its_reason(quiet_worker, monkeypatch):
    """A real fast-lane decode of bytes that are no image: Pillow's refusal, classified."""
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)
    src = quiet_worker / "originals" / "x.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"not a png")
    aid, jid = _seed("image/png", "fast", str(src))
    before = _failures("light", "decode_error")
    try:
        with capture_logs() as logs:
            assert _run_once() == "processed"
        assert _sql("SELECT state::text FROM media_jobs WHERE id = :j", {"j": jid}) == [("failed",)]
        assert _failures("light", "decode_error") - before == 1
        terminal = [e for e in logs if e["event"] == "media_job_failed_terminal"]
        assert terminal and terminal[0]["reason"] == "decode_error"
        assert terminal[0]["lane"] == "light"
    finally:
        _cleanup(aid)


def test_a_failed_transcode_is_tallied_under_the_transcode_lane(quiet_worker, monkeypatch):
    """A real ffmpeg-shaped subprocess exiting non-zero, through process_slow."""
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", lambda src, dest, threads: ["sh", "-c", "exit 3"])
    src = quiet_worker / "originals" / "v.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"not really a video")
    aid, _ = _seed("video/mp4", "slow", str(src))
    before = (_failures("transcode", "ffmpeg_failed"), _failures("light", "ffmpeg_failed"))
    try:
        assert _run_once() == "processed"
        after = (_failures("transcode", "ffmpeg_failed"), _failures("light", "ffmpeg_failed"))
        assert (after[0] - before[0], after[1] - before[1]) == (1, 0)
    finally:
        _cleanup(aid)


def test_a_retry_is_not_a_failure(quiet_worker, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 3)

    async def _boom(db, job):
        raise MemoryError()

    monkeypatch.setattr(media, "process_job", _boom)
    aid, jid = _seed("image/png", "fast", "/nonexistent")
    before = _failures("light", "out_of_memory")
    try:
        assert _run_once() == "deferred"
        assert _sql("SELECT state::text FROM media_jobs WHERE id = :j", {"j": jid}) == [("queued",)]
        assert _failures("light", "out_of_memory") == before
    finally:
        _cleanup(aid)


def test_the_stale_claim_sweep_tallies_its_terminal_jobs_as_stale_claim(quiet_worker, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_CLAIM_STALE_SECONDS", 100)
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 3)
    video, vjid = _seed("video/mp4", "slow", "/x.mp4", attempts=2)
    photo, pjid = _seed("image/heic", "slow", "/x.heic", attempts=2)
    young, yjid = _seed("image/png", "fast", "/x.png", attempts=0)  # re-queued, not failed
    _sql(
        "UPDATE media_jobs SET state = 'processing', updated_at = now() - interval '200 seconds' "
        "WHERE id IN (:a, :b, :c)",
        {"a": vjid, "b": pjid, "c": yjid},
    )
    before = (_failures("transcode", "stale_claim"), _failures("light", "stale_claim"))

    async def _sweep():
        async with async_session() as db:
            await worker.recover_stale_media_claims(db)
            await db.commit()

    try:
        asyncio.run(_sweep())
        after = (_failures("transcode", "stale_claim"), _failures("light", "stale_claim"))
        assert (after[0] - before[0], after[1] - before[1]) == (1, 1)
    finally:
        _cleanup(video, photo, young)


def test_a_tally_that_cannot_be_written_never_costs_the_failure_record(quiet_worker, monkeypatch):
    """The tally is telemetry: a refused write (a worker running before the app migrated) is
    logged, and the job's failed state and the photo's still commit."""
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(tallies, "MEDIA_FAILURES_TABLE", "no_such_tally_table")

    async def _boom(db, job):
        raise MemoryError()

    monkeypatch.setattr(media, "process_job", _boom)
    aid, jid = _seed("image/png", "fast", "/nonexistent")
    try:
        with capture_logs() as logs:
            assert _run_once() == "processed"
        assert _sql("SELECT state::text FROM media_jobs WHERE id = :j", {"j": jid}) == [("failed",)]
        assert _sql("SELECT status::text FROM attachments WHERE id = :a", {"a": aid}) == [
            ("failed",)
        ]
        assert any(e["event"] == "tally_write_failed" for e in logs)
    finally:
        _cleanup(aid)


async def test_a_stop_is_never_swallowed_by_the_tally_guard(monkeypatch):
    class _Session:
        def begin_nested(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await tallies.count_upload_sessions(_Session(), "expired", 1)  # type: ignore[arg-type]


def test_two_writers_bumping_one_row_both_count(db_engine):
    _sql("DELETE FROM media_job_failure_tallies")

    async def _bump_and_hold(gate: asyncio.Event):
        async with async_session() as db:
            await tallies.count_media_job_failures(db, "light", FailureReason.other)
            await gate.wait()  # both transactions are open on the same row before either commits
            await db.commit()

    async def _race():
        gate = asyncio.Event()
        first = asyncio.create_task(_bump_and_hold(gate))
        await asyncio.sleep(0.3)  # the first insert holds the row; the second queues behind it
        second = asyncio.create_task(_bump_and_hold(gate))
        await asyncio.sleep(0.3)
        gate.set()
        await asyncio.gather(first, second)

    try:
        asyncio.run(_race())
        assert _failures("light", "other") == 2
    finally:
        _sql("DELETE FROM media_job_failure_tallies")


# --- the expired-session count -------------------------------------------------------------------


def _expired_session(tmp_path: Path) -> str:
    uid = f"exp1046-{secrets.token_urlsafe(6)}"
    _sql(
        "INSERT INTO uploads "
        "(upload_uid, filename, mime, declared_size, bytes_received, expires_at) "
        "VALUES (:u, 'a.bin', 'application/octet-stream', 10, 0, now() - interval '1 minute')",
        {"u": uid},
    )
    return uid


@pytest.fixture
def clean_uploads(monkeypatch, tmp_path, db_engine):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _sql("DELETE FROM uploads")
    yield tmp_path
    _sql("DELETE FROM uploads")


def test_the_reaper_counts_each_session_it_expires(clean_uploads):
    first, second = _expired_session(clean_uploads), _expired_session(clean_uploads)
    before = _expired()
    reaped = asyncio.run(svc.reap_expired_committed())
    assert reaped == 2
    assert _sql(
        "SELECT count(*) FROM uploads WHERE upload_uid IN (:a, :b)", {"a": first, "b": second}
    ) == [(0,)]
    assert _expired() - before == 2
    # A sweep with nothing to reap writes nothing.
    assert asyncio.run(svc.reap_expired_committed()) == 0
    assert _expired() - before == 2


def test_a_reaper_that_rolls_back_counts_nothing(clean_uploads):
    _expired_session(clean_uploads)
    before = _expired()

    async def _reap_then_roll_back():
        async with async_session() as db:
            await svc.reap_expired(db)
            await db.rollback()

    asyncio.run(_reap_then_roll_back())
    assert _expired() == before
    assert _sql("SELECT count(*) FROM uploads") == [(1,)]


def test_a_refused_expired_tally_never_costs_the_reap(clean_uploads, monkeypatch):
    monkeypatch.setattr(tallies, "UPLOAD_SESSIONS_TABLE", "no_such_tally_table")
    _expired_session(clean_uploads)
    with capture_logs() as logs:
        assert asyncio.run(svc.reap_expired_committed()) == 1
    assert _sql("SELECT count(*) FROM uploads") == [(0,)]
    assert any(e["event"] == "tally_write_failed" for e in logs)


# --- the scrape -----------------------------------------------------------------------------------


def test_every_lane_and_reason_is_published_from_the_first_scrape(client, db_engine):
    _sql("DELETE FROM media_job_failure_tallies")
    body = _scrape(client)
    for lane in MEDIA_JOB_CLASSES:
        for reason in FailureReason:
            labels = f'lane="{lane}",reason="{reason.value}"'
            assert _labelled(body, f"{qm.JOBS_FAILED}_total", labels) == 0, labels


def test_the_scrape_reads_the_tallies(client, db_engine):
    _sql("DELETE FROM media_job_failure_tallies")
    _sql("DELETE FROM upload_session_tallies")
    _sql(
        "INSERT INTO media_job_failure_tallies (lane, reason, count) VALUES "
        "('transcode', 'timeout', 4), ('light', 'decode_error', 7)"
    )
    _sql("INSERT INTO upload_session_tallies (result, count) VALUES ('expired', 5)")
    try:
        body = _scrape(client)
        failed = f"{qm.JOBS_FAILED}_total"
        assert _labelled(body, failed, 'lane="transcode",reason="timeout"') == 4
        assert _labelled(body, failed, 'lane="light",reason="decode_error"') == 7
        assert _labelled(body, failed, 'lane="light",reason="timeout"') == 0
        sessions = "memosng_upload_sessions_total"
        assert _labelled(body, sessions, 'result="expired"') == 5
        # The in-process results are still in the same family.
        assert _labelled(body, sessions, 'result="completed"') is not None
        assert body.count("# TYPE memosng_upload_sessions_total counter") == 1
    finally:
        _sql("DELETE FROM media_job_failure_tallies")
        _sql("DELETE FROM upload_session_tallies")


def test_expired_reads_zero_before_the_first_reap(client, db_engine):
    _sql("DELETE FROM upload_session_tallies")
    assert _labelled(_scrape(client), "memosng_upload_sessions_total", 'result="expired"') == 0


async def test_a_database_that_cannot_answer_keeps_the_in_process_session_results(
    client, db_engine, monkeypatch
):
    body = _scrape(client)
    assert not _absent(body, f"{qm.JOBS_FAILED}_total")  # control: published while readable
    assert _labelled(body, "memosng_upload_sessions_total", 'result="expired"') is not None
    url = qm.async_session.kw["bind"].url.set(database="memosng_no_such_database")
    broken = create_async_engine(url, poolclass=NullPool)
    monkeypatch.setattr(qm, "async_session", async_sessionmaker(broken))
    try:
        body = _scrape(client)
    finally:
        await broken.dispose()
    # What cannot be read is not published: no guessed zero for the worker's counts...
    assert _absent(body, f"{qm.JOBS_FAILED}_total")
    assert _labelled(body, "memosng_upload_sessions_total", 'result="expired"') is None
    # ...while the results this process counted itself still are.
    assert _labelled(body, "memosng_upload_sessions_total", 'result="completed"') is not None


# --- the migration -------------------------------------------------------------------------------


def _tables() -> set[str]:
    rows = _sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name IN ('media_job_failure_tallies', 'upload_session_tallies')"
    )
    return {r[0] for r in rows or []}


def test_the_migration_round_trips(db_engine):
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    users_before = _sql("SELECT count(*) FROM users")[0][0]
    both = {"media_job_failure_tallies", "upload_session_tallies"}
    assert _tables() == both  # control: head carries them
    try:
        command.downgrade(cfg, BEFORE_TALLIES_REVISION)
        assert _tables() == set()
    finally:
        command.upgrade(cfg, "head")
    assert _tables() == both
    assert _sql("SELECT count(*) FROM users")[0][0] == users_before
