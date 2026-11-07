"""Guest-sync metrics on the app's `/metrics` (trip pass P3, MEMNG-1044).

Every assertion reads the REAL scrape body or the real per-scrape read, against rows in the real
database: the collector is the unit under test, so nothing here stands in for it. A positive
control (`python_gc`) proves each body was actually read.
"""

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.services import trip_ingest
from app.services import trip_ingest_metrics as m
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import SYNC_URL
from tests.test_attachments_taken import _sql

TABLES = (
    "trip_ingest_cursor",
    "pass_sync_state",
    "pass_events",
    "ingest_blobs",
    "ingest_batches",
    "pass_devices",
    "passes",
)
FAMILIES = (m.SNAPSHOT_AGE, m.REQUIRED, m.PULL_LAG, m.BLOBS_PENDING, m.QUARANTINED)


@pytest.fixture
def clean(db_engine):
    for table in TABLES:
        _sql(f"DELETE FROM {table}")
    yield
    for table in TABLES:
        _sql(f"DELETE FROM {table}")


def _scrape(client) -> str:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "python_gc" in r.text, "positive control: the metrics body was not read"
    return r.text


def _absent(body: str, name: str) -> bool:
    """No sample (labelled or not) and no HELP/TYPE line of the family: a bare `name ` substring
    test would pass on a labelled sample and is not enough."""
    token = re.compile(rf"(^|\s){re.escape(name)}(\{{|\s|$)")
    return not any(token.search(line) for line in body.splitlines())


def _value(body: str, name: str) -> float | None:
    """The sample of an unlabelled gauge, or None when the scrape carries no sample of it."""
    for line in body.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[1])
    return None


def _pass(
    uid: str,
    *,
    expires_in: timedelta = timedelta(days=3),
    revoked: bool = False,
    revoked_ago: timedelta = timedelta(0),
    version: int = 1,
) -> int:
    rows = _sql(
        "INSERT INTO passes (uid, secret_hash, label, expires_at, version, revoked_at, "
        "revoked_version) VALUES (:u, :h, 'trip', now() + :e, :v, :ra, :rv) RETURNING id",
        {
            "u": uid,
            "h": f"hash-{uid}",
            "e": expires_in,
            "v": version,
            "ra": datetime.now(UTC) - revoked_ago if revoked else None,
            "rv": version if revoked else None,
        },
    )
    return rows[0][0]


def _synced(pass_id: int, acked_version: int, *, acked_ago: timedelta = timedelta(0)) -> None:
    """The ingest host acknowledged `acked_version`; `push_passes` stamps `last_push_at` in the
    same statement as the version it acks."""
    _sql(
        "INSERT INTO pass_sync_state (pass_id, acked_version, last_push_at) "
        "VALUES (:p, :v, now() - :ago)",
        {"p": pass_id, "v": acked_version, "ago": acked_ago},
    )


def _batch(
    pass_id: int,
    uid: str,
    state: str,
    *,
    age: timedelta = timedelta(0),
    acked: bool = False,
    quarantine_delivered: bool = False,
) -> int:
    rows = _sql(
        "INSERT INTO ingest_batches (uid, pass_id, state, created_at, acked_at, quarantined_at) "
        "VALUES (:u, :p, :s, now() - :age, :a, :q) RETURNING id",
        {
            "u": uid,
            "p": pass_id,
            "s": state,
            "age": age,
            "a": datetime.now(UTC) if acked else None,
            "q": datetime.now(UTC) if quarantine_delivered else None,
        },
    )
    return rows[0][0]


def _blob(batch_id: int, n: int, *, filed: bool) -> None:
    _sql(
        "INSERT INTO ingest_blobs (batch_id, sha256, size_bytes, mime, filename, filed_at) "
        "VALUES (:b, :s, 10, 'image/jpeg', :f, :at)",
        {
            "b": batch_id,
            "s": f"{n:064x}",
            "f": f"{n}.jpg",
            "at": datetime.now(UTC) if filed else None,
        },
    )


def _cursor(last_sync_at: datetime | None, last_error: str | None = None) -> None:
    _sql(
        "INSERT INTO trip_ingest_cursor (id, last_sync_at, last_error) VALUES (1, :t, :e)",
        {"t": last_sync_at, "e": last_error},
    )


GONE = timedelta(days=31)  # past the 30-day window in which an ended pass still requires a sync


def _expired_pass() -> int:
    """A pass that ended more than 30 days ago, so it alone requires no sync."""
    return _pass("p-expired", expires_in=-GONE)


