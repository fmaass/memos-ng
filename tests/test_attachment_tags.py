"""Picture tags (MEMNG-863): a tag on the attachment itself, in its own namespace.

A picture tag is set and cleared by its owner (`PUT`/`DELETE /api/attachments/{uid}/tags/{name}`,
both 204 with no body, both idempotent) — except the heart, `favorites`, which any member who can
view the picture sets and clears — and read back on the attachment's own payloads. It is NOT
a memo tag: `?pictureTag=` and `GET /api/attachments/picture-tags` read only picture tags, while
`?tag=`, `GET /api/attachments/tags` and a collection's tag arm keep reading only the memo's, so a
memo hashtag `#favorites` puts nothing in Favorites and a picture tag moves no trip.

Every test starts from an empty library (`lib`), and every expected set is a literal written by
hand.
"""

import asyncio
from typing import Any

import pytest
from app import auth
from app.main import app
from app.routers import assets as assets_router
from app.services import attachment_tags as tag_svc
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from tests import test_collections_api as coll_api
from tests.test_assets import _walk
from tests.test_attachments_taken import _sql
from tests.test_collections_api import TAG, _att, _create, _members, _memo

lib = coll_api.lib
two = coll_api.two

FAV = "favorites"


def _tag(client, uid: str, name: str = FAV):
    return client.put(f"/api/attachments/{uid}/tags/{name}")


def _untag(client, uid: str, name: str = FAV):
    return client.delete(f"/api/attachments/{uid}/tags/{name}")


def _rows() -> list[tuple[str, str]]:
    """Every picture-tag row, straight from SQL — the oracle, not the endpoint."""
    return [
        (r[0], r[1])
        for r in _sql(
            "SELECT a.uid, t.name FROM attachment_tags x "
            "JOIN attachments a ON a.id = x.attachment_id "
            "JOIN tags t ON t.id = x.tag_id ORDER BY a.uid, t.name"
        )
    ]


def _meta_tags(client, uid: str) -> list[str]:
    r = client.get(f"/api/attachments/{uid}")
    assert r.status_code == 200, r.text
    return r.json()["tags"]


def _listed(client, query: str) -> set[str]:
    r = client.get(f"/api/attachments?limit=500&{query}")
    assert r.status_code == 200, r.text
    return {i["uid"] for i in r.json()}


def _picture_counts(client, query: str = "") -> list[dict[str, Any]]:
    r = client.get(f"/api/attachments/picture-tags?{query}")
    assert r.status_code == 200, r.text
    return r.json()


def _status_att(uid: str, owner: str, memo_id: int | None, *, mime: str, status: str) -> int:
    return _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
        "storage_path, blurhash) VALUES "
        "(:u,(SELECT id FROM users WHERE subject=:o),:m,'x',:mime,1,"
        "CAST(:st AS attachment_status),'/x','LKO2') RETURNING id",
        {"u": uid, "o": owner, "m": memo_id, "mime": mime, "st": status},
    )[0][0]


# --- writes -----------------------------------------------------------------------------------


def test_the_owner_adds_a_tag_and_a_repeat_changes_nothing(lib):
    client, me = lib
    _att("pt-a", me)
    for _ in range(2):
        r = _tag(client, "pt-a")
        assert r.status_code == 204, r.text
        assert r.content == b""
        assert _meta_tags(client, "pt-a") == [FAV]
        assert _rows() == [("pt-a", FAV)]


def test_the_tags_read_back_sorted(lib):
    client, me = lib
    _att("pt-s", me)
    for name in ("zebra", FAV, "Alpha"):
        assert _tag(client, "pt-s", name).status_code == 204
    assert _meta_tags(client, "pt-s") == sorted(["zebra", FAV, "Alpha"])


def test_remove_clears_the_tag_and_removing_an_absent_tag_is_204(lib):
    client, me = lib
    _att("pt-r", me)
    assert _tag(client, "pt-r").status_code == 204
    for _ in range(2):
        r = _untag(client, "pt-r")
        assert r.status_code == 204, r.text
        assert r.content == b""
        assert _meta_tags(client, "pt-r") == []
        assert _rows() == []
    # A name no tag row carries at all is just as absent.
    assert _untag(client, "pt-r", "never_seen").status_code == 204


