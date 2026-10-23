"""Albums — the collection kind whose pictures are chosen one by one (MEMNG-802, plan 4295 Phase S).

A trip's membership is derived from its tag and span; an album has neither, and its members are the
rows of `collection_members`. Everything else is shared with trips: the household edits every
album, the member read is the library's projection and cursor under the caller's visibility, and a
picture with no memo or a detached one never belongs.

The library is empty at the start of every test (`lib` / `two` from the trip suite), expected
member lists are literals written by hand, and their ORDER is asked of SQL (`_in_taken_desc`),
never of the endpoint under test.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from app.services import collections as coll_svc

from tests import test_collections_api as coll_api
from tests.test_attachments_taken import _sql
from tests.test_collections_api import END, INSIDE, OUTSIDE, START, TAG, _att, _in_taken_desc, _memo

lib = coll_api.lib
two = coll_api.two

BEFORE_ALBUMS_REVISION = "a584468dae25"


def _album(client, title: str = "At the sea", **fields: Any) -> dict[str, Any]:
    r = client.post("/api/collections", json={"title": title, "kind": "album", **fields})
    assert r.status_code == 201, r.text
    return r.json()


def _add(client, uid: str, uids: list[str]) -> dict[str, str]:
    r = client.post(f"/api/collections/{uid}/attachments", json={"attachmentUids": uids})
    assert r.status_code == 200, r.text
    return {i["attachmentUid"]: i["outcome"] for i in r.json()["results"]}


def _member_rows(collection_uid: str | None = None) -> list[str]:
    """The stored member rows, as attachment uids — read from SQL, not from the endpoint."""
    if collection_uid is None:
        rows = _sql(
            "SELECT a.uid FROM collection_members cm JOIN attachments a ON a.id = cm.attachment_id"
        )
    else:
        rows = _sql(
            "SELECT a.uid FROM collection_members cm "
            "JOIN attachments a ON a.id = cm.attachment_id "
            "JOIN collections c ON c.id = cm.collection_id WHERE c.uid = :u",
            {"u": collection_uid},
        )
    return sorted(r[0] for r in rows)


def _seed_member(collection_uid: str, attachment_uid: str) -> None:
    """A member row written past the route, for states the route itself would refuse to create
    (a picture that later became invisible or detached)."""
    _sql(
        "INSERT INTO collection_members (collection_id, attachment_id) VALUES "
        "((SELECT id FROM collections WHERE uid=:c),(SELECT id FROM attachments WHERE uid=:a))",
        {"c": collection_uid, "a": attachment_uid},
    )


def _stored(uid: str) -> dict[str, Any]:
    kind, title, tag, start, end, cover = _sql(
        "SELECT c.kind, c.title, c.tag, c.span_start, c.span_end, a.uid FROM collections c "
        "LEFT JOIN attachments a ON a.id = c.cover_attachment_id WHERE c.uid = :u",
        {"u": uid},
    )[0]
    return {"kind": kind, "title": title, "tag": tag, "start": start, "end": end, "cover": cover}


def _updated_at(uid: str) -> datetime:
    return _sql("SELECT updated_at FROM collections WHERE uid=:u", {"u": uid})[0][0]


def _age(uid: str) -> datetime:
    """Push the record's `updated_at` into the past, so a later write is visible as a change."""
    _sql(
        "UPDATE collections SET updated_at = '2001-01-01T00:00:00Z' WHERE uid=:u",
        {"u": uid},
    )
    return _updated_at(uid)


# --- the kind-aware validator (service, every input category) --------------------------------


AWARE = datetime.fromisoformat(START)
LATER = datetime.fromisoformat(END)


