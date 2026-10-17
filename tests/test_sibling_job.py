"""The `sibling` media job against a real database (MEMNG-796).

img-manager is the dependency and is stubbed at `sibling.lookup` — the module attribute the job
calls — with a SPY: every "did it ask?" assertion is made on the spy's calls first, at the decision
point (MEMNG-361), before any end-state row is read. Rows are seeded and read back over a separate
sync connection, because the job commits its own transactions.
"""

import asyncio
import hashlib
import io
import secrets
import threading
from datetime import UTC, datetime, timedelta

import pytest
from anyio import to_thread
from app.config import settings
from app.database import async_session
from app.models import MediaJob
from app.services import media, reclaim, sibling
from app.services.sibling import Candidate, SiblingUnavailable, StemResult
from app.worker import run_once
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

SIBLING_DATE = datetime(2023, 7, 14, 9, 30, 15, tzinfo=UTC)
UPLOADED_AT = SIBLING_DATE + timedelta(days=2)
EXIF_DATE = datetime(2021, 2, 3, 4, 5, 6, tzinfo=UTC)
SIBLING_LAT, SIBLING_LON = 46.123456, 7.654321


def _sql(query: str, **params):
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(query), params)
            return result.all() if result.returns_rows else None
    finally:
        eng.dispose()


@pytest.fixture
def owners(db_engine):
    """Two members: the one whose RAW library img-manager indexes (in the fence) and another."""
    made = []
    for _ in range(2):
        made.append(
            _sql(
                "INSERT INTO users (subject, role, default_visibility, theme) "
                "VALUES (:s, 'user', 'PUBLIC', 'dark') RETURNING id",
                s=f"sibling-{secrets.token_urlsafe(6)}",
            )[0][0]
        )
    yield made[0], made[1]
    for owner in made:
        ids = [r[0] for r in _sql("SELECT id FROM attachments WHERE owner_id = :o", o=owner)]
        for aid in ids:
            _sql("DELETE FROM media_jobs WHERE attachment_id = :a", a=aid)
        _sql("DELETE FROM attachments WHERE owner_id = :o", o=owner)
        _sql("DELETE FROM users WHERE id = :o", o=owner)


@pytest.fixture
def enabled(monkeypatch, owners):
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://img-manager.test:8000")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "tok-never-logged")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", str(owners[0]))
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    return owners


def _seed(
    owner: int,
    *,
    filename: str = "_ARA8207 - screen.png",
    mime: str = "image/png",
    capture_instant: datetime | None = None,
    capture_civil: str | None = None,
    capture_source: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_source: str | None = "none",
    storage_path: str = "/nonexistent",
    sha256: str | None = None,
    job_state: str = "processing",
    created_at: datetime = UPLOADED_AT,
    captured_at: datetime | None = None,
    memo_created_at: datetime | None = None,
) -> tuple[int, int]:
    # The upload instant defaults to two days after SIBLING_DATE so every candidate below is
    # PLAUSIBLE (MEMNG-843) unless a test moves one of the time anchors on purpose.
    memo_id = None
    if memo_created_at is not None:
        memo_id = _sql(
            "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at,"
            " updated_at) VALUES (:u, :o, 'm', CAST('PUBLIC' AS visibility), false, :t, :t)"
            " RETURNING id",
            u=secrets.token_urlsafe(10),
            o=owner,
            t=memo_created_at,
        )[0][0]
    aid = _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status,"
        " storage_path, sha256, capture_instant, capture_civil, capture_source, latitude,"
        " longitude, location_source, created_at, captured_at) VALUES (:u, :o, :mid, :f, :m, 1,"
        " 'ready', :sp, :sha, :ci, :cc, :cs, :la, :lo, :ls, :ca, :cap) RETURNING id",
        u=secrets.token_urlsafe(10),
        o=owner,
        mid=memo_id,
        f=filename,
        m=mime,
        sp=storage_path,
        sha=sha256,
        ci=capture_instant,
        cc=capture_civil,
        cs=capture_source,
        la=latitude,
        lo=longitude,
        ls=location_source,
        ca=created_at,
        cap=captured_at,
    )[0][0]
    jid = _sql(
        "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct)"
        " VALUES (:a, 'fast', 'sibling', CAST(:s AS job_state), 0, 0) RETURNING id",
        a=aid,
        s=job_state,
    )[0][0]
    return aid, jid


