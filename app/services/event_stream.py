"""The `/api/events` transport: Postgres LISTEN/NOTIFY fanned out in-process (MEMNG-964).

A status write and its NOTIFY share one transaction, so a rolled-back write announces nothing and a
committed one is announced only once it is visible. The payload carries ids only (NOTIFY caps at
8000 bytes and the row is the authority), and the hub reads the row back once per notification
for every subscriber of that owner rather than once per subscriber.

One listener connection per process, outside the SQLAlchemy pool: a pooled connection would be
handed to a request between LISTENs, and a held one would cost the bounded prod pool a slot for
the life of the process. Two processes mean two listeners, each serving its own subscribers.
"""

import asyncio
import contextlib
import json
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobState, MediaJob, MediaJobProgress

logger = structlog.get_logger()

ATTACHMENT_STATUS = "attachment_status"
UPLOAD_PROGRESS = "upload_progress"
MEMO_CHANGED = "memo_changed"
CHANNELS = (ATTACHMENT_STATUS, UPLOAD_PROGRESS, MEMO_CHANGED)

KEEPALIVE_SECONDS = 25.0
RETRY_MS = 3000
RING_SECONDS = 300.0
# A count cap on top of the time window, so a burst cannot grow the ring without bound; evicting by
# count raises the replay floor exactly as expiry does, and a client below it gets the snapshot.
RING_MAX = 20_000
QUEUE_MAX = 512
HEALTH_INTERVAL = 10.0
HEALTH_TIMEOUT = 5.0
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 30.0


# --- emitters: called inside the transaction that writes the status ---


async def notify_attachment_status(db: AsyncSession, uid: str, owner_id: int | None) -> None:
    if owner_id is None:
        return  # an owner-less row has no subscriber: the stream is per user
    payload = json.dumps({"uid": uid, "owner_id": owner_id})
    await db.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": ATTACHMENT_STATUS, "p": payload})


async def notify_attachment_status_for_job(db: AsyncSession, job_id: int) -> None:
    """The progress sample's announcement. The sample is written in a session that holds no
    attachment, so the ids are resolved in SQL — a plain read, which the worker's FOR UPDATE on the
    job row does not block."""
    await db.execute(
        text(
            "SELECT pg_notify(:ch, json_build_object('uid', a.uid, 'owner_id', a.owner_id)::text) "
            "FROM media_jobs j JOIN attachments a ON a.id = j.attachment_id WHERE j.id = :jid"
        ),
        {"ch": ATTACHMENT_STATUS, "jid": job_id},
    )


# --- the row read-back ---


def active_progress_subquery():
    """Per attachment, the least-advanced unfinished job's live percentage (see _user_snapshot)."""
    return (
        select(
            MediaJob.attachment_id.label("attachment_id"),
            func.min(func.coalesce(MediaJobProgress.progress_pct, MediaJob.progress_pct)).label(
                "pct"
            ),
        )
        .join(MediaJobProgress, MediaJobProgress.job_id == MediaJob.id, isouter=True)
        .where(MediaJob.state.notin_((JobState.done, JobState.failed)))
        .group_by(MediaJob.attachment_id)
        .subquery()
    )


def status_payload(
    uid: str,
    status: AttachmentStatus,
    pct: int,
    width: int | None,
    height: int | None,
    blurhash: str | None,
) -> dict:
    """The exact frame bodies the poll stream sent: the snapshot row for an in-flight or failed
    attachment, the ready event with its layout metadata for a finished one."""
    if status == AttachmentStatus.ready:
        return {
            "uid": uid,
            "status": "ready",
            "progress_pct": 100,
            "width": width,
            "height": height,
            "blurhash": blurhash,
        }
    return {"uid": uid, "status": status.value, "progress_pct": pct}


