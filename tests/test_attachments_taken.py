"""The taken-date contract of the media library (MEMNG-507, plan 3929 Phase 2).

`takenAt` = coalesce(capture_instant, the memo's created_at, the attachment's created_at) — the
owner's decision of 2026-09-22. `captured_at` is NEVER in that chain: it is `capture_instant or
client_mtime or now` (app/services/uploads.py), so reading it would date a photo by the browser's
file mtime or by the migration day. Every date below is chosen so a chain that read it would land
somewhere else and fail.

Every test here starts from an EMPTY library (`_wipe`), so the expected lists are exact literals
written by hand, never derived from another endpoint of the same server.
"""

import pytest
from app.config import settings
from app.database import engine as async_engine
from app.main import app
from app.routers import assets
from PIL import Image
from sqlalchemy import create_engine, event, text
from starlette.routing import Match

from tests.conftest import SYNC_URL
from tests.test_assets import _walk

OTHER = "taken_other"


def _sql(stmt: str, params: dict | None = None):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        result = c.execute(text(stmt), params or {})
        rows = result.fetchall() if result.returns_rows else []
    eng.dispose()
    return rows


def _wipe():
    for table in ("frame_share_jobs", "media_jobs", "attachments", "memos", "tags"):
        _sql(f"DELETE FROM {table}")


@pytest.fixture
def lib(logged_in_client):
    """The logged-in client over an empty library, plus a second household member `OTHER`."""
    _wipe()
    _sql("DELETE FROM users WHERE subject=:s", {"s": OTHER})
    _sql("INSERT INTO users (subject, role) VALUES (:s,'user')", {"s": OTHER})
    try:
        yield logged_in_client
    finally:
        _wipe()
        _sql("DELETE FROM users WHERE subject=:s", {"s": OTHER})


def _memo(uid, owner, visibility="PUBLIC", created_at="2026-05-12T10:00:00Z", tags=()):
    mid = _sql(
        "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, updated_at) "
        "VALUES (:u,(SELECT id FROM users WHERE subject=:o),'m',CAST(:v AS visibility),false,"
        "CAST(:c AS timestamptz),now()) RETURNING id",
        {"u": uid, "o": owner, "v": visibility, "c": created_at},
    )[0][0]
    for name in tags:
        tid = _sql(
            "INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=:n "
            "RETURNING id",
            {"n": name},
        )[0][0]
        _sql("INSERT INTO memo_tags (memo_id, tag_id) VALUES (:m,:t)", {"m": mid, "t": tid})
    return mid


def _att(
    uid,
    owner,
    memo_id=None,
    *,
    mime="image/jpeg",
    filename="x.jpg",
    capture_instant=None,
    captured_at=None,
    created_at="2026-06-21T09:00:00Z",
):
    return _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
        "storage_path, blurhash, capture_instant, captured_at, created_at) VALUES "
        "(:u,(SELECT id FROM users WHERE subject=:o),:m,:f,:mime,1,"
        "CAST('ready' AS attachment_status),'/x','LKO2',CAST(:ci AS timestamptz),"
        "CAST(:ca AS timestamptz),CAST(:cr AS timestamptz)) RETURNING id",
        {
            "u": uid,
            "o": owner,
            "m": memo_id,
            "f": filename,
            "mime": mime,
            "ci": capture_instant,
            "ca": captured_at,
            "cr": created_at,
        },
    )[0][0]


def _buckets(client, query):
    r = client.get(f"/api/attachments/buckets?{query}")
    assert r.status_code == 200, r.text
    body = r.json()
    for b in body:
        # F2: the signed thumbnail is the cover's OWN url, minted after authorisation — never
        # another row's. Asserting the uid alone would pass a url signed for a different item.
        # A video has no thumb file, only a poster (app/services/poster.py), so its cover is the
        # poster tier; the mime comes from SQL here, not from the endpoint under test.
        (mime,) = _sql("SELECT mime FROM attachments WHERE uid=:u", {"u": b["coverUid"]})[0]
        tier = "poster" if mime.startswith("video/") else "thumb"
        assert b["coverThumbnailUrl"].split("?")[0] == f"/api/attachments/{b['coverUid']}/{tier}"
        assert "sig=" in b["coverThumbnailUrl"]
    return [(b["key"], b["count"], b["coverUid"]) for b in body]


