"""The destructive-bootstrap allowlist: wrong target, no connection, no drop."""

from pathlib import Path

import pytest
from sqlalchemy.engine.url import make_url

from tests.db_guard import assert_database_is_disposable, assert_urls_target_one_database


@pytest.mark.parametrize(
    "url",
    [
        # The real production cluster: allowlisted database name on a remote host.
        "postgresql+psycopg2://memosng:memosng@192.168.1.50:5455/memosng",
        # Local host but a database the suite has no business dropping.
        "postgresql+psycopg2://memosng:memosng@localhost:5433/postgres",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_prod",
        # Near-misses on the parallel scratch pattern: prefix alone is not the pattern.
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_evil",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_A",
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


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a",
        "postgresql+psycopg2://memosng:memosng@127.0.0.1:5433/memosng_test_worker_2",
        "postgresql+asyncpg://memosng:memosng@localhost:5433/memosng_test_gate",
    ],
)
def test_parallel_scratch_databases_pass_on_local_hosts(url):
    # One throwaway database per concurrent run; the session fixture drops schemas, so
    # two runs sharing `memosng` tear each other's tables down mid-suite.
    assert_database_is_disposable(url)


def test_parallel_scratch_name_on_a_remote_host_is_still_refused():
    with pytest.raises(RuntimeError, match=r"192\.168\.1\.50/memosng_test_a"):
        assert_database_is_disposable(
            "postgresql+psycopg2://memosng:memosng@192.168.1.50:5455/memosng_test_a"
        )


def test_a_scratch_database_still_cannot_be_rerouted():
    with pytest.raises(RuntimeError, match="query parameters"):
        assert_database_is_disposable(
            "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a?host=192.168.1.50"
        )


def test_split_brain_between_the_two_engines_is_refused():
    # Exporting only DATABASE_URL_SYNC leaves the app's async engine on the shared
    # database, which is the cross-run pollution the scratch databases exist to end.
    with pytest.raises(RuntimeError, match="Export BOTH"):
        assert_urls_target_one_database(
            "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a",
            "postgresql+asyncpg://memosng:memosng@localhost:5433/memosng",
        )


def test_both_engines_on_the_same_database_pass():
    assert_urls_target_one_database(
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a",
        make_url("postgresql+asyncpg://memosng:memosng@localhost:5433/memosng_test_a"),
    )


@pytest.mark.parametrize(
    "url",
    [
        # Query params reroute libpq past the URL authority: make_url reports
        # localhost/memosng while the DROP actually reaches the ?host= target.
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng?host=192.168.1.50",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng?dbname=prod",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng?hostaddr=192.168.1.50",
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng?service=prod",
    ],
)
def test_query_string_rerouting_is_refused(url):
    with pytest.raises(RuntimeError, match="query parameters"):
        assert_database_is_disposable(url)


@pytest.mark.parametrize("var", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_libpq_env_overrides_are_refused(var, monkeypatch):
    # PGHOSTADDR routes the connection even when the URL names localhost.
    monkeypatch.setenv(var, "192.168.1.50")
    with pytest.raises(RuntimeError, match="routing override"):
        assert_database_is_disposable(
            "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng"
        )


def test_url_objects_are_accepted():
    assert_database_is_disposable(
        make_url("postgresql+asyncpg://memosng:memosng@localhost:5433/memosng")
    )


def test_conftest_guards_before_any_engine_is_created():
    # The wiring pin: both guard calls must sit in conftest at import time, ahead
    # of the fixture that drops the schema — deleting either turns this red.
    lines = (Path(__file__).parent / "conftest.py").read_text().splitlines()
    sync_guard = [
        i for i, line in enumerate(lines)
        if line.strip() == "assert_database_is_disposable(SYNC_URL)"
    ]
    async_guard = [
        i for i, line in enumerate(lines)
        if line.strip() == "assert_database_is_disposable(async_engine.url)"
    ]
    same_db_guard = [
        i for i, line in enumerate(lines)
        if line.strip() == "assert_urls_target_one_database(SYNC_URL, async_engine.url)"
    ]
    assert sync_guard, "the sync bootstrap guard call is missing from conftest.py"
    assert async_guard, "the async engine guard call is missing from conftest.py"
    assert same_db_guard, "the same-database guard call is missing from conftest.py"
    engine_at = next(i for i, line in enumerate(lines) if "create_engine(SYNC_URL)" in line)
    assert max(sync_guard[0], async_guard[0], same_db_guard[0]) < engine_at