def _row(aid: int) -> dict:
    rows = _sql(
        "SELECT capture_instant, capture_civil, capture_source, latitude, longitude,"
        " location_source, sibling_outcome, captured_at, sha256 FROM attachments WHERE id = :a",
        a=aid,
    )
    return dict(rows[0]._mapping) if rows else {}


def _cand(**over) -> Candidate:
    values = {
        "image_id": 1,
        "datetime_original": SIBLING_DATE,
        "gps_latitude": SIBLING_LAT,
        "gps_longitude": SIBLING_LON,
        "is_pick": True,
    }
    values.update(over)
    return Candidate(**values)


def _spy(monkeypatch, answer):
    """Replace img-manager with a recorder. `answer` is the per-variant candidate lists (never
    truncated), or an exception."""
    calls: list[dict] = []

    async def fake_lookup(stems):
        calls.append({"stems": list(stems)})
        if isinstance(answer, BaseException):
            raise answer
        return [StemResult(list(found)) for found in answer]

    monkeypatch.setattr(sibling, "lookup", fake_lookup)
    return calls


async def _process(jid: int) -> None:
    async with async_session() as db:
        job = await db.get(MediaJob, jid)
        assert job is not None
        await db.commit()
        await sibling.process_sibling_job(db, job)
        await db.commit()


