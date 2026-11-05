"""Upload, media-queue and event-stream families on the app's `/metrics` (MEMNG-962).

Every in-process family is moved by driving its real code path (the upload routes, the admission
gate, the `/api/events` endpoint) and read back from the registry `/metrics` serves; every
database-derived family is read from the real scrape body against rows in the real database.
Nothing here stands in for the instrumentation under test.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings
from app.routers import assets as assets_router
from app.routers.uploads import body_admission
from app.services import upload_metrics as um
from app.services import upload_queue_metrics as qm
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.test_attachments_taken import _sql
from tests.test_trip_ingest_metrics import _absent, _scrape, _value

ALL_FAMILIES = (
    "memosng_upload_bytes_total",
    "memosng_upload_sessions_total",
    "memosng_upload_patch_seconds",
    "memosng_upload_admission_rejections_total",
    "memosng_upload_sessions_open",
    "memosng_media_jobs_queued",
    "memosng_media_job_seconds",
    "memosng_events_subscribers",
)
DB_FAMILIES = (qm.SESSIONS_OPEN, qm.JOBS_QUEUED, qm.JOB_SECONDS)


def _clean():
    _sql("DELETE FROM media_jobs")
    _sql("DELETE FROM attachments")
    _sql("DELETE FROM uploads")


@pytest.fixture
def clean(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _clean()
    yield
    _clean()


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _start(client, size: int, name: str = "a.bin") -> str:
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": "application/octet-stream", "size": size},
    )
    assert r.status_code == 201, r.text
    return r.json()["upload_uid"]


def _labelled(body: str, name: str, labels: str) -> float | None:
    for line in body.splitlines():
        if line.startswith(f"{name}{{{labels}}} "):
            return float(line.split()[-1])
    return None


def test_bytes_count_what_a_patch_committed_per_principal(clean, logged_in_client):
    client, _ = logged_in_client
    data = b"x" * 5000
    before = _sample("memosng_upload_bytes_total", {"principal": "session"})
    uid = _start(client, len(data))
    assert client.patch(
        f"/api/uploads/{uid}", content=data[:3000], headers={"Upload-Offset": "0"}
    ).is_success
    # A replay of a range the server already holds adds nothing: the family counts bytes that
    # arrived, not bytes that were sent.
    assert client.patch(
        f"/api/uploads/{uid}", content=data[:3000], headers={"Upload-Offset": "0"}
    ).is_success
    assert client.patch(
        f"/api/uploads/{uid}", content=data[3000:], headers={"Upload-Offset": "3000"}
    ).is_success
    after = _sample("memosng_upload_bytes_total", {"principal": "session"})
    assert after - before == len(data)


async def test_a_bearer_upload_counts_under_the_pat_principal(clean, pat_client):
    bearer, _, _ = pat_client
    data = b"y" * 1234
    before = _sample("memosng_upload_bytes_total", {"principal": "pat"})
    session_before = _sample("memosng_upload_bytes_total", {"principal": "session"})
    r = await bearer.post(
        "/api/uploads",
        json={"filename": "p.bin", "mime": "application/octet-stream", "size": len(data)},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["upload_uid"]
    r = await bearer.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert r.is_success, r.text
    assert _sample("memosng_upload_bytes_total", {"principal": "pat"}) - before == len(data)
    assert _sample("memosng_upload_bytes_total", {"principal": "session"}) == session_before


def test_patch_seconds_observes_every_admitted_body(clean, logged_in_client):
    client, _ = logged_in_client
    data = b"z" * 100
    before = _sample("memosng_upload_patch_seconds_count")
    uid = _start(client, len(data))
    assert client.patch(
        f"/api/uploads/{uid}", content=data[:40], headers={"Upload-Offset": "0"}
    ).is_success
    # An offset gap is refused AFTER admission: it held a slot, so its duration is observed too.
    gap = client.patch(f"/api/uploads/{uid}", content=data[60:], headers={"Upload-Offset": "60"})
    assert gap.status_code == 409
    assert _sample("memosng_upload_patch_seconds_count") - before == 2
    assert _sample("memosng_upload_patch_seconds_sum") > 0


def test_sessions_result_completed_deduped_and_rejected_size(clean, logged_in_client):
    client, _ = logged_in_client
    completed = _sample("memosng_upload_sessions_total", {"result": "completed"})
    deduped = _sample("memosng_upload_sessions_total", {"result": "deduped"})
    rejected = _sample("memosng_upload_sessions_total", {"result": "rejected_size"})
    data = b"same-bytes" * 300
    for name in ("one.bin", "two.bin"):
        uid = _start(client, len(data), name)
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        assert client.post(f"/api/uploads/{uid}/finalize").status_code == 200
    too_big = client.post(
        "/api/uploads",
        json={
            "filename": "huge.bin",
            "mime": "application/octet-stream",
            "size": settings.MAX_UPLOAD_MIB * 1024 * 1024 + 1,
        },
    )
    assert too_big.status_code == 413
    # Past the declared size at the chunk append: the other place a size refusal ends a session.
    small = _start(client, 10, "small.bin")
    over = client.patch(f"/api/uploads/{small}", content=b"q" * 11, headers={"Upload-Offset": "0"})
    assert over.status_code == 413
    assert _sample("memosng_upload_sessions_total", {"result": "completed"}) - completed == 1
    assert _sample("memosng_upload_sessions_total", {"result": "deduped"}) - deduped == 1
    assert _sample("memosng_upload_sessions_total", {"result": "rejected_size"}) - rejected == 2


def test_every_session_result_is_published_before_its_first_count():
    """MEMNG-1167 — `linked` joins the results pre-registered at import, so a quiet hour reads 0
    rather than no data. Asked of a FRESH interpreter: in this one, any earlier test that counted
    a result has already made its sample exist, pre-registered or not."""
    import subprocess
    import sys

    probe = (
        "from app.services import upload_metrics as um\n"
        "print(sorted(s.labels['result'] for m in um.UPLOAD_SESSIONS.collect() "
        "for s in m.samples if s.name.endswith('_total') and s.value == 0))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == str(
        sorted(["completed", "deduped", "linked", "withdrawn", "rejected_size"])
    )


def test_a_finalize_that_rolls_back_counts_no_result(clean, logged_in_client, monkeypatch):
    """The result is counted at COMMIT: a finalize whose transaction dies leaves the session open,
    and the client will finalize it again — counting it now would count it twice."""
    from app.routers import uploads as uploads_router

    client, _ = logged_in_client
    data = b"rollback" * 100
    uid = _start(client, len(data))
    client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    before = _sample("memosng_upload_sessions_total", {"result": "completed"})
    real_finalize = uploads_router.svc.finalize

    async def finalize_then_fail(db, *args, **kwargs):
        await real_finalize(db, *args, **kwargs)
        await db.rollback()
        raise ValueError("forced rollback after finalize")

    monkeypatch.setattr(uploads_router.svc, "finalize", finalize_then_fail)
    assert client.post(f"/api/uploads/{uid}/finalize").status_code == 400
    assert _sample("memosng_upload_sessions_total", {"result": "completed"}) == before
    assert _sql("SELECT 1 FROM uploads WHERE upload_uid = :u", {"u": uid}), "session not restored"


def test_admission_refusals_count_by_the_cap_that_refused(monkeypatch):
    assert body_admission.total == 0
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 1)
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_PROCESS", 2)
    user = _sample("memosng_upload_admission_rejections_total", {"scope": "user"})
    process = _sample("memosng_upload_admission_rejections_total", {"scope": "process"})
    held: list[int] = []
    try:
        assert body_admission.admit(901)
        held.append(901)
        assert not body_admission.admit(901)  # this user's one slot is taken
        assert body_admission.admit(902)
        held.append(902)
        assert not body_admission.admit(903)  # the process's two slots are taken
    finally:
        for user_id in held:
            body_admission.release(user_id)
    assert _sample("memosng_upload_admission_rejections_total", {"scope": "user"}) - user == 1
    assert _sample("memosng_upload_admission_rejections_total", {"scope": "process"}) - process == 1


def test_sessions_open_is_read_from_the_uploads_table(clean, logged_in_client):
    client, _ = logged_in_client
    assert _value(_scrape(client), qm.SESSIONS_OPEN) == 0
    _start(client, 10, "a.bin")
    expired = _start(client, 10, "b.bin")
    # Past expires_at the session takes no more bytes worth waiting for: the reaper owns it now.
    _sql(
        "UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE upload_uid = :u",
        {"u": expired},
    )
    assert _value(_scrape(client), qm.SESSIONS_OPEN) == 1


def _job(lane: str, state: str, *, kind: str = "derive", age_s: float = 0, took_s: float = 0):
    created = datetime.now(UTC) - timedelta(seconds=age_s)
    _sql(
        "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct, "
        "created_at, updated_at) VALUES (1, :lane, :kind, :state, 0, 0, :c, :u)",
        {
            "lane": lane,
            "kind": kind,
            "state": state,
            "c": created,
            "u": created + timedelta(seconds=took_s),
        },
    )


def test_queued_jobs_are_counted_per_lane_with_an_empty_lane_at_zero(clean, client):
    _job("fast", "queued")
    _job("fast", "queued")
    _job("fast", "processing")
    _job("fast", "done")
    body = _scrape(client)
    assert _labelled(body, qm.JOBS_QUEUED, 'lane="fast"') == 2
    assert _labelled(body, qm.JOBS_QUEUED, 'lane="slow"') == 0


def test_job_seconds_buckets_recent_finished_jobs_by_lane_and_kind(clean, client):
    _job("fast", "done", age_s=60, took_s=3)
    _job("fast", "done", age_s=60, took_s=40)
    _job("slow", "done", age_s=7200, took_s=1200)  # finished 6000 s ago: outside the hour
    _job("slow", "done", age_s=1500, took_s=1200)
    _job("fast", "done", kind="printready", age_s=30, took_s=2)
    _job("fast", "failed", age_s=60, took_s=5)  # not a finished derivation
    body = _scrape(client)
    fast = 'kind="derive",lane="fast"'
    assert _labelled(body, f"{qm.JOB_SECONDS}_bucket", f'{fast},le="5.0"') == 1
    assert _labelled(body, f"{qm.JOB_SECONDS}_bucket", f'{fast},le="60.0"') == 2
    assert _labelled(body, f"{qm.JOB_SECONDS}_bucket", f'{fast},le="+Inf"') == 2
    assert _labelled(body, f"{qm.JOB_SECONDS}_gcount", fast) == 2
    assert _labelled(body, f"{qm.JOB_SECONDS}_gsum", fast) == pytest.approx(43, abs=0.5)
    slow = 'kind="derive",lane="slow"'
    assert _labelled(body, f"{qm.JOB_SECONDS}_bucket", f'{slow},le="900.0"') == 0
    assert _labelled(body, f"{qm.JOB_SECONDS}_bucket", f'{slow},le="1800.0"') == 1
    assert _labelled(body, f"{qm.JOB_SECONDS}_gcount", 'kind="printready",lane="fast"') == 1


async def test_a_database_that_cannot_answer_drops_the_queue_families_only(
    clean, client, monkeypatch
):
    body = _scrape(client)
    assert not any(_absent(body, name) for name in DB_FAMILIES)
    url = qm.async_session.kw["bind"].url.set(database="memosng_no_such_database")
    broken = create_async_engine(url, poolclass=NullPool)
    monkeypatch.setattr(qm, "async_session", async_sessionmaker(broken))
    try:
        body = _scrape(client)
    finally:
        await broken.dispose()
    for name in DB_FAMILIES:
        assert _absent(body, name), name
    assert not _absent(body, "memosng_upload_bytes_total")
    assert not _absent(body, "memosng_events_subscribers")


def test_events_subscribers_counts_a_live_stream_and_lets_it_go(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    seen: list[float] = []

    async def snapshot(user_id: int) -> list[dict]:
        seen.append(_sample("memosng_events_subscribers"))
        return []

    monkeypatch.setattr(assets_router, "_user_snapshot", snapshot)
    # The stream never ends on its own and the test client returns only a finished body.
    monkeypatch.setattr(assets_router, "STREAM_LIFETIME_SECONDS", 0.2)
    before = _sample("memosng_events_subscribers")
    r = client.get("/api/events")
    assert r.status_code == 200
    assert seen and seen[0] == before + 1
    assert _sample("memosng_events_subscribers") == before


async def test_a_subscriber_that_disconnects_mid_stream_is_released():
    closed: list[bool] = []

    async def forever():
        try:
            while True:
                yield "data: {}\n\n"
        finally:
            closed.append(True)

    before = _sample("memosng_events_subscribers")
    stream = um.events_subscriber(forever())
    await anext(stream)
    assert _sample("memosng_events_subscribers") == before + 1
    await stream.aclose()  # what Starlette does when the client goes away
    assert _sample("memosng_events_subscribers") == before
    assert closed == [True], "the wrapped stream's own cleanup did not run on disconnect"


def test_every_family_is_on_the_one_metrics_endpoint(clean, client):
    _job("fast", "done", age_s=10, took_s=1)
    body = _scrape(client)
    for name in ALL_FAMILIES:
        assert not _absent(body, name), name
    assert um.register_once(REGISTRY) is um.register_once(REGISTRY)
    assert qm.register_once(REGISTRY) is qm.register_once(REGISTRY)


class _Broken:
    """An instrument whose every call raises — what a mislabelled or broken metric does."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise RuntimeError(f"instrument {name} broke")

        return _raise