@pytest.mark.parametrize(
    ("kind", "tag", "start", "end", "valid"),
    [
        ("album", None, None, None, True),  # an album is a title alone
        ("album", "sea", None, None, False),  # an album with a tag
        ("album", None, AWARE, LATER, False),  # an album with a whole span
        ("album", None, AWARE, None, False),  # an album with half a span
        ("album", "sea", AWARE, LATER, False),  # an album with both
        ("trip", None, None, None, False),  # a trip with neither source
        ("trip", "sea", None, None, True),  # a trip of a tag
        ("trip", None, AWARE, LATER, True),  # a trip of a span
        ("trip", "sea", AWARE, LATER, True),  # a trip of both
        ("folder", "sea", None, None, False),  # a kind that does not exist
    ],
)
def test_the_validator_judges_every_kind_and_source_combination(kind, tag, start, end, valid):
    call = {"kind": kind, "tag": tag, "span_start": start, "span_end": end, "tz": None}
    if valid:
        coll_svc._validate(**call)
    else:
        with pytest.raises(coll_svc.CollectionInvalid):
            coll_svc._validate(**call)


# --- create -----------------------------------------------------------------------------------


def test_an_album_is_created_with_a_title_alone_and_answers_its_kind(lib):
    client, _ = lib
    created = _album(client, "At the sea")
    assert created["kind"] == "album"
    assert created["tag"] is None and created["spanStart"] is None and created["spanEnd"] is None
    assert created["memberCount"] == 0
    assert _stored(created["uid"])["kind"] == "album"
    assert client.get(f"/api/collections/{created['uid']}").json()["kind"] == "album"


def test_a_trip_created_without_a_kind_is_a_trip(lib):
    client, _ = lib
    created = coll_api._create(client, tag=TAG)
    assert created["kind"] == "trip"
    assert _stored(created["uid"])["kind"] == "trip"
    explicit = coll_api._create(client, tag=TAG, kind="trip")
    assert explicit["kind"] == "trip"


@pytest.mark.parametrize(
    "fields",
    [
        {"tag": TAG},
        {"spanStart": START, "spanEnd": END},
        {"spanStart": START},
        {"tag": TAG, "spanStart": START, "spanEnd": END},
    ],
)
def test_an_album_with_a_tag_or_a_span_is_422_and_stores_nothing(lib, fields):
    client, _ = lib
    r = client.post("/api/collections", json={"title": "A", "kind": "album", **fields})
    assert r.status_code == 422, r.text
    assert _sql("SELECT count(*) FROM collections")[0][0] == 0


def test_an_unknown_kind_is_422(lib):
    client, _ = lib
    r = client.post("/api/collections", json={"title": "A", "kind": "folder", "tag": TAG})
    assert r.status_code == 422, r.text
    assert _sql("SELECT count(*) FROM collections")[0][0] == 0


