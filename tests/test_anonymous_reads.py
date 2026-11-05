"""MEMNG-835: no route reads memo data for a caller that presents no credential.

PUBLIC means "every signed-in member of the household", never "the internet". The app enforces
that itself rather than trusting the edge router: an anonymous request reaching the container
directly (inside the docker network, Traefik bypassed) is refused with 401 on every surface
that reads memo data, and only the routes a client needs BEFORE it holds a credential answer it.

Two halves:

* `test_every_route_refuses_an_anonymous_caller` walks the LIVE app's routes and sends each
  method an anonymous request (and a bogus bearer), so a dependency that only optionally
  authenticates — the old compat `optional_user` — is caught by what it answers, not by its
  name. A future route is refused by default: it either authenticates or is added to
  `ANONYMOUS_ROUTES` below with its reason, in review.
* `test_a_read_route_answers_401_anonymously_and_200_to_a_bearer` drives every memo-data read
  route with real seeded ids, so the 401 is shown next to the 200 a device token still gets.
"""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

import pytest
from app import auth
from app.config import settings
from app.main import FRONTEND_DIR, HashedAssetFiles, app, mount_spa
from app.models import User
from fastapi import Depends, FastAPI, WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import create_engine, text
from starlette.convertors import FloatConvertor, UUIDConvertor
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, Mount, Route

from tests.conftest import SYNC_URL

# Every (method, path template) that answers WITHOUT a credential, each with why. Nothing here
# reads memo data. Adding an entry is a reviewed act: the route then reaches the internet as soon
# as the edge lets it through.
ANONYMOUS_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness probe, and the iOS sign-in handshake reads apiMajor from it "
    "before the device holds a token",
    ("GET", "/privacy"): "the published privacy policy; App Store review reads it signed out",
    ("GET", "/metrics"): "Prometheus scrape: request counters by route template, no memo content",
    ("GET", "/api/auth/login"): "starts the OIDC login; the caller has no credential yet",
    ("GET", "/api/auth/callback"): "the OIDC redirect target; the code and state are its authority",
    ("POST", "/api/auth/logout"): "ends the session the cookie names, if any; a no-op otherwise",
    ("POST", "/api/auth/local/login"): "LOCAL_LOGIN sign-in; the username and password are the "
    "authority, and with LOCAL_LOGIN unset it answers 404",
    ("POST", "/api/auth/native/exchange"): "a device obtains its first token here; requiring one "
    "would be circular",
    ("POST", "/api/auth/native/manage-grant"): "the handoff code minted by a fresh Authelia login "
    "and its PKCE verifier are the authority",
    ("GET", "/ios/auth/callback"): "the HTTPS callback the native login is redirected to",
    ("GET", "/.well-known/apple-app-site-association"): "Apple fetches the association file "
    "anonymously",
    ("GET", "/api/push/vapid-public-key"): "the server's public VAPID key, needed to subscribe; "
    "no memo data",
    ("GET", "/openapi.json"): "the API schema: route shapes, no memo data",
    ("HEAD", "/openapi.json"): "the API schema: route shapes, no memo data",
    # Registered only outside production (app/main.py, docs_url/redoc_url).
    ("GET", "/docs"): "development-only schema browser, absent in production",
    ("HEAD", "/docs"): "development-only schema browser, absent in production",
    ("GET", "/docs/oauth2-redirect"): "development-only schema browser, absent in production",
    ("HEAD", "/docs/oauth2-redirect"): "development-only schema browser, absent in production",
    ("GET", "/redoc"): "development-only schema browser, absent in production",
    ("HEAD", "/redoc"): "development-only schema browser, absent in production",
    # The /api/* fallback answers a JSON 404 for an unmatched API path; it reads nothing.
    **{
        (m, "/api/{path}"): "JSON 404 for an unmatched API path; reads nothing"
        for m in (
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        )
    },
    ("GET", "/{path}"): "the SPA shell: static HTML that signs the browser in; reads no data",
}
# Static mounts, by path: the content-hashed SPA build (only present when the frontend is built).
ANONYMOUS_MOUNTS = {"/assets": "content-hashed SPA scripts and styles; no data"}

# A credential that is presented but valid for nobody must be refused too — the old compat
# surface downgraded it to anonymous and served PUBLIC memos.
BOGUS_BEARERS = ("Bearer memos_pat_not-a-real-token", "Bearer not-even-a-pat")

