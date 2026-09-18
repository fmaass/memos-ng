"""Refusal gate for the destructive test bootstrap.

The session fixture drops and recreates the `public` schema of whatever
DATABASE_URL_SYNC points at, and API-driven tests commit through the app's
async engine. A stray environment variable — a shell that still carries
production credentials, a copied .env — must never be enough to aim those
writes at a real database, so every target has to prove it is a local
throwaway before any engine connects.

The check refuses more than a bad authority: libpq can be rerouted past the
URL's host/database via query parameters (?host=, ?hostaddr=, ?dbname=,
?service=) and via environment overrides (PGHOSTADDR routes the connection
even when the URL names localhost), so any query string and any routing env
override is refused outright rather than enumerated.

Concurrent suite runs need one throwaway database each, because the session
fixture's DROP SCHEMA would otherwise pull the tables out from under a sibling
run. `memosng_test_<suffix>` names are therefore allowlisted alongside the
shared `memosng` — on the same local hosts and under the same query-string and
routing-override refusals.

TWO GUARDS, DIFFERENT HAZARDS. The allowlist above is about WHERE: it reads the
URLs and refuses a target this suite has no business dropping. It says nothing
about WHEN — an allowlisted name is exactly as droppable while a sibling run is
halfway through it, and that is the accident this module's second guard exists
for: a run that shared `memosng_test_p1` with another suite had its tables
dropped mid-flight and produced 43 failures, 8 errors and 36 UndefinedTable hits
in unrelated modules. `acquire_bootstrap_lock` claims the database for one run
at a time with a session-level Postgres advisory lock, so the second run refuses
before it connects a bootstrap engine instead of discovering the collision in
another module's traceback.
"""

import atexit
import contextlib
import os
import re
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.pool import NullPool

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_DATABASES = frozenset({"memosng"})
# Per-run scratch databases for parallel suite runs; see the module docstring.
PARALLEL_DATABASE_PATTERN = re.compile(r"^memosng_test_[a-z0-9_]+$")
ROUTING_ENV_OVERRIDES = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
# libpq's default when a URL omits the port; two URLs that differ only in spelling of the
# loopback host reach the same server, two that differ in port do not.
DEFAULT_PORT = 5432


def _refuse(reason: str) -> None:
    raise RuntimeError(
        "Refusing the destructive test bootstrap: "
        f"{reason} "
        f"(allowlisted hosts: {sorted(ALLOWED_HOSTS)}, "
        f"databases: {sorted(ALLOWED_DATABASES)} "
        f"or /{PARALLEL_DATABASE_PATTERN.pattern}/). "
        "Point the test database URLs at the local dev Postgres instead."
    )


def _database_is_allowed(database: str) -> bool:
    return database in ALLOWED_DATABASES or bool(PARALLEL_DATABASE_PATTERN.fullmatch(database))


def assert_database_is_disposable(url: str | URL) -> None:
    """Abort unless `url` provably targets an allowlisted local host AND database."""
    offenders = [k for k in ROUTING_ENV_OVERRIDES if os.environ.get(k)]
    if offenders:
        _refuse(f"libpq routing override(s) set in the environment: {offenders}")
    parsed = make_url(url) if isinstance(url, str) else url
    if parsed.query:
        _refuse(f"the URL carries query parameters {dict(parsed.query)}, which can reroute libpq")
    host = parsed.host or ""
    database = parsed.database or ""
    if host not in ALLOWED_HOSTS or not _database_is_allowed(database):
        _refuse(
            f"{host or '<no host>'}/{database or '<no database>'} is not an "
            "allowlisted local test target"
        )


def _resolved_target(url: URL) -> tuple[str, int, str]:
    """The server+database a URL actually reaches, as a comparable triple.

    The loopback spellings collapse to one token — `localhost`, `127.0.0.1` and `::1` are
    the same reachability class the host allowlist already grants. Ports do NOT collapse:
    5433 and 5432 are two different Postgres servers on this box, and a bootstrap that
    drops one server's schema while the API tests write into the other is the split brain
    this check exists to catch.
    """
    host = url.host or ""
    canonical = "loopback" if host in ALLOWED_HOSTS else host
    return (canonical, url.port or DEFAULT_PORT, url.database or "")


def _render(target: tuple[str, int, str]) -> str:
    host, port, database = target
    return f"{host}:{port}/{database or '<no database>'}"


def assert_urls_target_one_database(sync_url: str | URL, async_url: str | URL) -> None:
    """Abort unless both engines resolve to the same server and database.

    Only DATABASE_URL_SYNC is exported for a scratch run in the obvious reading, but the
    app's async engine reads DATABASE_URL — half-configured, the API-driven tests would
    keep committing into the shared `memosng` and re-create the cross-run pollution that
    the per-run databases exist to end.
    """
    sync = _resolved_target(make_url(sync_url) if isinstance(sync_url, str) else sync_url)
    asyn = _resolved_target(make_url(async_url) if isinstance(async_url, str) else async_url)
    if sync != asyn:
        raise RuntimeError(
            "Refusing the destructive test bootstrap: the sync bootstrap targets "
            f"{_render(sync)} while the app's async engine targets {_render(asyn)}. "
            "Export BOTH DATABASE_URL and DATABASE_URL_SYNC at the same database."
        )


