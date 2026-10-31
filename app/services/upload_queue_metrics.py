"""Open upload sessions and the media queue, read from the database per scrape (MEMNG-962).

The media queue is drained by the worker, which has no HTTP server, so its state reaches
Prometheus through `media_jobs`. Open sessions are read the same way rather than counted in
process: a session also ends in the worker (the reaper, a trip filing), where no in-process gauge
of the app's could see it go.

This follows `trip_ingest_metrics` (see its docstring for the ADR-0009 trade): ONE statement per
scrape on the app's own capped pool, cut off at `SCRAPE_TIMEOUT_S`, and **what cannot be read is
not published** — a failed or slow read yields none of these families, never a guessed zero.

`memosng_media_job_seconds` is the time from enqueue to done (`updated_at - created_at` of a `done`
row: the done write is the last one a finished job takes), queue wait and retries included,
because that is the delay a person sees between Post and a thumbnail. The run time alone is not
recoverable: the claim's own timestamp is overwritten by the done write, and keeping it would take
a column. It is a GAUGE histogram over the jobs that finished in the last `FINISHED_WINDOW`, not a
counter histogram over the table, because rows leave `media_jobs` when their attachment is
deleted, and a cumulative bucket that shrinks is a counter reset Prometheus would misread.

The two counters the worker keeps (MEMNG-1046) are read from its durable tallies for the same
reason (`app/services/tallies.py`): `memosng_media_jobs_failed_total{lane,reason}`, every pair
published, and the `expired` result of `memosng_upload_sessions_total`. That family's other results
are counted in this process (`upload_metrics.UPLOAD_SESSIONS`), so this collector publishes the
whole family, merged: a read that fails drops `expired` and keeps the in-process results.
"""

from dataclasses import dataclass
from datetime import timedelta

import structlog
from anyio import fail_after, from_thread
from prometheus_client import CollectorRegistry
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeHistogramMetricFamily,
    GaugeMetricFamily,
)
from prometheus_client.metrics_core import Metric
from prometheus_client.samples import Sample
from sqlalchemy import JSON, func, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session
from app.models import (
    MEDIA_JOB_CLASSES,
    FailureReason,
    JobLane,
    JobState,
    MediaJob,
    MediaJobFailureTally,
    Upload,
    UploadSessionTally,
)
from app.services import upload_metrics
from app.services.trip_ingest_metrics import descriptors_of

logger = structlog.get_logger(__name__)

SESSIONS_OPEN = "memosng_upload_sessions_open"
JOBS_QUEUED = "memosng_media_jobs_queued"
JOB_SECONDS = "memosng_media_job_seconds"
JOBS_FAILED = "memosng_media_jobs_failed"
SESSIONS = "memosng_upload_sessions"

# Long enough that a household's sparse uploads leave something to chart, short enough that a
# worker falling behind shows within the hour rather than being averaged away.
FINISHED_WINDOW = timedelta(hours=1)
# A photo derives in seconds; a video transcode is bounded by WORKER_TRANSCODE_STALL_SECONDS.
BUCKETS: tuple[float, ...] = (1, 5, 15, 60, 300, 900, 1800, 3600, 7200)

# Well inside Prometheus's default 10 s scrape timeout, so a slow database costs these families,
# not the whole scrape.
SCRAPE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class FinishedJobs:
    lane: str
    kind: str
    count: int
    seconds: float
    # Cumulative, one per entry of BUCKETS; +Inf is `count`.
    buckets: tuple[int, ...]


@dataclass(frozen=True)
class QueueState:
    sessions_open: int
    queued: dict[str, int]
    finished: tuple[FinishedJobs, ...]
    sessions_expired: int
    # (lane, reason) -> terminal failures, as the worker tallied them.
    failed: dict[tuple[str, str], int]


def _statement():
    # Past expires_at a session is the reaper's, whether or not the hourly sweep has run yet.
    sessions_open = (
        select(func.count()).select_from(Upload).where(Upload.expires_at > func.now())
    ).scalar_subquery()
    queued_rows = (
        select(MediaJob.lane.label("lane"), func.count().label("n"))
        .where(MediaJob.state == JobState.queued)
        .group_by(MediaJob.lane)
        .subquery()
    )
    queued = select(
        func.coalesce(
            func.json_object_agg(queued_rows.c.lane, queued_rows.c.n),
            func.json_build_object(),
            type_=JSON,
        )
    ).scalar_subquery()
    seconds = func.extract("epoch", MediaJob.updated_at - MediaJob.created_at)
    finished_rows = (
        select(
            MediaJob.lane.label("lane"),
            MediaJob.kind.label("kind"),
            func.count().label("n"),
            func.sum(seconds).label("s"),
            array([func.count().filter(seconds <= bound) for bound in BUCKETS]).label("b"),
        )
        .where(
            MediaJob.state == JobState.done,
            MediaJob.updated_at > func.now() - FINISHED_WINDOW,
        )
        .group_by(MediaJob.lane, MediaJob.kind)
        .subquery()
    )
    finished = select(
        func.coalesce(
            func.json_agg(
                func.json_build_array(
                    finished_rows.c.lane,
                    finished_rows.c.kind,
                    finished_rows.c.n,
                    finished_rows.c.s,
                    finished_rows.c.b,
                )
            ),
            func.json_build_array(),
            type_=JSON,
        )
    ).scalar_subquery()
    sessions_expired = func.coalesce(
        select(UploadSessionTally.count)
        .where(UploadSessionTally.result == "expired")
        .scalar_subquery(),
        0,
    )
    failed = select(
        func.coalesce(
            func.json_agg(
                func.json_build_array(
                    MediaJobFailureTally.lane,
                    MediaJobFailureTally.reason,
                    MediaJobFailureTally.count,
                )
            ),
            func.json_build_array(),
            type_=JSON,
        )
    ).scalar_subquery()
    return select(
        sessions_open.label("sessions_open"),
        queued.label("queued"),
        finished.label("finished"),
        sessions_expired.label("sessions_expired"),
        failed.label("failed"),
    )


