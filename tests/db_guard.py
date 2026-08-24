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
"""

import os
import re

from sqlalchemy.engine.url import URL, make_url

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
