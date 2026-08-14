"""The photo-book export (MEMNG-49 Phase B): selection, print verdicts, and the archive.

The dpi tests are the heart of this file and they are deliberately written the hard way: every
expected number is computed from the geometry BY HAND in the test (`px * 2.54 / printed_cm`), never
by calling the implementation's own formula. A test that reused `effective_dpi` to predict
`effective_dpi` would pass no matter how wrong the placement model was, which is the one thing
worth guarding here.
"""

import io
import json
import os
import time
import tracemalloc
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from app.config import settings
from app.models import JobKind
from app.services import photobook, printready
from app.services.export import stream_zip
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

BOOK = photobook.geometry("21x21", 1.5, 300)  # the plan's 21x21 cm layflat


# --------------------------------------------------------------------------------------------
# geometry parsing
# --------------------------------------------------------------------------------------------


def test_page_cm_parses_the_default_book():
    assert photobook.parse_page_cm("21x21") == (21.0, 21.0)


def test_page_cm_accepts_a_rectangular_page_and_decimals():
    assert photobook.parse_page_cm("29.7x21") == (29.7, 21.0)


@pytest.mark.parametrize("value", ["21", "21x", "x21", "21*21", "", "twentyx21", "-21x21", "0x21"])
def test_page_cm_rejects_nonsense(value):
    with pytest.raises(photobook.GeometryError):
        photobook.parse_page_cm(value)


def test_page_cm_rejects_an_absurd_page():
    with pytest.raises(photobook.GeometryError):
        photobook.parse_page_cm("5000x5000")


@pytest.mark.parametrize("max_pages", [0, -1, 11])
def test_geometry_rejects_an_impossible_spread(max_pages):
    with pytest.raises(photobook.GeometryError):
        photobook.geometry("21x21", max_pages, 300)


@pytest.mark.parametrize("dpi", [0, -300, 5000])
def test_geometry_rejects_an_impossible_dpi(dpi):
    with pytest.raises(photobook.GeometryError):
        photobook.geometry("21x21", 1.5, dpi)


# --------------------------------------------------------------------------------------------
# effective dpi — every expectation computed by hand from the geometry
# --------------------------------------------------------------------------------------------


def test_effective_dpi_reproduces_the_plans_anchor():
    """The plan's own worked case: a 3:2 photo at full page height on a 21x21 layflat spans
    1.5 pages = 31.5 cm, and 3720 px across 31.5 cm is the 300 dpi the rung was sized to clear."""
    printed_cm = 31.5  # 21 cm page x 1.5 pages, by hand
    expected = 3720 * 2.54 / printed_cm  # 299.96...

    assert photobook.effective_dpi(3720, 2480, BOOK) == pytest.approx(expected)
    assert round(expected) == 300


def test_effective_dpi_on_a_clean_hand_computed_case():
    """4000 px across a 20 cm print is exactly 508 dpi (4000 x 2.54 / 20), no rounding involved.

    Geometry by hand: a 10x10 cm page at max_pages=2 gives a 20x10 cm box; the photo is 2:1, so
    10 cm of height wants 20 cm of width — exactly the box. Printed width = 20 cm.
    """
    geom = photobook.geometry("10x10", 2.0, 300)

    assert photobook.effective_dpi(4000, 2000, geom) == pytest.approx(508.0)


def test_a_square_photo_is_bound_by_the_page_height_not_the_spread():
    """A square photo cannot use the 1.5-page width: at full page height it is only 21 cm wide,
    so it fills one page. dpi = 3000 x 2.54 / 21 (by hand) = 362.857..."""
    assert photobook.effective_dpi(3000, 3000, BOOK) == pytest.approx(3000 * 2.54 / 21.0)


def test_a_panorama_is_bound_by_the_spread_width():
    """A 3:1 panorama at full page height would want 63 cm; the 1.5-page cap holds it to 31.5 cm,
    so it prints shorter than the page. dpi = 6000 x 2.54 / 31.5 (by hand)."""
    assert photobook.effective_dpi(6000, 2000, BOOK) == pytest.approx(6000 * 2.54 / 31.5)


def test_max_pages_is_a_cap_not_a_multiplier():
    """Raising max_pages does NOT stretch a photo that already fits: a 3:2 photo is height-bound
    at 1.5 pages, so allowing 3 pages leaves its printed size — and its dpi — unchanged.

    Both sides are pinned to the plan's hand-computed anchor (3720 px over 31.5 cm) rather than to
    each other: comparing one call against another would pass identically if the formula were
    wrong in both.
    """
    anchor = 3720 * 2.54 / 31.5  # = 299.96..., the plan's 300 dpi target
    wider = photobook.geometry("21x21", 3.0, 300)

    assert photobook.effective_dpi(3720, 2480, BOOK) == pytest.approx(anchor)
    assert photobook.effective_dpi(3720, 2480, wider) == pytest.approx(anchor)


def test_lowering_max_pages_really_does_shrink_the_print_and_raise_the_dpi():
    """The other direction, so the cap is not merely inert: at 1.0 pages the same 3:2 photo is
    width-bound at 21 cm instead of 31.5, and 3720 x 2.54 / 21 (by hand) is the dpi."""
    single = photobook.geometry("21x21", 1.0, 300)

    assert photobook.effective_dpi(3720, 2480, single) == pytest.approx(3720 * 2.54 / 21.0)


