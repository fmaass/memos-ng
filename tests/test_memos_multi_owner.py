"""`owner=` on GET /api/memos is repeatable and ORs (MEMNG-133).

The iOS filter sheet asks for several household members at once, so the parameter that used to
name exactly one `User.subject` now names a set and answers with their union. Everything else about
the filter is unchanged, and the single-owner cases in tests/test_memos.py are the regression proof
for that — they are deliberately not touched here.
"""

from datetime import UTC, datetime, timedelta

from app.models import Visibility
from app.services import memos as svc
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_memos import _user

OWNERS = ("mowner_a", "mowner_b", "mowner_c")
PAGE = 2


# --- the normalisation the filter is built on ------------------------------------------------


def test_owner_subjects_reads_both_spellings_and_means_the_same_thing():
    assert svc.owner_subjects(None) == []
    assert svc.owner_subjects("alice") == ["alice"]
    assert svc.owner_subjects(["alice"]) == ["alice"]
    assert svc.owner_subjects(["alice", "bob"]) == ["alice", "bob"]


def test_owner_subjects_drops_the_spellings_that_named_nobody_before():
    # An empty string filtered nothing when owner was a bare string (`if owner:`); it must not
    # start meaning "a user whose subject is the empty string" now that it rides in a list.
    assert svc.owner_subjects("") == []
    assert svc.owner_subjects([]) == []
    assert svc.owner_subjects(["", "alice"]) == ["alice"]


def test_owner_subjects_asks_for_a_repeated_member_once():
    assert svc.owner_subjects(["alice", "bob", "alice"]) == ["alice", "bob"]


# --- the union, at the service ----------------------------------------------------------------


async def _seed_three_owners(adb):
    """One PUBLIC memo each for a, b and c, plus a PRIVATE one for b. Returns the users."""
    alice, bob, carol = [await _user(adb, s) for s in OWNERS]
    made = {}
    for user, subject in ((alice, "mowner_a"), (bob, "mowner_b"), (carol, "mowner_c")):
        made[subject] = await svc.create_memo(
            adb, user, content=f"{subject} public", visibility=Visibility.PUBLIC
        )
    made["mowner_b_private"] = await svc.create_memo(
        adb, bob, content="b private", visibility=Visibility.PRIVATE
    )
    await adb.flush()
    return alice, bob, carol, made


async def test_two_owners_return_the_union_and_nothing_from_a_third(adb):
    alice, _, _, made = await _seed_three_owners(adb)
    reader = await _user(adb, "mowner_reader")
    items, _, _ = await svc.list_memos(adb, reader, owner=["mowner_a", "mowner_b"])
    uids = {m.uid for m in items}
    assert made["mowner_a"].uid in uids
    assert made["mowner_b"].uid in uids
    assert made["mowner_c"].uid not in uids
    assert alice is not None


async def test_repeating_one_owner_returns_their_memos_once(adb):
    _, _, _, made = await _seed_three_owners(adb)
    reader = await _user(adb, "mowner_reader")
    items, _, total = await svc.list_memos(
        adb, reader, owner=["mowner_a", "mowner_a"], with_total=True
    )
    uids = [m.uid for m in items]
    assert uids == [made["mowner_a"].uid]
    assert total == 1


async def test_a_single_owner_still_answers_the_way_a_bare_string_did(adb):
    _, _, _, made = await _seed_three_owners(adb)
    reader = await _user(adb, "mowner_reader")
    as_string, _, total_string = await svc.list_memos(
        adb, reader, owner="mowner_a", with_total=True
    )
    as_list, _, total_list = await svc.list_memos(adb, reader, owner=["mowner_a"], with_total=True)
    assert [m.uid for m in as_string] == [m.uid for m in as_list] == [made["mowner_a"].uid]
    assert total_string == total_list == 1


async def test_the_union_never_widens_what_the_caller_may_see(adb):
    """Asking for a member does not grant sight of them: their PRIVATE memos stay theirs, and the
    same query run by the owner does see them — so this fails if the visibility clause is dropped
    rather than passing for want of data."""
    _, bob, _, made = await _seed_three_owners(adb)
    reader = await _user(adb, "mowner_reader")
    owners = ["mowner_a", "mowner_b"]

    seen_by_reader = {m.uid for m in (await svc.list_memos(adb, reader, owner=owners))[0]}
    assert made["mowner_b_private"].uid not in seen_by_reader

    seen_by_bob = {m.uid for m in (await svc.list_memos(adb, bob, owner=owners))[0]}
    assert made["mowner_b_private"].uid in seen_by_bob


async def test_the_union_still_composes_with_the_other_filters(adb):
    alice, bob, _, _ = await _seed_three_owners(adb)
    reader = await _user(adb, "mowner_reader")
    tagged = await svc.create_memo(adb, alice, content="a #keep", visibility=Visibility.PUBLIC)
    await svc.create_memo(adb, bob, content="b #keep", visibility=Visibility.PROTECTED)
    await adb.flush()

    items, _, _ = await svc.list_memos(
        adb, reader, owner=["mowner_a", "mowner_b"], tags=["keep"], visibility=Visibility.PUBLIC
    )
    # Both owners wrote a #keep memo; only alice's is PUBLIC, so the AND of the other filters
    # still narrows the union rather than being swallowed by it.
    assert [m.uid for m in items] == [tagged.uid]


