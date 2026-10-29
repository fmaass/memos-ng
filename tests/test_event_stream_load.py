"""MEMNG-964's kill criterion, measured: 200 subscribers, 50 concurrent status emitters.

Skipped by the default run (it holds ~250 coroutines and a few thousand commits against the test
database for a while); run it with `MEMNG964_LOAD=1 uv run pytest -q -s -m load`.

Latency is from just before an emitter's COMMIT to the first frame a subscriber receives that
reflects that write (frames coalesce, so a later write's frame covers an earlier one). Each write
stamps its index into `width`, which the ready frame carries. Replay drops are the frames a peer
that stayed connected received after a reconnecting subscriber's Last-Event-ID but the replay did
not deliver.
"""

import asyncio
import json
import os
import statistics
import time
from typing import Any

import pytest
from app.database import async_session
from app.models import Attachment
from app.routers.assets import _ready_meta, _user_snapshot, media_event_stream
from app.services import event_stream as es
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

pytestmark = [
    pytest.mark.load,
    pytest.mark.usefixtures("db_engine"),
    pytest.mark.skipif(not os.environ.get("MEMNG964_LOAD"), reason="load test: MEMNG964_LOAD=1"),
]

OWNERS = 10
SUBSCRIBERS = 200
EMITTERS = 50
WRITES = 20
REPLAY_WRITES = 10
P95_LIMIT = 2.0


def _sql(stmt: str, **params) -> Any:
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            res = c.execute(text(stmt), params)
            return res.scalars().all() if res.returns_rows else None
    finally:
        eng.dispose()


class Sub:
    def __init__(self, hub: es.EventHub, owner: int, last_event_id: str | None = None) -> None:
        self.owner = owner
        self.frames: list[tuple[str, str, int, float]] = []  # (id, uid, width, at)
        self.last_id: str | None = None

        async def snap():
            return await _user_snapshot(owner)

        async def meta(uids):
            return await _ready_meta(owner, uids)

        self.gen = media_event_stream(
            hub, owner, snap, meta, last_event_id=last_event_id, keepalive=0.5
        )
        self.idle = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        async for chunk in self.gen:
            if chunk.startswith(":"):
                self.idle.set()
                continue
            fields = dict(
                line.partition(": ")[::2] for line in chunk.strip("\n").split("\n") if line
            )
            if "data" not in fields:
                continue
            data = json.loads(fields["data"])
            self.last_id = fields["id"]
            self.frames.append(
                (fields["id"], data["uid"], data.get("width") or 0, time.monotonic())
            )


async def _emit(uid: str, first: int, count: int, stamps: dict) -> None:
    for k in range(first, first + count):
        async with async_session() as db:
            att = (await db.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one()
            att.width = k
            await es.notify_attachment_status(db, att.uid, att.owner_id)
            stamps[(uid, k)] = time.monotonic()
            await db.commit()


async def test_load_200_subscribers_50_emitters():
    owners = _sql(
        "INSERT INTO users (subject, role) SELECT 'load-' || g, 'user' "
        "FROM generate_series(1, :n) g RETURNING id",
        n=OWNERS,
    )
    uids = [f"load-{i}" for i in range(EMITTERS)]
    owner_of = {uid: owners[i % OWNERS] for i, uid in enumerate(uids)}
    for uid in uids:
        _sql(
            "INSERT INTO attachments (uid, owner_id, filename, mime, size, status, storage_path,"
            " width) VALUES (:u,:o,'x.jpg','image/jpeg',1,'ready','/x',0)",
            u=uid,
            o=owner_of[uid],
        )
    hub = es.EventHub(es._listener_dsn(), es.fetch_status_frames)
    await hub.start()
    subs: list[Sub] = []
    try:
        assert await hub.wait_connected(10)
        # Opened in batches: each opening reads a snapshot, and the test engine has no pool to
        # queue 200 of them behind, while the dev server allows 100 connections in all.
        subs = [Sub(hub, owners[i % OWNERS]) for i in range(SUBSCRIBERS)]
        for at in range(0, SUBSCRIBERS, 20):
            batch = subs[at : at + 20]
            for s in batch:
                s.task = asyncio.create_task(s.run())
            await asyncio.gather(*(s.idle.wait() for s in batch))

        stamps: dict[tuple[str, int], float] = {}
        started = time.monotonic()
        await asyncio.gather(*(_emit(uid, 1, WRITES, stamps) for uid in uids))
        emit_seconds = time.monotonic() - started
        await asyncio.sleep(2.0)  # the tail of the fan-out

        latencies: list[float] = []
        live_missing = 0
        for s in subs:
            mine = [uid for uid in uids if owner_of[uid] == s.owner]
            for uid in mine:
                arrivals = [(w, at) for _, u, w, at in s.frames if u == uid]
                for k in range(1, WRITES + 1):
                    first = next((at for w, at in arrivals if w >= k), None)
                    if first is None:
                        live_missing += 1
                    else:
                        latencies.append(max(0.0, first - stamps[(uid, k)]))

        # Replay: half the subscribers go away, a second burst lands, they come back.
        leaving = subs[: SUBSCRIBERS // 2]
        staying = {s.owner: s for s in subs[SUBSCRIBERS // 2 :]}
        cut = {}
        for s in leaving:
            assert s.task is not None
            s.task.cancel()
            await asyncio.gather(s.task, return_exceptions=True)
            await s.gen.aclose()
            cut[id(s)] = s.last_id
        marks = {o: len(staying[o].frames) for o in staying}
        await asyncio.gather(*(_emit(uid, WRITES + 1, REPLAY_WRITES, stamps) for uid in uids))
        await asyncio.sleep(2.0)

        replay_drops = 0
        replayed_total = 0
        for s in leaving:
            back = Sub(hub, s.owner, last_event_id=cut[id(s)])
            back.task = asyncio.create_task(back.run())
            await asyncio.wait_for(back.idle.wait(), 10)
            back.task.cancel()
            await asyncio.gather(back.task, return_exceptions=True)
            await back.gen.aclose()
            peer = staying[s.owner]
            cut_seq = int((cut[id(s)] or "x.0").rpartition(".")[2])
            expected = [
                f[0]
                for f in peer.frames[marks[s.owner] :]
                if int(f[0].rpartition(".")[2]) > cut_seq
            ]
            got = [f[0] for f in back.frames]
            replayed_total += len(got)
            replay_drops += len([fid for fid in expected if fid not in got])
            assert got == sorted(got, key=lambda i: int(i.rpartition(".")[2]))

        latencies.sort()
        p95 = latencies[int(0.95 * (len(latencies) - 1))]
        print(
            f"\nLOAD subscribers={SUBSCRIBERS} emitters={EMITTERS} writes={EMITTERS * WRITES}"
            f" emit_s={emit_seconds:.2f} frames_published={hub.head}"
            f" samples={len(latencies)} p50={statistics.median(latencies) * 1000:.1f}ms"
            f" p95={p95 * 1000:.1f}ms max={latencies[-1] * 1000:.1f}ms"
            f" live_missing={live_missing} replayed={replayed_total}"
            f" replay_drops={replay_drops}"
        )
        assert live_missing == 0
        assert replay_drops == 0
        assert p95 < P95_LIMIT
    finally:
        for s in subs:
            if s.task is not None:
                s.task.cancel()
        await asyncio.gather(*(s.task for s in subs if s.task), return_exceptions=True)
        await hub.stop()
        _sql("DELETE FROM attachments WHERE uid LIKE 'load-%'")
        _sql("DELETE FROM users WHERE subject LIKE 'load-%'")
