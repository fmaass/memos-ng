from datetime import UTC, datetime, timedelta

from app.models import Attachment, AttachmentStatus, Memo, User, Visibility
from app.routers.memos import MemoOut, _out
from app.services import memos as svc
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL


async def _user(adb, subject):
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def test_create_parses_tags_and_generates_uid(adb):
    u = await _user(adb, "alice")
    m = await svc.create_memo(adb, u, content="hi #foo #bar #foo", visibility=Visibility.PRIVATE)
    assert m.uid and len(m.uid) >= 12
    assert sorted(t.name for t in m.tags) == ["bar", "foo"]


async def test_visibility_matrix(adb):
    alice, bob = await _user(adb, "alice"), await _user(adb, "bob")
    priv = await svc.create_memo(adb, alice, content="secret", visibility=Visibility.PRIVATE)
    prot = await svc.create_memo(adb, alice, content="prot", visibility=Visibility.PROTECTED)
    pub = await svc.create_memo(adb, alice, content="pub", visibility=Visibility.PUBLIC)

    _, _, total_a = await svc.list_memos(adb, alice)
    assert total_a == 3  # owner sees own PRIVATE

    items_b, _, total_b = await svc.list_memos(adb, bob)
    uids_b = {m.uid for m in items_b}
    # This assertion FAILS if the PRIVATE visibility filter is dropped (bob would see priv).
    assert priv.uid not in uids_b
    assert prot.uid in uids_b and pub.uid in uids_b and total_b == 2

    assert await svc.get_memo(adb, bob, priv.uid) is None
    assert (await svc.get_memo(adb, alice, priv.uid)).uid == priv.uid


async def test_filters_date_tag_visibility_owner_combo(adb):
    alice, bob = await _user(adb, "alice"), await _user(adb, "bob")
    old = await svc.create_memo(adb, alice, content="old #x", visibility=Visibility.PUBLIC)
    old.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    new = await svc.create_memo(adb, alice, content="new #y", visibility=Visibility.PROTECTED)
    new.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    await svc.create_memo(adb, bob, content="bobs #x", visibility=Visibility.PUBLIC)
    await adb.flush()

    _, _, before = await svc.list_memos(adb, alice, created_before=datetime(2022, 1, 1, tzinfo=UTC))
    assert before == 1  # only the 2020 memo
    items, _, _ = await svc.list_memos(adb, alice, tags=["x"])
    assert {m.uid for m in items} == {old.uid} or all(
        "x" in [t.name for t in m.tags] for m in items
    )
    _, _, vis = await svc.list_memos(adb, alice, visibility=Visibility.PROTECTED)
    assert vis == 1
    _, _, owned = await svc.list_memos(adb, alice, owner="bob")
    assert owned == 1
    # composed: alice's PUBLIC tagged x
    _, _, combo = await svc.list_memos(
        adb, alice, owner="alice", tags=["x"], visibility=Visibility.PUBLIC
    )
    assert combo == 1


async def test_cursor_pagination_stable_totalcount(adb):
    u = await _user(adb, "alice")
    for i in range(5):
        m = await svc.create_memo(adb, u, content=f"m{i}", visibility=Visibility.PUBLIC)
        m.created_at = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=i)
    await adb.flush()

    seen, cursor = [], None
    for _ in range(3):
        items, cursor, total = await svc.list_memos(adb, u, limit=2, cursor=cursor)
        assert total == 5
        seen.extend(m.uid for m in items)
        if cursor is None:
            break
    assert len(seen) == 5 and len(set(seen)) == 5  # no dupes, no gaps


async def test_sort_asc_desc(adb):
    u = await _user(adb, "alice")
    a = await svc.create_memo(adb, u, content="a", visibility=Visibility.PUBLIC)
    a.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    b = await svc.create_memo(adb, u, content="b", visibility=Visibility.PUBLIC)
    b.created_at = datetime(2024, 6, 1, tzinfo=UTC)
    await adb.flush()
    desc, _, _ = await svc.list_memos(adb, u, sort="-created_at")
    asc, _, _ = await svc.list_memos(adb, u, sort="created_at")
    assert desc[0].uid == b.uid and asc[0].uid == a.uid


