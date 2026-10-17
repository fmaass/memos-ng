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


# --- `--audit` (MEMNG-843): the read-only re-judgement of every date the lookup already borrowed.
MAY_POST = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)
AUG_EXPORT = datetime(2026, 8, 12, 20, 30, 6, tzinfo=UTC)
AUG_POST = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)


def _memo(owner: int, at: datetime) -> int:
    return _sql(
        "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, updated_at)"
        " VALUES (:u, :o, 'm', CAST('PUBLIC' AS visibility), false, :t, :t) RETURNING id",
        u=secrets.token_urlsafe(10),
        o=owner,
        t=at,
    )[0][0]


def _borrowed(
    owner: int,
    memo: int | None,
    *,
    borrowed: datetime | None,
    created_at: datetime,
    captured_at: datetime | None,
    capture_source: str | None = "sibling",
    location_source: str = "none",
    storage_path: str = "/nonexistent",
) -> int:
    return _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status,"
        " storage_path, capture_instant, capture_source, location_source, latitude, longitude,"
        " sibling_outcome, created_at, captured_at) VALUES (:u, :o, :m, 'IMG_9052.png',"
        " 'image/png', 1, 'ready', :sp, :ci, :cs, :ls, :la, :lo, 'unique', :ca, :cap)"
        " RETURNING id",
        u=secrets.token_urlsafe(10),
        o=owner,
        m=memo,
        sp=storage_path,
        ci=borrowed,
        cs=capture_source,
        ls=location_source,
        la=1.0 if location_source == "sibling" else None,
        lo=2.0 if location_source == "sibling" else None,
        ca=created_at,
        cap=captured_at,
    )[0][0]


@pytest.fixture
def audited(db_engine):
    _sql("DELETE FROM media_jobs")
    _sql("DELETE FROM attachments")
    owner = _user()
    may, aug = _memo(owner, MAY_POST), _memo(owner, AUG_POST)
    rows = {
        "years_off": _borrowed(
            owner,
            may,
            borrowed=datetime(2011, 2, 28, tzinfo=UTC),
            created_at=MAY_POST,
            captured_at=MAY_POST,
        ),
        "after_export": _borrowed(
            owner,
            aug,
            borrowed=datetime(2026, 8, 15, 17, 0, 12, tzinfo=UTC),
            created_at=AUG_POST,
            captured_at=AUG_EXPORT,
        ),
        "plausible": _borrowed(
            owner,
            aug,
            borrowed=datetime(2026, 8, 12, 13, 2, 3, tzinfo=UTC),
            created_at=AUG_POST,
            captured_at=AUG_EXPORT,
        ),
        "own_exif": _borrowed(
            owner,
            may,
            borrowed=datetime(2011, 2, 28, tzinfo=UTC),
            created_at=MAY_POST,
            captured_at=MAY_POST,
            capture_source="exif",
            location_source="sibling",
        ),
    }
    yield owner, rows
    _sql("DELETE FROM media_jobs")
    _sql("DELETE FROM attachments")
    _sql("DELETE FROM users WHERE id = :o", o=owner)


def test_the_audit_lists_every_borrowed_date_the_window_refuses_and_why(audited):
    _, rows = audited

    report = asyncio.run(bf.audit())

    assert report.sibling_dated == 3
    assert {(r.attachment_id, r.reason) for r in report.refused} == {
        (rows["years_off"], "before-window"),
        (rows["after_export"], "after-export"),
    }
    # The EXIF-dated row borrowed only a location; no candidate date was kept to judge it by.
    assert report.location_only == 1


def test_the_audit_writes_nothing_and_enqueues_nothing(audited, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the audit asked img-manager")

    monkeypatch.setattr(sibling, "lookup", refuse)
    before, jobs = _attachments_snapshot(), _job_count()

    report = asyncio.run(bf.audit())

    assert report.refused
    assert _attachments_snapshot() == before
    assert _job_count() == jobs
    assert "AUDIT (read-only" in report.format()


def _lightroom_png(path) -> str:
    """A real PNG whose only date is the XMP packet Lightroom writes (MEMNG-844)."""
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_itxt(
        "XML:com.adobe.xmp",
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
        ' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about=""'
        ' xmlns:exif="http://ns.adobe.com/exif/1.0/"'
        ' exif:DateTimeOriginal="2026-08-12T18:45:48.298+01:00"/></rdf:RDF></x:xmpmeta>',
    )
    Image.new("RGB", (4, 3)).save(path, "PNG", pnginfo=info)
    return str(path)


def test_the_audit_lists_the_borrowed_rows_whose_own_xmp_now_dates_them(audited, tmp_path):
    owner, rows = audited
    memo = _memo(owner, AUG_POST)
    own = _borrowed(
        owner,
        memo,
        borrowed=datetime(2026, 4, 5, 11, 0, tzinfo=UTC),
        created_at=AUG_POST,
        captured_at=AUG_EXPORT,
        storage_path=_lightroom_png(tmp_path / "_AR59974 - screen.png"),
    )
    before = _attachments_snapshot()

    report = asyncio.run(bf.audit())

    assert [(r.attachment_id, r.source, r.instant) for r in report.self_dated] == [
        (own, "xmp", datetime(2026, 8, 12, 17, 45, 48, tzinfo=UTC))
    ]
    # The seeded rows point at no file: counted, never guessed at.
    assert report.unreadable == len([k for k in rows if k != "own_exif"])
    assert _attachments_snapshot() == before
    assert f"attachment {own} memo {memo}: xmp 2026-08-12T17:45:48+00:00" in report.format()
