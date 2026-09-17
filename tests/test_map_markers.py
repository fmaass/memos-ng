"""`GET /api/map/markers` — the ONE place a stored coordinate is served.

The endpoint is client-neutral by design (the iOS client clusters on the device off the same body,
plan decision D8), so it takes no query parameter at all: it answers the whole located set the
caller may see. What it may see is the FEED's rule, not a rule of its own — `visible_clause` plus
"not archived" plus "ready" — because a pin is a photo, and a photo the feed hides is a photo the
map hides.

Each exclusion below is a separate case with its own row, because each one is a different clause
and a marker leaking through any of them hands a private photo's location to another member. The
visibility cases drive the SERVICE against the async session; the serialization cases drive the
real HTTP route with rows committed through the sync engine (the two clients must not share one
test — a TestClient call blocks the loop the async session lives on).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.models import Attachment, AttachmentStatus, Memo, User, Visibility
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

REPO = Path(__file__).resolve().parent.parent
OPENAPI = REPO / "openapi.json"


def _wipe():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM frame_share_jobs"))
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM memos"))
    eng.dispose()


async def _member(adb, subject: str) -> User:
    found = (await adb.execute(select(User).where(User.subject == subject))).scalar_one_or_none()
    if found is None:
        found = User(subject=subject, display_name=subject)
        adb.add(found)
        await adb.flush()
    return found


_uid_counter = iter(range(1, 10_000))


async def _memo(adb, owner: User, *, visibility=Visibility.PUBLIC, archived=False) -> Memo:
    memo = Memo(
        uid=f"memo-{next(_uid_counter)}",
        owner_id=owner.id,
        content="somewhere",
        visibility=visibility,
        archived_at=datetime.now(UTC) if archived else None,
    )
    adb.add(memo)
    await adb.flush()
    return memo


async def _located(
    adb,
    memo: Memo,
    *,
    uid: str,
    latitude: float | None = 12.5,
    longitude: float | None = 45.25,
    status=AttachmentStatus.ready,
    capture_instant: datetime | None = None,
    captured_at: datetime | None = None,
) -> Attachment:
    att = Attachment(
        uid=uid,
        owner_id=memo.owner_id,
        memo_id=memo.id,
        filename=f"{uid}.jpg",
        mime="image/jpeg",
        size=1,
        status=status,
        storage_path=f"/nowhere/{uid}.jpg",
        latitude=latitude,
        longitude=longitude,
        location_source="exif" if latitude is not None else "none",
        capture_instant=capture_instant,
        captured_at=captured_at,
    )
    adb.add(att)
    await adb.flush()
    return att


# --------------------------------------------------------------------------------------------
# Who may ask
# --------------------------------------------------------------------------------------------


def test_the_markers_endpoint_refuses_an_anonymous_caller(client):
    assert client.get("/api/map/markers").status_code == 401


async def test_a_pat_bearer_is_accepted(pat_client):
    bearer, _subject, _token = pat_client
    answered = await bearer.get("/api/map/markers")
    assert answered.status_code == 200
    assert answered.json() == {"markers": []}


def test_a_session_cookie_is_accepted(logged_in_client):
    client, _ = logged_in_client
    answered = client.get("/api/map/markers")
    assert answered.status_code == 200
    assert answered.json()["markers"] == []


# --------------------------------------------------------------------------------------------
# Which photos become markers
# --------------------------------------------------------------------------------------------


async def test_a_located_ready_attachment_of_a_visible_memo_is_a_marker(adb):
    from app.services import map_markers

    owner = await _member(adb, "viewer")
    memo = await _memo(adb, owner)
    taken = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    await _located(adb, memo, uid="m1", capture_instant=taken)

    rows = await map_markers.located_markers(adb, owner)
    assert [(att.uid, memo_uid) for att, memo_uid, _ in rows] == [("m1", memo.uid)]
    assert rows[0][2] == taken


@pytest.mark.parametrize(
    ("case", "kwargs", "foreign"),
    [
        ("archived memo", {"archived": True}, False),
        ("another member's PRIVATE memo", {"visibility": Visibility.PRIVATE}, True),
    ],
)
async def test_a_hidden_memos_photo_contributes_no_marker(adb, case, kwargs, foreign):
    from app.services import map_markers

    viewer = await _member(adb, "viewer")
    owner = await _member(adb, "someone-else") if foreign else viewer
    memo = await _memo(adb, owner, **kwargs)
    await _located(adb, memo, uid="hidden1")

    assert await map_markers.located_markers(adb, viewer) == [], case


async def test_a_non_ready_attachment_contributes_no_marker(adb):
    from app.services import map_markers

    owner = await _member(adb, "viewer")
    memo = await _memo(adb, owner)
    await _located(adb, memo, uid="pending1", status=AttachmentStatus.processing)

    assert await map_markers.located_markers(adb, owner) == []


async def test_an_attachment_without_coordinates_contributes_no_marker(adb):
    from app.services import map_markers

    owner = await _member(adb, "viewer")
    memo = await _memo(adb, owner)
    await _located(adb, memo, uid="nogps1", latitude=None, longitude=None)

    assert await map_markers.located_markers(adb, owner) == []


async def test_a_members_own_private_memo_is_their_own_marker(adb):
    from app.services import map_markers

    owner = await _member(adb, "viewer")
    memo = await _memo(adb, owner, visibility=Visibility.PRIVATE)
    await _located(adb, memo, uid="mine1")

    assert [att.uid for att, _, _ in await map_markers.located_markers(adb, owner)] == ["mine1"]


async def test_another_members_protected_memo_is_a_marker(adb):
    # The positive control for the PRIVATE case above: the feed rule shows a member everything
    # that is not someone else's PRIVATE memo, and the map must not be stricter than the feed.
    from app.services import map_markers

    viewer = await _member(adb, "viewer")
    owner = await _member(adb, "someone-else")
    memo = await _memo(adb, owner, visibility=Visibility.PROTECTED)
    await _located(adb, memo, uid="shared1")

    assert [att.uid for att, _, _ in await map_markers.located_markers(adb, viewer)] == ["shared1"]


async def test_taken_at_falls_back_through_the_chain_and_orders_ascending(adb):
    # `captured_at` is nullable and the archive importer creates rows with NEITHER capture field,
    # so the chain has to end in a NOT NULL column. No stored date is ever rewritten to make that
    # true — the fallback is an expression, not an UPDATE.
    from app.services import map_markers

    owner = await _member(adb, "viewer")
    memo = await _memo(adb, owner)
    base = datetime(2026, 3, 1, tzinfo=UTC)
    await _located(adb, memo, uid="third", capture_instant=base + timedelta(days=2))
    await _located(adb, memo, uid="second", captured_at=base + timedelta(days=1))
    imported = await _located(adb, memo, uid="first")  # neither capture field: created_at wins

    rows = await map_markers.located_markers(adb, owner)
    # Inserted third/second/first, answered oldest-first — so the order is the taken date's, not
    # the row order's. The imported row lands last because the end of its chain is its UPLOAD time.
    assert [att.uid for att, _, _ in rows] == ["second", "third", "first"]
    assert [taken for _, _, taken in rows[:2]] == [
        base + timedelta(days=1),
        base + timedelta(days=2),
    ]
    assert rows[2][2] == imported.created_at
    assert all(taken is not None for _, _, taken in rows)


# --------------------------------------------------------------------------------------------
# What the body looks like
# --------------------------------------------------------------------------------------------


def test_the_served_marker_carries_the_ref_fields_and_a_signed_thumb(logged_in_client):
    client, subject = logged_in_client
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            owner_id = c.execute(
                text("SELECT id FROM users WHERE subject = :s"), {"s": subject}
            ).scalar_one()
            memo_id = c.execute(
                text(
                    "INSERT INTO memos"
                    " (uid, owner_id, content, visibility, pinned, created_at, updated_at)"
                    " VALUES ('memo-served', :o, 'somewhere', 'PUBLIC', false, now(), now())"
                    " RETURNING id"
                ),
                {"o": owner_id},
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO attachments"
                    " (uid, owner_id, memo_id, filename, mime, size, status, storage_path,"
                    "  latitude, longitude, location_source, capture_instant)"
                    " VALUES ('served1', :o, :m, 's.jpg', 'image/jpeg', 1, 'ready', '/nowhere',"
                    "  12.5, 45.25, 'exif', '2026-05-06T00:00:00+00:00')"
                ),
                {"o": owner_id, "m": memo_id},
            )

        answered = client.get("/api/map/markers")
        assert answered.status_code == 200
        [marker] = answered.json()["markers"]
        assert marker["uid"] == "served1"
        assert marker["memo_uid"] == "memo-served"
        assert (marker["latitude"], marker["longitude"]) == (12.5, 45.25)
        assert marker["taken_at"] == "2026-05-06T00:00:00Z"
        assert marker["mime"] == "image/jpeg"
        assert marker["thumbnailUrl"].startswith("/api/attachments/served1/thumb?")
        assert "sig=" in marker["thumbnailUrl"]
    finally:
        eng.dispose()
        _wipe()


def test_the_generated_schema_describes_the_endpoint_and_the_marker():
    # The frontend's types are generated from this file; a route added without regenerating it is
    # a contract the SPA cannot see.
    schema = json.loads(OPENAPI.read_text())
    assert "/api/map/markers" in schema["paths"]
    marker = schema["components"]["schemas"]["MapMarker"]["properties"]
    assert {"memo_uid", "latitude", "longitude", "taken_at"} <= set(marker)
    # ...and the coordinate is on the MARKER alone: the feed's ref must not have grown one.
    assert "latitude" not in schema["components"]["schemas"]["AttachmentRef"]["properties"]
