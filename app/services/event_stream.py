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
import enum
import json
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import asyncpg
import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    JobState,
    MediaJob,
    MediaJobProgress,
    Visibility,
)

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
# Notifications waiting for the dispatcher while it reads rows back. Past it a notification is
# dropped and every subscriber is resynced, whose snapshot reads the row the dropped one named.
INBOX_MAX = 10_000
# The longest seq an id may carry: nineteen digits hold any int64, and nothing longer is parsed.
SEQ_DIGITS_MAX = 19
HEALTH_INTERVAL = 10.0
HEALTH_TIMEOUT = 5.0
# The listener close on the way out, before falling back to terminate. A stop is allowed 5 s in
# all, and docker-entrypoint.sh's response drain (3 s) comes first, so HEALTH_TIMEOUT does not fit
# here: a stalled connection held the stop to 3 + 5 s (MEMNG-1174).
STOP_CLOSE_TIMEOUT = 1.0
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 30.0
# upload.progress is coalesced to at most one frame per upload per this many seconds.
PROGRESS_INTERVAL = 1.0
# Uploads whose last frame is older than the interval are forgotten once the table grows past this.
PROGRESS_TRACKED_MAX = 1024

# The SSE `event:` names. Media-status frames keep the spec's default, which the shipped iOS builds
# and the web client's onmessage handle; every other kind takes a name of its own, which those
# clients ignore (see app/routers/assets.sse_event).
MESSAGE = "message"
UPLOAD_PROGRESS_EVENT = "upload.progress"
MEMO_CREATED = "memo.created"
MEMO_UPDATED = "memo.updated"
MEMO_DELETED = "memo.deleted"
MEMO_EVENTS = (MEMO_CREATED, MEMO_UPDATED, MEMO_DELETED)

type MemoChange = Literal["created", "updated", "deleted"]


class Audience(enum.StrEnum):
    """Who a frame is for, relative to the `owner_id` it carries. Every subscriber of the stream is
    a household member — `/api/events` authenticates with `require_user`, and a trip-pass guest is
    not a user — so the household is every subscriber, and nothing outside it can ever subscribe."""

    OWNER = "owner"  # the owner alone
    OTHERS = "others"  # every household member but the owner
    HOUSEHOLD = "household"  # every household member, the owner included

    def reaches(self, owner_id: int, user_id: int) -> bool:
        if self is Audience.OWNER:
            return user_id == owner_id
        if self is Audience.OTHERS:
            return user_id != owner_id
        return True


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


async def notify_upload_progress(
    db: AsyncSession, upload_uid: str, owner_id: int | None, bytes_received: int, declared_size: int
) -> None:
    """A committed append tail. Every one is announced; the hub coalesces (EventHub._admit_progress)
    because each listener process hears every NOTIFY, so the bound holds per subscriber however many
    processes take the appends."""
    if owner_id is None:
        return
    payload = json.dumps(
        {
            "upload_uid": upload_uid,
            "owner_id": owner_id,
            "bytes_received": bytes_received,
            "declared_size": declared_size,
        }
    )
    await db.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": UPLOAD_PROGRESS, "p": payload})


def memo_change_frames(
    change: MemoChange, before: Visibility | None, after: Visibility | None
) -> list[tuple[str, Audience]]:
    """The frames one memo write makes, by its visibility before and after — the rule
    `services.memos.visible_clause` reads with: a PRIVATE memo is its owner's alone, any other is
    the household's. A memo leaving a member's view is a delete for them, one entering it a
    create; the owner always sees the edit as the edit it is."""
    if change == "created":
        return [(MEMO_CREATED, _audience(after))]
    if change == "deleted":
        return [(MEMO_DELETED, _audience(before))]
    was_shared = before is not None and before != Visibility.PRIVATE
    is_shared = after is not None and after != Visibility.PRIVATE
    if was_shared == is_shared:
        return [(MEMO_UPDATED, Audience.HOUSEHOLD if is_shared else Audience.OWNER)]
    return [
        (MEMO_UPDATED, Audience.OWNER),
        (MEMO_CREATED if is_shared else MEMO_DELETED, Audience.OTHERS),
    ]


def _audience(visibility: Visibility | None) -> Audience:
    if visibility is None or visibility == Visibility.PRIVATE:
        return Audience.OWNER
    return Audience.HOUSEHOLD