def _oracle(direction: str) -> list[str]:
    """The library in taken order, straight from SQL — written here, not borrowed from the app."""
    return [
        r[0]
        for r in _sql(
            "SELECT a.uid FROM attachments a LEFT JOIN memos m ON m.id = a.memo_id "
            f"ORDER BY coalesce(a.capture_instant, m.created_at, a.created_at) {direction}, "
            f"a.id {direction}"
        )
    ]


# --- (a) the fallback chain -----------------------------------------------------------------


def test_taken_at_falls_back_to_the_memo_date_then_created_at_never_captured_at(lib):
    client, me = lib
    memo = _memo("fb-m", me, created_at="2026-05-12T10:00:00Z")
    _att(
        "fb-exif",
        me,
        memo,
        capture_instant="2026-06-01T08:00:00Z",
        captured_at="2026-06-21T09:00:00Z",
    )
    # migrated: no instant, captured_at = the migration day → the MEMO's date, never captured_at
    _att("fb-memo", me, memo, captured_at="2026-06-21T09:00:00Z")
    # an orphan has no memo → its own created_at, still never captured_at
    _att("fb-orph", me, captured_at="2026-07-01T00:00:00Z", created_at="2026-06-22T11:00:00Z")
    by_uid = {i["uid"]: i for i in client.get("/api/attachments").json()}
    assert by_uid["fb-exif"].get("takenAt") == "2026-06-01T08:00:00Z"
    assert by_uid["fb-memo"].get("takenAt") == "2026-05-12T10:00:00Z"
    assert by_uid["fb-orph"].get("takenAt") == "2026-06-22T11:00:00Z"


# --- (b) + (d) the composite keyset ---------------------------------------------------------


def _seed_tie(me):
    """Seven rows; three share one sub-second instant and straddle a limit-2 page boundary."""
    memo = _memo("tie-m", me, created_at="2026-01-01T00:00:00Z")
    _att("tie-new-1", me, memo, capture_instant="2026-09-01T12:00:02Z")
    _att("tie-old-1", me, memo, capture_instant="2026-09-01T11:00:00Z")
    for n in range(3):
        _att(f"tie-{n}", me, memo, capture_instant="2026-09-01T12:00:00.500000Z")
    _att("tie-new-0", me, memo, capture_instant="2026-09-01T12:00:01Z")
    _att("tie-old-0", me, memo)  # the memo's date, far older


def test_taken_desc_pages_across_a_three_way_tie_without_skip_or_repeat(lib):
    client, me = lib
    _seed_tie(me)
    paged, pages = _walk(client, "/api/attachments?order=taken_desc", 2)
    expected = _oracle("DESC")
    assert len(expected) == 7  # positive control: the oracle saw the whole seed
    assert expected[:2] == ["tie-new-1", "tie-new-0"]  # the tie sits at positions 3-5
    assert pages >= 4
    assert paged == expected


def test_taken_asc_mirrors_taken_desc(lib):
    client, me = lib
    _seed_tie(me)
    desc, _ = _walk(client, "/api/attachments?order=taken_desc", 2)
    asc, pages = _walk(client, "/api/attachments?order=taken_asc", 2)
    assert pages >= 4
    assert asc == _oracle("ASC")
    assert asc == list(reversed(desc))


def test_taken_order_serves_takenat_descending_in_rfc3339(lib):
    client, me = lib
    _seed_tie(me)
    body = client.get("/api/attachments?order=taken_desc&limit=3").json()
    stamps = [i["takenAt"] for i in body]
    assert stamps == ["2026-09-01T12:00:02Z", "2026-09-01T12:00:01Z", "2026-09-01T12:00:00Z"]


# --- (c) cursor kinds -----------------------------------------------------------------------


def test_a_cursor_of_one_kind_is_refused_by_the_other_order(lib):
    client, me = lib
    for n in range(3):
        _att(f"ck-{n}", me, capture_instant=f"2026-09-0{n + 1}T10:00:00Z")
    taken_page = client.get("/api/attachments?order=taken_desc&limit=1")
    assert taken_page.status_code == 200, taken_page.text  # the prerequisite: a real taken cursor
    taken_cursor = taken_page.headers["x-next-cursor"]
    id_cursor = client.get("/api/attachments?limit=1").headers["x-next-cursor"]
    assert client.get(f"/api/attachments?order=taken_desc&cursor={taken_cursor}").status_code == 200
    for order in ("desc", "asc"):
        r = client.get(f"/api/attachments?order={order}&cursor={taken_cursor}")
        assert r.status_code == 422, (order, r.text)
    for order in ("taken_desc", "taken_asc"):
        r = client.get(f"/api/attachments?order={order}&cursor={id_cursor}")
        assert r.status_code == 422, (order, r.text)


