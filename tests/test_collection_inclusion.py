"""`POST /api/collections/{uid}/attachments` — putting pictures INTO a trip (MEMNG-785).

Inclusion is a memo edit: a picture belongs to a trip by its memo's tag or by the span, so making
one a member appends the trip's tag to its memo — under the memo's write lock, to the content the
lock re-read, with `memo_tags` rebuilt in the same transaction — and clears that picture's
exclusion. Only the caller's own memos are written; everything else is answered per uid.

Every memo here is seeded CONSISTENTLY — the tags in its content are exactly its `memo_tags` rows —
so "the rebuild keeps the earlier tags" is a claim about real content, not about a fixture whose
content says `m`. Concurrency cases use committed rows and two bearer requests on separate
sessions, held at a seam so both have read before either takes the lock.
"""

import asyncio
from typing import Any

import pytest
from app.models import Tag
from app.routers import collections as coll_router
from app.services import memos as memos_svc
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from tests import test_collections_api as coll_api
from tests.test_attachments_taken import _sql
from tests.test_collections_api import END, INSIDE, OUTSIDE, START, _att, _create, _members

# The collections module's fixtures, re-exported so pytest finds them here.
lib = coll_api.lib
two = coll_api.two

TRIP = "c785trip"
OTHER_TRIP = "c785other"
NEVER_USED = "c785fresh"


def _memo(uid: str, owner: str, content: str, *, visibility="PUBLIC", created_at=OUTSIDE) -> int:
    """A memo whose `memo_tags` are exactly the tags its content names."""
    mid = _sql(
        "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, updated_at) "
        "VALUES (:u,(SELECT id FROM users WHERE subject=:o),:content,CAST(:v AS visibility),"
        "false,CAST(:c AS timestamptz),now()) RETURNING id",
        {"u": uid, "o": owner, "content": content, "v": visibility, "c": created_at},
    )[0][0]
    for name in memos_svc.parse_tags(content):
        tid = _sql(
            "INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=:n "
            "RETURNING id",
            {"n": name},
        )[0][0]
        _sql("INSERT INTO memo_tags (memo_id, tag_id) VALUES (:m,:t)", {"m": mid, "t": tid})
    return mid


def _content(memo_uid: str) -> str:
    return _sql("SELECT content FROM memos WHERE uid=:u", {"u": memo_uid})[0][0]


def _tag_rows(memo_uid: str) -> list[str]:
    return [
        r[0]
        for r in _sql(
            "SELECT t.name FROM memo_tags mt JOIN tags t ON t.id = mt.tag_id "
            "JOIN memos m ON m.id = mt.memo_id WHERE m.uid=:u ORDER BY t.name",
            {"u": memo_uid},
        )
    ]


def _include(client, uid: str, uids: list[str]) -> dict[str, str]:
    r = client.post(f"/api/collections/{uid}/attachments", json={"attachmentUids": uids})
    assert r.status_code == 200, r.text
    return {o["attachmentUid"]: o["outcome"] for o in r.json()["results"]}


def _trip(client, tag: str = TRIP) -> str:
    return _create(client, tag=tag, spanStart=START, spanEnd=END)["uid"]


# --- the edit --------------------------------------------------------------------------------


def test_an_untagged_memo_outside_the_span_becomes_a_member(lib):
    client, me = lib
    _att("in-p", me, _memo("in-m", me, "Lunch by the lake #food #friends"), capture_instant=OUTSIDE)
    uid = _trip(client)
    assert _members(client, uid) == []  # control: neither arm admits it yet

    assert _include(client, uid, ["in-p"]) == {"in-p": "added"}

    content = _content("in-m")
    assert content.startswith("Lunch by the lake #food #friends")
    assert memos_svc.parse_tags(content) == ["food", "friends", TRIP]
    assert _tag_rows("in-m") == sorted(["food", "friends", TRIP])
    memo = client.get("/api/memos/in-m").json()
    assert sorted(memo["tags"]) == sorted(["food", "friends", TRIP])
    assert _members(client, uid) == ["in-p"]