async def test_fulltext_search(adb):
    u = await _user(adb, "alice")
    await svc.create_memo(adb, u, content="apples and oranges", visibility=Visibility.PUBLIC)
    await svc.create_memo(adb, u, content="bananas only", visibility=Visibility.PUBLIC)
    items, _, total = await svc.list_memos(adb, u, q="apples")
    assert total == 1 and "apples" in items[0].content


async def test_archive_hides_from_default_list(adb):
    u = await _user(adb, "alice")
    m = await svc.create_memo(adb, u, content="x", visibility=Visibility.PUBLIC)
    await svc.update_memo(adb, m, archived=True)
    _, _, default = await svc.list_memos(adb, u)
    _, _, witharch = await svc.list_memos(adb, u, include_archived=True)
    assert default == 0 and witharch == 1


async def test_tag_counts(adb):
    u = await _user(adb, "alice")
    await svc.create_memo(adb, u, content="#travel a", visibility=Visibility.PUBLIC)
    await svc.create_memo(adb, u, content="#travel b", visibility=Visibility.PUBLIC)
    await svc.create_memo(adb, u, content="#food c", visibility=Visibility.PUBLIC)
    counts = dict(await svc.list_tags(adb, u))
    assert counts == {"travel": 2, "food": 1}


# --- HTTP-level: auth-by-default + schema hygiene ---


def test_memos_requires_auth(client):
    assert client.get("/api/memos").status_code == 401


def test_memoout_has_no_dead_fields():
    fields = set(MemoOut.model_fields)
    for dead in ("externalLink", "motionMedia", "snippet", "property"):
        assert dead not in fields
    assert {"uid", "content", "visibility", "tags", "owner"} <= fields


async def test_delete_memo_removes_it(adb):
    u = await _user(adb, "alice")
    m = await svc.create_memo(adb, u, content="bye", visibility=Visibility.PUBLIC)
    await svc.delete_memo(adb, m)
    assert await svc.get_memo(adb, u, m.uid) is None


async def test_heatmap_groups_by_date(adb):
    from datetime import UTC, datetime

    u = await _user(adb, "alice")
    a = await svc.create_memo(adb, u, content="x", visibility=Visibility.PUBLIC)
    a.created_at = datetime(2024, 3, 1, 9, tzinfo=UTC)
    b = await svc.create_memo(adb, u, content="y", visibility=Visibility.PUBLIC)
    b.created_at = datetime(2024, 3, 1, 18, tzinfo=UTC)
    c = await svc.create_memo(adb, u, content="z", visibility=Visibility.PUBLIC)
    c.created_at = datetime(2024, 3, 2, 9, tzinfo=UTC)
    await adb.flush()
    hm = await svc.heatmap(adb, u)
    assert hm.get("2024-03-01") == 2 and hm.get("2024-03-02") == 1


async def test_create_memo_links_only_owners_attachments(adb):
    alice = await _user(adb, "alice")
    bob = await _user(adb, "bob")
    a_att = Attachment(
        uid="a-att", owner_id=alice.id, filename="a.jpg", mime="image/jpeg",
        size=1, status=AttachmentStatus.ready,
    )
    b_att = Attachment(
        uid="b-att", owner_id=bob.id, filename="b.jpg", mime="image/jpeg",
        size=1, status=AttachmentStatus.ready,
    )
    adb.add_all([a_att, b_att])
    await adb.flush()

    memo = await svc.create_memo(
        adb, alice, content="pics", visibility=Visibility.PRIVATE,
        attachment_uids=["a-att", "b-att"],  # tries to claim bob's too
    )
    await adb.refresh(a_att)
    await adb.refresh(b_att)
    # Authz: only alice's attachment links; bob's is silently ignored (not stolen).
    assert a_att.memo_id == memo.id
    assert b_att.memo_id is None