async def fetch_status_frames(keys: list[tuple[str, int]]) -> list[tuple[int, dict]]:
    """(owner_id, payload) per notified (uid, owner_id), in notification order.

    Matched on uid AND owner: the payload names the owner the fan-out routes on, and a row that
    does not belong to that owner yields nothing rather than reaching the wrong subscribers. A row
    deleted since its NOTIFY yields nothing either.
    """
    if not keys:
        return []
    active = active_progress_subquery()
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    Attachment.uid,
                    Attachment.owner_id,
                    Attachment.status,
                    func.coalesce(active.c.pct, 0),
                    Attachment.width,
                    Attachment.height,
                    Attachment.blurhash,
                )
                .join(active, active.c.attachment_id == Attachment.id, isouter=True)
                .where(Attachment.uid.in_({uid for uid, _ in keys}))
            )
        ).all()
    by_key = {(r[0], r[1]): r for r in rows}
    out: list[tuple[int, dict]] = []
    for key in keys:
        row = by_key.get(key)
        if row is not None:
            uid, owner_id, status, pct, w, h, b = row
            out.append((owner_id, status_payload(uid, status, pct, w, h, b)))
    return out


# --- the hub ---


@dataclass(frozen=True)
class Frame:
    seq: int
    owner_id: int
    payload: dict
    at: float


class Resync:
    """Frames may have been lost (the listener reconnected or a read-back failed): resend state."""


class Close:
    """This subscriber overflowed its queue; ending the response makes the client reconnect and
    replay from the ring, which the overflow never touched."""


RESYNC = Resync()
CLOSE = Close()

type Item = Frame | Resync | Close
type Fetch = Callable[[list[tuple[str, int]]], Awaitable[list[tuple[int, dict]]]]
type Connect = Callable[[str], Awaitable[Any]]


@dataclass(eq=False)
class Subscription:
    owner_id: int
    queue: asyncio.Queue[Item]
    replay: list[Frame] | None = field(default=None)


def _listener_dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _asyncpg_connect(dsn: str) -> Any:
    return await asyncpg.connect(dsn)


