"""The three admin ACTION endpoints: preview, test-send and run now (MEMNG-234).

`tests/test_admin_digest.py` already sweeps every route in this router for authorization, and it
covers these three the moment they exist because it reads the route table off the app. What is
left is behaviour, and three contracts carry real risk:

* **preview persists nothing** — no run row, no `digest_hosted_media` row, no Dropbox call;
* **test-send goes to the calling admin's own address**, resolved from the authenticated session
  and never from a request field. An endpoint that took an address would be an open relay for
  anyone who reached it, and would let a probe reach `digest_recipients`;
* **run-now CLAIMS and does not execute** — it commits the run row (which IS the single-flight
  lock), answers 202 with a `Location`, and leaves the pipeline to the worker. Its 409 comes from
  the INSERT conflicting, never from a pre-flight "is anything running?" read — a check-then-insert
  is the TOCTOU race the unique index exists to close.
"""

import secrets
from datetime import UTC, datetime

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestTrigger, User, Visibility
from app.services.digest import mailer as digest_mailer
from app.services.digest import runner as digest_runner
from sqlalchemy import delete

from tests.digest_support import (
    FakeTransport,
    add_recipients,
    forbid_real_smtp,
    hosted_rows,
    reset_digest_state,
    run_rows,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_mobile,
    write_web_mp4,
)