# --- guest_sync_required: from the tables, one case per input category ---------------------------


async def test_nothing_to_sync_publishes_required_zero(clean, client):
    body = _scrape(client)
    assert _value(body, m.REQUIRED) == 0.0


@pytest.mark.parametrize(
    "case, required",
    [
        ("live", 1.0),
        ("expired_recently", 1.0),
        ("expired_over_30_days", 0.0),
        ("revoked_and_acked_recently", 1.0),
        ("revoked_and_acked_over_30_days", 0.0),
        ("revoked_not_acked_over_30_days", 1.0),
        ("revoked_not_acked", 1.0),
        ("revoked_never_pushed", 1.0),
    ],
)
async def test_a_pass_requires_sync_while_honoured_or_ended_within_30_days(
    clean, client, case, required
):
    """A pass that ended recently may still have batches on the ingest host that home has never
    pulled (no home row exists for them), so a sync stays required for 30 days after the end."""
    if case == "live":
        _pass("p1")
    elif case == "expired_recently":
        _pass("p1", expires_in=-timedelta(hours=1))
    elif case == "expired_over_30_days":
        _expired_pass()
    elif case == "revoked_and_acked_recently":
        _synced(_pass("p1", revoked=True, revoked_ago=timedelta(hours=1), version=2), 2)
    elif case == "revoked_and_acked_over_30_days":
        _synced(_pass("p1", revoked=True, revoked_ago=GONE, version=2), 2, acked_ago=GONE)
        _cursor(datetime.now(UTC))  # a pull has completed since the ack
    elif case == "revoked_not_acked_over_30_days":
        _synced(_pass("p1", revoked=True, revoked_ago=GONE, version=2), 1)
    elif case == "revoked_not_acked":
        _synced(_pass("p1", revoked=True, version=2), 1)
    else:
        _pass("p1", revoked=True, version=2)
    assert _value(_scrape(client), m.REQUIRED) == required


@pytest.mark.parametrize(
    "last_tick_ago",
    [
        timedelta(days=41),  # the last tick that completed was before the outage
        # A tick completed after the revocation while the ingest host still held an older version
        # (`push_passes` records no ack then and the tick goes on): only the ack can anchor it.
        timedelta(days=35),
    ],
)
async def test_a_revocation_acked_after_a_long_outage_requires_sync_until_a_pull_completes(
    clean, client, last_tick_ago
):
    """MEMNG-1179, the ticket's scenario. The pass is revoked while the link is down for more than
    30 days, and the ingest host accepts a batch meanwhile (it honours the pass until it acks the
    revocation). On reconnect `push_passes` commits the ack and the worker dies before
    `pull_batches`: home holds no row for that batch and the pass is no longer live, so only the
    window, measured from the ACK rather than from `revoked_at`, can say a sync is owed."""
    _cursor(datetime.now(UTC) - last_tick_ago)
    pass_id = _pass("p1", revoked=True, revoked_ago=timedelta(days=40), version=2)
    _synced(pass_id, 2, acked_ago=timedelta(seconds=5))  # the ack, committed on reconnect
    assert _value(_scrape(client), m.REQUIRED) == 1.0


@pytest.mark.parametrize("pulled_ago", [timedelta(days=34), timedelta(0)])
async def test_a_revoked_pass_pulled_after_its_ack_over_30_days_ago_requires_no_sync(
    clean, client, pulled_ago
):
    """Once a pull has completed after the ack, the ingest host can hold nothing more for the pass
    (it refuses uploads from the ack on), so the 30-day window from the ack is all that remains."""
    _cursor(datetime.now(UTC) - pulled_ago)
    pass_id = _pass("p1", revoked=True, revoked_ago=timedelta(days=40), version=2)
    _synced(pass_id, 2, acked_ago=timedelta(days=35))
    assert _value(_scrape(client), m.REQUIRED) == 0.0


@pytest.mark.parametrize(
    "known, last_sync_ago, required",
    [
        (True, timedelta(days=41), 1.0),  # no pull completed since the pass expired
        (True, None, 1.0),  # no tick ever completed
        (True, timedelta(0), 0.0),  # pulled since the expiry, which is over 30 days ago
        (False, timedelta(days=41), 0.0),  # never acked: the ingest host never held the pass
    ],
)
async def test_an_expired_pass_requires_sync_until_a_pull_completes_after_its_expiry(
    clean, client, known, last_sync_ago, required
):
    """Expiry has the same hole: a pass that expires during an outage of more than 30 days may
    have batches the ingest host accepted before the expiry and home has never pulled."""
    if last_sync_ago is not None:
        _cursor(datetime.now(UTC) - last_sync_ago)
    pass_id = _pass("p1", expires_in=-timedelta(days=40))
    if known:
        _synced(pass_id, 1, acked_ago=timedelta(days=45))
    assert _value(_scrape(client), m.REQUIRED) == required