async def notify_memo_changed(
    db: AsyncSession,
    change: MemoChange,
    uid: str,
    owner_id: int,
    *,
    before: Visibility | None,
    after: Visibility | None,
    guest: dict | None,
) -> None:
    """Called inside the transaction of the memo write, with the visibility the write found and the
    one it leaves: the recipients are decided here, at emission. Ids only, plus the guest flag
    (`MemoOut.guest`): the row is the authority, and a guest's name is bounded by its column."""
    for event, audience in memo_change_frames(change, before, after):
        payload = json.dumps(
            {
                "event": event,
                "uid": uid,
                "owner_id": owner_id,
                "audience": audience.value,
                "guest": guest,
            }
        )
        await db.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": MEMO_CHANGED, "p": payload})


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
    event: str = MESSAGE
    audience: Audience = Audience.OWNER

    def reaches(self, user_id: int) -> bool:
        return self.audience.reaches(self.owner_id, user_id)


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
        inbox_max: int = INBOX_MAX,
        health_interval: float = HEALTH_INTERVAL,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
        clock: Callable[[], float] = time.monotonic,
        progress_interval: float = PROGRESS_INTERVAL,
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
        self._clock = clock
        self._progress_interval = progress_interval
        # upload_uid -> when its last upload.progress frame went out (on `clock`).
        self._progress_sent: dict[str, float] = {}
        # owner_id -> {uid: when it was last remembered (on `clock`)}: the attachments a stream of
        # that owner was watching when it ended, or announced ready without knowing the client got
        # the frame. See remember_watched.
        self._watched: dict[int, dict[str, float]] = {}
        # Per boot, so an id from before a restart can never be mistaken for one of this run's
        # seqs: the time makes it readable, the random tail separates two processes started in the
        # same millisecond.
        self.epoch = f"{time.time_ns() // 1_000_000:x}{secrets.token_hex(2)}"
        self._seq = 0
        # Every frame with seq > _floor is in the ring; an id below it may have missed one. Eviction
        # moves it, and only eviction.
        self._floor = 0
        # An id at or below this may have missed a frame that never reached the ring (a resync:
        # the listener reconnected, a read-back failed, the inbox overflowed). Monotonic — a later
        # eviction must not bring an invalidated id back. Seq 0 names no frame and is at or below
        # it from birth, so `event_id(0)` is the id that always asks for a snapshot.
        self._invalid = 0
        self._ring: deque[Frame] = deque()
        self._subs: dict[int, set[Subscription]] = {}
        self._inbox: asyncio.Queue[tuple[str, str]] = asyncio.Queue(inbox_max)
        self._inbox_overflowed = False
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

    @property
    def unresumable_id(self) -> str:
        """An id of this boot that no replay accepts: the frames of a snapshot before its last
        carry it, so a client that drops partway through is sent the snapshot again."""
        return self.event_id(0)

    def _replay_from(self, last_event_id: str | None) -> int | None:
        if not last_event_id:
            return None
        epoch, _, seq_text = last_event_id.strip().rpartition(".")
        # ASCII digits only: str.isdigit() also says yes to '²' (which int() then refuses) and to
        # other scripts' digits (which int() reads), and a garbled header must mean "snapshot".
        if (
            epoch != self.epoch
            or not seq_text.isascii()
            or not seq_text.isdigit()
            or len(seq_text) > SEQ_DIGITS_MAX
        ):
            return None
        seq = int(seq_text)
        if seq <= self._invalid or seq < self._floor or seq > self._seq:
            return None
        return seq

    # -- subscribers --

    def subscribe(self, owner_id: int, last_event_id: str | None = None) -> Subscription:
        """Register before anything is read, so a frame published while the caller builds its
        snapshot lands in the queue rather than in the gap. `replay` is None when the caller must
        send a snapshot instead: no id, another boot's id, one older than the ring, or one a resync
        invalidated."""
        self._evict()
        sub = Subscription(owner_id, asyncio.Queue(self._queue_max))
        since = self._replay_from(last_event_id)
        if since is not None:
            sub.replay = [f for f in self._ring if f.seq > since and f.reaches(owner_id)]
        self._subs.setdefault(owner_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        owned = self._subs.get(sub.owner_id)
        if owned is not None:
            owned.discard(sub)
            if not owned:
                del self._subs[sub.owner_id]

    # -- watched attachments, across a reconnect (MEMNG-1174) --

    def remember_watched(self, owner_id: int, uids: set[str] | list[str]) -> None:
        """Keep `uids` for the owner's next snapshot, for the ring's window.

        A ready event the stream synthesises from an absence (a watched uid that dropped out of
        the snapshot) is rebuilt from nothing but that stream's own watched set, and a reconnect
        starts with an empty one. So a client cut off before the frame arrived — before the
        resend's first frame, between its frames, or at the last one, sent but never received —
        would never hear that the attachment finished. The stream hands its watched set here when
        it ends and its synthesised-ready uids when it builds them, and every snapshot of the same
        owner recalls them: a uid that is still in flight is listed anyway, and one that is not is
        announced ready again, with its metadata read back. Per owner, not per id: the client
        keeps an id from before the cut, or from inside the snapshot, and neither names what it
        watched. The window is the ring's because a reconnect later than that is not expected to
        resume either; a redundant ready frame inside it is harmless, the clients apply it twice.
        """
        self._expire_watched(owner_id)
        if uids:
            now = self._clock()
            self._watched.setdefault(owner_id, {}).update(dict.fromkeys(uids, now))

    def recall_watched(self, owner_id: int) -> set[str]:
        """The uids remembered for the owner within the ring's window."""
        self._expire_watched(owner_id)
        return set(self._watched.get(owner_id, ()))

    def _expire_watched(self, owner_id: int) -> None:
        # Pruned on every touch of the owner, so what is kept is what that owner watched within
        # the window and no more; an owner nobody touches again keeps one window's worth at most.
        remembered = self._watched.get(owner_id)
        if remembered is None:
            return
        cutoff = self._clock() - self._ring_seconds
        for uid in [uid for uid, at in remembered.items() if at <= cutoff]:
            del remembered[uid]
        if not remembered:
            del self._watched[owner_id]

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

    def publish(
        self,
        owner_id: int,
        payload: dict,
        *,
        event: str = MESSAGE,
        audience: Audience = Audience.OWNER,
    ) -> Frame:
        """One ring entry per frame, whatever its audience: replay filters it per user exactly as
        the live fan-out does, through `Frame.reaches`."""
        self._seq += 1
        frame = Frame(self._seq, owner_id, payload, time.monotonic(), event, audience)
        self._ring.append(frame)
        self._evict()
        if audience is Audience.OWNER:
            targets = list(self._subs.get(owner_id, ()))
        else:
            targets = [
                sub
                for user_id, owned in self._subs.items()
                if frame.reaches(user_id)
                for sub in owned
            ]
        for sub in targets:
            self._deliver(sub, frame)
        return frame

    def _admit_progress(self, upload_uid: str) -> bool:
        """At most one upload.progress frame per upload per interval, leading edge: the first tail
        of a window goes out, the rest of that window are dropped. A dropped tail is never sent
        late — the next one carries a later offset, and finalize announces the attachment."""
        now = self._clock()
        last = self._progress_sent.get(upload_uid)
        if last is not None and now - last < self._progress_interval:
            return False
        if len(self._progress_sent) >= PROGRESS_TRACKED_MAX:
            horizon = now - self._progress_interval
            self._progress_sent = {u: t for u, t in self._progress_sent.items() if t > horizon}
        self._progress_sent[upload_uid] = now
        return True

    def _publish_direct(self, channel: str, raw: str) -> None:
        """The kinds whose NOTIFY carries the whole frame: no row is read back for them."""
        try:
            body = json.loads(raw)
            owner_id = int(body["owner_id"])
            if channel == UPLOAD_PROGRESS:
                upload_uid = str(body["upload_uid"])
                payload = {
                    "upload_uid": upload_uid,
                    "bytes_received": int(body["bytes_received"]),
                    "declared_size": int(body["declared_size"]),
                }
                if self._admit_progress(upload_uid):
                    self.publish(owner_id, payload, event=UPLOAD_PROGRESS_EVENT)
                return
            event = str(body["event"])
            if event not in MEMO_EVENTS:
                raise ValueError(event)
            audience = Audience(body["audience"])
            payload = {
                "uid": str(body["uid"]),
                "kind": event.removeprefix("memo."),
                "guest": body.get("guest"),
            }
        except (ValueError, KeyError, TypeError):
            logger.warning("events_bad_payload", channel=channel)
            return
        self.publish(owner_id, payload, event=event, audience=audience)

    def resync_all(self) -> None:
        # Whatever was lost may belong after any id a client already holds, the head included, so
        # every id up to now stops being replayable.
        self._invalid = self._seq
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
        await self._close_conn(STOP_CLOSE_TIMEOUT)

    def abandon(self) -> None:
        """Drop a hub whose event loop is gone (each test client request runs its own loop): its
        tasks can no longer run, so only the socket is released, synchronously."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.terminate()

    async def _close_conn(self, within: float = HEALTH_TIMEOUT) -> None:
        conn, self._conn = self._conn, None
        self._connected.clear()
        if conn is not None:
            try:
                async with asyncio.timeout(within):
                    await conn.close()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.terminate()

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        try:
            self._inbox.put_nowait((channel, payload))
        except asyncio.QueueFull:
            # The dispatcher is stuck behind a read-back. Dropping is safe only because the
            # dispatcher resyncs every subscriber once it next drains the inbox: that is after
            # this drop, so the snapshot it causes reads the row this notification named.
            if not self._inbox_overflowed:
                logger.warning("events_inbox_overflow", inbox_max=self._inbox.maxsize)
            self._inbox_overflowed = True

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
            if self._inbox_overflowed:
                # One resync per overflow episode, not per dropped notification: a drop after this
                # line needs the inbox full again, so this loop comes back round to it.
                self._inbox_overflowed = False
                self.resync_all()
            keys: list[tuple[str, int]] = []
            for channel, raw in batch:
                if channel != ATTACHMENT_STATUS:
                    self._publish_direct(channel, raw)
                    continue
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
