"""APP_ROLE (trip pass P2): what each role serves, refuses at startup and imports.

The allowlist is the contract. The walk reads the RUNTIME route table — `app.router.routes`,
mounts and `include_in_schema=False` routes included — not the OpenAPI document, and requires the
(method, path) set of each ingest role to equal its allowlist exactly. The probe then sends the
UNION of every role's paths, the hidden ones and `%2f` / `..` variants to each ingest role with no
credential, a guest token, a member PAT, a member session cookie and the service token, and
requires a refusal for everything outside the allowlist; positive controls prove the same
machinery reaches an allowed route.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
from app.config import INGEST_TOKEN_MIN_CHARS, MEMBER_SETTINGS, Settings
from app.ingest.factory import create_app
from app.main import app as home_app
from starlette.routing import Mount

from tests.ingest_support import (
    SERVICE_TOKEN,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    redeem,
    seeded_pass,
)

REPO = Path(__file__).resolve().parent.parent

GUEST_ALLOWLIST = {
    ("GET", "/health"),
    ("GET", "/.well-known/apple-app-site-association"),
    ("GET", "/p/{secret}"),
    ("GET", "/guest-assets/{name}"),
    ("POST", "/api/pass/redeem"),
    ("GET", "/api/guest/me"),
    ("POST", "/api/guest/uploads"),
    ("HEAD", "/api/guest/uploads/{upload_uid}"),
    ("PATCH", "/api/guest/uploads/{upload_uid}"),
    ("POST", "/api/guest/memos"),
    ("GET", "/api/guest/batches"),
}
HOME_IFACE_ALLOWLIST = {
    ("GET", "/health"),
    ("PUT", "/ingest/passes"),
    ("GET", "/ingest/passes/suspended"),
    ("PUT", "/ingest/device-revocations"),
    ("GET", "/ingest/devices"),
    ("GET", "/ingest/batches"),
    ("GET", "/ingest/blobs/{sha256}"),
    ("POST", "/ingest/batches/{uid}/ack"),
    ("POST", "/ingest/batches/{uid}/quarantine"),
}
ALLOWLISTS = {"ingest": GUEST_ALLOWLIST, "ingest-home": HOME_IFACE_ALLOWLIST}


def route_set(app) -> set[tuple[str, str]]:
    """Every (method, path) the app's router serves. A mount, wrapped or not, is reported as
    ("MOUNT", path) rather than skipped for having no child routes."""
    found: set[tuple[str, str]] = set()
    for route in app.router.routes:
        if isinstance(route, Mount):
            found.add(("MOUNT", route.path))
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        assert path is not None, f"a route without a path: {route!r}"
        if not methods:
            found.add(("ANY", path))
            continue
        for method in methods:
            found.add((method, path))
    return found


# --- the walk ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ingest", "ingest-home"])
def test_each_ingest_role_serves_exactly_its_allowlist(role):
    assert route_set(create_app(role)) == ALLOWLISTS[role]


def test_the_home_role_serves_no_guest_or_ingest_route():
    home = route_set(home_app)
    guestish = {
        (m, p)
        for m, p in home
        if p.startswith(("/api/guest", "/api/pass/", "/ingest", "/p/", "/guest-assets"))
    }
    assert guestish == set()
    # control: the walk sees what home does serve, hidden routes included
    assert ("GET", "/metrics") in home and ("GET", "/api/memos") in home


def test_an_unknown_role_builds_nothing():
    with pytest.raises(ValueError):
        create_app("home")  # type: ignore[arg-type]


# --- the union probe ---------------------------------------------------------------------------

HIDDEN = [
    "/",
    "/index.html",
    "/assets/index.js",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/metrics",
    "/privacy",
    "/api/nothing",
    "/ios/auth/callback",
    "/api/guest",
    "/api/guest/",
    "/api/guest/me/",
    "/ingest",
    "/ingest/",
]
VARIANTS = [
    "/api/guest/uploads/a%2fb",
    "/api/guest%2fme",
    "/api%2fguest%2fme",
    "/ingest%2fbatches",
    "/ingest/blobs/..%2f..%2fetc%2fpasswd",
    "/api/guest/../../ingest/batches",
    "/api/guest/me/..",
    "/p/../api/guest/me",
    "/p/x/../../ingest/batches",
    "/ingest/../api/guest/me",
    "/api/guest/./me",
    "/API/GUEST/ME",
    "/ingest/batches/x/ack/..",
    "/.well-known/../ingest/devices",
]
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _template_regex(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$")


def _allowed(role: str, method: str, path: str) -> bool:
    return any(m == method and _template_regex(t).match(path) for m, t in ALLOWLISTS[role])


def union_probes() -> list[tuple[str, str]]:
    templates = {p for _, p in route_set(home_app)}
    templates |= {p for _, p in GUEST_ALLOWLIST | HOME_IFACE_ALLOWLIST}
    probes = {(_concrete(p), "") for p in templates} | {(p, "") for p in HIDDEN}
    return sorted(probes) + [(v, "raw") for v in VARIANTS]


async def asgi_call(app, method: str, raw: str, headers: dict[str, str]) -> int:
    """One request straight into the ASGI app: the path reaches the router exactly as given —
    no client normalizes a `..` away — with `%2f` decoded the way a server decodes it."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw),
        "raw_path": raw.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 50000),
        "server": ("door", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        return {"type": "http.disconnect"}

    status = 0

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await app(scope, receive, send)
    return status


async def test_the_union_of_every_roles_paths_is_refused_outside_each_allowlist(
    door,  # noqa: F811
    home_iface,  # noqa: F811
):
    _, secret = await seeded_pass(home_iface)
    guest_token = (await redeem(door, secret))["device_token"]
    credentials = {
        "none": {},
        "guest": {"Authorization": f"Bearer {guest_token}"},
        "member-pat": {"Authorization": "Bearer memos_pat_" + "a" * 43},
        "member-session": {"Cookie": "memos_sid=abc"},
        "service": {"Authorization": f"Bearer {SERVICE_TOKEN}"},
    }
    probes = union_probes()
    assert len(probes) > 100  # the union is the whole home table, not a sample of it
    checked = 0
    for role in ("ingest", "ingest-home"):
        app = create_app(role)
        for raw, _kind in probes:
            for method in METHODS:
                if _allowed(role, method, unquote(raw)):  # judged on what the router sees
                    continue
                for name, headers in credentials.items():
                    status = await asgi_call(app, method, raw, headers)
                    assert status in (401, 404, 405), (role, method, raw, name, status)
                    checked += 1
    assert checked > 5000
    # Positive controls through the very same call: the allowed routes answer.
    door_app, home_iface_app = create_app("ingest"), create_app("ingest-home")
    assert await asgi_call(door_app, "GET", "/api/guest/me", credentials["guest"]) == 200
    assert await asgi_call(door_app, "GET", "/health", {}) == 200
    assert await asgi_call(home_iface_app, "GET", "/ingest/batches", credentials["service"]) == 200


async def test_member_and_foreign_credentials_are_refused_on_every_guest_route(
    door,  # noqa: F811
    home_iface,  # noqa: F811
):
    _, secret = await seeded_pass(home_iface)
    await redeem(door, secret)
    app = create_app("ingest")
    for header in (
        {},
        {"Authorization": "Bearer memos_pat_" + "a" * 43},
        {"Authorization": f"Bearer {SERVICE_TOKEN}"},
        {"Authorization": "Bearer trip_dev_" + "z" * 43},
        {"Cookie": "memos_sid=abc"},
    ):
        for method, path in GUEST_ALLOWLIST:
            if not path.startswith("/api/guest"):
                continue
            assert await asgi_call(app, method, _concrete(path), header) == 401, (method, path)


# --- the service token -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer wrong",
        f"Bearer {SERVICE_TOKEN}x",
        f"Basic {SERVICE_TOKEN}",
        f"{SERVICE_TOKEN}",
    ],
)
async def test_the_home_interface_refuses_anything_but_the_service_token(home_iface, header):  # noqa: F811
    headers = {"Authorization": header} if header is not None else {}
    home_iface.headers.pop("Authorization", None)
    r = await home_iface.get("/ingest/batches", headers=headers)
    assert r.status_code == 401
    r = await home_iface.get(
        "/ingest/batches", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
    )
    assert r.status_code == 200  # control


