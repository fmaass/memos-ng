"""The `/api/collections` surface (MEMNG-746, plan PLAN_2026-10-10 Phase 2).

A collection is a household object: every signed-in member lists, opens and edits every one, and
the last write wins. What it SHOWS is the caller's view: `memberCount`, the member read and the
cover go through the library's own visibility, so another member's PRIVATE media never reaches a
caller by way of a collection — not as a member, not as a cover, not as a distinguishable 404.

Every test starts from an EMPTY library and an empty collections table (`_wipe`), and the expected
member lists are literals written by hand (their ORDER is asked of SQL, never of the endpoint under
test). Spans live in 2031 and tags carry a `c746` prefix, so nothing seeded elsewhere can match.
"""

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from app.database import async_session
from app.main import (
    SECURITY_SCHEMES,
    app,
    attach_database_busy_responses,
    attach_route_security,
    collapse_nullable_anyof,
)
from app.models import User
from app.routers import collections as coll_router
from app.services import collections as coll_svc
from fastapi import HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_assets import _walk
from tests.test_attachments_taken import _sql
from tests.test_uploads import _login_as

OTHER = "c746_other"
START = "2031-03-01T00:00:00Z"
END = "2031-03-10T23:59:59Z"
INSIDE = "2031-03-05T12:00:00Z"
OUTSIDE = "2031-06-01T12:00:00Z"
TAG = "c746t"

# The eight operations MEMNG-746 added, the inclusion MEMNG-785 adds and the album member removal
# MEMNG-802 adds, and nothing else under the prefix.
OPERATIONS = {
    ("get", "/api/collections"),
    ("post", "/api/collections"),
    ("get", "/api/collections/{uid}"),
    ("patch", "/api/collections/{uid}"),
    ("delete", "/api/collections/{uid}"),
    ("put", "/api/collections/{uid}/exclusions/{attachment_uid}"),
    ("delete", "/api/collections/{uid}/exclusions/{attachment_uid}"),
    ("get", "/api/collections/{uid}/attachments"),
    ("post", "/api/collections/{uid}/attachments"),
    ("delete", "/api/collections/{uid}/members/{attachment_uid}"),
}


def _wipe():
    for table in (
        "collection_exclusions",
        "collections",
        "frame_share_jobs",
        "media_jobs",
        "attachments",
        "memos",
        "tags",
    ):
        _sql(f"DELETE FROM {table}")


@pytest.fixture
def lib(logged_in_client):
    """The logged-in member A over an empty library and no collections."""
    _wipe()
    try:
        yield logged_in_client
    finally:
        _wipe()


@pytest.fixture
def two(lib, monkeypatch):
    """A (the logged-in client) and B — a second household member with a real session of its own,
    so "B edits A's collection" is two users on the wire, not one user with two cookies."""
    a_client, a = lib
    _sql("DELETE FROM users WHERE subject=:s", {"s": OTHER})
    b_client = _login_as(monkeypatch, OTHER)
    try:
        yield a_client, a, b_client, OTHER
    finally:
        _wipe()
        _sql("DELETE FROM users WHERE subject=:s", {"s": OTHER})


def _memo(uid, owner, *, visibility="PUBLIC", created_at=OUTSIDE, tags=()):
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


def _att(uid, owner, memo_id=None, *, mime="image/jpeg", capture_instant=None):
    return _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
        "storage_path, blurhash, capture_instant, created_at) VALUES "
        "(:u,(SELECT id FROM users WHERE subject=:o),:m,:f,:mime,1,"
        "CAST('ready' AS attachment_status),'/x','LKO2',CAST(:ci AS timestamptz),"
        "CAST(:cr AS timestamptz)) RETURNING id",
        {
            "u": uid,
            "o": owner,
            "m": memo_id,
            "f": f"{uid}.jpg",
            "mime": mime,
            "ci": capture_instant,
            "cr": OUTSIDE,
        },
    )[0][0]