def test_taken_cursor_round_trips_at_full_precision_and_refuses_other_shapes():
    from datetime import UTC, datetime

    from app.services.cursors import InvalidCursor, encode_cursor_payload

    when = datetime(2026, 9, 1, 12, 0, 0, 500000, tzinfo=UTC)
    token = assets.encode_taken_cursor(when, 42)
    assert token == encode_cursor_payload("t|2026-09-01T12:00:00.500000Z|42")
    assert assets.decode_taken_cursor(token) == (when, 42)
    for bad in (
        encode_cursor_payload("42"),  # an id cursor
        encode_cursor_payload("t|2026-09-01T12:00:00|42"),  # naive: no instant at all
        encode_cursor_payload("t|nonsense|42"),
        encode_cursor_payload("t|2026-09-01T12:00:00Z|x"),
        encode_cursor_payload("x|2026-09-01T12:00:00Z|42"),
        encode_cursor_payload("t|2026-09-01T12:00:00Z"),
    ):
        with pytest.raises(InvalidCursor):
            assets.decode_taken_cursor(bad)
    with pytest.raises(InvalidCursor):
        assets.decode_attachment_cursor(token)  # the id path refuses a taken cursor


# --- (e) + (g) buckets ----------------------------------------------------------------------


def _seed_zurich_edges(me):
    _att("z-aug31", me, capture_instant="2026-08-31T22:30:00Z")  # 00:30 on 1 Sept in Zurich
    _att("z-nye", me, capture_instant="2025-12-31T23:30:00Z")  # 00:30 on 1 Jan 2026 in Zurich
    _att("z-mid-aug", me, capture_instant="2026-08-15T10:00:00Z")
    _att("z-sep1-am", me, capture_instant="2026-09-01T05:00:00Z")
    _att("z-sep1-tie", me, capture_instant="2026-09-01T05:00:00Z")  # same instant, higher id


def test_buckets_group_by_the_europe_zurich_calendar_and_cover_the_newest(lib):
    client, me = lib
    _seed_zurich_edges(me)
    assert _buckets(client, "size=month") == [
        ("2026-09", 3, "z-sep1-tie"),
        ("2026-08", 1, "z-mid-aug"),
        ("2026-01", 1, "z-nye"),
    ]
    assert _buckets(client, "size=day") == [
        ("2026-09-01", 3, "z-sep1-tie"),
        ("2026-08-15", 1, "z-mid-aug"),
        ("2026-01-01", 1, "z-nye"),
    ]
    assert _buckets(client, "size=year") == [("2026", 5, "z-sep1-tie")]
    # an explicit zone is honoured, not ignored: in UTC the two edge items stay where UTC puts them
    assert _buckets(client, "size=year&tz=UTC") == [
        ("2026", 4, "z-sep1-tie"),
        ("2025", 1, "z-nye"),
    ]


def test_buckets_honour_the_type_filter(lib):
    client, me = lib
    _att("bt-img", me, capture_instant="2026-09-02T10:00:00Z")
    _att("bt-vid", me, mime="video/mp4", capture_instant="2026-09-03T10:00:00Z")
    assert _buckets(client, "size=month&type=image") == [("2026-09", 1, "bt-img")]
    assert _buckets(client, "size=month&type=video") == [("2026-09", 1, "bt-vid")]
    assert _buckets(client, "size=month") == [("2026-09", 2, "bt-vid")]