@pytest.mark.parametrize(
    "state, kwargs, required",
    [
        ("open", {}, 1.0),
        ("complete", {}, 1.0),
        ("complete", {"acked": True}, 0.0),
        ("acked", {"acked": True}, 0.0),
        ("quarantined", {}, 1.0),
        ("quarantined", {"quarantine_delivered": True}, 0.0),
    ],
)
async def test_a_batch_home_still_owes_an_answer_requires_sync_with_no_live_pass(
    clean, client, state, kwargs, required
):
    _batch(_expired_pass(), "b1", state, **kwargs)
    assert _value(_scrape(client), m.REQUIRED) == required


# --- the snapshot: age from the stored last_sync_at, absent before the first sync -----------------


async def test_the_age_is_computed_from_the_stored_last_sync_at(clean, client):
    _cursor(datetime.now(UTC) - timedelta(seconds=1000))
    age = _value(_scrape(client), m.SNAPSHOT_AGE)
    assert age is not None
    assert 1000 <= age < 1060


async def test_no_snapshot_row_leaves_the_age_absent_and_every_count_at_zero(clean, client):
    """The age is the one series with no honest value before the first sync: 0 would read as
    fresh, a large number would invent a time. Absence is what `GuestSyncAbsent` keys on. The
    counts and `required` come from the tables, which answer whether or not a sync ever ran."""
    _pass("p1")
    body = _scrape(client)
    assert _absent(body, m.SNAPSHOT_AGE)
    assert _value(body, m.REQUIRED) == 1.0
    assert _value(body, m.PULL_LAG) == 0.0
    assert _value(body, m.BLOBS_PENDING) == 0.0
    assert _value(body, m.QUARANTINED) == 0.0


async def test_a_row_that_never_synced_leaves_the_age_absent(clean, client):
    _cursor(None, "link: GET /ingest/devices: ConnectError")
    body = _scrape(client)
    assert _absent(body, m.SNAPSHOT_AGE)
    assert _value(body, m.REQUIRED) == 0.0


async def test_a_failing_sweep_records_last_error_and_keeps_last_sync_at(clean, client):
    """The real boundary guard against a link that is down: the error is recorded, the stored
    sync time is not moved, and the published age keeps growing from it."""
    then = datetime.now(UTC) - timedelta(seconds=2000)
    _cursor(then)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("link down", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(down), base_url="http://pluto"
    ) as link:
        assert await trip_ingest.run_sync_safe(link) == "link_down"

    [(last_sync_at, last_error)] = _sql(
        "SELECT last_sync_at, last_error FROM trip_ingest_cursor WHERE id = 1"
    )
    assert last_sync_at == then
    assert last_error is not None and last_error.startswith("link:")
    age = _value(_scrape(client), m.SNAPSHOT_AGE)
    assert age is not None and age >= 2000


async def test_a_successful_sweep_resets_the_age(clean, client):
    _cursor(datetime.now(UTC) - timedelta(seconds=2000))

    def empty(request: httpx.Request) -> httpx.Response:
        key = "devices" if request.url.path.endswith("/devices") else "batches"
        # A stream, not `json=`: the puller reads the raw body, which a pre-read response refuses.
        return httpx.Response(
            200, stream=httpx.ByteStream(json.dumps({key: [], "next": 0}).encode())
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(empty), base_url="http://pluto"
    ) as link:
        assert await trip_ingest.run_sync_safe(link) == "ok"
    age = _value(_scrape(client), m.SNAPSHOT_AGE)
    assert age is not None and age < 60


# --- counts and lag -------------------------------------------------------------------------------


