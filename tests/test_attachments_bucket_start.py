"""Opening the library at a bucket in one request (MEMNG-649, the server half of MEMNG-636).

`?bucket=<key>&tz=<zone>` on a taken order starts the list at the bucket that key names in that
zone — the key exactly as `/api/attachments/buckets` returns it — so a client opens a tapped year,
month or day with ONE list request instead of paging down to it. Every expected list below is a
literal written by hand over an empty library (`lib`), never derived from the endpoint under test.
"""

import pytest

from tests.test_assets import _walk
from tests.test_attachments_taken import (
    OTHER,
    _att,
    _memo,
    _sql,
    lib,  # noqa: F401  (pytest finds a fixture by name)
)


@pytest.fixture
def blib(lib):  # noqa: F811
    return lib


def _seed(me):
    _att("bs-nye", me, capture_instant="2025-12-31T23:30:00Z")  # 00:30 on 1 Jan 2026 in Zurich
    _att("bs-mid-aug", me, capture_instant="2026-08-15T10:00:00Z")
    _att("bs-aug31", me, capture_instant="2026-08-31T22:30:00Z")  # 00:30 on 1 Sept in Zurich
    _att("bs-sep1", me, capture_instant="2026-09-01T05:00:00Z")
    _att("bs-sep1-tie", me, capture_instant="2026-09-01T05:00:00Z")  # same instant, higher id
    _att("bs-sep30", me, capture_instant="2026-09-30T22:30:00Z")  # 00:30 on 1 Oct in Zurich
    _att("bs-oct", me, capture_instant="2026-10-02T10:00:00Z")


def _open(client, query, limit=500):
    walked, _ = _walk(client, f"/api/attachments?{query}", limit)
    return walked


DESC_ALL = ["bs-oct", "bs-sep30", "bs-sep1-tie", "bs-sep1", "bs-aug31", "bs-mid-aug", "bs-nye"]


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("2026-10", DESC_ALL),  # the newest month: the whole library
        ("2026-09", DESC_ALL[2:]),  # 1 Oct 00:30 local is October's, 1 Sept 00:30 September's
        ("2026-08", DESC_ALL[5:]),
        ("2026-07", ["bs-nye"]),  # an empty month opens at the next older picture
        ("2026-01", ["bs-nye"]),
        ("2025-12", []),  # the New Year's Eve picture is 2026's in Zurich
        ("2026", DESC_ALL),
        ("2025", []),
        ("2026-09-01", DESC_ALL[2:]),
        ("2026-08-31", DESC_ALL[5:]),  # 22:30Z on the 31st is the 1st in Zurich
    ],
)
def test_taken_desc_opens_at_the_newest_picture_of_the_bucket(blib, bucket, expected):
    client, me = blib
    _seed(me)
    assert _open(client, f"order=taken_desc&bucket={bucket}&tz=Europe/Zurich") == expected


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("2026-09", ["bs-aug31", "bs-sep1", "bs-sep1-tie", "bs-sep30", "bs-oct"]),
        ("2026-10", ["bs-sep30", "bs-oct"]),
        (
            "2026-01",
            ["bs-nye", "bs-mid-aug", "bs-aug31", "bs-sep1", "bs-sep1-tie", "bs-sep30", "bs-oct"],
        ),
        ("2026-11", []),
        ("2026-09-01", ["bs-aug31", "bs-sep1", "bs-sep1-tie", "bs-sep30", "bs-oct"]),
    ],
)
def test_taken_asc_opens_at_the_oldest_picture_of_the_bucket(blib, bucket, expected):
    client, me = blib
    _seed(me)
    assert _open(client, f"order=taken_asc&bucket={bucket}&tz=Europe/Zurich") == expected


def test_the_zone_is_the_one_the_buckets_were_counted_in(blib):
    client, me = blib
    _seed(me)
    assert _open(client, "order=taken_desc&bucket=2026-09&tz=UTC") == DESC_ALL[1:]
    assert _open(client, "order=taken_desc&bucket=2025&tz=UTC") == ["bs-nye"]
    # the default zone is the buckets' default
    assert _open(client, "order=taken_desc&bucket=2026-09") == DESC_ALL[2:]


