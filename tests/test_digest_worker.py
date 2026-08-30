"""The digest inside the worker loop (MEMNG-234).

The property that matters is the one the whole background-task design exists for: **the digest is
off the worker's critical path.** The loop is serial — it drains a media job, drains a frame-share
job and stamps the heartbeat in one pass — while a digest run makes SMTP sessions and Dropbox calls
whose upload-session operations allow 600 s each. Awaited inline, one run would stall media work
for minutes and starve the heartbeat, and the worker healthcheck's 30-minute staleness bar plus
`autoheal=true` would eventually kill the container MID-SEND, which is exactly the crash window the
delivery guarantee is about.

So the test drives the REAL `run()` with a digest task that blocks, and asserts that media drains
and heartbeats keep happening while it is in flight.
"""

import asyncio
import secrets
from datetime import UTC, datetime, time

import pytest
from app import migrations, worker
from app.config import settings as app_settings
from app.database import async_session
from app.models import User, Visibility
from app.services.digest import runner as digest_runner
from sqlalchemy import delete
from structlog.testing import capture_logs

from tests.digest_support import (
    FakeTransport,
    add_recipients,
    reset_digest_state,
    run_rows,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_thumb,
)

SENDER = "sender@example.com"


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients("a@example.com")
    subject = f"digest-worker-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed_one_memo(subject: str, data_root) -> None:
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
        write_thumb(data_root, attachment.uid)
        await db.commit()


# --- the gate read ---------------------------------------------------------------------------


async def test_the_gate_is_false_while_the_digest_is_disabled(env):
    set_digest_settings(enabled=False)
    assert await worker.digest_is_due() is False


async def test_the_gate_fires_once_the_digest_is_armed_and_past_send_time(env):
    set_digest_settings(enabled=True, send_time="00:00")
    assert await worker.digest_is_due() is True


async def test_an_in_flight_run_makes_the_gate_false_before_the_index_has_to_decide(env):
    """The unique index is the guarantee; the day ledger counting an in-progress row is what makes
    "a second worker does not even try" the ordinary case rather than a race that usually loses."""
    set_digest_settings(enabled=True, send_time="00:00")
    async with async_session() as db:
        await digest_runner.open_run(
            db, trigger="scheduled", local_date=datetime.now(UTC).date()
        )
    assert await worker.digest_is_due() is False


async def test_a_test_run_does_not_count_as_todays_attempt(env):
    """B7: importing or probing on the morning of enablement must not swallow the first scheduled
    send."""
    set_digest_settings(enabled=True, send_time="00:00")
    async with async_session() as db:
        run_id = await digest_runner.open_run(
            db, trigger="test", local_date=datetime.now(UTC).date()
        )
        await digest_runner.finish_run(db, run_id, status=digest_runner.SENT)
    assert await worker.digest_is_due() is True


async def test_a_gate_error_answers_not_due_rather_than_propagating(env, monkeypatch):
    """A database hiccup on the gate read must never halt media processing — the same boundary
    guard every other periodic concern in this loop gets."""

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(digest_runner, "load_settings", boom)
    with capture_logs() as logs:
        assert await worker.digest_is_due() is False
    assert any(entry["event"] == "digest.gate_error" for entry in logs)


# --- the spawn -------------------------------------------------------------------------------


