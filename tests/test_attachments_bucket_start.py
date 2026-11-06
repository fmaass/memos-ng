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


# MEMNG-732: the two edges of the bucket arithmetic the ordinary-day seeds above never touch.
# A picture taken at exactly local midnight on a bucket boundary, once in summer time (22:00Z on
# 31 Aug is midnight of 1 Sept) and once in winter time (23:00Z on 31 Dec is midnight of 1 Jan, a
# year boundary too), each with a picture one second before it.
def _seed_midnight(me):
    _att("mn-oldyear", me, capture_instant="2025-12-31T22:59:59Z")  # 23:59:59 on 31 Dec 2025
    _att("mn-newyear", me, capture_instant="2025-12-31T23:00:00Z")  # 00:00:00 on 1 Jan 2026
    _att("mn-before", me, capture_instant="2026-08-31T21:59:59Z")  # 23:59:59 on 31 Aug
    _att("mn-midnight", me, capture_instant="2026-08-31T22:00:00Z")  # 00:00:00 on 1 Sept
    _att("mn-after", me, capture_instant="2026-09-01T10:00:00Z")


MN_DESC = ["mn-after", "mn-midnight", "mn-before", "mn-newyear", "mn-oldyear"]


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("2026-09", MN_DESC),
        ("2026-09-01", MN_DESC),
        ("2026-08", MN_DESC[2:]),  # local midnight is the 1st's, never the 31st's
        ("2026-08-31", MN_DESC[2:]),
        ("2026", MN_DESC),
        ("2026-01", MN_DESC[3:]),
        ("2026-01-01", MN_DESC[3:]),
        ("2025", ["mn-oldyear"]),
        ("2025-12-31", ["mn-oldyear"]),
    ],
)
def test_taken_desc_opens_a_picture_taken_at_local_midnight_in_its_new_bucket(
    blib, bucket, expected
):
    client, me = blib
    _seed_midnight(me)
    assert _open(client, f"order=taken_desc&bucket={bucket}&tz=Europe/Zurich") == expected


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("2026-09", ["mn-midnight", "mn-after"]),
        ("2026-09-01", ["mn-midnight", "mn-after"]),
        ("2026-08-31", ["mn-before", "mn-midnight", "mn-after"]),
        ("2026", ["mn-newyear", "mn-before", "mn-midnight", "mn-after"]),
        ("2026-01-01", ["mn-newyear", "mn-before", "mn-midnight", "mn-after"]),
        ("2025-12-31", MN_DESC[::-1]),
    ],
)
def test_taken_asc_opens_a_picture_taken_at_local_midnight_in_its_new_bucket(
    blib, bucket, expected
):
    client, me = blib
    _seed_midnight(me)
    assert _open(client, f"order=taken_asc&bucket={bucket}&tz=Europe/Zurich") == expected


def test_the_buckets_count_a_picture_taken_at_local_midnight_in_its_new_bucket(blib):
    client, me = blib
    _seed_midnight(me)
    assert _buckets(client, "month") == [
        ("2026-09", 2, "mn-after"),
        ("2026-08", 1, "mn-before"),
        ("2026-01", 1, "mn-newyear"),
        ("2025-12", 1, "mn-oldyear"),
    ]


# Both 2026 changeover days in Zurich: 29 March is 23 hours long (23:00Z to 22:00Z the next day)
# and 25 October 25 hours (22:00Z to 23:00Z the next day). Each day gets a picture half an hour
# inside either end and a neighbour half an hour outside, so a bucket counted at one fixed
# offset, summer's or winter's, misplaces at least one of them.
def _seed_dst(me):
    _att("dst-mar28-late", me, capture_instant="2026-03-28T22:30:00Z")  # 23:30 CET, 28 Mar
    _att("dst-mar29-early", me, capture_instant="2026-03-28T23:30:00Z")  # 00:30 CET, 29 Mar
    _att("dst-mar29-late", me, capture_instant="2026-03-29T21:30:00Z")  # 23:30 CEST, 29 Mar
    _att("dst-mar30-early", me, capture_instant="2026-03-29T22:30:00Z")  # 00:30 CEST, 30 Mar
    _att("dst-oct24-late", me, capture_instant="2026-10-24T21:30:00Z")  # 23:30 CEST, 24 Oct
    _att("dst-oct25-early", me, capture_instant="2026-10-24T22:30:00Z")  # 00:30 CEST, 25 Oct
    _att("dst-oct25-late", me, capture_instant="2026-10-25T22:30:00Z")  # 23:30 CET, 25 Oct
    _att("dst-oct26-early", me, capture_instant="2026-10-25T23:30:00Z")  # 00:30 CET, 26 Oct


DST_DESC = [
    "dst-oct26-early",
    "dst-oct25-late",
    "dst-oct25-early",
    "dst-oct24-late",
    "dst-mar30-early",
    "dst-mar29-late",
    "dst-mar29-early",
    "dst-mar28-late",
]


def _buckets(client, size):
    r = client.get(f"/api/attachments/buckets?size={size}&tz=Europe/Zurich")
    assert r.status_code == 200, r.text
    return [(b["key"], b["count"], b["coverUid"]) for b in r.json()]


def test_the_buckets_count_both_changeover_days_by_their_local_length(blib):
    client, me = blib
    _seed_dst(me)
    assert _buckets(client, "day") == [
        ("2026-10-26", 1, "dst-oct26-early"),
        ("2026-10-25", 2, "dst-oct25-late"),
        ("2026-10-24", 1, "dst-oct24-late"),
        ("2026-03-30", 1, "dst-mar30-early"),
        ("2026-03-29", 2, "dst-mar29-late"),
        ("2026-03-28", 1, "dst-mar28-late"),
    ]
    assert _buckets(client, "month") == [
        ("2026-10", 4, "dst-oct26-early"),
        ("2026-03", 4, "dst-mar30-early"),
    ]


@pytest.mark.parametrize(
    ("order", "bucket", "expected"),
    [
        ("taken_desc", "2026-10-25", DST_DESC[1:]),
        ("taken_desc", "2026-03-29", DST_DESC[5:]),
        ("taken_asc", "2026-10-25", ["dst-oct25-early", "dst-oct25-late", "dst-oct26-early"]),
        ("taken_asc", "2026-03-29", DST_DESC[::-1][1:]),
    ],
)
def test_a_changeover_day_opens_at_its_own_first_picture(blib, order, bucket, expected):
    client, me = blib
    _seed_dst(me)
    assert _open(client, f"order={order}&bucket={bucket}&tz=Europe/Zurich") == expected


@pytest.mark.parametrize("size", ["day", "month"])
def test_every_bucket_around_a_changeover_opens_where_its_count_says(blib, size):
    """The open position agrees with the counts the SPA draws: a desc walk opened at a bucket
    starts at its cover and holds exactly that bucket's count more than the walk opened at the
    next older bucket; an asc walk opened at it holds the counts of it and every newer one."""
    client, me = blib
    _seed_dst(me)
    buckets = _buckets(client, size)
    for i, (key, _, cover) in enumerate(buckets):
        desc = _open(client, f"order=taken_desc&bucket={key}&tz=Europe/Zurich")
        asc = _open(client, f"order=taken_asc&bucket={key}&tz=Europe/Zurich")
        assert desc[:1] == [cover], key
        assert len(desc) == sum(n for _, n, _ in buckets[i:]), key
        assert len(asc) == sum(n for _, n, _ in buckets[: i + 1]), key
