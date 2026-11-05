"""The upload and event-stream families the APP observes in its own request path (MEMNG-962).

They are in-process instruments because the app is where these things happen: bytes arrive in a
PATCH, admission refuses there, a session ends in the finalize that consumes it, and an
`/api/events` stream lives in this process. Their database-derived siblings (open sessions, the
media queue) are `upload_queue_metrics`, which reads the tables per scrape instead.

Created unregistered and registered by `register_once`, like the other collectors on `/metrics`:
the test suite re-imports `app.main`, and prometheus_client refuses a duplicate by raising.
"""

import functools
from collections.abc import AsyncIterator, Callable
from typing import Literal

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

# The auth path that admitted the request (`request.state.auth_kind`): a PAT is the iOS app, a
# session the web SPA, a capture grant the iOS share extension (MEMNG-938). Never a user: the panel
# asks "are bytes arriving", not whose.
Principal = Literal["pat", "session", "capture_grant"]
# Which cap refused: one account's bodies or the whole process's.
AdmissionScope = Literal["user", "process"]
# How a session ended. `rejected_size` is the fourth value this process publishes (see
# `count_size_rejection`); `expired` is the worker's, read from its tally per scrape
# (`upload_queue_metrics`). `withdrawn` is the owner's DELETE (MEMNG-953), counted here when it
# commits; the reap that later removes that row counts nothing more for it.
SessionEnd = Literal["completed", "deduped", "withdrawn"]

UPLOAD_BYTES = Counter(
    "memosng_upload_bytes",
    "Upload bytes the server committed from a PATCH body (replays of held ranges add nothing).",
    ["principal"],
    registry=None,
)
SESSIONS_HELP = "Upload sessions ended, by result. Counted when the ending transaction commits."
UPLOAD_SESSIONS = Counter(
    "memosng_upload_sessions",
    SESSIONS_HELP,
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
for _result in ("completed", "deduped", "withdrawn", "rejected_size"):
    UPLOAD_SESSIONS.labels(result=_result)
for _scope in ("user", "process"):
    ADMISSION_REJECTIONS.labels(scope=_scope)

_PENDING_KEY = "memosng_upload_session_results"


def _never_raises[**P](record: Callable[P, None]) -> Callable[P, None]:
    """Instrumentation never changes a request's outcome: these run on the 413, the 429, in the
    PATCH's `finally` and inside a commit, where a raise would replace the answer (or report a
    committed finalize as failed). A broken instrument is logged instead."""

    @functools.wraps(record)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            record(*args, **kwargs)
        except Exception:
            logger.exception("upload_metrics.record_failed", record=record.__name__)

    return guarded


@_never_raises
def count_bytes(principal: str, received: int) -> None:
    if received > 0:
        UPLOAD_BYTES.labels(principal=principal).inc(received)


@_never_raises
def count_rejection(scope: AdmissionScope) -> None:
    ADMISSION_REJECTIONS.labels(scope=scope).inc()


@_never_raises
def count_size_rejection() -> None:
    """A size refusal ends nothing in the database (the create is never written, the PATCH rolls
    back), so there is no commit to wait for: it is counted when it is answered."""
    UPLOAD_SESSIONS.labels(result="rejected_size").inc()


@_never_raises
def count_session_end(db: AsyncSession, result: SessionEnd) -> None:
    """Record a session's end, to be counted only if the transaction that ends it commits.

    A finalize that rolls back leaves the session open, and the client finalizes it again; counted
    here it would be counted twice. Called at every site that consumes an `uploads` row, which is
    every caller of the finalize family — the standalone route, the memo create, a trip filing —
    and at the withdraw, which ends a session without deleting its row (the reap does that later).

    `expired` is not counted here: the reaper runs in the worker, which serves no `/metrics`, so it
    bumps a tally row in its own transaction (`tallies.count_upload_sessions`) that the scrape
    reads.
    """
    db.sync_session.info.setdefault(_PENDING_KEY, []).append(result)


@_never_raises
def observe_patch_seconds(seconds: float) -> None:
    UPLOAD_PATCH_SECONDS.observe(seconds)


# Both hooks also fire for a SAVEPOINT (released or rolled back inside the transaction), which is
# neither the commit that ends the session nor the rollback that keeps it: only the outermost
# transaction's end decides. A root commit or rollback with a savepoint still open fires once for
# the savepoint and once more, un-nested, for the root.
@event.listens_for(Session, "after_commit")
@_never_raises
def _count_committed(session: Session) -> None:
    if session.in_nested_transaction():
        return
    for result in session.info.pop(_PENDING_KEY, ()):
        UPLOAD_SESSIONS.labels(result=result).inc()


@event.listens_for(Session, "after_rollback")
@_never_raises
def _drop_rolled_back(session: Session) -> None:
    if session.in_nested_transaction():
        return
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
        # UPLOAD_SESSIONS is not registered here: `upload_queue_metrics` publishes it, merged
        # with the `expired` result the worker tallies in the database (MEMNG-1046).
        for metric in (
            UPLOAD_BYTES,
            UPLOAD_PATCH_SECONDS,
            ADMISSION_REJECTIONS,
            EVENTS_SUBSCRIBERS,
        ):
            registry.register(metric)
        _registered = True
    return _registered
