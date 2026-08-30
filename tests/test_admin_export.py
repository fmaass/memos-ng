"""The admin-only export-settings API (MEMNG-238).

Two things are proved for every route: what it does for an admin, and that nobody else reaches it.
The second is a parametrised sweep over the whole route table rather than a case per handler, so a
route added to this router later is covered the moment it exists — an authorization check that has
to be remembered per endpoint is the class of hole LSN-0020 is about. It is deliberately the same
shape as tests/test_admin_digest.py's sweep, which covers only the `/api/admin/digest` prefix.
"""

import pytest
from app.config import settings as app_settings
from app.main import app
from app.services import printready
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

PREFIX = "/api/admin/export"

# (method, path) for every route this router publishes. Read off the app rather than typed out, so
# the sweeps below cannot silently stop covering a route.
ADMIN_ROUTES = sorted(
    (method, route.path)
    for route in app.routes
    if isinstance(route, APIRoute) and route.path.startswith(PREFIX)
    for method in sorted(route.methods or ())
)


@pytest.fixture(autouse=True)
def export_state(db_engine):
    """Reset the settings row around every test in this module.

    These tests drive the real API, which COMMITS — so a PUT in one would otherwise be the starting
    state of the next. The row is re-seeded the way the migration seeds it (naming only `id`), so
    each test starts from the documented defaults rather than from whatever the previous one saved.
    """

    def reset() -> None:
        engine = create_engine(SYNC_URL)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM export_settings"))
            conn.execute(text("INSERT INTO export_settings (id) VALUES (1)"))
        engine.dispose()

    reset()
    yield
    reset()


def _payload(**overrides) -> dict:
    payload = {
        "print_long_edge": 4096,
        "print_jpeg_quality": 92,
        "print_metadata_policy": "strip",
        "print_grayscale_single_channel": True,
        "mail_long_edge": 1600,
        "mail_jpeg_quality": 80,
        "mail_subsampling": "4:2:0",
        "mail_progressive": False,
    }
    payload.update(overrides)
    return payload


# --- nobody but an admin gets in --------------------------------------------------------------


def test_the_sweep_below_covers_every_route_this_router_publishes():
    """Positive control: an empty parametrisation passes for the wrong reason."""
    assert len(ADMIN_ROUTES) == 2, ADMIN_ROUTES
    assert sorted({path for _, path in ADMIN_ROUTES}) == [f"{PREFIX}/settings"]
    assert sorted(method for method, _ in ADMIN_ROUTES) == ["GET", "PUT"]


