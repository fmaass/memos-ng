"""APP_ROLE (trip pass P2): what each role serves, refuses at startup and imports.

The allowlist is the contract. The walk reads the RUNTIME route table — `app.router.routes`,
mounts and `include_in_schema=False` routes included — not the OpenAPI document, and requires the
(method, path) set of each role to equal its allowlist exactly. The probe then sends the UNION of
every role's paths, the hidden ones and `%2f` / `..` variants to each role with no credential, a
guest token, a member PAT, a member session cookie and the service token, and requires a refusal
for everything outside the allowlist; positive controls prove the same machinery reaches an
allowed route. Home alone keeps a shell catch-all, so there a GET may also answer that static
shell, byte for byte, except on a guest path.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
from app.config import INGEST_TOKEN_MIN_CHARS, MEMBER_SETTINGS, Settings, settings
from app.ingest import guest
from app.ingest.factory import create_app
from app.main import FRONTEND_DIR, mount_spa
from app.main import app as home_app
from app.services import asset_urls
from starlette.routing import Mount

from tests.ingest_support import (
    JPEG,
    SERVICE_TOKEN,
    bearer,
    complete,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    redeem,
    seeded_pass,
    upload,
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
# Home's whole surface, hidden routes included. A route added to home fails the walk until it is
# named here, which is the point: the review of that line is where a surface change gets seen.
# `/docs` and `/redoc` are dropped in production (app/main.py); the suite runs outside it.
HOME_ALLOWLIST = {
    ("GET", "/.well-known/apple-app-site-association"),
    ("GET", "/api/admin/digest/hosting"),
    ("GET", "/api/admin/digest/preview"),
    ("GET", "/api/admin/digest/recipients"),
    ("POST", "/api/admin/digest/recipients"),
    ("DELETE", "/api/admin/digest/recipients/{recipient_id}"),
    ("PATCH", "/api/admin/digest/recipients/{recipient_id}"),
    ("POST", "/api/admin/digest/run"),
    ("GET", "/api/admin/digest/runs"),
    ("GET", "/api/admin/digest/runs/{run_id}"),
    ("GET", "/api/admin/digest/settings"),
    ("PUT", "/api/admin/digest/settings"),
    ("POST", "/api/admin/digest/test-send"),
    ("GET", "/api/admin/export/settings"),
    ("PUT", "/api/admin/export/settings"),
    ("GET", "/api/attachments"),
    ("GET", "/api/attachments/buckets"),
    ("GET", "/api/attachments/picture-tags"),
    ("GET", "/api/attachments/tags"),
    ("DELETE", "/api/attachments/{uid}"),
    ("GET", "/api/attachments/{uid}"),
    ("POST", "/api/attachments/{uid}/print"),
    ("DELETE", "/api/attachments/{uid}/rendition/{variant_key}"),
    ("GET", "/api/attachments/{uid}/rendition/{variant_key}"),
    ("GET", "/api/attachments/{uid}/renditions"),
    ("POST", "/api/attachments/{uid}/renditions"),
    ("DELETE", "/api/attachments/{uid}/tags/{name}"),
    ("PUT", "/api/attachments/{uid}/tags/{name}"),
    ("GET", "/api/attachments/{uid}/{tier}"),
    ("GET", "/api/auth/callback"),
    ("POST", "/api/auth/local/login"),
    ("GET", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/native/exchange"),
    ("POST", "/api/auth/native/logout"),
    ("POST", "/api/auth/native/manage-grant"),
    ("POST", "/api/auth/native/renew"),
    ("GET", "/api/auth/native/token"),
    ("PATCH", "/api/auth/native/token"),
    ("GET", "/api/auth/user"),
    ("GET", "/api/collection-covers"),
    ("DELETE", "/api/collection-covers/{kind}/{key}"),
    ("PUT", "/api/collection-covers/{kind}/{key}"),
    ("GET", "/api/collections"),
    ("POST", "/api/collections"),
    ("DELETE", "/api/collections/{uid}"),
    ("GET", "/api/collections/{uid}"),
    ("PATCH", "/api/collections/{uid}"),
    ("GET", "/api/collections/{uid}/attachments"),
    ("POST", "/api/collections/{uid}/attachments"),
    ("DELETE", "/api/collections/{uid}/exclusions/{attachment_uid}"),
    ("PUT", "/api/collections/{uid}/exclusions/{attachment_uid}"),
    ("DELETE", "/api/collections/{uid}/members/{attachment_uid}"),
    ("GET", "/api/collections/{uid}/passes"),
    ("POST", "/api/collections/{uid}/passes"),
    ("DELETE", "/api/devices/live-activity-token"),
    ("POST", "/api/devices/live-activity-token"),
    ("GET", "/api/events"),
    ("GET", "/api/export"),
    ("GET", "/api/exports/photobook"),
    ("GET", "/api/map/assets/{path:path}"),
    ("GET", "/api/map/manifest"),
    ("GET", "/api/map/markers"),
    ("GET", "/api/map/tiles/{name}"),
    ("HEAD", "/api/map/tiles/{name}"),
    ("GET", "/api/me"),
    ("PATCH", "/api/me"),
    ("GET", "/api/members"),
    ("GET", "/api/memos"),
    ("POST", "/api/memos"),
    ("DELETE", "/api/memos/{uid}"),
    ("GET", "/api/memos/{uid}"),
    ("PATCH", "/api/memos/{uid}"),
    ("POST", "/api/memos/{uid}/pin"),
    ("POST", "/api/memos/{uid}/reactions"),
    ("DELETE", "/api/memos/{uid}/reactions/{emoji}"),
    ("POST", "/api/memos/{uid}/unpin"),
    ("DELETE", "/api/passes/{uid}"),
    ("GET", "/api/passes/{uid}"),
    ("PATCH", "/api/passes/{uid}"),
    ("GET", "/api/passes/{uid}/contributions"),
    ("POST", "/api/passes/{uid}/contributions/hide"),
    ("POST", "/api/passes/{uid}/contributions/remove"),
    ("POST", "/api/passes/{uid}/contributions/unhide"),
    ("GET", "/api/passes/{uid}/devices"),
    ("DELETE", "/api/passes/{uid}/devices/{device_uid}"),
    ("POST", "/api/passes/{uid}/unsuspend"),
    ("GET", "/api/pats"),
    ("POST", "/api/pats"),
    ("DELETE", "/api/pats/{pat_id}"),
    ("DELETE", "/api/push/subscribe"),
    ("POST", "/api/push/subscribe"),
    ("GET", "/api/push/vapid-public-key"),
    ("GET", "/api/settings"),
    ("PATCH", "/api/settings"),
    ("GET", "/api/stats/heatmap"),
    ("GET", "/api/tags"),
    ("POST", "/api/tokens/capture-grant"),
    ("DELETE", "/api/tokens/capture-grant/{grant_id}"),
    ("GET", "/api/trip-ingest/quarantine"),
    ("POST", "/api/trip-ingest/quarantine/unknown/resolve"),
    ("POST", "/api/trip-ingest/quarantine/{batch_uid}/resolve"),
    ("POST", "/api/uploads"),
    ("POST", "/api/uploads/batch"),
    ("GET", "/api/uploads/limits"),
    ("DELETE", "/api/uploads/{upload_uid}"),
    ("HEAD", "/api/uploads/{upload_uid}"),
    ("PATCH", "/api/uploads/{upload_uid}"),
    ("GET", "/api/uploads/{upload_uid}/capture"),
    ("POST", "/api/uploads/{upload_uid}/finalize"),
    ("GET", "/api/v1/memos"),
    ("GET", "/api/v1/memos/{memo_id}"),
    ("DELETE", "/api/{path:path}"),
    ("GET", "/api/{path:path}"),
    ("PATCH", "/api/{path:path}"),
    ("POST", "/api/{path:path}"),
    ("PUT", "/api/{path:path}"),
    ("GET", "/docs"),
    ("HEAD", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("GET", "/file/attachments/{uid}/{filename}"),
    ("GET", "/health"),
    ("GET", "/ios/auth/callback"),
    ("GET", "/metrics"),
    ("GET", "/openapi.json"),
    ("HEAD", "/openapi.json"),
    ("GET", "/privacy"),
    ("GET", "/redoc"),
    ("HEAD", "/redoc"),
    ("GET", "/{path:path}"),
}
# The two catch-alls answer every path, so they cannot excuse a probe the way a real route does.
HOME_CATCH_ALLS = {
    ("GET", "/{path:path}"),
    *((m, "/api/{path:path}") for m in ("GET", "POST", "PUT", "DELETE", "PATCH")),
}
ALLOWLISTS = {
    "ingest": GUEST_ALLOWLIST,
    "ingest-home": HOME_IFACE_ALLOWLIST,
    "home": HOME_ALLOWLIST - HOME_CATCH_ALLS,
}


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


def test_the_home_role_serves_exactly_its_allowlist():
    built = {("MOUNT", "/assets")} if FRONTEND_DIR.is_dir() else set()
    assert route_set(home_app) == HOME_ALLOWLIST | built


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


async def asgi_answer(app, method: str, raw: str, headers: dict[str, str]) -> tuple[int, bytes]:
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
    body = b""

    async def send(message):
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, body


async def asgi_call(app, method: str, raw: str, headers: dict[str, str]) -> int:
    return (await asgi_answer(app, method, raw, headers))[0]


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


SHELL = b"<!doctype html><title>the member app shell</title>"


@pytest.fixture
def home_as_built(tmp_path, monkeypatch):
    """Home's real table and middleware with the SPA tiers as the image has them. frontend/dist is
    a build output, so a checkout otherwise only runs the "not built" 503 branch, which is not
    what production answers."""
    tree = tmp_path / "dist"
    (tree / "assets").mkdir(parents=True)
    (tree / "index.html").write_bytes(SHELL)
    kept = [
        r
        for r in home_app.router.routes
        if not isinstance(r, Mount) and getattr(r, "path", None) != "/{path:path}"
    ]
    monkeypatch.setattr(home_app.router, "routes", kept)
    mount_spa(home_app, tree)
    return home_app


def test_home_as_built_is_the_allowlist_plus_the_asset_tier(home_as_built):
    assert route_set(home_as_built) == HOME_ALLOWLIST | {("MOUNT", "/assets")}


async def test_the_union_of_every_roles_paths_is_refused_outside_the_home_allowlist(
    home_as_built,
    door,  # noqa: F811
    home_iface,  # noqa: F811
    pat_client,
):
    _, secret = await seeded_pass(home_iface)
    guest_token = (await redeem(door, secret))["device_token"]
    _, _, member_token = pat_client
    credentials = {
        "none": {},
        "guest": {"Authorization": f"Bearer {guest_token}"},
        "member-pat": {"Authorization": "Bearer memos_pat_" + "a" * 43},
        "member-session": {"Cookie": "memos_sid=abc"},
        "service": {"Authorization": f"Bearer {SERVICE_TOKEN}"},
        "real-member": {"Authorization": f"Bearer {member_token}"},
    }
    probes = union_probes()
    assert len(probes) > 100
    checked = shell = 0
    for raw, _kind in probes:
        path = unquote(raw)
        for method in METHODS:
            if _allowed("home", method, path):
                continue
            for name, headers in credentials.items():
                status, body = await asgi_answer(home_as_built, method, raw, headers)
                where = (method, raw, name, status)
                checked += 1
                if path.startswith(("/p/", "/guest-assets")):  # never the shell: the door's alone
                    assert status in (404, 405), where
                elif status == 200:  # the catch-all: static bytes, not a route's answer
                    assert (method, body) == ("GET", SHELL), where
                    shell += 1
                else:
                    assert status in (401, 404, 405), where
    assert checked > 3000
    assert shell > 0  # control: the image's catch-all is what answered, not the 503 branch
    # Positive controls through the very same call: home's own routes answer.
    assert await asgi_call(home_as_built, "GET", "/health", {}) == 200
    assert await asgi_call(home_as_built, "GET", "/api/memos", {}) == 401
    assert await asgi_call(home_as_built, "GET", "/api/memos", credentials["real-member"]) == 200
    for fake in ("guest", "service", "member-pat"):
        assert await asgi_call(home_as_built, "GET", "/api/memos", credentials[fake]) == 401, fake


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


# --- the guest door mints no signed URL --------------------------------------------------------

SIGNED_URL = re.compile(rb"[?&]sig=|[?&]exp=\d|/api/attachments/|/file/attachments/")


async def test_no_signed_url_is_minted_on_the_guest_door_surface(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Every route of the door is driven through a whole guest session. No answer may carry a
    signed URL, and the signer's one funnel must never run: a response model could hide a URL it
    minted and a new field could carry one past the spy, so each check covers the other."""
    minted: list[str] = []
    real_sig = asset_urls._sig

    def spy(uid: str, *rest) -> str:
        minted.append(uid)
        return real_sig(uid, *rest)

    monkeypatch.setattr(asset_urls, "_sig", spy)
    # Controls: all three minting entry points reach the spy, and the shape matches what they mint.
    for url in (
        asset_urls.sign_asset_url("u", "thumb", 1, now=1.0),
        *asset_urls.signed_tier_urls("u", 1, now=1.0).values(),
        asset_urls.signed_cover_url("u", "image/jpeg", 1, now=1.0),
    ):
        assert SIGNED_URL.search(url.encode()), url
    assert len(minted) >= 3
    minted.clear()

    dist = tmp_path / "dist-guest"
    (dist / "guest-assets").mkdir(parents=True)
    (dist / "guest.html").write_text("<!doctype html><title>guest</title>")
    (dist / "guest-assets" / "guest-abcd1234.js").write_bytes(b"console.log('guest')")
    monkeypatch.setattr(guest, "GUEST_DIST", dist)
    monkeypatch.setattr(settings, "APPLE_TEAM_ID", "TEAM123456")

    answers: list[tuple[str, str, int, bytes]] = []

    async def keep(response):
        await response.aread()
        heads = "".join(f"{k}: {v}\n" for k, v in response.headers.items()).encode()
        request = response.request
        answers.append(
            (request.method, request.url.path, response.status_code, heads + response.content)
        )

    door.event_hooks["response"].append(keep)

    _, secret = await seeded_pass(home_iface)
    assert (await door.get("/health")).status_code == 200
    assert (await door.get("/.well-known/apple-app-site-association")).status_code == 200
    assert (await door.get("/p/joined")).status_code == 200
    assert (await door.get("/guest-assets/guest-abcd1234.js")).status_code == 200
    token = (await redeem(door, secret))["device_token"]
    assert (await door.get("/api/guest/me", headers=bearer(token))).status_code == 200
    uid = await upload(door, token, JPEG)
    assert (await door.head(f"/api/guest/uploads/{uid}", headers=bearer(token))).status_code == 200
    done = await complete(door, token, [uid])
    assert done.status_code in (200, 201), done.text
    assert (await door.get("/api/guest/batches", headers=bearer(token))).status_code == 200

    # Control: the session really walked the whole door, so a zero below covers every route.
    walked = {
        (m, t)
        for m, t in GUEST_ALLOWLIST
        for method, path, status, _ in answers
        if m == method and status < 300 and _template_regex(t).match(path)
    }
    assert walked == GUEST_ALLOWLIST
    assert minted == []
    assert [a[:3] for a in answers if SIGNED_URL.search(a[3])] == []


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


def test_no_signed_url_can_be_minted_on_the_guest_door_because_the_signer_is_not_loaded():
    door_modules = _fresh("app.ingest.asgi", "ingest")
    assert door_modules.returncode == 0, door_modules.stderr
    assert "app.ingest.guest" in door_modules.stdout.split()  # control: the listing is real
    assert "app.services.asset_urls" not in door_modules.stdout.split()
    home_modules = _fresh("app.main", "home")
    assert home_modules.returncode == 0, home_modules.stderr
    assert "app.services.asset_urls" in home_modules.stdout.split()  # control: home does load it


def test_the_forbidden_pattern_does_see_the_member_routers_of_home():
    done = _fresh("app.main", "home")
    assert done.returncode == 0, done.stderr
    assert "app.routers.memos" in [m for m in done.stdout.split() if FORBIDDEN.match(m)]


def test_each_entry_module_refuses_the_other_role():
    refused = _fresh("app.main", "ingest")
    assert refused.returncode != 0 and "app.ingest.asgi:app" in refused.stderr
    refused = _fresh("app.ingest.asgi", "home")
    assert refused.returncode != 0 and "app.main:app" in refused.stderr