PREFIX = "/api/admin/digest"
SENDER = "sender@example.com"
MEMOS_HOST = "memos.discomarder.live"


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients("a@example.com", "b@example.com")
    set_digest_settings(enabled=True, start_date="2026-06-01")
    subject = f"digest-actions-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    """Every test in this module drives the real handler, so a missed transport seam would open a
    socket to iCloud. It fails as a named assertion instead."""
    forbid_real_smtp(monkeypatch)


@pytest.fixture
def capture_sends(monkeypatch) -> FakeTransport:
    """Replace the SMTP TRANSPORT — the external system — and nothing else.

    The handler, the runner, the renderer and the splitter all run for real; only the socket is
    substituted, which is the whole point (a real network call in a test is a defect).
    """
    transport = FakeTransport()
    monkeypatch.setattr(digest_runner, "default_transport", lambda: transport)
    return transport


async def _seed_memo_with_photo(subject: str, data_root, *, content: str = "hello") -> int:
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db,
            user,
            content=content,
            visibility=Visibility.PUBLIC,
            created_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
        attachment = await seed_attachment(db, user, memo, mime="image/jpeg")
        write_mobile(data_root, attachment.uid)
        await db.commit()
        return memo.id


# --- preview ---------------------------------------------------------------------------------


async def test_preview_returns_what_would_go_out_and_persists_nothing(
    env, admin_client, capture_sends
):
    subject, data_root = env
    memo_id = await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.get(f"{PREFIX}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["memo_count"] == 1 and body["parts"] == 1
    assert [item["memo_id"] for item in body["selected"]] == [memo_id]
    assert body["selected"][0]["local_day"] == "2026-06-20"
    assert "hello" in body["html"]
    assert body["plain"].startswith("The Daily Dami Memos Mail")
    assert run_rows() == [] and hosted_rows() == []
    assert capture_sends.sent == []


async def test_preview_names_the_deferred_memos_and_why(env, admin_client):
    subject, data_root = env
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(db, user, visibility=Visibility.PUBLIC, created_at=datetime.now(UTC))
        await seed_attachment(db, user, memo, mime="image/jpeg")  # no thumb on disk
        await db.commit()
        deferred_uid = memo.uid
    client, _ = admin_client
    body = client.get(f"{PREFIX}/preview").json()
    assert body["selected"] == []
    assert [item["uid"] for item in body["deferred"]] == [deferred_uid]
    assert body["deferred"][0]["reason"]


async def test_preview_of_an_empty_selection_still_renders_a_body(env, admin_client):
    client, _ = admin_client
    body = client.get(f"{PREFIX}/preview").json()
    assert body["memo_count"] == 0 and body["parts"] == 0
    assert "No memos selected." in body["html"]


async def test_preview_strips_a_memos_ng_link(env, admin_client):
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root, content=f"see [it](https://{MEMOS_HOST}/m/abc)")
    client, _ = admin_client
    assert MEMOS_HOST not in client.get(f"{PREFIX}/preview").json()["html"]


# --- test-send -------------------------------------------------------------------------------


async def test_a_test_send_goes_to_the_calling_admin_only(env, admin_client, capture_sends):
    """`admin@x` is the address the admin fixture's session carries. The recipients configured
    above must not see this mail in any configuration."""
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.post(f"{PREFIX}/test-send", json={})
    assert response.status_code == 200, response.text
    assert response.json() == {"sent_to": "admin@x", "memo_count": 1}
    assert [mail.recipients for mail in capture_sends.sent] == [["admin@x"]]
    assert "a@example.com" not in capture_sends.sent[0].message.as_string()


async def test_a_test_send_writes_no_run_row_and_no_hosted_media(env, admin_client, capture_sends):
    """A6: a probe must never create a Dropbox file the registry does not know about, or one the
    retention sweep would later have to guess at."""
    subject, data_root = env
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db,
            user,
            content="a clip",
            visibility=Visibility.PUBLIC,
            created_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
        attachment = await seed_attachment(db, user, memo, mime="video/mp4", filename="c.mp4")
        write_web_mp4(data_root, attachment.uid)
        await db.commit()
    set_digest_settings(hosting_enabled=True)
    client, _ = admin_client
    assert client.post(f"{PREFIX}/test-send", json={}).status_code == 200
    assert run_rows() == []
    assert hosted_rows() == []
    assert len(capture_sends.sent) == 1


async def test_a_test_send_always_sends_even_with_nothing_selected(
    env, admin_client, capture_sends
):
    client, _ = admin_client
    response = client.post(f"{PREFIX}/test-send", json={})
    assert response.status_code == 200
    assert response.json()["memo_count"] == 0
    assert len(capture_sends.sent) == 1


async def test_the_empty_flag_is_a_pure_smtp_probe(env, admin_client, capture_sends):
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.post(f"{PREFIX}/test-send", json={"empty": True})
    assert response.json()["memo_count"] == 0
    assert len(capture_sends.sent) == 1
    assert run_rows() == []


async def test_a_test_send_is_refused_when_smtp_is_unconfigured(
    env, admin_client, monkeypatch, capture_sends
):
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "")
    client, _ = admin_client
    refused = client.post(f"{PREFIX}/test-send", json={})
    assert refused.status_code == 422
    assert refused.json()["detail"] == digest_runner.ERROR_SMTP_UNCONFIGURED
    assert capture_sends.sent == []


async def test_a_test_send_never_takes_its_destination_from_the_request(
    env, admin_client, capture_sends
):
    """An endpoint that accepted an address would be an open relay for anyone who reached it. The
    field is not merely ignored — the schema refuses it."""
    client, _ = admin_client
    response = client.post(f"{PREFIX}/test-send", json={"to": "stranger@example.com"})
    assert response.status_code == 200, response.text
    assert response.json()["sent_to"] == "admin@x"
    assert [mail.recipients for mail in capture_sends.sent] == [["admin@x"]]
    assert "stranger@example.com" not in capture_sends.sent[0].message.as_string()


# --- run now ---------------------------------------------------------------------------------


async def test_run_now_claims_the_run_and_sends_nothing_itself(env, admin_client, capture_sends):
    """The request commits the LOCK and returns. Nothing is rendered, uploaded or mailed inside
    it: a run legitimately lasts minutes to hours, and a contract that awaited it would put a
    browser tab and a proxy read timeout in charge of whether the household gets its mail."""
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.post(f"{PREFIX}/run")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["trigger"] == DigestTrigger.manual
    assert body["status"] is None
    assert set(body) == {"id", "status", "trigger"}
    assert response.headers["location"] == f"{PREFIX}/runs/{body['id']}"

    # The row is in flight and UNADOPTED — no heartbeat, because nothing has started it yet.
    assert [(row["trigger"], row["status"], row["heartbeat_at"]) for row in run_rows()] == [
        (DigestTrigger.manual, None, None)
    ]
    assert capture_sends.sent == []
    from tests.digest_support import delivery_rows

    assert delivery_rows() == []

    # …and it is readable at the URL the response pointed at.
    followed = client.get(f"{PREFIX}/runs/{body['id']}")
    assert followed.status_code == 200, followed.text
    assert followed.json()["status"] is None


async def test_run_now_answers_409_while_a_run_is_in_flight(env, admin_client, capture_sends):
    """The 409 is the caught unique violation. Asserted by letting the handler race a row that is
    ALREADY in flight rather than by stubbing a pre-check — a pre-check is the race, not the
    guard."""
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    async with async_session() as db:
        await digest_runner.open_run(
            db, trigger=DigestTrigger.scheduled, local_date=datetime.now(UTC).date()
        )
    client, _ = admin_client
    refused = client.post(f"{PREFIX}/run")
    assert refused.status_code == 409, refused.text
    assert capture_sends.sent == []
    assert len(run_rows()) == 1


async def test_run_now_succeeds_once_the_in_flight_run_ends(env, admin_client, capture_sends):
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    async with async_session() as db:
        run_id = await digest_runner.open_run(
            db, trigger=DigestTrigger.scheduled, local_date=datetime.now(UTC).date()
        )
    client, _ = admin_client
    assert client.post(f"{PREFIX}/run").status_code == 409
    async with async_session() as db:
        await digest_runner.finish_run(db, run_id, status=digest_runner.FAILED)
    assert client.post(f"{PREFIX}/run").status_code == 202
    assert capture_sends.sent == []  # the claim sends nothing; the worker does


async def test_a_guard_that_bites_writes_a_failed_row_rather_than_refusing_the_claim(
    env, admin_client, capture_sends, monkeypatch
):
    """The guards are part of the RUN, not of the request. The claim succeeds — it is only a lock —
    and the failure appears where an operator looks for it: the run row, the Runs table and the
    metrics. Answering the CLAIM 422 would put the failure only in a response body nobody keeps.
    """
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "")
    client, _ = admin_client
    claimed = client.post(f"{PREFIX}/run")
    assert claimed.status_code == 202, claimed.text
    run_id = claimed.json()["id"]

    result = await digest_runner.run_claimed_digest_safe(run_id, transport=capture_sends)
    assert result is not None and result.status == digest_runner.FAILED
    assert result.error == digest_runner.ERROR_SMTP_UNCONFIGURED
    assert capture_sends.sent == []
    row = client.get(f"{PREFIX}/runs/{run_id}").json()
    assert row["status"] == digest_runner.FAILED
    assert row["error"] == digest_runner.ERROR_SMTP_UNCONFIGURED


async def test_the_test_send_handler_refreshes_the_metrics_snapshot(
    env, admin_client, capture_sends
):
    """ "The metrics change after a send" has to be probeable without a 60-second wait, which is
    what `refresh_now()` at the end of the test-send handler is for.

    Run-now no longer refreshes anything, and that is correct rather than an omission: at the
    moment it answers, nothing has happened yet — the row it committed is in flight, contributes
    to no terminal label, and the worker's outcome reaches the app through the ordinary 60 s
    refresher.
    """
    from app.services.digest import metrics as digest_metrics

    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    async with async_session() as db:
        run_id = await digest_runner.open_run(
            db, trigger=DigestTrigger.scheduled, local_date=datetime.now(UTC).date()
        )
        await digest_runner.finish_run(db, run_id, status=digest_runner.SENT, memos_sent=1)
    digest_metrics.set_snapshot(None)
    client, _ = admin_client
    assert client.post(f"{PREFIX}/test-send", json={"empty": True}).status_code == 200
    snapshot = digest_metrics.current()
    assert snapshot is not None
    assert snapshot.runs_by_status == {digest_runner.SENT: 1}
    digest_metrics.set_snapshot(None)


def test_the_real_transport_is_what_the_handlers_use_by_default(monkeypatch):
    """A positive control on the fixture above: the substitution is a TEST seam, and production
    builds a real smtplib transport from the app settings."""
    monkeypatch.setattr(app_settings, "SMTP_HOST", "smtp.example")
    monkeypatch.setattr(app_settings, "SMTP_PORT", 587)
    transport = digest_runner.default_transport()
    assert isinstance(transport, digest_mailer.SmtplibTransport)


# --- the sender guard reaches test-send too (review B3) -----------------------------------------


async def test_a_test_send_to_an_admin_who_IS_the_sender_is_refused(
    env, admin_client, capture_sends, monkeypatch
):
    """A real run refuses `sender == recipient` (AC 7) because iCloud DROPS From == To with
    attachments. The probe an admin uses to CHECK the channel must refuse it for the same reason —
    otherwise it reports a clean send and delivers nothing, which is worse than no probe.

    `admin@x` is the address the admin fixture's session carries, so making it the sender is how
    this configuration is reached.
    """
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", "admin@x")
    client, _ = admin_client
    refused = client.post(f"{PREFIX}/test-send", json={})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == digest_runner.ERROR_SENDER_IS_RECIPIENT
    assert capture_sends.sent == []


async def test_a_test_send_is_allowed_when_the_admin_is_not_the_sender(
    env, admin_client, capture_sends
):
    """A positive control on the refusal above: it must bite on the collision only."""
    client, _ = admin_client
    assert client.post(f"{PREFIX}/test-send", json={}).status_code == 200
    assert [mail.recipients for mail in capture_sends.sent] == [["admin@x"]]


@pytest.mark.parametrize(
    ("language", "date_range"),
    [("de", "20. Juni 2026"), ("es", "20 de junio de 2026"), ("en", "June 20, 2026")],
)
async def test_the_test_send_language_selector_picks_the_mails_language(
    env, admin_client, capture_sends, language, date_range
):
    """MEMNG-1245: the admin chooses which group's mail the probe is."""
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.post(f"{PREFIX}/test-send", json={"language": language})
    assert response.status_code == 200, response.text
    assert capture_sends.sent[0].message["Subject"].endswith(date_range)