def test_removing_one_tag_leaves_the_others_and_other_pictures_alone(lib):
    client, me = lib
    _att("pt-x", me)
    _att("pt-y", me)
    for uid in ("pt-x", "pt-y"):
        assert _tag(client, uid).status_code == 204
    assert _tag(client, "pt-x", "keep").status_code == 204
    assert _untag(client, "pt-x").status_code == 204
    assert _rows() == [("pt-x", "keep"), ("pt-y", FAV)]


def test_any_viewer_hearts_and_unhearts_another_members_picture(two):
    """The heart is the household's, not the uploader's: B sets and clears it on A's picture."""
    a_client, me, b_client, _ = two
    _att("fam-pub", me, _memo("fam-pub-m", me, visibility="PUBLIC"))
    _att("fam-prot", me, _memo("fam-prot-m", me, visibility="PROTECTED"))
    for uid in ("fam-pub", "fam-prot"):
        r = _tag(b_client, uid)
        assert r.status_code == 204, r.text
        assert r.content == b""
    assert _rows() == [("fam-prot", FAV), ("fam-pub", FAV)]
    assert _meta_tags(a_client, "fam-pub") == [FAV]
    assert _meta_tags(b_client, "fam-pub") == [FAV]
    # B clears B's own heart, and A's heart is just as clearable by B.
    assert _untag(b_client, "fam-pub").status_code == 204
    assert _rows() == [("fam-prot", FAV)]
    assert _tag(a_client, "fam-pub").status_code == 204
    r = _untag(b_client, "fam-pub")
    assert r.status_code == 204, r.text
    assert r.content == b""
    assert _meta_tags(a_client, "fam-pub") == []
    assert _rows() == [("fam-prot", FAV)]


def test_a_heart_on_a_picture_the_caller_cannot_view_is_404_and_writes_nothing(two):
    """`_viewable`'s 404: another member's PRIVATE picture, their orphan and an unknown uid answer
    alike, so a write never tells a member which uids exist."""
    a_client, me, b_client, _ = two
    _att("fam-priv", me, _memo("fam-priv-m", me, visibility="PRIVATE"))
    _att("fam-orph", me)
    for uid in ("fam-priv", "fam-orph"):
        assert _tag(a_client, uid).status_code == 204  # the owner can (the positive control)
        assert b_client.get(f"/api/attachments/{uid}").status_code == 404  # B cannot see it
    for uid in ("fam-priv", "fam-orph", "no-such-uid"):
        assert _tag(b_client, uid).status_code == 404
        assert _untag(b_client, uid).status_code == 404
    assert _rows() == [("fam-orph", FAV), ("fam-priv", FAV)]


@pytest.mark.parametrize("name", ["keep", "Favorites", "favorites_2"])
def test_every_other_tag_name_stays_owner_only(two, name):
    """Only the heart is family-wide; the match is exact, so a near name is just another tag."""
    a_client, me, b_client, _ = two
    _att("fam-own", me, _memo("fam-own-m", me, visibility="PUBLIC"))
    assert b_client.get("/api/attachments/fam-own").status_code == 200  # B can see it
    assert _tag(b_client, "fam-own", name).status_code == 404
    assert _rows() == []
    assert _tag(a_client, "fam-own", name).status_code == 204
    assert _untag(b_client, "fam-own", name).status_code == 404
    assert _rows() == [("fam-own", name)]


def test_a_heart_set_by_one_member_lists_for_every_viewer(two):
    """The Favorites card's reads — `?pictureTag=favorites` and the picture-tag counts — show B's
    heart on A's picture to A and to B alike."""
    a_client, me, b_client, _ = two
    _att("fam-list", me, _memo("fam-list-m", me, visibility="PUBLIC"))
    _att("fam-plain", me, _memo("fam-plain-m", me, visibility="PUBLIC"))
    assert _tag(b_client, "fam-list").status_code == 204
    for client in (a_client, b_client):
        assert _listed(client, f"pictureTag={FAV}") == {"fam-list"}
        assert _picture_counts(client) == [{"name": FAV, "count": 1}]
    assert _listed(a_client, f"pictureTag={FAV}&mine=true") == {"fam-list"}
    assert _listed(b_client, f"pictureTag={FAV}&mine=true") == set()


