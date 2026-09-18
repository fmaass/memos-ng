"""MEMNG-86: the worker must not drain against a schema older than the head its image expects.

The gate is a read-only poll of alembic_version (app/migrations.py). These pin: it reads the real
head, it clears immediately when the schema is current, it FAILS (raises) on a schema that never
catches up, the worker loop is gated on it, and only the app entrypoint runs alembic.
"""

import inspect
import os
import subprocess
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app import migrations, worker

REPO = Path(__file__).resolve().parent.parent


def _fixed_head(value: str | None):
    async def _head() -> str | None:
        return value

    return _head


def _counting_head(value: str | None):
    """A stand-in `current_db_head` that records how often the gate actually polled it."""

    class _Head:
        calls = 0

        async def __call__(self) -> str | None:
            _Head.calls += 1
            return value

    return _Head()


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
            raise SQLAlchemyError('relation "alembic_version" does not exist')

    monkeypatch.setattr(migrations, "async_session", lambda: _BoomSession())
    assert await migrations.current_db_head() is None


def test_worker_run_gates_on_schema_before_recovery_and_loop():
    # Wiring pin (like the frame-share recovery pins): the gate must sit BEFORE the startup recovery
    # sweep and the drain loop, or it guards nothing. With the per-class consumer split the loop is
    # no longer the only thing that touches the schema — the consumer SPAWN is pinned behind the
    # gate too, or the consumers would claim against the old schema while the gate still waits.
    src = inspect.getsource(worker.run)
    head, sep, tail = src.partition("wait_for_migrated_schema()")
    assert sep, "worker.run no longer calls the schema gate"
    assert "run_frame_share_recover_safe()" in tail, "recovery sweep must run AFTER the schema gate"
    assert "while True:" in tail, "the supervisor loop must run AFTER the schema gate"
    assert "consume_media(" in tail, "the consumers must be spawned AFTER the schema gate"


def test_only_the_app_entrypoint_runs_alembic():
    # Single-migrator invariant (MEMNG-86): the worker never runs alembic, so two containers can
    # never migrate at once. The worker branch execs the worker with no migration; the app path is
    # the sole `alembic upgrade head`.
    entrypoint = (REPO / "docker-entrypoint.sh").read_text()
    worker_branch, sep, app_path = entrypoint.partition("exec python -m app.worker")
    assert sep, "docker-entrypoint.sh no longer has the worker exec this pin anchors on"
    assert "alembic" not in worker_branch, "the worker path must NOT run alembic"
    assert app_path.count("alembic upgrade head") == 1, "the app path is the one and only migrator"


# --- ROLLBACK_MODE: the previous image has to be able to start on a NEWER schema ----------------
#
# Expand-only migrations mean a rollback keeps the new columns and runs the old code against them.
# Two things stop that today: the app entrypoint runs `alembic upgrade head` unconditionally (the
# old image's graph does not contain the new revision → "Can't locate revision"), and this gate
# waits for a head EQUAL to the old image's (→ an endless wait). ROLLBACK_MODE=1 is what the NEXT
# rollback uses instead of a compose overlay; the overlay in RELEASING.md is for the one release
# that ships this flag, whose previous image does not have it yet.


async def test_the_gate_accepts_an_unknown_db_head_under_rollback_mode(monkeypatch):
    # A revision this image's graph has never heard of is, by construction, a NEWER one: nothing
    # else could have written it. Under ROLLBACK_MODE the worker proceeds against it.
    unknown = "ffffffffffff"
    monkeypatch.setattr(migrations, "current_db_head", _fixed_head(unknown))
    monkeypatch.setattr(migrations.settings, "ROLLBACK_MODE", True)
    assert await migrations.wait_for_migrated_schema(budget_s=5, interval=0.01) == unknown


async def test_the_gate_keeps_waiting_on_an_unknown_db_head_without_rollback_mode(monkeypatch):
    # The default is unchanged and still fails safe: without the flag the same schema is one this
    # code must not drain against, so the budget runs out and the worker exits for Docker to
    # restart. The poll counter is the proof it WAITED rather than returning some other way.
    polls = _counting_head("ffffffffffff")
    monkeypatch.setattr(migrations, "current_db_head", polls)
    monkeypatch.setattr(migrations.settings, "ROLLBACK_MODE", False)
    with pytest.raises(migrations.SchemaGateTimeout):
        await migrations.wait_for_migrated_schema(budget_s=0.3, interval=0.05)
    assert polls.calls > 1


async def test_rollback_mode_still_waits_for_a_head_the_graph_KNOWS(monkeypatch):
    # The flag is not "skip the gate". A DB at an OLDER known revision is a migration in flight —
    # exactly the case MEMNG-86 exists for — and the worker waits for it, flag or no flag.
    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    revisions = [rev.revision for rev in script.walk_revisions()]
    older = revisions[1]  # the revision before this image's head
    polls = _counting_head(older)
    monkeypatch.setattr(migrations, "current_db_head", polls)
    monkeypatch.setattr(migrations.settings, "ROLLBACK_MODE", True)
    with pytest.raises(migrations.SchemaGateTimeout):
        await migrations.wait_for_migrated_schema(budget_s=0.3, interval=0.05)
    assert polls.calls > 1


def _run_entrypoint(tmp_path: Path, **env: str) -> list[str]:
    """RUN docker-entrypoint.sh with alembic and uvicorn shimmed, and report what it invoked.

    Executed rather than grepped: the text of a shell script is not its behaviour, and the failure
    this guards against — a rollback deploy whose container migrates the database backwards and
    never serves — is a behaviour. The shims are first on PATH, log their argv and exit 0, so the
    script runs to its final `exec` in milliseconds and touches nothing.
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    log = tmp_path / "invoked.log"
    for name in ("alembic", "uvicorn"):
        shim = shim_dir / name
        shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "$INVOKED_LOG"\nexit 0\n')
        shim.chmod(0o755)

    result = subprocess.run(
        ["bash", "docker-entrypoint.sh"],
        cwd=REPO,
        env={
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "INVOKED_LOG": str(log),
            "HOME": str(tmp_path),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_the_entrypoint_skips_the_migration_under_rollback_mode(tmp_path):
    invoked = _run_entrypoint(tmp_path, ROLLBACK_MODE="1")

    assert not [line for line in invoked if line.startswith("alembic")], (
        "a rolled-back image migrating the database is the outage this flag exists to prevent"
    )
    # ...and it still SERVES. "Ran no migration" would also be true of a container that crashed.
    assert [line for line in invoked if line.startswith("uvicorn")]


def test_the_entrypoint_migrates_exactly_once_before_serving_without_the_flag(tmp_path):
    invoked = _run_entrypoint(tmp_path)

    assert [line.split()[0] for line in invoked] == ["alembic", "uvicorn"], invoked
    assert invoked[0] == "alembic upgrade head"
