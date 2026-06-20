import pytest
from app.main import app
from fastapi.testclient import TestClient


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