async def test_two_members_hearting_one_picture_at_once_leave_one_row(two, monkeypatch):
    """A and B, each by its own token, held past the lookup until both have looked up: the pair's
    primary key makes them one row, and neither request errors."""
    a_client, me, b_client, _ = two
    _att("fam-race", me, _memo("fam-race-m", me, visibility="PUBLIC"))
    tokens = []
    for client in (a_client, b_client):
        created = client.post("/api/pats", json={"name": "device"})
        assert created.status_code == 201, created.text
        tokens.append(created.json()["token"])
    real = tag_svc.add
    both_looked_up = asyncio.Barrier(2)
    entered: list[int] = []

    async def held(db, att, name):
        entered.append(1)
        async with asyncio.timeout(10):
            await both_looked_up.wait()
        return await real(db, att, name)

    monkeypatch.setattr(tag_svc, "add", held)
    path = f"/api/attachments/fam-race/tags/{FAV}"
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {tokens[0]}"},
        ) as a_bearer,
        AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {tokens[1]}"},
        ) as b_bearer,
    ):
        try:
            results = await asyncio.gather(a_bearer.put(path), b_bearer.put(path))
        finally:
            await auth.drain_pat_stamp_tasks()
    assert [r.status_code for r in results] == [204, 204], [r.text for r in results]
    assert len(entered) == 2  # both really overlapped past the lookup
    assert _rows() == [("fam-race", FAV)]


@pytest.mark.parametrize(
    ("segment", "why"),
    [
        ("%23x", "a hash sign"),
        ("x" * 256, "256 characters"),
        ("a-b", "a hyphen"),
        ("a%20b", "a space"),
    ],
)
def test_an_invalid_name_is_422_and_writes_nothing(lib, segment, why):
    client, me = lib
    _att("pt-bad", me)
    assert _tag(client, "pt-bad", segment).status_code == 422, why
    assert _untag(client, "pt-bad", segment).status_code == 422, why
    assert _rows() == []


def test_a_name_of_255_characters_is_accepted(lib):
    client, me = lib
    _att("pt-255", me)
    assert _tag(client, "pt-255", "x" * 255).status_code == 204
    assert _rows() == [("pt-255", "x" * 255)]


def test_an_empty_name_segment_is_answered_by_routing_never_204(lib):
    client, me = lib
    _att("pt-empty", me)
    for method in ("put", "delete"):
        r = getattr(client, method)("/api/attachments/pt-empty/tags/")
        assert r.status_code in (404, 405), (method, r.status_code)
    assert _rows() == []


async def test_two_concurrent_adds_of_a_new_tag_leave_one_tag_and_one_row(
    lib, pat_client, monkeypatch
):
    """Both requests are held after their lookups until both have looked up, so both create the
    tag and both insert the pair: the unique name and the pair's primary key — not a read before
    the write — make them one tag row and one picture-tag row, and neither request errors."""
    bearer, me, _ = pat_client
    _att("pt-race", me)
    real = tag_svc.add
    both_looked_up = asyncio.Barrier(2)
    entered: list[int] = []

    async def held(db, att, name):
        entered.append(1)
        async with asyncio.timeout(10):
            await both_looked_up.wait()
        return await real(db, att, name)

    monkeypatch.setattr(tag_svc, "add", held)
    path = "/api/attachments/pt-race/tags/brand_new"
    results = await asyncio.gather(bearer.put(path), bearer.put(path))
    assert [r.status_code for r in results] == [204, 204], [r.text for r in results]
    assert len(entered) == 2  # both really overlapped past the lookup
    assert _rows() == [("pt-race", "brand_new")]
    assert _sql("SELECT count(*) FROM tags WHERE name='brand_new'")[0][0] == 1


