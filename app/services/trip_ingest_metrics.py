"""Guest-sync gauges for the trip pass puller, served on the app's `/metrics` (trip pass P3).

The puller runs in the worker, which has no HTTP server, so its health reaches Prometheus through
the database. The worker's boundary guard (`trip_ingest.run_sync_safe`) stamps the one-row
`trip_ingest_cursor` after every tick: `last_sync_at` only when the tick synced, `last_error`
either way. That row IS the snapshot, and its age is how a stalled, crashed or unconfigured puller
shows.

**This collector does database work on every scrape, unlike `DigestCollector` (ADR-0009).** The
plan accepts it (page 4506 § P3, r2 N3): the alerts must judge the tables themselves, never a count
the worker last wrote, or a dead worker freezes the very numbers that would have reported it. The
cost is bounded: ONE statement per scrape, run on the app's own capped pool (not a connection of
its own), and cut off at `SCRAPE_TIMEOUT_S`.

**What cannot be read is not published.** A failed or slow read yields no guest family at all, so
the series go ABSENT and `GuestSyncAbsent` fires; a guessed zero would have looked healthy.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from anyio import fail_after, from_thread
from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session
from app.models import IngestBatch, IngestBlob, Pass, PassSyncState, TripIngestCursor

logger = structlog.get_logger(__name__)

SNAPSHOT_AGE = "guest_sync_snapshot_age_seconds"
REQUIRED = "guest_sync_required"
PULL_LAG = "guest_pull_lag_seconds"
BLOBS_PENDING = "guest_blobs_pending"
QUARANTINED = "guest_batches_quarantined"

# Plan page 4506 § P3: the puller ticks "while ... a pass that existed in the last 30 days (to
# discover new batches)". `trip_ingest` keeps no such predicate (the worker ticks every 60 s while
# configured, app/worker.py:1553), so the window is stated here.
RECENT_PASS_WINDOW = timedelta(days=30)

# Well inside Prometheus's default 10 s scrape timeout, so a slow database costs these families,
# not the whole scrape.
SCRAPE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class GuestSyncState:
    last_sync_at: datetime | None
    required: bool
    oldest_open_at: datetime | None
    blobs_pending: int
    quarantined: int


def _statement():
    # Live = the ingest host may still accept uploads on it: not expired, and either not revoked
    # or revoked by a version the ingest host has not acknowledged yet (until then it still
    # honours the pass).
    revocation_acked = exists().where(
        PassSyncState.pass_id == Pass.id, PassSyncState.acked_version >= Pass.version
    )
    live_pass = exists().where(
        Pass.expires_at > func.now(),
        or_(Pass.revoked_at.is_(None), ~revocation_acked),
    )
    # Recent = the pass ended for the ingest host less than RECENT_PASS_WINDOW ago, or ended
    # before the last completed pull. Home cannot see a batch the ingest host has not been pulled
    # for: one completed after the last sync, with the pass expired or its revocation acked since,
    # leaves no home row at all. The plan's puller keeps ticking "while a pass existed in the
    # last 30 days (to discover new batches)", so a sync stays required for as long.
    # The ingest host honours a pass until it expires or until it ACKS the revocation, so a
    # revocation ends the pass at the ack (`last_push_at`, stamped in the statement that records
    # the acked version), not at `revoked_at`: a pass revoked during an outage of more than 30 days
    # is still accepting batches until the reconnect (MEMNG-1179).
    revocation_ack = case(
        (
            and_(Pass.revoked_at.is_not(None), PassSyncState.acked_version >= Pass.version),
            func.coalesce(PassSyncState.last_push_at, Pass.revoked_at),
        ),
    )
    ended_at = func.least(Pass.expires_at, func.coalesce(revocation_ack, Pass.expires_at))
    # Until a tick completes after the end, the window alone is not enough: a worker that dies
    # between the ack and `pull_batches`, or a link down for 30 days past an expiry, would leave
    # the batch unpulled and nothing saying so. `last_sync_at` is stamped only once a whole tick,
    # `pull_batches` included, has returned. Every pass is judged so, acked or not: a missing ack
    # does not prove the ingest host never held the pass (the 200 of a push can be lost, or home
    # can die before committing the ack), and `push_passes` records nothing before its request.
    last_sync_at = (
        select(TripIngestCursor.last_sync_at).where(TripIngestCursor.id == 1).scalar_subquery()
    )
    unpulled_since_end = or_(last_sync_at.is_(None), last_sync_at < ended_at)
    recent_pass = exists(
        select(Pass.id)
        .outerjoin(PassSyncState, PassSyncState.pass_id == Pass.id)
        .where(or_(ended_at > func.now() - RECENT_PASS_WINDOW, unpulled_since_end))
    )
    # Unacked = home still owes the ingest host its answer: not filed yet, filed but the ack not
    # delivered, or refused but the quarantine not delivered. A delivered quarantine is settled.
    owed_batch = exists().where(
        or_(
            IngestBatch.state == "open",
            and_(IngestBatch.state == "complete", IngestBatch.acked_at.is_(None)),
            and_(IngestBatch.state == "quarantined", IngestBatch.quarantined_at.is_(None)),
        )
    )
    return select(
        select(TripIngestCursor.last_sync_at)
        .where(TripIngestCursor.id == 1)
        .scalar_subquery()
        .label("last_sync_at"),
        or_(live_pass, recent_pass, owed_batch).label("required"),
        select(func.min(IngestBatch.created_at))
        .where(IngestBatch.state == "open")
        .scalar_subquery()
        .label("oldest_open_at"),
        select(func.count())
        .select_from(IngestBlob)
        .join(IngestBatch, IngestBatch.id == IngestBlob.batch_id)
        .where(IngestBatch.state == "open", IngestBlob.filed_at.is_(None))
        .scalar_subquery()
        .label("blobs_pending"),
        # Resolved batches stay `quarantined` for settle; unknown-pass quarantines have no row, so
        # the cursor's monotonic pair carries them (MEMNG-1045).
        (
            select(func.count())
            .select_from(IngestBatch)
            .where(IngestBatch.state == "quarantined", IngestBatch.resolved_at.is_(None))
            .scalar_subquery()
            + func.coalesce(
                select(
                    TripIngestCursor.quarantined_unknown_total
                    - TripIngestCursor.quarantined_unknown_cleared
                )
                .limit(1)
                .scalar_subquery(),
                0,
            )
        ).label("quarantined"),
    )


async def read_state(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> GuestSyncState:
    """The one per-scrape read. The factory is looked up at call time so a test can point it at a
    database that does not answer."""
    async with (session_factory or async_session)() as db:
        row = (await db.execute(_statement())).one()
    return GuestSyncState(
        last_sync_at=row.last_sync_at,
        required=bool(row.required),
        oldest_open_at=row.oldest_open_at,
        blobs_pending=int(row.blobs_pending),
        quarantined=int(row.quarantined),
    )


async def _read_bounded() -> GuestSyncState:
    with fail_after(SCRAPE_TIMEOUT_S):
        return await read_state()


def families_from(state: GuestSyncState, now: datetime) -> list[Metric]:
    out: list[Metric] = []
    if state.last_sync_at is not None:
        # No sync yet has no honest age: zero would read as fresh. Absent instead.
        out.append(
            GaugeMetricFamily(
                SNAPSHOT_AGE,
                "Seconds since the trip pass puller last completed a sync with the ingest host.",
                value=max(0.0, (now - state.last_sync_at).total_seconds()),
            )
        )
    out.append(
        GaugeMetricFamily(
            REQUIRED,
            "1 while a pass is live or ended within 30 days, or home owes a batch answer.",
            value=1.0 if state.required else 0.0,
        )
    )
    lag = 0.0
    if state.oldest_open_at is not None:
        lag = max(0.0, (now - state.oldest_open_at).total_seconds())
    out.append(
        GaugeMetricFamily(
            PULL_LAG,
            "Seconds the oldest pulled guest batch has waited to be filed (0 when none waits).",
            value=lag,
        )
    )
    out.append(
        GaugeMetricFamily(
            BLOBS_PENDING,
            "Files of pulled guest batches not filed yet.",
            value=float(state.blobs_pending),
        )
    )
    out.append(
        GaugeMetricFamily(
            QUARANTINED, "Guest batches home refused.", value=float(state.quarantined)
        )
    )
    return out


def descriptors_of(families: list[Metric]) -> list[Metric]:
    """The families' name, help and type without their samples, for a collector's `describe()`."""
    return [Metric(f.name, f.documentation, f.type, f.unit) for f in families]


# Every field set, so that the age family a scrape omits before the first sync is described too.
_DESCRIBE_AT = datetime(1970, 1, 1, tzinfo=UTC)
_DESCRIBE_STATE = GuestSyncState(
    last_sync_at=_DESCRIBE_AT,
    required=False,
    oldest_open_at=_DESCRIBE_AT,
    blobs_pending=0,
    quarantined=0,
)


class GuestSyncCollector:
    """Synchronous, as prometheus_client requires. The instrumentator's `/metrics` handler is a
    plain `def`, so Starlette runs it in an AnyIO worker thread; the read is handed back to the
    app's event loop, where the pooled async engine lives."""

    def describe(self):
        # Without it an auto-describing registry calls `collect()` at `register()`, at import time
        # where `from_thread.run` has no loop. Derived from `families_from` so the two cannot drift.
        return descriptors_of(families_from(_DESCRIBE_STATE, _DESCRIBE_AT))

    def collect(self):
        try:
            state = from_thread.run(_read_bounded)
        except Exception:
            logger.exception("trip_ingest.metrics_read_failed")
            return
        yield from families_from(state, datetime.now(UTC))


_registered: GuestSyncCollector | None = None


def register_once(registry: CollectorRegistry) -> GuestSyncCollector:
    """At most once per process: the test suite re-imports `app.main`, and prometheus_client
    refuses a duplicate collector by raising."""
    global _registered
    if _registered is None:
        _registered = GuestSyncCollector()
        registry.register(_registered)
    return _registered