def _create(client, **fields) -> dict[str, Any]:
    body = {"title": "Trip", **fields}
    r = client.post("/api/collections", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _row(uid: str) -> dict[str, Any] | None:
    rows = _sql(
        "SELECT c.title, c.tag, c.span_start, c.span_end, c.tz, a.uid AS cover "
        "FROM collections c LEFT JOIN attachments a ON a.id = c.cover_attachment_id "
        "WHERE c.uid = :u",
        {"u": uid},
    )
    if not rows:
        return None
    title, tag, start, end, tz, cover = rows[0]
    return {"title": title, "tag": tag, "start": start, "end": end, "tz": tz, "cover": cover}


def _exclusions(collection_uid: str | None = None) -> int:
    if collection_uid is None:
        return _sql("SELECT count(*) FROM collection_exclusions")[0][0]
    return _sql(
        "SELECT count(*) FROM collection_exclusions e JOIN collections c ON c.id = e.collection_id "
        "WHERE c.uid = :u",
        {"u": collection_uid},
    )[0][0]


def _members(client, uid: str, query: str = "") -> list[str]:
    r = client.get(f"/api/collections/{uid}/attachments{query}")
    assert r.status_code == 200, r.text
    return sorted(i["uid"] for i in r.json())


def _in_taken_desc(uids: list[str]) -> list[str]:
    """`uids` in the library's taken order, straight from SQL — the oracle, not the endpoint."""
    return [
        r[0]
        for r in _sql(
            "SELECT a.uid FROM attachments a LEFT JOIN memos m ON m.id = a.memo_id "
            "WHERE a.uid = ANY(:u) "
            "ORDER BY coalesce(a.capture_instant, m.created_at, a.created_at) DESC, a.id DESC",
            {"u": uids},
        )
    ]


# --- CRUD -----------------------------------------------------------------------------------


def test_crud_round_trip_answers_every_field(lib):
    client, me = lib
    memo = _memo("crud-m", me, tags=(TAG,))
    _att("crud-cover", me, memo, capture_instant=INSIDE)
    _att("crud-two", me, memo, capture_instant=OUTSIDE)

    made = _create(
        client,
        title="Lisbon",
        tag=TAG,
        spanStart="2031-03-01T01:00:00+01:00",
        spanEnd=END,
        tz="Europe/Lisbon",
        coverAttachmentUid="crud-cover",
    )
    assert set(made) == {
        "uid",
        "title",
        "kind",
        "tag",
        "spanStart",
        "spanEnd",
        "tz",
        "cover",
        "memberCount",
        "createdBy",
        "createdAt",
        "updatedAt",
    }
    assert made["title"] == "Lisbon"
    assert made["tag"] == TAG
    assert made["spanStart"] == START  # the offset instant, rendered in UTC
    assert made["spanEnd"] == END
    assert made["tz"] == "Europe/Lisbon"
    assert made["cover"]["uid"] == "crud-cover"
    assert made["cover"]["memoUid"] == "crud-m"
    assert made["memberCount"] == 2  # both by the tag, one by the span as well
    assert made["createdBy"] == me
    assert made["createdAt"].endswith("Z") and made["updatedAt"].endswith("Z")

    uid = made["uid"]
    got = client.get(f"/api/collections/{uid}")
    assert got.status_code == 200, got.text
    assert got.json() == made
    listed = client.get("/api/collections")
    assert listed.status_code == 200, listed.text
    assert [c["uid"] for c in listed.json()] == [uid]
    assert listed.json()[0] == made

    patched = client.patch(f"/api/collections/{uid}", json={"title": "Lisboa"})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "Lisboa"
    # exclude_unset: a field the PATCH did not name is untouched
    assert (body["tag"], body["spanStart"], body["tz"]) == (TAG, START, "Europe/Lisbon")
    assert body["cover"]["uid"] == "crud-cover"
    assert _row(uid) == {
        "title": "Lisboa",
        "tag": TAG,
        "start": datetime(2031, 3, 1, tzinfo=UTC),
        "end": datetime(2031, 3, 10, 23, 59, 59, tzinfo=UTC),
        "tz": "Europe/Lisbon",
        "cover": "crud-cover",
    }

    gone = client.delete(f"/api/collections/{uid}")
    assert gone.status_code == 204, gone.text
    assert client.get(f"/api/collections/{uid}").status_code == 404
    assert _row(uid) is None


def test_an_unknown_collection_is_404_on_every_route(lib):
    client, me = lib
    _att("unk-a", me, _memo("unk-m", me, tags=(TAG,)))
    for method, path in (
        ("get", "/api/collections/nope"),
        ("patch", "/api/collections/nope"),
        ("delete", "/api/collections/nope"),
        ("put", "/api/collections/nope/exclusions/unk-a"),
        ("delete", "/api/collections/nope/exclusions/unk-a"),
        ("get", "/api/collections/nope/attachments"),
    ):
        kwargs = {"json": {"title": "x"}} if method == "patch" else {}
        r = client.request(method.upper(), path, **kwargs)
        assert r.status_code == 404, (method, path, r.text)


def test_the_list_is_newest_span_first_and_span_less_last(lib):
    client, _ = lib
    tag_only = _create(client, title="tag only", tag=TAG)["uid"]
    march = _create(client, title="march", spanStart=START, spanEnd=END)["uid"]
    june = _create(
        client, title="june", spanStart="2031-06-01T00:00:00Z", spanEnd="2031-06-02T00:00:00Z"
    )["uid"]
    r = client.get("/api/collections")
    assert r.status_code == 200, r.text
    assert [c["uid"] for c in r.json()] == [june, march, tag_only]


def test_a_second_member_edits_anothers_collection_and_lists_it(two):
    a_client, a, b_client, _ = two
    uid = _create(a_client, title="A's trip", tag=TAG)["uid"]
    listed = b_client.get("/api/collections")
    assert listed.status_code == 200, listed.text
    assert [c["uid"] for c in listed.json()] == [uid]
    assert listed.json()[0]["createdBy"] == a
    r = b_client.patch(f"/api/collections/{uid}", json={"title": "B renamed it"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "B renamed it"
    assert r.json()["createdBy"] == a  # an edit grants nothing and moves no authorship
    assert _row(uid)["title"] == "B renamed it"  # type: ignore[index]
    assert a_client.get(f"/api/collections/{uid}").json()["title"] == "B renamed it"


def test_anonymous_is_401_on_every_route(lib):
    client, me = lib
    _att("anon-a", me, _memo("anon-m", me, tags=(TAG,)))
    uid = _create(client, tag=TAG)["uid"]  # a REAL uid, so a 404 cannot pass for the 401
    anon = TestClient(app, raise_server_exceptions=False)
    anon.headers["Origin"] = client.headers["Origin"]
    calls = [
        ("GET", "/api/collections", None),
        ("POST", "/api/collections", {"title": "x", "tag": TAG}),
        ("GET", f"/api/collections/{uid}", None),
        ("PATCH", f"/api/collections/{uid}", {"title": "x"}),
        ("DELETE", f"/api/collections/{uid}", None),
        ("PUT", f"/api/collections/{uid}/exclusions/anon-a", None),
        ("DELETE", f"/api/collections/{uid}/exclusions/anon-a", None),
        ("GET", f"/api/collections/{uid}/attachments", None),
        ("POST", f"/api/collections/{uid}/attachments", {"attachmentUids": ["anon-a"]}),
        ("DELETE", f"/api/collections/{uid}/members/anon-a", None),
    ]
    assert {(m.lower(), p.replace(uid, "{uid}").replace("anon-a", "{attachment_uid}"))
            for m, p, _ in calls} == OPERATIONS  # fmt: skip
    for method, path, body in calls:
        r = anon.request(method, path, json=body)
        assert r.status_code == 401, (method, path, r.status_code, r.text)
    assert _row(uid) is not None and _exclusions() == 0  # nothing an anonymous call did stuck
    assert _sql("SELECT count(*) FROM collections")[0][0] == 1


# --- create ---------------------------------------------------------------------------------


def test_an_idempotent_replay_of_create_is_201_with_the_same_uid_and_one_row(lib):
    client, me = lib
    first = client.post(
        "/api/collections", json={"title": "Once", "tag": TAG, "idempotencyKey": "c746-key-1"}
    )
    assert first.status_code == 201, first.text
    replay = client.post(
        "/api/collections",
        json={"title": "Twice?", "tag": "c746other", "idempotencyKey": "c746-key-1"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["uid"] == first.json()["uid"]
    assert replay.json()["title"] == "Once"  # the stored record, not the replay's fields
    assert _sql("SELECT count(*) FROM collections")[0][0] == 1
    other_key = client.post(
        "/api/collections", json={"title": "Other", "tag": TAG, "idempotencyKey": "c746-key-2"}
    )
    assert other_key.status_code == 201
    assert other_key.json()["uid"] != first.json()["uid"]  # control: a new key is a new record


def test_a_replay_is_answered_before_its_cover_is_resolved(lib):
    """The retry whose response was lost may name a cover reclaimed since: it still gets the
    record the first call stored, never the cover's 404."""
    client, me = lib
    _att("replay-cover", me, _memo("replay-m", me, tags=(TAG,)))
    body = {
        "title": "R",
        "tag": TAG,
        "coverAttachmentUid": "replay-cover",
        "idempotencyKey": "c746-replay-1",
    }
    first = client.post("/api/collections", json=body)
    assert first.status_code == 201, first.text
    _sql("DELETE FROM attachments WHERE uid='replay-cover'")
    replay = client.post("/api/collections", json=body)
    assert replay.status_code == 201, replay.text
    assert replay.json()["uid"] == first.json()["uid"]
    assert replay.json()["cover"] is None  # the cover went with its attachment (SET NULL)
    fresh = client.post("/api/collections", json={**body, "idempotencyKey": "c746-replay-2"})
    assert fresh.status_code == 404, fresh.text  # control: without a replay the cover decides


def test_a_keyed_create_whose_cover_went_with_a_concurrent_winner_answers_the_winner(
    lib, monkeypatch
):
    """The key lookup misses; a peer then commits the same (creator, key) and its cover is
    reclaimed; this request's cover lookup now 404s. The answer is still the peer's record — the
    replay — with 201, and the key holds exactly one row."""
    client, me = lib
    _att("win-cover", me, _memo("win-m", me, tags=(TAG,)))
    calls: list[str] = []

    async def peer_won_then_cover_went(db, user, uid):
        calls.append(uid)
        async with async_session() as peer:
            owner = (await peer.execute(select(User).where(User.subject == me))).scalar_one()
            await coll_svc.create_collection(
                peer, owner, title="Peer", tag=TAG, idempotency_key="c746-winner"
            )
            await peer.commit()
        raise HTTPException(status_code=404, detail="Attachment not found")

    monkeypatch.setattr(coll_router, "_viewable", peer_won_then_cover_went)
    body = {"title": "Me", "tag": TAG, "coverAttachmentUid": "win-cover"}
    r = client.post("/api/collections", json={**body, "idempotencyKey": "c746-winner"})
    assert calls == ["win-cover"]  # the race really ran: the lookup missed, then the cover 404d
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "Peer"
    rows = _sql("SELECT uid FROM collections WHERE idempotency_key='c746-winner'")
    assert [u for (u,) in rows] == [r.json()["uid"]]
    calls.clear()
    keyless = client.post("/api/collections", json=body)
    assert keyless.status_code == 404, keyless.text  # control: without a key the 404 stands


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"title": "", "tag": TAG}, "empty title"),
        ({"title": "x"}, "no tag and no span"),
        ({"title": "x", "spanStart": START}, "half a span"),
        ({"title": "x", "spanStart": END, "spanEnd": START}, "reversed span"),
        ({"title": "x", "spanStart": "2031-03-01T00:00:00", "spanEnd": END}, "naive bound"),
        ({"title": "x", "tag": TAG, "tz": "Mars/Olympus"}, "unknown zone"),
        ({"title": "x", "tag": "#c746t"}, "a hash is not part of a tag name"),
        ({"title": "x", "tag": "c746 t"}, "a tag name has no space"),
        ({"title": "x", "tag": "c746%"}, "a tag name has no pattern character"),
        ({"title": "x", "tag": TAG, "idempotencyKey": "short"}, "key below 8 chars"),
    ],
)
def test_create_refuses_an_invalid_record_with_422_and_stores_nothing(lib, body, why):
    client, _ = lib
    r = client.post("/api/collections", json=body)
    assert r.status_code == 422, (why, r.status_code, r.text)
    assert _sql("SELECT count(*) FROM collections")[0][0] == 0, why


# --- PATCH ----------------------------------------------------------------------------------


def test_patch_null_clears_the_cover_and_the_tag(lib):
    client, me = lib
    _att("clr-cover", me, _memo("clr-m", me, tags=(TAG,)), capture_instant=INSIDE)
    uid = _create(client, tag=TAG, spanStart=START, spanEnd=END, coverAttachmentUid="clr-cover")[
        "uid"
    ]
    assert _row(uid)["cover"] == "clr-cover"  # type: ignore[index]  # the precondition
    r = client.patch(f"/api/collections/{uid}", json={"coverAttachmentUid": None, "tag": None})
    assert r.status_code == 200, r.text
    assert r.json()["cover"] is None
    assert r.json()["tag"] is None
    assert r.json()["spanStart"] == START  # unnamed, so kept
    row = _row(uid)
    assert row is not None and row["cover"] is None and row["tag"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"spanStart": START},  # a tag-only record given one bound
        {"tag": None},  # the only source cleared
        {"spanEnd": "2031-02-01T00:00:00Z"},  # reversed against the stored start
        {"title": None},  # title is required and NOT nullable
        {"title": ""},
        {"tz": "Nowhere/Land"},
        {"spanStart": "2031-03-01T00:00:00"},  # naive
    ],
)
def test_patch_to_an_invalid_record_is_422_and_the_record_is_unchanged(lib, change):
    client, _ = lib
    uid = _create(client, title="Keep", tag=TAG)["uid"]
    if "spanEnd" in change:
        ok = client.patch(f"/api/collections/{uid}", json={"spanStart": START, "spanEnd": END})
        assert ok.status_code == 200, ok.text  # control: a whole span is accepted
    before = _row(uid)
    r = client.patch(f"/api/collections/{uid}", json=change)
    assert r.status_code == 422, (change, r.text)
    assert _row(uid) == before


