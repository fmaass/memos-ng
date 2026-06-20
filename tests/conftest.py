import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

SYNC_URL = os.environ.get(
    "DATABASE_URL_SYNC", "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng"
)


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


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so the degraded path's 503 is observable rather
    # than re-raised as a test error.
    return TestClient(app, raise_server_exceptions=False)


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