_PARAM = re.compile(r"\{([^}:]+)[^}]*\}")
# A path parameter is filled with "1" unless the route validates it against a fixed set BEFORE it
# authenticates: `/api/attachments/{uid}/{tier}` answers an unknown tier 404 without a lookup (the
# signed fast path must stay DB-free), so it is probed on a real tier, which reaches its auth.
SAMPLE_PARAMS = {"tier": "original"}


def _operations(routes: Iterable[BaseRoute]) -> list[tuple[str, str, APIRoute | Route]]:
    found: list[tuple[str, str, APIRoute | Route]] = []
    for route in routes:
        if isinstance(route, APIRoute | Route):
            found.extend((m, route.path_format, route) for m in sorted(route.methods or ()))
    return found


def _sample(route: APIRoute | Route, name: str) -> str:
    """A value the route's own converter accepts, so the probe reaches THIS route."""
    if name in SAMPLE_PARAMS:
        return SAMPLE_PARAMS[name]
    convertor = route.param_convertors.get(name)
    if isinstance(convertor, UUIDConvertor):
        return "00000000-0000-0000-0000-000000000001"
    if isinstance(convertor, FloatConvertor):
        return "1.5"
    return "1"


def _first_match(target: FastAPI, method: str, url: str) -> BaseRoute | None:
    scope = {"type": "http", "method": method, "path": url, "root_path": "", "headers": []}
    for route in target.router.routes:
        if route.matches(scope)[0] is Match.FULL:
            return route
    return None


def anonymous_answers(
    target: FastAPI, allowed: Iterable[tuple[str, str]]
) -> list[tuple[str, str, str, int]]:
    """Every (method, path, credential, status) where a credential-less or bogus-credential request
    to a route NOT in `allowed` was answered with anything but 401. Empty means the app holds.

    Two shapes are reported with a sentinel status instead of being probed, because a probe could
    not speak for them: a route of a kind this walk does not know how to send a request to
    (`unwalkable`, -1 — a WebSocket route, say), and a probe URL that the router would hand to some
    EARLIER route (`shadowed`, -2), whose 401 would then be mistaken for this one's."""
    allowed = set(allowed)
    client = TestClient(target, raise_server_exceptions=False)
    leaks: list[tuple[str, str, str, int]] = []
    for route in target.router.routes:
        # A Route with no method list accepts every method (a raw ASGI endpoint), so it yields no
        # operation to probe and would otherwise pass in silence.
        methodless = isinstance(route, Route) and not route.methods
        if methodless or not isinstance(route, APIRoute | Route | Mount):
            leaks.append(("?", getattr(route, "path", repr(route)), "unwalkable", -1))
    for method, path, route in _operations(target.router.routes):
        if (method, path) in allowed:
            continue
        url = _PARAM.sub(lambda m, r=route: _sample(r, m.group(1)), path)
        if _first_match(target, method, url) is not route:
            leaks.append((method, path, "shadowed", -2))
            continue
        for label, headers in (("none", {}), *((b, {"Authorization": b}) for b in BOGUS_BEARERS)):
            status = client.request(method, url, headers=headers).status_code
            if status != 401:
                leaks.append((method, path, label, status))
    return leaks


def unexpected_mounts(routes: Iterable[BaseRoute], frontend_dir: Path = FRONTEND_DIR) -> list[str]:
    """Every mounted application that is not the SPA's hashed-asset tier. A mount is checked by
    WHAT it serves, not only where: any ASGI app could be mounted at /assets, and the asset tier
    itself could be pointed at a directory of uploaded media."""
    odd: list[str] = []
    for route in routes:
        if not isinstance(route, Mount):
            continue
        app_ = route.app
        files = app_.app if isinstance(app_, GZipMiddleware) else None
        genuine = (
            isinstance(files, HashedAssetFiles)
            and files.directory is not None
            and Path(files.directory).resolve() == (frontend_dir / "assets").resolve()
        )
        if route.path not in ANONYMOUS_MOUNTS or not genuine:
            odd.append(route.path)
    return odd


def test_every_route_refuses_an_anonymous_caller():
    assert anonymous_answers(app, ANONYMOUS_ROUTES) == []


def test_the_allow_list_names_only_live_routes():
    """An entry for a route that no longer exists would silently pre-approve whatever later takes
    its path, so the list is held to the app as it stands."""
    live = {(method, path) for method, path, _ in _operations(app.router.routes)}
    assert set(ANONYMOUS_ROUTES) <= live, set(ANONYMOUS_ROUTES) - live


