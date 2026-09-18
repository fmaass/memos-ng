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
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import OperationalError
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
# …followed by the holder's pid NAMESPACE, because a bare pid means nothing across one.
NAMESPACE_SEPARATOR = "@"
# What Postgres stores of an application_name (NAMEDATALEN - 1); more is silently cut off.
APPLICATION_NAME_MAX_BYTES = 63
# SEQUENTIAL runs must never trip over each other, and a handoff is not instantaneous: a session
# lock is released when Postgres notices the holder's socket is gone, which is milliseconds after
# the process dies, not before. scripts/check-mutations.sh starts the next `uv run pytest` the
# instant the previous one exits, so the lock is polled for a short grace period before refusing.
# A genuinely concurrent suite is still holding after it — it has minutes of work left.
LOCK_HANDOFF_GRACE_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.1

# A connection this suite cannot open in a couple of seconds is not a database it is racing
# anyone for; the caller decides whether that is fatal (see `tolerate_unreachable`).
CONNECT_TIMEOUT_SECONDS = 5
# The remedy printed when a holder's pytest is gone but its backend still owns the lock; the dev
# database runs in this container (compose.dev.yaml), and nothing here terminates it for you.
DEV_DB_CONTAINER = "memos-ng-db-1"

# Who holds the lock in THIS database, joined to the activity view for the holder's identity.
# client_addr comes along because it decides whether the pid in application_name is a pid on
# THIS host: `_holder_is_on_this_host` compares it with the address this very connection shows.
_HOLDER_SQL = """
SELECT a.pid, a.application_name, a.backend_start, a.client_addr,
       (SELECT client_addr FROM pg_stat_activity WHERE pid = pg_backend_pid()) AS my_addr
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

# The same row, restricted to this very backend: it answers "do I still hold it", not "does
# anyone".
_OWNERSHIP_SQL = """
SELECT 1
FROM pg_locks
WHERE locktype = 'advisory'
  AND granted
  AND classid = :classid
  AND objid = :objid
  AND objsubid = 1
  AND pid = pg_backend_pid()
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""

# The lock lives on THIS connection for as long as the process does. Session-level, not
# transaction-level, and deliberately never released by a statement: a run that dies — crash,
# SIGKILL, a pulled plug — releases it by losing its socket, which is the whole reason the lock
# is taken on a connection of its own rather than on a pooled one the suite also writes through.
_lock_connection: Connection | None = None
_lock_engine: Engine | None = None


def _connection_still_holds_the_lock(conn: Connection) -> bool:
    """Ask Postgres, in one round-trip, whether THIS backend still owns the lock.

    The answer cannot come from memory. A session-level lock lives on its session, and a
    server restart or a `pg_terminate_backend` takes both away without telling this process;
    a run that then trusted its own bookkeeping would go on to DROP SCHEMA under whoever
    claimed the database in the meantime. A dead connection raises here, which is the same
    answer as a missing row: not held.
    """
    try:
        return bool(
            conn.execute(
                text(_OWNERSHIP_SQL),
                {"classid": BOOTSTRAP_LOCK_CLASSID, "objid": BOOTSTRAP_LOCK_OBJID},
            ).scalar()
        )
    except Exception:
        return False


def _holder_is_on_this_host(row: Row[Any]) -> bool:
    """Is the pid in the holder's application_name a pid of THIS machine?

    By the address it connected from, compared with the address this connection itself shows.
    The obvious rule — NULL or loopback means local — is wrong against the dev database, which
    runs in a container: a connection from this host arrives as the bridge gateway (10.240.15.1
    here), while a NULL client_addr is a unix-socket peer INSIDE the container, whose pid
    belongs to the container's namespace and says nothing about a process here. Same client
    address as this process = the same side of that boundary; anything else is not checkable,
    and an unchecked holder keeps the live-holder wording.
    """
    return row.client_addr is not None and row.client_addr == row.my_addr