def test_a_previously_excluded_tagged_picture_is_a_member_again(lib):
    client, me = lib
    _att("ex-p", me, _memo("ex-m", me, f"Pass #{TRIP} #hike"), capture_instant=OUTSIDE)
    uid = _trip(client)
    assert client.put(f"/api/collections/{uid}/exclusions/ex-p").status_code == 204
    assert _members(client, uid) == []  # control: the exclusion holds it out

    assert _include(client, uid, ["ex-p"]) == {"ex-p": "added"}

    assert _content("ex-m") == f"Pass #{TRIP} #hike"  # already tagged: no second tag
    assert coll_api._exclusions(uid) == 0
    assert _members(client, uid) == ["ex-p"]


def test_tagging_admits_the_memos_other_pictures_but_keeps_their_exclusions(lib):
    client, me = lib
    mid = _memo("sib-m", me, "Dinner #food")
    for p in ("sib-a", "sib-b", "sib-c"):
        _att(p, me, mid, capture_instant=OUTSIDE)
    uid = _trip(client)
    assert client.put(f"/api/collections/{uid}/exclusions/sib-c").status_code == 204

    assert _include(client, uid, ["sib-a"]) == {"sib-a": "added"}

    assert _members(client, uid) == ["sib-a", "sib-b"]  # sib-c's exclusion is still in force
    assert coll_api._exclusions(uid) == 1


def test_several_pictures_of_one_memo_are_one_append(lib):
    client, me = lib
    mid = _memo("one-m", me, "Beach")
    _att("one-a", me, mid, capture_instant=OUTSIDE)
    _att("one-b", me, mid, capture_instant=OUTSIDE)
    uid = _trip(client)

    r = client.post(
        f"/api/collections/{uid}/attachments", json={"attachmentUids": ["one-a", "one-b", "one-a"]}
    )
    assert r.status_code == 200, r.text
    # The raw array, not a dict of it: a duplicate uid is ONE row, at its first mention.
    assert r.json()["results"] == [
        {"attachmentUid": "one-a", "outcome": "added"},
        {"attachmentUid": "one-b", "outcome": "added"},
    ]
    assert _content("one-m") == f"Beach\n#{TRIP}"
    assert _content("one-m").count(f"#{TRIP}") == 1


def test_an_empty_memo_gets_the_tag_alone_and_a_trailing_newline_is_kept(lib):
    client, me = lib
    _att("em-p", me, _memo("em-m", me, ""), capture_instant=OUTSIDE)
    _att("nl-p", me, _memo("nl-m", me, "Note\n"), capture_instant=OUTSIDE)
    uid = _trip(client)
    assert _include(client, uid, ["em-p", "nl-p"]) == {"em-p": "added", "nl-p": "added"}
    assert _content("em-m") == f"#{TRIP}"
    assert _content("nl-m") == f"Note\n#{TRIP}"


def test_a_tag_inside_another_word_is_not_membership(lib):
    """Membership is `parse_tags`, never a substring: `#c785tripx` names a different tag."""
    client, me = lib
    _att("sub-p", me, _memo("sub-m", me, f"#{TRIP}x"), capture_instant=OUTSIDE)
    uid = _trip(client)
    assert _include(client, uid, ["sub-p"]) == {"sub-p": "added"}
    assert memos_svc.parse_tags(_content("sub-m")) == [f"{TRIP}x", TRIP]


# --- the per-uid answers -------------------------------------------------------------------