async def test_memo_out_serializes_attachment_tier_urls(adb):
    alice = await _user(adb, "alice")
    att = Attachment(
        uid="att1", owner_id=alice.id, filename="v.mp4", mime="video/mp4",
        size=1, status=AttachmentStatus.processing, blurhash="LKO2",
    )
    adb.add(att)
    await adb.flush()
    memo = await svc.create_memo(
        adb, alice, content="vid", visibility=Visibility.PRIVATE, attachment_uids=["att1"]
    )
    # Re-select so the selectin attachments/owner/tags relationships are eagerly loaded.
    m2 = (await adb.execute(select(Memo).where(Memo.uid == memo.uid))).scalar_one()
    out = _out(m2, alice.id)
    assert len(out.attachments) == 1
    a = out.attachments[0]
    assert a.uid == "att1" and a.status is AttachmentStatus.processing and a.blurhash == "LKO2"
    # Derived tiers are HMAC-signed (DB-free serve); original stays unsigned (auth path).
    assert a.thumbnailUrl.split("?")[0] == "/api/attachments/att1/thumb"
    assert "sig=" in a.thumbnailUrl
    assert a.webUrl.split("?")[0] == "/api/attachments/att1/web"
    assert a.posterUrl.split("?")[0] == "/api/attachments/att1/poster"
    assert a.downloadUrl == "/api/attachments/att1/original"


async def test_create_and_edit_custom_display_date(adb):
    # Parity with old memos: a memo can be backdated on create and its date edited later.
    u = await _user(adb, "dater")
    custom = datetime(2020, 5, 1, 12, 0, tzinfo=UTC)
    m = await svc.create_memo(
        adb, u, content="backdated", visibility=Visibility.PUBLIC, created_at=custom
    )
    assert m.created_at == custom
    new = datetime(2019, 1, 2, 9, 30, tzinfo=UTC)
    await svc.update_memo(adb, m, created_at=new)
    assert m.created_at == new


async def test_archived_only_filter(adb):
    u = await _user(adb, "alice")
    await svc.create_memo(adb, u, content="active", visibility=Visibility.PUBLIC)
    arch = await svc.create_memo(adb, u, content="arch", visibility=Visibility.PUBLIC)
    await svc.update_memo(adb, arch, archived=True)

    items, _, total = await svc.list_memos(adb, u, archived_only=True)
    assert total == 1 and items[0].uid == arch.uid  # only the archived memo
    _, _, default = await svc.list_memos(adb, u)
    assert default == 1  # default still excludes archived


def test_pin_unpin_rejects_non_owner(logged_in_client):
    # A user can SEE another user's PUBLIC memo, but pinning is a mutation — owner-only.
    # Regression: pin/unpin shared _require_owned (visibility check) without an ownership
    # guard, so any user could pin another's PUBLIC memo.
    client, _ = logged_in_client
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            oid = c.execute(
                text(
                    "INSERT INTO users (subject, role, default_visibility, theme) "
                    "VALUES ('other_owner','user','PUBLIC','dark') RETURNING id"
                )
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO memos (uid, owner_id, content, visibility, pinned, "
                    "created_at, updated_at) VALUES ('othermemo', :oid, 'x', 'PUBLIC', false, "
                    "now(), now())"
                ),
                {"oid": oid},
            )
        assert client.post("/api/memos/othermemo/pin").status_code == 403
        assert client.post("/api/memos/othermemo/unpin").status_code == 403
        with eng.connect() as c:
            still = c.execute(text("SELECT pinned FROM memos WHERE uid='othermemo'")).scalar_one()
        assert still is False  # untouched
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM memos WHERE uid='othermemo'"))
            c.execute(text("DELETE FROM users WHERE subject='other_owner'"))
        eng.dispose()