@pytest.mark.parametrize("px", [(None, None), (0, 100), (100, 0), (None, 100)])
def test_effective_dpi_is_unknown_without_dimensions(px):
    assert photobook.effective_dpi(px[0], px[1], BOOK) is None


# --------------------------------------------------------------------------------------------
# the rung projection
# --------------------------------------------------------------------------------------------


def test_rung_caps_the_long_edge_of_an_oversized_master():
    assert photobook.rung_px(9504, 6336) == (4096, 2731)  # 6336 x 4096 / 9504 = 2730.6 -> 2731


def test_rung_caps_a_portrait_master_on_its_long_edge():
    assert photobook.rung_px(6336, 9504) == (2731, 4096)


def test_rung_never_upscales_a_small_source():
    assert photobook.rung_px(800, 600) == (800, 600)


def test_rung_is_unknown_without_source_dimensions():
    assert photobook.rung_px(None, None) is None


# --------------------------------------------------------------------------------------------
# filenames
# --------------------------------------------------------------------------------------------


def test_filename_carries_sequence_date_and_uid():
    name = photobook.photobook_filename(1, date(2026, 1, 31), "abc123")

    assert name == "0001_2026-01-31_abc123.jpg"


def test_filenames_sort_into_book_order_as_text():
    names = [photobook.photobook_filename(i, date(2026, 1, 31), "u") for i in (1, 2, 10, 100)]

    assert names == sorted(names)  # zero padding is what makes 10 come after 2


def test_filename_neutralises_a_uid_that_would_escape_the_archive():
    name = photobook.photobook_filename(1, date(2026, 1, 31), "../../etc/passwd")

    assert "/" not in name and ".." not in Path(name).parts


# --------------------------------------------------------------------------------------------
# DB fixtures
# --------------------------------------------------------------------------------------------


def _memo(client, content="a photo #curated", visibility="PUBLIC") -> dict:
    r = client.post("/api/memos", json={"content": content, "visibility": visibility})
    assert r.status_code == 201
    return r.json()


def _engine():
    return create_engine(SYNC_URL)


def _set_created(memo_uid: str, when: datetime) -> None:
    eng = _engine()
    with eng.begin() as c:
        c.execute(
            text("UPDATE memos SET created_at = :w WHERE uid = :u"), {"w": when, "u": memo_uid}
        )
    eng.dispose()


def _attach(
    memo_uid: str,
    att_uid: str,
    subject: str,
    *,
    storage_path: Path | str,
    mime: str = "image/jpeg",
    width: int | None = None,
    height: int | None = None,
    captured_at: datetime | None = None,
) -> int:
    eng = _engine()
    with eng.begin() as c:
        oid = c.execute(text("SELECT id FROM users WHERE subject=:s"), {"s": subject}).scalar_one()
        mid = c.execute(text("SELECT id FROM memos WHERE uid=:u"), {"u": memo_uid}).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status,"
                " storage_path, width, height, captured_at) VALUES"
                " (:u,:o,:m,:f,:mi,:sz,CAST('ready' AS attachment_status),:sp,:w,:h,:ca)"
                " RETURNING id"
            ),
            {
                "u": att_uid,
                "o": oid,
                "m": mid,
                "f": f"{att_uid}.jpg",
                "mi": mime,
                "sz": 1,
                "sp": str(storage_path),
                "w": width,
                "h": height,
                "ca": captured_at,
            },
        ).scalar_one()
    eng.dispose()
    return aid


def _print_master(att_id: int, storage_path: Path) -> None:
    eng = _engine()
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO attachment_renditions"
                " (attachment_id, variant_key, filename, mime, size, storage_path)"
                " VALUES (:a, 'print:glossy', 'p.jpg', 'image/jpeg', 1, :sp)"
            ),
            {"a": att_id, "sp": str(storage_path)},
        )
    eng.dispose()


def _job(att_id: int, state: str, kind: str = JobKind.printready) -> None:
    eng = _engine()
    with eng.begin() as c:
        c.execute(
            text(
                # attempts/progress_pct are ORM-level defaults with no server default, so a raw
                # INSERT has to supply them.
                "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct)"
                " VALUES (:a, CAST('slow' AS job_lane), :k, CAST(:s AS job_state), 3, 0)"
            ),
            {"a": att_id, "k": str(kind), "s": state},
        )
    eng.dispose()


def _jobs_for(att_id: int, kind: str = JobKind.printready) -> list[str]:
    eng = _engine()
    with eng.begin() as c:
        rows = c.execute(
            text(
                "SELECT state::text FROM media_jobs WHERE attachment_id=:a AND kind=:k"
                " ORDER BY id"
            ),
            {"a": att_id, "k": str(kind)},
        ).scalars().all()
    eng.dispose()
    return list(rows)


def _cleanup(*aids: int) -> None:
    eng = _engine()
    with eng.begin() as c:
        for aid in aids:
            c.execute(text("DELETE FROM media_jobs WHERE attachment_id=:a"), {"a": aid})
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


def _source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / "originals" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(os.urandom(2048))
    return src


def _rendition(tmp_path: Path, uid: str, size: tuple[int, int] = (400, 200)) -> Path:
    """A REAL JPEG at the printready path — the manifest measures this file, so it has to decode."""
    dest = Path(settings.DATA_ROOT) / "assets" / printready.SUBDIR / f"{uid}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 120, 200)).save(dest, "JPEG", quality=92)
    return dest