@pytest.mark.parametrize("size", ["day", "month"])
def test_a_video_bucket_covers_with_its_poster_and_an_image_bucket_with_its_thumb(
    lib, monkeypatch, tmp_path, size
):
    """A video gets a poster and never a thumb (app/services/poster.py), so a cover minted as the
    thumb tier 404s. The two items sit in different months so they are separate buckets under
    both sizes, and only the files production has are on disk: the image's thumb, the video's
    poster."""
    client, me = lib
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _att("cv-img", me, capture_instant="2026-08-02T10:00:00Z")
    _att("cv-vid", me, mime="video/quicktime", capture_instant="2026-09-03T10:00:00Z")
    thumb = tmp_path / "assets" / "thumbs" / "cv-img.jpg"
    poster = tmp_path / "assets" / "web" / "cv-vid.poster.jpg"
    for path, colour in ((thumb, (10, 20, 30)), (poster, (200, 100, 50))):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), colour).save(path, "JPEG")
    keys = {"day": ("2026-09-03", "2026-08-02"), "month": ("2026-09", "2026-08")}[size]
    assert _buckets(client, f"size={size}") == [(keys[0], 1, "cv-vid"), (keys[1], 1, "cv-img")]
    covers = {
        b["coverUid"]: b["coverThumbnailUrl"]
        for b in client.get(f"/api/attachments/buckets?size={size}").json()
    }
    # The literal tier, not just a 200: the route also answers a thumb request with the poster, so
    # a fetch alone would stay green with the bucket still minting /thumb for a video.
    assert covers["cv-vid"].split("?")[0] == "/api/attachments/cv-vid/poster"
    assert covers["cv-img"].split("?")[0] == "/api/attachments/cv-img/thumb"
    for uid, path in (("cv-vid", poster), ("cv-img", thumb)):
        r = client.get(covers[uid])
        assert r.status_code == 200, (uid, r.text)
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == path.read_bytes()


def test_a_bucket_names_its_covers_media_type(lib):
    """MEMNG-914: a bucket carries its cover's mime, the same string the list gives as `mime`.
    Each month holds the other kind as an older item, so a cover mime taken from the wrong row,
    or one hard-coded to either kind, cannot pass both."""
    client, me = lib
    _att("cm-sep-img", me, mime="image/jpeg", capture_instant="2026-09-01T10:00:00Z")
    _att("cm-sep-vid", me, mime="video/mp4", capture_instant="2026-09-20T10:00:00Z")
    _att("cm-aug-vid", me, mime="video/quicktime", capture_instant="2026-08-01T10:00:00Z")
    _att("cm-aug-img", me, mime="image/png", capture_instant="2026-08-20T10:00:00Z")
    r = client.get("/api/attachments/buckets?size=month")
    assert r.status_code == 200, r.text
    got = [(b["key"], b["coverUid"], b.get("coverMime")) for b in r.json()]
    assert got == [
        ("2026-09", "cm-sep-vid", "video/mp4"),
        ("2026-08", "cm-aug-img", "image/png"),
    ]


@pytest.mark.parametrize(
    "query",
    ["size=week", "size=", "size=month&tz=Mars/Olympus_Mons", "size=month&tz=", "tz=UTC"],
)
def test_buckets_refuse_an_unknown_size_or_zone(lib, query):
    client, _ = lib
    assert client.get(f"/api/attachments/buckets?{query}").status_code == 422


# --- (f) the independent two-user visibility oracle -----------------------------------------

VISIBILITY_EXPECTED = {
    "day": [("2026-07-04", 2, "vis-a-memo"), ("2026-03-10", 1, "vis-a-orph")],
    "month": [("2026-07", 2, "vis-a-memo"), ("2026-03", 1, "vis-a-orph")],
    "year": [("2026", 3, "vis-a-memo")],
}


@pytest.mark.parametrize("size", ["day", "month", "year"])
def test_buckets_never_count_or_cover_another_members_private_media(lib, size):
    client, me = lib
    mine = _memo("vis-a-m", me, visibility="PRIVATE")
    _att("vis-a-memo", me, mine, capture_instant="2026-07-04T09:00:00Z")
    _att("vis-a-orph", me, capture_instant="2026-03-10T10:00:00Z")
    theirs_pub = _memo("vis-b-pub", OTHER, visibility="PUBLIC")
    _att("vis-b-pub", OTHER, theirs_pub, capture_instant="2026-07-04T08:00:00Z")
    theirs_priv = _memo("vis-b-priv", OTHER, visibility="PRIVATE")
    # newest of their day and a year of their own: a leak changes a count, a cover AND a key
    _att("vis-b-priv-1", OTHER, theirs_priv, capture_instant="2026-07-04T20:00:00Z")
    _att("vis-b-priv-2", OTHER, theirs_priv, capture_instant="2025-11-11T10:00:00Z")
    _att("vis-b-orph", OTHER, capture_instant="2026-03-10T12:00:00Z")
    # B's PUBLIC item is visible to A and counted in A's July bucket; it is older than A's own
    assert _buckets(client, f"size={size}") == VISIBILITY_EXPECTED[size]