def test_an_add_whose_attachment_was_deleted_meanwhile_is_404(lib, monkeypatch):
    client, me = lib
    _att("pt-gone", me)
    real = tag_svc.add

    async def deleted_first(db, att, name):
        _sql("DELETE FROM attachments WHERE uid='pt-gone'")
        return await real(db, att, name)

    monkeypatch.setattr(tag_svc, "add", deleted_first)
    assert _tag(client, "pt-gone").status_code == 404
    assert _rows() == []
    monkeypatch.setattr(tag_svc, "add", real)
    _att("pt-after", me)
    assert _tag(client, "pt-after").status_code == 204  # the session recovered


def test_an_integrity_error_that_is_not_a_vanished_attachment_is_not_a_404(lib, monkeypatch):
    client, me = lib
    _att("pt-ie", me)

    class _Unique(Exception):
        sqlstate = "23505"

    async def unique_violation(db, att, name):
        raise IntegrityError("INSERT …", {}, _Unique())

    monkeypatch.setattr(tag_svc, "add", unique_violation)
    assert _tag(client, "pt-ie").status_code == 500


def test_deleting_a_tagged_attachment_leaves_no_picture_tag_row(lib):
    client, me = lib
    _att("pt-del", me)
    assert _tag(client, "pt-del").status_code == 204
    assert client.delete("/api/attachments/pt-del").status_code == 204
    assert _rows() == []


# --- reads ------------------------------------------------------------------------------------


def _separation(me: str) -> None:
    """`memo-fav` hangs on a memo tagged `#favorites`; `pic-fav` is picture-tagged favorites on an
    untagged memo; `plain` is neither."""
    _att("memo-fav", me, _memo("sep-tagged", me, tags=(FAV,)))
    _att("pic-fav", me, _memo("sep-plain", me))
    _att("plain", me)


def test_picture_tag_lists_the_picture_tagged_and_not_a_memo_tagged_sibling(lib):
    client, me = lib
    _separation(me)
    assert _tag(client, "pic-fav").status_code == 204
    assert _listed(client, f"pictureTag={FAV}") == {"pic-fav"}


def test_the_memo_tag_filter_is_unchanged_by_a_picture_tag(lib):
    client, me = lib
    _separation(me)
    before = _listed(client, f"tag={FAV}")
    assert _tag(client, "pic-fav").status_code == 204
    assert _listed(client, f"tag={FAV}") == before == {"memo-fav"}


def test_the_item_carries_its_picture_tags_beside_its_memos_tags(lib):
    client, me = lib
    _separation(me)
    assert _tag(client, "pic-fav").status_code == 204
    assert _tag(client, "memo-fav", "zz").status_code == 204
    r = client.get("/api/attachments?limit=500")
    by_uid = {i["uid"]: (i["tags"], i["pictureTags"]) for i in r.json()}
    assert by_uid == {
        "memo-fav": ([FAV], ["zz"]),
        "pic-fav": ([], [FAV]),
        "plain": ([], []),
    }


def test_picture_tag_narrows_together_with_type_and_mine(two):
    a_client, me, b_client, other = two
    shared = _memo("n-shared", me, visibility="PUBLIC")
    _att("n-a-img", me, shared)
    _att("n-a-vid", me, shared, mime="video/mp4")
    _att("n-b-img", other, shared)
    _att("n-a-untagged", me, shared)
    for client, uid in ((a_client, "n-a-img"), (a_client, "n-a-vid"), (b_client, "n-b-img")):
        assert _tag(client, uid).status_code == 204
    assert _listed(a_client, f"pictureTag={FAV}") == {"n-a-img", "n-a-vid", "n-b-img"}
    assert _listed(a_client, f"pictureTag={FAV}&type=image") == {"n-a-img", "n-b-img"}
    assert _listed(a_client, f"pictureTag={FAV}&mine=true") == {"n-a-img", "n-a-vid"}
    assert _listed(a_client, f"pictureTag={FAV}&type=image&mine=true") == {"n-a-img"}
    assert _listed(a_client, "pictureTag=unknown") == set()


