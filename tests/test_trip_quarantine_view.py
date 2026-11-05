"""The owner's resolve, the unknown-pass count and the door's suspended view (MEMNG-1045, 1082)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.database import async_session
from app.models import Pass, PassEvent, User
from app.routers import trip_ingest as router
from app.services import passes as passes_svc
from app.services import trip_ingest, trip_ingest_metrics
from fastapi import HTTPException
from sqlalchemy import select
from starlette.routing import Match

from tests import test_trip_ingest_puller as tp
from tests.ingest_support import JPEG

pluto_schema = tp.pluto_schema
world = tp.world


async def _owner(w) -> User:
    async with async_session() as db:
        owner = await db.get(User, w.owner_id)
        assert owner is not None
        return owner


async def _quarantined(w) -> str:
    """A real batch through the door and refused at home, its quarantine delivered or owed."""
    trip = await tp._trip(w)
    token = await tp._guest(w, trip)
    uid = await tp._send(w, token, (JPEG, "a.jpg"))
    real = trip_ingest.file_open
    trip_ingest.file_open = tp._no_filing
    try:
        assert await tp._tick(w) == "ok"
    finally:
        trip_ingest.file_open = real
    [(batch_id,)] = tp._sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": uid})
    await trip_ingest._refuse_open(batch_id, "bad bytes")
    return uid


async def _gauge() -> int:
    return (await trip_ingest_metrics.read_state()).quarantined


async def _resolve(uid: str, owner: User) -> None:
    async with async_session() as db:
        await router.resolve_batch(uid, owner, db)


async def test_resolve_drops_the_gauge_keeps_the_state_and_survives_a_refed_page(world):
    uid = await _quarantined(world)
    owner = await _owner(world)
    assert await _gauge() == 1
    await _resolve(uid, owner)
    assert await _gauge() == 0
    assert tp._sql("SELECT state, resolved_by FROM ingest_batches WHERE uid = :u", {"u": uid}) == [
        ("quarantined", owner.id)
    ]
    assert await tp._tick(world) == "ok"  # the feed answers this batch again: nothing changes
    assert await _gauge() == 0
    async with async_session() as db:
        assert (await router.list_quarantine(owner, db)).batches == []


async def test_resolve_during_an_outage_still_delivers_the_quarantine(world, monkeypatch):
    calls: list[str] = []
    real = trip_ingest._push_quarantine

    async def down(client, uid):
        calls.append(uid)
        raise trip_ingest.LinkDown("link is down")

    uid = await _quarantined_undelivered(world, monkeypatch, down)
    await _resolve(uid, await _owner(world))
    monkeypatch.setattr(trip_ingest, "_push_quarantine", real)
    assert await tp._tick(world) == "ok"
    assert tp._pluto_batch(uid)[0][0] == "quarantined"


async def _quarantined_undelivered(w, monkeypatch, down) -> str:
    monkeypatch.setattr(trip_ingest, "_push_quarantine", down)
    return await _quarantined(w)


async def test_resolving_what_is_not_quarantined_is_a_404(world):
    with pytest.raises(HTTPException) as err:
        await _resolve("nope", await _owner(world))
    assert err.value.status_code == 404


async def test_a_batch_the_door_named_unknown_resolves_as_a_batch_not_as_the_counter(world):
    """The uid is door-chosen, and the literal route shadows `{batch_uid}` for this one name, so a
    bare POST there is the batch; only a `through` is the counter. Unhandled, its alert stays."""
    owner = await _owner(world)
    uid = await _quarantined(world)
    tp._sql("UPDATE ingest_batches SET uid = 'unknown' WHERE uid = :u", {"u": uid})
    tp._sql("UPDATE trip_ingest_cursor SET quarantined_unknown_total = 2")
    assert await _gauge() == 3
    path = "/api/trip-ingest/quarantine/unknown/resolve"
    first = next(
        r
        for r in router.router.routes
        if r.matches({"type": "http", "path": path, "method": "POST"})[0] == Match.FULL  # type: ignore[union-attr]
    )
    assert first.endpoint is router.resolve_unknown  # type: ignore[union-attr]
    async with async_session() as db:
        await router.resolve_unknown(owner, db, router.UnknownResolveIn(through=2))
    assert await _gauge() == 1, "a through must clear only the counter"
    async with async_session() as db:
        await router.resolve_unknown(owner, db, router.UnknownResolveIn())
    assert await _gauge() == 0


async def _bare_cursor() -> None:
    async with async_session() as db:
        await trip_ingest._cursor(db)
        await db.commit()


async def test_an_unknown_pass_is_counted_even_when_its_page_fails_and_replays(world, monkeypatch):
    """The real feed, twice: the page dies after the door moved, so the cursor stays and the replay
    reads the batch as settled and returns early. Only the count taken beside the page survives."""
    trip = await tp._trip(world)
    batch = await tp._send(world, await tp._guest(world, trip), (JPEG, "a.jpg"))
    tp._sql("UPDATE pluto.passes SET uid = 'not-a-home-pass'")
    real = trip_ingest._record_batch
    died = 0

    async def dies_after_the_entry(db, client, raw, errors):
        nonlocal died
        await real(db, client, raw, errors)
        died += 1
        raise trip_ingest.LinkDown("the page died")

    monkeypatch.setattr(trip_ingest, "_record_batch", dies_after_the_entry)
    assert await tp._tick(world) != "ok"
    assert died == 1
    assert tp._pluto_batch(batch) == [("quarantined", False)]
    assert tp._sql("SELECT quarantined_unknown_total FROM trip_ingest_cursor") == [(1,)]
    [(since,)] = tp._sql("SELECT batches_since FROM trip_ingest_cursor")
    monkeypatch.setattr(trip_ingest, "_record_batch", real)
    assert await tp._tick(world) == "ok"
    [(replayed,)] = tp._sql("SELECT batches_since FROM trip_ingest_cursor")
    assert replayed > since, "the page never replayed"
    assert tp._sql("SELECT quarantined_unknown_total FROM trip_ingest_cursor") == [(1,)]
    assert await _gauge() == 1


async def test_a_stale_through_cannot_clear_a_newer_incident(world):
    await _bare_cursor()
    tp._sql("UPDATE trip_ingest_cursor SET quarantined_unknown_total = 3 RETURNING id")
    owner = await _owner(world)
    async with async_session() as db:
        await router.resolve_unknown(owner, db, router.UnknownResolveIn(through=2))
    assert await _gauge() == 1
    async with async_session() as db:
        await router.resolve_unknown(owner, db, router.UnknownResolveIn(through=99))
    assert tp._sql("SELECT quarantined_unknown_cleared FROM trip_ingest_cursor") == [(3,)]
    async with async_session() as db:
        await router.resolve_unknown(owner, db, router.UnknownResolveIn(through=1))
    assert tp._sql("SELECT quarantined_unknown_cleared FROM trip_ingest_cursor") == [(3,)]
    assert await _gauge() == 0


async def _view_row(pass_uid: str):
    return tp._sql(
        "SELECT s.door_suspended_at IS NOT NULL, s.door_suspended_seen_at IS NOT NULL "
        "FROM pass_sync_state s JOIN passes p ON p.id = s.pass_id WHERE p.uid = :u",
        {"u": pass_uid},
    )


def _door(status: int, body: object) -> httpx.AsyncClient:
    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(body).encode()),
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://door")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"detail": "Not Found"}),
        (200, {"passes": "no"}),
        (200, {"passes": [{"pass_uid": "../x", "suspended_at": "2026-11-04T00:00:00Z"}]}),
        (200, {"passes": [{"pass_uid": "p", "suspended_at": "not a time"}]}),
        (200, {"passes": [{"pass_uid": "p", "suspended_at": None}]}),
        (
            200,
            {
                "passes": [
                    {"pass_uid": f"p{i}", "suspended_at": "2026-11-04T00:00:00Z"}
                    for i in range(501)
                ]
            },
        ),
    ],
    ids=["old_door", "not_a_list", "bad_uid", "bad_time", "null_time", "over_cap"],
)
async def test_an_unusable_door_answer_leaves_the_view_unknown(world, status, body):
    trip = await tp._trip(world)
    tp._sql("DELETE FROM pass_sync_state")
    async with _door(status, body) as client:
        await trip_ingest.refresh_door_view(client)
    assert await _view_row(trip.pass_uid) == []


async def test_a_valid_snapshot_is_complete_clamped_and_drops_unknown_uids(world):
    trip = await tp._trip(world)
    other = await tp._trip(world, tag="other")
    future = (datetime.now(UTC) + timedelta(days=9)).isoformat()
    entries = [
        {"pass_uid": trip.pass_uid, "suspended_at": future},
        {"pass_uid": "never-issued", "suspended_at": future},
    ]
    async with _door(200, {"passes": entries}) as client:
        await trip_ingest.refresh_door_view(client)
    assert await _view_row(trip.pass_uid) == [(True, True)]
    assert await _view_row(other.pass_uid) == [(False, True)]
    [(at,)] = tp._sql(
        "SELECT s.door_suspended_at FROM pass_sync_state s JOIN passes p ON p.id = s.pass_id "
        "WHERE p.uid = :u",
        {"u": trip.pass_uid},
    )
    assert at <= datetime.now(UTC)
    assert tp._sql("SELECT count(*) FROM passes WHERE uid = 'never-issued'") == [(0,)]


async def test_the_door_route_lists_every_suspended_pass_and_nothing_else(world):
    trip = await tp._trip(world)
    await tp._guest(world, trip)
    assert await tp._tick(world) == "ok"
    answer = await world.link.get("/ingest/passes/suspended")
    assert answer.status_code == 200 and answer.json() == {"passes": []}
    tp._sql("UPDATE pluto.passes SET suspended_at = now() RETURNING id")
    answer = await world.link.get("/ingest/passes/suspended")
    assert [e["pass_uid"] for e in answer.json()["passes"]] == [trip.pass_uid]


async def test_both_lift_forms_read_as_a_pending_lift(world):
    trip = await tp._trip(world)
    async with async_session() as db:
        p = (await db.execute(select(Pass).where(Pass.uid == trip.pass_uid))).scalar_one()
        db.add(PassEvent(pass_id=p.id, kind="unsuspend_requested", version=7))
        await db.commit()
        assert (await passes_svc.unsuspend_versions(db, [p.id])) == {p.id: 7}


def _a_bad_door_answer(kind: str):
    async def rewrite(request, response):
        if request.url.path != "/ingest/passes/suspended":
            return response
        if kind == "old_door":
            return httpx.Response(
                404,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(b'{"detail": "Not Found"}'),
            )
        body: object = {"passes": "no"}
        if kind == "over_cap":
            at = "2026-11-04T00:00:00Z"
            body = {
                "passes": [
                    {"pass_uid": f"p{i}", "suspended_at": at}
                    for i in range(trip_ingest.SUSPENDED_VIEW_CAP + 1)
                ]
            }
        return tp._json_answer(body)

    return rewrite


@pytest.mark.parametrize("kind", ["old_door", "malformed", "over_cap", "link_down"])
async def test_a_full_tick_carries_on_through_a_bad_door_answer_and_keeps_the_earlier_view(
    world, kind
):
    trip = await tp._trip(world)
    token = await tp._guest(world, trip)
    await tp._send(world, token, (JPEG + b"1", "a.jpg"))
    tp._sql("UPDATE pluto.passes SET suspended_at = now()")
    assert await tp._tick(world) == "ok"
    view = "SELECT s.door_suspended_at, s.door_suspended_seen_at FROM pass_sync_state s"
    [(suspended, seen)] = tp._sql(view)
    assert suspended is not None and seen is not None, "the earlier view never landed"
    assert len(tp._memos()) == 1
    tp._sql("UPDATE pluto.passes SET suspended_at = NULL")
    await tp._send(world, token, (JPEG + b"2", "b.jpg"))
    if kind == "link_down":
        world.wire.down_paths = ("/ingest/passes/suspended",)
    else:
        world.wire.rewrite = _a_bad_door_answer(kind)
    assert await tp._tick(world) == "ok"
    assert len(tp._memos()) == 2, "the bad door answer stopped ingestion"
    assert tp._sql(view) == [(suspended, seen)]
    world.wire.down_paths, world.wire.rewrite = (), None
    assert await tp._tick(world) == "ok"
    [(cleared, again)] = tp._sql(view)
    assert cleared is None and again > seen, "control: a good answer moves the view"


def _an_unknown_named_batch() -> None:
    tp._sql(
        "INSERT INTO passes (uid, secret_hash, label, expires_at) "
        "VALUES ('resolve-pass', 'h', 'l', now() + interval '1 day')"
    )
    for uid in ("unknown", "ordinary"):
        tp._sql(
            "INSERT INTO ingest_batches (uid, pass_id, state, quarantined_at) "
            "SELECT :u, id, 'quarantined', now() FROM passes WHERE uid = 'resolve-pass'",
            {"u": uid},
        )


def test_a_bodyless_post_resolves_a_batch_the_door_named_unknown(logged_in_client):
    """Through the HTTP client, because the sheet sends no body and a handler call cannot see a 422:
    a hostile door could name a batch so that the owner can never clear it."""
    client, _subject = logged_in_client
    _an_unknown_named_batch()
    base = "/api/trip-ingest/quarantine"
    try:
        assert client.post(f"{base}/ordinary/resolve").status_code == 204
        answer = client.post(f"{base}/unknown/resolve")
        assert answer.status_code == 204, answer.text
        assert (
            tp._sql("SELECT uid FROM ingest_batches WHERE resolved_at IS NULL ORDER BY uid") == []
        )
        assert client.post(f"{base}/unknown/resolve", json={}).status_code == 204
    finally:
        tp._sql("DELETE FROM passes WHERE uid = 'resolve-pass'")
