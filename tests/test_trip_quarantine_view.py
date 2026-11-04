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


async def _bare_cursor() -> None:
    async with async_session() as db:
        await trip_ingest._cursor(db)
        await db.commit()


async def test_an_unknown_pass_is_counted_even_when_its_page_fails_and_replays(world, monkeypatch):
    await _bare_cursor()
    pushed: list[str] = []

    async def push(client, uid):
        pushed.append(uid)
        return True

    monkeypatch.setattr(trip_ingest, "_push_quarantine", push)
    raw = {"batch_id": "orphan1", "state": "complete", "pass_uid": "never-issued"}

    class PageFailed(Exception):
        pass

    with pytest.raises(PageFailed):
        async with async_session() as db:
            await trip_ingest._record_batch(db, world.link, raw, [])
            raise PageFailed  # the page's transaction dies after the door moved
    assert pushed == ["orphan1"]
    assert tp._sql("SELECT quarantined_unknown_total FROM trip_ingest_cursor") == [(1,)]
    assert await _gauge() == 1


async def test_a_stale_through_cannot_clear_a_newer_incident(world):
    await _bare_cursor()
    tp._sql("UPDATE trip_ingest_cursor SET quarantined_unknown_total = 3 RETURNING id")
    owner = await _owner(world)
    async with async_session() as db:
        await router.resolve_unknown(router.UnknownResolveIn(through=2), owner, db)
    assert await _gauge() == 1
    async with async_session() as db:
        await router.resolve_unknown(router.UnknownResolveIn(through=99), owner, db)
    assert tp._sql("SELECT quarantined_unknown_cleared FROM trip_ingest_cursor") == [(3,)]
    async with async_session() as db:
        await router.resolve_unknown(router.UnknownResolveIn(through=1), owner, db)
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