# --- (k) the tag filter and the tag counts --------------------------------------------------


def _seed_tags(me):
    m1 = _memo("tg-m1", me, tags=("hike", "dog"))
    _att("tg-m1-a", me, m1, capture_instant="2026-06-01T10:00:00Z")
    _att("tg-m1-b", me, m1, capture_instant="2026-06-02T10:00:00Z")
    _att("tg-m1-c", me, m1, capture_instant="2026-07-01T10:00:00Z")
    m2 = _memo("tg-m2", me, visibility="PRIVATE", tags=("hike",))
    _att("tg-m2-v", me, m2, mime="video/mp4", capture_instant="2026-07-02T10:00:00Z")
    m3 = _memo("tg-m3", OTHER, visibility="PRIVATE", tags=("hike",))
    _att("tg-m3-a", OTHER, m3, capture_instant="2026-07-03T10:00:00Z")
    _att("tg-m3-b", OTHER, m3, capture_instant="2025-07-03T10:00:00Z")
    m4 = _memo("tg-m4", OTHER, tags=("dog",))
    _att("tg-m4-a", OTHER, m4, capture_instant="2026-05-01T10:00:00Z")
    _att("tg-m4-b", OTHER, m4, capture_instant="2026-05-02T10:00:00Z")
    _memo("tg-m5", me, tags=("textonly",))  # a tagged memo without pictures
    m6 = _memo("tg-m6", me)
    _att("tg-m6-a", me, m6, capture_instant="2026-06-03T10:00:00Z")
    _att("tg-orph", me, capture_instant="2026-06-04T10:00:00Z")


def test_tag_filters_the_list_to_visible_media_of_tagged_memos(lib):
    client, me = lib
    _seed_tags(me)
    hike = {i["uid"] for i in client.get("/api/attachments?tag=hike").json()}
    assert hike == {"tg-m1-a", "tg-m1-b", "tg-m1-c", "tg-m2-v"}
    walked, _ = _walk(client, "/api/attachments?tag=hike&order=taken_desc", 1)
    assert walked == ["tg-m2-v", "tg-m1-c", "tg-m1-b", "tg-m1-a"]
    video = [i["uid"] for i in client.get("/api/attachments?tag=hike&type=video").json()]
    assert video == ["tg-m2-v"]


def test_an_unknown_tag_is_an_empty_page_never_a_404(lib):
    client, me = lib
    _seed_tags(me)
    r = client.get("/api/attachments?tag=nosuchtag&limit=1")
    assert r.status_code == 200
    assert r.json() == []
    assert "x-next-cursor" not in r.headers
    assert client.get("/api/attachments?limit=1").headers["x-next-cursor"]  # control
    b = client.get("/api/attachments/buckets?size=month&tag=nosuchtag")
    assert b.status_code == 200 and b.json() == []


TAG_BUCKETS = {
    "day": [
        ("2026-07-02", 1, "tg-m2-v"),
        ("2026-07-01", 1, "tg-m1-c"),
        ("2026-06-02", 1, "tg-m1-b"),
        ("2026-06-01", 1, "tg-m1-a"),
    ],
    "month": [("2026-07", 2, "tg-m2-v"), ("2026-06", 2, "tg-m1-b")],
    "year": [("2026", 4, "tg-m2-v")],
}


@pytest.mark.parametrize("size", ["day", "month", "year"])
def test_buckets_under_a_tag_count_only_that_tags_visible_media(lib, size):
    client, me = lib
    _seed_tags(me)
    assert _buckets(client, f"size={size}&tag=hike") == TAG_BUCKETS[size]


def test_tag_counts_are_per_picture_under_visibility_and_kind(lib):
    client, me = lib
    _seed_tags(me)

    def counts(query=""):
        r = client.get(f"/api/attachments/tags{query}")
        assert r.status_code == 200, r.text
        return [(t["name"], t["count"]) for t in r.json()]

    # dog: tg-m1's three pictures + tg-m4's two; hike: three + the video; B's private
    # hike memo is invisible; textonly carries no picture
    assert counts() == [("dog", 5), ("hike", 4)]
    assert counts("?type=image") == [("dog", 5), ("hike", 3)]
    assert counts("?type=video") == [("hike", 1)]
    grid = client.get("/api/attachments?tag=hike&type=video").json()
    assert dict(counts("?type=video"))["hike"] == len(grid)


