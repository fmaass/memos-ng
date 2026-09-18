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
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from tests import db_guard
from tests.conftest import SYNC_URL
from tests.db_guard import (
    APPLICATION_NAME_PREFIX,
    BOOTSTRAP_LOCK_KEY,
    DEV_DB_CONTAINER,
    LOCK_HANDOFF_GRACE_SECONDS,
)

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

# Re-entry after the lock connection died. The claim is made, its backend is terminated from a
# second connection (what a Postgres restart does to every session at once), and the claim is
# then made again — with or without a sibling that grabbed the database in between.
REENTRY_PROBE = """
import os
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from tests import db_guard

dsn = os.environ["DATABASE_URL_SYNC"]
mode = sys.argv[1]

assert db_guard.acquire_bootstrap_lock(dsn) is True
lock_conn = db_guard._lock_connection
assert lock_conn is not None
backend_pid = lock_conn.execute(text("SELECT pg_backend_pid()")).scalar()

killer = create_engine(dsn, poolclass=NullPool, isolation_level="AUTOCOMMIT")
with killer.connect() as conn:
    conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid})

sibling = create_engine(
    dsn,
    poolclass=NullPool,
    isolation_level="AUTOCOMMIT",
    connect_args={"application_name": db_guard._application_name()},
)
if mode == "sibling":
    held = sibling.connect()
    deadline = time.monotonic() + 30
    while not held.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": db_guard.BOOTSTRAP_LOCK_KEY}
    ).scalar():
        assert time.monotonic() < deadline, "the terminated backend never let the lock go"
        time.sleep(0.05)
    print("SIBLING HOLDS IT")

try:
    print("RETURNED:", db_guard.acquire_bootstrap_lock(dsn))
except RuntimeError as exc:
    print("REFUSED:", exc)
"""

# A module that never opens the database — the case that has to survive a stopped Postgres.
DATABASE_FREE_SESSION = """
def test_needs_no_database():
    assert 2 + 2 == 4
"""

# Session start never runs here: a plain interpreter, driving the fixture body that drops the
# schema, with a sibling already holding the lock. It is the point-of-use claim under test, so
# the marker table printed at the end is the proof that no drop happened.
FIXTURE_CLAIM_PROBE = """
import os

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

import tests.conftest as conftest
from tests import db_guard
from tests.db_guard import BOOTSTRAP_LOCK_KEY

dsn = os.environ["DATABASE_URL_SYNC"]
sibling = create_engine(
    dsn,
    poolclass=NullPool,
    isolation_level="AUTOCOMMIT",
    connect_args={"application_name": db_guard._application_name()},
)
held = sibling.connect()
assert held.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY}).scalar()

marker = create_engine(dsn, poolclass=NullPool)
with marker.begin() as conn:
    conn.execute(text("CREATE TABLE lock_marker (note text)"))
    conn.execute(text("INSERT INTO lock_marker VALUES ('the sibling was here')"))

try:
    next(conftest.db_engine.__wrapped__())
except RuntimeError as exc:
    print("REFUSED:", exc)
else:
    print("ADMITTED: the fixture bootstrapped without holding the lock")
with marker.connect() as conn:
    print("MARKER:", conn.execute(text("SELECT to_regclass('public.lock_marker')")).scalar())
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
            # Nothing was collected or run: a session that got as far as running a test prints
            # an outcome summary ("1 passed in 0.3s", "1 failed in 0.3s") whatever the verdict.
            assert not re.search(r"\b\d+ (passed|failed|error|skipped|xfailed)\b", output), output
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


def _closed_local_port() -> int:
    """A localhost port with nothing listening: a connect there is refused, not hung."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_a_database_free_run_survives_a_database_that_is_not_there(tmp_path: Path):
    # Before the lock existed, `pytest tests/test_db_guard.py`, `--collect-only` and any run
    # that never opens Postgres worked with the server stopped. A claim that cannot be made is
    # not a refusal: nobody else can be holding a database nothing can reach.
    test_file = tmp_path / "test_database_free.py"
    test_file.write_text(DATABASE_FREE_SESSION)
    port = _closed_local_port()
    dead = make_url(SYNC_URL).set(host="127.0.0.1", port=port, database="memosng_test_no_server")
    env = {
        **os.environ,
        "DATABASE_URL_SYNC": dead.render_as_string(hide_password=False),
        "DATABASE_URL": dead.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        _session_argv(test_file),
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout


def test_the_drop_path_refuses_even_when_session_start_never_claimed(tmp_path: Path):
    # The invariant is not "a session claims the database" but "no DROP SCHEMA without the
    # claim". This drives the fixture body in a process where no session hook ever ran.
    probe = tmp_path / "fixture_claim_probe.py"
    probe.write_text(FIXTURE_CLAIM_PROBE)
    with _scratch_database() as database:
        done = subprocess.run(
            [sys.executable, str(probe)],
            cwd=REPO_ROOT,
            env=_session_env(database),
            capture_output=True,
            text=True,
            timeout=LOCK_HANDOFF_GRACE_SECONDS + 120,
        )
        output = done.stdout + done.stderr
        assert done.returncode == 0, output
        assert "REFUSED: Refusing the destructive test bootstrap" in output, output
        # And it refused instead of dropping: the sibling's table is still there.
        assert "MARKER: lock_marker" in output, output


@contextlib.contextmanager
def _lock_held_as(database: str, application_name: str) -> Iterator[int]:
    """Hold the bootstrap lock on `database` under a chosen application_name; yield backend pid."""
    engine = create_engine(
        _scratch_url(database, "postgresql+psycopg2"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"application_name": application_name},
    )
    conn = engine.connect()
    try:
        assert conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": BOOTSTRAP_LOCK_KEY}
        ).scalar()
        yield int(conn.execute(text("SELECT pg_backend_pid()")).scalar() or 0)
    finally:
        conn.close()
        engine.dispose()


