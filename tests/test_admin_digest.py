"""The admin-only digest API (MEMNG-233).

Two things are proved for every route: what it does for an admin, and that nobody else reaches it.
The second is a parametrised sweep over the whole route table rather than a case per handler, so a
route added to this router later is covered the moment it exists — an authorization check that has
to be remembered per endpoint is the class of hole LSN-0020 is about.
"""

from datetime import UTC, date, datetime

import pytest
from app.config import settings as app_settings
from app.main import app
from app.models import DigestTrigger
from app.routers.admin_digest import normalise_email
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

PREFIX = "/api/admin/digest"

# (method, path) for every route this router publishes. Read off the app rather than typed out, so
# the sweeps below cannot silently stop covering a route.
ADMIN_ROUTES = sorted(
    (method, route.path)
    for route in app.routes
    if isinstance(route, APIRoute) and route.path.startswith(PREFIX)
    for method in sorted(route.methods or ())
)


def _url(path: str) -> str:
    """A concrete URL for a route template — `{recipient_id}` becomes an id nothing owns.

    The sweeps assert a REFUSAL, and the refusal has to happen before the handler ever looks the
    id up; an id that does not exist proves exactly that (a 404 here would mean the credential
    check was skipped).
    """
    return path.replace("{recipient_id}", "999999")


@pytest.fixture(autouse=True)
def digest_state(db_engine):
    """Reset the digest tables around every test in this module.

    These tests drive the real API, which COMMITS — so without this a recipient added by one test
    would still be there for the next one's "the last enabled recipient" arithmetic. The settings
    row is re-seeded the way the migration seeds it (naming only `id`), so each test starts from
    the documented defaults rather than from whatever the previous test saved.
    """

    def reset() -> None:
        engine = create_engine(SYNC_URL)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM digest_deliveries"))
            conn.execute(text("DELETE FROM digest_runs"))
            conn.execute(text("DELETE FROM digest_recipients"))
            conn.execute(text("DELETE FROM digest_settings"))
            conn.execute(text("INSERT INTO digest_settings (id) VALUES (1)"))
        engine.dispose()

    reset()
    yield
    reset()


def _settings_payload(**overrides) -> dict:
    payload = {
        "enabled": False,
        "send_time": "07:00:00",
        "timezone": "Europe/Zurich",
        "visibility_scope": "PUBLIC_PROTECTED",
        "start_date": "2026-06-01",
        "grace_minutes": 30,
        "max_email_bytes": 18874368,
        "max_videos_per_memo": 2,
        "max_memos_per_run": 40,
        "mail_title": "The Daily Dami Memos Mail",
        "from_name": "The Daily Dami Memos Mail",
        "from_email": "",
        "hosting_enabled": False,
        "dropbox_folder": "/memos-mail",
        "retention_days": 30,
        "link_images_full": False,
    }
    payload.update(overrides)
    return payload


def _arm_digest(enabled: bool = True) -> None:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(text("UPDATE digest_settings SET enabled = :e"), {"e": enabled})
    engine.dispose()


# --- the address rule, as a pure function -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a@b.co", "a@b.co"),
        ("  Fabian.Maass@Example.COM ", "fabian.maass@example.com"),
        ("first+tag@sub.domain.example", "first+tag@sub.domain.example"),
        ("o'brien@example.com", "o'brien@example.com"),
        ("", None),
        ("   ", None),
        ("nobody", None),
        ("no@domain", None),
        ("@example.com", None),
        ("two@@example.com", None),
        ("space in@example.com", None),
        ("trailing@example.com.", None),
        ("a@-example.com", None),
        ("a@example.c", None),
        ("a" * 320 + "@example.com", None),
    ],
)
def test_normalise_email_decides_shape_and_stored_form_together(raw, expected):
    assert normalise_email(raw) == expected


# --- nobody but an admin gets in --------------------------------------------------------------


def test_the_sweep_below_covers_every_route_this_router_publishes():
    """Positive control: an empty parametrisation passes for the wrong reason."""
    assert len(ADMIN_ROUTES) == 8, ADMIN_ROUTES
    assert (
        sorted({path for _, path in ADMIN_ROUTES})
        == [
            f"{PREFIX}/hosting",
            f"{PREFIX}/recipients",
            f"{PREFIX}/recipients/{{recipient_id}}",
            f"{PREFIX}/runs",
            f"{PREFIX}/settings",
        ]
    )


