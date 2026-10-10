"""Which attachments belong to a collection (MEMNG-746, plan PLAN_2026-10-10 Phase 1).

A member has a memo, is not detached, matches the collection's tag OR lies inside its span by the
library's own `takenAt` (both bounds inclusive), and is not excluded. What the CALLER may see is
`library_filters`' business, composed beside the clause exactly as `GET /api/attachments` composes
it — so every query below outer-joins `memos` and appends the clause to the filter list.

Every fixture is built so that dropping one part of the clause changes the answer: a tag-only, a
span-only, a both and a neither row in one collection; a viewer-owned orphan inside the span; a
synthetic row that still has its memo but carries a detach stamp; an excluded row beside an
identical included one. Tag names and spans (2031) are private to this module, so the expected
sets are exact literals, not derived from the code under test.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    Collection,
    CollectionExclusion,
    Memo,
    Tag,
    User,
    Visibility,
)
from app.services import collections as coll_svc
from app.services.attachments import collection_member_clause, library_filters
from app.services.memos import detach_attachments
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

START = datetime(2031, 3, 1, 0, 0, tzinfo=UTC)
END = datetime(2031, 3, 10, 23, 59, 59, tzinfo=UTC)
INSIDE = datetime(2031, 3, 5, 12, 0, tzinfo=UTC)
OUTSIDE = datetime(2031, 6, 1, 12, 0, tzinfo=UTC)
TICK = timedelta(microseconds=1)


async def _user(adb, subject: str) -> User:
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def _tag(adb, name: str) -> Tag:
    found = (await adb.execute(select(Tag).where(Tag.name == name))).scalar_one_or_none()
    if found is not None:
        return found
    t = Tag(name=name)
    adb.add(t)
    await adb.flush()
    return t


async def _memo(
    adb,
    owner: User,
    uid: str,
    *,
    tags: tuple[str, ...] = (),
    created_at: datetime = OUTSIDE,
    visibility: Visibility = Visibility.PUBLIC,
) -> Memo:
    m = Memo(
        uid=uid,
        owner_id=owner.id,
        content="m",
        visibility=visibility,
        created_at=created_at,
        tags=[await _tag(adb, n) for n in tags],
    )
    adb.add(m)
    await adb.flush()
    return m


async def _att(
    adb,
    owner: User,
    memo: Memo | None,
    uid: str,
    *,
    capture_instant: datetime | None = None,
    captured_at: datetime | None = None,
    created_at: datetime = OUTSIDE,
    detached_at: datetime | None = None,
    mime: str = "image/jpeg",
) -> Attachment:
    a = Attachment(
        uid=uid,
        owner_id=owner.id,
        memo_id=None if memo is None else memo.id,
        filename=f"{uid}.jpg",
        mime=mime,
        size=1,
        status=AttachmentStatus.ready,
        storage_path=f"/x/{uid}",
        capture_instant=capture_instant,
        captured_at=captured_at,
        created_at=created_at,
        detached_at=detached_at,
    )
    adb.add(a)
    await adb.flush()
    return a


async def _coll(adb, owner: User, **fields) -> Collection:
    c, created = await coll_svc.create_collection(adb, owner, title="Trip", **fields)
    assert created
    return c


async def _members(adb, collection: Collection, viewer: User) -> list[str]:
    """The member uids as `viewer` sees them, composed the way the library route composes."""
    stmt = (
        select(Attachment.uid)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        .where(*library_filters(viewer, None, None), collection_member_clause(collection))
    )
    uids = list((await adb.execute(stmt)).scalars().all())
    assert len(uids) == len(set(uids)), f"duplicate members: {uids}"
    return sorted(uids)


# --- the two arms ------------------------------------------------------------------------------


async def _arms_fixture(adb, tag: str):
    a = await _user(adb, f"arms-{tag}")
    tagged = await _memo(adb, a, f"m-{tag}-t", tags=(tag,))
    plain = await _memo(adb, a, f"m-{tag}-p")
    await _att(adb, a, tagged, f"{tag}-tag-only", capture_instant=OUTSIDE)
    await _att(adb, a, plain, f"{tag}-span-only", capture_instant=INSIDE)
    await _att(adb, a, tagged, f"{tag}-both", capture_instant=INSIDE)
    await _att(adb, a, plain, f"{tag}-neither", capture_instant=OUTSIDE)
    return a


async def test_tag_or_span_both_arms_and_neither_in_one_fixture(adb):
    a = await _arms_fixture(adb, "c746arms")
    c = await _coll(adb, a, tag="c746arms", span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["c746arms-both", "c746arms-span-only", "c746arms-tag-only"]


async def test_tag_only_collection_matches_only_the_tag(adb):
    a = await _arms_fixture(adb, "c746tagonly")
    c = await _coll(adb, a, tag="c746tagonly")
    assert await _members(adb, c, a) == ["c746tagonly-both", "c746tagonly-tag-only"]


async def test_span_only_collection_matches_only_the_span(adb):
    a = await _arms_fixture(adb, "c746spanonly")
    c = await _coll(adb, a, span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["c746spanonly-both", "c746spanonly-span-only"]


# --- exclusions --------------------------------------------------------------------------------


async def test_an_exclusion_removes_exactly_its_row_and_include_restores_it(adb):
    a = await _user(adb, "excl")
    m = await _memo(adb, a, "m-excl", tags=("c746excl",))
    gone = await _att(adb, a, m, "excl-gone", capture_instant=INSIDE)
    await _att(adb, a, m, "excl-kept", capture_instant=INSIDE)
    c = await _coll(adb, a, tag="c746excl")
    await coll_svc.exclude(adb, c, gone)
    await coll_svc.exclude(adb, c, gone)  # idempotent: the composite key holds one row
    assert await _members(adb, c, a) == ["excl-kept"]
    rows = await adb.execute(
        select(func.count())
        .select_from(CollectionExclusion)
        .where(CollectionExclusion.collection_id == c.id)
    )
    assert rows.scalar_one() == 1
    await coll_svc.include(adb, c, gone)
    await coll_svc.include(adb, c, gone)  # idempotent
    assert await _members(adb, c, a) == ["excl-gone", "excl-kept"]


async def test_an_exclusion_belongs_to_its_own_collection_only(adb):
    a = await _user(adb, "excl-scope")
    m = await _memo(adb, a, "m-excl-scope", tags=("c746scope",))
    x = await _att(adb, a, m, "scope-x", capture_instant=INSIDE)
    c1 = await _coll(adb, a, tag="c746scope")
    c2 = await _coll(adb, a, tag="c746scope")
    await coll_svc.exclude(adb, c1, x)
    assert await _members(adb, c1, a) == []
    assert await _members(adb, c2, a) == ["scope-x"]


async def test_a_tag_is_matched_exactly_never_as_a_pattern(adb):
    a = await _user(adb, "literal")
    m = await _memo(adb, a, "m-literal", tags=("c746litXYZ",))
    exact = await _memo(adb, a, "m-literal-exact", tags=("c746lit%",))
    await _att(adb, a, m, "lit-lookalike", capture_instant=INSIDE)
    await _att(adb, a, exact, "lit-exact", capture_instant=INSIDE)
    c = await _coll(adb, a, tag="c746lit%")
    assert await _members(adb, c, a) == ["lit-exact"]


# --- memo present, not detached ----------------------------------------------------------------


async def test_an_ordinarily_detached_attachment_inside_the_span_is_absent(adb):
    a = await _user(adb, "det-a")
    m = await _memo(adb, a, "m-det-a", tags=("c746deta",))
    gone = await _att(adb, a, m, "deta-gone", capture_instant=INSIDE)
    await _att(adb, a, m, "deta-kept", capture_instant=INSIDE)
    detach_attachments([gone])
    await adb.flush()
    c = await _coll(adb, a, tag="c746deta", span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["deta-kept"]


async def test_a_stamped_row_that_still_has_its_memo_is_absent(adb):
    """The SYNTHETIC case: memo_id set AND detached_at stamped. Normal detachment clears memo_id
    too, so only this row tells the `detached_at IS NULL` part apart from the memo part."""
    a = await _user(adb, "det-b")
    m = await _memo(adb, a, "m-det-b", tags=("c746detb",))
    await _att(adb, a, m, "detb-stamped", capture_instant=INSIDE, detached_at=INSIDE)
    await _att(adb, a, m, "detb-control", capture_instant=INSIDE)
    c = await _coll(adb, a, tag="c746detb", span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["detb-control"]


async def test_an_orphan_inside_the_span_is_absent_while_its_attached_twin_is_present(adb):
    """The orphan is the viewer's OWN, so `library_visible` admits it; only the memo part of the
    clause keeps it out."""
    a = await _user(adb, "orphan")
    m = await _memo(adb, a, "m-orphan")
    await _att(adb, a, None, "orphan-lonely", capture_instant=INSIDE)
    await _att(adb, a, m, "orphan-attached", capture_instant=INSIDE)
    c = await _coll(adb, a, span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["orphan-attached"]


# --- the membership date is takenAt ------------------------------------------------------------


async def test_no_capture_instant_dates_by_the_memo_not_captured_at_or_own_created_at(adb):
    a = await _user(adb, "nocap")
    in_memo = await _memo(adb, a, "m-nocap-in", created_at=INSIDE)
    out_memo = await _memo(adb, a, "m-nocap-out", created_at=OUTSIDE)
    await _att(adb, a, in_memo, "nocap-member", captured_at=OUTSIDE, created_at=OUTSIDE)
    await _att(adb, a, out_memo, "nocap-mirror", captured_at=INSIDE, created_at=INSIDE)
    c = await _coll(adb, a, span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["nocap-member"]


async def test_both_bounds_are_inclusive_at_the_exact_instant(adb):
    a = await _user(adb, "bounds")
    m = await _memo(adb, a, "m-bounds")
    await _att(adb, a, m, "b-at-start", capture_instant=START)
    await _att(adb, a, m, "b-at-end", capture_instant=END)
    await _att(adb, a, m, "b-before", capture_instant=START - TICK)
    await _att(adb, a, m, "b-after", capture_instant=END + TICK)
    c = await _coll(adb, a, span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["b-at-end", "b-at-start"]


# --- visibility stays the library's ------------------------------------------------------------


async def test_a_private_memo_is_hidden_from_another_member_through_both_arms(adb):
    a = await _user(adb, "vis-a")
    b = await _user(adb, "vis-b")
    priv_tagged = await _memo(adb, a, "m-vis-pt", tags=("c746vis",), visibility=Visibility.PRIVATE)
    priv_plain = await _memo(adb, a, "m-vis-pp", visibility=Visibility.PRIVATE)
    pub = await _memo(adb, a, "m-vis-pub", tags=("c746vis",), visibility=Visibility.PUBLIC)
    await _att(adb, a, priv_tagged, "vis-priv-tag", capture_instant=OUTSIDE)
    await _att(adb, a, priv_plain, "vis-priv-span", capture_instant=INSIDE)
    await _att(adb, a, pub, "vis-public", capture_instant=OUTSIDE)
    c = await _coll(adb, a, tag="c746vis", span_start=START, span_end=END)
    assert await _members(adb, c, a) == ["vis-priv-span", "vis-priv-tag", "vis-public"]
    assert await _members(adb, c, b) == ["vis-public"]
    assert await coll_svc.member_count(adb, c, a) == 3
    assert await coll_svc.member_count(adb, c, b) == 1


# --- the three CHECKs, in the database ---------------------------------------------------------


_BAD_ROWS = [
    ("no-source", None, None, None, "ck_collections_source"),
    ("start-only", None, START, None, "ck_collections_span_pair"),
    ("start-only-tagged", "t", START, None, "ck_collections_span_pair"),
    # Also source-less, and Postgres names the first CHECK it evaluates: the source one.
    ("end-only", None, None, END, "ck_collections_source"),
    ("end-only-tagged", "t", None, END, "ck_collections_span_pair"),
    ("reversed", None, END, START, "ck_collections_span_order"),
]


async def _raw_insert(adb, uid, tag, start, end):
    await adb.execute(
        text(
            "INSERT INTO collections (uid, title, tag, span_start, span_end) "
            "VALUES (:u, 'raw', :t, :s, :e)"
        ),
        {"u": uid, "t": tag, "s": start, "e": end},
    )


@pytest.mark.parametrize(("uid", "tag", "start", "end", "constraint"), _BAD_ROWS)
async def test_the_database_refuses_an_invalid_record(adb, uid, tag, start, end, constraint):
    with pytest.raises(IntegrityError) as caught:
        async with adb.begin_nested():
            await _raw_insert(adb, f"raw-{uid}", tag, start, end)
    assert constraint in str(caught.value)


@pytest.mark.parametrize(
    ("uid", "tag", "start", "end"),
    [
        ("ok-tag", "t", None, None),
        ("ok-span", None, START, END),
        ("ok-both", "t", START, END),
        ("ok-instant", None, START, START),
    ],
)
async def test_the_database_accepts_a_valid_record(adb, uid, tag, start, end):
    await _raw_insert(adb, f"raw-{uid}", tag, start, end)
    got = await adb.execute(
        text("SELECT count(*) FROM collections WHERE uid = :u"), {"u": f"raw-{uid}"}
    )
    assert got.scalar_one() == 1


# --- the service's own validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"span_start": START},
        {"tag": "t", "span_end": END},
        {"span_start": END, "span_end": START},
        {"tag": ""},
        {"tag": "   "},
        {"span_start": START.replace(tzinfo=None), "span_end": END},
        {"tag": "t", "tz": "Mars/Olympus_Mons"},
    ],
)
async def test_create_refuses_an_invalid_record_before_the_database_sees_it(adb, fields):
    a = await _user(adb, "val")
    with pytest.raises(coll_svc.CollectionInvalid):
        await coll_svc.create_collection(adb, a, title="Trip", **fields)
    stored = await adb.execute(
        select(func.count()).select_from(Collection).where(Collection.created_by == a.id)
    )
    assert stored.scalar_one() == 0


async def test_update_null_clears_cover_and_tag_and_refuses_half_a_span(adb):
    a = await _user(adb, "upd")
    m = await _memo(adb, a, "m-upd")
    cover = await _att(adb, a, m, "upd-cover", capture_instant=INSIDE)
    c = await _coll(
        adb,
        a,
        tag="c746upd",
        span_start=START,
        span_end=END,
        tz="Europe/Zurich",
        cover_attachment_id=cover.id,
    )
    await coll_svc.update_collection(adb, c, {"title": "Renamed"})
    assert (c.title, c.tag, c.cover_attachment_id, c.tz) == (
        "Renamed",
        "c746upd",
        cover.id,
        "Europe/Zurich",
    )

    await coll_svc.update_collection(adb, c, {"cover_attachment_id": None, "tag": None, "tz": None})
    await adb.refresh(c)
    assert (c.cover_attachment_id, c.tag, c.tz) == (None, None, None)
    assert (c.span_start, c.span_end) == (START, END)

    with pytest.raises(coll_svc.CollectionInvalid):
        await coll_svc.update_collection(adb, c, {"span_end": None})
    with pytest.raises(coll_svc.CollectionInvalid):
        await coll_svc.update_collection(adb, c, {"span_start": None, "span_end": None})
    await adb.refresh(c)
    assert (c.tag, c.span_start, c.span_end) == (None, START, END)


async def test_update_refuses_clearing_one_bound_of_a_tagged_collection(adb):
    a = await _user(adb, "upd-tagged")
    c = await _coll(adb, a, tag="c746updt", span_start=START, span_end=END)
    with pytest.raises(coll_svc.CollectionInvalid):
        await coll_svc.update_collection(adb, c, {"span_start": None})
    # The object itself first: a refresh would reload over a write made before the refusal.
    assert (c.tag, c.span_start, c.span_end) == ("c746updt", START, END)
    await adb.refresh(c)
    assert (c.tag, c.span_start, c.span_end) == ("c746updt", START, END)
    await coll_svc.update_collection(adb, c, {"span_start": None, "span_end": None})
    await adb.refresh(c)
    assert (c.tag, c.span_start, c.span_end) == ("c746updt", None, None)


async def test_update_of_a_collection_deleted_meanwhile_is_collection_gone(adb):
    a = await _user(adb, "upd-gone")
    c = await _coll(adb, a, tag="c746gone")
    await adb.execute(text("DELETE FROM collections WHERE id = :i"), {"i": c.id})
    with pytest.raises(coll_svc.CollectionGone):
        await coll_svc.update_collection(adb, c, {"title": "Late"})


async def test_update_refuses_an_unknown_field(adb):
    a = await _user(adb, "upd-unknown")
    c = await _coll(adb, a, tag="c746updu")
    with pytest.raises(KeyError):
        await coll_svc.update_collection(adb, c, {"created_by": None})


# --- create idempotency, get, list, delete, FK lifecycle ---------------------------------------


async def test_create_with_a_key_twice_is_one_record_and_the_key_is_per_creator(adb):
    a = await _user(adb, "idem-a")
    b = await _user(adb, "idem-b")
    first, made = await coll_svc.create_collection(adb, a, title="T", tag="t", idempotency_key="k1")
    again, made_again = await coll_svc.create_collection(
        adb, a, title="Other", tag="u", idempotency_key="k1"
    )
    assert (made, made_again, again.id, again.title) == (True, False, first.id, "T")
    theirs, made_theirs = await coll_svc.create_collection(
        adb, b, title="T", tag="t", idempotency_key="k1"
    )
    assert made_theirs and theirs.id != first.id
    keyed = await adb.execute(
        select(func.count()).select_from(Collection).where(Collection.idempotency_key == "k1")
    )
    assert keyed.scalar_one() == 2


async def test_a_keyed_replay_returns_the_record_even_after_its_cover_was_deleted(adb):
    """Response lost, the cover reclaimed, the client retries with the same key and the same
    cover: the replay is answered from the stored record before the cover is looked at — the
    record the first call made, never CoverGone (the memo route's replay-first order)."""
    a = await _user(adb, "idem-cover")
    m = await _memo(adb, a, "m-idem-cover")
    cover = await _att(adb, a, m, "idem-cover-att", capture_instant=INSIDE)
    cover_id = cover.id
    first, made = await coll_svc.create_collection(
        adb, a, title="T", tag="c746idemcover", cover_attachment_id=cover_id, idempotency_key="k-c"
    )
    await adb.execute(delete(Attachment).where(Attachment.id == cover_id))
    again, made_again = await coll_svc.create_collection(
        adb, a, title="T", tag="c746idemcover", cover_attachment_id=cover_id, idempotency_key="k-c"
    )
    assert (made, made_again, again.uid) == (True, False, first.uid)
    # The replay is the stored record, so it answers even when its own fields would be refused.
    replay, made_replay = await coll_svc.create_collection(adb, a, title="T", idempotency_key="k-c")
    assert (made_replay, replay.uid) == (False, first.uid)


async def test_get_list_and_delete(adb):
    a = await _user(adb, "gld")
    early = await _coll(adb, a, tag="c746gld", span_start=START, span_end=END)
    late = await _coll(
        adb, a, span_start=START + timedelta(days=30), span_end=END + timedelta(days=30)
    )
    spanless = await _coll(adb, a, tag="c746gld")
    assert (await coll_svc.get_collection(adb, early.uid)).id == early.id
    assert await coll_svc.get_collection(adb, "no-such-uid") is None
    mine = [c.id for c in await coll_svc.list_collections(adb) if c.created_by == a.id]
    assert mine == [late.id, early.id, spanless.id]
    await coll_svc.delete_collection(adb, early)
    assert await coll_svc.get_collection(adb, early.uid) is None


async def test_deleting_a_collection_or_an_attachment_takes_its_exclusions(adb):
    a = await _user(adb, "fk")
    m = await _memo(adb, a, "m-fk", tags=("c746fk",))
    x = await _att(adb, a, m, "fk-x", capture_instant=INSIDE)
    y = await _att(adb, a, m, "fk-y", capture_instant=INSIDE)
    c1 = await _coll(adb, a, tag="c746fk", cover_attachment_id=y.id)
    c2 = await _coll(adb, a, tag="c746fk")
    await coll_svc.exclude(adb, c1, x)
    await coll_svc.exclude(adb, c2, y)
    await coll_svc.delete_collection(adb, c1)
    left = await adb.execute(
        select(CollectionExclusion.collection_id, CollectionExclusion.attachment_id)
    )
    assert [r for r in left if r[0] == c1.id] == []
    await adb.execute(delete(Attachment).where(Attachment.id == y.id))
    left = await adb.execute(
        select(func.count())
        .select_from(CollectionExclusion)
        .where(CollectionExclusion.collection_id == c2.id)
    )
    assert left.scalar_one() == 0
    await adb.refresh(c2)
    assert c2.title == "Trip"


async def test_a_deleted_cover_is_cleared_and_a_deleted_creator_leaves_the_collection(adb):
    a = await _user(adb, "fk-cover")
    m = await _memo(adb, a, "m-fk-cover")
    cover = await _att(adb, a, m, "fk-cover", capture_instant=INSIDE)
    c = await _coll(adb, a, tag="c746cover", cover_attachment_id=cover.id)
    await adb.execute(delete(Attachment).where(Attachment.id == cover.id))
    got = await adb.execute(
        text("SELECT cover_attachment_id, created_by FROM collections WHERE id = :i"), {"i": c.id}
    )
    assert got.one() == (None, a.id)
    await adb.execute(delete(Memo).where(Memo.owner_id == a.id))
    await adb.execute(delete(User).where(User.id == a.id))
    got = await adb.execute(
        text("SELECT cover_attachment_id, created_by FROM collections WHERE id = :i"), {"i": c.id}
    )
    assert got.one() == (None, None)


# --- concurrency: committed rows, two sessions racing ------------------------------------------


@pytest.fixture
async def committed_user(db_engine):
    subject = "c746-race"
    async with async_session() as s:
        await s.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        u = User(subject=subject, role="user")
        s.add(u)
        await s.commit()
        uid = u.id
    try:
        yield uid
    finally:
        async with async_session() as s:
            await s.execute(text("DELETE FROM collections WHERE created_by = :u"), {"u": uid})
            await s.execute(text("DELETE FROM attachments WHERE owner_id = :u"), {"u": uid})
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await s.commit()


async def test_two_racing_keyed_creates_are_one_record(committed_user, monkeypatch):
    """Both racers are held after their key lookup MISSES until both have missed, so both insert:
    one wins and the other necessarily takes the ON CONFLICT DO NOTHING path and reads the winner
    back — three lookups in all (two misses, one hit), never a sequential replay."""
    real_lookup = coll_svc.get_by_idempotency_key
    both_missed = asyncio.Barrier(2)
    lookups: list[bool] = []

    async def held_lookup(db, created_by, key):
        found = await real_lookup(db, created_by, key)
        lookups.append(found is not None)
        if found is None and lookups.count(False) <= 2:
            async with asyncio.timeout(10):
                await both_missed.wait()
        return found

    monkeypatch.setattr(coll_svc, "get_by_idempotency_key", held_lookup)

    async def create() -> tuple[int, bool]:
        async with async_session() as s:
            user = await s.get(User, committed_user)
            assert user is not None
            c, made = await coll_svc.create_collection(
                s, user, title="Race", tag="c746race", idempotency_key="race-key"
            )
            await s.commit()
            return c.id, made

    results = await asyncio.gather(create(), create())
    assert results[0][0] == results[1][0]
    assert sorted(made for _, made in results) == [False, True]
    assert lookups == [False, False, True]
    async with async_session() as s:
        n = await s.execute(
            text("SELECT count(*) FROM collections WHERE created_by = :u"), {"u": committed_user}
        )
        assert n.scalar_one() == 1


async def test_two_racing_exclusions_are_one_row(committed_user):
    async with async_session() as s:
        user = await s.get(User, committed_user)
        assert user is not None
        att = await _att(s, user, None, "c746-race-att")
        c, _ = await coll_svc.create_collection(s, user, title="Race", tag="c746race2")
        await s.commit()
        cid, aid = c.id, att.id

    async def exclude() -> None:
        async with async_session() as s:
            c = await s.get(Collection, cid)
            a = await s.get(Attachment, aid)
            assert c is not None and a is not None
            await coll_svc.exclude(s, c, a)
            await s.commit()

    await asyncio.gather(exclude(), exclude())
    async with async_session() as s:
        n = await s.execute(
            text("SELECT count(*) FROM collection_exclusions WHERE collection_id = :c"), {"c": cid}
        )
        assert n.scalar_one() == 1


async def test_an_update_revalidates_against_the_row_a_concurrent_update_committed(committed_user):
    """A clears the tag (valid: the span remains) while B, holding the SAME pre-A view, clears the
    span (valid in that view: the tag remains). B must wait for A's row lock, then judge the row A
    committed — which has no tag — and refuse with CollectionInvalid, never reach the database's
    source CHECK as an IntegrityError. Deterministic: B's stale view is taken before A writes, and
    B is observed waiting on the lock before A commits."""
    async with async_session() as s:
        user = await s.get(User, committed_user)
        assert user is not None
        c, _ = await coll_svc.create_collection(
            s, user, title="Lock", tag="c746lock", span_start=START, span_end=END
        )
        await s.commit()
        cid = c.id

    async with async_session() as first, async_session() as second:
        stale = await second.get(Collection, cid)  # B's view: tag and span both present
        assert stale is not None and stale.tag == "c746lock"
        await second.commit()  # B holds no snapshot or lock while it waits for its turn
        await second.execute(text("SET LOCAL statement_timeout = '5s'"))
        b_pid = await _backend_pid(second)
        mine = await first.get(Collection, cid)
        assert mine is not None
        await coll_svc.update_collection(first, mine, {"tag": None})  # A: row locked, not committed

        b_update = asyncio.create_task(
            coll_svc.update_collection(second, stale, {"span_start": None, "span_end": None})
        )
        async with asyncio.timeout(10):
            while not (await _lock_state(b_pid))[0]:
                assert not b_update.done(), b_update
                await asyncio.sleep(0.05)
        assert not b_update.done()
        await first.commit()

        with pytest.raises(coll_svc.CollectionInvalid):
            await b_update
        await second.rollback()

    async with async_session() as s:
        row = await s.execute(
            text("SELECT tag, span_start, span_end FROM collections WHERE id = :i"), {"i": cid}
        )
        assert row.one() == (None, START, END)


async def _backend_pid(session) -> int:
    return (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def _lock_state(pid: int) -> tuple[bool, bool]:
    """(is `pid` waiting on any lock, does it hold the row-lock intent on `collections`). A FOR
    UPDATE / FOR KEY SHARE leaves a RowShareLock on its table for the rest of the transaction; a
    plain read leaves only AccessShareLock — so RowShareLock on `collections` is "C taken"."""
    async with async_session() as s:
        await s.execute(text("SET LOCAL statement_timeout = '5s'"))
        waiting = await s.execute(
            text("SELECT count(*) FROM pg_locks WHERE pid = :p AND NOT granted"), {"p": pid}
        )
        holds_c = await s.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE pid = :p AND granted "
                "AND relation = 'collections'::regclass AND mode = 'RowShareLock'"
            ),
            {"p": pid},
        )
        return waiting.scalar_one() > 0, holds_c.scalar_one() > 0