@pytest.mark.parametrize("size", ["year", "month", "day"])
def test_a_desc_open_lands_on_every_buckets_cover_in_one_request(blib, size):
    """The property the client relies on: for each bucket the buckets endpoint names, the first
    item of ONE list request is that bucket's cover — under a tag filter too."""
    client, me = blib
    _seed(me)
    m = _memo("bs-tagged", me, tags=("hike",))
    _att("bs-hike-1", me, m, capture_instant="2026-08-20T10:00:00Z")
    _att("bs-hike-2", me, m, capture_instant="2026-03-03T10:00:00Z")
    # An OLDER populated year, with and without the tag: with one year only, a list that ignored
    # the bucket would still start at that year's cover, because it is the newest picture overall.
    _att("bs-2024", me, capture_instant="2024-06-01T10:00:00Z")
    old = _memo("bs-tagged-old", me, tags=("hike",))
    _att("bs-hike-2024", me, old, capture_instant="2024-05-05T10:00:00Z")
    for extra in ("", "&tag=hike"):
        buckets = client.get(f"/api/attachments/buckets?size={size}{extra}").json()
        # control: some bucket opens somewhere other than the top of the library, so an ignored
        # bucket cannot pass by landing on the newest picture every time
        assert len(buckets) >= 2
        assert any(b["coverUid"] != buckets[0]["coverUid"] for b in buckets[1:])
        for b in buckets:
            r = client.get(f"/api/attachments?order=taken_desc&limit=1&bucket={b['key']}{extra}")
            assert r.status_code == 200, r.text
            assert [i["uid"] for i in r.json()] == [b["coverUid"]], (size, extra, b["key"])


def test_the_type_and_tag_filters_still_apply_after_the_bucket(blib):
    client, me = blib
    _seed(me)
    _att("bs-video", me, mime="video/mp4", capture_instant="2026-09-05T10:00:00Z")
    theirs = _memo("bs-theirs", OTHER, visibility="PRIVATE")
    _att("bs-private", OTHER, theirs, capture_instant="2026-09-10T10:00:00Z")
    q = "order=taken_desc&bucket=2026-09&tz=Europe/Zurich"
    assert _open(client, f"{q}&type=video") == ["bs-video"]
    assert _open(client, f"{q}&type=image") == DESC_ALL[2:]  # and never another's PRIVATE one


def test_paging_from_a_bucket_neither_repeats_nor_skips(blib):
    """`_walk` resends every parameter with the cursor, bucket included, page by page."""
    client, me = blib
    _seed(me)
    walked, pages = _walk(client, "/api/attachments?order=taken_desc&bucket=2026-09", 1)
    assert walked == DESC_ALL[2:]
    assert pages == 5
    # the cursor alone continues the same walk: the bucket bound is behind it
    first = client.get("/api/attachments?order=taken_desc&bucket=2026-09&limit=2")
    cursor = first.headers["x-next-cursor"]
    rest = client.get(f"/api/attachments?order=taken_desc&limit=10&cursor={cursor}").json()
    assert [i["uid"] for i in first.json() + rest] == DESC_ALL[2:]


def test_a_picture_added_mid_walk_lands_by_its_date_not_by_the_bucket(blib):
    client, me = blib
    _seed(me)
    first = client.get("/api/attachments?order=taken_desc&bucket=2026-09&limit=2")
    assert [i["uid"] for i in first.json()] == ["bs-sep1-tie", "bs-sep1"]
    _att("bs-late-oct", me, capture_instant="2026-10-20T10:00:00Z")  # above the bucket
    _att("bs-late-aug", me, capture_instant="2026-08-20T10:00:00Z")  # below the cursor
    cursor = first.headers["x-next-cursor"]
    rest = client.get(
        f"/api/attachments?order=taken_desc&bucket=2026-09&limit=10&cursor={cursor}"
    ).json()
    assert [i["uid"] for i in rest] == ["bs-aug31", "bs-late-aug", "bs-mid-aug", "bs-nye"]


@pytest.mark.parametrize(
    "query",
    [
        "order=taken_desc&bucket=2026-13",
        "order=taken_desc&bucket=2026-02-30",
        "order=taken_desc&bucket=2026-9",
        "order=taken_desc&bucket=26-09",
        "order=taken_desc&bucket=0000",
        "order=taken_desc&bucket=",
        "order=taken_desc&bucket=2026-09%0A",
        "order=taken_desc&bucket=2026-09'%20OR%201=1",
        "order=taken_desc&bucket=２０２６",  # full-width digits are digits to a naive regex
        "order=taken_desc&bucket=2026-09&tz=Mars/Olympus",
        "order=desc&bucket=2026-09",  # an upload-order walk has no taken bucket to open
        "bucket=2026-09",  # the default order is the upload order
        "order=asc&bucket=2026-09",
    ],
)
def test_a_bucket_that_cannot_be_opened_is_a_422_never_the_top_of_the_library(blib, query):
    client, me = blib
    _seed(me)
    r = client.get(f"/api/attachments?{query}")
    assert r.status_code == 422, (query, r.status_code, r.text)


def test_the_unbounded_list_is_unchanged(blib):
    client, me = blib
    _seed(me)
    assert _open(client, "order=taken_desc") == DESC_ALL  # control for every case above
    assert _sql("SELECT count(*) FROM attachments")[0][0] == 7
