import os

__version__ = os.environ.get("APP_VERSION", "dev")

# The API major a client checks /health for. It cannot be derived from `__version__`, which is
# "dev" whenever APP_VERSION is unset; tests/test_api_major.py holds it to pyproject.toml's major.
API_MAJOR = 1


def check_api_major(version: str) -> None:
    """Raise when a built version's major is not API_MAJOR. The unset "dev" build names none."""
    if version == "dev":
        return
    major = version.split(".", 1)[0]
    if not major.isdigit() or int(major) != API_MAJOR:
        raise ValueError(f"version {version!r} does not have major API_MAJOR = {API_MAJOR}")