def test_reactions_toggle_count_and_reacted(logged_in_client):
    client, _ = logged_in_client
    uid = client.post("/api/memos", json={"content": "react", "visibility": "PUBLIC"}).json()["uid"]

    r = client.post(f"/api/memos/{uid}/reactions", json={"emoji": "👍"}).json()
    assert r["reactions"] == [{"emoji": "👍", "count": 1, "reacted": True}]

    # idempotent — same user, same emoji
    r2 = client.post(f"/api/memos/{uid}/reactions", json={"emoji": "👍"}).json()
    assert r2["reactions"][0]["count"] == 1

    # a second emoji
    r3 = client.post(f"/api/memos/{uid}/reactions", json={"emoji": "❤️"}).json()
    assert {x["emoji"] for x in r3["reactions"]} == {"👍", "❤️"}

    # remove the first
    r4 = client.delete(f"/api/memos/{uid}/reactions/👍").json()
    assert r4["reactions"] == [{"emoji": "❤️", "count": 1, "reacted": True}]

    # the memo list also carries reactions
    listed = next(m for m in client.get("/api/memos").json()["items"] if m["uid"] == uid)
    assert listed["reactions"][0]["emoji"] == "❤️"


async def test_list_memos_total_is_opt_in(adb):
    u = await _user(adb, "alice")
    await svc.create_memo(adb, u, content="a", visibility=Visibility.PUBLIC)
    await svc.create_memo(adb, u, content="b", visibility=Visibility.PUBLIC)

    # The COUNT scans every visible memo and the feed never shows a total — so it's skippable.
    _, _, skipped = await svc.list_memos(adb, u, with_total=False)
    assert skipped is None
    # Opt-in still returns an accurate count for callers that need it.
    _, _, counted = await svc.list_memos(adb, u, with_total=True)
    assert counted == 2


def test_list_memos_endpoint_omits_total_by_default(logged_in_client):
    client, _ = logged_in_client
    client.post("/api/memos", json={"content": "one", "visibility": "PUBLIC"})
    client.post("/api/memos", json={"content": "two", "visibility": "PUBLIC"})

    # Default feed response carries no total (the COUNT is skipped on the hot path).
    assert client.get("/api/memos").json()["totalCount"] is None
    # Explicit opt-in computes it.
    assert client.get("/api/memos?with_total=true").json()["totalCount"] == 2


def test_memo_create_survives_frame_enqueue_error(logged_in_client, monkeypatch):
    """The optional frame-share enqueue is savepoint-isolated: an unexpected error inside it (even
    after a partial DB write) must NOT abort the core memo save, and its partial work is rolled
    back. Without the SAVEPOINT the raise would 500 the endpoint and lose the memo."""
    client, _ = logged_in_client
    from app.routers import memos as memos_router

    async def insert_probe_then_boom(db, memo, user):
        from app.models import Tag

        db.add(Tag(name="frame-savepoint-probe"))
        await db.flush()  # a real write inside the enqueue's savepoint
        raise RuntimeError("enqueue exploded after a partial write")

    monkeypatch.setattr(
        memos_router.frame_share_svc, "maybe_enqueue_frame_shares", insert_probe_then_boom
    )
    r = client.post("/api/memos", json={"content": "core write must survive"})
    assert r.status_code in (200, 201)  # NOT a 500 — the memo save is not hostage to the feature
    uid = r.json()["uid"]
    assert client.get(f"/api/memos/{uid}").status_code == 200  # memo persisted

    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as c:
            probe = c.execute(
                text("SELECT count(*) FROM tags WHERE name = 'frame-savepoint-probe'")
            ).scalar()
            jobs = c.execute(
                text(
                    "SELECT count(*) FROM frame_share_jobs j JOIN memos m ON j.memo_id = m.id "
                    "WHERE m.uid = :uid"
                ),
                {"uid": uid},
            ).scalar()
        assert probe == 0  # savepoint rolled the partial write back
        assert jobs == 0  # no frame-share job leaked
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM tags WHERE name = 'frame-savepoint-probe'"))
        eng.dispose()