async def test_an_update_setting_a_cover_being_deleted_waits_on_it_first_and_finds_it_gone(
    committed_user,
):
    """Lock order A < C. The deleter holds the cover attachment's row (as reclaim does,
    app/services/reclaim.py:278-283) before the update starts; the update must wait on THAT row
    while holding no lock on the collection, and once the delete commits it is refused with
    CoverGone — never a deadlock, never a foreign-key IntegrityError — leaving the record as it
    was. statement_timeout bounds every statement, so a wrong order fails rather than hangs."""
    async with async_session() as s:
        user = await s.get(User, committed_user)
        assert user is not None
        att = await _att(s, user, None, "c746-cover-del")
        c, _ = await coll_svc.create_collection(s, user, title="Cover", tag="c746coverdel")
        await s.commit()
        cid, aid = c.id, att.id

    async with async_session() as deleter, async_session() as updater:
        await deleter.execute(text("SET LOCAL statement_timeout = '5s'"))
        await deleter.execute(select(Attachment.id).where(Attachment.id == aid).with_for_update())
        mine = await updater.get(Collection, cid)
        assert mine is not None
        await updater.commit()
        await updater.execute(text("SET LOCAL statement_timeout = '5s'"))
        pid = await _backend_pid(updater)

        task = asyncio.create_task(
            coll_svc.update_collection(updater, mine, {"cover_attachment_id": aid})
        )
        async with asyncio.timeout(10):
            while not (state := await _lock_state(pid))[0]:
                assert not task.done(), task
                await asyncio.sleep(0.05)
        holds_c_while_waiting = state[1]
        await deleter.execute(delete(Attachment).where(Attachment.id == aid))
        await deleter.commit()
        try:
            await task
            outcome: object = "updated"
        except Exception as exc:  # the assertion below names whatever arrived
            outcome = exc
        await updater.rollback()

    assert isinstance(outcome, coll_svc.CoverGone), repr(outcome)
    assert not holds_c_while_waiting
    async with async_session() as s:
        row = await s.execute(
            text("SELECT cover_attachment_id, tag FROM collections WHERE id = :i"), {"i": cid}
        )
        assert row.one() == (None, "c746coverdel")


