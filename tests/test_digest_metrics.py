"""The digest metrics: the snapshot, the collector, and the absence guard (MEMNG-234).

Two properties are the whole point and both are asserted against the real `/metrics` body:

* a scrape does **no database work** (ADR-0009 — the constraint the pool-exhaustion fix left
  behind), so the collector reads the snapshot and nothing else;
* a **stale snapshot publishes nothing**. A refresher that died would otherwise freeze the last
  snapshot, and a snapshot frozen at `enabled=0` makes `MemosDigestStale` (which requires `== 1`)
  and `MemosDigestMetricsAbsent` (which requires absence) both silent forever — exactly the
  silent-failure class this feature exists to end. The series must go ABSENT, not stale.
"""

import asyncio
import secrets
import time
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestTrigger, User, Visibility
from app.services.digest import metrics as digest_metrics
from app.services.digest import runner as digest_runner
from sqlalchemy import delete, text
from structlog.testing import capture_logs

from tests.digest_support import (
    FakeTransport,
    add_recipients,
    reset_digest_state,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_mobile,
)

NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
SENDER = "sender@example.com"


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    digest_metrics.set_snapshot(None)
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients("a@example.com")
    set_digest_settings(enabled=True, start_date="2026-06-01")
    subject = f"digest-metrics-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        digest_metrics.set_snapshot(None)
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


def _names(families) -> list[str]:
    return [family.name for family in families]


def _sample(families, name: str, labels: dict | None = None) -> float:
    for family in families:
        for sample in family.samples:
            if sample.name == name and (labels is None or sample.labels == labels):
                return sample.value
    raise AssertionError(f"{name} {labels or ''} is not published: {_names(families)}")


async def _insert_run(
    trigger: str, status: str | None, *, minutes_ago: int = 0, local_date: str = "2026-07-01"
) -> int:
    """One run row, at a stated local date — the scheduled-day partial index means two `scheduled`
    rows cannot share one."""
    # `finished_at` is composed in Python rather than bound twice: asyncpg cannot infer the type
    # of a parameter that appears only inside a CASE predicate.
    finished = "NULL" if status is None else f"now() - make_interval(mins => {int(minutes_ago)})"
    async with async_session() as db:
        run_id = await db.scalar(
            text(
                "INSERT INTO digest_runs (trigger, local_date, status, finished_at) VALUES "
                f"(:trigger, :local_date, :status, {finished}) RETURNING id"
            ),
            {"trigger": trigger, "status": status, "local_date": date.fromisoformat(local_date)},
        )
        await db.commit()
    assert run_id is not None
    return int(run_id)


# --- the snapshot ------------------------------------------------------------------------------


async def test_the_snapshot_reads_enabled_and_the_run_counts(env):
    await _insert_run(DigestTrigger.scheduled, "sent", local_date="2026-07-01")
    await _insert_run(DigestTrigger.scheduled, "failed", local_date="2026-07-02")
    await _insert_run(DigestTrigger.manual, "sent")
    snapshot = await digest_metrics.refresh_now()
    assert snapshot.enabled is True
    assert snapshot.runs_by_status == {"sent": 2, "failed": 1}
    assert snapshot.last_success > 0


async def test_an_in_flight_run_contributes_to_no_label_until_it_ends(env):
    """A DB-derived Counter may not move backwards, so a row whose outcome is unknown must not be
    counted under any status."""
    run_id = await _insert_run(DigestTrigger.scheduled, None)
    snapshot = await digest_metrics.refresh_now()
    assert snapshot.runs_by_status == {}
    assert snapshot.last_success == 0.0
    async with async_session() as db:
        await digest_runner.finish_run(db, run_id, status=digest_runner.SENT)
    after = await digest_metrics.refresh_now()
    assert after.runs_by_status == {"sent": 1}
    assert after.last_success > 0


async def test_last_success_ignores_test_and_import_triggers(env):
    """B5/B7: an import writes history and a test-send goes to one admin address. Either reading as
    an SMTP success would silence the 26 h stale alert with a run that mailed the household
    nothing."""
    await _insert_run(DigestTrigger.import_, "sent")
    await _insert_run(DigestTrigger.test, "sent", local_date="2026-07-02")
    snapshot = await digest_metrics.refresh_now()
    assert snapshot.last_success == 0.0
    assert snapshot.runs_by_status == {"sent": 2}  # they still COUNT as runs
    await _insert_run(DigestTrigger.scheduled, "sent")
    assert (await digest_metrics.refresh_now()).last_success > 0


async def test_nothing_to_send_counts_as_a_success(env):
    """A quiet day is a working digest, not a stale one — the alert must not fire on it."""
    await _insert_run(DigestTrigger.scheduled, "nothing_to_send")
    assert (await digest_metrics.refresh_now()).last_success > 0


async def test_a_failed_run_does_not_advance_last_success(env):
    await _insert_run(DigestTrigger.scheduled, "failed")
    assert (await digest_metrics.refresh_now()).last_success == 0.0


# --- the collector -----------------------------------------------------------------------------


async def test_the_collector_publishes_the_families_the_alerts_need(env):
    await _insert_run(DigestTrigger.scheduled, "sent")
    await digest_metrics.refresh_now()
    families = digest_metrics.families()
    assert digest_metrics.ENABLED in _names(families)
    assert digest_metrics.LAST_SUCCESS in _names(families)
    assert digest_metrics.RUNS in _names(families)
    assert _sample(families, digest_metrics.ENABLED) == 1.0
    assert _sample(families, "memosng_digest_runs_total", {"status": "sent"}) == 1.0