def _delete_jobs(att_id: int) -> None:
    eng = _engine()
    with eng.begin() as c:
        c.execute(text("DELETE FROM media_jobs WHERE attachment_id=:a"), {"a": att_id})
    eng.dispose()


def _job_updated_at(att_id: int) -> datetime:
    """The latest printready job's `updated_at` — what the staleness rule compares against."""
    eng = _engine()
    with eng.begin() as c:
        stamp = c.execute(
            text(
                "SELECT updated_at FROM media_jobs WHERE attachment_id=:a AND kind='printready'"
                " ORDER BY id DESC LIMIT 1"
            ),
            {"a": att_id},
        ).scalar_one()
    eng.dispose()
    return stamp


def _bump_ctime_past_job(source: Path, att_id: int) -> None:
    """Re-write `source` until its ctime strictly postdates the recorded failure.

    Established by looping, not by statement order: ctime is not settable, inode times come off a
    coarse clock (see _make_stale), and here a DATABASE clock is being compared against a
    FILESYSTEM one. Raises rather than proceeding, so the test can never degrade into asserting
    nothing.
    """
    failed_at = _job_updated_at(att_id).timestamp()
    for _ in range(500):
        source.write_bytes(os.urandom(2048))
        if source.stat().st_ctime > failed_at:
            return
        time.sleep(0.005)
    raise AssertionError("could not make the source newer than the recorded failure")


def _fail_after_source(source: Path, att_id: int) -> None:
    """Record a terminal failure whose timestamp is at or after the source's last change — the
    precondition for "this failure still describes the file on disk". The mirror image of
    _bump_ctime_past_job, established the same way and for the same two-clock reason."""
    for _ in range(500):
        _job(att_id, "failed")
        if _job_updated_at(att_id).timestamp() >= source.stat().st_ctime:
            return
        _delete_jobs(att_id)
        time.sleep(0.005)
    raise AssertionError("could not record a failure at or after the source's change time")


def _backdate_job_creation(att_id: int) -> None:
    """Move the job's `created_at` an hour before its `updated_at`, leaving `updated_at` alone.

    Recreates the ordinary case the two columns disagree about: a job enqueued long before it
    finally failed. A raw UPDATE does not fire SQLAlchemy's `onupdate`, so `updated_at` keeps the
    value it was recorded with.
    """
    eng = _engine()
    with eng.begin() as c:
        c.execute(
            text(
                "UPDATE media_jobs SET created_at = updated_at - interval '1 hour'"
                " WHERE attachment_id = :a AND kind = 'printready'"
            ),
            {"a": att_id},
        )
    eng.dispose()


