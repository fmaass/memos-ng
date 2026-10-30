"""The upload and event-stream families the APP observes in its own request path (MEMNG-962).

They are in-process instruments because the app is where these things happen: bytes arrive in a
PATCH, admission refuses there, a session ends in the finalize that consumes it, and an
`/api/events` stream lives in this process. Their database-derived siblings (open sessions, the
media queue) are `upload_queue_metrics`, which reads the tables per scrape instead.

Created unregistered and registered by `register_once`, like the other collectors on `/metrics`:
the test suite re-imports `app.main`, and prometheus_client refuses a duplicate by raising.
"""

from collections.abc import AsyncIterator
from typing import Literal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# The auth path that admitted the request (`request.state.auth_kind`): a PAT is the iOS app, a
# session the web SPA, a capture grant the iOS share extension (MEMNG-938). Never a user: the panel
# asks "are bytes arriving", not whose.
Principal = Literal["pat", "session", "capture_grant"]
# Which cap refused: one account's bodies or the whole process's.
AdmissionScope = Literal["user", "process"]
# How a consumed session ended. `rejected_size` is the third value this process publishes (see
# `count_size_rejection`); the plan's `expired` and `withdrawn` are not decided here (see
# `count_session_end`).
SessionEnd = Literal["completed", "deduped"]

UPLOAD_BYTES = Counter(
    "memosng_upload_bytes",
    "Upload bytes the server committed from a PATCH body (replays of held ranges add nothing).",
    ["principal"],
    registry=None,
)
UPLOAD_SESSIONS = Counter(
    "memosng_upload_sessions",
    "Upload sessions ended, by result. Counted when the ending transaction commits.",
    ["result"],
    registry=None,
)
# From a small photo chunk on the LAN to a phone's whole-file body over a slow uplink.
UPLOAD_PATCH_SECONDS = Histogram(
    "memosng_upload_patch_seconds",
    "Seconds an admitted upload PATCH held its body slot, success or refusal.",
    buckets=(0.05, 0.25, 1, 2.5, 5, 10, 30, 60, 120, 300, 900),
    registry=None,
)
ADMISSION_REJECTIONS = Counter(
    "memosng_upload_admission_rejections",
    "Upload bodies refused 429 before a byte was read, by the cap that refused.",
    ["scope"],
    registry=None,
)
EVENTS_SUBSCRIBERS = Gauge(
    "memosng_events_subscribers",
    "Live /api/events streams in this process.",
    registry=None,
)

# Every label value published from the first scrape, so a rate over a quiet hour reads 0 rather
# than no data.
for _principal in ("pat", "session"):
    UPLOAD_BYTES.labels(principal=_principal)
for _result in ("completed", "deduped", "rejected_size"):
    UPLOAD_SESSIONS.labels(result=_result)
for _scope in ("user", "process"):
    ADMISSION_REJECTIONS.labels(scope=_scope)

_PENDING_KEY = "memosng_upload_session_results"


def count_bytes(principal: str, received: int) -> None:
    if received > 0:
        UPLOAD_BYTES.labels(principal=principal).inc(received)


def count_rejection(scope: AdmissionScope) -> None:
    ADMISSION_REJECTIONS.labels(scope=scope).inc()


def count_size_rejection() -> None:
    """A size refusal ends nothing in the database (the create is never written, the PATCH rolls
    back), so there is no commit to wait for: it is counted when it is answered."""
    UPLOAD_SESSIONS.labels(result="rejected_size").inc()


def count_session_end(db: AsyncSession, result: SessionEnd) -> None:
    """Record a session's end, to be counted only if the transaction that deletes its row commits.

    A finalize that rolls back leaves the session open, and the client finalizes it again; counted
    here it would be counted twice. Called at every site that consumes an `uploads` row, which is
    every caller of the finalize family — the standalone route, the memo create, a trip filing.

    `expired` is not counted: the reaper runs in the worker, which serves no `/metrics`, and the row
    it deletes is the only record there was. `withdrawn` has no server path (no route deletes a
    session on request).
    """
    db.sync_session.info.setdefault(_PENDING_KEY, []).append(result)


@event.listens_for(Session, "after_commit")
def _count_committed(session: Session) -> None:
    for result in session.info.pop(_PENDING_KEY, ()):
        UPLOAD_SESSIONS.labels(result=result).inc()


@event.listens_for(Session, "after_rollback")
def _drop_rolled_back(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


async def events_subscriber[T](stream: AsyncIterator[T]) -> AsyncIterator[T]:
    """Count `stream` as a live subscriber from its first frame until it is closed.

    A wrapper rather than inline inc/dec so the `/api/events` rewrite (MEMNG-964) keeps the gauge
    by wrapping its new stream. The `finally` is what a disconnect reaches: Starlette closes the
    generator when the client goes away, and the close is handed on to the wrapped stream so its
    own cleanup runs then rather than whenever it is collected.
    """
    EVENTS_SUBSCRIBERS.inc()
    try:
        async for frame in stream:
            yield frame
    finally:
        EVENTS_SUBSCRIBERS.dec()
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()


_registered: bool = False


def register_once(registry: CollectorRegistry) -> bool:
    global _registered
    if not _registered:
        for metric in (
            UPLOAD_BYTES,
            UPLOAD_SESSIONS,
            UPLOAD_PATCH_SECONDS,
            ADMISSION_REJECTIONS,
            EVENTS_SUBSCRIBERS,
        ):
            registry.register(metric)
        _registered = True
    return _registered