def test_the_buckets_take_the_picture_tag_too(lib):
    client, me = lib
    _att("bk-in", me, capture_instant="2034-01-02T12:00:00Z")
    _att("bk-out", me, capture_instant="2035-01-02T12:00:00Z")
    assert _tag(client, "bk-in").status_code == 204
    r = client.get(f"/api/attachments/buckets?size=year&pictureTag={FAV}")
    assert r.status_code == 200, r.text
    assert [(b["key"], b["count"], b["coverUid"]) for b in r.json()] == [("2034", 1, "bk-in")]


def test_the_picture_tag_counts_count_each_picture_once_and_ignore_memo_tags(lib):
    client, me = lib
    _separation(me)
    tags_before = client.get("/api/attachments/tags").json()
    assert _picture_counts(client) == []
    assert _tag(client, "pic-fav").status_code == 204
    assert _tag(client, "pic-fav").status_code == 204
    assert _tag(client, "plain", "other").status_code == 204
    assert _picture_counts(client) == [
        {"name": FAV, "count": 1},
        {"name": "other", "count": 1},
    ]
    assert client.get("/api/attachments/tags").json() == tags_before == [{"name": FAV, "count": 1}]


def test_the_picture_tag_counts_order_most_items_first_then_by_name(lib):
    client, me = lib
    for uid in ("o-1", "o-2", "o-3"):
        _att(uid, me)
    for uid, name in (("o-1", "b"), ("o-2", "b"), ("o-1", "a"), ("o-3", "c")):
        assert _tag(client, uid, name).status_code == 204
    assert [(c["name"], c["count"]) for c in _picture_counts(client)] == [
        ("b", 2),
        ("a", 1),
        ("c", 1),
    ]


# A ready, a processing and a failed picture, of both kinds and both owners, on memos of two
# visibilities and as an orphan — every one of them picture-tagged favorites. (query, expected).
STATUS_CASES: list[tuple[str, set[str]]] = [
    ("", {"s-ready", "s-proc", "s-fail", "s-vid", "s-orph", "s-b-pub"}),
    ("type=image", {"s-ready", "s-proc", "s-fail", "s-orph", "s-b-pub"}),
    ("type=video", {"s-vid"}),
    ("mine=true", {"s-ready", "s-proc", "s-fail", "s-vid", "s-orph"}),
    ("mine=false", {"s-ready", "s-proc", "s-fail", "s-vid", "s-orph", "s-b-pub"}),
    ("visibility=PUBLIC", {"s-ready", "s-proc", "s-b-pub"}),
    ("visibility=PRIVATE", {"s-fail", "s-vid"}),
    ("visibility=PROTECTED", set()),
    ("type=image&mine=true&visibility=PUBLIC", {"s-ready", "s-proc"}),
]


@pytest.fixture
def statuses(two):
    a_client, me, b_client, other = two
    pub = _memo("s-pub", me, visibility="PUBLIC")
    priv = _memo("s-priv", me, visibility="PRIVATE")
    b_pub = _memo("s-bm", other, visibility="PUBLIC")
    b_priv = _memo("s-bp", other, visibility="PRIVATE")
    mine = [
        ("s-ready", pub, "image/jpeg", "ready"),
        ("s-proc", pub, "image/jpeg", "processing"),
        ("s-fail", priv, "image/jpeg", "failed"),
        ("s-vid", priv, "video/mp4", "processing"),
        ("s-orph", None, "image/png", "failed"),
    ]
    for uid, memo, mime, status in mine:
        _status_att(uid, me, memo, mime=mime, status=status)
        assert _tag(a_client, uid).status_code == 204
    for uid, memo in (("s-b-pub", b_pub), ("s-b-priv", b_priv)):
        _status_att(uid, other, memo, mime="image/jpeg", status="processing")
        assert _tag(b_client, uid).status_code == 204
    return a_client