async def test_a_delete_of_a_cover_being_set_waits_for_the_update_and_then_clears_it(
    committed_user,
):
    """The mirror: the update holds the cover's KEY SHARE and the collection row, uncommitted; the
    attachment delete waits for it, then proceeds, and its ON DELETE SET NULL clears the cover."""
    async with async_session() as s:
        user = await s.get(User, committed_user)
        assert user is not None
        att = await _att(s, user, None, "c746-cover-set")
        c, _ = await coll_svc.create_collection(s, user, title="Cover", tag="c746coverset")
        await s.commit()
        cid, aid = c.id, att.id

    async with async_session() as updater, async_session() as deleter:
        await updater.execute(text("SET LOCAL statement_timeout = '5s'"))
        mine = await updater.get(Collection, cid)
        assert mine is not None
        await coll_svc.update_collection(updater, mine, {"cover_attachment_id": aid})
        await deleter.execute(text("SET LOCAL statement_timeout = '5s'"))
        pid = await _backend_pid(deleter)

        async def remove() -> None:
            await deleter.execute(delete(Attachment).where(Attachment.id == aid))
            await deleter.commit()

        task = asyncio.create_task(remove())
        async with asyncio.timeout(10):
            while not (await _lock_state(pid))[0]:
                assert not task.done(), task
                await asyncio.sleep(0.05)
        await updater.commit()
        await task

    async with async_session() as s:
        row = await s.execute(
            text("SELECT cover_attachment_id FROM collections WHERE id = :i"), {"i": cid}
        )
        assert row.one() == (None,)
        gone = await s.execute(text("SELECT count(*) FROM attachments WHERE id = :i"), {"i": aid})
        assert gone.scalar_one() == 0


