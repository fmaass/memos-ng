"""`/api/collection-covers` (MEMNG-853): one chosen cover per DERIVED collection — a tag, a month
or a year — kept for the whole household.

Trips and albums are stored records and carry their own cover (`/api/collections`); tag, month and
year collections are computed, so their cover lives in `collection_covers`, keyed by kind and key.
Setting one asks only that the caller may SEE the picture (`_viewable`); reading them lists only
the covers whose picture the READER may see, so a deleted or hidden picture simply drops out and
the client falls back to its default cover.

Every test starts from an empty library and no covers (`_wipe`); expected values are literals.
Tags carry a `c853` prefix and dates live in 2031, so nothing seeded elsewhere can match.
"""

import asyncio
import copy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from alembic import command
from alembic.config import Config
from app.main import (
    SECURITY_SCHEMES,
    app,
    attach_database_busy_responses,
    attach_route_security,
    collapse_nullable_anyof,
)
from app.services import collection_covers as covers_svc
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.test_attachments_taken import _sql
from tests.test_uploads import _login_as

OTHER = "c853_other"
THIRD = "c853_third"
# The revision this module's migration sits on; the round trip walks down to it by NAME, so a
# migration landing on top later cannot make `-1` silently test a different revision.
BEFORE_COVERS_REVISION = "5c2e9a7d41b8"

OPERATIONS = {
    ("get", "/api/collection-covers"),
    ("put", "/api/collection-covers/{kind}/{key}"),
    ("delete", "/api/collection-covers/{kind}/{key}"),
}

# One valid key per kind — a tag name, a month and a year.
KEYS = {"tag": "c853trip", "month": "2031-03", "year": "2031"}


def _wipe():
    for table in (
        "collection_covers",
        "collection_exclusions",
        "collections",
        "frame_share_jobs",
        "media_jobs",
        "attachments",
        "memos",
        "tags",
    ):
        _sql(f"DELETE FROM {table}")


@pytest.fixture
def lib(logged_in_client):
    """The logged-in member A over an empty library and no covers."""
    _wipe()
    try:
        yield logged_in_client
    finally:
        _wipe()


@pytest.fixture
def three(lib, monkeypatch):
    """A (the logged-in client), B and C — three members with real sessions of their own."""
    a_client, a = lib
    for s in (OTHER, THIRD):
        _sql("DELETE FROM users WHERE subject=:s", {"s": s})
    b_client = _login_as(monkeypatch, OTHER)
    c_client = _login_as(monkeypatch, THIRD)
    try:
        yield a_client, a, b_client, c_client
    finally:
        _wipe()
        for s in (OTHER, THIRD):
            _sql("DELETE FROM users WHERE subject=:s", {"s": s})


def _memo(uid, owner, *, visibility="PUBLIC"):
    return _sql(
        "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, updated_at) "
        "VALUES (:u,(SELECT id FROM users WHERE subject=:o),'m',CAST(:v AS visibility),false,"
        "now(),now()) RETURNING id",
        {"u": uid, "o": owner, "v": visibility},
    )[0][0]


def _att(uid, owner, memo_id=None):
    return _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
        "storage_path, blurhash, created_at) VALUES "
        "(:u,(SELECT id FROM users WHERE subject=:o),:m,:f,'image/jpeg',1,"
        "CAST('ready' AS attachment_status),'/x','LKO2',now()) RETURNING id",
        {"u": uid, "o": owner, "m": memo_id, "f": f"{uid}.jpg"},
    )[0][0]


def _rows() -> list[tuple[str, str, str]]:
    """Every stored cover as `(kind, key, attachment uid)`, read from SQL — the oracle."""
    return [
        tuple(r)
        for r in _sql(
            "SELECT c.kind, c.key, a.uid FROM collection_covers c "
            "JOIN attachments a ON a.id = c.attachment_id ORDER BY c.kind, c.key"
        )
    ]


