"""Trip passes, the owner side (plan PLAN_2026-10-27_trip-pass-pluto P1).

A pass is a household object hung off a collection, so every signed-in member issues, lists, edits
and revokes passes on every collection — the collections rule (app/routers/collections.py:3-4);
`created_by` records who issued it and grants nothing here. The secret is shown ONCE and only its
sha256 is stored. `version` is the pass's epoch: every change raises it, and a revoke is one
terminal transition stamped with the version that performed it — re-reading, editing or revoking a
revoked pass answers 410, never a resurrection.

Every test starts from empty pass, collection and library tables (`_wipe`). Tags carry a `tp1`
prefix so nothing seeded elsewhere can match.
"""

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from app.database import async_session
from app.main import app
from app.models import Collection, User
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_attachments_taken import _sql
from tests.test_collections_api import _att, _memo
from tests.test_uploads import _login_as

OTHER = "tp1_other"
TAG = "tp1trip"
START = "2031-03-01T00:00:00Z"
END = "2031-03-10T23:59:59Z"


def _wipe():
    for table in (
        "pass_sync_state",
        "pass_events",
        "ingest_blobs",
        "ingest_batches",
        "guest_uploads",
        "pass_devices",
        "passes",
        "collection_exclusions",
        "collection_members",
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
    _wipe()
    try:
        yield logged_in_client
    finally:
        _wipe()


@pytest.fixture
def two(lib, monkeypatch):
    a_client, a = lib
    _sql("DELETE FROM users WHERE subject=:s", {"s": OTHER})
    b_client = _login_as(monkeypatch, OTHER)
    try:
        yield a_client, a, b_client, OTHER
    finally:
        _wipe()
        _sql("DELETE FROM users WHERE subject=:s", {"s": OTHER})


def _trip(client, *, tag: str | None = TAG, span: bool = False) -> str:
    body: dict = {"title": "Sardinia", "kind": "trip"}
    if tag is not None:
        body["tag"] = tag
    if span:
        body |= {"spanStart": START, "spanEnd": END}
    r = client.post("/api/collections", json=body)
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def _album(client) -> str:
    r = client.post("/api/collections", json={"title": "Best of", "kind": "album"})
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def _issue(client, coll_uid: str, **extra):
    return client.post(f"/api/collections/{coll_uid}/passes", json={"label": "friends", **extra})


def _sync_row(pass_uid: str):
    return _sql(
        "SELECT s.push_urgent, s.push_requested_at IS NOT NULL, s.acked_version "
        "FROM pass_sync_state s JOIN passes p ON p.id = s.pass_id WHERE p.uid = :u",
        {"u": pass_uid},
    )


def _device(pass_uid: str, device_uid: str, name: str = "Anna") -> int:
    return _sql(
        "INSERT INTO pass_devices (uid, pass_id, display_name, platform) VALUES "
        "(:d, (SELECT id FROM passes WHERE uid = :p), :n, 'web') RETURNING id",
        {"d": device_uid, "p": pass_uid, "n": name},
    )[0][0]


# --- issue -----------------------------------------------------------------------------------


def test_issue_shows_the_secret_once_and_stores_only_its_sha256(lib):
    client, _ = lib
    coll = _trip(client)
    r = _issue(client, coll)
    assert r.status_code == 201, r.text
    body = r.json()
    secret = body["secret"]
    assert len(secret) >= 43  # token_urlsafe(32): 256 bits
    assert body["url"].endswith(f"/p/{secret}")
    assert body["qrSvg"].lstrip().startswith("<svg") and "</svg>" in body["qrSvg"]
    p = body["pass"]
    assert p["collectionUid"] == coll and p["version"] == 1 and p["state"] == "active"
    assert p["scope"] == "contribute" and p["maxDevices"] == 30
    (stored,) = _sql("SELECT secret_hash FROM passes WHERE uid = :u", {"u": p["uid"]})[0]
    assert stored == hashlib.sha256(secret.encode()).hexdigest()
    # Shown once: no read hands the secret back.
    again = client.get(f"/api/passes/{p['uid']}")
    assert again.status_code == 200 and "secret" not in again.json()
    listed = client.get(f"/api/collections/{coll}/passes").json()
    assert [x["uid"] for x in listed] == [p["uid"]] and "secret" not in listed[0]
    assert secret not in str(listed) and secret not in again.text


def test_two_issues_mint_two_secrets(lib):
    client, _ = lib
    coll = _trip(client)
    a, b = _issue(client, coll).json(), _issue(client, coll).json()
    assert a["secret"] != b["secret"] and a["pass"]["uid"] != b["pass"]["uid"]


def _titled_trip(client, title: str) -> str:
    r = client.post(
        "/api/collections",
        json={"title": title, "kind": "trip", "spanStart": START, "spanEnd": END},
    )
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def _stored_tag(coll_uid: str) -> str | None:
    return _sql("SELECT tag FROM collections WHERE uid = :u", {"u": coll_uid})[0][0]


def test_issue_on_a_dates_only_trip_gives_it_the_squashed_title_as_tag(lib):
    client, _ = lib
    coll = _titled_trip(client, "Cyprus 2026")
    assert _stored_tag(coll) is None
    r = _issue(client, coll)
    assert r.status_code == 201, r.text
    assert _stored_tag(coll) == "cyprus2026"
    assert client.get(f"/api/collections/{coll}").json()["tag"] == "cyprus2026"
    listed = {c["uid"]: c["tag"] for c in client.get("/api/collections").json()}
    assert listed[coll] == "cyprus2026"
    (span,) = _sql("SELECT span_start IS NOT NULL FROM collections WHERE uid = :u", {"u": coll})
    assert span[0] is True  # the span stays; the tag is added beside it


def test_the_derived_tag_deliberately_pulls_in_memos_already_carrying_it(lib):
    """The owner's rule: a trip named "Cyprus 2026" holds the #cyprus2026 memos — reuse is the
    point, so there is no uniqueness check and no suffix."""
    client, me = lib
    coll = _titled_trip(client, "Cyprus 2026")
    _att("cy-old", me, _memo("cy-old-m", me, tags=("cyprus2026",)))  # outside the span
    assert client.get(f"/api/collections/{coll}").json()["memberCount"] == 0
    assert _issue(client, coll).status_code == 201
    assert _stored_tag(coll) == "cyprus2026"
    assert client.get(f"/api/collections/{coll}").json()["memberCount"] == 1


def test_issue_never_replaces_an_existing_tag(lib):
    client, _ = lib
    coll = _trip(client, tag="Keep_Me", span=True)
    assert _issue(client, coll).status_code == 201
    assert _stored_tag(coll) == "Keep_Me"


def test_issue_on_a_trip_whose_legacy_tag_fails_parse_tags_is_409_and_keeps_it(lib):
    client, _ = lib
    coll = _trip(client, span=True)
    _sql("UPDATE collections SET tag = 'has-dash' WHERE uid = :u", {"u": coll})
    r = _issue(client, coll)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "trip_has_no_tag"
    assert _stored_tag(coll) == "has-dash"
    assert _sql("SELECT count(*) FROM passes")[0][0] == 0


@pytest.mark.parametrize("title", ["!!! ---", "🏖️ 🌊", "   "])
def test_a_title_that_squashes_to_nothing_falls_back_to_trip_and_the_uid(lib, title):
    client, _ = lib
    coll = _titled_trip(client, title)
    assert _issue(client, coll).status_code == 201, title
    want = "trip" + "".join(ch for ch in coll if ch.isascii() and ch.isalnum())[:8].lower()
    assert _stored_tag(coll) == want
    from app.services.memos import parse_tags

    assert parse_tags(f"#{want}") == [want]


def test_a_trip_deleted_before_the_lock_is_404_and_stores_nothing(lib, monkeypatch):
    from app.services import passes as pass_svc

    client, _ = lib
    coll = _titled_trip(client, "Gone")

    async def deleted_meanwhile():
        _sql("DELETE FROM collections WHERE uid = :u", {"u": coll})

    monkeypatch.setattr(pass_svc, "_before_pass_lock", deleted_meanwhile)
    r = _issue(client, coll)
    assert r.status_code == 404, r.text
    assert _sql("SELECT count(*) FROM passes")[0][0] == 0


def test_the_squash_keeps_non_ascii_letters_parse_tags_reads():
    from app.services import passes as pass_svc

    assert pass_svc.derive_trip_tag("Zürich – Bern '26", "abcdefghij") == "zürichbern26"
    assert pass_svc.derive_trip_tag("Cyprus_2026", "abcdefghij") == "cyprus2026"
    assert pass_svc.derive_trip_tag("…", "a-b_c1234567") == "tripabc12345"
    # "İ".lower() is "i" plus a combining dot, which is no letter and goes.
    assert pass_svc.derive_trip_tag("İstanbul", "abcdefghij") == "istanbul"
    assert pass_svc.derive_trip_tag("", "AbC-dEf_GhIj") == "tripabcdefgh"
    assert pass_svc.derive_trip_tag("x" * 300, "abcdefghij") == "x" * 255  # the column's bound


def test_issue_on_an_album_needs_no_tag(lib):
    client, _ = lib
    album = _album(client)
    r = _issue(client, album)
    assert r.status_code == 201, r.text
    assert _stored_tag(album) is None  # an album is never given a tag


def test_issue_on_an_unknown_collection_is_404(lib):
    client, _ = lib
    assert _issue(client, "nope").status_code == 404


def test_issue_defaults_the_expiry_to_span_end_plus_14_days(lib):
    client, _ = lib
    coll = _trip(client, span=True)
    p = _issue(client, coll).json()["pass"]
    assert p["expiresAt"] == "2031-03-24T23:59:59Z"


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_trip(client, ended_days_ago: int) -> str:
    end = datetime.now(UTC) - timedelta(days=ended_days_ago)
    body = {"title": "Old trip", "kind": "trip", "tag": TAG}
    body |= {"spanStart": _iso(end - timedelta(days=7)), "spanEnd": _iso(end)}
    r = client.post("/api/collections", json=body)
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def test_issue_on_a_trip_that_ended_30_days_ago_defaults_to_14_days_from_now(lib):
    # Decided for MEMNG-1024: default to max(span end + 14 d, now + 14 d) rather than refuse.
    client, _ = lib
    coll = _past_trip(client, 30)
    before = datetime.now(UTC)
    r = _issue(client, coll)
    assert r.status_code == 201, r.text
    p = r.json()["pass"]
    assert p["state"] == "active"
    expires = datetime.fromisoformat(p["expiresAt"].replace("Z", "+00:00"))
    assert before + timedelta(days=14) - timedelta(seconds=1) <= expires
    assert expires <= datetime.now(UTC) + timedelta(days=14) + timedelta(seconds=1)


def test_issue_on_a_past_trip_keeps_an_explicit_expiry(lib):
    client, _ = lib
    coll = _past_trip(client, 30)
    explicit = _iso(datetime.now(UTC) + timedelta(days=3))
    r = _issue(client, coll, expiresAt=explicit)
    assert r.status_code == 201, r.text
    assert r.json()["pass"]["expiresAt"] == explicit


def test_issue_refuses_a_past_expiry_and_a_bad_scope(lib):
    client, _ = lib
    coll = _trip(client)
    assert _issue(client, coll, expiresAt="2001-01-01T00:00:00Z").status_code == 422
    assert _issue(client, coll, scope="admin").status_code == 422
    assert _issue(client, coll, maxDevices=0).status_code == 422
    assert _sql("SELECT count(*) FROM passes")[0][0] == 0


def test_issue_records_an_event_and_a_sync_row(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]
    kinds = _sql(
        "SELECT e.kind, e.version FROM pass_events e JOIN passes p ON p.id = e.pass_id "
        "WHERE p.uid = :u ORDER BY e.id",
        {"u": p["uid"]},
    )
    assert [tuple(k) for k in kinds] == [("issued", 1)]
    assert [tuple(r) for r in _sync_row(p["uid"])] == [(False, True, 0)]


# --- every member, not just the issuer --------------------------------------------------------


def test_a_non_creator_member_lists_edits_and_revokes_a_pass(two):
    a_client, _, b_client, b = two
    coll = _trip(a_client)
    p = _issue(a_client, coll).json()["pass"]
    listed = b_client.get(f"/api/collections/{coll}/passes")
    assert listed.status_code == 200 and [x["uid"] for x in listed.json()] == [p["uid"]]
    edited = b_client.patch(f"/api/passes/{p['uid']}", json={"label": "B's name"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["label"] == "B's name" and edited.json()["version"] == 2
    issued_by_b = _issue(b_client, coll)
    assert issued_by_b.status_code == 201
    assert issued_by_b.json()["pass"]["createdBy"] == b
    revoked = b_client.delete(f"/api/passes/{p['uid']}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "revoked"
    assert p["createdBy"] != b  # control: B really was not the issuer


def test_every_route_refuses_an_anonymous_caller(lib):
    client, _ = lib
    coll = _trip(client)
    p = _issue(client, coll).json()["pass"]["uid"]
    anon = TestClient(app, raise_server_exceptions=False)
    calls = [
        ("post", f"/api/collections/{coll}/passes", {"label": "x"}),
        ("get", f"/api/collections/{coll}/passes", None),
        ("get", f"/api/passes/{p}", None),
        ("patch", f"/api/passes/{p}", {"label": "x"}),
        ("delete", f"/api/passes/{p}", None),
        ("post", f"/api/passes/{p}/unsuspend", None),
        ("get", f"/api/passes/{p}/devices", None),
        ("delete", f"/api/passes/{p}/devices/dev1", None),
        ("get", f"/api/passes/{p}/contributions", None),
        ("post", f"/api/passes/{p}/contributions/hide", {"attachmentUids": ["a"]}),
        ("post", f"/api/passes/{p}/contributions/unhide", {"attachmentUids": ["a"]}),
        ("post", f"/api/passes/{p}/contributions/remove", {"attachmentUids": ["a"]}),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        r = anon.request(method, path, **kwargs)
        assert r.status_code == 401, (method, path, r.status_code)
    # Control: the same calls with the session get past auth.
    assert client.get(f"/api/passes/{p}").status_code == 200


# --- edit and revoke --------------------------------------------------------------------------


def test_every_change_raises_the_version(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    v = [
        client.patch(f"/api/passes/{p}", json={"label": f"l{i}"}).json()["version"] for i in (1, 2)
    ]
    assert v == [2, 3]
    assert client.patch(f"/api/passes/{p}", json={"maxDevices": 5}).json()["version"] == 4
    assert client.patch(f"/api/passes/{p}", json={"scope": "contribute_view"}).json()["scope"] == (
        "contribute_view"
    )


def test_an_empty_patch_changes_nothing_and_keeps_the_version(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    r = client.patch(f"/api/passes/{p}", json={})
    assert r.status_code == 200 and r.json()["version"] == 1


def test_revoke_is_one_terminal_transition_and_reads_as_410(lib):
    client, _ = lib
    coll = _trip(client)
    p = _issue(client, coll).json()["pass"]["uid"]
    client.patch(f"/api/passes/{p}", json={"label": "x"})
    r = client.delete(f"/api/passes/{p}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "revoked" and body["version"] == 3 and body["revokedVersion"] == 3
    assert body["revokedAt"] is not None
    for method, kwargs in (("get", {}), ("patch", {"json": {"label": "back"}}), ("delete", {})):
        again = client.request(method, f"/api/passes/{p}", **kwargs)
        assert again.status_code == 410, (method, again.text)
        assert again.json()["detail"] == "pass_revoked"
    (row,) = _sql("SELECT label, version, revoked_version FROM passes WHERE uid=:u", {"u": p})
    assert tuple(row) == ("x", 3, 3)  # the 410s changed nothing
    listed = client.get(f"/api/collections/{coll}/passes").json()
    assert [(x["uid"], x["state"]) for x in listed] == [(p, "revoked")]


def test_revoke_marks_an_immediate_push_pending(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    _sql(
        "UPDATE pass_sync_state SET push_requested_at = NULL "
        "WHERE pass_id = (SELECT id FROM passes WHERE uid = :u)",
        {"u": p},
    )
    assert [tuple(r) for r in _sync_row(p)] == [(False, False, 0)]  # control
    client.delete(f"/api/passes/{p}")
    assert [tuple(r) for r in _sync_row(p)] == [(True, True, 0)]
    kinds = _sql(
        "SELECT kind, version FROM pass_events WHERE pass_id=(SELECT id FROM passes WHERE uid=:u) "
        "ORDER BY id",
        {"u": p},
    )
    assert [tuple(k) for k in kinds] == [("issued", 1), ("revoked", 2)]


def test_unsuspend_raises_the_version_records_the_lift_and_owes_an_immediate_push(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    _sql(
        "UPDATE pass_sync_state SET push_requested_at = NULL "
        "WHERE pass_id = (SELECT id FROM passes WHERE uid = :u)",
        {"u": p},
    )
    assert [tuple(r) for r in _sync_row(p)] == [(False, False, 0)]  # control
    r = client.post(f"/api/passes/{p}/unsuspend")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["state"] == "active"
    assert [tuple(r) for r in _sync_row(p)] == [(True, True, 0)]
    events = _sql(
        "SELECT kind, version, detail FROM pass_events "
        "WHERE pass_id=(SELECT id FROM passes WHERE uid=:u) ORDER BY id",
        {"u": p},
    )
    assert [tuple(e) for e in events] == [("issued", 1, None), ("updated", 2, "unsuspend")]


def test_unsuspend_of_a_revoked_pass_is_410_and_changes_nothing(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    client.delete(f"/api/passes/{p}")
    assert client.post(f"/api/passes/{p}/unsuspend").status_code == 410
    assert _sql("SELECT version FROM passes WHERE uid = :u", {"u": p}) == [(2,)]


def test_revoke_cascades_to_every_device(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    _device(p, "devA")
    _device(p, "devB", "Ben")
    client.delete(f"/api/passes/{p}")
    devices = client.get(f"/api/passes/{p}/devices").json()
    assert sorted((d["uid"], d["revoked"]) for d in devices) == [("devA", True), ("devB", True)]


def test_an_unknown_pass_is_404(lib):
    client, _ = lib
    assert client.get("/api/passes/nope").status_code == 404
    assert client.patch("/api/passes/nope", json={"label": "x"}).status_code == 404
    assert client.delete("/api/passes/nope").status_code == 404
    assert client.post("/api/passes/nope/unsuspend").status_code == 404
    assert client.get("/api/passes/nope/devices").status_code == 404
    assert client.get("/api/passes/nope/contributions").status_code == 404


async def _wait_until_blocked_by(holder_pid: int, ceiling: float = 10) -> None:
    """Until some backend is waiting on a lock THIS holder has — Postgres's own
    `pg_blocking_pids`, so an unrelated waiter elsewhere in the database cannot answer for it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ceiling
    while loop.time() < deadline:
        blocked = _sql(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND :h = ANY(pg_blocking_pids(pid))",
            {"h": holder_pid},
        )[0][0]
        if blocked:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("nothing ever waited on the holder's lock — this test proves nothing")


async def test_a_revoke_waits_on_the_row_lock_and_then_refuses_the_committed_revoke(
    lib, pat_client
):
    """Another transaction holds the pass row and revokes it. The request must WAIT on the row
    (Postgres's own `pg_stat_activity` proves the wait, not a sleep) and, once that commits, read
    the revoked row and answer 410. Without the row lock it reads the pre-commit row, waits only at
    its own UPDATE and then overwrites the committed revoke with a second one — a 200."""
    from sqlalchemy import text

    bearer, _, _ = pat_client
    coll = (await bearer.post("/api/collections", json={"title": "R", "tag": TAG})).json()["uid"]
    uid = (await bearer.post(f"/api/collections/{coll}/passes", json={"label": "r"})).json()[
        "pass"
    ]["uid"]
    async with async_session() as holder:
        await holder.execute(text("SELECT id FROM passes WHERE uid = :u FOR UPDATE"), {"u": uid})
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        request = asyncio.ensure_future(bearer.delete(f"/api/passes/{uid}"))
        await _wait_until_blocked_by(holder_pid)
        assert not request.done()
        await holder.execute(
            text(
                "UPDATE passes SET version = 2, revoked_version = 2, revoked_at = now() "
                "WHERE uid = :u"
            ),
            {"u": uid},
        )
        await holder.commit()
    r = await asyncio.wait_for(request, 10)
    assert r.status_code == 410, r.text  # 200 = it overwrote the committed revoke
    assert r.json()["detail"] == "pass_revoked"
    (row,) = _sql("SELECT version, revoked_version FROM passes WHERE uid=:u", {"u": uid})
    assert tuple(row) == (2, 2)
    assert _sql("SELECT count(*) FROM pass_events WHERE kind='revoked'")[0][0] == 0


async def test_a_patch_racing_a_revoke_cannot_resurrect_the_pass(lib, pat_client, monkeypatch):
    from app.services import passes as pass_svc

    bearer, _, _ = pat_client
    coll = (await bearer.post("/api/collections", json={"title": "R", "tag": TAG})).json()["uid"]
    uid = (await bearer.post(f"/api/collections/{coll}/passes", json={"label": "r"})).json()[
        "pass"
    ]["uid"]
    both = asyncio.Barrier(2)

    async def held():
        async with asyncio.timeout(10):
            await both.wait()

    monkeypatch.setattr(pass_svc, "_before_pass_lock", held)
    results = await asyncio.gather(
        bearer.delete(f"/api/passes/{uid}"),
        bearer.patch(f"/api/passes/{uid}", json={"label": "late"}),
    )
    codes = [r.status_code for r in results]
    (row,) = _sql("SELECT revoked_at IS NOT NULL, label FROM passes WHERE uid=:u", {"u": uid})
    assert row[0] is True  # whatever the order, the pass ends revoked
    if codes[1] == 410:
        assert row[1] == "r"  # the edit came second and changed nothing
    else:
        assert codes == [200, 200] and row[1] == "late"  # the edit came first; revoke still won


async def test_two_issues_racing_on_an_untagged_trip_both_succeed_with_one_tag(
    lib, pat_client, monkeypatch
):
    from app.services import passes as pass_svc

    bearer, _, _ = pat_client
    made = await bearer.post(
        "/api/collections",
        json={"title": "Cyprus 2026", "kind": "trip", "spanStart": START, "spanEnd": END},
    )
    coll = made.json()["uid"]
    both = asyncio.Barrier(2)

    async def held():
        async with asyncio.timeout(10):
            await both.wait()

    monkeypatch.setattr(pass_svc, "_before_pass_lock", held)
    results = await asyncio.gather(
        bearer.post(f"/api/collections/{coll}/passes", json={"label": "a"}),
        bearer.post(f"/api/collections/{coll}/passes", json={"label": "b"}),
    )
    assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
    assert _stored_tag(coll) == "cyprus2026"
    assert _sql("SELECT count(*) FROM passes")[0][0] == 2


async def test_an_issue_waits_on_the_trip_row_and_keeps_a_tag_committed_meanwhile(lib, pat_client):
    """Another transaction holds the trip row and gives it a tag. Issuance must WAIT on the row and
    then read the committed tag — never write its derived one over it. Without the lock it read the
    tag as NULL before the holder committed, waits only at its own UPDATE, and overwrites."""
    from sqlalchemy import text

    bearer, _, _ = pat_client
    made = await bearer.post(
        "/api/collections",
        json={"title": "Cyprus 2026", "kind": "trip", "spanStart": START, "spanEnd": END},
    )
    coll = made.json()["uid"]
    async with async_session() as holder:
        await holder.execute(
            text("SELECT id FROM collections WHERE uid = :u FOR UPDATE"), {"u": coll}
        )
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        request = asyncio.ensure_future(
            bearer.post(f"/api/collections/{coll}/passes", json={"label": "r"})
        )
        await _wait_until_blocked_by(holder_pid)
        assert not request.done()
        await holder.execute(
            text("UPDATE collections SET tag = 'chosen_by_hand' WHERE uid = :u"), {"u": coll}
        )
        await holder.commit()
    r = await asyncio.wait_for(request, 10)
    assert r.status_code == 201, r.text
    assert _stored_tag(coll) == "chosen_by_hand"  # "cyprus2026" = it overwrote the committed tag


def test_deleting_the_collection_revokes_its_live_passes_as_one_versioned_transition(lib):
    """The FK only clears `collection_id`; the delete must also run the revoke — the version that
    performs it, every device with it, an URGENT push — or the edge never learns the pass died.
    The contributions stay: the memos and pictures are the household's, not the collection's."""
    client, me = lib
    coll = _trip(client)
    live = _issue(client, coll).json()["pass"]["uid"]
    gone = _issue(client, coll).json()["pass"]["uid"]
    assert client.delete(f"/api/passes/{gone}").status_code == 200  # revoked at 2 already
    other = _issue(client, _trip(client, tag=OTHER)).json()["pass"]["uid"]
    _device(live, "devA")
    _device(live, "devB")
    _contribution(live, me, "gc1", "devA")
    _sql(
        "UPDATE pass_sync_state SET push_requested_at = NULL "
        "WHERE pass_id = (SELECT id FROM passes WHERE uid = :u)",
        {"u": live},
    )
    assert [tuple(r) for r in _sync_row(live)] == [(False, False, 0)]  # control
    assert client.delete(f"/api/collections/{coll}").status_code == 204
    (row,) = _sql(
        "SELECT collection_id IS NULL, version, revoked_version, revoked_at IS NOT NULL, "
        "revoked_by = (SELECT id FROM users WHERE subject = :me) FROM passes WHERE uid = :u",
        {"u": live, "me": me},
    )
    assert tuple(row) == (True, 2, 2, True, True)
    devices = _sql(
        "SELECT count(*) FILTER (WHERE revoked_version = 2 AND revoked_at IS NOT NULL), count(*) "
        "FROM pass_devices WHERE pass_id = (SELECT id FROM passes WHERE uid = :u)",
        {"u": live},
    )
    assert tuple(devices[0]) == (2, 2)
    assert [tuple(r) for r in _sync_row(live)] == [(True, True, 0)]
    events = _sql(
        "SELECT kind, version, detail FROM pass_events "
        "WHERE pass_id = (SELECT id FROM passes WHERE uid = :u) ORDER BY id",
        {"u": live},
    )
    assert [tuple(e) for e in events] == [("issued", 1, None), ("revoked", 2, "collection_deleted")]
    (g,) = _sql(
        "SELECT version, revoked_version, (SELECT count(*) FROM pass_events e "
        "WHERE e.pass_id = p.id AND e.kind = 'revoked') FROM passes p WHERE uid = :u",
        {"u": gone},
    )
    assert tuple(g) == (2, 2, 1)  # an already-revoked pass is not revoked twice
    (o,) = _sql("SELECT revoked_at IS NULL, version FROM passes WHERE uid = :u", {"u": other})
    assert tuple(o) == (True, 1)  # another collection's pass is untouched
    kept = _sql(
        "SELECT a.uid, m.pass_id IS NOT NULL FROM attachments a JOIN memos m ON m.id = a.memo_id "
        "WHERE a.uid = 'gc1'"
    )
    assert [tuple(k) for k in kept] == [("gc1", True)]
    assert client.get(f"/api/passes/{live}").status_code == 410


@pytest.mark.parametrize(
    "base",
    [
        "",
        "http://trip.example",
        "trip.example",
        "https://",
        "https://trip.example/p",
        "https://trip.example?x=1",
        "https://user@trip.example",
        "https://:secret@trip.example",
        "https://trip.example:abc",
        "https://trip.example:99999",
        "https://[broken",
    ],
)
def test_issue_refuses_a_base_url_that_is_not_an_https_origin(lib, monkeypatch, base):
    from app.config import settings

    client, _ = lib
    coll = _trip(client)
    monkeypatch.setattr(settings, "TRIP_PASS_BASE_URL", base)
    r = _issue(client, coll)
    assert r.status_code == 503, r.text
    assert "TRIP_PASS_BASE_URL" in r.json()["detail"]
    assert _sql("SELECT count(*) FROM passes")[0][0] == 0


@pytest.mark.parametrize("base", ["https://trip.example", "https://trip.example/"])
def test_issue_names_the_configured_origin(lib, monkeypatch, base):
    from app.config import settings

    client, _ = lib
    monkeypatch.setattr(settings, "TRIP_PASS_BASE_URL", base)
    r = _issue(client, _trip(client))
    assert r.status_code == 201, r.text
    assert re.fullmatch(r"https://trip\.example/p/[A-Za-z0-9_-]{43}", r.json()["url"])


# --- devices ----------------------------------------------------------------------------------


def test_revoking_one_device_leaves_the_others_and_raises_the_version(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    _device(p, "devA")
    _device(p, "devB", "Ben")
    r = client.delete(f"/api/passes/{p}/devices/devA")
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] is True and r.json()["revokedVersion"] == 2
    devices = {d["uid"]: d for d in client.get(f"/api/passes/{p}/devices").json()}
    assert devices["devA"]["revoked"] is True and devices["devB"]["revoked"] is False
    assert devices["devA"]["displayName"] == "Anna"
    assert client.get(f"/api/passes/{p}").json()["version"] == 2
    assert [tuple(r) for r in _sync_row(p)] == [(True, True, 0)]
    again = client.delete(f"/api/passes/{p}/devices/devA")
    assert again.status_code == 410 and again.json()["detail"] == "device_revoked"
    assert client.delete(f"/api/passes/{p}/devices/nope").status_code == 404


def test_a_device_of_another_pass_is_404(lib):
    client, _ = lib
    coll = _trip(client)
    p1 = _issue(client, coll).json()["pass"]["uid"]
    p2 = _issue(client, coll).json()["pass"]["uid"]
    _device(p2, "devX")
    assert client.delete(f"/api/passes/{p1}/devices/devX").status_code == 404
    devices = client.get(f"/api/passes/{p2}/devices").json()
    assert devices[0]["revoked"] is False


def test_revoking_a_device_of_a_revoked_pass_is_410(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    _device(p, "devA")
    client.delete(f"/api/passes/{p}")
    r = client.delete(f"/api/passes/{p}/devices/devA")
    assert r.status_code == 410 and r.json()["detail"] == "pass_revoked"


# --- contributions ----------------------------------------------------------------------------


def _contribution(pass_uid: str, owner: str, att_uid: str, device_uid: str) -> int:
    memo_id = _memo(f"m-{att_uid}", owner, visibility="PROTECTED", tags=(TAG,))
    _sql(
        "UPDATE memos SET pass_id=(SELECT id FROM passes WHERE uid=:p), guest_caption='hi' "
        "WHERE id=:m",
        {"p": pass_uid, "m": memo_id},
    )
    att = _att(att_uid, owner, memo_id)
    _sql(
        "UPDATE attachments SET contributor_name='Anna', contributed_by_device_id="
        "(SELECT id FROM pass_devices WHERE uid=:d) WHERE id=:a",
        {"d": device_uid, "a": att},
    )
    return att


def test_contributions_list_hide_unhide_on_a_trip(lib):
    client, me = lib
    coll = _trip(client)
    p = _issue(client, coll).json()["pass"]["uid"]
    _device(p, "devA")
    _contribution(p, me, "ga1", "devA")
    _att("own1", me, _memo("own-m", me, tags=(TAG,)))  # a member's own picture: not listed
    listed = client.get(f"/api/passes/{p}/contributions")
    assert listed.status_code == 200, listed.text
    items = listed.json()
    assert [
        (i["attachmentUid"], i["contributorName"], i["deviceUid"], i["hidden"]) for i in items
    ] == [("ga1", "Anna", "devA", False)]
    assert items[0]["caption"] == "hi"
    r = client.post(f"/api/passes/{p}/contributions/hide", json={"attachmentUids": ["ga1", "own1"]})
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [
        {"attachmentUid": "ga1", "outcome": "hidden"},
        {"attachmentUid": "own1", "outcome": "notContribution"},
    ]
    assert client.get(f"/api/passes/{p}/contributions").json()[0]["hidden"] is True
    members = [m["uid"] for m in client.get(f"/api/collections/{coll}/attachments").json()]
    assert "ga1" not in members and "own1" in members
    r = client.post(f"/api/passes/{p}/contributions/unhide", json={"attachmentUids": ["ga1"]})
    assert r.json()["results"] == [{"attachmentUid": "ga1", "outcome": "unhidden"}]
    members = [m["uid"] for m in client.get(f"/api/collections/{coll}/attachments").json()]
    assert "ga1" in members


def test_contributions_hide_unhide_on_an_album(lib):
    client, me = lib
    coll = _album(client)
    p = _issue(client, coll).json()["pass"]["uid"]
    _device(p, "devA")
    att = _contribution(p, me, "ga2", "devA")
    _sql(
        "INSERT INTO collection_members (collection_id, attachment_id) VALUES "
        "((SELECT id FROM collections WHERE uid=:c), :a)",
        {"c": coll, "a": att},
    )
    assert client.get(f"/api/passes/{p}/contributions").json()[0]["hidden"] is False
    r = client.post(f"/api/passes/{p}/contributions/hide", json={"attachmentUids": ["ga2"]})
    assert r.json()["results"] == [{"attachmentUid": "ga2", "outcome": "hidden"}]
    assert client.get(f"/api/passes/{p}/contributions").json()[0]["hidden"] is True
    r = client.post(f"/api/passes/{p}/contributions/unhide", json={"attachmentUids": ["ga2"]})
    assert r.json()["results"] == [{"attachmentUid": "ga2", "outcome": "unhidden"}]
    assert client.get(f"/api/passes/{p}/contributions").json()[0]["hidden"] is False


def test_contributions_remove_deletes_the_picture_and_stays_moderatable_after_revoke(two):
    a_client, a, b_client, _ = two
    p = _issue(a_client, _trip(a_client)).json()["pass"]["uid"]
    _device(p, "devA")
    _contribution(p, a, "ga3", "devA")
    a_client.delete(f"/api/passes/{p}")
    # B — not the pass's issuer and not the picture's owner — moderates after the revoke.
    r = b_client.post(f"/api/passes/{p}/contributions/remove", json={"attachmentUids": ["ga3"]})
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [{"attachmentUid": "ga3", "outcome": "removed"}]
    assert _sql("SELECT count(*) FROM attachments WHERE uid='ga3'")[0][0] == 0
    assert b_client.get(f"/api/passes/{p}/contributions").json() == []


def test_a_contribution_of_another_pass_is_not_this_pass_s_to_moderate(lib):
    """Scope: two passes on ONE trip. A picture that came in through the second is not the
    first's, so the first's verbs answer `notContribution` and leave it where it is."""
    client, me = lib
    coll = _trip(client)
    p1 = _issue(client, coll).json()["pass"]["uid"]
    p2 = _issue(client, coll).json()["pass"]["uid"]
    _device(p2, "devB")
    _contribution(p2, me, "gb1", "devB")
    for verb in ("hide", "remove"):
        r = client.post(f"/api/passes/{p1}/contributions/{verb}", json={"attachmentUids": ["gb1"]})
        assert r.json()["results"] == [{"attachmentUid": "gb1", "outcome": "notContribution"}]
    assert client.get(f"/api/passes/{p1}/contributions").json() == []
    assert _sql("SELECT count(*) FROM collection_exclusions")[0][0] == 0
    assert _sql("SELECT count(*) FROM attachments WHERE uid='gb1'")[0][0] == 1
    assert [c["attachmentUid"] for c in client.get(f"/api/passes/{p2}/contributions").json()] == [
        "gb1"
    ]


def test_a_remove_whose_files_could_not_go_answers_error_not_removed(lib, monkeypatch):
    """reclaim KEEPS a row whose files it could not unlink; answering `removed` would tell the
    owner the picture is gone while it is all still there."""
    from app.services import reclaim

    client, me = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    _device(p, "devA")
    _contribution(p, me, "ge1", "devA")

    async def kept(attachment_id, mode, **_):
        return reclaim.ReclaimResult(outcome="failed_unlink", failed_paths=("/x",))

    monkeypatch.setattr(reclaim, "reclaim_row_shielded", kept)
    r = client.post(f"/api/passes/{p}/contributions/remove", json={"attachmentUids": ["ge1"]})
    assert r.json()["results"] == [{"attachmentUid": "ge1", "outcome": "error"}]
    assert _sql("SELECT count(*) FROM pass_events WHERE kind='removed'")[0][0] == 0


def test_contribution_bodies_are_bounded(lib):
    client, _ = lib
    p = _issue(client, _trip(client)).json()["pass"]["uid"]
    for verb in ("hide", "unhide", "remove"):
        assert (
            client.post(
                f"/api/passes/{p}/contributions/{verb}", json={"attachmentUids": []}
            ).status_code
            == 422
        )
        too_many = {"attachmentUids": [f"a{i}" for i in range(501)]}
        assert (
            client.post(f"/api/passes/{p}/contributions/{verb}", json=too_many).status_code == 422
        )


# --- guest text never reaches tag extraction ---------------------------------------------------


async def _guest_memo(coll_uid: str, subject: str, *, caption: str | None, name: str | None):
    from app.services import passes as pass_svc

    async with async_session() as db:
        owner = (await db.execute(select(User).where(User.subject == subject))).scalar_one()
        collection = (
            await db.execute(select(Collection).where(Collection.uid == coll_uid))
        ).scalar_one()
        pass_row, _ = await pass_svc.issue_pass(db, collection, owner, label="g")
        memo = await pass_svc.create_guest_memo(
            db, pass_row, caption=caption, contributor_name=name, idempotency_key="trip:b1"
        )
        await db.commit()
        return memo.uid


@pytest.mark.parametrize(
    ("caption", "name"),
    [
        ("look #other_trip", None),
        (None, "#other_trip"),
        ("#other_trip #tp1trip_x", "Anna #other_trip"),
        ("", ""),
    ],
    ids=["caption", "name", "both", "empty"],
)
async def test_a_guest_name_or_caption_with_a_hashtag_creates_no_tag(lib, caption, name):
    client, me = lib
    coll = _trip(client)
    memo_uid = await _guest_memo(coll, me, caption=caption, name=name)
    (content, vis, gcap) = _sql(
        "SELECT content, visibility::text, guest_caption FROM memos WHERE uid=:u", {"u": memo_uid}
    )[0]
    assert content == f"#{TAG}"
    assert vis == "PROTECTED"
    assert gcap == (caption or None)
    tags = _sql(
        "SELECT t.name FROM tags t JOIN memo_tags mt ON mt.tag_id=t.id "
        "JOIN memos m ON m.id=mt.memo_id WHERE m.uid=:u",
        {"u": memo_uid},
    )
    assert [t[0] for t in tags] == [TAG]
    assert _sql("SELECT count(*) FROM tags WHERE name LIKE 'other%'")[0][0] == 0


async def test_a_guest_memo_for_a_trip_that_lost_its_tag_is_refused(lib):
    from app.services import passes as pass_svc

    client, me = lib
    coll = _trip(client, span=True)
    async with async_session() as db:
        owner = (await db.execute(select(User).where(User.subject == me))).scalar_one()
        collection = (
            await db.execute(select(Collection).where(Collection.uid == coll))
        ).scalar_one()
        pass_row, _ = await pass_svc.issue_pass(db, collection, owner, label="g")
        await db.commit()
    client.patch(f"/api/collections/{coll}", json={"tag": None})
    async with async_session() as db:
        pass_row = await pass_svc.get_pass(db, pass_row.uid)
        with pytest.raises(pass_svc.TripHasNoTag):
            await pass_svc.create_guest_memo(
                db, pass_row, caption="#x", contributor_name=None, idempotency_key="trip:b2"
            )
    assert _sql("SELECT count(*) FROM memos")[0][0] == 0


def test_guest_memo_content_is_exactly_the_trip_tag():
    from app.services import passes as pass_svc
    from app.services.memos import parse_tags

    trip = Collection(kind="trip", tag="tp1trip", title="t", uid="u")
    assert pass_svc.guest_memo_content(trip) == "#tp1trip"
    assert parse_tags(pass_svc.guest_memo_content(trip)) == ["tp1trip"]
    for bad in (None, "has-dash", "two words", ""):
        with pytest.raises(pass_svc.TripHasNoTag):
            pass_svc.guest_memo_content(Collection(kind="trip", tag=bad, title="t", uid="u"))
    assert pass_svc.guest_memo_content(Collection(kind="album", tag=None, title="a", uid="u")) == ""


# --- the published contract -------------------------------------------------------------------

PASS_OPERATIONS = {
    ("post", "/api/collections/{uid}/passes"),
    ("get", "/api/collections/{uid}/passes"),
    ("get", "/api/passes/{uid}"),
    ("patch", "/api/passes/{uid}"),
    ("delete", "/api/passes/{uid}"),
    ("post", "/api/passes/{uid}/unsuspend"),
    ("get", "/api/passes/{uid}/devices"),
    ("delete", "/api/passes/{uid}/devices/{device_uid}"),
    ("get", "/api/passes/{uid}/contributions"),
    ("post", "/api/passes/{uid}/contributions/hide"),
    ("post", "/api/passes/{uid}/contributions/unhide"),
    ("post", "/api/passes/{uid}/contributions/remove"),
}


def test_openapi_names_exactly_the_pass_operations_all_member_authenticated():
    paths = app.openapi()["paths"]
    found = {
        (m, p)
        for p, item in paths.items()
        if p.startswith("/api/passes") or p.endswith("/passes")
        for m in item
    }
    assert found == PASS_OPERATIONS
    for method, path in PASS_OPERATIONS:
        op = paths[path][method]
        assert op["security"], (method, path)
        assert "401" in op["responses"] and "404" in op["responses"], (method, path)
    assert "409" in paths["/api/collections/{uid}/passes"]["post"]["responses"]
    for method, path in (
        ("get", "/api/passes/{uid}"),
        ("patch", "/api/passes/{uid}"),
        ("delete", "/api/passes/{uid}"),
        ("post", "/api/passes/{uid}/unsuspend"),
        ("delete", "/api/passes/{uid}/devices/{device_uid}"),
    ):
        assert "410" in paths[path][method]["responses"], (method, path)


def test_no_guest_route_exists_on_the_home_role():
    paths = app.openapi()["paths"]
    assert not [p for p in paths if p.startswith(("/api/guest", "/api/pass/", "/ingest", "/p/"))]
    assert not [
        r
        for r in app.routes
        if re.match(r"^/(api/guest|api/pass/|ingest|p/)", getattr(r, "path", ""))
    ]