async def test_paging_the_union_visits_every_row_once_and_no_outsider(adb):
    """The cursor is a keyset over (created_at, id) and carries NO filter state — the caller
    resends `owner` on every page — so the property under test is that the multi-owner condition
    is reapplied identically page after page. A page that quietly dropped it would pull in the
    third owner's interleaved rows."""
    alice, bob, carol = [await _user(adb, s) for s in OWNERS]
    base = datetime(2024, 3, 1, tzinfo=UTC)
    wanted, minute = [], 0
    for _ in range(3):  # a, b, c, a, b, c, … so an outsider sits between every wanted pair
        for user, subject in ((alice, "mowner_a"), (bob, "mowner_b"), (carol, "mowner_c")):
            memo = await svc.create_memo(
                adb, user, content=f"{subject} {minute}", visibility=Visibility.PUBLIC
            )
            memo.created_at = base + timedelta(minutes=minute)
            if subject != "mowner_c":
                wanted.append(memo.uid)
            minute += 1
    await adb.flush()

    reader = await _user(adb, "mowner_reader")
    owners = ["mowner_a", "mowner_b"]
    single_shot = [m.uid for m in (await svc.list_memos(adb, reader, owner=owners, limit=100))[0]]

    paged, cursor, pages = [], None, 0
    while True:
        items, cursor, _ = await svc.list_memos(
            adb, reader, owner=owners, limit=PAGE, cursor=cursor
        )
        paged.extend(m.uid for m in items)
        pages += 1
        if cursor is None:
            break
        assert pages < 10  # a cursor that never terminates is a failure, not a hang

    assert pages > 1  # the seed really did page
    assert paged == single_shot  # same rows, same order, no gap and no repeat
    assert sorted(paged) == sorted(wanted)


# --- the union, over the wire -------------------------------------------------------------------


def _sql(statement: str, **params):
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(statement), params)
            return result.all() if result.returns_rows else []
    finally:
        eng.dispose()


def _seed_committed_memos() -> list[str]:
    """Three PUBLIC memos each for a, b and c, interleaved in time. Returns a+b's uids, newest
    first — the order GET /api/memos must hand them back in."""
    uids = []
    for subject in OWNERS:
        _sql(
            "INSERT INTO users (subject, role, default_visibility, theme) "
            "VALUES (:s, 'user', 'PUBLIC', 'dark')",
            s=subject,
        )
    minute = 0
    for _ in range(3):
        for subject in OWNERS:
            uid = f"mo_{subject}_{minute}"
            _sql(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, "
                "updated_at) SELECT :u, id, :c, 'PUBLIC', false, "
                "TIMESTAMPTZ '2024-03-01 00:00:00+00' + make_interval(mins => :m), now() "
                "FROM users WHERE subject = :s",
                u=uid,
                c=f"{subject} {minute}",
                m=minute,
                s=subject,
            )
            if subject != "mowner_c":
                uids.append(uid)
            minute += 1
    return list(reversed(uids))


def _drop_committed_memos():
    _sql("DELETE FROM memos WHERE uid LIKE 'mo_mowner_%'")
    _sql("DELETE FROM users WHERE subject LIKE 'mowner_%'")


def test_the_query_string_really_carries_a_repeated_owner(logged_in_client):
    """httpx spells `?owner=a&owner=b` for a list, which is the shape the iOS client sends — the
    service-level cases above would still pass if FastAPI parsed only the last one."""
    client, _ = logged_in_client
    expected = _seed_committed_memos()
    try:
        response = client.get(
            "/api/memos",
            params={"owner": ["mowner_a", "mowner_b"], "limit": 100},
        )
        assert response.status_code == 200, response.text
        assert [m["uid"] for m in response.json()["items"]] == expected
    finally:
        _drop_committed_memos()


def test_paging_over_the_wire_keeps_the_multi_owner_filter_on_every_page(logged_in_client):
    client, _ = logged_in_client
    expected = _seed_committed_memos()
    try:
        seen, cursor, pages = [], None, 0
        while True:
            params: dict[str, object] = {"owner": ["mowner_a", "mowner_b"], "limit": PAGE}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/memos", params=params).json()
            seen.extend(m["uid"] for m in body["items"])
            cursor = body["nextCursor"]
            pages += 1
            if cursor is None:
                break
            assert pages < 10
        assert pages > 1
        assert seen == expected  # every wanted row once, in order, and no mowner_c row anywhere
    finally:
        _drop_committed_memos()


def test_a_single_owner_over_the_wire_is_unchanged(logged_in_client):
    client, _ = logged_in_client
    expected = _seed_committed_memos()
    try:
        only_a = [uid for uid in expected if "mowner_a" in uid]
        body = client.get("/api/memos", params={"owner": "mowner_a", "limit": 100}).json()
        assert [m["uid"] for m in body["items"]] == only_a
    finally:
        _drop_committed_memos()