# One fixed key for the whole repo: the bytes of b"memo_ng" read as one big-endian integer.
# A single key is enough because an advisory lock is scoped to the DATABASE it is taken in —
# the lock tag carries the database oid — so two runs on two scratch databases never see each
# other's lock, while two runs on the same database always do.
BOOTSTRAP_LOCK_KEY = 0x6D656D6F5F6E67
# pg_locks splits a one-argument advisory key into classid (high 32 bits) and objid (low 32),
# with objsubid 1; the two-argument form uses objsubid 2. Only an exact match is this lock.
BOOTSTRAP_LOCK_CLASSID = BOOTSTRAP_LOCK_KEY >> 32
BOOTSTRAP_LOCK_OBJID = BOOTSTRAP_LOCK_KEY & 0xFFFFFFFF
# The holder writes its OS pid here, because a backend pid names a Postgres process nobody can
# act on: what the reader of a refusal needs is the pytest run to wait for or kill.
APPLICATION_NAME_PREFIX = "memosng-pytest:"
# SEQUENTIAL runs must never trip over each other, and a handoff is not instantaneous: a session
# lock is released when Postgres notices the holder's socket is gone, which is milliseconds after
# the process dies, not before. scripts/check-mutations.sh starts the next `uv run pytest` the
# instant the previous one exits, so the lock is polled for a short grace period before refusing.
# A genuinely concurrent suite is still holding after it — it has minutes of work left.
LOCK_HANDOFF_GRACE_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.1

# Who holds the lock in THIS database, joined to the activity view for the holder's identity.
_HOLDER_SQL = """
SELECT a.pid, a.application_name, a.backend_start
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory'
  AND l.granted
  AND l.classid = :classid
  AND l.objid = :objid
  AND l.objsubid = 1
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
LIMIT 1
"""

# The lock lives on THIS connection for as long as the process does. Session-level, not
# transaction-level, and deliberately never released by a statement: a run that dies — crash,
# SIGKILL, a pulled plug — releases it by losing its socket, which is the whole reason the lock
# is taken on a connection of its own rather than on a pooled one the suite also writes through.
_lock_connection: Connection | None = None
_lock_engine: Engine | None = None


def _describe_holder(conn: Connection) -> str:
    row = conn.execute(
        text(_HOLDER_SQL),
        {"classid": BOOTSTRAP_LOCK_CLASSID, "objid": BOOTSTRAP_LOCK_OBJID},
    ).first()
    if row is None:
        # It let go between the failed attempt and this query, or its backend is no longer
        # visible. Refuse anyway — the grace period already gave a clean handoff its chance.
        return "by a session that pg_stat_activity no longer shows"
    name = row.application_name or "<no application_name>"
    return f"by {name} (Postgres backend pid {row.pid}, connected since {row.backend_start})"


def acquire_bootstrap_lock(url: str | URL) -> None:
    """Claim the target database for this run, or abort naming the run that already has it.

    Call this once, before anything connects a bootstrap engine and long before the session
    fixture drops `public`. The lock is held on a dedicated connection kept open for the whole
    run and closed by `release_bootstrap_lock`.
    """
    global _lock_connection, _lock_engine
    if _lock_connection is not None:
        return
    parsed = make_url(url) if isinstance(url, str) else url
    engine = create_engine(
        parsed,
        # NullPool so that closing the connection closes the socket: a pooled connection handed
        # back to the pool stays open, and an open socket goes on holding the lock.
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"application_name": f"{APPLICATION_NAME_PREFIX}{os.getpid()}"},
    )
    conn = engine.connect()
    deadline = time.monotonic() + LOCK_HANDOFF_GRACE_SECONDS
    while True:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": BOOTSTRAP_LOCK_KEY}
        ).scalar()
        if got:
            _lock_connection, _lock_engine = conn, engine
            # The connection outlives any fixture teardown, so the last word on releasing it
            # belongs to the interpreter, not to a hook that a hard exit may skip.
            atexit.register(release_bootstrap_lock)
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(LOCK_POLL_SECONDS)
    holder = _describe_holder(conn)
    conn.close()
    engine.dispose()
    raise RuntimeError(
        "Refusing the destructive test bootstrap: the test database "
        f"{parsed.database or '<no database>'} is already claimed {holder}. "
        "That run's session fixture owns the `public` schema; dropping it now would pull the "
        "tables out from under it. Give this run a database of its own — export both "
        "DATABASE_URL_SYNC and DATABASE_URL at a memosng_test_<suffix> the other run is not using."
    )


def release_bootstrap_lock() -> None:
    """Close the lock connection, which is what releases the lock. Safe to call twice."""
    global _lock_connection, _lock_engine
    conn, engine = _lock_connection, _lock_engine
    _lock_connection, _lock_engine = None, None
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()
    if engine is not None:
        with contextlib.suppress(Exception):
            engine.dispose()