def test_the_outcomes_name_already_member_not_owner_and_orphan(two):
    a_client, me, b_client, other = two
    _att("o-tag", me, _memo("o-tm", me, f"#{TRIP}"), capture_instant=OUTSIDE)
    _att("o-span", me, _memo("o-sm", me, "inside"), capture_instant=INSIDE)
    _att("o-b-pub", other, _memo("o-bm", other, "theirs"), capture_instant=OUTSIDE)
    _att("o-b-priv", other, _memo("o-bp", other, "hidden", visibility="PRIVATE"))
    _att("o-orph", me, None)
    _att("o-b-orph", other, None)
    uid = _trip(a_client)

    assert _include(
        a_client,
        uid,
        ["o-tag", "o-span", "o-b-pub", "o-b-priv", "o-orph", "o-b-orph", "o-nothing"],
    ) == {
        "o-tag": "alreadyMember",
        "o-span": "alreadyMember",
        "o-b-pub": "notOwner",
        # Another member's private picture, another member's orphan and a uid that names nothing
        # get the one answer, so the outcome cannot tell a hidden row from a missing one.
        "o-b-priv": "notOwner",
        "o-b-orph": "notOwner",
        "o-nothing": "notOwner",
        "o-orph": "orphan",
    }
    assert _content("o-tm") == f"#{TRIP}"
    assert _content("o-sm") == "inside"  # a span member is not written to
    assert _content("o-bm") == "theirs"
    assert _content("o-bp") == "hidden"


def test_a_picture_on_my_memo_uploaded_by_another_member_is_mine_to_include(two):
    """Ownership is the MEMO being edited, not the item's `mine`."""
    a_client, me, b_client, other = two
    _att("x-p", other, _memo("x-m", me, "mine"), capture_instant=OUTSIDE)
    uid = _trip(a_client)
    assert _include(a_client, uid, ["x-p"]) == {"x-p": "added"}
    assert _content("x-m") == f"mine\n#{TRIP}"


def test_a_picture_i_uploaded_onto_another_members_memo_is_not_mine_to_include(two):
    """The inverse: the caller owns the ATTACHMENT, another member owns the memo — not written."""
    a_client, me, b_client, other = two
    _att("inv-p", me, _memo("inv-m", other, "theirs #food"), capture_instant=OUTSIDE)
    uid = _trip(a_client)
    assert _include(a_client, uid, ["inv-p"]) == {"inv-p": "notOwner"}
    assert _content("inv-m") == "theirs #food"
    assert _tag_rows("inv-m") == ["food"]
    assert _members(a_client, uid) == []


def test_a_tagless_trip_is_409_and_writes_nothing(lib):
    client, me = lib
    _att("nt-p", me, _memo("nt-m", me, "x"), capture_instant=OUTSIDE)
    uid = _create(client, spanStart=START, spanEnd=END)["uid"]
    r = client.post(f"/api/collections/{uid}/attachments", json={"attachmentUids": ["nt-p"]})
    assert r.status_code == 409, r.text
    assert _content("nt-m") == "x"


def test_an_unknown_collection_is_404(lib):
    r = lib[0].post("/api/collections/nope/attachments", json={"attachmentUids": ["a"]})
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    "body",
    [
        {"attachmentUids": []},
        {},
        {"attachmentUids": "a"},
        {"attachmentUids": [f"u{n}" for n in range(501)]},
    ],
)
def test_a_malformed_body_is_422(lib, body):
    client, _ = lib
    uid = _trip(client)
    assert client.post(f"/api/collections/{uid}/attachments", json=body).status_code == 422


def _excluded(collection_uid: str, attachment_uid: str) -> bool:
    return bool(
        _sql(
            "SELECT 1 FROM collection_exclusions e JOIN collections c ON c.id = e.collection_id "
            "JOIN attachments a ON a.id = e.attachment_id WHERE c.uid=:c AND a.uid=:a",
            {"c": collection_uid, "a": attachment_uid},
        )
    )