async def test_an_unset_service_token_admits_nobody_not_even_an_empty_bearer(
    home_iface,  # noqa: F811
    monkeypatch,
):
    from app.config import settings

    monkeypatch.setattr(settings, "INGEST_SERVICE_TOKEN", "")
    home_iface.headers.pop("Authorization", None)
    for header in ("Bearer ", "Bearer", "bearer  "):
        r = await home_iface.get("/ingest/batches", headers={"Authorization": header})
        assert r.status_code == 401, header


# --- startup refusals --------------------------------------------------------------------------


def _settings(monkeypatch, **values) -> Settings:
    for name in (*MEMBER_SETTINGS, "APP_ROLE", "INGEST_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


TOKEN = "k" * INGEST_TOKEN_MIN_CHARS


@pytest.mark.parametrize("role", ["ingest", "ingest-home"])
@pytest.mark.parametrize("name", MEMBER_SETTINGS)
def test_both_ingest_roles_refuse_every_member_setting(monkeypatch, role, name):
    extra = {"INGEST_SERVICE_TOKEN": TOKEN} if role == "ingest-home" else {}
    _settings(monkeypatch, APP_ROLE=role, **extra).validate_runtime_secrets()  # control
    s = _settings(monkeypatch, APP_ROLE=role, **extra, **{name: "1"})
    with pytest.raises(RuntimeError, match=name):
        s.validate_runtime_secrets()


def test_the_ingest_roles_need_no_session_secret_in_production(monkeypatch):
    _settings(monkeypatch, APP_ROLE="ingest", ENV="production").validate_runtime_secrets()
    s = _settings(monkeypatch, APP_ROLE="ingest-home", ENV="production", INGEST_SERVICE_TOKEN=TOKEN)
    s.validate_runtime_secrets()
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):  # home still demands its own
        _settings(monkeypatch, APP_ROLE="home", ENV="production").validate_runtime_secrets()


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"APP_ROLE": "home", "INGEST_SERVICE_TOKEN": TOKEN}, "INGEST_SERVICE_TOKEN"),
        ({"APP_ROLE": "home", "INGEST_TRUST_CF_CONNECTING_IP": True}, "INGEST_TRUST_CF"),
        ({"APP_ROLE": "ingest", "INGEST_SERVICE_TOKEN": TOKEN}, "INGEST_SERVICE_TOKEN"),
        ({"APP_ROLE": "ingest-home"}, "INGEST_SERVICE_TOKEN"),
        ({"APP_ROLE": "ingest-home", "INGEST_SERVICE_TOKEN": TOKEN[:-1]}, "64 random bytes"),
        (
            {
                "APP_ROLE": "ingest-home",
                "INGEST_SERVICE_TOKEN": TOKEN,
                "INGEST_TRUST_CF_CONNECTING_IP": True,
            },
            "INGEST_TRUST_CF",
        ),
    ],
)
def test_each_role_refuses_the_settings_of_another(monkeypatch, values, match):
    with pytest.raises(RuntimeError, match=match):
        _settings(monkeypatch, **values).validate_runtime_secrets()


