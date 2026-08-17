"""MEMNG-86: the worker must not drain against a schema older than the head its image expects.

The gate is a read-only poll of alembic_version (app/migrations.py). These pin: it reads the real
head, it clears immediately when the schema is current, it FAILS (raises) on a schema that never
catches up, the worker loop is gated on it, and only the app entrypoint runs alembic.
"""

import inspect
from pathlib import Path

import pytest
from app import migrations, worker

REPO = Path(__file__).resolve().parent.parent


async def test_expected_head_matches_the_migrated_db(db_engine):
    # expected_head() reads the migration scripts bundled in the image; current_db_head() reads the
    # live alembic_version. On a DB migrated to head (the fixture) they MUST agree — a bug that read
    # the wrong script location or a branch head would diverge here.
    head = migrations.expected_head()
    assert head and isinstance(head, str)
    assert await migrations.current_db_head() == head


async def test_wait_returns_immediately_when_schema_is_current(db_engine):
    # Steady state / a no-migration deploy: the very first poll matches, so the gate clears at once.
    got = await migrations.wait_for_migrated_schema(budget_s=5, interval=0.05)
    assert got == migrations.expected_head()


async def test_wait_raises_when_schema_never_reaches_expected_head(db_engine, monkeypatch):
    # The core AC: a worker whose code expects a head the DB will never reach does NOT proceed — it
    # exhausts the budget and raises (the worker then exits and Docker restarts it). Simulated by
    # pinning the expected head to a revision the live DB is not at.
    monkeypatch.setattr(migrations, "expected_head", lambda: "deadbeefface_not_a_real_head")
    with pytest.raises(migrations.SchemaGateTimeout):
        await migrations.wait_for_migrated_schema(budget_s=0.3, interval=0.05)


async def test_current_db_head_returns_none_on_db_error(monkeypatch):
    # An un-migrated schema (no alembic_version) or a transient connection blip must read as None so
    # the gate keeps polling rather than crashing on the first miss. Exercised by making the session
    # dependency raise a SQLAlchemyError (the class ProgrammingError/OperationalError belong to).
    from sqlalchemy.exc import SQLAlchemyError

    class _BoomSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *a, **k):
            raise SQLAlchemyError("relation \"alembic_version\" does not exist")

    monkeypatch.setattr(migrations, "async_session", lambda: _BoomSession())
    assert await migrations.current_db_head() is None


def test_worker_run_gates_on_schema_before_recovery_and_loop():
    # Wiring pin (like the frame-share recovery pins): the gate must sit BEFORE the startup recovery
    # sweep and the drain loop, or it guards nothing.
    src = inspect.getsource(worker.run)
    head, sep, tail = src.partition("wait_for_migrated_schema()")
    assert sep, "worker.run no longer calls the schema gate"
    assert "run_frame_share_recover_safe()" in tail, "recovery sweep must run AFTER the schema gate"
    assert "while True:" in tail, "the drain loop must run AFTER the schema gate"


def test_only_the_app_entrypoint_runs_alembic():
    # Single-migrator invariant (MEMNG-86): the worker never runs alembic, so two containers can
    # never migrate at once. The worker branch execs the worker with no migration; the app path is
    # the sole `alembic upgrade head`.
    entrypoint = (REPO / "docker-entrypoint.sh").read_text()
    worker_branch, sep, app_path = entrypoint.partition("exec python -m app.worker")
    assert sep, "docker-entrypoint.sh no longer has the worker exec this pin anchors on"
    assert "alembic" not in worker_branch, "the worker path must NOT run alembic"
    assert app_path.count("alembic upgrade head") == 1, "the app path is the one and only migrator"