@pytest.mark.parametrize(
    ("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES]
)
def test_a_non_admin_session_is_refused_with_403(logged_in_client, method, path):
    client, _ = logged_in_client
    response = client.request(method, _url(path), json={})
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Administrator role required"


@pytest.mark.parametrize(
    ("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES]
)
def test_an_anonymous_request_is_refused(client, method, path):
    response = client.request(method, _url(path), json={})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    ("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES]
)
async def test_an_access_token_never_reaches_an_admin_route(admin_client, method, path):
    """The token belongs to the ADMIN account itself, so the only thing that can refuse it is the
    rule that a bearer is refused before the cookie is read — not the role check. It is answered
    401 (no session), never 403 (wrong role), and never served.

    A separate client with an EMPTY cookie jar: reusing the admin's own client with an extra header
    would leave the session cookie in the jar and the assertion would pass with the bearer branch
    deleted.
    """
    from app import auth

    client, _ = admin_client
    created = client.post("/api/pats", json={"name": "digest-device"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}", "Origin": "http://testserver"},
    ) as bearer:
        try:
            response = await bearer.request(method, _url(path), json={})
        finally:
            await auth.drain_pat_stamp_tasks()
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Session authentication required"


# --- settings ---------------------------------------------------------------------------------


def test_an_admin_reads_the_seeded_settings(admin_client):
    client, _ = admin_client
    response = client.get(f"{PREFIX}/settings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["timezone"] == "Europe/Zurich"
    assert body["visibility_scope"] == "PUBLIC_PROTECTED"
    assert body["start_date"] == "2026-06-01"
    assert body["send_time"] == "07:00:00"
    assert body["max_memos_per_run"] == 40
    assert body["from_email"] == ""
    assert body["updated_by"] is None
    assert "max_video_attach_bytes" not in body


def test_a_put_updates_the_row_and_stamps_who_did_it(admin_client):
    client, subject = admin_client
    response = client.put(
        f"{PREFIX}/settings",
        json=_settings_payload(
            enabled=True, retention_days=14, mail_title="Household Digest", grace_minutes=45
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["retention_days"] == 14
    assert response.json()["mail_title"] == "Household Digest"
    assert response.json()["updated_by"] == subject

    reread = client.get(f"{PREFIX}/settings").json()
    assert reread["enabled"] is True
    assert reread["grace_minutes"] == 45


def test_a_put_never_creates_a_second_settings_row(admin_client):
    client, _ = admin_client
    for title in ("one", "two", "three"):
        assert (
            client.put(f"{PREFIX}/settings", json=_settings_payload(mail_title=title)).status_code
            == 200
        )
    engine = create_engine(SYNC_URL)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, mail_title FROM digest_settings")).all()
    engine.dispose()
    assert rows == [(1, "three")]


@pytest.mark.parametrize(
    "override",
    [
        {"retention_days": 0},
        {"max_memos_per_run": 0},
        {"grace_minutes": -1},
        {"max_email_bytes": 10},
        {"mail_title": ""},
        {"visibility_scope": "PRIVATE"},
        {"send_time": "not a time"},
        {"start_date": "yesterday"},
        {"from_email": "not-an-address"},
        {"dropbox_folder": ""},
    ],
)
def test_an_implausible_settings_value_is_refused(admin_client, override):
    client, _ = admin_client
    assert client.put(f"{PREFIX}/settings", json=_settings_payload(**override)).status_code == 422


def test_a_settings_put_missing_a_field_is_refused(admin_client):
    """PUT is the WHOLE row — an omitted field is a client bug, not "leave it alone"."""
    client, _ = admin_client
    payload = _settings_payload()
    del payload["timezone"]
    assert client.put(f"{PREFIX}/settings", json=payload).status_code == 422


def test_a_from_email_is_stored_normalised_and_may_be_emptied(admin_client):
    client, _ = admin_client
    saved = client.put(
        f"{PREFIX}/settings", json=_settings_payload(from_email="  Digest@Example.COM ")
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["from_email"] == "digest@example.com"
    cleared = client.put(f"{PREFIX}/settings", json=_settings_payload(from_email=""))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["from_email"] == ""


def test_arming_the_digest_with_no_recipients_is_allowed_in_phase_1(admin_client):
    """Explicit: the "enabled with zero enabled recipients" refusal is Phase 2's PUT guard. Phase 1
    must NOT reject it, or the panel could not be built against this contract."""
    client, _ = admin_client
    assert client.put(f"{PREFIX}/settings", json=_settings_payload(enabled=True)).status_code == 200


# --- recipients -------------------------------------------------------------------------------


def test_a_recipient_is_added_lower_cased_and_attributed(admin_client):
    client, subject = admin_client
    created = client.post(
        f"{PREFIX}/recipients", json={"email": "  Someone@Example.COM ", "display_name": "Someone"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email"] == "someone@example.com"
    assert body["display_name"] == "Someone"
    assert body["enabled"] is True
    assert body["added_by"] == subject

    listed = client.get(f"{PREFIX}/recipients")
    assert listed.status_code == 200, listed.text
    assert [r["email"] for r in listed.json()] == ["someone@example.com"]


def test_the_recipient_list_is_ordered_by_address(admin_client):
    client, _ = admin_client
    for email in ("zoe@example.com", "adam@example.com", "mia@example.com"):
        assert client.post(f"{PREFIX}/recipients", json={"email": email}).status_code == 201
    assert [r["email"] for r in client.get(f"{PREFIX}/recipients").json()] == [
        "adam@example.com",
        "mia@example.com",
        "zoe@example.com",
    ]


@pytest.mark.parametrize("malformed", ["nobody", "no@domain", "@example.com", "a b@example.com"])
def test_a_malformed_address_is_refused(admin_client, malformed):
    client, _ = admin_client
    response = client.post(f"{PREFIX}/recipients", json={"email": malformed})
    assert response.status_code == 422, response.text
    assert client.get(f"{PREFIX}/recipients").json() == []


def test_a_duplicate_address_is_refused_case_insensitively(admin_client):
    client, _ = admin_client
    first = client.post(f"{PREFIX}/recipients", json={"email": "dupe@example.com"})
    assert first.status_code == 201, first.text
    clash = client.post(f"{PREFIX}/recipients", json={"email": "DUPE@Example.com"})
    assert clash.status_code == 409, clash.text
    assert len(client.get(f"{PREFIX}/recipients").json()) == 1


def test_a_recipient_can_be_renamed_and_toggled(admin_client):
    client, _ = admin_client
    created = client.post(
        f"{PREFIX}/recipients", json={"email": "toggle@example.com", "display_name": "Old"}
    )
    recipient_id = created.json()["id"]

    renamed = client.patch(f"{PREFIX}/recipients/{recipient_id}", json={"display_name": "New"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "New"
    assert renamed.json()["enabled"] is True  # untouched by a name-only patch

    disabled = client.patch(f"{PREFIX}/recipients/{recipient_id}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["display_name"] == "New"  # untouched by an enabled-only patch


def test_a_display_name_is_cleared_by_an_explicit_null_and_kept_by_omission(admin_client):
    client, _ = admin_client
    recipient_id = client.post(
        f"{PREFIX}/recipients", json={"email": "named@example.com", "display_name": "Named"}
    ).json()["id"]
    kept = client.patch(f"{PREFIX}/recipients/{recipient_id}", json={"enabled": True})
    assert kept.json()["display_name"] == "Named"
    cleared = client.patch(f"{PREFIX}/recipients/{recipient_id}", json={"display_name": None})
    assert cleared.json()["display_name"] is None


def test_a_recipient_is_deleted(admin_client):
    client, _ = admin_client
    recipient_id = client.post(f"{PREFIX}/recipients", json={"email": "gone@example.com"}).json()[
        "id"
    ]
    assert client.delete(f"{PREFIX}/recipients/{recipient_id}").status_code == 204
    assert client.get(f"{PREFIX}/recipients").json() == []


def test_an_unknown_recipient_is_a_404(admin_client):
    client, _ = admin_client
    assert client.patch(f"{PREFIX}/recipients/999999", json={"enabled": False}).status_code == 404
    assert client.delete(f"{PREFIX}/recipients/999999").status_code == 404


def test_the_last_enabled_recipient_of_an_armed_digest_cannot_be_disabled(admin_client):
    """A2: the panel must not be able to leave a digest switched on with nobody to send to — that
    is the silent morning failure this whole feature exists to end."""
    client, _ = admin_client
    recipient_id = client.post(f"{PREFIX}/recipients", json={"email": "only@example.com"}).json()[
        "id"
    ]
    _arm_digest(True)

    refused = client.patch(f"{PREFIX}/recipients/{recipient_id}", json={"enabled": False})
    assert refused.status_code == 422, refused.text
    assert "last enabled recipient" in refused.json()["detail"]
    assert client.get(f"{PREFIX}/recipients").json()[0]["enabled"] is True


def test_the_last_enabled_recipient_of_an_armed_digest_cannot_be_deleted(admin_client):
    client, _ = admin_client
    recipient_id = client.post(f"{PREFIX}/recipients", json={"email": "only@example.com"}).json()[
        "id"
    ]
    _arm_digest(True)

    refused = client.delete(f"{PREFIX}/recipients/{recipient_id}")
    assert refused.status_code == 422, refused.text
    assert len(client.get(f"{PREFIX}/recipients").json()) == 1


def test_disabling_the_digest_first_is_the_supported_path(admin_client):
    client, _ = admin_client
    recipient_id = client.post(f"{PREFIX}/recipients", json={"email": "only@example.com"}).json()[
        "id"
    ]
    _arm_digest(True)
    assert client.delete(f"{PREFIX}/recipients/{recipient_id}").status_code == 422

    assert (
        client.put(f"{PREFIX}/settings", json=_settings_payload(enabled=False)).status_code == 200
    )
    assert client.delete(f"{PREFIX}/recipients/{recipient_id}").status_code == 204
    assert client.get(f"{PREFIX}/recipients").json() == []


def test_the_guard_only_bites_on_the_LAST_enabled_recipient(admin_client):
    """Control for the two refusals above: with a second enabled recipient the same call succeeds,
    so the 422 comes from the arithmetic and not from "the digest is on"."""
    client, _ = admin_client
    first = client.post(f"{PREFIX}/recipients", json={"email": "one@example.com"}).json()["id"]
    client.post(f"{PREFIX}/recipients", json={"email": "two@example.com"})
    _arm_digest(True)
    assert client.delete(f"{PREFIX}/recipients/{first}").status_code == 204


def test_an_already_disabled_recipient_can_be_removed_from_an_armed_digest(admin_client):
    """Removing a row that is already disabled cannot lower the enabled count, so the guard must
    not refuse it — the digest still has its one enabled recipient afterwards."""
    client, _ = admin_client
    keeper = client.post(f"{PREFIX}/recipients", json={"email": "keeper@example.com"}).json()["id"]
    spare = client.post(
        f"{PREFIX}/recipients", json={"email": "spare@example.com", "enabled": False}
    ).json()["id"]
    _arm_digest(True)

    assert client.delete(f"{PREFIX}/recipients/{spare}").status_code == 204
    assert [r["id"] for r in client.get(f"{PREFIX}/recipients").json()] == [keeper]


# --- runs -------------------------------------------------------------------------------------


def _insert_runs() -> None:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        for day, trigger, status in (
            (date(2026, 8, 28), DigestTrigger.scheduled, "sent"),
            (date(2026, 8, 29), DigestTrigger.scheduled, "nothing_to_send"),
            (date(2026, 8, 30), DigestTrigger.scheduled, "failed"),
        ):
            conn.execute(
                text(
                    "INSERT INTO digest_runs (trigger, started_at, finished_at, status, "
                    "memos_sent, parts, parts_sent, parts_claimed, recipients, local_date) "
                    "VALUES (:t, :s, :s, :st, 3, 1, 1, 1, 6, :d)"
                ),
                {
                    "t": trigger.value,
                    "s": datetime(day.year, day.month, day.day, 5, 0, tzinfo=UTC),
                    "st": status,
                    "d": day,
                },
            )
    engine.dispose()


def test_runs_come_back_newest_first_with_their_bookkeeping(admin_client):
    client, _ = admin_client
    _insert_runs()
    response = client.get(f"{PREFIX}/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["local_date"] for r in body] == ["2026-08-30", "2026-08-29", "2026-08-28"]
    assert body[0]["status"] == "failed"
    assert body[0]["trigger"] == "scheduled"
    assert body[0]["parts_claimed"] == 1
    assert body[0]["recipients"] == 6
    assert body[0]["started_at"] == "2026-08-30T05:00:00Z"


def test_the_runs_list_honours_its_limit(admin_client):
    client, _ = admin_client
    _insert_runs()
    assert len(client.get(f"{PREFIX}/runs?limit=2").json()) == 2
    assert client.get(f"{PREFIX}/runs?limit=0").status_code == 422
    assert client.get(f"{PREFIX}/runs?limit=201").status_code == 422


def test_an_in_flight_run_is_published_with_a_null_status(admin_client):
    """NULL is how "still running" reaches the panel; a run row is inserted before any work.

    Committed through an engine of its own, not the per-test `db_session`: that session's writes
    live inside a transaction the fixture rolls back, so the app's own connection would never see
    the row and the assertion would pass for the wrong reason.
    """
    client, _ = admin_client
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO digest_runs (trigger, local_date) VALUES (:t, :d)"),
            {"t": DigestTrigger.manual.value, "d": date(2026, 8, 30)},
        )
    engine.dispose()

    body = client.get(f"{PREFIX}/runs").json()
    assert len(body) == 1
    assert body[0]["status"] is None
    assert body[0]["finished_at"] is None
    assert body[0]["trigger"] == "manual"


# --- hosting ----------------------------------------------------------------------------------


def test_hosting_reports_not_configured_when_the_keys_are_absent(admin_client):
    client, _ = admin_client
    response = client.get(f"{PREFIX}/hosting")
    assert response.status_code == 200, response.text
    assert response.json() == {"configured": False, "account": None}


def test_hosting_reports_configured_once_all_three_keys_are_present(admin_client, monkeypatch):
    client, _ = admin_client
    monkeypatch.setattr(app_settings, "DROPBOX_APP_KEY", "key")
    monkeypatch.setattr(app_settings, "DROPBOX_APP_SECRET", "secret")
    monkeypatch.setattr(app_settings, "DROPBOX_REFRESH_TOKEN", "refresh")
    assert client.get(f"{PREFIX}/hosting").json()["configured"] is True


@pytest.mark.parametrize(
    "missing", ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]
)
def test_two_keys_out_of_three_is_not_configured(admin_client, monkeypatch, missing):
    """Two of the three cannot refresh a token, so reporting "configured" would make the panel
    promise hosting that silently degrades to an inline-only mail every morning."""
    client, _ = admin_client
    for key in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        monkeypatch.setattr(app_settings, key, "" if key == missing else "value")
    assert client.get(f"{PREFIX}/hosting").json()["configured"] is False


def test_hosting_never_echoes_a_credential(admin_client, monkeypatch):
    client, _ = admin_client
    monkeypatch.setattr(app_settings, "DROPBOX_APP_KEY", "sup3rsecretkey")
    monkeypatch.setattr(app_settings, "DROPBOX_APP_SECRET", "sup3rsecretsecret")
    monkeypatch.setattr(app_settings, "DROPBOX_REFRESH_TOKEN", "sup3rsecretrefresh")
    body = client.get(f"{PREFIX}/hosting").text
    assert "sup3rsecret" not in body
    assert '"configured":true' in body.replace(" ", "")  # control: the request really was served


# --- the guard serializes, it does not merely count --------------------------------------------


def _seed_enabled_recipients(*emails: str) -> list[int]:
    engine = create_engine(SYNC_URL)
    ids: list[int] = []
    with engine.begin() as conn:
        for email in emails:
            ids.append(
                conn.execute(
                    text(
                        "INSERT INTO digest_recipients (email, enabled) "
                        "VALUES (:e, true) RETURNING id"
                    ),
                    {"e": email},
                ).scalar_one()
            )
        conn.execute(text("UPDATE digest_settings SET enabled = true"))
    engine.dispose()
    return ids


def _enabled_recipient_count() -> int:
    engine = create_engine(SYNC_URL)
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM digest_recipients WHERE enabled")
        ).scalar_one()
    engine.dispose()
    return count


async def test_two_overlapping_disables_cannot_empty_an_armed_digest(db_engine, monkeypatch):
    """The A2 guard counts, and a count is a check-then-act — so it has to be SERIALIZED.

    Two requests, two real database sessions on two real connections, driving the REAL handlers
    (`remove_digest_recipient` and `update_digest_recipient`) against the last two enabled
    recipients of an armed digest. The interleave is forced with events rather than sleeps: the
    first request is held open at its COMMIT (its own session's `commit` is wrapped, which patches
    a dependency of the handler and not the handler), the second then enters the guard while the
    first still holds the row lock, and `wait_for` proves it is genuinely BLOCKED rather than
    merely losing a race — on the unserialized code it sails through in milliseconds, counts the
    other recipient as still enabled, and both disables succeed, leaving zero.
    """
    import asyncio

    from app.database import async_session
    from app.models import DigestRecipient, User
    from app.routers.admin_digest import (
        DigestRecipientPatch,
        remove_digest_recipient,
        update_digest_recipient,
    )
    from fastapi import HTTPException
    from sqlalchemy import select

    first_id, second_id = _seed_enabled_recipients("first@example.com", "second@example.com")
    admin = User(subject="race_admin", role="admin")
    first_holds_the_lock = asyncio.Event()
    second_has_entered = asyncio.Event()
    let_the_first_commit = asyncio.Event()
    outcomes: dict[str, object] = {}

    async def first_request() -> None:
        async with async_session() as db:
            real_commit = db.commit

            async def held_commit() -> None:
                first_holds_the_lock.set()
                await let_the_first_commit.wait()
                await real_commit()

            monkeypatch.setattr(db, "commit", held_commit)
            await remove_digest_recipient(first_id, admin, db)
            outcomes["first"] = 204

    async def second_request() -> None:
        async with async_session() as db:
            target = (
                await db.execute(
                    select(DigestRecipient).where(DigestRecipient.id == second_id)
                )
            ).scalar_one()
            assert target.enabled is True  # control: it really is an enabled recipient
            await first_holds_the_lock.wait()
            second_has_entered.set()
            try:
                await update_digest_recipient(
                    second_id, DigestRecipientPatch(enabled=False), admin, db
                )
            except HTTPException as refused:
                await db.rollback()
                outcomes["second"] = refused.status_code
            else:
                outcomes["second"] = 200

    first_task = asyncio.create_task(first_request())
    second_task = asyncio.create_task(second_request())
    try:
        await first_holds_the_lock.wait()
        await second_has_entered.wait()
        with pytest.raises(TimeoutError):
            # Blocked on the singleton's row lock, which the first request holds until it commits.
            # Unserialized, this returns almost immediately and the assertion fails.
            await asyncio.wait_for(asyncio.shield(second_task), timeout=3.0)
    finally:
        let_the_first_commit.set()
        await asyncio.gather(first_task, second_task)

    assert outcomes == {"first": 204, "second": 422}
    assert _enabled_recipient_count() == 1


async def test_two_overlapping_disables_both_succeed_when_a_third_recipient_remains(db_engine):
    """Control for the test above: serialization must not turn a legitimate pair of disables into
    a refusal. With three enabled recipients both requests are allowed and one is left."""
    import asyncio

    from app.database import async_session
    from app.models import User
    from app.routers.admin_digest import remove_digest_recipient

    ids = _seed_enabled_recipients("a@example.com", "b@example.com", "c@example.com")
    admin = User(subject="race_admin", role="admin")

    async def remove(recipient_id: int) -> None:
        async with async_session() as db:
            await remove_digest_recipient(recipient_id, admin, db)

    await asyncio.gather(remove(ids[0]), remove(ids[1]))
    assert _enabled_recipient_count() == 1


def test_an_over_long_display_name_is_refused_rather_than_500(admin_client):
    """A 256-character name used to pass request validation and die at the VARCHAR(255) on commit,
    which reaches the caller as a 500 for what is an ordinary input error."""
    client, _ = admin_client
    recipient_id = client.post(f"{PREFIX}/recipients", json={"email": "long@example.com"}).json()[
        "id"
    ]
    refused = client.patch(
        f"{PREFIX}/recipients/{recipient_id}", json={"display_name": "x" * 256}
    )
    assert refused.status_code == 422, refused.text
    # Control: one character shorter is stored, so the bound is the bound and not a blanket refusal.
    accepted = client.patch(
        f"{PREFIX}/recipients/{recipient_id}", json={"display_name": "x" * 255}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["display_name"] == "x" * 255


def test_an_over_long_display_name_is_refused_on_create_too(admin_client):
    client, _ = admin_client
    response = client.post(
        f"{PREFIX}/recipients", json={"email": "long2@example.com", "display_name": "x" * 256}
    )
    assert response.status_code == 422, response.text
    assert client.get(f"{PREFIX}/recipients").json() == []
