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

from tests.db_guard import (
    acquire_bootstrap_lock,
    assert_database_is_disposable,
    assert_urls_target_one_database,
    release_bootstrap_lock,
)

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


def pytest_sessionstart(session: pytest.Session) -> None:
    """Claim the database for this run before collection, so nothing can drop a live schema.

    Session start, not import time: the two guards above read URLs and can run anywhere, but
    the lock is a claim by a RUN, and only a real session goes on to drop `public`. A bare
    `import tests.conftest` — tests/test_db_guard.py does exactly that, in a subprocess, to
    prove the URL guards fire before any engine connects — must not take the parent suite's
    database away from it. This hook runs before collection and long before `db_engine` builds
    an engine, so a refusal here happens with the other run's schema still untouched.
    """
    try:
        acquire_bootstrap_lock(SYNC_URL)
    except RuntimeError as exc:
        # Reported as a usage error: `ERROR: <message>` and exit 4, rather than the
        # INTERNALERROR traceback a bare raise in a hook produces, which reads like a crash.
        raise pytest.UsageError(str(exc)) from exc


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    release_bootstrap_lock()


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


@pytest.fixture(scope="session")
def _empty_hwmon(tmp_path_factory):
    return tmp_path_factory.mktemp("hwmon-empty")


@pytest.fixture(autouse=True)
def _thermal_sensor_is_not_this_machine(_empty_hwmon):
    """No test reads the running machine's CPU temperature.

    The guard resolves its sensor by chip name, and the host this suite runs on IS the production
    host — a real coretemp package that crosses THERMAL_MAX_C under load. Left alone, whether a
    media test deferred would depend on how warm the machine happened to be, so every test starts
    from an empty hwmon tree: nothing found, state UNKNOWN, guard open. A test that wants a
    temperature sets THERMAL_ZONE or points HWMON_ROOT at a tree of its own — both override this.

    Saves and restores by hand rather than taking `monkeypatch`, and that is not a style choice:
    an AUTOUSE fixture requesting monkeypatch has it built before every other fixture of the test,
    so it is also undone after them — which left another test's `commit` patch in place during a
    fixture teardown that committed, and leaked a row into two later tests. A fixture that exists
    to keep the suite deterministic must not reorder anyone else's teardown.
    """
    from app.config import settings
    from app.services import media

    saved_root, saved_zone = media.HWMON_ROOT, settings.THERMAL_ZONE
    media.HWMON_ROOT = _empty_hwmon
    settings.THERMAL_ZONE = ""
    try:
        yield
    finally:
        media.HWMON_ROOT = saved_root
        settings.THERMAL_ZONE = saved_zone


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    # The login rate-limiter keeps per-IP state in a module dict; all tests share the one
    # TestClient IP, so reset it between tests or repeated logins eventually 429.
    from app import auth

    auth._RL.clear()
    yield
    auth._RL.clear()