# --- (l) route order ------------------------------------------------------------------------


def _matched_endpoint(path: str) -> str:
    scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.endpoint.__name__  # type: ignore[attr-defined]
    return "<none>"


def test_the_literal_routes_are_answered_by_their_own_handlers(lib):
    client, me = lib
    memo = _memo("ro-m", me, tags=("ro",))
    _att("ro-a", me, memo, capture_instant="2026-09-02T10:00:00Z")
    assert _matched_endpoint("/api/attachments/buckets") == "attachment_buckets"
    assert _matched_endpoint("/api/attachments/tags") == "attachment_tags"
    assert _matched_endpoint("/api/attachments/ro-a") == "attachment_meta"  # control
    b = client.get("/api/attachments/buckets?size=month")
    assert b.status_code == 200, b.text
    assert [set(i) for i in b.json()] == [
        {"key", "count", "coverUid", "coverThumbnailUrl", "coverMime", "tags"}
    ]
    t = client.get("/api/attachments/tags")
    assert t.status_code == 200, t.text
    assert t.json() == [{"name": "ro", "count": 1}]  # not {uid}'s AttachmentMeta or its 404


def test_the_new_endpoints_refuse_an_anonymous_caller(client):
    # The `{uid}` route refuses an anonymous caller too, so a 401 alone would stay green with
    # either literal route gone: the refusal must come from the handler the path is matched to.
    assert _matched_endpoint("/api/attachments/buckets") == "attachment_buckets"
    assert _matched_endpoint("/api/attachments/tags") == "attachment_tags"
    assert client.get("/api/attachments/buckets?size=month").status_code == 401
    assert client.get("/api/attachments/tags").status_code == 401


# --- (m) filename ---------------------------------------------------------------------------


def test_every_list_item_carries_its_stored_filename(lib):
    client, me = lib
    _att("fn-a", me, filename="IMG_0001.HEIC")
    _att("fn-b", me, filename="Ferien 2026 – Tag 1.mov", mime="video/quicktime")
    by_uid = {i["uid"]: i for i in client.get("/api/attachments").json()}
    assert by_uid["fn-a"].get("filename") == "IMG_0001.HEIC"
    assert by_uid["fn-b"].get("filename") == "Ferien 2026 – Tag 1.mov"


# --- pre-gate adversarial cases -------------------------------------------------------------


def test_boundary_an_exact_page_offers_no_cursor_and_an_empty_library_no_bucket(lib):
    client, me = lib
    assert client.get("/api/attachments/buckets?size=day").json() == []
    assert client.get("/api/attachments/tags").json() == []
    _att("bd-0", me, capture_instant="2026-09-01T10:00:00Z")
    _att("bd-1", me, capture_instant="2026-09-02T10:00:00Z")
    exact = client.get("/api/attachments?order=taken_desc&limit=2")
    assert [i["uid"] for i in exact.json()] == ["bd-1", "bd-0"]
    assert "x-next-cursor" not in exact.headers  # limit == rows: no phantom next page
    one = client.get("/api/attachments?order=taken_asc&limit=1")
    assert [i["uid"] for i in one.json()] == ["bd-0"]
    rest = client.get(
        f"/api/attachments?order=taken_asc&limit=1&cursor={one.headers['x-next-cursor']}"
    )
    assert [i["uid"] for i in rest.json()] == ["bd-1"]
    assert "x-next-cursor" not in rest.headers


def test_a_tag_name_is_matched_exactly_never_as_a_pattern(lib):
    client, me = lib
    literal = _memo("lit-m1", me, tags=("a_b", "50%"))
    _att("lit-1", me, literal, capture_instant="2026-09-01T10:00:00Z")
    lookalike = _memo("lit-m2", me, tags=("axb", "50 percent"))
    _att("lit-2", me, lookalike, capture_instant="2026-09-02T10:00:00Z")
    assert [i["uid"] for i in client.get("/api/attachments?tag=a_b").json()] == ["lit-1"]
    assert [i["uid"] for i in client.get("/api/attachments?tag=50%25").json()] == ["lit-1"]
    assert _buckets(client, "size=month&tag=a_b") == [("2026-09", 1, "lit-1")]