async def test_a_test_send_in_an_unknown_language_is_422_and_sends_nothing(
    env, admin_client, capture_sends
):
    client, _ = admin_client
    response = client.post(f"{PREFIX}/test-send", json={"language": "fr"})
    assert response.status_code == 422
    assert capture_sends.sent == []


# --- link mode (MEMNG-1246) ----------------------------------------------------------------------


async def test_a_link_mode_preview_names_the_day_it_links_and_publishes_nothing(
    env, admin_client, capture_sends, monkeypatch
):
    subject, data_root = env
    monkeypatch.setattr(app_settings, "DAILY_PAGE_HOST", "dami.discomarder.live")
    set_digest_settings(delivery_mode="link")
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.get(f"{PREFIX}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery_mode"] == "link"
    assert body["link_day"] == "2026-06-20" and body["link_older_days"] == 0
    assert body["link_url"] == "https://dami.discomarder.live/"
    assert 'href="https://dami.discomarder.live/"' in body["html"]
    assert body["parts"] == 1
    assert run_rows() == [] and capture_sends.sent == []


async def test_an_attachments_preview_carries_no_link_fields(env, admin_client):
    subject, data_root = env
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    body = client.get(f"{PREFIX}/preview").json()
    assert body["delivery_mode"] == "attachments"
    assert body["link_day"] is None and body["link_url"] is None


async def test_a_link_mode_preview_with_a_memos_ng_page_host_is_a_422(
    env, admin_client, monkeypatch
):
    subject, data_root = env
    monkeypatch.setattr(app_settings, "DAILY_PAGE_HOST", MEMOS_HOST)
    set_digest_settings(delivery_mode="link")
    await _seed_memo_with_photo(subject, data_root)
    client, _ = admin_client
    response = client.get(f"{PREFIX}/preview")
    assert response.status_code == 422
    assert "memos-ng host" in response.json()["detail"]
