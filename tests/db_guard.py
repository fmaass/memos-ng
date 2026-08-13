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
"""

import os

from sqlalchemy.engine.url import URL, make_url

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_DATABASES = frozenset({"memosng"})
ROUTING_ENV_OVERRIDES = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


def _refuse(reason: str) -> None:
    raise RuntimeError(
        "Refusing the destructive test bootstrap: "
        f"{reason} "
        f"(allowlisted hosts: {sorted(ALLOWED_HOSTS)}, "
        f"databases: {sorted(ALLOWED_DATABASES)}). "
        "Point the test database URLs at the local dev Postgres instead."
    )


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
    if host not in ALLOWED_HOSTS or database not in ALLOWED_DATABASES:
        _refuse(
            f"{host or '<no host>'}/{database or '<no database>'} is not an "
            "allowlisted local test target"
        )
