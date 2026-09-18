"""Two real pytest sessions, one database: the second must refuse, not drop the first's schema.

The allowlist tests live next door in test_db_guard.py and are pure — they read URLs. This
module is the other guard, and it cannot be proven in-process: what has to be observed is one
run REFUSING while another run is mid-flight, which takes two interpreters, a real Postgres and
a real advisory lock.

SAFETY: every session started here is aimed at a throwaway database this module creates and
drops, never at the database the suite that runs this test is itself using. In the red case the
second session is precisely the thing that drops a live `public` schema, so pointing it at the
suite's own database would destroy the run asserting the behaviour.
"""

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from tests.conftest import SYNC_URL
from tests.db_guard import APPLICATION_NAME_PREFIX, LOCK_HANDOFF_GRACE_SECONDS

REPO_ROOT = Path(__file__).resolve().parent.parent
READY_TIMEOUT = 120.0
# The parked session waits for the release file; the bound is its own dead-man switch, so a test
# that dies without reaching its teardown cannot leave a pytest parked on this machine for ever.
PARK_TIMEOUT = 300.0

# The first session: it claims the database, plants a marker in `public`, then parks. Nothing
# here depends on the repo's fixtures — the marker is what a wrongly admitted second session
# would destroy, and it has to exist before that session starts.
PARKED_SESSION = """
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, text

READY = Path({ready!r})
RELEASE = Path({release!r})


def test_park_holding_the_bootstrap_lock():
    engine = create_engine(os.environ["DATABASE_URL_SYNC"])
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE lock_marker (note text)"))
        conn.execute(text("INSERT INTO lock_marker VALUES ('the first session was here')"))
    engine.dispose()
    READY.write_text(str(os.getpid()))
    deadline = time.monotonic() + {park_timeout!r}
    while time.monotonic() < deadline and not RELEASE.exists():
        time.sleep(0.05)
"""

# The second and third sessions. Requesting `db_engine` is the whole point: that fixture is what
# drops `public`, so a session admitted by mistake destroys the marker table.
BOOTSTRAPPING_SESSION = """
def test_bootstraps_the_schema(db_engine):
    assert db_engine is not None
"""


def _scratch_url(database: str, drivername: str) -> str:
    return (
        make_url(SYNC_URL)
        .set(drivername=drivername, database=database)
        .render_as_string(hide_password=False)
    )


@contextlib.contextmanager
def _scratch_database() -> Iterator[str]:
    """A database of this test's own, created and dropped through the suite's own connection."""
    name = f"memosng_test_lock_{os.getpid()}"
    assert name != make_url(SYNC_URL).database, "the throwaway name collided with the suite's own"
    admin = create_engine(SYNC_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield name
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _session_env(database: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL_SYNC": _scratch_url(database, "postgresql+psycopg2"),
        "DATABASE_URL": _scratch_url(database, "postgresql+asyncpg"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _session_argv(test_file: Path) -> list[str]:
    # `-p tests.conftest` loads the repo's conftest — its import-time guards, its session hooks
    # and its fixtures — for a test file that lives outside the repo, so nothing this test
    # generates is ever written into the working tree.
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.conftest",
        str(test_file),
    ]


def _run_session(
    test_file: Path, database: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _session_argv(test_file),
        cwd=REPO_ROOT,
        env=_session_env(database),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_holder_pid(ready: Path, proc: subprocess.Popen[str]) -> int:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if ready.exists():
            return int(ready.read_text())
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"the first session exited early ({proc.returncode}):\n{out}\n{err}")
        time.sleep(0.05)
    pytest.fail("the first session never reported that it holds the lock")


def test_a_second_session_refuses_instead_of_dropping_the_running_one_s_schema(tmp_path: Path):
    parked_file = tmp_path / "test_parked_session.py"
    bootstrap_file = tmp_path / "test_bootstrapping_session.py"
    ready = tmp_path / "holder.pid"
    release = tmp_path / "release"
    parked_file.write_text(
        PARKED_SESSION.format(ready=str(ready), release=str(release), park_timeout=PARK_TIMEOUT)
    )
    bootstrap_file.write_text(BOOTSTRAPPING_SESSION)

    with _scratch_database() as database:
        first = subprocess.Popen(
            _session_argv(parked_file),
            cwd=REPO_ROOT,
            env=_session_env(database),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            holder_pid = _wait_for_holder_pid(ready, first)

            second = _run_session(
                bootstrap_file, database, timeout=LOCK_HANDOFF_GRACE_SECONDS + 120
            )
            output = second.stdout + second.stderr

            # Refused as a usage error, before collection — not a failing test, and not a
            # traceback out of a half-run session.
            assert second.returncode == 4, output
            assert "Refusing the destructive test bootstrap" in output
            assert "passed" not in output, "a test ran; the refusal came too late"
            # The holder is named by the pid a human can act on, not only by a backend pid.
            assert f"{APPLICATION_NAME_PREFIX}{holder_pid}" in output

            # And the first session's schema is still standing.
            assert _marker_note(database) == "the first session was here"
        finally:
            release.write_text("go")
            try:
                first_out, first_err = first.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                first.kill()
                first_out, first_err = first.communicate()
        assert first.returncode == 0, f"{first_out}\n{first_err}"

        # Sequential, not concurrent: the same database, once the holder is gone, is admitted —
        # the handoff scripts/check-mutations.sh makes between modules must not be refused.
        third = _run_session(bootstrap_file, database, timeout=300)
        assert third.returncode == 0, third.stdout + third.stderr


def _marker_note(database: str) -> str | None:
    engine = create_engine(
        _scratch_url(database, "postgresql+psycopg2"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT to_regclass('public.lock_marker')")).scalar() is None:
                return None
            return conn.execute(text("SELECT note FROM lock_marker")).scalar()
    finally:
        engine.dispose()
