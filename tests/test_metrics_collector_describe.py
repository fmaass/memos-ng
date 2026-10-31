"""Registering the DB-reading collectors must not read the database (MEMNG-1051).

The global REGISTRY auto-describes: a collector without `describe()` is `collect()`ed at
`register()`, which at boot runs outside any worker thread, so `from_thread.run` has no loop and
every start logged a `metrics_read_failed` traceback. These register real collectors on a fresh
auto-describing registry, outside an event loop, as `app.main` does at import.
"""

import pytest
from app.services import trip_ingest_metrics as tm
from app.services import upload_queue_metrics as qm
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

COLLECTORS = [
    pytest.param(qm.UploadQueueCollector, id="upload_queue"),
    pytest.param(tm.GuestSyncCollector, id="guest_sync"),
]


@pytest.mark.parametrize("make", COLLECTORS)
def test_register_does_not_read(make):
    registry = CollectorRegistry(auto_describe=True)
    with capture_logs() as logs:
        registry.register(make())
    assert [e for e in logs if e["event"].endswith("metrics_read_failed")] == []


@pytest.mark.parametrize("make", COLLECTORS)
def test_register_keeps_the_name_collision_check(make):
    # An empty describe() would also skip the read, but would let a second collector publish the
    # same series unnoticed; the duplicate must still be refused at registration.
    registry = CollectorRegistry(auto_describe=True)
    registry.register(make())
    with pytest.raises(ValueError, match="Duplicated timeseries"):
        registry.register(make())


@pytest.mark.parametrize(
    ("make", "published"),
    [
        pytest.param(
            qm.UploadQueueCollector,
            {
                qm.SESSIONS_OPEN: "gauge",
                qm.JOBS_QUEUED: "gauge",
                qm.JOB_SECONDS: "gaugehistogram",
                qm.JOBS_FAILED: "counter",
                qm.SESSIONS: "counter",
                qm.ORPHANED: "gauge",
            },
            id="upload_queue",
        ),
        pytest.param(
            tm.GuestSyncCollector,
            {
                tm.SNAPSHOT_AGE: "gauge",
                tm.REQUIRED: "gauge",
                tm.PULL_LAG: "gauge",
                tm.BLOBS_PENDING: "gauge",
                tm.QUARANTINED: "gauge",
            },
            id="guest_sync",
        ),
    ],
)
def test_describe_names_every_published_family_without_samples(make, published):
    # Includes the guest-sync age family, which a scrape omits until the first sync.
    described = list(make().describe())
    assert {m.name: m.type for m in described} == published
    assert all(m.samples == [] for m in described)