def test_the_settings_each_role_needs_start(monkeypatch):
    _settings(monkeypatch, APP_ROLE="ingest", INGEST_TRUST_CF_CONNECTING_IP=True)
    _settings(monkeypatch, APP_ROLE="ingest-home", INGEST_SERVICE_TOKEN=TOKEN)
    _settings(monkeypatch).validate_runtime_secrets()


# --- fresh-process imports ---------------------------------------------------------------------

LOADED = (
    "import sys, {module};"
    "print('\\n'.join(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))))"
)
FORBIDDEN = re.compile(
    r"^app\.(routers(\..*)?|auth|main|csrf|worker|services\.(native_auth|pats|management_grant"
    r"|asset_urls|digest(\..*)?|push|memos|collections))$"
)


def _fresh(module: str, role: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "APP_ROLE": role}
    return subprocess.run(
        [sys.executable, "-c", LOADED.format(module=module)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("role", ["ingest", "ingest-home"])
def test_an_ingest_role_never_imports_a_member_module(role):
    done = _fresh("app.ingest.asgi", role)
    assert done.returncode == 0, done.stderr
    loaded = done.stdout.split()
    assert "app.ingest.guest" in loaded  # control: the listing is real
    assert [m for m in loaded if FORBIDDEN.match(m)] == []


def test_the_forbidden_pattern_does_see_the_member_routers_of_home():
    done = _fresh("app.main", "home")
    assert done.returncode == 0, done.stderr
    assert "app.routers.memos" in [m for m in done.stdout.split() if FORBIDDEN.match(m)]


def test_each_entry_module_refuses_the_other_role():
    refused = _fresh("app.main", "ingest")
    assert refused.returncode != 0 and "app.ingest.asgi:app" in refused.stderr
    refused = _fresh("app.ingest.asgi", "home")
    assert refused.returncode != 0 and "app.main:app" in refused.stderr