def test_a_broken_instrument_never_changes_a_request_outcome(clean, logged_in_client, monkeypatch):
    """Instrumentation sits on the 413, 429 and PATCH paths (one of them in a `finally`); a metric
    call that raises must be logged, never answered in place of the request's own outcome."""
    client, _ = logged_in_client
    uid = _start(client, 10)
    for name in ("UPLOAD_BYTES", "UPLOAD_SESSIONS", "UPLOAD_PATCH_SECONDS", "ADMISSION_REJECTIONS"):
        monkeypatch.setattr(um, name, _Broken())
    too_big = client.post(
        "/api/uploads",
        json={
            "filename": "huge.bin",
            "mime": "application/octet-stream",
            "size": settings.MAX_UPLOAD_MIB * 1024 * 1024 + 1,
        },
    )
    assert too_big.status_code == 413
    assert (
        client.patch(
            f"/api/uploads/{uid}", content=b"q" * 11, headers={"Upload-Offset": "0"}
        ).status_code
        == 413
    )
    ok = client.patch(f"/api/uploads/{uid}", content=b"q" * 10, headers={"Upload-Offset": "0"})
    assert ok.is_success, ok.text
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_PROCESS", 0)
    assert (
        client.patch(
            f"/api/uploads/{uid}", content=b"q", headers={"Upload-Offset": "10"}
        ).status_code
        == 429
    )
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_PROCESS", 10)
    assert client.post(f"/api/uploads/{uid}/finalize").status_code == 200


async def test_a_savepoint_neither_counts_nor_drops_the_outer_result(db_engine):
    """The result is the OUTER transaction's: a SAVEPOINT released inside it is not the commit,
    and a SAVEPOINT rolled back inside it is not the rollback."""
    from app.database import async_session
    from sqlalchemy import text

    before = _sample("memosng_upload_sessions_total", {"result": "completed"})
    async with async_session() as db:
        um.count_session_end(db, "completed")
        async with db.begin_nested():
            await db.execute(text("SELECT 1"))
        await db.rollback()
    assert _sample("memosng_upload_sessions_total", {"result": "completed"}) == before

    async with async_session() as db:
        um.count_session_end(db, "completed")
        with pytest.raises(Exception, match="division by zero"):
            async with db.begin_nested():
                await db.execute(text("SELECT 1/0"))
        await db.commit()
    assert _sample("memosng_upload_sessions_total", {"result": "completed"}) == before + 1