def _real_jpeg(path: Path, size: tuple[int, int]) -> Path:
    """A decodable JPEG of exactly `size`: the manifest reads real headers, so stubs will not do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 60, 30)).save(path, "JPEG", quality=90)
    return path


def _make_stale(source: Path, rendition: Path) -> None:
    """Re-write `source` until its ctime is STRICTLY newer than `rendition`'s.

    Not a sleep-and-hope: ctime cannot be set from userland, and Linux stamps inode times from a
    coarse clock (~1-4 ms here), so two writes issued microseconds apart routinely land on the same
    tick — which is what made an ordering-only version of this setup flaky. Looping until the
    precondition actually holds makes the test deterministic, and failing loudly if it never does
    keeps it from silently degrading into a test of nothing.
    """
    for _ in range(500):
        source.write_bytes(os.urandom(2048))
        if source.stat().st_ctime > rendition.stat().st_ctime:
            return
        time.sleep(0.005)
    raise AssertionError("could not make the source newer than its rendition")


def _manifest(client, **params) -> dict:
    r = client.get("/api/exports/photobook", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------------------------
# auth + parameter validation
# --------------------------------------------------------------------------------------------


def test_photobook_requires_auth(client):
    assert client.get("/api/exports/photobook").status_code == 401


def test_photobook_zip_requires_auth_too(client):
    assert client.get("/api/exports/photobook?format=zip").status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"profile": "print-2560"},
        {"profile": ""},
        {"format": "tar"},
        {"page_cm": "21"},
        {"page_cm": "0x21"},
        {"page_cm": "twenty-one"},
        {"max_pages": 0},
        {"max_pages": -1},
        {"dpi": 0},
        {"dpi": 99999},
    ],
)
def test_photobook_rejects_bad_parameters(logged_in_client, params):
    client, _ = logged_in_client

    assert client.get("/api/exports/photobook", params=params).status_code == 422


def test_photobook_accepts_the_only_profile(logged_in_client):
    client, _ = logged_in_client

    body = _manifest(client, profile="print-4096")

    assert body["profile"] == "print-4096"


# --------------------------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------------------------


def test_selection_defaults_to_the_curated_tag(logged_in_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    curated = _memo(client, "in the book #curated")
    other = _memo(client, "not in the book #holiday")
    a1 = _attach(curated["uid"], "pb-sel-1", subject, storage_path=_source(tmp_path, "a.jpg"))
    a2 = _attach(other["uid"], "pb-sel-2", subject, storage_path=_source(tmp_path, "b.jpg"))
    try:
        body = _manifest(client)
    finally:
        _cleanup(a1, a2)

    assert [p["attachment"] for p in body["photos"]] == ["pb-sel-1"]
    assert body["tag"] == "curated"


def test_selection_follows_an_explicit_tag(logged_in_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    curated = _memo(client, "curated one #curated")
    holiday = _memo(client, "holiday one #holiday")
    a1 = _attach(curated["uid"], "pb-tag-1", subject, storage_path=_source(tmp_path, "a.jpg"))
    a2 = _attach(holiday["uid"], "pb-tag-2", subject, storage_path=_source(tmp_path, "b.jpg"))
    try:
        body = _manifest(client, tag="holiday")
    finally:
        _cleanup(a1, a2)

    assert [p["attachment"] for p in body["photos"]] == ["pb-tag-2"]
    assert body["tag"] == "holiday"


def test_selection_excludes_non_photo_attachments(logged_in_client, tmp_path, monkeypatch):
    """Mirrors the shipped curated filter: image/* only, and never an SVG or a video — the print
    encoder cannot decode either, so they must not enter the book at all."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "mixed media #curated")
    photo = _attach(memo["uid"], "pb-mime-jpg", subject, storage_path=_source(tmp_path, "a.jpg"))
    svg = _attach(
        memo["uid"], "pb-mime-svg", subject, storage_path=_source(tmp_path, "b.svg"),
        mime="image/svg+xml",
    )
    video = _attach(
        memo["uid"], "pb-mime-mp4", subject, storage_path=_source(tmp_path, "c.mp4"),
        mime="video/mp4",
    )
    try:
        body = _manifest(client)
    finally:
        _cleanup(photo, svg, video)

    assert [p["attachment"] for p in body["photos"]] == ["pb-mime-jpg"]


def test_selection_honours_inclusive_date_bounds(logged_in_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    aids = []
    for day, uid in ((10, "pb-d-10"), (20, "pb-d-20"), (30, "pb-d-30")):
        memo = _memo(client, f"day {day} #curated")
        _set_created(memo["uid"], datetime(2026, 3, day, 12, 0, tzinfo=UTC))
        src = _source(tmp_path, f"{uid}.jpg")
        aids.append(_attach(memo["uid"], uid, subject, storage_path=src))
    try:
        window = _manifest(client, **{"from": "2026-03-20", "to": "2026-03-30"})
        open_end = _manifest(client, **{"from": "2026-03-20"})
        open_start = _manifest(client, **{"to": "2026-03-10"})
    finally:
        _cleanup(*aids)

    # `to` is the whole day, so the memo posted at noon on the 30th is inside the range
    assert [p["attachment"] for p in window["photos"]] == ["pb-d-20", "pb-d-30"]
    assert [p["attachment"] for p in open_end["photos"]] == ["pb-d-20", "pb-d-30"]
    assert [p["attachment"] for p in open_start["photos"]] == ["pb-d-10"]


def test_another_owners_photos_are_never_selected(logged_in_client, tmp_path, monkeypatch):
    """A book is the owner's own material. The intruder memo is PUBLIC and #curated — visible in
    the feed, and still not a page in this user's book."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    mine = _memo(client, "mine #curated")
    a1 = _attach(mine["uid"], "pb-own-mine", subject, storage_path=_source(tmp_path, "a.jpg"))

    eng = _engine()
    with eng.begin() as c:
        other_id = c.execute(
            text("INSERT INTO users (subject, role) VALUES ('pb_intruder','user') RETURNING id")
        ).scalar_one()
        other_memo = c.execute(
            text(
                # `pinned` is an ORM-level default with no server default (see _job)
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned) VALUES"
                " ('pb-other-memo', :o, 'theirs #curated', CAST('PUBLIC' AS visibility), false)"
                " RETURNING id"
            ),
            {"o": other_id},
        ).scalar_one()
        tag_id = c.execute(text("SELECT id FROM tags WHERE name='curated'")).scalar_one()
        c.execute(
            text("INSERT INTO memo_tags (memo_id, tag_id) VALUES (:m, :t)"),
            {"m": other_memo, "t": tag_id},
        )
        a2 = c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status,"
                " storage_path) VALUES ('pb-own-theirs', :o, :m, 't.jpg', 'image/jpeg', 1,"
                " CAST('ready' AS attachment_status), :sp) RETURNING id"
            ),
            {"o": other_id, "m": other_memo, "sp": str(_source(tmp_path, "t.jpg"))},
        ).scalar_one()
    eng.dispose()

    try:
        body = _manifest(client)
    finally:
        _cleanup(a1, a2)
        eng = _engine()
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject='pb_intruder'"))
        eng.dispose()

    assert [p["attachment"] for p in body["photos"]] == ["pb-own-mine"]
    assert body["totals"]["selected"] == 1


# --------------------------------------------------------------------------------------------
# readiness + the manifest
# --------------------------------------------------------------------------------------------


def test_manifest_reports_a_ready_photo_from_its_real_rendition(
    logged_in_client, tmp_path, monkeypatch
):
    """The dpi must come from the file that exists, not from a projection of the source.

    The source is recorded as 8000x4000 (which would project to 4096x2048) while the rendition on
    disk is really 400x200 — so a manifest that reported 4096 would be reading the wrong thing.
    Geometry by hand: a 10x10 cm page at max_pages=2 is a 20x10 cm box; the photo is 2:1 and so
    fills it at 20 cm wide; dpi = 400 x 2.54 / 20 = 50.8.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "ready photo #curated")
    aid = _attach(
        memo["uid"], "pb-ready-1", subject,
        storage_path=_source(tmp_path, "big.jpg"), width=8000, height=4000,
    )
    _rendition(tmp_path, "pb-ready-1", (400, 200))
    try:
        body = _manifest(client, page_cm="10x10", max_pages=2.0, dpi=300)
    finally:
        _cleanup(aid)

    photo = body["photos"][0]
    assert photo["status"] == "ready"
    assert (photo["width"], photo["height"]) == (400, 200)
    assert photo["projected"] is False
    assert photo["effective_dpi"] == pytest.approx(50.8)
    assert photo["meets_dpi"] is False  # 50.8 dpi is nowhere near the 300 asked for


def test_manifest_projects_dpi_for_a_photo_that_is_not_generated_yet(
    logged_in_client, tmp_path, monkeypatch
):
    """Before the rendition exists the size is the source put through the rung, flagged as such.

    9504x6336 projects to 4096x2731 (by hand: 6336 x 4096 / 9504 = 2730.6). At 21x21 / 1.5 pages
    that photo is 1.5:1, so 21 cm of height wants 31.5 cm of width — exactly the spread cap — and
    dpi = 4096 x 2.54 / 31.5 = 330.3.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "not generated yet #curated")
    aid = _attach(
        memo["uid"], "pb-proj-1", subject,
        storage_path=_source(tmp_path, "huge.jpg"), width=9504, height=6336,
    )
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    photo = body["photos"][0]
    assert photo["status"] == "pending"
    assert (photo["width"], photo["height"]) == (4096, 2731)
    assert photo["projected"] is True
    assert photo["effective_dpi"] == pytest.approx(4096 * 2.54 / 31.5, abs=0.05)
    assert photo["meets_dpi"] is True  # 330 dpi clears the 300 the plan sizes the rung for


def test_a_pending_projection_uses_the_print_master_that_will_actually_render(
    logged_in_client, tmp_path, monkeypatch
):
    """The projection must describe the file the worker will OPEN, not the attachment's original.

    `printready.source_path` prefers the print master — the user's edited crop — and a master is
    often far smaller than the original it came from. Here the original is 9504x6336 (which alone
    would project to 4096x2731 and a comfortable ~330 dpi) while the master is 800x600, and the
    rung never upscales, so 800x600 is what will be printed.

    Geometry by hand: 800/600 = 4:3, so at 21x21 / 1.5 pages the photo is height-bound —
    printed width = min(21 x 1.5, 21 x 4/3) = 28.0 cm — and dpi = 800 x 2.54 / 28.0 = 72.57.
    Reporting the original's ~330 here would flip meets_dpi from false to TRUE, which is precisely
    the direction that makes this field dangerous rather than merely wrong.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "edited crop #curated")
    aid = _attach(
        memo["uid"], "pb-master-1", subject,
        storage_path=_source(tmp_path, "huge-original.jpg"), width=9504, height=6336,
    )
    _print_master(aid, _real_jpeg(tmp_path / "masters" / "crop.jpg", (800, 600)))
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    photo = body["photos"][0]
    assert photo["status"] == "pending"
    assert photo["source"] == "print-master"
    assert (photo["width"], photo["height"]) == (800, 600)  # NOT 4096x2731
    assert photo["projected"] is True
    assert photo["effective_dpi"] == pytest.approx(800 * 2.54 / 28.0, abs=0.05)
    assert photo["meets_dpi"] is False  # ~73 dpi: the warning triangle this endpoint exists for


def test_a_projection_reports_unknown_rather_than_the_wrong_photos_dimensions(
    logged_in_client, tmp_path, monkeypatch
):
    """When the master cannot be measured, the manifest says so — it does not fall back to the
    original's dimensions, because a confident wrong dpi is worse than an absent one."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "unreadable crop #curated")
    aid = _attach(
        memo["uid"], "pb-master-2", subject,
        storage_path=_source(tmp_path, "orig2.jpg"), width=9504, height=6336,
    )
    master = tmp_path / "masters" / "not-an-image.jpg"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"this is not a JPEG")
    _print_master(aid, master)
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    photo = body["photos"][0]
    assert (photo["width"], photo["height"]) == (None, None)
    assert photo["projected"] is False
    assert photo["effective_dpi"] is None
    assert photo["meets_dpi"] is None


def test_manifest_reports_an_unavailable_photo_when_the_source_is_gone(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "lost original #curated")
    aid = _attach(
        memo["uid"], "pb-gone-1", subject, storage_path=tmp_path / "originals" / "missing.jpg"
    )
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "unavailable"
    assert body["totals"] == {
        "selected": 1, "ready": 0, "pending": 0, "failed": 0, "unavailable": 1
    }
    assert _jobs_for(aid) == []  # nothing to derive from — never queue a job that can only fail


def test_manifest_names_the_print_master_as_the_source_when_there_is_one(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "has a print master #curated")
    aid = _attach(memo["uid"], "pb-src-1", subject, storage_path=_source(tmp_path, "orig.jpg"))
    _print_master(aid, _source(tmp_path, "master.jpg"))
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["source"] == "print-master"


def test_manifest_names_the_original_as_the_source_without_a_print_master(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "plain photo #curated")
    aid = _attach(memo["uid"], "pb-src-2", subject, storage_path=_source(tmp_path, "orig.jpg"))
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["source"] == "original"


def test_totals_count_every_member_once(logged_in_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "a bit of everything #curated")
    ready = _attach(memo["uid"], "pb-tot-ready", subject, storage_path=_source(tmp_path, "r.jpg"))
    _rendition(tmp_path, "pb-tot-ready")
    pending = _attach(memo["uid"], "pb-tot-pend", subject, storage_path=_source(tmp_path, "p.jpg"))
    failed = _attach(memo["uid"], "pb-tot-fail", subject, storage_path=_source(tmp_path, "f.jpg"))
    _job(failed, "failed")
    gone = _attach(memo["uid"], "pb-tot-gone", subject, storage_path=tmp_path / "nope.jpg")
    try:
        body = _manifest(client)
    finally:
        _cleanup(ready, pending, failed, gone)

    assert body["totals"] == {
        "selected": 4, "ready": 1, "pending": 1, "failed": 1, "unavailable": 1
    }
    assert sum(body["totals"][k] for k in ("ready", "pending", "failed", "unavailable")) == 4


# --------------------------------------------------------------------------------------------
# enqueue-on-request
# --------------------------------------------------------------------------------------------


def test_requesting_a_book_enqueues_the_photos_that_need_generating(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "needs work #curated")
    aid = _attach(memo["uid"], "pb-enq-1", subject, storage_path=_source(tmp_path, "a.jpg"))
    try:
        assert _jobs_for(aid) == []  # nothing queued before the request
        body = _manifest(client)
        queued = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "pending"
    assert queued == ["queued"]


def test_a_photo_that_is_already_ready_is_not_enqueued(logged_in_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "already done #curated")
    aid = _attach(memo["uid"], "pb-enq-2", subject, storage_path=_source(tmp_path, "a.jpg"))
    _rendition(tmp_path, "pb-enq-2")
    try:
        body = _manifest(client)
        queued = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "ready"
    assert queued == []


def test_polling_the_manifest_does_not_pile_up_duplicate_jobs(
    logged_in_client, tmp_path, monkeypatch
):
    """Re-requesting is how progress is watched, so it has to stay free: the second and third
    request must see the job already in flight and add nothing."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "polled #curated")
    aid = _attach(memo["uid"], "pb-enq-3", subject, storage_path=_source(tmp_path, "a.jpg"))
    try:
        for _ in range(3):
            body = _manifest(client)
        queued = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "pending"
    assert queued == ["queued"]


def test_a_terminally_failed_photo_is_reported_not_retried(logged_in_client, tmp_path, monkeypatch):
    """`failed` only happens at the attempt limit, and a book gets polled while it builds — an
    automatic retry would re-run a 60 MP encode on every poll."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "broken source #curated")
    aid = _attach(memo["uid"], "pb-fail-1", subject, storage_path=_source(tmp_path, "a.jpg"))
    _job(aid, "failed")
    try:
        body = _manifest(client)
        body = _manifest(client)
        jobs = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "failed"
    assert jobs == ["failed"]  # still just the one, after two requests


def test_replacing_the_source_clears_a_terminal_failure(logged_in_client, tmp_path, monkeypatch):
    """A terminal failure is scoped to the SOURCE it failed against, not to the photo.

    The worker gave up on a file that would not decode; the user then replaced it with a good one.
    The old verdict describes a file that no longer exists, so the photo must go back in the queue —
    otherwise fixing the master could never fix the page.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "master replaced #curated")
    source = _source(tmp_path, "replaced.jpg")
    aid = _attach(memo["uid"], "pb-refail-1", subject, storage_path=source)
    _job(aid, "failed")
    _bump_ctime_past_job(source, aid)  # the master is replaced AFTER the failure was recorded
    try:
        body = _manifest(client)
        jobs = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "pending"
    assert jobs == ["failed", "queued"]  # the stale verdict kept, a fresh attempt queued


def test_an_unchanged_source_keeps_its_terminal_failure(logged_in_client, tmp_path, monkeypatch):
    """The inverse pin: nothing changed on disk, so the failure still describes reality and the
    no-auto-retry property holds — no new job, however often the manifest is polled."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "still broken #curated")
    source = _source(tmp_path, "broken.jpg")
    aid = _attach(memo["uid"], "pb-refail-2", subject, storage_path=source)
    _fail_after_source(source, aid)
    try:
        body = _manifest(client)
        body = _manifest(client)
        jobs = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "failed"
    assert jobs == ["failed"]  # untouched after two polls


def test_staleness_is_judged_against_when_the_job_FAILED_not_when_it_was_enqueued(
    logged_in_client, tmp_path, monkeypatch
):
    """Pins the column choice: `updated_at`, not `created_at`.

    media_jobs has no completion column, so the failure time is `updated_at` — the write that set
    `state = failed`. `created_at` is when the job was ENQUEUED, which for a job that sat in the
    slow lane before failing can be hours earlier.

    Here the job was enqueued an hour before it failed, and the source has not changed since the
    FAILURE (only since the enqueue). The verdict must therefore stand. Judging by `created_at`
    would call this failure stale and re-queue a 60 MP encode on every manifest poll — the exact
    storm the no-auto-retry rule exists to prevent.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "long-queued failure #curated")
    source = _source(tmp_path, "slow-lane.jpg")
    aid = _attach(memo["uid"], "pb-refail-3", subject, storage_path=source)
    _fail_after_source(source, aid)  # failed AFTER the source was written
    _backdate_job_creation(aid)  # ...but enqueued an hour BEFORE it
    try:
        body = _manifest(client)
        jobs = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "failed"
    assert jobs == ["failed"]  # no retry: the source predates the failure, not just the enqueue


def test_a_failed_job_does_not_hide_a_rendition_that_is_actually_on_disk(
    logged_in_client, tmp_path, monkeypatch
):
    """The file is probed before the job history, so a good rendition wins over a stale failure."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "failed but present #curated")
    aid = _attach(memo["uid"], "pb-fail-2", subject, storage_path=_source(tmp_path, "a.jpg"))
    _job(aid, "failed")
    _rendition(tmp_path, "pb-fail-2")
    try:
        body = _manifest(client)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "ready"


def test_a_newer_successful_job_supersedes_an_older_failure(
    logged_in_client, tmp_path, monkeypatch
):
    """Latest-job-wins: an old failure must not condemn a photo whose regeneration is running."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "retried #curated")
    aid = _attach(memo["uid"], "pb-fail-3", subject, storage_path=_source(tmp_path, "a.jpg"))
    _job(aid, "failed")
    _job(aid, "processing")
    try:
        body = _manifest(client)
        jobs = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "pending"
    assert jobs == ["failed", "processing"]  # the running one was left alone, none added


def test_printready_jobs_are_the_only_history_consulted(logged_in_client, tmp_path, monkeypatch):
    """A failed UPLOAD job (kind=derive) says nothing about the print rendition."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "upload failed once #curated")
    aid = _attach(memo["uid"], "pb-kind-1", subject, storage_path=_source(tmp_path, "a.jpg"))
    _job(aid, "failed", kind=JobKind.derive)
    try:
        body = _manifest(client)
        print_jobs = _jobs_for(aid, kind=JobKind.printready)
        derive_jobs = _jobs_for(aid, kind=JobKind.derive)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "pending"  # NOT failed — that failure was an upload
    assert print_jobs == ["queued"]  # and the print rendition was queued normally
    assert derive_jobs == ["failed"]  # the upload job is untouched


# --------------------------------------------------------------------------------------------
# the archive
# --------------------------------------------------------------------------------------------


def _zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert zf.testzip() is None
    return zf


def test_zip_carries_ready_renditions_under_their_numbered_names(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "one ready photo #curated")
    _set_created(memo["uid"], datetime(2026, 1, 31, 9, 0, tzinfo=UTC))
    aid = _attach(
        memo["uid"], "pb-zip-1", subject, storage_path=_source(tmp_path, "a.jpg"),
        captured_at=datetime(2019, 7, 4, 12, 30, tzinfo=UTC),
    )
    payload = _rendition(tmp_path, "pb-zip-1").read_bytes()
    try:
        zf = _zip(client.get("/api/exports/photobook?format=zip"))
    finally:
        _cleanup(aid)

    name = "0001_2026-01-31_pb-zip-1.jpg"
    assert zf.read(name) == payload  # the real rendition bytes, not a placeholder
    assert zf.getinfo(name).date_time[:3] == (2019, 7, 4)  # stamped from captured_at
    assert not [n for n in zf.namelist() if n.endswith(".md")]  # a book is photos, not memo text


def test_zip_orders_photos_by_memo_date(logged_in_client, tmp_path, monkeypatch):
    """Three memos, deliberately created out of date order, must come back in date order — that
    numbering IS the book's page sequence for a folder import."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    aids = []
    for day, uid in ((15, "pb-ord-b"), (1, "pb-ord-a"), (28, "pb-ord-c")):
        memo = _memo(client, f"day {day} #curated")
        _set_created(memo["uid"], datetime(2026, 2, day, 8, 0, tzinfo=UTC))
        src = _source(tmp_path, f"{uid}.jpg")
        aids.append(_attach(memo["uid"], uid, subject, storage_path=src))
        _rendition(tmp_path, uid)
    try:
        zf = _zip(client.get("/api/exports/photobook?format=zip"))
    finally:
        _cleanup(*aids)

    photos = [n for n in zf.namelist() if n != "manifest.json"]
    assert photos == [
        "0001_2026-02-01_pb-ord-a.jpg",
        "0002_2026-02-15_pb-ord-b.jpg",
        "0003_2026-02-28_pb-ord-c.jpg",
    ]
    assert photos == sorted(photos)  # a folder import lands in the same order


def test_zip_skips_a_photo_that_is_not_ready_and_says_so_in_the_manifest(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "half a book #curated")
    _set_created(memo["uid"], datetime(2026, 4, 2, 8, 0, tzinfo=UTC))
    ready = _attach(memo["uid"], "pb-skip-ok", subject, storage_path=_source(tmp_path, "r.jpg"))
    _rendition(tmp_path, "pb-skip-ok")
    pending = _attach(memo["uid"], "pb-skip-no", subject, storage_path=_source(tmp_path, "p.jpg"))
    try:
        zf = _zip(client.get("/api/exports/photobook?format=zip"))
    finally:
        _cleanup(ready, pending)

    names = [n for n in zf.namelist() if n != "manifest.json"]
    assert names == ["0001_2026-04-02_pb-skip-ok.jpg"]  # the pending one contributes no bytes

    records = json.loads(zf.read("manifest.json"))
    by_uid = {r["attachment"]: r for r in records}
    assert len(records) == 2  # but it IS still a numbered page of the book
    assert by_uid["pb-skip-no"]["status"] == "pending"
    assert by_uid["pb-skip-no"]["filename"] == "0002_2026-04-02_pb-skip-no.jpg"
    assert by_uid["pb-skip-no"]["media"] == [
        {"attachment": "pb-skip-no", "file": "0002_2026-04-02_pb-skip-no.jpg", "missing": True}
    ]


def test_a_stale_rendition_is_never_shipped_as_the_current_one(
    logged_in_client, tmp_path, monkeypatch
):
    """A rendition older than its source is NOT ready, and its bytes must stay out of the book.

    This is the case that separates "is there a file at the print path" from "is that file the
    print of THIS source": the master was re-edited after the rendition was made, so the file on
    disk prints the old crop. It has to be regenerated, not exported.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _memo(client, "re-edited master #curated")
    _set_created(memo["uid"], datetime(2026, 6, 5, 8, 0, tzinfo=UTC))
    source = _source(tmp_path, "stale.jpg")
    aid = _attach(memo["uid"], "pb-stale-1", subject, storage_path=source)
    rendition = _rendition(tmp_path, "pb-stale-1")
    stale_bytes = rendition.read_bytes()
    _make_stale(source, rendition)  # the master changes AFTER the rendition was made
    try:
        body = _manifest(client)
        zf = _zip(client.get("/api/exports/photobook?format=zip"))
        queued = _jobs_for(aid)
    finally:
        _cleanup(aid)

    assert body["photos"][0]["status"] == "pending"
    assert queued == ["queued"]  # it was queued for regeneration
    name = "0001_2026-06-05_pb-stale-1.jpg"
    assert name not in zf.namelist()
    assert stale_bytes not in zf.read("manifest.json")  # nothing of the old file leaked in
    record = json.loads(zf.read("manifest.json"))[0]
    assert record["media"] == [{"attachment": "pb-stale-1", "file": name, "missing": True}]


def test_numbering_is_stable_while_the_book_is_still_building(
    logged_in_client, tmp_path, monkeypatch
):
    """A photo finishing later must slot into its own place, not renumber the pages after it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    aids = []
    for day, uid in ((1, "pb-num-a"), (2, "pb-num-b")):
        memo = _memo(client, f"day {day} #curated")
        _set_created(memo["uid"], datetime(2026, 5, day, 8, 0, tzinfo=UTC))
        src = _source(tmp_path, f"{uid}.jpg")
        aids.append(_attach(memo["uid"], uid, subject, storage_path=src))
    _rendition(tmp_path, "pb-num-b")  # only the SECOND page is ready
    try:
        before = _manifest(client)
        _rendition(tmp_path, "pb-num-a")  # now the first one finishes
        after = _manifest(client)
    finally:
        _cleanup(*aids)

    assert [p["filename"] for p in before["photos"]] == [p["filename"] for p in after["photos"]]
    assert [p["status"] for p in before["photos"]] == ["pending", "ready"]
    assert [p["status"] for p in after["photos"]] == ["ready", "ready"]


def test_zip_of_an_empty_selection_is_a_valid_archive(
    logged_in_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, _ = logged_in_client

    zf = _zip(client.get("/api/exports/photobook?format=zip&tag=nothing-has-this-tag"))

    assert zf.namelist() == ["manifest.json"]
    assert json.loads(zf.read("manifest.json")) == []


# --------------------------------------------------------------------------------------------
# bounded memory — the property inherited from the shared archive writer
# --------------------------------------------------------------------------------------------


async def test_photobook_archive_streams_large_renditions_in_bounded_memory(tmp_path, monkeypatch):
    """40 MB of print renditions through a peak of a few hundred KB.

    Print renditions are the largest things this codebase serves, so this is the property that
    matters most here. It is driven through `photobook.archive_items` rather than hand-built items,
    so any buffering added on the photo-book side — accumulating entries, reading a file to size it,
    building the archive before sending — lands in the peak and fails the test.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    entries = []
    each = 8 * 1024 * 1024
    for i in range(5):
        path = tmp_path / f"big{i}.jpg"
        path.write_bytes(os.urandom(each))
        entries.append(
            photobook.PhotoEntry(
                sequence=i + 1,
                attachment_uid=f"big{i}",
                memo_uid=f"m{i}",
                memo_date=date(2026, 1, 1),
                filename=f"{i + 1:04d}_2026-01-01_big{i}.jpg",
                source="original",
                status="ready",
                width=4096,
                height=2731,
                projected=False,
                effective_dpi=330.3,
                meets_dpi=True,
                captured_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                path=path,
            )
        )
    book = photobook.Photobook(tag="curated", geometry=BOOK, entries=tuple(entries))

    tracemalloc.start()
    try:
        total = 0
        async for chunk in stream_zip(photobook.archive_items(book)):
            total += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert total > 5 * each  # the payloads really went through (stored, not compressed)
    assert peak < 4_000_000