def test_a_malformed_taken_cursor_is_a_422_never_a_restart(lib):
    from app.services.cursors import encode_cursor_payload

    client, me = lib
    for n in range(3):
        _att(f"mc-{n}", me, capture_instant=f"2026-09-0{n + 1}T10:00:00Z")
    for payload in ("t|2026-09-01T10:00:00|1", "t|2026-09-01T10:00:00Z|", "t||1", "t|x|1"):
        r = client.get(f"/api/attachments?order=taken_desc&cursor={encode_cursor_payload(payload)}")
        assert r.status_code == 422, (payload, r.status_code, r.text)


def test_the_default_order_stays_id_even_where_the_taken_order_disagrees(lib):
    client, me = lib
    # ids ascend while the taken dates DESCEND, so an id order and a taken order cannot agree
    _att("sc-0", me, capture_instant="2026-09-03T10:00:00Z")
    _att("sc-1", me, capture_instant="2026-09-02T10:00:00Z")
    _att("sc-2", me, capture_instant="2026-09-01T10:00:00Z")
    assert [i["uid"] for i in client.get("/api/attachments").json()] == ["sc-2", "sc-1", "sc-0"]
    assert [i["uid"] for i in client.get("/api/attachments?order=asc").json()] == [
        "sc-0",
        "sc-1",
        "sc-2",
    ]
    taken = [i["uid"] for i in client.get("/api/attachments?order=taken_desc").json()]
    assert taken == ["sc-0", "sc-1", "sc-2"]  # control: the orders really differ


def test_an_insert_racing_a_taken_walk_neither_repeats_nor_skips(lib):
    client, me = lib
    for n in range(4):
        _att(f"rc-{n}", me, capture_instant=f"2026-09-0{n + 1}T10:00:00Z")
    first = client.get("/api/attachments?order=taken_desc&limit=2")
    assert [i["uid"] for i in first.json()] == ["rc-3", "rc-2"]
    # a second writer lands one item above the cursor and one below it, mid-walk
    _att("rc-new-top", me, capture_instant="2026-09-09T10:00:00Z")
    _att("rc-new-mid", me, capture_instant="2026-09-01T12:00:00Z")
    cursor = first.headers["x-next-cursor"]
    rest = client.get(f"/api/attachments?order=taken_desc&limit=10&cursor={cursor}")
    assert [i["uid"] for i in rest.json()] == ["rc-1", "rc-new-mid", "rc-0"]


# --- (n) the tags of the memo a picture hangs on, on the list and on the buckets ------------


def _seed_bucket_tags(me):
    """`_seed_tags` plus what a bucket's tags must keep out: another member's PRIVATE memo with a
    tag of its own on a day that is visible, and a memo with more tags than a client shows."""
    _seed_tags(me)
    secret = _memo("tg-secret", OTHER, visibility="PRIVATE", tags=("secret",))
    _att("tg-secret-a", OTHER, secret, capture_instant="2026-06-01T11:00:00Z")
    many = _memo("tg-many", me, tags=("e5", "a1", "d4", "c3", "b2"))
    _att("tg-many-a", me, many, capture_instant="2025-01-05T10:00:00Z")


def _bucket_tags(client, query):
    r = client.get(f"/api/attachments/buckets?{query}")
    assert r.status_code == 200, r.text
    return [(b["key"], b["tags"]) for b in r.json()]


def test_every_list_item_carries_the_tags_of_its_memo_sorted_by_name(lib):
    client, me = lib
    _seed_bucket_tags(me)
    r = client.get("/api/attachments?limit=500")
    assert r.status_code == 200, r.text
    tags = {i["uid"]: i["tags"] for i in r.json()}
    assert tags == {
        "tg-m1-a": ["dog", "hike"],
        "tg-m1-b": ["dog", "hike"],
        "tg-m1-c": ["dog", "hike"],
        "tg-m2-v": ["hike"],
        "tg-m4-a": ["dog"],
        "tg-m4-b": ["dog"],
        "tg-m6-a": [],  # a memo without tags
        "tg-orph": [],  # no memo at all
        "tg-many-a": ["a1", "b2", "c3", "d4", "e5"],  # every tag, the server truncates nothing
    }
    # the taken orders select through their own branch and must carry the field too
    walked = client.get("/api/attachments?order=taken_asc&limit=500&tag=hike").json()
    assert [(i["uid"], i["tags"]) for i in walked] == [
        ("tg-m1-a", ["dog", "hike"]),
        ("tg-m1-b", ["dog", "hike"]),
        ("tg-m1-c", ["dog", "hike"]),
        ("tg-m2-v", ["hike"]),
    ]