def _listed(client) -> list[dict[str, Any]]:
    r = client.get("/api/collection-covers")
    assert r.status_code == 200, r.text
    return r.json()


# --- set, read, clear, replace — for every kind ---------------------------------------------------


@pytest.mark.parametrize("kind", ["tag", "month", "year"])
def test_set_read_replace_and_clear_a_cover(lib, kind):
    client, me = lib
    m = _memo("sr-m", me)
    _att("sr-a", me, m)
    _att("sr-b", me, m)
    key = KEYS[kind]
    path = f"/api/collection-covers/{kind}/{key}"

    r = client.put(path, json={"attachmentUid": "sr-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": kind, "key": key, "attachmentUid": "sr-a"}
    assert _rows() == [(kind, key, "sr-a")]
    assert _listed(client) == [{"kind": kind, "key": key, "attachmentUid": "sr-a"}]

    r = client.put(path, json={"attachmentUid": "sr-b"})  # replace: still one row
    assert r.status_code == 200, r.text
    assert _rows() == [(kind, key, "sr-b")]
    assert _listed(client) == [{"kind": kind, "key": key, "attachmentUid": "sr-b"}]

    for _ in range(2):  # clearing is idempotent
        r = client.delete(path)
        assert r.status_code == 204, r.text
        assert _rows() == []
    assert _listed(client) == []


def test_the_three_kinds_are_independent_even_when_their_keys_spell_alike(lib):
    """`2031` is a valid tag name AND a valid year: the kind is part of the key, so the tag's cover
    and the year's cover are two rows, and clearing one leaves the other."""
    client, me = lib
    m = _memo("ind-m", me)
    _att("ind-a", me, m)
    _att("ind-b", me, m)
    assert client.put("/api/collection-covers/tag/2031", json={"attachmentUid": "ind-a"}).is_success
    assert client.put(
        "/api/collection-covers/year/2031", json={"attachmentUid": "ind-b"}
    ).is_success
    assert _rows() == [("tag", "2031", "ind-a"), ("year", "2031", "ind-b")]
    assert client.delete("/api/collection-covers/tag/2031").status_code == 204
    assert _rows() == [("year", "2031", "ind-b")]


def test_the_listing_is_empty_with_no_covers(lib):
    client, _ = lib
    assert _listed(client) == []


def test_a_cover_does_not_touch_a_stored_collection(lib):
    """Scope: a derived collection's cover is its own table; a trip with the same tag keeps its
    own cover, and the cover listing names no trip."""
    client, me = lib
    m = _memo("sc-m", me)
    _att("sc-a", me, m)
    _att("sc-b", me, m)
    trip = client.post(
        "/api/collections", json={"title": "T", "tag": "c853trip", "coverAttachmentUid": "sc-a"}
    )
    assert trip.status_code == 201, trip.text
    r = client.put("/api/collection-covers/tag/c853trip", json={"attachmentUid": "sc-b"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/collections/{trip.json()['uid']}").json()["cover"]["uid"] == "sc-a"
    assert _listed(client) == [{"kind": "tag", "key": "c853trip", "attachmentUid": "sc-b"}]


# --- who may set it, and who sees it --------------------------------------------------------------


def test_another_members_viewable_picture_can_be_set_and_everyone_who_can_see_it_reads_it(three):
    a_client, _, b_client, c_client = three
    _att("pub-b", OTHER, _memo("pub-m", OTHER, visibility="PUBLIC"))
    r = a_client.put("/api/collection-covers/month/2031-03", json={"attachmentUid": "pub-b"})
    assert r.status_code == 200, r.text
    expected = [{"kind": "month", "key": "2031-03", "attachmentUid": "pub-b"}]
    for client in (a_client, b_client, c_client):  # household-wide, not per user
        assert _listed(client) == expected


def test_an_unviewable_picture_is_404_and_writes_nothing(three):
    a_client, a, _, _ = three
    _att("priv-b", OTHER, _memo("priv-m", OTHER, visibility="PRIVATE"))
    _att("orphan-b", OTHER)  # another member's media on no memo
    _att("mine-a", a, _memo("mine-m", a))
    assert a_client.put(
        "/api/collection-covers/year/2031", json={"attachmentUid": "mine-a"}
    ).is_success
    for uid in ("priv-b", "orphan-b", "no-such-uid"):
        r = a_client.put("/api/collection-covers/year/2031", json={"attachmentUid": uid})
        assert r.status_code == 404, (uid, r.text)
        r = a_client.put("/api/collection-covers/tag/c853new", json={"attachmentUid": uid})
        assert r.status_code == 404, (uid, r.text)
    # Neither the existing cover was replaced nor a new one written.
    assert _rows() == [("year", "2031", "mine-a")]


def test_a_cover_the_reader_may_not_see_is_left_out_of_the_listing(three):
    """B sets a cover from B's own PRIVATE memo: B reads it, A and C do not — and nothing errors."""
    a_client, _, b_client, c_client = three
    _att("hid-b", OTHER, _memo("hid-m", OTHER, visibility="PRIVATE"))
    r = b_client.put("/api/collection-covers/tag/c853hid", json={"attachmentUid": "hid-b"})
    assert r.status_code == 200, r.text
    assert _listed(b_client) == [{"kind": "tag", "key": "c853hid", "attachmentUid": "hid-b"}]
    assert _listed(a_client) == []
    assert _listed(c_client) == []
    # Made visible later, it reappears without anyone setting it again.
    _sql("UPDATE memos SET visibility='PROTECTED' WHERE uid='hid-m'")
    assert _listed(a_client) == [{"kind": "tag", "key": "c853hid", "attachmentUid": "hid-b"}]


def test_a_deleted_attachment_falls_out_of_the_listing(lib):
    client, me = lib
    m = _memo("del-m", me)
    _att("del-a", me, m)
    _att("keep-a", me, m)
    assert client.put(
        "/api/collection-covers/tag/c853del", json={"attachmentUid": "del-a"}
    ).is_success
    assert client.put(
        "/api/collection-covers/year/2031", json={"attachmentUid": "keep-a"}
    ).is_success
    _sql("DELETE FROM attachments WHERE uid='del-a'")
    assert _listed(client) == [{"kind": "year", "key": "2031", "attachmentUid": "keep-a"}]
    assert _rows() == [("year", "2031", "keep-a")]  # the row went with the attachment


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("month", "2031-13"),
        ("month", "2031-00"),
        ("month", "2031-3"),
        ("month", "2031"),
        ("month", "2031-03-01"),
        ("year", "31"),
        ("year", "20311"),
        ("year", "2031-03"),
        ("tag", "c853-x"),
        ("tag", "#c853"),
        ("tag", "a" * 256),
    ],
)
def test_a_malformed_key_is_422_on_put_and_delete_and_writes_nothing(lib, kind, key):
    client, me = lib
    _att("mal-a", me, _memo("mal-m", me))
    path = f"/api/collection-covers/{kind}/{quote(key, safe='')}"  # `#` would start a fragment
    r = client.put(path, json={"attachmentUid": "mal-a"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"] == ["path", "key"]
    assert client.delete(path).status_code == 422
    assert _rows() == []


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("tag", "a" * 255),  # the longest tag name a memo can carry
        ("tag", "Zürich"),  # a memo's `#tag` may be any word, not only ASCII
        ("tag", "2031"),
        ("month", "2031-01"),
        ("month", "2031-12"),
        ("year", "1970"),
    ],
)
def test_every_well_formed_key_is_accepted_and_stored_as_sent(lib, kind, key):
    client, me = lib
    _att("ok-a", me, _memo("ok-m", me))
    path = f"/api/collection-covers/{kind}/{quote(key, safe='')}"
    r = client.put(path, json={"attachmentUid": "ok-a"})
    assert r.status_code == 200, r.text
    assert _rows() == [(kind, key, "ok-a")]
    assert _listed(client) == [{"kind": kind, "key": key, "attachmentUid": "ok-a"}]


def test_a_malformed_key_is_refused_before_the_attachment_is_looked_up(lib):
    """A bad key answers 422 even for a picture the caller may not see, so the two refusals never
    depend on each other's order in a way a client could probe."""
    client, _ = lib
    r = client.put("/api/collection-covers/month/2031-13", json={"attachmentUid": "no-such"})
    assert r.status_code == 422, r.text


def test_an_unknown_kind_is_422(lib):
    client, me = lib
    _att("kind-a", me, _memo("kind-m", me))
    for kind in ("trip", "album", "day", "Tag"):
        r = client.put(f"/api/collection-covers/{kind}/2031", json={"attachmentUid": "kind-a"})
        assert r.status_code == 422, (kind, r.text)
        assert client.delete(f"/api/collection-covers/{kind}/2031").status_code == 422
    assert _rows() == []


def test_a_body_without_an_attachment_uid_is_422(lib):
    client, _ = lib
    for body in ({}, {"attachmentUid": ""}, {"attachmentUid": None}):
        r = client.put("/api/collection-covers/year/2031", json=body)
        assert r.status_code == 422, (body, r.text)
    assert _rows() == []


def test_anonymous_is_401_on_every_route(lib):
    client, me = lib
    _att("anon-a", me, _memo("anon-m", me))
    assert client.put(
        "/api/collection-covers/year/2031", json={"attachmentUid": "anon-a"}
    ).is_success
    anon = TestClient(app, raise_server_exceptions=False)
    anon.headers["Origin"] = client.headers["Origin"]
    calls = [
        ("GET", "/api/collection-covers", None),
        ("PUT", "/api/collection-covers/year/2031", {"attachmentUid": "anon-a"}),
        ("DELETE", "/api/collection-covers/year/2031", None),
    ]
    assert {(m.lower(), p.replace("year/2031", "{kind}/{key}")) for m, p, _ in calls} == OPERATIONS
    for method, path, body in calls:
        r = anon.request(method, path, json=body)
        assert r.status_code == 401, (method, path, r.status_code, r.text)
    assert _rows() == [("year", "2031", "anon-a")]  # nothing an anonymous call did stuck


# --- races -------------------------------------------------------------------------------------


async def test_two_concurrent_first_puts_of_one_key_leave_one_row(lib, pat_client, monkeypatch):
    """Both requests are held after their lookups until both have looked up, so both write a key
    that did not exist: the primary key settles it as one row, the later write wins, and neither
    request sees the other's insert as an error."""
    bearer, me, _ = pat_client
    m = _memo("race-m", me)
    _att("race-a", me, m)
    _att("race-b", me, m)
    real = covers_svc.set_cover
    both_looked_up = asyncio.Barrier(2)
    entered: list[int] = []

    async def held(db, kind, key, attachment):
        entered.append(1)
        async with asyncio.timeout(10):
            await both_looked_up.wait()
        return await real(db, kind, key, attachment)

    monkeypatch.setattr(covers_svc, "set_cover", held)
    path = "/api/collection-covers/month/2031-03"
    results = await asyncio.gather(
        bearer.put(path, json={"attachmentUid": "race-a"}),
        bearer.put(path, json={"attachmentUid": "race-b"}),
    )
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert len(entered) == 2  # both really overlapped past the lookup
    rows = _rows()
    assert len(rows) == 1 and rows[0][:2] == ("month", "2031-03")
    assert rows[0][2] in {"race-a", "race-b"}


def test_a_put_whose_attachment_was_deleted_meanwhile_is_404_and_writes_nothing(lib, monkeypatch):
    """The picture passes the lookup and is deleted before the write holds it: the answer is the
    404 an unknown picture gets, never a 500 from the foreign key."""
    client, me = lib
    _att("gone-a", me, _memo("gone-m", me))
    real = covers_svc.set_cover

    async def deleted_first(db, kind, key, attachment):
        _sql("DELETE FROM attachments WHERE uid='gone-a'")
        return await real(db, kind, key, attachment)

    monkeypatch.setattr(covers_svc, "set_cover", deleted_first)
    r = client.put("/api/collection-covers/tag/c853gone", json={"attachmentUid": "gone-a"})
    assert r.status_code == 404, r.text
    assert _rows() == []


# --- the contract --------------------------------------------------------------------------------


def _covers_route(route: Any) -> bool:
    return (
        isinstance(route, APIRoute) and route.endpoint.__module__ == "app.routers.collection_covers"
    )


def _document(routes: list[Any]) -> dict[str, Any]:
    """The served document built the way `app.main.custom_openapi` builds it, over `routes`."""
    schema = get_openapi(title=app.title, version=app.version, routes=routes)
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
    collapse_nullable_anyof(schema)
    attach_route_security(schema, routes)
    attach_database_busy_responses(schema)
    return schema


def test_openapi_names_exactly_the_three_cover_operations():
    paths = app.openapi()["paths"]
    found = {
        (method, path)
        for path, item in paths.items()
        if path.startswith("/api/collection-covers")
        for method in item
    }
    assert found == OPERATIONS
    put = paths["/api/collection-covers/{kind}/{key}"]["put"]
    params = {p["name"]: p for p in put["parameters"]}
    assert params["kind"]["schema"]["enum"] == ["tag", "month", "year"]
    for code in ("200", "401", "404", "422"):
        assert code in put["responses"], code
    # The refusals are named in the contract's text, not only by their codes.
    assert "422" in put["description"] and "404" in put["description"]
    for path, item in paths.items():
        if not path.startswith("/api/collection-covers"):
            continue
        for method, op in item.items():
            assert "401" in op["responses"], (method, path)
            assert op["security"], (method, path)


def test_the_cover_routes_change_no_other_path_or_schema():
    """Built with and without this module's routes, the document differs ONLY by the cover paths
    and the schemas they introduce — purely additive, so the release is a minor version."""
    routes = list(app.routes)
    full = _document(routes)
    without = _document([r for r in routes if not _covers_route(r)])
    assert any(_covers_route(r) for r in routes)  # control: the module's routes are served
    assert set(full["paths"]) - set(without["paths"]) == {p for _, p in OPERATIONS}
    for path in without["paths"]:
        assert full["paths"][path] == without["paths"][path], path
    full_schemas = full["components"]["schemas"]
    without_schemas = without["components"]["schemas"]
    assert set(full_schemas) - set(without_schemas) == {"CollectionCoverOut", "CollectionCoverSet"}
    for name, schema in without_schemas.items():
        assert full_schemas[name] == schema, name
    rest = copy.deepcopy(full)
    for key in ("paths", "components"):
        rest.pop(key)
        without.pop(key)
    assert rest == without


# --- the migration -------------------------------------------------------------------------------


def _covers_table_exists() -> bool:
    return bool(
        _sql(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='collection_covers'"
        )
    )


def test_the_migration_round_trips(db_engine):
    """Down to the revision below it removes exactly the table; up again restores it, empty and
    with its cascade, and leaves every other table's rows where they were."""
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    users_before = _sql("SELECT count(*) FROM users")[0][0]
    assert _covers_table_exists()  # control: head carries it
    try:
        command.downgrade(cfg, BEFORE_COVERS_REVISION)
        assert not _covers_table_exists()
    finally:
        command.upgrade(cfg, "head")
    assert _covers_table_exists()
    assert _sql("SELECT count(*) FROM collection_covers")[0][0] == 0
    assert _sql("SELECT count(*) FROM users")[0][0] == users_before
    on_delete = _sql(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid='collection_covers'::regclass AND contype='f'"
    )
    assert [r[0] for r in on_delete] == ["c"]  # ON DELETE CASCADE from attachments