def test_patch_of_a_collection_deleted_meanwhile_is_404(lib, monkeypatch):
    """`CollectionGone` raised past the route's lookup — mapped by class to 404."""
    client, _ = lib
    uid = _create(client, tag=TAG)["uid"]
    real = coll_svc.update_collection

    async def deleted_first(db, collection, changes):
        _sql("DELETE FROM collections WHERE uid=:u", {"u": uid})
        return await real(db, collection, changes)

    monkeypatch.setattr(coll_svc, "update_collection", deleted_first)
    r = client.patch(f"/api/collections/{uid}", json={"title": "late"})
    assert r.status_code == 404, r.text


def test_patch_whose_cover_was_deleted_meanwhile_is_404(lib, monkeypatch):
    """`CoverGone` raised past the route's `_viewable` — mapped by class to 404, record kept."""
    client, me = lib
    _att("late-cover", me, _memo("late-m", me, tags=(TAG,)))
    uid = _create(client, title="Kept", tag=TAG)["uid"]
    real = coll_svc.update_collection

    async def cover_deleted_first(db, collection, changes):
        _sql("DELETE FROM attachments WHERE uid='late-cover'")
        return await real(db, collection, changes)

    monkeypatch.setattr(coll_svc, "update_collection", cover_deleted_first)
    r = client.patch(f"/api/collections/{uid}", json={"coverAttachmentUid": "late-cover"})
    assert r.status_code == 404, r.text
    assert _row(uid) == {
        "title": "Kept",
        "tag": TAG,
        "start": None,
        "end": None,
        "tz": None,
        "cover": None,
    }