async def test_a_second_spawn_is_refused_while_one_is_in_flight(env, monkeypatch):
    """Single-flight is two-layered: this handle guards the PROCESS (cheap, immediate) and the
    committed run row guards across processes (durable, authoritative)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run():
        started.set()
        await release.wait()

    monkeypatch.setattr(digest_runner, "run_scheduled_digest_safe", slow_run)
    assert worker.spawn_digest() is True
    await started.wait()
    assert worker.digest_task_in_flight() is True
    assert worker.spawn_digest() is False
    release.set()
    await asyncio.sleep(0.05)
    assert worker.digest_task_in_flight() is False
    assert worker.spawn_digest() is True
    await asyncio.sleep(0.05)


# --- the loop --------------------------------------------------------------------------------


async def test_the_loop_keeps_draining_and_beating_while_a_digest_is_in_flight(env, monkeypatch):
    """The proof that the digest is off the worker's critical path.

    The fake transport's send blocks for far longer than a poll interval. If `run()` awaited the
    digest inline, the media drain count and the heartbeat count would both stop at the value they
    held when the digest started.
    """
    subject, data_root = env
    await _seed_one_memo(subject, data_root)
    set_digest_settings(enabled=True, send_time="00:00", start_date="2026-06-01")

    beats = {"n": 0}
    drains = {"n": 0}
    transport = FakeTransport(delay=1.0)

    async def counted_media_drain() -> str:
        drains["n"] += 1
        return "idle"

    async def idle_frame_drain() -> str:
        return "idle"

    async def noop(*args, **kwargs):
        return 0

    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "beat", lambda: beats.__setitem__("n", beats["n"] + 1))
    monkeypatch.setattr(worker, "run_media_drain", counted_media_drain)
    monkeypatch.setattr(worker, "run_frame_share_drain", idle_frame_drain)
    monkeypatch.setattr(worker, "run_frame_share_recover_safe", noop)
    monkeypatch.setattr(worker, "run_media_recover_safe", noop)
    monkeypatch.setattr(migrations, "wait_for_migrated_schema", noop)
    # The hourly reap block fires on the FIRST pass (`last_reap = 0.0`) and its four real sweeps
    # are not what this test is about — left in, their latency is what the loop would be measured
    # against instead of the digest's.
    monkeypatch.setattr(worker, "reap_expired_committed", noop)
    monkeypatch.setattr(worker, "sweep_job_progress_safe", noop)
    monkeypatch.setattr(worker, "run_commit_race_reconcile_safe", noop)
    monkeypatch.setattr(worker, "sweep_digest_safe", noop)

    # Bound BEFORE the patch, or the replacement would call itself: `spawn_digest` resolves the
    # name on the module at call time, which is exactly why the patch works at all.
    real_scheduled_run = digest_runner.run_scheduled_digest_safe

    async def scheduled_with_fake_smtp():
        return await real_scheduled_run(transport=transport)

    monkeypatch.setattr(
        digest_runner, "run_scheduled_digest_safe", scheduled_with_fake_smtp
    )

    loop_task = asyncio.create_task(worker.run())
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if worker.digest_task_in_flight():
                break
        assert worker.digest_task_in_flight(), "the gate never spawned a digest task"
        beats_at_spawn, drains_at_spawn = beats["n"], drains["n"]
        await asyncio.sleep(0.35)  # well inside the 1.0 s send
        assert worker.digest_task_in_flight(), "the digest finished too fast to prove anything"
        assert beats["n"] > beats_at_spawn, "the heartbeat stopped while the digest was in flight"
        assert drains["n"] > drains_at_spawn, "media drains stopped while the digest was in flight"
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not worker.digest_task_in_flight():
                break
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
    assert len(transport.sent) == 1
    assert run_rows()[0]["status"] == digest_runner.SENT


async def test_the_worker_sweeps_run_the_digests_two_periodic_jobs(env, monkeypatch):
    called: list[str] = []

    async def fake_stale(*args, **kwargs) -> int:
        called.append("stale")
        return 0

    async def fake_retention(*args, **kwargs) -> int:
        called.append("retention")
        return 0

    monkeypatch.setattr(digest_runner, "sweep_stale_runs", fake_stale)
    monkeypatch.setattr(digest_runner, "sweep_hosted_media_safe", fake_retention)
    await worker.sweep_digest_safe()
    assert called == ["stale", "retention"]


def test_the_gate_helper_uses_the_pure_function_and_not_its_own_clock_logic():
    """The worker reads two facts and asks the pure gate; the decision itself is not duplicated
    here, which is what makes the DST tests in `test_digest_gate.py` binding on production."""
    import inspect

    source = inspect.getsource(worker.digest_is_due)
    assert "digest_gate.digest_due(" in source
    assert "send_time" not in source  # no second copy of the comparison


def test_the_default_send_time_is_a_time_not_a_string():
    """A guard on the fixture helpers above: `set_digest_settings(send_time="00:00")` must land as
    a real TIME, or the gate comparison would be string-vs-time and raise."""
    from app.models import DigestSettings

    assert isinstance(DigestSettings.__table__.c.send_time.type.python_type(0, 0), time)


# --- the sweeps cannot stop the loop (review B2) ------------------------------------------------


async def test_the_digest_sweeps_go_through_the_isolated_entry_points(env, monkeypatch):
    """Both, not one: an exception escaping either propagates out of `run()` and stops the whole
    worker — heartbeat, media drain and frame drain with it."""
    called: list[str] = []

    async def fake_stale_safe(*args, **kwargs) -> int:
        called.append("stale_safe")
        return 0

    async def fake_retention(*args, **kwargs) -> int:
        called.append("retention_safe")
        return 0

    monkeypatch.setattr(digest_runner, "sweep_stale_runs_safe", fake_stale_safe)
    monkeypatch.setattr(digest_runner, "sweep_hosted_media_safe", fake_retention)
    await worker.sweep_digest_safe()
    assert called == ["stale_safe", "retention_safe"]


async def test_a_failing_stale_sweep_leaves_the_loop_running(env, monkeypatch):
    """Drives the REAL loop with a sweep that raises on every call. Before the boundary guard this
    propagated out of `run()` and the worker stopped stamping, draining and beating."""
    beats = {"n": 0}
    drains = {"n": 0}

    async def exploding_sweep(*args, **kwargs) -> int:
        raise RuntimeError("simulated database outage in the stale sweep")

    async def counted_media_drain() -> str:
        drains["n"] += 1
        return "idle"

    async def idle_frame_drain() -> str:
        return "idle"

    async def noop(*args, **kwargs):
        return 0

    async def never_due() -> bool:
        return False

    monkeypatch.setattr(digest_runner, "sweep_stale_runs", exploding_sweep)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "REAP_INTERVAL_SECONDS", 0)  # the reap block on EVERY pass
    monkeypatch.setattr(worker, "beat", lambda: beats.__setitem__("n", beats["n"] + 1))
    monkeypatch.setattr(worker, "run_media_drain", counted_media_drain)
    monkeypatch.setattr(worker, "run_frame_share_drain", idle_frame_drain)
    monkeypatch.setattr(worker, "run_frame_share_recover_safe", noop)
    monkeypatch.setattr(worker, "run_media_recover_safe", noop)
    monkeypatch.setattr(worker, "reap_expired_committed", noop)
    monkeypatch.setattr(worker, "sweep_job_progress_safe", noop)
    monkeypatch.setattr(worker, "run_commit_race_reconcile_safe", noop)
    monkeypatch.setattr(worker, "digest_is_due", never_due)
    monkeypatch.setattr(migrations, "wait_for_migrated_schema", noop)

    loop_task = asyncio.create_task(worker.run())
    try:
        with capture_logs() as logs:
            await asyncio.sleep(0.3)
        assert not loop_task.done(), "the loop stopped when the sweep raised"
        assert drains["n"] >= 3, drains
        assert beats["n"] >= 3, beats
        assert any(entry["event"] == "digest.stale_sweep_error" for entry in logs)
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