@pytest.mark.parametrize(("query", "expected"), STATUS_CASES)
def test_the_picture_tag_count_is_the_length_of_the_fully_paged_listing(statuses, query, expected):
    sep = "&" if query else ""
    walked, _ = _walk(statuses, f"/api/attachments?pictureTag={FAV}{sep}{query}", 1)
    assert len(walked) == len(set(walked))  # no page repeated a row
    assert set(walked) == expected
    counts = {c["name"]: c["count"] for c in _picture_counts(statuses, query)}
    assert counts.get(FAV, 0) == len(walked)


def test_a_collection_tag_does_not_gain_a_picture_tagged_member(lib):
    client, me = lib
    _att("c-memo", me, _memo("c-m", me, tags=(TAG,)))
    _att("c-pic", me, _memo("c-p", me))
    uid = _create(client, tag=TAG)["uid"]
    assert _members(client, uid) == ["c-memo"]
    assert _tag(client, "c-pic", TAG).status_code == 204
    assert _members(client, uid) == ["c-memo"]
    assert client.get(f"/api/collections/{uid}").json()["memberCount"] == 1


def test_a_non_viewer_sees_neither_the_picture_nor_its_count(two):
    a_client, me, b_client, other = two
    _att("hid-priv", other, _memo("hid-m", other, visibility="PRIVATE"))
    _att("hid-orph", other)
    for uid in ("hid-priv", "hid-orph"):
        assert _tag(b_client, uid).status_code == 204
    # The owner sees both (the positive control), the other member neither.
    assert _listed(b_client, f"pictureTag={FAV}") == {"hid-priv", "hid-orph"}
    assert _picture_counts(b_client) == [{"name": FAV, "count": 2}]
    assert _listed(a_client, f"pictureTag={FAV}") == set()
    assert _picture_counts(a_client) == []


def test_the_memo_payload_and_the_map_marker_carry_the_picture_tags(lib):
    client, me = lib
    _att("ref-a", me, _memo("ref-m", me))
    _att("ref-b", me, _memo("ref-m2", me))
    _sql("UPDATE attachments SET latitude=47.0, longitude=8.0 WHERE uid IN ('ref-a', 'ref-b')")
    assert _tag(client, "ref-a").status_code == 204
    assert _tag(client, "ref-a", "alpha").status_code == 204
    memo = client.get("/api/memos/ref-m").json()
    assert [a["tags"] for a in memo["attachments"]] == [["alpha", FAV]]
    untagged = client.get("/api/memos/ref-m2").json()
    assert [a["tags"] for a in untagged["attachments"]] == [[]]
    r = client.get("/api/map/markers")
    assert r.status_code == 200, r.text
    markers = {m["uid"]: m["tags"] for m in r.json()["markers"]}
    assert markers == {"ref-a": ["alpha", FAV], "ref-b": []}


async def test_the_write_routes_answer_a_bearer_token(lib, pat_client):
    """`require_pat_or_user`: the native client writes with its token."""
    _, me = lib
    bearer, _, _ = pat_client
    _att("pt-pat", me)
    assert (await bearer.put(f"/api/attachments/pt-pat/tags/{FAV}")).status_code == 204
    assert _rows() == [("pt-pat", FAV)]
    assert (await bearer.delete(f"/api/attachments/pt-pat/tags/{FAV}")).status_code == 204
    assert _rows() == []


def test_the_write_routes_are_registered_ahead_of_the_uid_catch_alls():
    """`/attachments/picture-tags` must not be answered as the uid `picture-tags`."""
    paths = [getattr(r, "path", "") for r in assets_router.router.routes]
    assert paths.index("/api/attachments/picture-tags") < paths.index("/api/attachments/{uid}")


def test_the_published_404_names_the_heart_as_a_viewer_write():
    """The OpenAPI text a client is generated from must not still say the heart is owner-only."""
    paths = app.openapi()["paths"]["/api/attachments/{uid}/tags/{name}"]
    for method in ("put", "delete"):
        text = paths[method]["responses"]["404"]["description"]
        assert "for `favorites`, one the caller cannot view" in text, text
        assert "favorites" in paths[method]["description"]
