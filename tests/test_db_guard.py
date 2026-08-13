"""The destructive-bootstrap allowlist: wrong target, no connection, no drop."""

from pathlib import Path

import pytest

from tests.db_guard import assert_database_is_disposable


@pytest.mark.parametrize(
    "url",
    [
        # The real production cluster: allowlisted database name on a remote host.
        "postgresql+psycopg2://memosng:memosng@192.168.1.50:5455/memosng",
        # Local host but a database the suite has no business dropping.
        "postgresql+psycopg2://memosng:memosng@localhost:5433/postgres",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_prod",
        # Hostless URL (unix socket / default): nothing provable, refuse.
        "postgresql+psycopg2://memosng:memosng@/memosng",
    ],
)
def test_non_allowlisted_targets_abort(url):
    with pytest.raises(RuntimeError, match="Refusing the destructive test bootstrap"):
        assert_database_is_disposable(url)


def test_the_refusal_names_the_offending_target():
    with pytest.raises(RuntimeError, match=r"192\.168\.1\.50/memosng"):
        assert_database_is_disposable(
            "postgresql+psycopg2://memosng:memosng@192.168.1.50:5455/memosng"
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng",
        "postgresql+psycopg2://memosng:memosng@127.0.0.1:5433/memosng",
    ],
)
def test_local_throwaway_targets_pass(url):
    assert_database_is_disposable(url)


def test_conftest_guards_before_any_engine_is_created():
    # The wiring pin: the guard call must sit in conftest at import time, ahead of
    # the fixture that drops the schema — deleting the call turns this red.
    lines = (Path(__file__).parent / "conftest.py").read_text().splitlines()
    guard_lines = [
        i for i, line in enumerate(lines)
        if line.strip() == "assert_database_is_disposable(SYNC_URL)"
    ]
    assert guard_lines, "the bootstrap guard call is missing from conftest.py"
    engine_at = next(i for i, line in enumerate(lines) if "create_engine(SYNC_URL)" in line)
    assert guard_lines[0] < engine_at