def test_a_key_error_from_the_service_stays_a_500_never_a_404(lib, monkeypatch):
    """KeyError is a LookupError: a blanket `except LookupError` would answer a programming error
    as an unknown collection. The mapping is by class, so it stays a 500."""
    client, _ = lib
    uid = _create(client, tag=TAG)["uid"]

    async def broken(db, collection, changes):
        raise KeyError("not editable: ['uid']")

    monkeypatch.setattr(coll_svc, "update_collection", broken)
    r = client.patch(f"/api/collections/{uid}", json={"title": "x"})
    assert r.status_code == 500, r.text


# --- exclusions -----------------------------------------------------------------------------


def test_exclusion_put_twice_then_delete_twice_is_all_204_and_one_row_at_most(lib):
    client, me = lib
    memo = _memo("ex-m", me, tags=(TAG,))
    _att("ex-a", me, memo)
    _att("ex-b", me, memo)
    uid = _create(client, tag=TAG)["uid"]
    assert _members(client, uid) == ["ex-a", "ex-b"]
    for _ in range(2):
        r = client.put(f"/api/collections/{uid}/exclusions/ex-a")
        assert r.status_code == 204, r.text
        assert _exclusions(uid) == 1
    assert _members(client, uid) == ["ex-b"]
    assert client.get(f"/api/collections/{uid}").json()["memberCount"] == 1
    for _ in range(2):
        r = client.delete(f"/api/collections/{uid}/exclusions/ex-a")
        assert r.status_code == 204, r.text
        assert _exclusions(uid) == 0
    assert _members(client, uid) == ["ex-a", "ex-b"]