def test_a_keyed_replay_answers_the_stored_kind_whatever_the_replay_says(lib):
    client, _ = lib
    first = _album(client, idempotencyKey="k-album-1")
    replay = client.post(
        "/api/collections",
        json={"title": "T", "kind": "trip", "tag": TAG, "idempotencyKey": "k-album-1"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["uid"] == first["uid"] and replay.json()["kind"] == "album"
    assert _sql("SELECT count(*) FROM collections")[0][0] == 1


# --- patch ------------------------------------------------------------------------------------


def test_an_album_is_renamed_and_its_cover_set_and_cleared(lib):
    client, me = lib
    _att("cov-p", me, _memo("cov-m", me))
    uid = _album(client, "Old")["uid"]
    r = client.patch(f"/api/collections/{uid}", json={"title": "New"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "New" and r.json()["kind"] == "album"
    r = client.patch(f"/api/collections/{uid}", json={"coverAttachmentUid": "cov-p"})
    assert r.status_code == 200, r.text
    assert r.json()["cover"]["uid"] == "cov-p"
    assert _stored(uid)["cover"] == "cov-p"
    r = client.patch(f"/api/collections/{uid}", json={"coverAttachmentUid": None})
    assert r.status_code == 200, r.text
    assert r.json()["cover"] is None
    assert _stored(uid) == {
        "kind": "album",
        "title": "New",
        "tag": None,
        "start": None,
        "end": None,
        "cover": None,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"tag": TAG},
        {"spanStart": START, "spanEnd": END},
        {"spanStart": START},
        {"kind": "trip"},
        {"kind": "album"},
        {"title": "Renamed", "kind": "trip"},
    ],
)
def test_an_album_refuses_a_source_or_a_kind_on_patch_and_is_unchanged(lib, change):
    client, me = lib
    _att("keep-p", me, _memo("keep-m", me))
    uid = _album(client, "Keep", coverAttachmentUid="keep-p")["uid"]
    before = _stored(uid)
    r = client.patch(f"/api/collections/{uid}", json=change)
    assert r.status_code == 422, (change, r.text)
    assert _stored(uid) == before


def test_a_trip_refuses_a_kind_on_patch_and_is_unchanged(lib):
    client, _ = lib
    uid = coll_api._create(client, title="Keep", tag=TAG)["uid"]
    before = _stored(uid)
    r = client.patch(f"/api/collections/{uid}", json={"kind": "album", "tag": None})
    assert r.status_code == 422, r.text
    assert _stored(uid) == before


# --- the list ---------------------------------------------------------------------------------


def test_the_list_carries_kind_trips_first_then_albums_by_latest_activity(lib):
    client, me = lib
    _att("act-p", me, _memo("act-m", me))
    late_trip = coll_api._create(client, title="late", spanStart=END, spanEnd=END)["uid"]
    early_trip = coll_api._create(client, title="early", spanStart=START, spanEnd=START)["uid"]
    tag_trip = coll_api._create(client, title="tag", tag=TAG)["uid"]
    quiet = _album(client, "quiet")["uid"]
    busy = _album(client, "busy")["uid"]
    _age(quiet)
    _age(busy)
    _sql("UPDATE collections SET updated_at = '2002-01-01T00:00:00Z' WHERE uid=:u", {"u": quiet})
    assert _add(client, busy, ["act-p"]) == {"act-p": "added"}  # activity moves `busy` up

    listed = client.get("/api/collections").json()
    assert [(c["uid"], c["kind"]) for c in listed] == [
        (late_trip, "trip"),
        (early_trip, "trip"),
        (tag_trip, "trip"),
        (busy, "album"),
        (quiet, "album"),
    ]
    albums = client.get("/api/collections?kind=album").json()
    assert [c["uid"] for c in albums] == [busy, quiet]
    trips = client.get("/api/collections?kind=trip").json()
    assert [c["uid"] for c in trips] == [late_trip, early_trip, tag_trip]
    assert client.get("/api/collections?kind=folder").status_code == 422


# --- membership -------------------------------------------------------------------------------


def test_album_members_are_its_rows_that_the_caller_sees_and_that_are_attached(two):
    a_client, a, b_client, b = two
    _att("own", a, _memo("own-m", a, tags=(TAG,)))
    _att("b-pub", b, _memo("b-pub-m", b))
    _att("b-priv", b, _memo("b-priv-m", b, visibility="PRIVATE"))
    _att("detached", a, _memo("detached-m", a))
    _att("unlinked", a, _memo("unlinked-m", a))
    _att("not-added", a, _memo("not-added-m", a, tags=(TAG,)), capture_instant=INSIDE)
    uid = _album(a_client)["uid"]
    for att in ("own", "b-pub", "b-priv", "detached", "unlinked"):
        _seed_member(uid, att)
    _sql("UPDATE attachments SET detached_at = now() WHERE uid='detached'")
    _sql("UPDATE attachments SET detached_at = now(), memo_id = NULL WHERE uid='unlinked'")
    # An exclusion row is a trip's mechanism; written past the route, it changes nothing here.
    _sql(
        "INSERT INTO collection_exclusions (collection_id, attachment_id) VALUES "
        "((SELECT id FROM collections WHERE uid=:c),(SELECT id FROM attachments WHERE uid='own'))",
        {"c": uid},
    )

    assert coll_api._members(a_client, uid) == ["b-pub", "own"]
    assert a_client.get(f"/api/collections/{uid}").json()["memberCount"] == 2
    assert coll_api._members(b_client, uid) == ["b-priv", "b-pub", "own"]
    assert b_client.get(f"/api/collections/{uid}").json()["memberCount"] == 3


def test_a_trip_with_the_same_pictures_is_untouched_by_member_rows(lib):
    client, me = lib
    _att("t-in", me, _memo("t-in-m", me, tags=(TAG,)))
    _att("t-out", me, _memo("t-out-m", me), capture_instant=OUTSIDE)
    trip = coll_api._create(client, tag=TAG)["uid"]
    _seed_member(trip, "t-out")  # a stray row on a trip is not membership
    assert coll_api._members(client, trip) == ["t-in"]


def test_album_member_read_pages_in_taken_order_on_raw_pages(lib):
    """Seven members, pages of three: 3 + 3 + 1, in the library's taken order, no repeat and no
    skip — asserted on the RAW pages, whose order `_members` would sort away."""
    client, me = lib
    names = [f"pg{i}" for i in range(7)]
    instants = [
        "2031-03-05T12:00:00Z",
        "2031-03-05T12:00:00Z",  # a tie, settled by id
        "2031-01-01T00:00:00Z",
        None,  # dated by its memo
        "2031-09-09T09:09:09Z",
        "2031-03-05T12:00:00.500Z",  # sub-second
        "2030-12-31T23:59:59Z",
    ]
    for name, instant in zip(names, instants, strict=True):
        _att(name, me, _memo(f"{name}-m", me, created_at=INSIDE), capture_instant=instant)
    _att("pg-outsider", me, _memo("pg-outsider-m", me), capture_instant=INSIDE)
    uid = _album(client)["uid"]
    assert set(_add(client, uid, names).values()) == {"added"}

    pages: list[list[str]] = []
    cursor = None
    for _ in range(4):
        query = "?limit=3" + (f"&cursor={cursor}" if cursor else "")
        r = client.get(f"/api/collections/{uid}/attachments{query}")
        assert r.status_code == 200, r.text
        pages.append([i["uid"] for i in r.json()])
        cursor = r.headers.get("X-Next-Cursor")
        if cursor is None:
            break
    assert [len(p) for p in pages] == [3, 3, 1]
    assert [u for p in pages for u in p] == _in_taken_desc(names)


# --- the batch add ----------------------------------------------------------------------------


def test_one_picture_of_five_joins_the_album_alone(lib):
    client, me = lib
    memo = _memo("five-m", me)
    for i in range(1, 6):
        _att(f"five-{i}", me, memo)
    uid = _album(client)["uid"]
    assert _add(client, uid, ["five-2"]) == {"five-2": "added"}
    assert coll_api._members(client, uid) == ["five-2"]
    assert _member_rows(uid) == ["five-2"]


def test_the_outcomes_cover_every_category(two):
    a_client, a, b_client, b = two
    _att("mine", a, _memo("mine-m", a))
    _att("again", a, _memo("again-m", a))
    _att("theirs-public", b, _memo("theirs-public-m", b))
    _att("theirs-private", b, _memo("theirs-private-m", b, visibility="PRIVATE"))
    _att("detached-linked", a, _memo("detached-linked-m", a))
    _att("detached-unlinked", a, _memo("detached-unlinked-m", a))
    _att("never-linked", a)  # an upload that no memo holds yet
    _att("their-orphan", b)  # another member's upload: not visible at all
    _sql("UPDATE attachments SET detached_at = now() WHERE uid='detached-linked'")
    _sql("UPDATE attachments SET detached_at = now(), memo_id = NULL WHERE uid='detached-unlinked'")
    uid = _album(a_client)["uid"]
    assert _add(a_client, uid, ["again"]) == {"again": "added"}

    asked = [
        "mine",
        "again",
        "theirs-public",
        "theirs-private",
        "no-such-uid",
        "detached-linked",
        "detached-unlinked",
        "never-linked",
        "their-orphan",
        "mine",  # named twice, answered once
    ]
    r = a_client.post(f"/api/collections/{uid}/attachments", json={"attachmentUids": asked})
    assert r.status_code == 200, r.text
    assert [(i["attachmentUid"], i["outcome"]) for i in r.json()["results"]] == [
        ("mine", "added"),
        ("again", "alreadyMember"),
        ("theirs-public", "added"),  # anyone may add any picture they can see
        ("theirs-private", "notVisible"),
        ("no-such-uid", "notVisible"),
        ("detached-linked", "orphan"),
        ("detached-unlinked", "orphan"),
        ("never-linked", "orphan"),
        ("their-orphan", "notVisible"),
    ]
    assert _member_rows(uid) == ["again", "mine", "theirs-public"]
    # Nothing about a memo changed: an album add is not a tag edit.
    assert _sql("SELECT count(*) FROM memo_tags")[0][0] == 0


@pytest.fixture
def refuse_member_insert():
    """A trigger that makes inserting the member row of `err-bad` raise, so that one row's
    transaction fails inside the database while its neighbours run on either side of it."""
    _sql(
        "CREATE OR REPLACE FUNCTION c802_refuse() RETURNS trigger AS $$ BEGIN "
        "IF NEW.attachment_id = (SELECT id FROM attachments WHERE uid = 'err-bad') THEN "
        "RAISE EXCEPTION 'c802 refused'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
    )
    _sql(
        "CREATE TRIGGER c802_refuse BEFORE INSERT ON collection_members "
        "FOR EACH ROW EXECUTE FUNCTION c802_refuse()"
    )
    try:
        yield
    finally:
        _sql("DROP TRIGGER IF EXISTS c802_refuse ON collection_members")
        _sql("DROP FUNCTION IF EXISTS c802_refuse()")


def test_one_failing_row_answers_error_and_its_neighbours_land(lib, refuse_member_insert):
    client, me = lib
    ids = [_att(name, me, _memo(f"{name}-m", me)) for name in ("err-first", "err-bad", "err-last")]
    assert ids == sorted(ids)  # the failing row really runs between the two good ones
    uid = _album(client)["uid"]
    assert _add(client, uid, ["err-first", "err-bad", "err-last"]) == {
        "err-first": "added",
        "err-bad": "error",
        "err-last": "added",
    }
    assert _member_rows(uid) == ["err-first", "err-last"]


def test_a_picture_deleted_while_its_add_waits_answers_error_alone(lib, monkeypatch):
    client, me = lib
    _att("gone", me, _memo("gone-m", me))
    _att("stays", me, _memo("stays-m", me))
    uid = _album(client)["uid"]
    real = coll_svc.add_member

    async def deleted_first(db, collection_id, attachment_id, **kwargs):
        _sql("DELETE FROM attachments WHERE uid='gone'")
        return await real(db, collection_id, attachment_id, **kwargs)

    monkeypatch.setattr(coll_svc, "add_member", deleted_first)
    assert _add(client, uid, ["gone", "stays"]) == {"gone": "error", "stays": "added"}
    assert _member_rows(uid) == ["stays"]


async def test_two_concurrent_adds_of_one_picture_leave_one_row(lib, pat_client, monkeypatch):
    bearer, me, _ = pat_client
    _att("race", me, _memo("race-m", me))
    created = await bearer.post("/api/collections", json={"title": "R", "kind": "album"})
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]
    real = coll_svc.add_member
    both_read = asyncio.Barrier(2)
    entered: list[int] = []

    async def held(db, collection_id, attachment_id, **kwargs):
        entered.append(1)
        async with asyncio.timeout(10):
            await both_read.wait()
        return await real(db, collection_id, attachment_id, **kwargs)

    monkeypatch.setattr(coll_svc, "add_member", held)
    body = {"attachmentUids": ["race"]}
    path = f"/api/collections/{uid}/attachments"
    results = await asyncio.gather(bearer.post(path, json=body), bearer.post(path, json=body))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert len(entered) == 2  # both really overlapped past the read
    outcomes = sorted(r.json()["results"][0]["outcome"] for r in results)
    assert outcomes == ["added", "alreadyMember"]
    assert _member_rows(uid) == ["race"]


def test_an_add_and_a_removal_move_the_albums_updated_at(lib):
    client, me = lib
    _att("upd", me, _memo("upd-m", me))
    uid = _album(client)["uid"]
    aged = _age(uid)
    assert _add(client, uid, ["upd"]) == {"upd": "added"}
    after_add = _updated_at(uid)
    assert after_add > aged
    aged = _age(uid)
    assert _add(client, uid, ["upd"]) == {"upd": "alreadyMember"}
    assert _updated_at(uid) == aged  # nothing written, nothing moved
    assert client.delete(f"/api/collections/{uid}/members/upd").status_code == 204
    assert _updated_at(uid) > aged
    aged = _age(uid)
    assert client.delete(f"/api/collections/{uid}/members/upd").status_code == 204
    assert _updated_at(uid) == aged  # an idempotent repeat removed nothing


def test_the_batch_add_keeps_its_request_limit(lib):
    client, _ = lib
    uid = _album(client)["uid"]
    many = [f"u{i}" for i in range(501)]
    r = client.post(f"/api/collections/{uid}/attachments", json={"attachmentUids": many})
    assert r.status_code == 422, r.text


# --- removal and the 409 pairs ----------------------------------------------------------------


def test_member_delete_is_idempotent_and_removes_one_row(lib):
    client, me = lib
    memo = _memo("rm-m", me)
    _att("rm-a", me, memo)
    _att("rm-b", me, memo)
    uid = _album(client)["uid"]
    assert set(_add(client, uid, ["rm-a", "rm-b"]).values()) == {"added"}
    for _ in range(2):
        r = client.delete(f"/api/collections/{uid}/members/rm-a")
        assert r.status_code == 204, r.text
        assert _member_rows(uid) == ["rm-b"]
    assert coll_api._members(client, uid) == ["rm-b"]
    r = client.delete(f"/api/collections/{uid}/members/no-such-uid")
    assert r.status_code == 204, r.text
    assert _member_rows(uid) == ["rm-b"]


def test_member_delete_of_an_invisible_picture_changes_nothing(two):
    a_client, a, b_client, b = two
    _att("b-hidden", b, _memo("b-hidden-m", b, visibility="PRIVATE"))
    uid = _album(b_client)["uid"]
    assert _add(b_client, uid, ["b-hidden"]) == {"b-hidden": "added"}
    r = a_client.delete(f"/api/collections/{uid}/members/b-hidden")
    assert r.status_code == 204, r.text
    assert _member_rows(uid) == ["b-hidden"]


def test_member_delete_on_an_unknown_collection_is_404(lib):
    client, _ = lib
    assert client.delete("/api/collections/nope/members/x").status_code == 404


def test_the_409_pairs(lib):
    """Exclusions are a trip's; member rows are an album's. Each route refuses the other kind
    and writes nothing."""
    client, me = lib
    _att("pair", me, _memo("pair-m", me, tags=(TAG,)))
    album = _album(client)["uid"]
    trip = coll_api._create(client, tag=TAG)["uid"]
    assert _add(client, album, ["pair"]) == {"pair": "added"}
    for method in ("put", "delete"):
        r = client.request(method.upper(), f"/api/collections/{album}/exclusions/pair")
        assert r.status_code == 409, (method, r.text)
        assert r.json()["detail"] == "albumHasNoExclusions"
    assert coll_api._exclusions() == 0
    _seed_member(trip, "pair")
    r = client.delete(f"/api/collections/{trip}/members/pair")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "tripHasNoMemberRows"
    assert _member_rows(trip) == ["pair"]
    # control: the trip's own exclusion route still answers
    assert client.put(f"/api/collections/{trip}/exclusions/pair").status_code == 204


# --- cascades ---------------------------------------------------------------------------------


def test_deleting_a_picture_takes_its_member_rows_with_it(lib):
    client, me = lib
    memo = _memo("cas-m", me)
    _att("cas-a", me, memo)
    _att("cas-b", me, memo)
    uid = _album(client)["uid"]
    assert set(_add(client, uid, ["cas-a", "cas-b"]).values()) == {"added"}
    assert client.delete("/api/attachments/cas-a").status_code == 204
    assert _member_rows() == ["cas-b"]
    assert coll_api._members(client, uid) == ["cas-b"]


def test_deleting_an_album_takes_its_member_rows_and_no_other(lib):
    client, me = lib
    _att("both", me, _memo("both-m", me))
    keep = _album(client, "keep")["uid"]
    drop = _album(client, "drop")["uid"]
    for uid in (keep, drop):
        assert _add(client, uid, ["both"]) == {"both": "added"}
    assert client.delete(f"/api/collections/{drop}").status_code == 204
    assert _member_rows() == ["both"]
    assert _member_rows(keep) == ["both"]


def test_a_picture_detached_after_it_was_added_leaves_the_listing(lib):
    client, me = lib
    _att("det", me, _memo("det-m", me))
    uid = _album(client)["uid"]
    assert _add(client, uid, ["det"]) == {"det": "added"}
    _sql("UPDATE attachments SET detached_at = now() WHERE uid='det'")
    assert coll_api._members(client, uid) == []
    assert client.get(f"/api/collections/{uid}").json()["memberCount"] == 0


# --- the migration ----------------------------------------------------------------------------


def _members_table_exists() -> bool:
    return bool(
        _sql(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='collection_members'"
        )
    )


def _kind_column_exists() -> bool:
    return bool(
        _sql(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='collections' AND column_name='kind'"
        )
    )


def _alembic() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_the_migration_round_trips_and_an_existing_trip_becomes_kind_trip(db_engine):
    cfg = _alembic()
    _sql("DELETE FROM collections")
    assert _members_table_exists() and _kind_column_exists()  # control: head carries both
    try:
        command.downgrade(cfg, BEFORE_ALBUMS_REVISION)
        assert not _members_table_exists() and not _kind_column_exists()
        _sql("INSERT INTO collections (uid, title, tag) VALUES ('mig-trip', 'Portugal', 'c802mig')")
    finally:
        command.upgrade(cfg, "head")
    try:
        assert _members_table_exists() and _kind_column_exists()
        rows = _sql("SELECT kind, tag FROM collections WHERE uid='mig-trip'")
        assert [tuple(r) for r in rows] == [("trip", "c802mig")]
        on_delete = sorted(
            r[0]
            for r in _sql(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid='collection_members'::regclass AND contype='f'"
            )
        )
        assert on_delete == ["c", "c", "n"]  # both parents CASCADE, `added_by` SET NULL
        with pytest.raises(Exception, match="ck_collections_source"):
            _sql("INSERT INTO collections (uid, title, kind) VALUES ('mig-bad', 'x', 'trip')")
        with pytest.raises(Exception, match="ck_collections_source"):
            _sql(
                "INSERT INTO collections (uid, title, kind, tag) "
                "VALUES ('mig-bad2', 'x', 'album', 'c802mig')"
            )
        with pytest.raises(Exception, match="ck_collections_kind"):
            _sql(
                "INSERT INTO collections (uid, title, kind, tag) "
                "VALUES ('mig-bad3', 'x', 'folder', 'c802mig')"
            )
    finally:
        _sql("DELETE FROM collections")


def test_the_downgrade_refuses_while_an_album_exists_and_changes_nothing(db_engine):
    cfg = _alembic()
    _sql("DELETE FROM collections")
    _sql("INSERT INTO collections (uid, title, kind) VALUES ('mig-album', 'Sea', 'album')")
    try:
        with pytest.raises(Exception, match="album"):
            command.downgrade(cfg, BEFORE_ALBUMS_REVISION)
        assert _members_table_exists() and _kind_column_exists()
        assert [r[0] for r in _sql("SELECT kind FROM collections WHERE uid='mig-album'")] == [
            "album"
        ]
    finally:
        _sql("DELETE FROM collections")
        command.upgrade(cfg, "head")


# --- pre-gate cases ---------------------------------------------------------------------------


def test_an_empty_album_lists_nothing_counts_nothing_and_carries_no_cursor(lib):
    client, me = lib
    _att("bystander", me, _memo("bystander-m", me, tags=(TAG,)), capture_instant=INSIDE)
    uid = _album(client)["uid"]
    r = client.get(f"/api/collections/{uid}/attachments?limit=1")
    assert r.status_code == 200, r.text
    assert r.json() == [] and "X-Next-Cursor" not in r.headers
    assert client.get(f"/api/collections/{uid}").json()["memberCount"] == 0


def test_a_title_that_says_kind_is_not_a_kind_change(lib):
    client, _ = lib
    uid = _album(client)["uid"]
    r = client.patch(f"/api/collections/{uid}", json={"title": "kind"})
    assert r.status_code == 200, r.text
    assert _stored(uid)["title"] == "kind"
    r = client.patch(f"/api/collections/{uid}", json={"title": '{"kind": "trip"}'})
    assert r.status_code == 200, r.text
    assert _stored(uid)["kind"] == "album"
