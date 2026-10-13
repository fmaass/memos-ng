"""The sibling backfill's three disjoint populations, against a real database (MEMNG-796).

One seeded row per category; each population must select exactly its own rows, a dry run must
write nothing, and the script must neither ask img-manager nor write an attachment column.
"""

import asyncio
import secrets
from datetime import UTC, datetime

import pytest
from app.config import settings
from app.scripts import backfill_sibling_metadata as bf
from app.services import sibling
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

EXIF_DATE = datetime(2021, 2, 3, 4, 5, 6, tzinfo=UTC)


def _sql(query: str, **params):
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(query), params)
            return result.all() if result.returns_rows else None
    finally:
        eng.dispose()


def _user() -> int:
    return _sql(
        "INSERT INTO users (subject, role, default_visibility, theme) "
        "VALUES (:s, 'user', 'PUBLIC', 'dark') RETURNING id",
        s=f"sibling-bf-{secrets.token_urlsafe(6)}",
    )[0][0]


def _attachment(owner: int, *, mime: str = "image/png", jobs: tuple[str, ...] = (), **cols) -> int:
    values = {
        "capture_instant": None,
        "capture_civil": None,
        "capture_source": None,
        "latitude": None,
        "longitude": None,
        "location_source": "none",
        "sibling_outcome": None,
    }
    values.update(cols)
    aid = _sql(
        "INSERT INTO attachments (uid, owner_id, filename, mime, size, status, storage_path,"
        " capture_instant, capture_civil, capture_source, latitude, longitude, location_source,"
        " sibling_outcome) VALUES (:u, :o, '_AR1 - screen.png', :m, 1, 'ready', '/nonexistent',"
        " :capture_instant, :capture_civil, :capture_source, :latitude, :longitude,"
        " :location_source, :sibling_outcome) RETURNING id",
        u=secrets.token_urlsafe(10),
        o=owner,
        m=mime,
        **values,
    )[0][0]
    for state in jobs:
        _sql(
            "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct)"
            " VALUES (:a, 'fast', 'sibling', CAST(:s AS job_state), 0, 0)",
            a=aid,
            s=state,
        )
    return aid


@pytest.fixture
def seeded(db_engine, monkeypatch):
    _sql("DELETE FROM media_jobs")
    _sql("DELETE FROM attachments")
    allowed, other = _user(), _user()
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://img-manager.test:8000")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "tok")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", str(allowed))
    rows = {
        "undated": _attachment(allowed),
        "other_owner": _attachment(other),
        "civil_with_gps": _attachment(
            allowed,
            capture_civil="2026-01-01T13:00:00",
            capture_source="exif",
            latitude=1.0,
            longitude=2.0,
            location_source="exif",
        ),
        "dated_located_heic": _attachment(
            allowed,
            mime="image/heic",
            capture_instant=EXIF_DATE,
            capture_source="exif",
            latitude=1.0,
            longitude=2.0,
            location_source="exif",
        ),
        "queued": _attachment(allowed, jobs=("queued",)),
        "failed": _attachment(allowed, jobs=("failed",)),
        "done_none": _attachment(allowed, jobs=("done",), sibling_outcome="none"),
        "done_anomaly": _attachment(allowed, jobs=("done",)),
        "ambiguous": _attachment(allowed, jobs=("done",), sibling_outcome="ambiguous"),
    }
    yield rows
    _sql("DELETE FROM media_jobs")
    _sql("DELETE FROM attachments")
    _sql("DELETE FROM users WHERE id IN (:a, :b)", a=allowed, b=other)


def _job_count() -> int:
    return _sql("SELECT count(*) FROM media_jobs")[0][0]


def _queued_sibling_jobs() -> set[int]:
    return {
        r[0]
        for r in _sql(
            "SELECT attachment_id FROM media_jobs WHERE kind = 'sibling' AND state = 'queued'"
            " AND lane = 'fast'"
        )
    }


def _attachments_snapshot():
    return _sql("SELECT * FROM attachments ORDER BY id")


def _selected(population: bf.Population) -> set[int]:
    async def go():
        from app.database import async_session

        async with async_session() as db:
            return {r[0] for r in (await db.execute(bf.candidate_statement(population))).all()}

    return asyncio.run(go())


def test_the_default_dry_run_selects_the_undated_row_and_the_anomaly_and_writes_nothing(seeded):
    jobs_before = _job_count()

    report = asyncio.run(bf.run(bf.Population.default, execute=False))

    assert _selected(bf.Population.default) == {seeded["undated"], seeded["done_anomaly"]}
    assert report.selected == 2 and report.enqueued == 0
    assert report.by_mime == {"image/png": 2}
    assert _job_count() == jobs_before


def test_default_execute_honours_the_limit_then_drains_then_is_idempotent(seeded):
    queued_before = _queued_sibling_jobs()
    first = asyncio.run(bf.run(bf.Population.default, execute=True, limit=1))
    assert first.enqueued == 1
    assert _queued_sibling_jobs() - queued_before == {seeded["undated"]}  # the lower id

    second = asyncio.run(bf.run(bf.Population.default, execute=True))
    assert second.enqueued == 1
    assert _queued_sibling_jobs() - queued_before == {seeded["undated"], seeded["done_anomaly"]}

    jobs = _job_count()
    third = asyncio.run(bf.run(bf.Population.default, execute=True))
    assert third.enqueued == 0
    assert _job_count() == jobs


def test_retry_failed_enqueues_only_the_failed_row(seeded):
    queued_before = _queued_sibling_jobs()
    report = asyncio.run(bf.run(bf.Population.retry_failed, execute=True))
    assert report.enqueued == 1
    assert _queued_sibling_jobs() - queued_before == {seeded["failed"]}


def test_include_ambiguous_enqueues_only_the_ambiguous_row(seeded):
    queued_before = _queued_sibling_jobs()
    report = asyncio.run(bf.run(bf.Population.include_ambiguous, execute=True))
    assert report.enqueued == 1
    assert _queued_sibling_jobs() - queued_before == {seeded["ambiguous"]}


def test_the_three_populations_are_disjoint(seeded):
    default = _selected(bf.Population.default)
    retry = _selected(bf.Population.retry_failed)
    ambiguous = _selected(bf.Population.include_ambiguous)
    assert default and retry and ambiguous
    assert not (default & retry) and not (default & ambiguous) and not (retry & ambiguous)


def test_a_failed_latest_job_beside_a_live_one_is_not_retried(seeded):
    # The live job is the OLDER one, so "latest is failed" alone would select the row: only the
    # live-job exclusion keeps a retry from stacking a second job on one still in flight.
    [owner] = settings.sibling_owner_ids
    aid = _attachment(owner, jobs=("queued", "failed"))
    assert aid not in _selected(bf.Population.retry_failed)
    assert _selected(bf.Population.retry_failed) == {seeded["failed"]}


def test_the_script_never_asks_img_manager_nor_writes_an_attachment_column(seeded, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the backfill must never call img-manager")

    monkeypatch.setattr(sibling, "lookup", refuse)
    before = _attachments_snapshot()
    for population in bf.Population:
        asyncio.run(bf.run(population, execute=True))
    assert _attachments_snapshot() == before