class EventHub:
    def __init__(
        self,
        dsn: str,
        fetch: Fetch,
        *,
        connect: Connect = _asyncpg_connect,
        ring_seconds: float = RING_SECONDS,
        ring_max: int = RING_MAX,
        queue_max: int = QUEUE_MAX,
        health_interval: float = HEALTH_INTERVAL,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
    ) -> None:
        self._dsn = dsn
        self._fetch = fetch
        self._connect = connect
        self._ring_seconds = ring_seconds
        self._ring_max = ring_max
        self._queue_max = queue_max
        self._health_interval = health_interval
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        # Per boot, so an id from before a restart can never be mistaken for one of this run's
        # seqs: the time makes it readable, the random tail separates two processes started in the
        # same millisecond.
        self.epoch = f"{time.time_ns() // 1_000_000:x}{secrets.token_hex(2)}"
        self._seq = 0
        # Every frame with seq > _floor is in the ring; an id below it may have missed one.
        self._floor = 0
        self._ring: deque[Frame] = deque()
        self._subs: dict[int, set[Subscription]] = {}
        self._inbox: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._connected = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._conn: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.listener_pid: int | None = None
        self.connects = 0

    # -- ids --

    @property
    def head(self) -> int:
        return self._seq

    def event_id(self, seq: int) -> str:
        return f"{self.epoch}.{seq}"

    def _replay_from(self, last_event_id: str | None) -> int | None:
        if not last_event_id:
            return None
        epoch, _, seq_text = last_event_id.strip().rpartition(".")
        if epoch != self.epoch or not seq_text.isdigit():
            return None
        seq = int(seq_text)
        if seq < self._floor or seq > self._seq:
            return None
        return seq

    # -- subscribers --

    def subscribe(self, owner_id: int, last_event_id: str | None = None) -> Subscription:
        """Register before anything is read, so a frame published while the caller builds its
        snapshot lands in the queue rather than in the gap. `replay` is None when the caller must
        send a snapshot instead: no id, another boot's id, or one older than the ring."""
        self._evict()
        sub = Subscription(owner_id, asyncio.Queue(self._queue_max))
        since = self._replay_from(last_event_id)
        if since is not None:
            sub.replay = [f for f in self._ring if f.seq > since and f.owner_id == owner_id]
        self._subs.setdefault(owner_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        owned = self._subs.get(sub.owner_id)
        if owned is not None:
            owned.discard(sub)
            if not owned:
                del self._subs[sub.owner_id]

    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subs.values())

    def _deliver(self, sub: Subscription, item: Item) -> None:
        try:
            sub.queue.put_nowait(item)
        except asyncio.QueueFull:
            # Never wait on one reader: the fan-out loop serves every subscriber of the process.
            self.unsubscribe(sub)
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.queue.put_nowait(CLOSE)
            logger.warning("events_subscriber_overflow", owner_id=sub.owner_id)

    def publish(self, owner_id: int, payload: dict) -> Frame:
        self._seq += 1
        frame = Frame(self._seq, owner_id, payload, time.monotonic())
        self._ring.append(frame)
        self._evict()
        for sub in list(self._subs.get(owner_id, ())):
            self._deliver(sub, frame)
        return frame

    def resync_all(self) -> None:
        # Whatever was lost may have been the only frame of an id a client already holds, so
        # every id up to now stops being replayable.
        self._floor = self._seq
        for owned in list(self._subs.values()):
            for sub in list(owned):
                self._deliver(sub, RESYNC)

    def _evict(self) -> None:
        cutoff = time.monotonic() - self._ring_seconds
        while self._ring and (self._ring[0].at < cutoff or len(self._ring) > self._ring_max):
            self._floor = self._ring.popleft().seq

    # -- lifecycle --

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._tasks = [
            asyncio.create_task(self._listen(), name="events-listener"),
            asyncio.create_task(self._dispatch(), name="events-dispatch"),
        ]

    async def wait_connected(self, within: float) -> bool:
        try:
            async with asyncio.timeout(within):
                await self._connected.wait()
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        await self._close_conn()

    def abandon(self) -> None:
        """Drop a hub whose event loop is gone (each test client request runs its own loop): its
        tasks can no longer run, so only the socket is released, synchronously."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.terminate()

    async def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        self._connected.clear()
        if conn is not None:
            try:
                async with asyncio.timeout(HEALTH_TIMEOUT):
                    await conn.close()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.terminate()

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        self._inbox.put_nowait((channel, payload))

    async def _listen(self) -> None:
        delay = self._backoff_initial
        while True:
            lost = asyncio.Event()
            try:
                conn = await self._connect(self._dsn)
                self._conn = conn
                conn.add_termination_listener(lambda _c, ev=lost: ev.set())
                for channel in CHANNELS:
                    await conn.add_listener(channel, self._on_notify)
                self.listener_pid = conn.get_server_pid()
                self.connects += 1
                self._connected.set()
                # Every connect, the first included: a subscriber registered while there was no
                # LISTEN may have built its snapshot before a NOTIFY nobody heard.
                self.resync_all()
                delay = self._backoff_initial
                logger.info("events_listener_connected", pid=self.listener_pid)
                while not lost.is_set():
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(self._health_interval):
                            await lost.wait()
                    if lost.is_set():
                        break
                    # A half-open TCP connection reports nothing until something is sent on it.
                    async with asyncio.timeout(HEALTH_TIMEOUT):
                        await conn.fetchval("SELECT 1")
                logger.warning("events_listener_lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("events_listener_failed", error=str(exc), retry_in=delay)
            await self._close_conn()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)

    async def _dispatch(self) -> None:
        while True:
            batch = [await self._inbox.get()]
            while not self._inbox.empty():
                batch.append(self._inbox.get_nowait())
            keys: list[tuple[str, int]] = []
            for channel, raw in batch:
                if channel != ATTACHMENT_STATUS:
                    continue  # the upload_progress / memo_changed emitters arrive with MEMNG-965
                try:
                    body = json.loads(raw)
                    key = (str(body["uid"]), int(body["owner_id"]))
                except (ValueError, KeyError, TypeError):
                    logger.warning("events_bad_payload", channel=channel)
                    continue
                if key not in keys:
                    keys.append(key)
            if not keys:
                continue
            try:
                frames = await self._fetch(keys)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("events_fetch_failed", error=str(exc))
                self.resync_all()
                continue
            for owner_id, payload in frames:
                self.publish(owner_id, payload)


_hub: EventHub | None = None


async def get_hub() -> EventHub:
    """The process's hub, started on first use and replaced if it belongs to a dead event loop."""
    global _hub
    loop = asyncio.get_running_loop()
    if _hub is None or _hub.loop is not loop:
        if _hub is not None:
            _hub.abandon()
        _hub = EventHub(_listener_dsn(), fetch_status_frames)
        await _hub.start()
    return _hub


async def shutdown_hub() -> None:
    global _hub
    hub, _hub = _hub, None
    if hub is not None and hub.loop is asyncio.get_running_loop():
        await hub.stop()