async def read_state(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> QueueState:
    """The one per-scrape read. The factory is looked up at call time so a test can point it at a
    database that does not answer."""
    async with (session_factory or async_session)() as db:
        row = (await db.execute(_statement())).one()
    return QueueState(
        sessions_open=int(row.sessions_open),
        queued={str(lane): int(n) for lane, n in row.queued.items()},
        finished=tuple(
            FinishedJobs(
                lane=lane,
                kind=kind,
                count=int(n),
                seconds=float(s),
                buckets=tuple(int(b) for b in buckets),
            )
            for lane, kind, n, s, buckets in row.finished
        ),
        sessions_expired=int(row.sessions_expired),
        failed={(str(lane), str(reason)): int(n) for lane, reason, n in row.failed},
    )


async def _read_bounded() -> QueueState:
    with fail_after(SCRAPE_TIMEOUT_S):
        return await read_state()


def families_from(state: QueueState) -> list[Metric]:
    sessions = GaugeMetricFamily(
        SESSIONS_OPEN,
        "Upload sessions that can still take bytes (not past expires_at).",
        value=float(state.sessions_open),
    )
    queued = GaugeMetricFamily(
        JOBS_QUEUED, "Media jobs waiting for a worker, by lane.", labels=["lane"]
    )
    # Both lanes always: an empty lane is a measured zero, and its series must exist for the panel.
    for lane in JobLane:
        queued.add_metric([lane.value], float(state.queued.get(lane.value, 0)))
    seconds = GaugeHistogramMetricFamily(
        JOB_SECONDS,
        "Seconds from enqueue to done of the media jobs that finished in the last hour.",
        labels=["lane", "kind"],
    )
    for jobs in state.finished:
        bounds = [str(float(b)) for b in BUCKETS] + ["+Inf"]
        counts = [*jobs.buckets, jobs.count]
        seconds.add_metric(
            [jobs.lane, jobs.kind],
            buckets=[(bound, float(n)) for bound, n in zip(bounds, counts, strict=True)],
            gsum_value=jobs.seconds,
        )
    failed = CounterMetricFamily(
        JOBS_FAILED,
        "Media jobs that failed terminally, by worker consumer and reason.",
        labels=["lane", "reason"],
    )
    # Every pair, so a quiet hour reads 0 rather than no data; a pair only a newer worker writes
    # is published as it was tallied.
    pairs = {(lane, reason.value): 0 for lane in MEDIA_JOB_CLASSES for reason in FailureReason}
    for (lane, reason), n in {**pairs, **state.failed}.items():
        failed.add_metric([lane, reason], float(n))
    return [sessions, queued, seconds, failed]


def sessions_family(expired: int | None) -> Metric:
    """`memosng_upload_sessions` as this process counted it, plus the worker's `expired` when the
    tally could be read (None: it could not, and a guessed zero would be a lie)."""
    family = Metric(SESSIONS, upload_metrics.SESSIONS_HELP, "counter")
    for metric in upload_metrics.UPLOAD_SESSIONS.collect():
        family.samples.extend(metric.samples)
    if expired is not None:
        family.samples.append(Sample(f"{SESSIONS}_total", {"result": "expired"}, float(expired)))
    return family


_DESCRIBE_STATE = QueueState(sessions_open=0, queued={}, finished=(), sessions_expired=0, failed={})


class UploadQueueCollector:
    """Synchronous, as prometheus_client requires; the read runs on the app's event loop, as
    `trip_ingest_metrics.GuestSyncCollector` explains."""

    def describe(self):
        # Keeps `register()` from calling `collect()` outside a loop; see GuestSyncCollector.
        return descriptors_of([sessions_family(None), *families_from(_DESCRIBE_STATE)])

    def collect(self):
        try:
            state = from_thread.run(_read_bounded)
        except Exception:
            logger.exception("upload_queue.metrics_read_failed")
            state = None
        yield sessions_family(None if state is None else state.sessions_expired)
        if state is not None:
            yield from families_from(state)


_registered: UploadQueueCollector | None = None


def register_once(registry: CollectorRegistry) -> UploadQueueCollector:
    """At most once per process: the test suite re-imports `app.main`, and prometheus_client
    refuses a duplicate collector by raising."""
    global _registered
    if _registered is None:
        _registered = UploadQueueCollector()
        registry.register(_registered)
    return _registered