async def test_last_success_is_published_as_zero_when_there_has_never_been_one(env):
    """Always published: the stale rule and every `/api/v1/query` read-back must work from day one,
    and a missing series is indistinguishable from a broken exporter."""
    await digest_metrics.refresh_now()
    assert _sample(digest_metrics.families(), digest_metrics.LAST_SUCCESS) == 0.0


async def test_a_disabled_digest_publishes_enabled_zero(env):
    set_digest_settings(enabled=False)
    await digest_metrics.refresh_now()
    assert _sample(digest_metrics.families(), digest_metrics.ENABLED) == 0.0


def test_a_missing_snapshot_publishes_nothing():
    digest_metrics.set_snapshot(None)
    assert digest_metrics.families() == []


def test_a_stale_snapshot_publishes_NOTHING_rather_than_a_frozen_value():
    """The guard. A frozen `enabled=0` would silence both alert rules forever; an ABSENT series
    fires `MemosDigestMetricsAbsent`."""
    fresh = digest_metrics.Snapshot(
        taken_at=time.time(), enabled=True, last_success=1.0, runs_by_status={"sent": 1}
    )
    digest_metrics.set_snapshot(fresh)
    assert _names(digest_metrics.families())  # published while fresh
    stale = replace(fresh, taken_at=time.time() - digest_metrics.SNAPSHOT_MAX_AGE_S - 1)
    digest_metrics.set_snapshot(stale)
    assert digest_metrics.families() == []


def test_the_snapshot_age_is_published_for_diagnosis():
    digest_metrics.set_snapshot(
        digest_metrics.Snapshot(taken_at=time.time() - 5, enabled=True, last_success=0.0)
    )
    age = _sample(digest_metrics.families(), digest_metrics.SNAPSHOT_AGE)
    assert 4.0 <= age <= 60.0


# --- the refresher -----------------------------------------------------------------------------


async def test_an_exception_in_one_refresh_cycle_is_logged_and_the_next_still_runs(env):
    """The task must not die: a dead refresher freezes the snapshot, and only the collector's
    staleness check would then be left between that and a silent failure."""
    cycles = {"n": 0}

    class _FlakyFactory:
        def __call__(self):
            cycles["n"] += 1
            if cycles["n"] == 1:
                raise RuntimeError("simulated database outage")
            return async_session()

    monkeypatched = _FlakyFactory()
    task = asyncio.create_task(
        digest_metrics.refresher(  # type: ignore[arg-type]
            monkeypatched, interval=0.01, backoff=0.01
        )
    )
    with capture_logs() as logs:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if cycles["n"] >= 2 and digest_metrics.current() is not None:
                break
    task.cancel()
    assert cycles["n"] >= 2, "the refresher stopped after the failing cycle"
    assert digest_metrics.current() is not None
    assert any(entry["event"] == "digest.metrics_refresh_error" for entry in logs)


async def test_the_refresher_backs_off_rather_than_spinning():
    assert digest_metrics.REFRESH_BACKOFF_S < digest_metrics.REFRESH_INTERVAL_S


# --- the real /metrics endpoint ------------------------------------------------------------------


async def test_the_families_are_exposed_on_the_apps_metrics_endpoint_and_change_after_a_run(
    env, client
):
    """AC 9 as amended. The scrape is the REAL endpoint, and the positive control proves the body
    was actually read rather than an empty string matching an empty expectation."""
    subject, data_root = env
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db,
            user,
            content="hello",
            visibility=Visibility.PUBLIC,
            created_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
        attachment = await seed_attachment(db, user, memo, mime="image/jpeg")
        write_mobile(data_root, attachment.uid)
        await db.commit()

    await digest_metrics.refresh_now()
    before = client.get("/metrics")
    assert before.status_code == 200
    body = before.text
    assert "python_gc" in body, "positive control: the metrics body was not read"
    digest_lines = [line for line in body.splitlines() if line.startswith("memosng_digest_")]
    assert len(digest_lines) >= 3, digest_lines
    # The HELP/TYPE header is emitted for an empty family; what must be absent is a SAMPLE.
    assert "memosng_digest_runs_total{" not in body  # no terminal run yet

    await digest_runner.run_digest(transport=FakeTransport(), now_utc=NOW)
    await digest_metrics.refresh_now()
    after = client.get("/metrics").text
    assert 'memosng_digest_runs_total{status="sent"} 1.0' in after
    last_success = [
        line
        for line in after.splitlines()
        if line.startswith("memosng_digest_last_success_timestamp_seconds ")
    ]
    assert last_success and float(last_success[0].split()[1]) > 0


async def test_a_stale_snapshot_removes_the_families_from_the_real_scrape(env, client):
    await digest_metrics.refresh_now()
    assert "memosng_digest_enabled" in client.get("/metrics").text
    snapshot = digest_metrics.current()
    assert snapshot is not None
    digest_metrics.set_snapshot(
        replace(snapshot, taken_at=time.time() - digest_metrics.SNAPSHOT_MAX_AGE_S - 1)
    )
    body = client.get("/metrics").text
    assert "python_gc" in body, "positive control: the metrics body was not read"
    assert "memosng_digest_" not in body


def test_registering_the_collector_twice_is_a_no_op():
    """`app.main` is imported repeatedly by the test suite's fixtures, and prometheus_client
    refuses a duplicate collector by RAISING — which would turn a second import into a crash."""
    from prometheus_client import REGISTRY

    first = digest_metrics.register_once(REGISTRY)
    second = digest_metrics.register_once(REGISTRY)
    assert first is second