async def test_two_concurrent_exclusion_puts_leave_one_row(lib, pat_client, monkeypatch):
    """Both requests are held after their lookups until both have looked up, so both insert: the
    pair's primary key — not a read before the write — is what makes them one row, and neither
    request sees the other's insert as an error."""
    bearer, me, _ = pat_client
    _att("race-a", me, _memo("race-m", me, tags=(TAG,)))
    created = await bearer.post("/api/collections", json={"title": "R", "tag": TAG})
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]
    real = coll_svc.exclude
    both_looked_up = asyncio.Barrier(2)
    entered: list[int] = []

    async def held(db, collection, attachment):
        entered.append(1)
        async with asyncio.timeout(10):
            await both_looked_up.wait()
        return await real(db, collection, attachment)

    monkeypatch.setattr(coll_svc, "exclude", held)
    path = f"/api/collections/{uid}/exclusions/race-a"
    results = await asyncio.gather(bearer.put(path), bearer.put(path))
    assert [r.status_code for r in results] == [204, 204], [r.text for r in results]
    assert len(entered) == 2  # both really overlapped past the lookup
    assert _exclusions(uid) == 1


def test_an_exclusion_put_whose_attachment_was_deleted_meanwhile_is_404(lib, monkeypatch):
    client, me = lib
    _att("fk-a", me, _memo("fk-m", me, tags=(TAG,)))
    uid = _create(client, tag=TAG)["uid"]
    real = coll_svc.exclude

    async def parent_deleted_first(db, collection, attachment):
        _sql("DELETE FROM attachments WHERE uid='fk-a'")
        return await real(db, collection, attachment)

    seen: list[Any] = []
    rolled: list[Any] = []
    real_rollback = AsyncSession.rollback

    async def spy_rollback(self):
        rolled.append(self)
        return await real_rollback(self)

    async def capture(db, collection, attachment):
        seen.append(db)
        return await parent_deleted_first(db, collection, attachment)

    monkeypatch.setattr(AsyncSession, "rollback", spy_rollback)
    monkeypatch.setattr(coll_svc, "exclude", capture)
    r = client.put(f"/api/collections/{uid}/exclusions/fk-a")
    assert r.status_code == 404, r.text
    assert len(seen) == 1 and any(s is seen[0] for s in rolled)  # the request's OWN session
    assert _exclusions() == 0
    monkeypatch.setattr(coll_svc, "exclude", real)
    assert client.get(f"/api/collections/{uid}").status_code == 200  # the session recovered


def test_an_exclusion_put_whose_collection_was_deleted_meanwhile_is_404(lib, monkeypatch):
    client, me = lib
    _att("fkc-a", me, _memo("fkc-m", me, tags=(TAG,)))
    uid = _create(client, tag=TAG)["uid"]
    real = coll_svc.exclude

    async def collection_deleted_first(db, collection, attachment):
        _sql("DELETE FROM collections WHERE uid=:u", {"u": uid})
        return await real(db, collection, attachment)

    monkeypatch.setattr(coll_svc, "exclude", collection_deleted_first)
    r = client.put(f"/api/collections/{uid}/exclusions/fkc-a")
    assert r.status_code == 404, r.text
    assert _exclusions() == 0


def test_an_integrity_error_that_is_not_a_vanished_parent_is_not_answered_as_404(lib, monkeypatch):
    """Only a foreign-key violation means "a parent went"; any other integrity failure is not
    the caller's unknown resource and must not be dressed up as one."""
    client, me = lib
    _att("ie-a", me, _memo("ie-m", me, tags=(TAG,)))
    uid = _create(client, tag=TAG)["uid"]

    class _Unique(Exception):
        sqlstate = "23505"

    async def unique_violation(db, collection, attachment):
        raise IntegrityError("INSERT …", {}, _Unique())

    monkeypatch.setattr(coll_svc, "exclude", unique_violation)
    r = client.put(f"/api/collections/{uid}/exclusions/ie-a")
    assert r.status_code == 500, r.text


def test_deleting_a_collection_leaves_no_exclusion_rows(lib):
    client, me = lib
    memo = _memo("dc-m", me, tags=(TAG,))
    _att("dc-a", me, memo)
    _att("dc-b", me, memo)
    keep = _create(client, title="keep", tag=TAG)["uid"]
    drop = _create(client, title="drop", tag=TAG)["uid"]
    for uid in (keep, drop):
        for att in ("dc-a", "dc-b"):
            assert client.put(f"/api/collections/{uid}/exclusions/{att}").status_code == 204
    assert _exclusions() == 4
    assert client.delete(f"/api/collections/{drop}").status_code == 204
    assert _exclusions(drop) == 0
    assert _exclusions() == 2  # control: the other collection's rows stay