def test_no_mount_other_than_the_spa_assets():
    assert unexpected_mounts(app.router.routes) == []


def test_the_mount_check_knows_the_real_spa_tier_from_an_impostor(tmp_path):
    """frontend/dist exists only in the image, so the live app above has no mount in a checkout;
    the genuine tier is stood up over a fixture tree to show the check passes it, next to an
    arbitrary app mounted at the same path, which it must not."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html>")
    genuine = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mount_spa(genuine, tmp_path)
    assert unexpected_mounts(genuine.router.routes, tmp_path) == []

    impostor = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    impostor.mount("/assets", FastAPI())
    assert unexpected_mounts(impostor.router.routes, tmp_path) == ["/assets"]

    # the genuine stack, aimed at somewhere other than the build's assets
    (tmp_path / "media").mkdir()
    misaimed = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    misaimed.mount("/assets", GZipMiddleware(HashedAssetFiles(directory=tmp_path / "media")))
    assert unexpected_mounts(misaimed.router.routes, tmp_path) == ["/assets"]


def test_the_walk_reports_a_route_it_cannot_probe():
    probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @probe.websocket("/api/live")
    async def live(ws: WebSocket):
        await ws.accept()

    assert anonymous_answers(probe, set()) == [("?", "/api/live", "unwalkable", -1)]


def test_the_walk_reports_a_route_that_accepts_every_method():
    class Raw:  # an ASGI app, not a function: Starlette then leaves the route's methods unset
        async def __call__(self, scope, receive, send):
            await JSONResponse({"memo": "leaked"})(scope, receive, send)

    probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    probe.router.routes.append(Route("/api/raw", Raw()))
    assert probe.router.routes[-1].methods is None
    assert anonymous_answers(probe, set()) == [("?", "/api/raw", "unwalkable", -1)]


def test_the_walk_probes_each_route_on_a_value_its_converter_accepts():
    """A guarded int route declared ahead of an anonymous uuid route on the same template: probed
    with "1", the uuid route's request would be answered by the int route's 401. Each probe must
    reach the route it is for."""
    probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def guarded(user: Annotated[User, Depends(auth.require_pat_or_user)]):
        return {"id": user.id}

    async def anonymous():
        return {"memo": "leaked"}

    probe.add_api_route("/api/things/{key:int}", guarded, methods=["GET"])
    probe.add_api_route("/api/things/{key:uuid}", anonymous, methods=["GET"])
    leaks = anonymous_answers(probe, set())
    assert {(p, label) for _, p, label, _ in leaks} == {
        ("/api/things/{key}", "none"),
        ("/api/things/{key}", BOGUS_BEARERS[0]),
        ("/api/things/{key}", BOGUS_BEARERS[1]),
    }
    assert all(status == 200 for *_, status in leaks)


def test_the_walk_reports_a_probe_an_earlier_route_would_answer():
    probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def guarded(user: Annotated[User, Depends(auth.require_pat_or_user)]):
        return {"id": user.id}

    async def anonymous():
        return {"memo": "leaked"}

    probe.add_api_route("/api/things/{key}", guarded, methods=["GET"])
    probe.add_api_route("/api/things/{key:int}", anonymous, methods=["GET"])
    assert anonymous_answers(probe, set()) == [("GET", "/api/things/{key}", "shadowed", -2)]


def test_the_walk_catches_a_route_that_takes_no_credential():
    """The walker against a route that forgot its dependency, and one whose dependency only
    OPTIONALLY authenticates: both must be reported, or the walk above proves nothing."""
    probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @probe.get("/api/forgot/{uid}")
    async def forgot(uid: str):
        return {"uid": uid}

    async def maybe_user() -> None:
        return None

    @probe.get("/api/optional")
    async def optional(user: None = Depends(maybe_user)):
        return {"user": user}

    leaks = anonymous_answers(probe, set())
    assert {(m, p) for m, p, _, _ in leaks} == {
        ("GET", "/api/forgot/{uid}"),
        ("GET", "/api/optional"),
    }


def test_the_walk_passes_a_route_that_requires_a_user():
    probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @probe.get("/api/guarded")
    async def guarded(user: Annotated[User, Depends(auth.require_pat_or_user)]):
        return {"id": user.id}

    assert anonymous_answers(probe, set()) == []


# --- every memo-data read route, with real ids -------------------------------------------------

ATT_UID = "anonread-att"


@pytest.fixture
async def read_world(pat_client, monkeypatch, tmp_path):
    """A PUBLIC memo with a photo attachment (original + thumbnail on disk) and a collection, all
    owned by the bearer's user — PUBLIC on purpose: it is the visibility the old compat surface
    handed to anyone."""
    bearer, subject, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    thumb = tmp_path / "assets" / "thumbs" / f"{ATT_UID}.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")
    original = tmp_path / "originals" / f"{ATT_UID}.jpg"
    original.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (4, 5, 6)).save(original, "JPEG")

    created = await bearer.post(
        "/api/memos", json={"content": "Anon read probe #anonread", "visibility": "PUBLIC"}
    )
    assert created.status_code == 201, created.text
    memo_uid = created.json()["uid"]
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner, memo_id = c.execute(
            text("SELECT owner_id, id FROM memos WHERE uid = :u"), {"u": memo_uid}
        ).one()
        c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
                "storage_path) VALUES (:u,:o,:m,'IMG.jpeg','image/jpeg',:s,"
                "CAST('ready' AS attachment_status),:p)"
            ),
            {
                "u": ATT_UID,
                "o": owner,
                "m": memo_id,
                "s": original.stat().st_size,
                "p": str(original),
            },
        )
    eng.dispose()
    collection = await bearer.post(
        "/api/collections", json={"title": "Anon read", "tag": "anonread"}
    )
    assert collection.status_code == 201, collection.text
    yield {
        "bearer": bearer,
        "token": token,
        "memo": memo_uid,
        "att": ATT_UID,
        "col": collection.json()["uid"],
    }


READ_ROUTES = [
    "/api/auth/user",
    "/api/memos",
    "/api/memos?q=anonread",
    "/api/memos/{memo}",
    "/api/tags",
    "/api/stats/heatmap",
    "/api/attachments",
    "/api/attachments/buckets?size=month",
    "/api/attachments/tags",
    "/api/attachments/{att}",
    "/api/attachments/{att}/renditions",
    "/api/attachments/{att}/original",
    "/api/attachments/{att}/thumb",
    "/api/collections",
    "/api/collections/{col}",
    "/api/collections/{col}/attachments",
    "/api/members",
    "/api/map/markers",
    "/api/settings",
    "/api/export",
    "/api/v1/memos",
    "/api/v1/memos/{memo}",
    "/file/attachments/{att}/IMG.jpeg?thumbnail=true",
]


@pytest.mark.parametrize("template", READ_ROUTES)
async def test_a_read_route_answers_401_anonymously_and_200_to_a_bearer(read_world, template):
    url = template.format(**read_world)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        assert (await anon.get(url)).status_code == 401
        for bogus in BOGUS_BEARERS:
            assert (await anon.get(url, headers={"Authorization": bogus})).status_code == 401
    answered = await read_world["bearer"].get(url)
    assert answered.status_code == 200, answered.text


async def test_the_compat_feed_answers_a_bearer_with_the_payload_it_always_had(read_world):
    """The device-token payload of /api/v1/memos is unchanged: the 0.29.1 envelope, the memo
    named by its uid, its attachment on it. Only the anonymous answer moved."""
    body = (await read_world["bearer"].get("/api/v1/memos")).json()
    assert set(body) == {"memos", "nextPageToken"}
    memo = next(m for m in body["memos"] if m["name"] == f"memos/{read_world['memo']}")
    assert memo["visibility"] == "PUBLIC"
    assert [a["name"] for a in memo["attachments"]] == [f"attachments/{ATT_UID}"]
    single = (await read_world["bearer"].get(f"/api/v1/memos/{read_world['memo']}")).json()
    assert single == memo


async def test_a_signed_media_url_is_still_its_own_credential(read_world):
    """The one lane that serves without a cookie or a bearer stays open: a signed tier URL, minted
    by an authenticated read, is the capability the iOS image loader and the SPA's <img> tags use
    (ADR-0009). The same tier unsigned is a 401, and a tampered signature is refused."""
    meta = (await read_world["bearer"].get(f"/api/attachments/{ATT_UID}")).json()
    signed = meta["thumbnailUrl"]
    assert "sig=" in signed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        served = await anon.get(signed)
        assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"
        assert (await anon.get(f"/api/attachments/{ATT_UID}/thumb")).status_code == 401
        assert (
            await anon.get(signed[:-2] + ("00" if signed[-2:] != "00" else "11"))
        ).status_code == 403
