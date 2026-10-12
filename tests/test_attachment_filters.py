"""`mine` and `visibility` on every library surface, and `visibility` on the item (MEMNG-785).

`library_visible` stays the authorization predicate; the two parameters only NARROW what it
admits, through `library_filters`, so the list, the buckets, the tag counts and a collection's
member read answer every combination with the same rows. `mine=true` is ATTACHMENT ownership — the
item's own `mine` — so the caller's orphan stays under it and another member's picture on the
caller's memo does not; `visibility=X` is the memo's visibility, so an orphan (no memo) is absent
under any value. PRIVATE is accepted, as the memo listing accepts it, and still never reaches
another member's private memo.

One fixture covers every category at once: each of A's three visibilities, B's PUBLIC and
PROTECTED (visible to A) and PRIVATE (never), A's and B's orphans, and B's picture on A's public
memo. The expected sets are literals written by hand.
"""

from typing import Any

import pytest

from tests import test_collections_api as coll_api
from tests.test_collections_api import TAG, _att, _create, _memo

# The collections module's fixtures, re-exported so pytest finds them here.
lib = coll_api.lib
two = coll_api.two

YEAR = "2033"
TAKEN = "2033-05-05T12:00:00Z"

EVERYTHING_A_SEES = {"a_pub", "a_prot", "a_priv", "a_orph", "b_pub", "b_prot", "b_on_a"}
ORPHANS = {"a_orph"}

# (query, the uids A sees under it). Every combination of the two parameters, each value once.
COMBINATIONS: list[tuple[str, set[str]]] = [
    ("", EVERYTHING_A_SEES),
    ("mine=false", EVERYTHING_A_SEES),
    ("mine=true", {"a_pub", "a_prot", "a_priv", "a_orph"}),
    ("visibility=PUBLIC", {"a_pub", "b_pub", "b_on_a"}),
    ("visibility=PROTECTED", {"a_prot", "b_prot"}),
    ("visibility=PRIVATE", {"a_priv"}),
    ("mine=true&visibility=PUBLIC", {"a_pub"}),
    ("mine=true&visibility=PROTECTED", {"a_prot"}),
    ("mine=true&visibility=PRIVATE", {"a_priv"}),
    ("mine=false&visibility=PROTECTED", {"a_prot", "b_prot"}),
]


def _seed(me: str, other: str) -> None:
    a_pub = _memo("fa-pub", me, visibility="PUBLIC", tags=(TAG,))
    a_prot = _memo("fa-prot", me, visibility="PROTECTED", tags=(TAG,))
    a_priv = _memo("fa-priv", me, visibility="PRIVATE", tags=(TAG,))
    b_pub = _memo("fb-pub", other, visibility="PUBLIC", tags=(TAG,))
    b_prot = _memo("fb-prot", other, visibility="PROTECTED", tags=(TAG,))
    b_priv = _memo("fb-priv", other, visibility="PRIVATE", tags=(TAG,))
    for uid, owner, memo in (
        ("a_pub", me, a_pub),
        ("a_prot", me, a_prot),
        ("a_priv", me, a_priv),
        ("a_orph", me, None),
        ("b_pub", other, b_pub),
        ("b_prot", other, b_prot),
        ("b_priv", other, b_priv),
        ("b_orph", other, None),
        ("b_on_a", other, a_pub),
    ):
        _att(uid, owner, memo, capture_instant=TAKEN)


@pytest.fixture
def seeded(two):
    a_client, me, b_client, other = two
    _seed(me, other)
    return a_client


def _list(client, path: str, query: str) -> list[dict[str, Any]]:
    sep = "&" if query else ""
    r = client.get(f"{path}?limit=500{sep}{query}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize(("query", "expected"), COMBINATIONS)
def test_the_library_list_narrows_by_mine_and_visibility(seeded, query, expected):
    assert {i["uid"] for i in _list(seeded, "/api/attachments", query)} == expected


@pytest.mark.parametrize(("query", "expected"), COMBINATIONS)
def test_the_buckets_count_and_cover_exactly_the_narrowed_rows(seeded, query, expected):
    sep = "&" if query else ""
    r = seeded.get(f"/api/attachments/buckets?size=year{sep}{query}")
    assert r.status_code == 200, r.text
    buckets = r.json()
    assert [b["key"] for b in buckets] == [YEAR]
    assert buckets[0]["count"] == len(expected)
    assert buckets[0]["coverUid"] in expected


@pytest.mark.parametrize(("query", "expected"), COMBINATIONS)
def test_the_tag_counts_count_the_narrowed_rows_and_never_an_orphan(seeded, query, expected):
    r = seeded.get(f"/api/attachments/tags?{query}")
    assert r.status_code == 200, r.text
    assert r.json() == [{"name": TAG, "count": len(expected - ORPHANS)}]


@pytest.mark.parametrize(("query", "expected"), COMBINATIONS)
def test_the_member_read_narrows_like_the_library(seeded, query, expected):
    uid = _create(seeded, tag=TAG)["uid"]
    members = _list(seeded, f"/api/collections/{uid}/attachments", query)
    assert {i["uid"] for i in members} == expected - ORPHANS


def test_the_item_names_its_memos_visibility_and_null_for_an_orphan(seeded):
    by_uid = {i["uid"]: i["visibility"] for i in _list(seeded, "/api/attachments", "")}
    assert by_uid == {
        "a_pub": "PUBLIC",
        "a_prot": "PROTECTED",
        "a_priv": "PRIVATE",
        "a_orph": None,
        "b_pub": "PUBLIC",
        "b_prot": "PROTECTED",
        "b_on_a": "PUBLIC",
    }


def test_the_member_read_and_a_cover_carry_the_visibility_too(seeded):
    created = _create(seeded, tag=TAG, coverAttachmentUid="a_prot")
    assert created["cover"]["visibility"] == "PROTECTED"
    members = _list(seeded, f"/api/collections/{created['uid']}/attachments", "")
    assert {i["uid"]: i["visibility"] for i in members}["b_prot"] == "PROTECTED"


def test_an_orphan_cover_renders_with_a_null_visibility(seeded):
    created = _create(seeded, tag=TAG, coverAttachmentUid="a_orph")
    assert created["cover"]["uid"] == "a_orph"
    assert created["cover"]["visibility"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/attachments",
        "/api/attachments/buckets?size=year",
        "/api/attachments/tags",
    ],
)
def test_an_unknown_visibility_is_422_on_every_library_surface(seeded, path):
    sep = "&" if "?" in path else "?"
    assert seeded.get(f"{path}{sep}visibility=SECRET").status_code == 422


def test_an_unknown_visibility_is_422_on_the_member_read(seeded):
    uid = _create(seeded, tag=TAG)["uid"]
    assert seeded.get(f"/api/collections/{uid}/attachments?visibility=SECRET").status_code == 422


def test_b_sees_its_own_slice_under_mine_and_never_as_private(two):
    """The other side of the same fixture: the narrowing is the CALLER's, and PRIVATE under
    another member's eyes stays bounded by the authorization predicate."""
    a_client, me, b_client, other = two
    _seed(me, other)
    assert {i["uid"] for i in _list(b_client, "/api/attachments", "mine=true")} == {
        "b_pub",
        "b_prot",
        "b_priv",
        "b_orph",
        "b_on_a",
    }
    assert {i["uid"] for i in _list(b_client, "/api/attachments", "visibility=PRIVATE")} == {
        "b_priv"
    }