async def test_a_cover_that_does_not_exist_is_cover_gone_on_create_and_update(adb):
    a = await _user(adb, "cover-missing")
    with pytest.raises(coll_svc.CoverGone):
        await coll_svc.create_collection(adb, a, title="T", tag="t", cover_attachment_id=2**31 - 1)
    c = await _coll(adb, a, tag="c746covermissing")
    with pytest.raises(coll_svc.CoverGone):
        await coll_svc.update_collection(adb, c, {"cover_attachment_id": 2**31 - 1})
    assert c.cover_attachment_id is None


async def test_a_keyed_create_whose_cover_went_with_a_winner_returns_the_winner(
    committed_user, monkeypatch
):
    """B's key lookup misses; before B holds the cover, A creates with the same key and commits,
    and the cover attachment is deleted. The cover's absence must not hide A's record: B looks the
    key up again and answers with the winner, never CoverGone."""
    async with async_session() as s:
        user = await s.get(User, committed_user)
        assert user is not None
        att = await _att(s, user, None, "c746-cover-winner")
        await s.commit()
        aid = att.id

    real_lookup = coll_svc.get_by_idempotency_key
    winner: list[str] = []
    calls: list[int] = []

    async def lookup_then_lose(db, created_by, key):
        calls.append(1)
        found = await real_lookup(db, created_by, key)
        if len(calls) == 1:  # B's first lookup has missed; now A wins and the cover goes
            assert found is None
            async with async_session() as a_side:
                a_user = await a_side.get(User, committed_user)
                assert a_user is not None
                made, _ = await coll_svc.create_collection(
                    a_side,
                    a_user,
                    title="W",
                    tag="c746winner",
                    cover_attachment_id=aid,
                    idempotency_key="k-winner",
                )
                winner.append(made.uid)
                await a_side.execute(delete(Attachment).where(Attachment.id == aid))
                await a_side.commit()
        return found

    monkeypatch.setattr(coll_svc, "get_by_idempotency_key", lookup_then_lose)
    async with async_session() as b_side:
        b_user = await b_side.get(User, committed_user)
        assert b_user is not None
        got, made_by_b = await coll_svc.create_collection(
            b_side,
            b_user,
            title="W",
            tag="c746winner",
            cover_attachment_id=aid,
            idempotency_key="k-winner",
        )
        await b_side.commit()
    assert (got.uid, made_by_b) == (winner[0], False)