def _refusal_against(database: str) -> str:
    engine = create_engine(_scratch_url(database, "postgresql+psycopg2"), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return db_guard._refusal_message(database, conn)
    finally:
        engine.dispose()


def _reentry_probe(tmp_path: Path, mode: str) -> str:
    probe = tmp_path / "reentry_probe.py"
    probe.write_text(REENTRY_PROBE)
    with _scratch_database() as database:
        done = subprocess.run(
            [sys.executable, str(probe), mode],
            cwd=REPO_ROOT,
            env=_session_env(database),
            capture_output=True,
            text=True,
            timeout=LOCK_HANDOFF_GRACE_SECONDS + 120,
        )
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    return output


def test_a_claim_whose_connection_died_is_not_trusted_when_a_sibling_took_over(tmp_path: Path):
    # Holding the lock is a property of a live session, not of this process's memory: once the
    # backend is gone the database is anyone's, and re-entry must find that out from Postgres.
    output = _reentry_probe(tmp_path, "sibling")
    assert "SIBLING HOLDS IT" in output, output
    assert "REFUSED: Refusing the destructive test bootstrap" in output, output
    assert "RETURNED:" not in output, output


def test_a_claim_whose_connection_died_is_taken_again_when_nobody_else_holds_it(tmp_path: Path):
    # The other half: re-entry after a lost connection is not an error, it is a fresh claim.
    output = _reentry_probe(tmp_path, "alone")
    assert "RETURNED: True" in output, output
    assert "REFUSED" not in output, output


def test_a_live_holder_is_described_as_a_run_to_wait_for():
    with _scratch_database() as database:
        with _lock_held_as(database, db_guard._application_name()):
            message = _refusal_against(database)
    assert "is already claimed by" in message
    assert "export both" in message
    assert "is not running on this host" not in message


def test_a_holder_whose_process_is_gone_is_named_as_gone_with_the_remedy():
    # A real lingering backend: the lock is held by a connection whose application_name carries
    # a pid that has provably exited, which is what a SIGKILLed run leaves behind.
    corpse = subprocess.Popen([sys.executable, "-c", "pass"])
    corpse.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(corpse.pid, 0)
    with _scratch_database() as database:
        with _lock_held_as(database, db_guard._application_name(corpse.pid)) as backend_pid:
            message = _refusal_against(database)
    assert f"process {corpse.pid} is not running on this host" in message
    assert f"pg_terminate_backend({backend_pid})" in message
    assert DEV_DB_CONTAINER in message
    # It says the run is gone rather than telling the reader to wait for it.
    assert "the other run is not using" not in message


def test_a_dead_pid_from_another_namespace_keeps_the_live_wording():
    # Two runners in separate pid namespaces behind one client address look identical to
    # Postgres. The pid of a LIVE holder over there may well be free over here, and naming its
    # backend in a `pg_terminate_backend` line would be an instruction to kill a running suite.
    corpse = subprocess.Popen([sys.executable, "-c", "pass"])
    corpse.wait()
    elsewhere = f"{APPLICATION_NAME_PREFIX}{corpse.pid}@pid:[4026500000]"
    assert elsewhere != db_guard._application_name(corpse.pid)
    with _scratch_database() as database:
        with _lock_held_as(database, elsewhere):
            message = _refusal_against(database)
    assert "is already claimed by" in message
    assert "is not running on this host" not in message


def test_a_holder_without_a_namespace_in_its_name_keeps_the_live_wording():
    # An older run, or a platform where /proc/self/ns/pid cannot be read: nothing about its pid
    # is checkable, so it is described as the live run it may well be.
    corpse = subprocess.Popen([sys.executable, "-c", "pass"])
    corpse.wait()
    with _scratch_database() as database:
        with _lock_held_as(database, f"{APPLICATION_NAME_PREFIX}{corpse.pid}"):
            message = _refusal_against(database)
    assert "is already claimed by" in message
    assert "is not running on this host" not in message


def test_the_application_name_stays_inside_what_postgres_keeps():
    name = db_guard._application_name(1234567)
    assert name.startswith(f"{APPLICATION_NAME_PREFIX}1234567")
    assert len(name.encode()) <= db_guard.APPLICATION_NAME_MAX_BYTES, name


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