def test_deleting_an_excluded_attachment_leaves_no_dangling_exclusion_row(lib):
    client, me = lib
    memo = _memo("da-m", me, tags=(TAG,))
    _att("da-a", me, memo)
    _att("da-b", me, memo)
    uid = _create(client, tag=TAG)["uid"]
    for att in ("da-a", "da-b"):
        assert client.put(f"/api/collections/{uid}/exclusions/{att}").status_code == 204
    r = client.delete("/api/attachments/da-a")
    assert r.status_code == 204, r.text
    assert _sql("SELECT count(*) FROM attachments WHERE uid='da-a'")[0][0] == 0
    dangling = _sql(
        "SELECT count(*) FROM collection_exclusions e "
        "LEFT JOIN attachments a ON a.id = e.attachment_id WHERE a.id IS NULL"
    )[0][0]
    assert dangling == 0
    assert _exclusions(uid) == 1  # control: the other exclusion is still there


# --- viewer scope ---------------------------------------------------------------------------


def _private_both_arms(a, b):
    """B's media: PRIVATE through the tag arm and through the span arm, and a PUBLIC twin of
    each as the control that the fixture really is membership."""
    _att("b-priv-tag", b, _memo("b-priv-tag-m", b, visibility="PRIVATE", tags=(TAG,)))
    _att(
        "b-priv-span",
        b,
        _memo("b-priv-span-m", b, visibility="PRIVATE"),
        capture_instant=INSIDE,
    )
    _att("b-pub-tag", b, _memo("b-pub-tag-m", b, tags=(TAG,)))
    _att("b-pub-span", b, _memo("b-pub-span-m", b), capture_instant=INSIDE)
    _att("a-own", a, _memo("a-own-m", a, visibility="PRIVATE", tags=(TAG,)))
    # visible to both and a member of neither arm: the read is the collection, not the library
    _att("b-pub-out", b, _memo("b-pub-out-m", b), capture_instant=OUTSIDE)


def test_member_read_hides_another_members_private_pictures_through_both_arms(two):
    a_client, a, b_client, b = two
    _private_both_arms(a, b)
    uid = _create(a_client, tag=TAG, spanStart=START, spanEnd=END)["uid"]
    assert _members(a_client, uid) == ["a-own", "b-pub-span", "b-pub-tag"]
    # control: the hidden rows ARE members, for the member allowed to see them
    assert _members(b_client, uid) == ["b-priv-span", "b-priv-tag", "b-pub-span", "b-pub-tag"]


def test_member_count_is_the_callers_view(two):
    a_client, a, b_client, b = two
    _private_both_arms(a, b)
    uid = _create(a_client, tag=TAG, spanStart=START, spanEnd=END)["uid"]
    assert a_client.get(f"/api/collections/{uid}").json()["memberCount"] == 3
    assert b_client.get(f"/api/collections/{uid}").json()["memberCount"] == 4
    assert [c["memberCount"] for c in a_client.get("/api/collections").json()] == [3]
    assert [c["memberCount"] for c in b_client.get("/api/collections").json()] == [4]


def test_type_narrows_the_member_read_to_videos_or_images(lib):
    client, me = lib
    memo = _memo("kind-m", me, tags=(TAG,))
    _att("kind-img-1", me, memo)
    _att("kind-img-2", me, memo, mime="image/heic")
    _att("kind-vid-1", me, memo, mime="video/mp4")
    _att("kind-vid-2", me, memo, mime="video/quicktime")
    uid = _create(client, tag=TAG)["uid"]
    assert _members(client, uid, "?type=video") == ["kind-vid-1", "kind-vid-2"]
    assert _members(client, uid, "?type=image") == ["kind-img-1", "kind-img-2"]
    assert _members(client, uid) == ["kind-img-1", "kind-img-2", "kind-vid-1", "kind-vid-2"]


@pytest.mark.parametrize("kind", ["images", "videos", "all", "audio"])
def test_member_read_refuses_a_type_other_than_image_or_video(lib, kind):
    client, _ = lib
    uid = _create(client, tag=TAG)["uid"]
    r = client.get(f"/api/collections/{uid}/attachments?type={kind}")
    assert r.status_code == 422, r.text


def test_the_cover_of_a_private_memo_shows_for_its_owner_and_is_null_for_another(two):
    a_client, a, b_client, _ = two
    _att("a-priv-cover", a, _memo("a-priv-m", a, visibility="PRIVATE", tags=(TAG,)))
    uid = _create(a_client, tag=TAG, coverAttachmentUid="a-priv-cover")["uid"]
    mine = a_client.get(f"/api/collections/{uid}").json()
    assert mine["cover"]["uid"] == "a-priv-cover"
    assert mine["cover"]["thumbnailUrl"].startswith("/api/attachments/a-priv-cover/")
    assert a_client.get("/api/collections").json()[0]["cover"]["uid"] == "a-priv-cover"
    theirs = b_client.get(f"/api/collections/{uid}")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["cover"] is None
    assert b_client.get("/api/collections").json()[0]["cover"] is None
    assert "a-priv-cover" not in theirs.text  # no signed URL, no uid, anywhere in the body