@pytest.fixture
def refuse_exclusion_delete():
    """Installs, through a committed session, a trigger that makes deleting the exclusion of the
    attachment named `err-bad-p` raise — so that memo's transaction fails AFTER its content and
    `memo_tags` were written, at the exclusion delete, with real work to undo."""
    _sql(
        "CREATE OR REPLACE FUNCTION c785_refuse() RETURNS trigger AS $$ BEGIN "
        "IF OLD.attachment_id = (SELECT id FROM attachments WHERE uid = 'err-bad-p') THEN "
        "RAISE EXCEPTION 'c785 refused'; END IF; RETURN OLD; END $$ LANGUAGE plpgsql"
    )
    _sql(
        "CREATE TRIGGER c785_refuse BEFORE DELETE ON collection_exclusions "
        "FOR EACH ROW EXECUTE FUNCTION c785_refuse()"
    )
    try:
        yield
    finally:
        _sql("DROP TRIGGER IF EXISTS c785_refuse ON collection_exclusions")
        _sql("DROP FUNCTION IF EXISTS c785_refuse()")


def test_a_memo_failing_mid_write_is_rolled_back_whole_and_its_neighbours_land(
    lib, refuse_exclusion_delete
):
    """Three memos in one request, the failing one between two good ones (memos are taken in id
    order). Its content and `memo_tags` are written before the exclusion delete refuses, so only
    the route's rollback can take them back — and only a rollback lets the memo AFTER it run on a
    usable session. Everything is read back from another session."""
    client, me = lib
    order = []
    for name in ("err-first", "err-bad", "err-last"):
        order.append(_memo(name, me, f"{name} #food"))
        _att(f"{name}-p", me, order[-1], capture_instant=OUTSIDE)
    assert order == sorted(order)  # the failing memo really runs second
    uid = _trip(client)
    for name in ("err-first", "err-bad", "err-last"):
        assert client.put(f"/api/collections/{uid}/exclusions/{name}-p").status_code == 204

    assert _include(client, uid, ["err-first-p", "err-bad-p", "err-last-p"]) == {
        "err-first-p": "added",
        "err-bad-p": "error",
        "err-last-p": "added",
    }

    assert _content("err-bad") == "err-bad #food"
    assert _tag_rows("err-bad") == ["food"]
    assert _excluded(uid, "err-bad-p")
    for name in ("err-first", "err-last"):
        assert _content(name) == f"{name} #food\n#{TRIP}"
        assert _tag_rows(name) == sorted(["food", TRIP])
        assert not _excluded(uid, f"{name}-p")
    assert _members(client, uid) == ["err-first-p", "err-last-p"]


# --- concurrency -----------------------------------------------------------------------------


def _hold_before_lock(monkeypatch, parties: int, then=None) -> list[int]:
    """Hold every inclusion at the seam between its unlocked read and the memo lock until
    `parties` of them have arrived — so each has read before any locks."""
    barrier = asyncio.Barrier(parties)
    arrived: list[int] = []

    async def held():
        arrived.append(1)
        if then is not None:
            await then()
        async with asyncio.timeout(10):
            await barrier.wait()

    monkeypatch.setattr(coll_router, "_before_memo_lock", held)
    return arrived


async def test_two_inclusions_of_one_memo_into_two_trips_keep_both_tags(
    lib, pat_client, monkeypatch
):
    bearer, me, _ = pat_client
    _att("cc-p", me, _memo("cc-m", me, "Summit #hike"), capture_instant=OUTSIDE)
    uids = []
    for tag in (TRIP, OTHER_TRIP):
        created = await bearer.post("/api/collections", json={"title": tag, "tag": tag})
        assert created.status_code == 201, created.text
        uids.append(created.json()["uid"])
    arrived = _hold_before_lock(monkeypatch, 2)
    body = {"attachmentUids": ["cc-p"]}
    results = await asyncio.gather(
        *(bearer.post(f"/api/collections/{u}/attachments", json=body) for u in uids)
    )
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert [r.json()["results"][0]["outcome"] for r in results] == ["added", "added"]
    assert len(arrived) == 2  # both really read before either locked

    tags = memos_svc.parse_tags(_content("cc-m"))
    assert sorted(tags) == sorted(["hike", TRIP, OTHER_TRIP])
    assert all(_content("cc-m").count(f"#{t}") == 1 for t in (TRIP, OTHER_TRIP))
    assert _tag_rows("cc-m") == sorted(["hike", TRIP, OTHER_TRIP])
    listed = (await bearer.get("/api/memos/cc-m")).json()["tags"]
    assert sorted(listed) == sorted(["hike", TRIP, OTHER_TRIP])


