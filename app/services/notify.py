"""One seam for every notification a user gets about their household's media (MEMNG-967).

`notify(db, user_id, event)` hands one event to every consumer in `CONSUMERS`, in order. Today
there is one consumer, web push; the Live Activity update (MEMNG-950) plugs in beside it as a second
entry rather than as a second call site in the worker. Each event knows its own web-push payload,
so a consumer never has to tell the events apart to reach the wire.

The web-push consumer reaches the sender through the `push` MODULE (`push.send_to_user`), not a
name imported from it, so a test that replaces `push.send_to_user` still sees every send.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import push


@dataclass(frozen=True)
class AttachmentTerminal:
    """One attachment reached `ready` or `failed` — the per-file push the worker has always sent."""

    uid: str
    status: str
    filename: str

    def payload(self) -> dict:
        # The payload the service worker has always read: unchanged, byte for byte.
        return {"uid": self.uid, "status": self.status, "title": self.filename}


@dataclass(frozen=True)
class MemoLive:
    """A freshly created memo has no file left in flight: it is live for whoever may see it."""

    memo_uid: str
    owner: str  # the owner's display name
    title: str  # first line of the content, or the guest contributor
    attachments: int

    def payload(self) -> dict:
        # Nothing of the memo beyond these: a push travels through a third-party push service.
        return {
            "kind": "memo_live",
            "uid": self.memo_uid,
            "owner": self.owner,
            "title": self.title,
            "attachments": self.attachments,
        }


Event = AttachmentTerminal | MemoLive
Consumer = Callable[[AsyncSession, int | None, Event], Awaitable[None]]


async def web_push(db: AsyncSession, user_id: int | None, event: Event) -> None:
    await push.send_to_user(db, user_id, event.payload())


CONSUMERS: list[Consumer] = [web_push]


async def notify(db: AsyncSession, user_id: int | None, event: Event) -> None:
    """Deliver `event` to `user_id` through every consumer. Best-effort is each consumer's own
    contract (`push.send_to_user` never raises on a push-service error); this only fans out."""
    for consumer in CONSUMERS:
        await consumer(db, user_id, event)
