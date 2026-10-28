"""docker-entrypoint.sh as it ships: run for real with `alembic` and `uvicorn` stubbed on PATH, so
each role's bind, migration decision and file secrets are read from what the script executes."""

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "alembic").write_text('#!/bin/sh\necho "ALEMBIC $*"\n')
    (bin_dir / "uvicorn").write_text(
        '#!/bin/sh\necho "UVICORN $*"\n'
        'echo "TOKEN=${INGEST_SERVICE_TOKEN:-}"\n'
        'echo "URL=${DATABASE_URL:-}"\necho "SYNC=${DATABASE_URL_SYNC:-}"\n'
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    def _run(**env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(ENTRYPOINT)],
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}", **env},
            capture_output=True,
            text=True,
            check=False,
        )

    return _run


def _line(out: str, prefix: str) -> str:
    return next(line for line in out.splitlines() if line.startswith(prefix))


def test_ingest_home_listens_on_all_interfaces_port_8000_and_does_not_migrate(run):
    # pluto publishes it as 127.0.0.1:8091:8000; a loopback bind in the container is unreachable.
    r = run(APP_ROLE="ingest-home")
    assert r.returncode == 0, r.stderr
    assert _line(r.stdout, "UVICORN") == (
        "UVICORN app.ingest.asgi:app --host 0.0.0.0 --port 8000 --no-proxy-headers --no-access-log"
    )
    assert "ALEMBIC" not in r.stdout and "not migrating" in r.stdout


def test_the_door_listens_on_port_8000_and_migrates(run):
    r = run(APP_ROLE="ingest")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines.index("ALEMBIC upgrade head") < lines.index(_line(r.stdout, "UVICORN"))
    assert _line(r.stdout, "UVICORN") == (
        "UVICORN app.ingest.asgi:app --host 0.0.0.0 --port 8000 --no-proxy-headers --no-access-log"
    )


def test_home_starts_exactly_as_before(run):
    r = run()
    assert r.returncode == 0, r.stderr
    assert "ALEMBIC upgrade head" in r.stdout
    assert _line(r.stdout, "UVICORN").startswith(
        "UVICORN app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "
    )


def test_an_ingest_role_reads_its_token_and_database_password_from_files(run, tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 86 + "\n")
    password = tmp_path / "pw"
    password.write_text("p@ss/w:rd\n")
    r = run(
        APP_ROLE="ingest-home",
        INGEST_SERVICE_TOKEN_FILE=str(token),
        DATABASE_PASSWORD_FILE=str(password),
        DATABASE_USER="trip_ingest",
        DATABASE_HOST="trip-postgres:5432",
        DATABASE_NAME="trip_ingest",
    )
    assert r.returncode == 0, r.stderr
    assert _line(r.stdout, "TOKEN=") == "TOKEN=" + "t" * 86
    dsn = "trip_ingest:p%40ss%2Fw%3Ard@trip-postgres:5432/trip_ingest"
    assert _line(r.stdout, "URL=") == f"URL=postgresql+asyncpg://{dsn}"
    assert _line(r.stdout, "SYNC=") == f"SYNC=postgresql+psycopg2://{dsn}"


def test_home_never_reads_an_ingest_token_file(run, tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 86)
    r = run(INGEST_SERVICE_TOKEN_FILE=str(token))
    assert r.returncode == 0, r.stderr
    assert _line(r.stdout, "TOKEN=") == "TOKEN="


def test_a_password_file_without_the_rest_of_the_dsn_refuses_to_start(run, tmp_path):
    password = tmp_path / "pw"
    password.write_text("x")
    r = run(APP_ROLE="ingest", DATABASE_PASSWORD_FILE=str(password))
    assert r.returncode != 0 and "UVICORN" not in r.stdout and "DATABASE_USER" in r.stderr


@pytest.mark.parametrize("role", ["guest", "Ingest", ""])
def test_an_unknown_role_exits_64(run, role):
    r = run(APP_ROLE=role)
    if role == "":  # unset-equivalent: ${APP_ROLE:-home}
        assert r.returncode == 0 and "app.main:app" in r.stdout
        return
    assert r.returncode == 64 and "UVICORN" not in r.stdout