def test_a_unique_sibling_fills_both_null_columns_and_records_their_source(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert row["capture_instant"] == SIBLING_DATE
    assert row["capture_civil"] is None
    assert row["capture_source"] == "sibling"
    assert (row["latitude"], row["longitude"]) == (SIBLING_LAT, SIBLING_LON)
    assert row["location_source"] == "sibling"
    assert row["sibling_outcome"] == "unique"


def test_an_exif_instant_is_never_overwritten_while_a_missing_location_is_filled(
    enabled, monkeypatch
):
    allowed, _ = enabled
    aid, jid = _seed(allowed, capture_instant=EXIF_DATE, capture_source="exif")
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert row["capture_instant"] == EXIF_DATE
    assert row["capture_source"] == "exif"
    assert (row["latitude"], row["longitude"]) == (SIBLING_LAT, SIBLING_LON)
    assert row["location_source"] == "sibling"
    assert row["sibling_outcome"] == "unique"


def test_a_civil_only_capture_is_never_overwritten_while_a_missing_location_is_filled(
    enabled, monkeypatch
):
    allowed, _ = enabled
    aid, jid = _seed(allowed, capture_civil="2026-01-01T13:00:00", capture_source="exif")
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert row["capture_instant"] is None
    assert row["capture_civil"] == "2026-01-01T13:00:00"
    assert row["capture_source"] == "exif"
    assert (row["latitude"], row["longitude"]) == (SIBLING_LAT, SIBLING_LON)
    assert row["location_source"] == "sibling"


def test_a_file_provided_location_is_never_overwritten(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed, latitude=1.5, longitude=2.5, location_source="exif")
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert (row["latitude"], row["longitude"], row["location_source"]) == (1.5, 2.5, "exif")
    assert row["capture_instant"] == SIBLING_DATE  # the date the file lacked IS filled
    assert row["capture_source"] == "sibling"


def test_a_unique_sibling_with_no_date_and_no_gps_names_no_source(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    calls = _spy(
        monkeypatch, [[_cand(datetime_original=None, gps_latitude=None, gps_longitude=None)]]
    )

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert row["capture_instant"] is None and row["capture_source"] is None
    assert row["latitude"] is None and row["location_source"] == "none"
    assert row["sibling_outcome"] == "unique"


def test_ambiguous_writes_only_the_outcome(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    before = _row(aid)
    # Two RAWs that disagree on the place: agreeing ones would be a consensus (MEMNG-797).
    calls = _spy(
        monkeypatch,
        [[_cand(image_id=1, is_pick=False), _cand(image_id=2, is_pick=False, gps_latitude=1.0)]],
    )

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    after = _row(aid)
    assert after.pop("sibling_outcome") == "ambiguous"
    before.pop("sibling_outcome")
    assert after == before


def test_two_agreeing_candidates_fill_the_row_as_a_consensus(enabled, monkeypatch):
    # The same RAW in both img-manager tiers (MEMNG-797): no pick, same second, same place.
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    calls = _spy(
        monkeypatch, [[_cand(image_id=1, is_pick=False), _cand(image_id=2, is_pick=False)]]
    )

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert row["capture_instant"] == SIBLING_DATE
    assert row["capture_source"] == "sibling"
    assert (row["latitude"], row["longitude"]) == (SIBLING_LAT, SIBLING_LON)
    assert row["location_source"] == "sibling"
    assert row["sibling_outcome"] == "consensus"


def test_a_consensus_never_overwrites_an_exif_instant_while_a_missing_location_is_filled(
    enabled, monkeypatch
):
    allowed, _ = enabled
    aid, jid = _seed(allowed, capture_instant=EXIF_DATE, capture_source="exif")
    calls = _spy(
        monkeypatch, [[_cand(image_id=1, is_pick=False), _cand(image_id=2, is_pick=False)]]
    )

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    row = _row(aid)
    assert (row["capture_instant"], row["capture_source"]) == (EXIF_DATE, "exif")
    assert (row["latitude"], row["longitude"]) == (SIBLING_LAT, SIBLING_LON)
    assert row["location_source"] == "sibling"
    assert row["sibling_outcome"] == "consensus"


def test_every_variant_travels_in_one_request_and_the_first_with_candidates_elects(
    enabled, monkeypatch
):
    allowed, _ = enabled
    aid, jid = _seed(allowed, filename="_AR5123-Edit - web.png")
    calls = _spy(monkeypatch, [[], [_cand()]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_AR5123-Edit", "_AR5123"]}]
    assert _row(aid)["sibling_outcome"] == "unique"


def test_the_first_variant_with_candidates_decides_even_when_it_is_ambiguous(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed, filename="_AR5123-Edit - web.png")
    two = [_cand(image_id=1, is_pick=False), _cand(image_id=2, is_pick=False, gps_latitude=1.0)]
    calls = _spy(monkeypatch, [two, [_cand(image_id=3)]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_AR5123-Edit", "_AR5123"]}]
    row = _row(aid)
    assert row["sibling_outcome"] == "ambiguous"
    assert row["capture_instant"] is None


def test_no_candidates_records_none(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    calls = _spy(monkeypatch, [[]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    assert _row(aid)["sibling_outcome"] == "none"


# --- MEMNG-843: a stem is not an identity. iPhone `IMG_####` and Sony `_AR5####` counters repeat
# across cameras and years, so a candidate whose capture time the attachment's own timeline rules
# out is not this file's RAW. The shapes below are the production rows that borrowed wrongly.
MAY_POST = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)
AUG_EXPORT = datetime(2026, 8, 12, 20, 30, 6, tzinfo=UTC)
AUG_POST = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)


def _assert_nothing_borrowed(row: dict) -> None:
    assert row["sibling_outcome"] == "none"
    assert row["capture_instant"] is None and row["capture_source"] is None
    assert row["latitude"] is None and row["location_source"] == "none"


def test_an_iphone_counter_matching_a_frame_years_older_than_the_memo_borrows_nothing(
    enabled, monkeypatch
):
    # Rows 243/244/251: memo of 2026-05-12, IMG_9052 matched a 2011 frame.
    allowed, _ = enabled
    aid, jid = _seed(
        allowed,
        filename="IMG_9052.png",
        created_at=MAY_POST,
        captured_at=MAY_POST - timedelta(minutes=10),
        memo_created_at=MAY_POST,
    )
    _spy(monkeypatch, [[_cand(datetime_original=datetime(2011, 2, 28, 12, 0, tzinfo=UTC))]])

    asyncio.run(_process(jid))

    _assert_nothing_borrowed(_row(aid))


def test_a_raw_captured_after_the_export_existed_borrows_nothing(enabled, monkeypatch):
    # Row 1280: the export's own file time is 2026-08-12 20:30:06 UTC; the candidate was shot
    # three days later, which no RAW of this export can have been. Inside any window around the
    # memo, so only the ceiling refuses it.
    allowed, _ = enabled
    aid, jid = _seed(
        allowed,
        filename="_AR59735.png",
        created_at=AUG_POST,
        captured_at=AUG_EXPORT,
        memo_created_at=AUG_POST,
    )
    _spy(monkeypatch, [[_cand(datetime_original=datetime(2026, 8, 15, 17, 0, 12, tzinfo=UTC))]])

    asyncio.run(_process(jid))

    _assert_nothing_borrowed(_row(aid))


def test_a_raw_months_before_the_memo_borrows_nothing(enabled, monkeypatch):
    # Row 1257: `_AR59974` of a 2026-08-12 shoot matched another shoot's 2026-04-05 frame.
    allowed, _ = enabled
    aid, jid = _seed(
        allowed,
        filename="_AR59974.png",
        created_at=AUG_POST,
        captured_at=AUG_EXPORT,
        memo_created_at=AUG_POST,
    )
    _spy(monkeypatch, [[_cand(datetime_original=datetime(2026, 4, 5, 11, 0, tzinfo=UTC))]])

    asyncio.run(_process(jid))

    _assert_nothing_borrowed(_row(aid))


def test_a_refused_pick_leaves_the_one_plausible_candidate_to_win(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed, filename="IMG_9052.png", memo_created_at=UPLOADED_AT)
    old_pick = _cand(image_id=1, is_pick=True, datetime_original=datetime(2011, 2, 28, tzinfo=UTC))
    right = _cand(image_id=2, is_pick=False)
    _spy(monkeypatch, [[old_pick, right]])

    asyncio.run(_process(jid))

    row = _row(aid)
    assert row["sibling_outcome"] == "unique"
    assert row["capture_instant"] == SIBLING_DATE


def test_a_variant_whose_candidates_are_all_refused_falls_through_to_the_next(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed, filename="_AR5123-Edit - web.png", memo_created_at=UPLOADED_AT)
    stale = _cand(image_id=1, datetime_original=datetime(2016, 7, 2, tzinfo=UTC))
    calls = _spy(monkeypatch, [[stale], [_cand(image_id=2)]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_AR5123-Edit", "_AR5123"]}]
    row = _row(aid)
    assert row["sibling_outcome"] == "unique"
    assert row["capture_instant"] == SIBLING_DATE


def test_an_old_photo_posted_on_a_backdated_memo_still_borrows(enabled, monkeypatch):
    # The window is anchored on the MEMO, not on the upload: a 2016 RAW exported and posted in 2026
    # onto a memo dated 2016 is exactly what the memo says it is.
    allowed, _ = enabled
    old = datetime(2016, 7, 2, 15, 0, tzinfo=UTC)
    aid, jid = _seed(
        allowed,
        filename="_AR51234.png",
        created_at=MAY_POST,
        captured_at=MAY_POST,
        memo_created_at=old + timedelta(days=1),
    )
    _spy(monkeypatch, [[_cand(datetime_original=old)]])

    asyncio.run(_process(jid))

    row = _row(aid)
    assert row["sibling_outcome"] == "unique"
    assert row["capture_instant"] == old
    assert row["capture_source"] == "sibling"


def test_an_owner_outside_the_fence_makes_no_request_and_gets_none(enabled, monkeypatch):
    _, other = enabled
    aid, jid = _seed(other)
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == []
    row = _row(aid)
    assert row["sibling_outcome"] == "none"
    assert row["capture_instant"] is None and row["latitude"] is None


@pytest.mark.parametrize(
    "seed",
    [
        {"mime": "video/mp4", "filename": "_AR1.mp4"},
        {
            "capture_instant": EXIF_DATE,
            "capture_source": "exif",
            "latitude": 1.0,
            "longitude": 2.0,
            "location_source": "exif",
        },
        {"filename": " - screen.png"},
    ],
    ids=["not-an-image", "nothing-fillable", "no-stem"],
)
def test_nothing_to_ask_makes_no_request_and_records_none(enabled, monkeypatch, seed):
    allowed, _ = enabled
    aid, jid = _seed(allowed, **seed)
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == []
    assert _row(aid)["sibling_outcome"] == "none"


def test_disabled_at_processing_time_makes_no_request_and_raises(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    calls = _spy(monkeypatch, [[_cand()]])
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "")

    with pytest.raises(SiblingUnavailable):
        asyncio.run(_process(jid))

    assert calls == []
    assert _row(aid)["sibling_outcome"] is None


def test_no_transaction_is_open_while_img_manager_is_asked(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    seen: list[bool] = []

    async def go():
        async with async_session() as db:
            job = await db.get(MediaJob, jid)
            assert job is not None
            await db.commit()

            async def fake_lookup(stems):
                seen.append(db.in_transaction())
                return [StemResult([_cand()])]

            monkeypatch.setattr(sibling, "lookup", fake_lookup)
            await sibling.process_sibling_job(db, job)
            await db.commit()

    asyncio.run(go())

    assert seen == [False]
    assert _row(aid)["sibling_outcome"] == "unique"


def test_unavailable_is_requeued_with_one_attempt_by_the_real_failure_path(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed, job_state="queued")
    before = _row(aid)
    calls = _spy(monkeypatch, SiblingUnavailable("img-manager answered 503"))

    async def go():
        async with async_session() as db:
            return await run_once(db)

    assert asyncio.run(go()) == "deferred"

    assert calls == [{"stems": ["_ARA8207"]}]
    state, attempts, error = _sql(
        "SELECT state::text, attempts, error FROM media_jobs WHERE id = :j", j=jid
    )[0]
    assert (state, attempts) == ("queued", 1)
    assert error.startswith("SiblingUnavailable")
    assert _row(aid) == before  # every column, the outcome included, untouched


def test_disabled_through_the_worker_is_requeued_never_done(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed, job_state="queued")
    calls = _spy(monkeypatch, [[_cand()]])
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "")

    async def go():
        async with async_session() as db:
            return await run_once(db)

    assert asyncio.run(go()) == "deferred"
    assert calls == []
    assert _sql("SELECT state::text, attempts FROM media_jobs WHERE id = :j", j=jid)[0] == (
        "queued",
        1,
    )
    assert _row(aid)["sibling_outcome"] is None


def test_the_original_bytes_and_sha256_are_identical_after_the_job(enabled, monkeypatch, tmp_path):
    allowed, _ = enabled
    original = tmp_path / "_ARA8207 - screen.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buf, format="PNG")
    original.write_bytes(buf.getvalue())
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    aid, jid = _seed(allowed, storage_path=str(original), sha256=digest)
    calls = _spy(monkeypatch, [[_cand()]])

    asyncio.run(_process(jid))

    assert calls == [{"stems": ["_ARA8207"]}]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert original.read_bytes() == buf.getvalue()
    row = _row(aid)
    assert row["sha256"] == digest and row["sibling_outcome"] == "unique"


def test_no_log_line_carries_a_date_a_coordinate_a_stem_a_filename_or_the_token(
    enabled, monkeypatch
):
    allowed, _ = enabled
    _, jid = _seed(allowed)
    _spy(monkeypatch, [[_cand()]])

    with capture_logs() as logs:
        asyncio.run(_process(jid))

    resolved = [entry for entry in logs if entry["event"] == "sibling.resolved"]
    assert resolved and resolved[0]["outcome"] == "unique"
    assert resolved[0]["stem_variants"] == 1 and resolved[0]["candidates"] == 1
    flat = repr(logs)
    for leak in ("_ARA8207", "screen", "2023", "46.12", "7.65", "tok-never-logged"):
        assert leak not in flat, leak


def test_a_reclaim_during_the_lookup_leaves_the_write_nothing_and_the_job_discarded(
    enabled, monkeypatch, tmp_path
):
    """The lookup holds no lock, so a reclaim can delete the job AND the attachment while
    img-manager is being asked. The write must find them gone, write nothing, and hand run_once
    the AttachmentGone that discards the job — not a `done` update against a deleted row."""
    allowed, _ = enabled
    aid, jid = _seed(allowed, job_state="queued", storage_path=str(tmp_path / "gone.png"))

    async def fake_lookup(stems):
        async with async_session() as other:
            result = await reclaim.reclaim_attachment(
                other, aid, reclaim.Revalidate.IDENTITY, unlink=lambda paths: []
            )
        assert result.outcome == "reclaimed"
        return [StemResult([_cand()])]

    monkeypatch.setattr(sibling, "lookup", fake_lookup)

    async def go():
        async with async_session() as db:
            return await run_once(db)

    assert asyncio.run(go()) == "processed"
    assert _sql("SELECT id FROM attachments WHERE id = :a", a=aid) == []
    assert _sql("SELECT id FROM media_jobs WHERE id = :j", j=jid) == []


def test_the_result_write_and_a_concurrent_reclaim_never_deadlock(enabled, monkeypatch, tmp_path):
    """Two connections: B (the result write) has taken its FIRST lock; A (the reclaim deletion
    path) then runs. B locks the job row before the attachment — the order reclaim and
    guard_publish use — so A skips B's job row, takes the attachment and deletes it, and B finds
    it gone. Taken the other way round (attachment first) A would hold the job row while waiting
    for B's attachment and B the attachment while waiting for A's job row: SQLSTATE 40P01."""
    allowed, _ = enabled
    aid, jid = _seed(allowed, storage_path=str(tmp_path / "race.png"))
    _spy(monkeypatch, [[_cand()]])
    a_in_unlink = threading.Event()
    release_a = threading.Event()

    def held_unlink(paths):
        a_in_unlink.set()
        release_a.wait(10)
        return []

    async def go():
        b_holds_first = asyncio.Event()
        release_b = asyncio.Event()
        async with async_session() as db_b, async_session() as db_a:
            job = await db_b.get(MediaJob, jid)
            assert job is not None
            await db_b.commit()
            real_execute = db_b.execute
            locks = 0

            async def pausing_execute(statement, *args, **kwargs):
                nonlocal locks
                result = await real_execute(statement, *args, **kwargs)
                if getattr(statement, "_for_update_arg", None) is not None:
                    locks += 1
                    if locks == 1:
                        b_holds_first.set()
                        await release_b.wait()
                return result

            monkeypatch.setattr(db_b, "execute", pausing_execute)
            b = asyncio.create_task(sibling.process_sibling_job(db_b, job))
            await asyncio.wait_for(b_holds_first.wait(), 10)
            a = asyncio.create_task(
                reclaim.reclaim_attachment(
                    db_a, aid, reclaim.Revalidate.IDENTITY, unlink=held_unlink
                )
            )
            reached = await to_thread.run_sync(a_in_unlink.wait, 3)
            release_b.set()
            await asyncio.sleep(0.3)  # B is now queued behind A's attachment lock
            release_a.set()
            outcome = await asyncio.gather(a, b, return_exceptions=True)
            await db_b.rollback()
            return reached, outcome

    reached, (a_result, b_result) = asyncio.run(go())

    for result in (a_result, b_result):
        if isinstance(result, DBAPIError):
            pytest.fail(f"lock cycle: {getattr(result.orig, 'sqlstate', None)} {result}")
    assert reached, "the reclaim was blocked by the result write's first lock"
    assert isinstance(a_result, reclaim.ReclaimResult) and a_result.outcome == "reclaimed"
    assert isinstance(b_result, media.AttachmentGone)
    assert _sql("SELECT id FROM attachments WHERE id = :a", a=aid) == []


async def test_process_job_dispatches_a_sibling_job_to_the_sibling_path(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    routed: list[int] = []

    async def fake_sibling(db, job):
        routed.append(job.id)

    def no_derive(attachment):
        raise AssertionError("a sibling job must never reach the derive path")

    monkeypatch.setattr(sibling, "process_sibling_job", fake_sibling)
    monkeypatch.setattr(media, "process_image", no_derive)
    monkeypatch.setattr(media, "process_video_poster", no_derive)

    async with async_session() as db:
        job = await db.get(MediaJob, jid)
        assert job is not None
        await media.process_job(db, job)
        await db.rollback()

    assert routed == [jid]


# --- finalize enqueue --------------------------------------------------------------------------


def _png_without_metadata() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(buf, format="PNG")
    return buf.getvalue()


def _heic_with_date_and_gps() -> bytes:
    import pillow_heif

    pillow_heif.register_heif_opener()
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = "2025:05:06 07:08:09"
    ifd[0x9011] = "+02:00"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (12.0, 30.0, 0.0), "E", (45.0, 15.0, 0.0)
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buf, format="HEIF", exif=exif.tobytes())
    return buf.getvalue()


def _post_memo_with(client, name: str, mime: str, data: bytes) -> int:
    opened = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    created = client.post(
        "/api/memos", json={"visibility": "PRIVATE", "content": name, "upload_uids": [uid]}
    )
    assert created.status_code == 201, created.text
    att_uid = created.json()["attachments"][0]["uid"]
    return _sql("SELECT id FROM attachments WHERE uid = :u", u=att_uid)[0][0]


def _jobs(aid: int) -> list[tuple[int, str, str, str]]:
    return [
        tuple(r)
        for r in _sql(
            "SELECT id, kind, lane::text, state::text FROM media_jobs WHERE attachment_id = :a"
            " ORDER BY id",
            a=aid,
        )
    ]


@pytest.fixture
def session_owner(logged_in_client, monkeypatch, tmp_path):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    owner = _sql("SELECT id FROM users WHERE subject = :s", s=subject)[0][0]
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://img-manager.test:8000")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "tok")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", str(owner))
    yield client, owner
    for (aid,) in _sql("SELECT id FROM attachments WHERE owner_id = :o", o=owner):
        _sql("DELETE FROM media_jobs WHERE attachment_id = :a", a=aid)
    _sql("DELETE FROM attachments WHERE owner_id = :o", o=owner)
    _sql("DELETE FROM memos WHERE owner_id = :o", o=owner)


def test_finalize_enqueues_derive_then_sibling_for_an_undated_png(session_owner):
    client, _ = session_owner
    aid = _post_memo_with(client, "_ARA8207 - screen.png", "image/png", _png_without_metadata())
    jobs = _jobs(aid)
    assert [(kind, lane, state) for _, kind, lane, state in jobs] == [
        ("derive", "fast", "queued"),
        ("sibling", "fast", "queued"),
    ]
    assert jobs[0][0] < jobs[1][0]


def test_finalize_enqueues_no_sibling_for_a_heic_with_date_and_gps(session_owner):
    client, _ = session_owner
    aid = _post_memo_with(client, "IMG_0001.HEIC", "image/heic", _heic_with_date_and_gps())
    row = _row(aid)
    assert row["capture_source"] == "exif" and row["location_source"] == "exif"  # premise
    assert [kind for _, kind, _, _ in _jobs(aid)] == ["derive"]


def test_finalize_enqueues_no_sibling_when_img_manager_is_not_configured(
    session_owner, monkeypatch
):
    client, _ = session_owner
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "")
    aid = _post_memo_with(client, "_AR1.png", "image/png", _png_without_metadata())
    assert [kind for _, kind, _, _ in _jobs(aid)] == ["derive"]


def test_finalize_enqueues_no_sibling_for_an_owner_outside_the_fence(session_owner, monkeypatch):
    client, owner = session_owner
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", str(owner + 1000))
    aid = _post_memo_with(client, "_AR2.png", "image/png", _png_without_metadata())
    assert [kind for _, kind, _, _ in _jobs(aid)] == ["derive"]


def _through_the_real_lookup(monkeypatch, body: dict) -> None:
    """img-manager answers `body`; the job's own lookup parses it. Only the transport is stubbed."""
    import httpx

    real_lookup = sibling.lookup
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))

    async def via_transport(stems):
        return await real_lookup(stems, transport=transport)

    monkeypatch.setattr(sibling, "lookup", via_transport)


def _hundred_with_one_pick(truncated: bool) -> dict:
    candidates = [
        {
            "image_id": i,
            "folder_path": "/raw",
            "filename": f"_AR{i}.ARW",
            "datetime_original": "2023-07-14T09:30:15Z",
            "gps_latitude": SIBLING_LAT,
            "gps_longitude": SIBLING_LON,
            "is_pick": i == 1,
        }
        for i in range(1, 101)
    ]
    return {"results": [{"stem": "_ARA8207", "candidates": candidates, "truncated": truncated}]}


def test_a_truncated_candidate_list_is_ambiguous_even_with_one_pick(enabled, monkeypatch):
    # The pick is first only because img-manager orders picks first; a truncated list cannot
    # prove there is no second pick past the cap, so the client must not believe it.
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    _through_the_real_lookup(monkeypatch, _hundred_with_one_pick(truncated=True))

    asyncio.run(_process(jid))

    row = _row(aid)
    assert row["sibling_outcome"] == "ambiguous"
    assert row["capture_instant"] is None and row["capture_source"] is None
    assert row["latitude"] is None and row["location_source"] == "none"


def test_the_same_list_untruncated_elects_its_one_pick(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    _through_the_real_lookup(monkeypatch, _hundred_with_one_pick(truncated=False))

    asyncio.run(_process(jid))

    row = _row(aid)
    assert row["sibling_outcome"] == "unique"
    assert row["capture_instant"] == SIBLING_DATE


def test_a_candidates_mapping_is_unavailable_and_writes_nothing(enabled, monkeypatch):
    allowed, _ = enabled
    aid, jid = _seed(allowed)
    before = _row(aid)
    _through_the_real_lookup(
        monkeypatch, {"results": [{"stem": "_ARA8207", "candidates": {}, "truncated": False}]}
    )

    with pytest.raises(SiblingUnavailable):
        asyncio.run(_process(jid))

    assert _row(aid) == before
