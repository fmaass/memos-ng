"""Web push (VAPID) — notify a user's subscribed devices when their media finishes.

`send_push` is the blocking single-send (run in a worker thread); `send_to_user` fans
out to every subscription a user has and prunes the ones the push service reports Gone
(404/410). Both are no-ops when VAPID is unconfigured, so the pipeline runs unchanged
without push set up. pywebpush is referenced via the module (not `from … import webpush`)
so tests can monkeypatch `pywebpush.webpush` without network.
"""

import functools
import json
from collections.abc import Awaitable, Callable

import pywebpush
import structlog
from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PushSubscription

logger = structlog.get_logger(__name__)

# A push service returns these when the endpoint is permanently gone — stop retrying it.
DEAD_CODES = {404, 410}


def send_push(info: dict, payload: dict) -> None:
    """One blocking web-push send. Raises pywebpush.WebPushException on a push-service error."""
    pywebpush.webpush(
        subscription_info=info,
        data=json.dumps(payload),
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        vapid_claims={"sub": settings.VAPID_SUBJECT},
    )


# Asked immediately before each subscription's send; False stops every send still to come.
StillWanted = Callable[[], Awaitable[bool]]


async def send_to_user(
    db: AsyncSession, user_id: int | None, payload: dict, *, still_wanted: StillWanted | None = None
) -> int:
    """Push `payload` to all of `user_id`'s subscriptions; prune dead endpoints.

    No-op (returns 0) when VAPID is unconfigured or there is no owner. Best-effort: a
    transient push error is logged but never raised — a push failure must not fail the
    media job that triggered it. Returns the number of sends that succeeded.

    `still_wanted`, when given, is asked again before EVERY subscription, not once for the user:
    a user with a phone and a laptop is two sends, a push service round-trip apart, and what
    decided the first may have changed by the second (MEMNG-1189 — see `memo_live.announce`).
    """
    if user_id is None or not settings.VAPID_PRIVATE_KEY:
        return 0
    subs = (
        (await db.execute(select(PushSubscription).where(PushSubscription.user_id == user_id)))
        .scalars()
        .all()
    )
    sent = 0
    for sub in subs:
        if still_wanted is not None and not await still_wanted():
            break
        info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
        try:
            await to_thread.run_sync(functools.partial(send_push, info, payload))
            sent += 1
        except pywebpush.WebPushException as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", None) in DEAD_CODES:
                await db.delete(sub)  # endpoint permanently gone — prune so we stop retrying
            else:
                logger.warning("web_push_failed", user_id=user_id, error=str(exc))
        except Exception as exc:  # never let a push error escape into the worker's job loop
            logger.warning("web_push_error", user_id=user_id, error=str(exc))
    await db.flush()
    return sent