def test_a_cover_reference_to_another_members_private_attachment_is_404(two):
    a_client, a, _, b = two
    _att("b-secret", b, _memo("b-secret-m", b, visibility="PRIVATE"))
    _att("b-shared", b, _memo("b-shared-m", b))
    r = a_client.post(
        "/api/collections", json={"title": "x", "tag": TAG, "coverAttachmentUid": "b-secret"}
    )
    assert r.status_code == 404, r.text
    assert _sql("SELECT count(*) FROM collections")[0][0] == 0
    unknown = a_client.post(
        "/api/collections", json={"title": "x", "tag": TAG, "coverAttachmentUid": "no-such"}
    )
    assert unknown.status_code == 404
    assert unknown.json() == r.json()  # invisible and unknown are one answer
    uid = _create(a_client, tag=TAG, coverAttachmentUid="b-shared")["uid"]  # control: visible
    patched = a_client.patch(f"/api/collections/{uid}", json={"coverAttachmentUid": "b-secret"})
    assert patched.status_code == 404, patched.text
    assert _row(uid)["cover"] == "b-shared"  # type: ignore[index]


def test_an_exclusion_reference_to_another_members_private_attachment_is_404(two):
    a_client, a, _, b = two
    _att("bx-secret", b, _memo("bx-secret-m", b, visibility="PRIVATE", tags=(TAG,)))
    _att("bx-shared", b, _memo("bx-shared-m", b, tags=(TAG,)))
    uid = _create(a_client, tag=TAG)["uid"]
    for method in ("PUT", "DELETE"):
        r = a_client.request(method, f"/api/collections/{uid}/exclusions/bx-secret")
        assert r.status_code == 404, (method, r.text)
        unknown = a_client.request(method, f"/api/collections/{uid}/exclusions/no-such")
        assert unknown.status_code == 404
        assert unknown.json() == r.json()
    assert _exclusions() == 0
    ok = a_client.put(f"/api/collections/{uid}/exclusions/bx-shared")
    assert ok.status_code == 204, ok.text  # control: a visible attachment is excluded
    assert _exclusions(uid) == 1


# --- paging ---------------------------------------------------------------------------------


def _seed_page_fixture(me, b):
    """Eight members whose taken instants tie three ways at one sub-second instant, with
    neighbours one microsecond either side, plus four non-members of four different kinds."""
    memo = _memo("pg-m", me)
    for n in range(3):
        _att(f"pg-tie-{n}", me, memo, capture_instant="2031-03-05T12:00:00.500000Z")
    _att("pg-tie-up", me, memo, capture_instant="2031-03-05T12:00:00.500001Z")
    _att("pg-tie-down", me, memo, capture_instant="2031-03-05T12:00:00.499999Z")
    _att("pg-newest", me, memo, capture_instant="2031-03-10T23:59:59Z")  # the inclusive end
    _att("pg-oldest", me, memo, capture_instant=START)  # the inclusive start
    _att("pg-mid", me, memo, capture_instant="2031-03-07T08:00:00Z")
    _att("pg-out", me, memo, capture_instant=OUTSIDE)
    _att("pg-excluded", me, memo, capture_instant=INSIDE)
    _att("pg-orphan", me, None, capture_instant=INSIDE)
    _att("pg-b-priv", b, _memo("pg-b-m", b, visibility="PRIVATE"), capture_instant=INSIDE)


PAGE_MEMBERS = [
    "pg-mid",
    "pg-newest",
    "pg-oldest",
    "pg-tie-0",
    "pg-tie-1",
    "pg-tie-2",
    "pg-tie-down",
    "pg-tie-up",
]