async def test_an_edit_committed_after_the_read_and_before_the_lock_is_not_lost(
    lib, pat_client, monkeypatch
):
    bearer, me, _ = pat_client
    _att("ed-p", me, _memo("ed-m", me, "Draft #food"), capture_instant=OUTSIDE)
    created = await bearer.post("/api/collections", json={"title": "T", "tag": TRIP})
    uid = created.json()["uid"]
    edited: list[Any] = []

    async def ordinary_edit():
        r = await bearer.patch("/api/memos/ed-m", json={"content": "Final text #food #wine"})
        edited.append(r.status_code)

    _hold_before_lock(monkeypatch, 1, then=ordinary_edit)
    r = await bearer.post(f"/api/collections/{uid}/attachments", json={"attachmentUids": ["ed-p"]})
    assert r.status_code == 200, r.text
    assert edited == [200]  # control: the edit really committed inside the window
    assert _content("ed-m") == f"Final text #food #wine\n#{TRIP}"
    assert _tag_rows("ed-m") == sorted(["food", "wine", TRIP])


async def test_two_memos_racing_to_create_one_never_used_tag_both_land(
    lib, pat_client, monkeypatch
):
    """The trip's tag has no `tags` row (creating a collection stores only the string), and two
    inclusions on DIFFERENT memos hold different memo locks. Both are held after their tag lookup
    missed and before either inserts, so both try to create the row: the unique name must settle
    it, never fail one of them."""
    bearer, me, _ = pat_client
    _att("tr-a", me, _memo("tr-ma", me, "one"), capture_instant=OUTSIDE)
    _att("tr-b", me, _memo("tr-mb", me, "two"), capture_instant=OUTSIDE)
    created = await bearer.post("/api/collections", json={"title": "F", "tag": NEVER_USED})
    uid = created.json()["uid"]
    assert _sql("SELECT count(*) FROM tags WHERE name=:n", {"n": NEVER_USED})[0][0] == 0

    looked_up = asyncio.Barrier(2)
    gated: set[int] = set()
    real_execute = AsyncSession.execute

    async def execute(self, statement, *args, **kwargs):
        result = await real_execute(self, statement, *args, **kwargs)
        is_lookup = (
            isinstance(statement, Select)
            and [d.get("entity") for d in statement.column_descriptions] == [Tag]
            and NEVER_USED in str(statement.compile(compile_kwargs={"literal_binds": True}))
            and "memo_tags" not in str(statement)
        )
        if is_lookup and id(self) not in gated:
            gated.add(id(self))
            async with asyncio.timeout(10):
                await looked_up.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute)
    path = f"/api/collections/{uid}/attachments"
    results = await asyncio.gather(
        bearer.post(path, json={"attachmentUids": ["tr-a"]}),
        bearer.post(path, json={"attachmentUids": ["tr-b"]}),
    )
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert [r.json()["results"][0]["outcome"] for r in results] == ["added", "added"]
    assert len(gated) == 2  # both lookups really missed before either insert
    assert _sql("SELECT count(*) FROM tags WHERE name=:n", {"n": NEVER_USED})[0][0] == 1
    assert _tag_rows("tr-ma") == [NEVER_USED] and _tag_rows("tr-mb") == [NEVER_USED]
    assert sorted(i["uid"] for i in (await bearer.get(f"{path}")).json()) == ["tr-a", "tr-b"]