async def test_pending_blobs_quarantined_batches_and_the_pull_lag(clean, client):
    p = _pass("p1")
    old = _batch(p, "b-old", "open", age=timedelta(hours=2))
    _blob(old, 1, filed=True)  # a part already filed is not pending
    _blob(old, 2, filed=False)
    _blob(old, 3, filed=False)
    young = _batch(p, "b-young", "open", age=timedelta(minutes=5))
    _blob(young, 4, filed=False)
    done = _batch(p, "b-done", "complete")  # filed; its blobs are not pending
    _blob(done, 5, filed=False)
    _batch(p, "b-q1", "quarantined")
    _batch(p, "b-q2", "quarantined", quarantine_delivered=True)
    _batch(p, "b-acked", "acked", acked=True)

    body = _scrape(client)
    assert _value(body, m.BLOBS_PENDING) == 3.0
    assert _value(body, m.QUARANTINED) == 2.0
    lag = _value(body, m.PULL_LAG)
    assert lag is not None and 7200 <= lag < 7260


async def test_with_nothing_waiting_the_lag_is_zero(clean, client):
    p = _pass("p1")
    _batch(p, "b-acked", "acked", acked=True, age=timedelta(days=2))
    body = _scrape(client)
    assert _value(body, m.PULL_LAG) == 0.0
    assert _value(body, m.BLOBS_PENDING) == 0.0


# --- failure at scrape time ----------------------------------------------------------------------


async def test_a_database_that_cannot_answer_drops_every_guest_family_and_nothing_else(
    clean, client, monkeypatch
):
    """A scrape that cannot read the tables must not publish a guess, and must not take the rest
    of `/metrics` down with it: every guest family goes absent, which `GuestSyncAbsent` keys on."""
    _cursor(datetime.now(UTC))
    _pass("p1")
    before = _scrape(client)
    assert not any(_absent(before, name) for name in FAMILIES)
    url = m.async_session.kw["bind"].url.set(database="memosng_no_such_database")
    broken = create_async_engine(url, poolclass=NullPool)
    monkeypatch.setattr(m, "async_session", async_sessionmaker(broken))
    try:
        body = _scrape(client)
    finally:
        await broken.dispose()
    for name in FAMILIES:
        assert _absent(body, name), name


async def test_a_pass_that_expired_after_the_last_sync_keeps_the_stale_alert_armed(clean, client):
    """The gate's scenario: a guest completes a batch after the last sync, the worker stops, the
    only pass expires. Home holds no row for that batch, so only the pass can say a sync is owed;
    `GuestSyncStale` needs age > 900 AND required == 1."""
    _cursor(datetime.now(UTC) - timedelta(hours=2))
    _pass("p1", expires_in=-timedelta(hours=1))
    body = _scrape(client)
    age = _value(body, m.SNAPSHOT_AGE)
    assert age is not None and age > 900
    assert _value(body, m.REQUIRED) == 1.0


async def test_a_scrape_that_outlives_its_bound_publishes_nothing(clean, client, monkeypatch):
    """A read blocked behind a lock is cut off at the bound: the scrape answers in time, without
    the guest families. The lock is released only well after the bound."""
    _cursor(datetime.now(UTC))
    before = _scrape(client)
    assert not any(_absent(before, name) for name in FAMILIES)
    monkeypatch.setattr(m, "SCRAPE_TIMEOUT_S", 0.5)
    holder = create_engine(SYNC_URL)
    conn = holder.connect()
    tx = conn.begin()
    conn.execute(text("LOCK TABLE trip_ingest_cursor IN ACCESS EXCLUSIVE MODE"))
    release = threading.Timer(6.0, tx.rollback)
    release.start()
    try:
        started = time.monotonic()
        body = _scrape(client)
        elapsed = time.monotonic() - started
    finally:
        release.join()
        conn.close()
        holder.dispose()
    assert elapsed < 3.0, elapsed
    for name in FAMILIES:
        assert _absent(body, name), name


def test_the_collector_is_registered_once_and_its_samples_reach_the_scrape(clean, client):
    from prometheus_client import REGISTRY

    first = m.register_once(REGISTRY)
    assert m.register_once(REGISTRY) is first
    assert first in REGISTRY._collector_to_names  # registered, not merely constructed
    body = _scrape(client)
    assert _value(body, m.REQUIRED) == 0.0
    assert _value(body, m.QUARANTINED) == 0.0


async def test_overlapping_scrapes_each_publish_every_family(clean, client):
    """Two scrapers (or a scrape and its retry) at once: each read is its own statement, so
    neither may cost the other its families."""
    _cursor(datetime.now(UTC))
    _pass("p1")
    bodies: list[str] = []
    gate = threading.Barrier(4)

    def scrape() -> None:
        gate.wait()
        bodies.append(_scrape(client))

    threads = [threading.Thread(target=scrape) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(bodies) == 4
    for body in bodies:
        assert not any(_absent(body, name) for name in FAMILIES)