@pytest.fixture
def controlled_clock(monkeypatch, request):
    """Control every clock the code-redemption and limiter paths read; hand the test the instant.

    Two windows decide these tests and neither may be measured in real time. The limiter's is 60
    seconds of `time.monotonic()`; a handoff code's is `NATIVE_CODE_TTL_S` seconds of wall clock,
    and a management grant's is `MANAGE_GRANT_TTL_S`. A case that stalls between filling a bucket
    and asserting the refusal — a loaded box, a debugger, a swapped-out host — would watch its own
    window expire and read a correct outcome as the wrong one. Pinned, only the request COUNT and
    the explicit `advance()` can move anything.

    Pinned per module that READS a clock, never globally: only `monotonic` differs on the `time`
    stand-in and only `now` on the `datetime` one, every other attribute is the real thing, and no
    module outside this list changes its notion of time.

    The pin starts at the real instant rather than an arbitrary one, so a row the DATABASE stamps
    with its own `now()` — a `created_at` default — still sits where the pinned code expects it.
    A test that BACKDATES a row computes the instant from `now()` here and binds it as a
    parameter, never from SQL `now()`: the two clocks are independent once this fixture starts,
    and a stall between them is exactly the flake being removed. `advance()` moves both clocks
    together, forward only, which is the only motion a test may ask for.

    **The control is structural, not per-test.** Every patched read is counted, and at teardown a
    test that pinned the clock and then never had the APPLICATION read it fails by name. So the
    marker cannot sit on a case that exercises no clock path (where it would be noise a reader
    mistakes for coverage), and it cannot silently miss its target either: a pin that stopped
    reaching `app.auth` — a renamed import, a call site moved to another module — turns the case
    red instead of quietly restoring the wall clock (MEMNG-296 review round 1).

    A case that deliberately wants the real clock says so with `@pytest.mark.real_clock`; the
    fixture then patches nothing, counts nothing, and asserts nothing.
    """
    import time as real_time
    from datetime import UTC
    from datetime import datetime as real_datetime
    from datetime import timedelta as real_timedelta

    from app import auth
    from app.routers import auth as auth_router
    from app.services import management_grant, native_auth

    class ControlledClock:
        def __init__(self) -> None:
            self._instant = real_datetime.now(UTC)
            self._monotonic = real_time.monotonic()
            self.reads: dict[str, int] = {}

        @property
        def monotonic(self) -> float:
            """The pinned `time.monotonic()` — the value every limiter stamp taken under the pin
            carries, so a test can assert the pin reached the code it is exercising."""
            return self._monotonic

        def now(self):
            """The pinned `datetime.now(UTC)`."""
            return self._instant

        def advance(self, seconds: float) -> None:
            if seconds < 0:  # a window that reopens by going backwards tests nothing real
                raise ValueError("a controlled clock only moves forward")
            self._instant += real_timedelta(seconds=seconds)
            self._monotonic += seconds

        def _record(self, where: str) -> None:
            self.reads[where] = self.reads.get(where, 0) + 1

    clock = ControlledClock()

    if request.node.get_closest_marker("real_clock") is not None:
        yield clock  # unpinned by request: the instant is still readable, nothing is patched
        return

    def pinned_time(where: str):
        class PinnedTime:
            def monotonic(self) -> float:
                clock._record(f"{where}.time.monotonic")
                return clock.monotonic

            def __getattr__(self, name: str):
                return getattr(real_time, name)

        return PinnedTime()

    def pinned_datetime(where: str):
        # One subclass per module, so the spy can say WHICH module read the clock rather than
        # only that something did.
        class PinnedDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):  # every pinned call site passes UTC; a naive read is not one
                clock._record(f"{where}.datetime.now")
                return clock.now() if tz is None else clock.now().astimezone(tz)

        return PinnedDatetime

    monkeypatch.setattr(auth, "time", pinned_time("app.auth"))
    for module in (auth, auth_router, native_auth, management_grant):
        monkeypatch.setattr(module, "datetime", pinned_datetime(module.__name__))

    yield clock

    if not clock.reads:
        pytest.fail(
            "controlled_clock: the application never read the pinned clock during "
            f"{request.node.nodeid} — either the pin no longer reaches the code under test, or "
            "this case exercises no clock path and must not be pinned (drop the fixture, or "
            "mark it @pytest.mark.real_clock)",
            pytrace=False,
        )


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


@pytest.fixture
def admin_client(client, db_engine, monkeypatch):
    """A TestClient whose session belongs to an ADMIN account.

    A separate fixture rather than a parameter on `logged_in_client`, so every existing test keeps
    the ordinary (non-admin) session it was written against and the 403 cases below have a genuine
    counterpart to be refused next to. The role is not set by hand: it comes out of the real login
    flow from the Authelia `admins` group claim, which is the one place `User.role` is ever
    assigned (app/auth.py `upsert_user`) — so a change that stopped mapping the group would fail
    the admin tests instead of silently granting everyone.
    """
    from unittest.mock import AsyncMock

    from app import auth
    from app.config import settings

    subject = "test_admin_user"
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {
            "preferred_username": subject,
            "name": "Admin",
            "email": "admin@x",
            "groups": ["admins"],
        },
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