def _pid_namespace() -> str:
    """This process's pid namespace, as the kernel names it, or "" where that is unreadable."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def _application_name(pid: int | None = None) -> str:
    """`memosng-pytest:<pid>@<pid namespace>`, e.g. memosng-pytest:1234567@pid:[4026531836].

    The pid alone is ambiguous: two runners in separate pid namespaces behind one client
    address — containers on host networking, a published port — are indistinguishable to
    Postgres, and reading a live holder's pid as "gone" would put a live run's backend in the
    remedy line. The namespace makes the pid checkable or visibly not ours. The prefix stays
    exactly `memosng-pytest:<pid>`, which the refusal and its tests read; a name that would
    exceed what Postgres keeps (63 bytes) drops the namespace rather than being truncated into
    a lie, and then simply never qualifies for the dead-holder wording.
    """
    base = f"{APPLICATION_NAME_PREFIX}{os.getpid() if pid is None else pid}"
    namespace = _pid_namespace()
    if not namespace:
        return base
    name = f"{base}{NAMESPACE_SEPARATOR}{namespace}"
    return name if len(name.encode()) <= APPLICATION_NAME_MAX_BYTES else base


def _parse_application_name(name: str) -> tuple[int | None, str | None]:
    """(pid, namespace) out of an application_name, either part None when it is not there."""
    if not name.startswith(APPLICATION_NAME_PREFIX):
        return None, None
    pid_text, separator, namespace = name[len(APPLICATION_NAME_PREFIX) :].partition(
        NAMESPACE_SEPARATOR
    )
    try:
        pid = int(pid_text)
    except ValueError:
        return None, None
    return pid, (namespace if separator else None)


def _holder_process_is_gone(row: Row[Any]) -> bool:
    """True only when application_name names a pid that provably does not exist here.

    Both conditions are required and neither replaces the other: the namespace id says the pid
    is from the same pid namespace as ours, the client address says the connection comes from
    the same side of the container boundary. A namespace id is an inode number, unique only
    within one kernel — two machines can show the same one — so the address keeps a remote
    holder from ever reaching the pid check.
    """
    pid, namespace = _parse_application_name(row.application_name or "")
    if pid is None or namespace is None:
        return False  # an older run, or a platform where the namespace is unreadable
    if not namespace or namespace != _pid_namespace():
        return False
    if not _holder_is_on_this_host(row):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive, and owned by another user
    except OSError:
        return False
    return False


def _refusal_message(database: str, conn: Connection) -> str:
    """The refusal for a database somebody else has claimed, as the holder actually is."""
    row = conn.execute(
        text(_HOLDER_SQL),
        {"classid": BOOTSTRAP_LOCK_CLASSID, "objid": BOOTSTRAP_LOCK_OBJID},
    ).first()
    if row is None:
        # It let go between the failed attempt and this query, or its backend is no longer
        # visible. Refuse anyway — the grace period already gave a clean handoff its chance.
        holder = "by a session that pg_stat_activity no longer shows"
    else:
        name = row.application_name or "<no application_name>"
        holder = f"by {name} (Postgres backend pid {row.pid}, connected since {row.backend_start})"
        holder_pid, _ = _parse_application_name(name)
        if _holder_process_is_gone(row):
            # Truthful, because "wait for the other run" is advice about a run that no longer
            # exists. The lock is not stolen and the backend is not terminated from here: an
            # automatic kill would eventually kill a live run whose pid this check misread.
            return (
                "Refusing the destructive test bootstrap: the test database "
                f"{database} is still claimed {holder}, but process "
                f"{holder_pid} is not running on this host — that "
                "pytest is gone and only its Postgres backend still holds the lock. Nothing "
                "here steals a lock; release it with: docker exec "
                f'{DEV_DB_CONTAINER} psql -U memosng -d {database} -c "SELECT '
                f'pg_terminate_backend({row.pid})"'
            )
    return (
        "Refusing the destructive test bootstrap: the test database "
        f"{database} is already claimed {holder}. "
        "That run's session fixture owns the `public` schema; dropping it now would pull the "
        "tables out from under it. Give this run a database of its own — export both "
        "DATABASE_URL_SYNC and DATABASE_URL at a memosng_test_<suffix> the other run is not using."
    )


def acquire_bootstrap_lock(url: str | URL, *, tolerate_unreachable: bool = False) -> bool:
    """Claim the target database for this run, or abort naming the run that already has it.

    Idempotent: a run that already holds the lock keeps it and this returns True without
    touching Postgres, so the call may be made both at session start and again at the point of
    use — the fixture that drops `public` — and the invariant that matters holds on every path:
    nothing drops a schema without having claimed the database first.

    `tolerate_unreachable` is for the session-start call. A run that never opens Postgres
    (`--collect-only`, a `-k` that selects nothing, the pure guard tests) worked with the
    database stopped before this lock existed and still has to: an unreachable server is not a
    refusal, it is nothing to claim yet. The run proceeds unlocked and the claim is made when
    the database is first used. Returns whether the lock is now held.
    """
    global _lock_connection, _lock_engine
    if _lock_connection is not None:
        if _connection_still_holds_the_lock(_lock_connection):
            return True
        # Postgres was restarted, or the backend was terminated: the lock went with the
        # session, and a sibling may own the database by now. Remembering that we took it once
        # is not holding it — drop the corpse and go through the full acquisition again.
        release_bootstrap_lock()
    parsed = make_url(url) if isinstance(url, str) else url
    engine = create_engine(
        parsed,
        # NullPool so that closing the connection closes the socket: a pooled connection handed
        # back to the pool stays open, and an open socket goes on holding the lock.
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={
            "application_name": _application_name(),
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        },
    )
    try:
        conn = engine.connect()
    except OperationalError:
        engine.dispose()
        if tolerate_unreachable:
            return False
        raise
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
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(LOCK_POLL_SECONDS)
    message = _refusal_message(parsed.database or "<no database>", conn)
    conn.close()
    engine.dispose()
    raise RuntimeError(message)


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