BUCKET_TAGS = {
    # tags of the bucket's visible pictures' memos: most pictures first, then by name
    "day": [
        ("2026-07-02", ["hike"]),
        ("2026-07-01", ["dog", "hike"]),
        ("2026-06-04", []),
        ("2026-06-03", []),
        ("2026-06-02", ["dog", "hike"]),
        ("2026-06-01", ["dog", "hike"]),  # the PRIVATE "secret" picture of that day stays out
        ("2026-05-02", ["dog"]),
        ("2026-05-01", ["dog"]),
        ("2025-01-05", ["a1", "b2", "c3", "d4", "e5"]),
    ],
    "month": [
        ("2026-07", ["hike", "dog"]),  # hike 2 pictures, dog 1
        ("2026-06", ["dog", "hike"]),  # 2 each: by name
        ("2026-05", ["dog"]),
        ("2025-01", ["a1", "b2", "c3", "d4", "e5"]),
    ],
    "year": [("2026", ["dog", "hike"]), ("2025", ["a1", "b2", "c3", "d4", "e5"])],
}


@pytest.mark.parametrize("size", ["day", "month", "year"])
def test_bucket_tags_are_its_visible_memos_tags_most_pictures_first(lib, size):
    client, me = lib
    _seed_bucket_tags(me)
    assert _bucket_tags(client, f"size={size}") == BUCKET_TAGS[size]


def test_bucket_tags_follow_the_type_and_tag_filters(lib):
    client, me = lib
    _seed_bucket_tags(me)
    # the video's memo carries hike alone: dog came only from the July picture the filter drops
    assert _bucket_tags(client, "size=month&type=video") == [("2026-07", ["hike"])]
    # a tag filter keeps the pictures, and the tags shown are all of their memos' tags
    assert _bucket_tags(client, "size=month&tag=dog") == [
        ("2026-07", ["dog", "hike"]),
        ("2026-06", ["dog", "hike"]),
        ("2026-05", ["dog"]),
    ]


def test_a_tag_name_with_list_punctuation_round_trips_whole(lib):
    """An array read back through a joined string would split this name in three."""
    client, me = lib
    odd = 'a,b "c" {d}'
    m = _memo("tg-odd", me, tags=(odd, "plain"))
    _att("tg-odd-a", me, m, capture_instant="2026-02-02T10:00:00Z")
    item = client.get("/api/attachments").json()[0]
    assert item["tags"] == [odd, "plain"]
    assert _bucket_tags(client, "size=day") == [("2026-02-02", [odd, "plain"])]


def _statements(client, path):
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        r = client.get(path)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)
    assert r.status_code == 200, r.text
    return r.json(), seen


def test_tags_cost_the_list_and_the_buckets_no_extra_statement(lib):
    """The counts are the ones the untouched tree measured (`test_assets.py`, MEMNG-171): the list
    is the session and user lookups, the library SELECT and the renditions load; the buckets the
    two lookups and the one bucket SELECT. Tags read per row or per bucket add statements; tags
    read in a second statement let the count and the tags see two different moments."""
    client, me = lib
    for n in range(3):
        m = _memo(f"tg-n1-{n}", me, tags=(f"t{n}", "shared"))
        _att(f"tg-n1-a{n}", me, m, capture_instant=f"2026-03-0{n + 1}T10:00:00Z")
    body, seen = _statements(client, "/api/attachments?limit=3")
    assert [i["tags"] for i in body] == [["shared", "t2"], ["shared", "t1"], ["shared", "t0"]]
    assert any("FROM attachments" in s for s in seen)  # positive control
    assert len(seen) == 4, seen
    body, seen = _statements(client, "/api/attachments/buckets?size=day")
    assert [b["tags"] for b in body] == [["shared", "t2"], ["shared", "t1"], ["shared", "t0"]]
    assert any("FROM attachments" in s for s in seen)  # positive control
    assert len(seen) == 3, seen
