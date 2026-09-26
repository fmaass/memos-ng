"""`API_MAJOR` is the one number a client reads to know whether it speaks this server's API.

It is stated as a literal in app/version.py because nothing at runtime can supply it: `APP_VERSION`
is unset in development (`__version__ == "dev"`). So these tests hold the literal to the two
versions that CAN name a major — pyproject.toml's, which a release bumps, and the `APP_VERSION` a
built image carries — so the number cannot silently lag a major release.
"""

import importlib
import importlib.util
import pathlib

import pytest
from app import version as app_version
from app.version import API_MAJOR, check_api_major

ROOT = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_release_version", ROOT / "scripts" / "check_release_version.py"
)
assert _spec is not None and _spec.loader is not None
crv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crv)


def test_api_major_matches_pyproject_major():
    authority = crv.authoritative_version(ROOT)
    assert type(API_MAJOR) is int
    assert API_MAJOR == int(authority.split(".")[0]), (
        f"pyproject.toml is {authority} but app/version.py states API_MAJOR = {API_MAJOR}"
    )


@pytest.fixture
def app_version_from_env(monkeypatch):
    """Re-read app/version.py under a given `APP_VERSION`, exactly as the module reads it at
    import, and put the module back as the rest of the suite knows it afterwards."""

    def load(value: str):
        monkeypatch.setenv("APP_VERSION", value)
        return importlib.reload(app_version)

    yield load
    monkeypatch.undo()
    importlib.reload(app_version)


@pytest.mark.parametrize("value", ["1.11.0", "1.11.0+de31f2e", "1.0.0"])
def test_a_built_version_with_the_same_major_agrees(app_version_from_env, value):
    module = app_version_from_env(value)
    assert module.__version__ == value
    module.check_api_major(module.__version__)


@pytest.mark.parametrize("value", ["2.0.0", "0.9.0", "10.0.0+abc1234", "11.0.0", "garbage", ""])
def test_a_built_version_with_another_major_is_refused(app_version_from_env, value):
    # 10.x and 11.x start with the digit 1: a prefix comparison would wave them through.
    module = app_version_from_env(value)
    assert module.__version__ == value
    with pytest.raises(ValueError, match="API_MAJOR"):
        module.check_api_major(module.__version__)


def test_the_unset_development_version_names_no_major(app_version_from_env, monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    module = importlib.reload(app_version)
    assert module.__version__ == "dev"
    module.check_api_major(module.__version__)


def test_the_running_version_agrees_with_api_major():
    check_api_major(app_version.__version__)