@pytest.mark.parametrize(
    ("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES]
)
def test_a_non_admin_session_is_refused_with_403(logged_in_client, method, path):
    client, _ = logged_in_client
    response = client.request(method, path, json=_payload())
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Administrator role required"


@pytest.mark.parametrize(
    ("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES]
)
def test_an_anonymous_request_is_refused(client, method, path):
    response = client.request(method, path, json=_payload())
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    ("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES]
)
async def test_an_access_token_never_reaches_an_admin_route(admin_client, method, path):
    """The token belongs to the ADMIN account itself, so the only thing that can refuse it is the
    rule that a bearer is refused before the cookie is read — not the role check.

    A separate client with an EMPTY cookie jar: reusing the admin's own client with an extra header
    would leave the session cookie in the jar and the assertion would pass with the bearer branch
    deleted.
    """
    from app import auth

    client, _ = admin_client
    created = client.post("/api/pats", json={"name": "export-device"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}", "Origin": "http://testserver"},
    ) as bearer:
        try:
            response = await bearer.request(method, path, json=_payload())
        finally:
            await auth.drain_pat_stamp_tasks()
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Session authentication required"


# --- the seeded row IS the shipped tier -------------------------------------------------------


def test_an_admin_reads_the_seeded_settings(admin_client):
    client, _ = admin_client
    response = client.get(f"{PREFIX}/settings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["print_long_edge"] == 4096
    assert body["print_jpeg_quality"] == 92
    assert body["print_metadata_policy"] == "strip"
    assert body["print_grayscale_single_channel"] is True
    assert body["mail_long_edge"] == 1600
    assert body["mail_jpeg_quality"] == 80
    assert body["mail_subsampling"] == "4:2:0"
    assert body["mail_progressive"] is False
    assert body["updated_by"] is None


def test_the_seeded_row_resolves_to_the_shipped_profile_key(admin_client):
    """The load-bearing property of the seed: the defaults ARE the profile the printready tier on
    disk was built at, so deploying this migration re-derives nothing.

    Asserted against the literal `print-4096` rather than against `DEFAULT_PROFILE.key`, which
    would agree with itself no matter what either side said.
    """
    client, _ = admin_client
    assert client.get(f"{PREFIX}/settings").json()["print_profile_key"] == "print-4096"
    assert printready.DEFAULT_PROFILE.key == "print-4096"


def test_a_put_updates_the_row_and_stamps_who_did_it(admin_client):
    client, subject = admin_client
    response = client.put(
        f"{PREFIX}/settings",
        json=_payload(
            print_long_edge=5000,
            print_jpeg_quality=95,
            print_metadata_policy="keep_dates",
            print_grayscale_single_channel=False,
            mail_long_edge=1200,
            mail_jpeg_quality=77,
            mail_subsampling="4:4:4",
            mail_progressive=True,
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["print_long_edge"] == 5000
    assert body["print_jpeg_quality"] == 95
    assert body["print_metadata_policy"] == "keep_dates"
    assert body["print_grayscale_single_channel"] is False
    assert body["mail_long_edge"] == 1200
    assert body["mail_jpeg_quality"] == 77
    assert body["mail_subsampling"] == "4:4:4"
    assert body["mail_progressive"] is True
    assert body["updated_by"] == subject
    assert body["updated_at"].endswith("Z")
    # Persisted, not just echoed back out of the request.
    assert client.get(f"{PREFIX}/settings").json()["print_long_edge"] == 5000


def test_the_profile_key_follows_every_byte_affecting_field(admin_client):
    """One key per set of bytes, including the grayscale toggle.

    The toggle is the one that is easy to leave out — it changes a 3-channel JPEG into a 1-channel
    one without touching size, quality or metadata — and leaving it out would go on serving cached
    grayscale files after it was switched off. So each field is moved on its own and the key must
    move with it, and no two of the five keys may collide.
    """
    client, _ = admin_client
    keys = {}
    for label, overrides in (
        ("default", {}),
        ("edge", {"print_long_edge": 5000}),
        ("quality", {"print_jpeg_quality": 90}),
        ("metadata", {"print_metadata_policy": "keep_dates"}),
        ("grayscale", {"print_grayscale_single_channel": False}),
    ):
        response = client.put(f"{PREFIX}/settings", json=_payload(**overrides))
        assert response.status_code == 200, response.text
        keys[label] = response.json()["print_profile_key"]

    assert keys["default"] == "print-4096"
    assert len(set(keys.values())) == 5, keys
    assert keys["grayscale"] != keys["default"]
    # Path-safe: the key becomes a directory name under assets/printready/.
    for key in keys.values():
        assert "/" not in key and ".." not in key


@pytest.mark.parametrize(
    "overrides",
    [
        {"print_long_edge": 3000},
        {"print_long_edge": 4097},
        {"print_jpeg_quality": 89},
        {"print_jpeg_quality": 96},
        {"print_metadata_policy": "keep_everything"},
        {"print_grayscale_single_channel": "yes please"},
        {"mail_long_edge": 1000},
        {"mail_long_edge": 1601},
        {"mail_jpeg_quality": 76},
        {"mail_jpeg_quality": 84},
        {"mail_subsampling": "4:2:2"},
        {"mail_subsampling": "none"},
    ],
)
def test_a_value_outside_its_band_is_refused(admin_client, overrides):
    client, _ = admin_client
    assert client.put(f"{PREFIX}/settings", json=_payload(**overrides)).status_code == 422


def test_a_partial_body_is_refused_rather_than_merged(admin_client):
    """The PUT is the whole row: an omitted field is a client bug, not "leave it alone"."""
    client, _ = admin_client
    body = _payload()
    del body["print_jpeg_quality"]
    assert client.put(f"{PREFIX}/settings", json=body).status_code == 422


def test_a_refused_put_changes_nothing(admin_client):
    client, _ = admin_client
    refused = client.put(f"{PREFIX}/settings", json=_payload(print_jpeg_quality=200))
    assert refused.status_code == 422
    assert client.get(f"{PREFIX}/settings").json()["print_jpeg_quality"] == 92


# --- one row, and only ever one ---------------------------------------------------------------


def test_there_is_no_create_path_and_a_second_row_is_impossible(admin_client):
    """`CHECK (id = 1)` plus a router that only UPDATEs. The check is asserted against the database
    rather than reasoned about, because it is the half that survives a router bug."""
    client, _ = admin_client
    assert client.put(f"{PREFIX}/settings", json=_payload(print_jpeg_quality=93)).status_code == 200

    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM export_settings")).scalar_one() == 1
    with engine.connect() as conn, pytest.raises(Exception, match="ck_export_settings_singleton"):
        with conn.begin():
            conn.execute(text("INSERT INTO export_settings (id) VALUES (2)"))
    # Still one row after the refused insert, and the update above did not add a second.
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM export_settings")).scalar_one() == 1
    engine.dispose()

    # POST is not a verb this resource has at all — it never reaches a handler. (The status is the
    # SPA catch-all's 404 rather than a 405, because an unrouted method falls through to it.)
    assert ("POST", f"{PREFIX}/settings") not in ADMIN_ROUTES
    assert client.post(f"{PREFIX}/settings", json=_payload()).status_code == 404


def test_a_missing_settings_row_is_a_500_not_a_silent_insert(admin_client):
    """A broken install answers loudly. Creating the row here would be the INSERT path that makes a
    second row possible — the very thing the singleton design refuses to have."""
    client, _ = admin_client
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM export_settings"))
    engine.dispose()

    assert client.get(f"{PREFIX}/settings").status_code == 500
    assert client.put(f"{PREFIX}/settings", json=_payload()).status_code == 500

    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM export_settings")).scalar_one() == 0
    engine.dispose()


def test_the_route_is_on_the_session_lane_like_every_other_admin_surface():
    """The published contract, not just the runtime behaviour: these routes accept a cookie and
    nothing else, which is what `require_admin` in DEPENDENCY_SECURITY says."""
    paths = app.openapi()["paths"]
    for method in ("get", "put"):
        assert paths[f"{PREFIX}/settings"][method]["security"] == [
            {"sessionCookie": []}
        ], method
    assert app_settings.is_production is False  # the schema is only exported outside production
