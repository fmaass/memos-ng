"""The two digest gauges, served from a process-local snapshot the APP refreshes.

ADR-0009 is the constraint this design exists under: `/metrics` may do NO database work per
scrape. memos-ng shares a small pool with production on a shared Postgres, and a scrape that
queried would add connections outside the cap — which is the exact failure ADR-0009 was written
after. So a supervised task in the app's `lifespan` refreshes an in-memory snapshot every 60 s,
and the collector reads only that.

**A stale snapshot publishes NOTHING.** The refresher catches and logs every exception and retries
with backoff, but that is not enough on its own: a task that dies anyway would freeze the last
snapshot, and a snapshot frozen at `enabled=0` makes `MemosDigestStale` (which requires `== 1`)
and `MemosDigestMetricsAbsent` (which requires absence) both silent forever — the precise
silent-failure class this whole feature exists to end. Past `SNAPSHOT_MAX_AGE_S` the collector
therefore emits no `memosng_digest_*` family at all, the series go ABSENT, and the absence rule
fires. A frozen value would have looked healthy.
"""

import asyncio
import time
from dataclasses import dataclass, field

import structlog
from prometheus_client import CollectorRegistry
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session
from app.models import DigestRunStatus, DigestTrigger

logger = structlog.get_logger(__name__)

REFRESH_INTERVAL_S = 60.0
# Two and a half refresh intervals plus slack: long enough that one slow cycle is not an outage,
# short enough that a dead refresher goes absent inside the 5-minute scrape windows Prometheus
# rules work in.
SNAPSHOT_MAX_AGE_S = 300.0
REFRESH_BACKOFF_S = 10.0

ENABLED = "memosng_digest_enabled"
LAST_SUCCESS = "memosng_digest_last_success_timestamp_seconds"
RUNS = "memosng_digest_runs"
SNAPSHOT_AGE = "memosng_digest_snapshot_age_seconds"

# A run that did not actually mail the household: an import writes history and a test-send goes to
# one admin address. Neither may read as an SMTP success, or an import on the morning of
# enablement would silence the 26 h stale alert while nothing had been delivered at all.
#
# Spelled with the enums that DEFINE this vocabulary rather than with literals: `metrics` and
# `runner` both decide what "a success" is, and two copies of a status string are two places for
# it to be spelled differently once.
NON_DELIVERING_TRIGGERS = (DigestTrigger.test, DigestTrigger.import_)
SUCCESS_STATUSES = (DigestRunStatus.sent, DigestRunStatus.nothing_to_send)


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    enabled: bool
    # Unix seconds of the newest DELIVERING success, or 0.0 when there has never been one. Always
    # published: the stale rule and every `/api/v1/query` read-back must work from day one, and a
    # missing series is indistinguishable from a broken exporter.
    last_success: float
    runs_by_status: dict[str, int] = field(default_factory=dict)


_snapshot: Snapshot | None = None


def current() -> Snapshot | None:
    return _snapshot


def set_snapshot(snapshot: Snapshot | None) -> None:
    """Replace the snapshot. Tests use it to age one past `SNAPSHOT_MAX_AGE_S`."""
    global _snapshot
    _snapshot = snapshot


async def read_snapshot(db: AsyncSession) -> Snapshot:
    """One pass over the digest tables. Terminal rows only for the counter — a run still in flight
    (`status IS NULL`) contributes to no label until its outcome is known, because a Counter may
    not move backwards and a corrected label would have to."""
    enabled = bool(await db.scalar(sa_text("SELECT enabled FROM digest_settings WHERE id = 1")))
    last = await db.scalar(
        sa_text(
            "SELECT EXTRACT(EPOCH FROM max(finished_at)) FROM digest_runs "
            "WHERE status = ANY(:statuses) AND trigger <> ALL(:triggers)"
        ),
        {"statuses": list(SUCCESS_STATUSES), "triggers": list(NON_DELIVERING_TRIGGERS)},
    )
    rows = await db.execute(
        sa_text("SELECT status, count(*) FROM digest_runs WHERE status IS NOT NULL GROUP BY status")
    )
    return Snapshot(
        taken_at=time.time(),
        enabled=enabled,
        last_success=float(last) if last is not None else 0.0,
        runs_by_status={row[0]: int(row[1]) for row in rows},
    )


async def refresh_now(
    session_factory: async_sessionmaker[AsyncSession] = async_session,
) -> Snapshot:
    """Refresh the snapshot immediately.

    Called by the `run-now` and `test-send` handlers when they finish, and directly by tests, so
    "the metrics change after a run" is probeable rather than a 60-second wait. A SCHEDULED run
    happens in the worker process, which has no snapshot of its own — the app picks it up on the
    next interval.
    """
    async with session_factory() as db:
        snapshot = await read_snapshot(db)
    set_snapshot(snapshot)
    return snapshot


async def refresher(
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    *,
    interval: float = REFRESH_INTERVAL_S,
    backoff: float = REFRESH_BACKOFF_S,
) -> None:
    """The supervised loop the app's lifespan starts.

    Every exception is caught and logged and the loop keeps going with a shorter backoff — the
    same boundary guard `run_frame_share_recover_safe` applies, for the same reason: an exception
    escaping here would kill the task and freeze the snapshot. The collector's staleness check is
    what covers the case where it dies anyway.
    """
    while True:
        try:
            await refresh_now(session_factory)
            delay = interval
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("digest.metrics_refresh_error")
            delay = backoff
        await asyncio.sleep(delay)


class DigestCollector:
    """A synchronous custom collector over the snapshot alone — zero database work per scrape."""

    def collect(self):
        snapshot = _snapshot
        if snapshot is None or (time.time() - snapshot.taken_at) > SNAPSHOT_MAX_AGE_S:
            # Publish NOTHING. See the module docstring: absence is the guard, and a frozen value
            # would have looked healthy.
            return
        yield GaugeMetricFamily(
            ENABLED, "Whether the daily digest is armed.", value=1.0 if snapshot.enabled else 0.0
        )
        yield GaugeMetricFamily(
            LAST_SUCCESS,
            "Unix time of the last digest run that delivered (0 when there has never been one).",
            value=snapshot.last_success,
        )
        runs = CounterMetricFamily(
            RUNS, "Digest runs that reached a terminal status.", labels=["status"]
        )
        for status, count in sorted(snapshot.runs_by_status.items()):
            runs.add_metric([status], float(count))
        yield runs
        yield GaugeMetricFamily(
            SNAPSHOT_AGE,
            "Age of the snapshot these digest metrics are served from.",
            value=time.time() - snapshot.taken_at,
        )


_registered: DigestCollector | None = None


def register_once(registry: CollectorRegistry) -> DigestCollector:
    """Register the collector on `registry`, at most once per process.

    Guarded because `app.main` is imported repeatedly by the test suite and
    `prometheus_client` refuses a duplicate collector by RAISING — which would turn a second
    import into an import-time crash rather than a no-op. Returns the live collector either way,
    so a caller always has the object it just ensured is registered.
    """
    global _registered
    if _registered is None:
        _registered = DigestCollector()
        registry.register(_registered)
    return _registered


def families(collector: DigestCollector | None = None) -> list[Metric]:
    """What the collector would publish right now — for tests, and for a `/metrics` read-back."""
    return list((collector or DigestCollector()).collect())
