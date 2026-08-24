"""The destructive-bootstrap allowlist: wrong target, no connection, no drop."""

import os
import subprocess
import sys
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


def test_the_same_name_on_another_port_is_refused():
    # Two Postgres servers on this box: the bootstrap would drop 5433's schema while the
    # API-driven tests wrote into 5432. Same name, different server, still a split brain.
    split = r"loopback:5433/memosng_test_a.*loopback:5432/memosng_test_a"
    with pytest.raises(RuntimeError, match=split):
        assert_urls_target_one_database(
            "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a",
            "postgresql+asyncpg://memosng:memosng@localhost:5432/memosng_test_a",
        )


def test_an_omitted_port_resolves_to_the_libpq_default():
    # A URL without a port reaches 5432; comparing it raw against an explicit 5432 would
    # false-refuse, and against 5433 would false-pass.
    assert_urls_target_one_database(
        "postgresql+psycopg2://memosng:memosng@localhost/memosng_test_a",
        "postgresql+asyncpg://memosng:memosng@127.0.0.1:5432/memosng_test_a",
    )
    with pytest.raises(RuntimeError, match="loopback:5432"):
        assert_urls_target_one_database(
            "postgresql+psycopg2://memosng:memosng@localhost/memosng_test_a",
            "postgresql+asyncpg://memosng:memosng@localhost:5433/memosng_test_a",
        )


def test_the_loopback_spellings_are_one_host():
    # localhost / 127.0.0.1 / ::1 are interchangeable spellings of the same reachability
    # class the host allowlist already grants — only the port distinguishes servers.
    assert_urls_target_one_database(
        "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a",
        make_url("postgresql+asyncpg://memosng:memosng@127.0.0.1:5433/memosng_test_a"),
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


LOCAL_SYNC = "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng"
LOCAL_ASYNC = "postgresql+asyncpg://memosng:memosng@localhost:5433/memosng"


def _import_conftest(sync_url: str, async_url: str) -> subprocess.CompletedProcess[str]:
    """Import tests/conftest.py in a fresh interpreter under the given DSNs.

    Out-of-process on purpose: the guard fires at conftest IMPORT time, which this
    interpreter did once already, and only a fresh import re-runs it.
    """
    env = {
        **os.environ,
        "DATABASE_URL_SYNC": sync_url,
        "DATABASE_URL": async_url,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", "import tests.conftest"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )


def test_importing_conftest_on_a_local_target_succeeds():
    # The positive control for the two refusal cases below: without it, an unrelated
    # ImportError would make every "conftest refuses" assertion pass for the wrong reason.
    done = _import_conftest(LOCAL_SYNC, LOCAL_ASYNC)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("sync_url", "async_url", "expected"),
    [
        # A production DSN in the environment must kill collection, not the schema.
        (
            "postgresql+psycopg2://memosng:memosng@192.168.1.50:5455/memosng",
            LOCAL_ASYNC,
            "not an allowlisted local test target",
        ),
        # The async engine is guarded too: it is what API-driven tests commit through.
        (
            LOCAL_SYNC,
            "postgresql+asyncpg://memosng:memosng@192.168.1.50:5455/memosng",
            "not an allowlisted local test target",
        ),
        # Half-configured scratch run: the sync bootstrap moved, the app did not.
        (
            "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng_test_a",
            LOCAL_ASYNC,
            "Export BOTH",
        ),
    ],
)
def test_conftest_refuses_at_import_before_any_engine_connects(sync_url, async_url, expected):
    # Behavioral, not textual: importing conftest under a bad target must raise, and the
    # raise must happen at import — before any fixture builds an engine or drops a schema.
    done = _import_conftest(sync_url, async_url)
    assert done.returncode != 0, done.stdout
    assert "Refusing the destructive test bootstrap" in done.stderr
    assert expected in done.stderr
