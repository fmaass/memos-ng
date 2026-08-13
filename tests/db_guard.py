"""Refusal gate for the destructive test bootstrap.

The session fixture drops and recreates the `public` schema of whatever
DATABASE_URL_SYNC points at. A stray environment variable — a shell that still
carries production credentials, a copied .env — must never be enough to aim
that drop at a real database, so the target has to prove it is a local
throwaway before any engine connects.
"""

from sqlalchemy.engine.url import make_url

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_DATABASES = frozenset({"memosng"})


def assert_database_is_disposable(url: str) -> None:
    """Abort unless `url` targets an allowlisted local host AND database name."""
    parsed = make_url(url)
    host = parsed.host or ""
    database = parsed.database or ""
    if host not in ALLOWED_HOSTS or database not in ALLOWED_DATABASES:
        raise RuntimeError(
            "Refusing the destructive test bootstrap: "
            f"{host or '<no host>'}/{database or '<no database>'} is not an "
            f"allowlisted local test target (hosts: {sorted(ALLOWED_HOSTS)}, "
            f"databases: {sorted(ALLOWED_DATABASES)}). "
            "Point DATABASE_URL_SYNC at the local dev Postgres instead."
        )