def test_member_read_pages_across_tied_and_sub_second_instants_without_skip_or_repeat(two):
    a_client, a, _, b = two
    _seed_page_fixture(a, b)
    uid = _create(a_client, spanStart=START, spanEnd=END)["uid"]
    assert a_client.put(f"/api/collections/{uid}/exclusions/pg-excluded").status_code == 204
    expected = _in_taken_desc(PAGE_MEMBERS)
    assert sorted(expected) == PAGE_MEMBERS  # positive control: the oracle saw every member
    assert expected[:2] == ["pg-newest", "pg-mid"]
    assert expected[2:5] == ["pg-tie-up", "pg-tie-2", "pg-tie-1"]  # the tie straddles page 2|3
    for limit in (2, 3):
        paged, pages = _walk(a_client, f"/api/collections/{uid}/attachments", limit)
        assert paged == expected, limit
        assert pages == -(-len(expected) // limit)


def test_member_read_items_have_the_library_shape_and_taken_desc_by_default(lib):
    client, me = lib
    memo = _memo("shape-m", me, tags=(TAG,))
    _att("shape-old", me, memo, capture_instant=START)
    _att("shape-new", me, memo, capture_instant=END)
    uid = _create(client, tag=TAG)["uid"]
    mine = client.get(f"/api/collections/{uid}/attachments")
    assert mine.status_code == 200, mine.text
    library = client.get(f"/api/attachments?order=taken_desc&tag={TAG}").json()

    # Same projection for the same viewer. The signatures carry an expiry minted per request, so
    # the URLs are compared without their query; everything else must be equal as it stands.
    def unsigned(items):
        return [
            {k: v.split("?")[0] if k.endswith("Url") else v for k, v in i.items()} for i in items
        ]

    assert unsigned(mine.json()) == unsigned(library)
    assert len(library) == 2  # control: the library itself sees both
    assert [i["uid"] for i in mine.json()] == ["shape-new", "shape-old"]
    asc = client.get(f"/api/collections/{uid}/attachments?order=taken_asc").json()
    assert [i["uid"] for i in asc] == ["shape-old", "shape-new"]


def test_a_malformed_cursor_is_422(lib):
    client, me = lib
    _att("cur-a", me, _memo("cur-m", me, tags=(TAG,)))
    uid = _create(client, tag=TAG)["uid"]
    for cursor in ("M!Q==", "not-a-cursor", "MTIz"):  # junk, junk, an id cursor under taken order
        r = client.get(f"/api/collections/{uid}/attachments?cursor={cursor}")
        assert r.status_code == 422, (cursor, r.text)


def test_an_empty_collection_and_an_exact_page_carry_no_next_cursor(lib):
    client, me = lib
    memo = _memo("edge-m", me, tags=(TAG,))
    empty = _create(client, tag="c746nobody")
    assert empty["memberCount"] == 0
    r = client.get(f"/api/collections/{empty['uid']}/attachments")
    assert r.status_code == 200 and r.json() == []
    assert "x-next-cursor" not in r.headers
    for n in range(3):
        _att(f"edge-{n}", me, memo)
    uid = _create(client, tag=TAG)["uid"]
    exact = client.get(f"/api/collections/{uid}/attachments?limit=3")
    assert len(exact.json()) == 3 and "x-next-cursor" not in exact.headers
    one_short = client.get(f"/api/collections/{uid}/attachments?limit=2")
    assert len(one_short.json()) == 2 and "x-next-cursor" in one_short.headers


def test_a_tag_is_matched_as_a_name_never_as_a_pattern(lib):
    client, me = lib
    _att("pat-hit", me, _memo("pat-hit-m", me, tags=("c746_x",)))
    _att("pat-miss", me, _memo("pat-miss-m", me, tags=("c746ax",)))  # `_` as a LIKE wildcard
    uid = _create(client, tag="c746_x")["uid"]
    assert _members(client, uid) == ["pat-hit"]


# --- the published contract -----------------------------------------------------------------


def _collection_route(route: Any) -> bool:
    return isinstance(route, APIRoute) and route.endpoint.__module__ == "app.routers.collections"


def _document(routes: list[Any]) -> dict[str, Any]:
    """The served document built the way `app.main.custom_openapi` builds it, over `routes`."""
    schema = get_openapi(title=app.title, version=app.version, routes=routes)
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
    collapse_nullable_anyof(schema)
    attach_route_security(schema, routes)
    attach_database_busy_responses(schema)
    return schema


def test_openapi_names_exactly_the_ten_collection_operations():
    paths = app.openapi()["paths"]
    found = {
        (method, path)
        for path, item in paths.items()
        if path.startswith("/api/collections")
        for method in item
    }
    assert found == OPERATIONS
    assert all(
        _collection_route(r)
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/collections")
    )
    member_read = paths["/api/collections/{uid}/attachments"]["get"]
    assert "X-Next-Cursor" in member_read["responses"]["200"]["headers"]
    params = {p["name"]: p for p in member_read["parameters"]}
    assert set(params) == {"uid", "type", "limit", "cursor", "order", "mine", "visibility"}
    # A query that fails validation answers the array shape; an undecodable or wrong-kind cursor
    # the string one (the library's global cursor handler) — the contract must name both.
    refused = member_read["responses"]["422"]["content"]["application/json"]["schema"]
    assert {"$ref": "#/components/schemas/HTTPValidationError"} in refused["anyOf"]
    assert {
        "type": "object",
        "properties": {"detail": {"type": "string"}},
        "required": ["detail"],
    } in refused["anyOf"]
    assert len(refused["anyOf"]) == 2
    for path, item in paths.items():
        if not path.startswith("/api/collections"):
            continue
        for method, op in item.items():
            assert "401" in op["responses"], (method, path)
            if (method, path) != ("get", "/api/collections"):  # the one operation with no input
                assert "422" in op["responses"], (method, path)
            if path != "/api/collections":
                assert "404" in op["responses"], (method, path)
            assert op["security"], (method, path)


def test_the_collection_routes_change_no_other_path_or_schema():
    """Built with and without this module's routes, the document differs ONLY by the collection
    paths and the six schemas they introduce — no shared model renamed or reshaped, no other
    path touched. Selected by the endpoint's module, not by path, so a route this module added
    under another prefix would show up here as a changed path."""
    routes = list(app.routes)
    full = _document(routes)
    without = _document([r for r in routes if not _collection_route(r)])
    assert any(_collection_route(r) for r in routes)  # control: the module's routes are served
    added_paths = set(full["paths"]) - set(without["paths"])
    assert added_paths == {p for _, p in OPERATIONS}
    assert set(without["paths"]) <= set(full["paths"])
    for path in without["paths"]:
        assert full["paths"][path] == without["paths"][path], path
    full_schemas = full["components"]["schemas"]
    without_schemas = without["components"]["schemas"]
    assert set(full_schemas) - set(without_schemas) == {
        "CollectionOut",
        "CollectionCreate",
        "CollectionPatch",
        "CollectionInclusion",
        "CollectionInclusionOut",
        "InclusionResult",
    }
    for name, schema in without_schemas.items():
        assert full_schemas[name] == schema, name
    rest = copy.deepcopy(full)
    for key in ("paths", "components"):
        rest.pop(key)
        without.pop(key)
    assert rest == without
