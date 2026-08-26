import errno
import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.csrf import canonical_origin
from app.database import engine as async_engine
from app.main import app
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tests.db_guard import assert_database_is_disposable, assert_urls_target_one_database

SYNC_URL = os.environ.get(
    "DATABASE_URL_SYNC", "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng"
)
# Import-time, not fixture-time: collection must die before anything connects.
# Both engines are checked — the sync bootstrap that drops the schema AND the
# app's async engine, which API-driven tests commit real deletes through.
assert_database_is_disposable(SYNC_URL)
assert_database_is_disposable(async_engine.url)
# A parallel run exports a scratch database; both engines have to have followed it there.
assert_urls_target_one_database(SYNC_URL, async_engine.url)


@pytest.fixture(scope="session")
def db_engine():
    """A Postgres engine with the Alembic baseline applied from an empty schema.

    Drops + recreates `public`, then runs `alembic upgrade head` — so this fixture
    also exercises the migration end-to-end, not just the ORM metadata.
    """
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    os.environ["DATABASE_URL_SYNC"] = SYNC_URL
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    # Per-test transaction rolled back for isolation.
    conn = db_engine.connect()
    txn = conn.begin()
    sess = Session(bind=conn)
    try:
        yield sess
    finally:
        sess.close()
        if txn.is_active:  # a failed flush (IntegrityError test) already rolled back
            txn.rollback()
        conn.close()


@pytest_asyncio.fixture
async def adb(db_engine):
    """Async session bound to a transaction rolled back after each test (service tests)."""
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        sess = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield sess
        finally:
            await sess.close()
            await trans.rollback()


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    # The login rate-limiter keeps per-IP state in a module dict; all tests share the one
    # TestClient IP, so reset it between tests or repeated logins eventually 429.
    from app import auth

    auth._RL.clear()
    yield
    auth._RL.clear()


@pytest.fixture
def split_assets_mount(monkeypatch):
    """Reproduce the deployed storage layout, which no test box has: `<DATA_ROOT>/assets` (the
    derived tiers, on NVMe) is a SEPARATE filesystem sitting inside `<DATA_ROOT>` (originals and
    upload parts, on the HDD share). Any rename ACROSS that boundary fails with EXDEV in
    production while passing every test environment — which is exactly how the first real poster
    override 500'd in the field (MEMNG-143).

    Call the returned function with the DATA_ROOT the test is using. Both directions are guarded,
    and every rename that does NOT cross the boundary is delegated to the real one, so an
    over-broad patch cannot be what makes a test pass. `os.replace`/`os.rename` are the two
    primitives everything here goes through — `Path.rename`/`Path.replace` call them by module
    attribute, so they are covered too.

    Patching these patches a DEPENDENCY of the code under test: the placement itself runs in full.
    """

    def apply(data_root) -> None:
        assets = str(Path(data_root) / "assets")

        def guard(real):
            def wrapped(src, dst, **kwargs):
                if str(src).startswith(assets) != str(dst).startswith(assets):
                    raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
                return real(src, dst, **kwargs)

            return wrapped

        monkeypatch.setattr(os, "replace", guard(os.replace))
        monkeypatch.setattr(os, "rename", guard(os.rename))

    return apply


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so the degraded path's 503 is observable rather
    # than re-raised as a test error.
    c = TestClient(app, raise_server_exceptions=False)
    # Browsers attach Origin to every unsafe-method request, same-origin included, and the CSRF
    # middleware (app/csrf.py) demands it on cookie-authenticated mutations — so the default client
    # speaks as this app's own origin does. A per-request `headers={"Origin": ...}` still wins
    # (httpx merges request headers over client headers); that is how tests/test_csrf.py forges a
    # sibling origin, and how it drops the header entirely.
    c.headers["Origin"] = canonical_origin()
    return c


@pytest.fixture
def logged_in_client(client, db_engine, monkeypatch):
    """A TestClient carrying a real BFF session cookie (OIDC network calls mocked).

    Eager-added routes bake their handler at construction time, so app.dependency_overrides
    does not apply — authenticate through the real flow instead.
    """
    from unittest.mock import AsyncMock

    from app import auth
    from app.config import settings

    subject = "test_session_user"
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"preferred_username": subject, "name": "T", "email": "t@x", "groups": []},
    )
    login = client.get("/api/auth/login", follow_redirects=False)
    tx = login.cookies[auth.TX_COOKIE]
    state = auth.decrypt(tx)["state"]
    client.cookies.set(auth.TX_COOKIE, tx)
    cb = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    client.cookies.set(auth.SID_COOKIE, cb.cookies[auth.SID_COOKIE])
    try:
        yield client, subject
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        eng.dispose()


@pytest_asyncio.fixture
async def pat_client(logged_in_client):
    """`(bearer_client, subject, token)` — an httpx client that carries a PAT and nothing else.

    A SEPARATE client on purpose: reusing `logged_in_client` with an extra header would leave the
    session cookie in the jar, and every "the bearer got me in" assertion would still pass with the
    bearer branch deleted. This one has an empty cookie jar, so only the token can answer.

    The token is minted through the session client, which is also the only way to mint one
    (`/api/pats` is session-only by design).
    """
    from app import auth

    client, subject = logged_in_client
    created = client.post("/api/pats", json={"name": "device"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as bearer:
        try:
            yield bearer, subject, token
        finally:
            # The last_used stamp is a background task on THIS test's loop; land or cancel it here
            # rather than letting the loop close under a half-finished write.
            await auth.drain_pat_stamp_tasks()


class _CtxSession:
    """Async context manager standing in for app.database.async_session().

    `execute` either returns (DB reachable) or raises (DB down) per `fail`.
    The DB is a *dependency* of the health endpoint — mocking it here is allowed;
    the endpoint logic itself is never mocked.
    """

    def __init__(self, *, fail: bool):
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        if self._fail:
            raise RuntimeError("simulated db outage")
        return None


def session_factory(*, fail: bool):
    return lambda: _CtxSession(fail=fail)
