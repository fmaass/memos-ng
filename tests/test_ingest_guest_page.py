"""The guest web page on the door (trip pass P4, MEMNG-999): the ingest role serves the guest
document at `/p/{secret}` (and so `/p/joined`) and its hashed files under `/guest-assets/`, from
the separate guest build (`frontend/dist-guest`), and nothing else static. The page and its
assets are stood up over a fixture tree here: the build output exists only in the image.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from app.config import settings
from app.ingest import core, guest
from app.ingest.factory import create_app
from httpx import ASGITransport, AsyncClient

GUEST_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "worker-src 'self'; img-src 'self' blob:; media-src blob:; manifest-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

# Shaped like the Vite guest build's document: the two placeholders the door fills in, and the
# hashed entry under guest-assets/.
DOCUMENT = (
    "<!doctype html><html><head>"
    '<meta name="memos-app-url" content="__MEMOS_APP_URL__">'
    '<meta name="memos-accept" content="__MEMOS_ACCEPT__">'
    '<script type="module" crossorigin src="/guest-assets/guest-abcd1234.js"></script>'
    '<link rel="stylesheet" crossorigin href="/guest-assets/guest-abcd1234.css">'
    '</head><body><div id="guest"></div></body></html>'
)
ENTRY = b"console.log('guest entry')\n"
STYLE = b"body{margin:0}\n"
WORKER = b"self.onmessage=()=>{}\n"


@pytest.fixture
def guest_dist(tmp_path: Path, monkeypatch) -> Path:
    dist = tmp_path / "dist-guest"
    (dist / "guest-assets").mkdir(parents=True)
    (dist / "guest.html").write_text(DOCUMENT)
    (dist / "guest-assets" / "guest-abcd1234.js").write_bytes(ENTRY)
    (dist / "guest-assets" / "guest-abcd1234.css").write_bytes(STYLE)
    (dist / "guest-assets" / "hashWorker-abcd1234.js").write_bytes(WORKER)
    # a file beside the document that must never be reachable through the asset route
    (dist / "secret.txt").write_text("not an asset")
    monkeypatch.setattr(guest, "GUEST_DIST", dist)
    monkeypatch.setattr(settings, "INGEST_GUEST_APP_URL", "")
    return dist


@pytest_asyncio.fixture
async def page_door():
    async with AsyncClient(
        transport=ASGITransport(app=create_app("ingest")), base_url="http://door"
    ) as client:
        yield client


def _meta(html: str, name: str) -> str:
    marker = f'<meta name="{name}" content="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


@pytest.mark.parametrize("path", ["/p/some-secret-value", "/p/joined"])
async def test_the_guest_document_carries_its_policy_and_never_the_secret(
    guest_dist, page_door, path
):
    r = await page_door.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == GUEST_CSP
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert '<div id="guest">' in r.text  # control: this is the built guest document
    assert "some-secret-value" not in r.text
    assert "__MEMOS_" not in r.text  # every placeholder was filled


async def test_the_picker_accept_list_is_the_servers_allowlist(guest_dist, page_door):
    accept = _meta((await page_door.get("/p/x")).text, "memos-accept")
    assert accept.split(",") == [f".{ext}" for ext in core.ALLOWED_EXTENSIONS]


async def test_the_app_link_is_absent_while_unset(guest_dist, page_door):
    assert _meta((await page_door.get("/p/x")).text, "memos-app-url") == ""


async def test_the_app_link_is_the_configured_https_address(guest_dist, page_door, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_GUEST_APP_URL", "https://testflight.apple.com/join/AbC")
    html = (await page_door.get("/p/x")).text
    assert _meta(html, "memos-app-url") == "https://testflight.apple.com/join/AbC"


@pytest.mark.parametrize(
    "value",
    [
        "http://testflight.apple.com/join/AbC",
        "javascript:alert(1)",
        "//testflight.apple.com/join/AbC",
        "https://",
        "itms-services://?action=download",
    ],
)
async def test_an_app_link_that_is_not_https_is_never_offered(
    guest_dist, page_door, monkeypatch, value
):
    monkeypatch.setattr(settings, "INGEST_GUEST_APP_URL", value)
    assert _meta((await page_door.get("/p/x")).text, "memos-app-url") == ""


async def test_an_app_link_cannot_break_out_of_its_attribute(guest_dist, page_door, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_GUEST_APP_URL", 'https://x.example/"><script>1</script>')
    html = (await page_door.get("/p/x")).text
    assert "<script>1</script>" not in html
    assert _meta(html, "memos-app-url").startswith("https://x.example/&quot;&gt;")


async def test_the_guest_assets_are_served_hashed_and_typed(guest_dist, page_door):
    r = await page_door.get("/guest-assets/guest-abcd1234.js")
    assert r.status_code == 200 and r.content == ENTRY
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == GUEST_CSP  # the worker's own policy
    css = await page_door.get("/guest-assets/guest-abcd1234.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    worker = await page_door.get("/guest-assets/hashWorker-abcd1234.js")
    assert worker.status_code == 200 and worker.content == WORKER


@pytest.mark.parametrize(
    "path",
    [
        "/guest-assets/nope-00000000.js",
        "/guest-assets/.hidden.js",
        "/guest-assets/secret.txt",
        "/guest-assets/..%2fsecret.txt",
        "/guest-assets/..%2fguest.html",
        "/guest-assets/%2e%2e/guest.html",
        "/guest-assets/",
        "/guest-assets",
        "/guest.html",
        "/secret.txt",
        "/index.html",
        "/assets/index.js",
        "/",
        "/p/",
        "/p/a/b",
        "/sw.js",
        "/manifest.webmanifest",
        "/favicon.ico",
    ],
)
async def test_nothing_else_static_is_reachable_on_the_door(guest_dist, page_door, path):
    r = await page_door.get(path)
    assert r.status_code == 404, (path, r.status_code)
    assert "not an asset" not in r.text and '<div id="guest">' not in r.text


async def test_an_unbuilt_guest_page_is_a_503_that_still_echoes_nothing(
    guest_dist, page_door, monkeypatch
):
    monkeypatch.setattr(guest, "GUEST_DIST", guest_dist.parent / "absent")
    r = await page_door.get("/p/some-secret-value")
    assert r.status_code == 503
    assert "some-secret-value" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert (await page_door.get("/guest-assets/guest-abcd1234.js")).status_code == 404


async def test_the_home_interface_serves_no_guest_page(guest_dist):
    async with AsyncClient(
        transport=ASGITransport(app=create_app("ingest-home")), base_url="http://ingest-home"
    ) as client:
        for path in ("/p/x", "/p/joined", "/guest-assets/guest-abcd1234.js"):
            assert (await client.get(path)).status_code == 404, path
