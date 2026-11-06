"""Every recipient has a language, and a run sends one mail per language group (MEMNG-1245).

These tests COMMIT, like the runner's own: the per-group delivery ledger is a set of commit
boundaries, and only a real Postgres can say which rows landed. SMTP is the one system faked
(`FakeTransport`); its sends are numbered in run order, and the groups go out in the fixed order
`de`, `es`, `en`, which is what lets a test refuse exactly one group.
"""

import json
import secrets
from datetime import UTC, date, datetime

import pytest
from alembic import command
from alembic.config import Config
from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestLanguage, DigestTrigger, User, Visibility
from app.services.digest import i18n, render
from app.services.digest import metrics as digest_metrics
from app.services.digest import runner as digest_runner
from sqlalchemy import create_engine, delete, text

from tests.conftest import SYNC_URL
from tests.digest_support import (
    FakeTransport,
    add_recipients,
    delivery_rows,
    insert_run,
    plain_part,
    reset_digest_state,
    run_rows,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_mobile,
)

SENDER = "sender@example.com"
GERMAN = ["anna@example.com", "bernd@example.com"]
ENGLISH = ["carol@example.com"]
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 Europe/Zurich
LATER = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)

# The revision this story's migration sits on, named so a later migration cannot make the
# backfill test silently exercise a different one.
BEFORE_LANGUAGE_REVISION = "f8af654fa068"


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    set_digest_settings(enabled=True, start_date="2026-06-01", grace_minutes=30)
    subject = f"digest-lang-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed_photo_memos(subject: str, data_root, count: int) -> list[int]:
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo_ids = []
        for index in range(count):
            memo = await seed_memo(
                db,
                user,
                content=f"memo number {index}",
                visibility=Visibility.PUBLIC,
                created_at=datetime(2026, 6, 20, 10, index, tzinfo=UTC),
            )
            attachment = await seed_attachment(db, user, memo, mime="image/jpeg")
            write_mobile(data_root, attachment.uid, size=(64, 48), noise=True)
            memo_ids.append(memo.id)
        await db.commit()
    return memo_ids


def _ledger() -> set[tuple[int, str]]:
    return {(row["memo_id"], row["language"]) for row in delivery_rows()}


class _FirstSendRaises(FakeTransport):
    """`FakeTransport.raise_on_part` numbers sends by the mails it KEPT, so a raise would repeat on
    the next group; this raises on the first call only."""

    calls: int = 0

    async def send(self, message, sender, recipients):
        self.calls += 1
        if self.calls == 1:
            raise OSError("simulated SMTP connection failure")
        return await super().send(message, sender, recipients)


def _refused(addresses: list[str]) -> dict[str, tuple[int, bytes]]:
    return {address: (550, b"denied") for address in addresses}


# --- the catalogues ----------------------------------------------------------------------------


def test_the_three_languages_are_exactly_de_es_en():
    assert tuple(DigestLanguage) == ("de", "es", "en")
    assert i18n.LANGUAGES == ("de", "es", "en")
    assert i18n.DEFAULT_LANGUAGE == "de"


def test_every_catalogue_key_exists_in_all_three_languages():
    catalogues = i18n.load_catalogues()
    assert set(catalogues) == set(i18n.LANGUAGES)
    assert i18n.parity_gaps(catalogues) == {}
    # And every key carries the same placeholders everywhere: a translation that drops `{count}`
    # would raise at render time on the morning of the run, not here.
    assert i18n.placeholder_gaps(catalogues) == {}


def test_the_parity_check_names_a_key_missing_from_one_language():
    """The parity test is only as good as its check: a doctored catalogue must be reported."""
    catalogues = {lang: dict(entries) for lang, entries in i18n.load_catalogues().items()}
    removed = sorted(catalogues["es"])[0]
    del catalogues["es"][removed]
    assert i18n.parity_gaps(catalogues) == {"es": {removed}}


def test_the_placeholder_check_names_a_translation_that_lost_its_placeholder():
    catalogues = {lang: dict(entries) for lang, entries in i18n.load_catalogues().items()}
    catalogues["de"]["mail.more_not_shown"] = "mehr"
    assert i18n.placeholder_gaps(catalogues) == {"de": {"mail.more_not_shown"}}


def test_the_catalogue_files_ship_as_plain_json_beside_the_renderer():
    """One file per language, in the image, readable by anything that speaks JSON (the door)."""
    for language in i18n.LANGUAGES:
        path = i18n.LOCALE_DIR / f"{language}.json"
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


# --- dates through Babel -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "label"),
    [
        ("en", "Saturday, June 20, 2026"),
        ("de", "Samstag, 20. Juni 2026"),
        ("es", "sábado, 20 de junio de 2026"),
    ],
)
def test_a_day_label_is_written_in_the_groups_language(language, label):
    assert i18n.day_label(date(2026, 6, 20), language) == label


@pytest.mark.parametrize(
    ("language", "lo", "hi", "expected"),
    [
        ("en", date(2026, 6, 20), date(2026, 6, 20), "June 20, 2026"),
        ("en", date(2026, 6, 20), date(2026, 6, 22), "June 20 – 22, 2026"),
        ("en", date(2026, 6, 30), date(2026, 7, 2), "June 30 – July 2, 2026"),
        ("de", date(2026, 6, 20), date(2026, 6, 20), "20. Juni 2026"),
        ("de", date(2026, 6, 20), date(2026, 6, 22), "20.–22. Juni 2026"),
        ("de", date(2026, 6, 30), date(2026, 7, 2), "30. Juni – 2. Juli 2026"),
        ("es", date(2026, 6, 20), date(2026, 6, 20), "20 de junio de 2026"),
        ("es", date(2026, 6, 20), date(2026, 6, 22), "20–22 de junio de 2026"),
        ("es", date(2026, 6, 30), date(2026, 7, 2), "30 de junio – 2 de julio de 2026"),
    ],
)
def test_the_subject_date_range_is_written_in_the_groups_language(language, lo, hi, expected):
    groups = [render.DayGroup(date_label="", iso=d.isoformat()) for d in (hi, lo)]
    assert render.subject_date_range(groups, language) == expected


def test_a_day_is_the_local_day_of_the_digest_timezone_before_babel_names_it():
    """23:30 UTC on 19 June is already 20 June in Zurich: the label names the LOCAL day."""
    card = render.Card(
        memo_id=1, uid="u", content_html="", created_at=datetime(2026, 6, 19, 23, 30, tzinfo=UTC)
    )
    [group] = render.group_by_local_day([card], "Europe/Zurich", "de")
    assert group.date_label == "Samstag, 20. Juni 2026"


# --- one mail per language group ---------------------------------------------------------------


async def test_two_languages_send_two_localized_mails_and_the_run_records_both_groups(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed_photo_memos(subject, data_root, 2)
    transport = FakeTransport()

    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None and result.status == digest_runner.SENT
    assert [mail.recipients for mail in transport.sent] == [GERMAN, ENGLISH]
    assert [mail.message["Subject"] for mail in transport.sent] == [
        "The Daily Dami Memos Mail — 20. Juni 2026",
        "The Daily Dami Memos Mail — June 20, 2026",
    ]
    assert "Samstag, 20. Juni 2026" in plain_part(transport.sent[0].message)
    assert "Saturday, June 20, 2026" in plain_part(transport.sent[1].message)
    row = run_rows()[0]
    assert row["status"] == digest_runner.SENT
    assert row["groups_attempted"] == ["de", "en"]
    assert row["groups_accepted"] == ["de", "en"]
    assert _ledger() == {(m, lang) for m in memo_ids for lang in ("de", "en")}


async def test_a_rejected_group_writes_no_rows_while_the_accepted_group_commits(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed_photo_memos(subject, data_root, 2)
    transport = FakeTransport(refusals_by_part={1: _refused(GERMAN)})  # send 1 is the de group

    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None and result.status == digest_runner.PARTIAL
    assert _ledger() == {(m, "en") for m in memo_ids}
    row = run_rows()[0]
    assert row["status"] == digest_runner.PARTIAL
    assert row["groups_attempted"] == ["de", "en"]
    assert row["groups_accepted"] == ["en"]
    assert "de: part 1: refused by all 2" in (row["error"] or "")


async def test_a_group_whose_send_raises_is_refused_and_the_next_group_still_goes_out(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed_photo_memos(subject, data_root, 1)
    transport = _FirstSendRaises()

    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None and result.status == digest_runner.PARTIAL
    assert [mail.recipients for mail in transport.sent] == [ENGLISH]
    assert _ledger() == {(memo_ids[0], "en")}


async def test_the_next_run_reselects_exactly_the_refused_groups_memos(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed_photo_memos(subject, data_root, 2)
    await digest_runner.run_digest(
        transport=FakeTransport(refusals_by_part={1: _refused(GERMAN)}), now_utc=NOW
    )
    assert _ledger() == {(m, "en") for m in memo_ids}

    retry = FakeTransport()
    result = await digest_runner.run_digest(
        trigger=DigestTrigger.manual, transport=retry, now_utc=LATER
    )

    assert result is not None and result.status == digest_runner.SENT
    assert [mail.recipients for mail in retry.sent] == [GERMAN]  # the en group gets nothing
    assert result.memos_sent == len(memo_ids)
    second = run_rows()[1]
    assert second["groups_attempted"] == ["de"]
    assert second["groups_accepted"] == ["de"]
    second_rows = {
        (row["memo_id"], row["language"])
        for row in delivery_rows()
        if row["run_id"] == second["id"]
    }
    assert second_rows == {(m, "de") for m in memo_ids}


async def test_a_group_with_zero_enabled_recipients_is_skipped_not_failed(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients("dora@example.com", language="es", enabled=False)
    memo_ids = await _seed_photo_memos(subject, data_root, 1)
    transport = FakeTransport()

    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None and result.status == digest_runner.SENT
    assert [mail.recipients for mail in transport.sent] == [GERMAN]
    row = run_rows()[0]
    assert row["status"] == digest_runner.SENT
    assert row["error"] is None
    assert row["groups_attempted"] == ["de"]
    assert row["groups_accepted"] == ["de"]
    assert _ledger() == {(memo_ids[0], "de")}


async def test_a_group_with_nothing_new_is_not_attempted_while_another_group_sends(env):
    """A memo the de group already has is not a reason to mail de again."""
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed_photo_memos(subject, data_root, 1)
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO digest_deliveries (memo_id, run_id, language) VALUES (:m, :r, 'de')"),
            {"m": memo_ids[0], "r": insert_run(status="sent")},
        )
    engine.dispose()
    transport = FakeTransport()

    result = await digest_runner.run_digest(
        trigger=DigestTrigger.manual, transport=transport, now_utc=NOW
    )

    assert result is not None and result.status == digest_runner.SENT
    assert [mail.recipients for mail in transport.sent] == [ENGLISH]


async def test_every_group_refused_is_a_failed_run_not_a_partial_one(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    await _seed_photo_memos(subject, data_root, 1)
    transport = FakeTransport(refusals_by_part={1: _refused(GERMAN), 2: _refused(ENGLISH)})

    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None and result.status == digest_runner.FAILED
    assert delivery_rows() == []
    assert run_rows()[0]["groups_accepted"] == []


# --- the alert ---------------------------------------------------------------------------------


async def _alert_value() -> float:
    async with async_session() as db:
        digest_metrics.set_snapshot(await digest_metrics.read_snapshot(db))
    try:
        for family in digest_metrics.families():
            if family.name == digest_metrics.PARTIAL_ALERT:
                return family.samples[0].value
        raise AssertionError("the partial alert family is not published")
    finally:
        digest_metrics.set_snapshot(None)


async def test_two_partial_runs_in_a_row_raise_the_alert(env):
    insert_run(trigger="scheduled", status="partial", local_date=date(2026, 7, 1))
    assert await _alert_value() == 0.0  # one partial is not yet a pattern
    insert_run(trigger="scheduled", status="partial", local_date=date(2026, 7, 2))
    assert await _alert_value() == 1.0


async def test_a_run_that_was_not_partial_breaks_the_streak(env):
    insert_run(trigger="scheduled", status="partial", local_date=date(2026, 7, 1))
    insert_run(trigger="scheduled", status="sent", local_date=date(2026, 7, 2))
    insert_run(trigger="scheduled", status="partial", local_date=date(2026, 7, 3))
    assert await _alert_value() == 0.0


async def test_a_partial_run_counts_as_a_delivery_for_the_stale_alert(env):
    """Something WAS delivered; the two-partials alert, not the stale one, owns this signal."""
    run_id = insert_run(trigger="scheduled", status="partial", local_date=date(2026, 7, 1))
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE digest_runs SET finished_at = now() WHERE id = :i"), {"i": run_id}
        )
    engine.dispose()
    async with async_session() as db:
        snapshot = await digest_metrics.read_snapshot(db)
    assert snapshot.last_success > 0.0


# --- the migration -----------------------------------------------------------------------------


def _alembic_config() -> Config:
    from pathlib import Path

    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_the_migration_backfills_existing_deliveries_with_todays_single_recipient_language(
    db_engine,
):
    config = _alembic_config()
    engine = create_engine(SYNC_URL)
    subject = f"digest-lang-mig-{secrets.token_hex(4)}"
    reset_digest_state()
    try:
        command.downgrade(config, BEFORE_LANGUAGE_REVISION)
        with engine.begin() as conn:
            user_id = conn.execute(
                text("INSERT INTO users (subject, role) VALUES (:s, 'user') RETURNING id"),
                {"s": subject},
            ).scalar_one()
            memo_ids = [
                conn.execute(
                    text(
                        "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
                        "VALUES (:uid, :owner, 'x', 'PUBLIC', false) RETURNING id"
                    ),
                    {"uid": f"{subject}-{index}", "owner": user_id},
                ).scalar_one()
                for index in range(2)
            ]
            run_id = conn.execute(
                text(
                    "INSERT INTO digest_runs (trigger, status, local_date) "
                    "VALUES ('scheduled', 'sent', '2026-07-01') RETURNING id"
                )
            ).scalar_one()
            for memo_id in memo_ids:
                conn.execute(
                    text("INSERT INTO digest_deliveries (memo_id, run_id) VALUES (:m, :r)"),
                    {"m": memo_id, "r": run_id},
                )
            conn.execute(text("INSERT INTO digest_recipients (email) VALUES ('old@example.com')"))

        command.upgrade(config, "head")

        with engine.connect() as conn:
            delivered = set(
                conn.execute(text("SELECT memo_id, language FROM digest_deliveries")).all()
            )
            recipient_languages = set(
                conn.execute(text("SELECT language FROM digest_recipients")).scalars()
            )
            key = list(
                conn.execute(
                    text(
                        "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                        "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                        "WHERE i.indrelid = 'digest_deliveries'::regclass AND i.indisprimary "
                        "ORDER BY array_position(i.indkey, a.attnum)"
                    )
                ).scalars()
            )
        assert recipient_languages == {"de"}
        assert delivered == {(m, "de") for m in memo_ids}
        assert key == ["memo_id", "language"]
    finally:
        command.upgrade(config, "head")
        reset_digest_state()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        engine.dispose()


def test_a_memo_is_registered_once_per_language(db_engine):
    reset_digest_state()
    engine = create_engine(SYNC_URL)
    subject = f"digest-lang-key-{secrets.token_hex(4)}"
    try:
        with engine.begin() as conn:
            user_id = conn.execute(
                text("INSERT INTO users (subject, role) VALUES (:s, 'user') RETURNING id"),
                {"s": subject},
            ).scalar_one()
            memo_id = conn.execute(
                text(
                    "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
                    "VALUES (:uid, :owner, 'x', 'PUBLIC', false) RETURNING id"
                ),
                {"uid": subject, "owner": user_id},
            ).scalar_one()
        run_id = insert_run(status="sent")
        insert = text(
            "INSERT INTO digest_deliveries (memo_id, run_id, language) VALUES (:m, :r, :l)"
        )
        with engine.begin() as conn:
            conn.execute(insert, {"m": memo_id, "r": run_id, "l": "de"})
            conn.execute(insert, {"m": memo_id, "r": run_id, "l": "en"})
        with pytest.raises(Exception, match="digest_deliveries_pkey"), engine.begin() as conn:
            conn.execute(insert, {"m": memo_id, "r": run_id, "l": "de"})
        with (
            pytest.raises(Exception, match="ck_digest_deliveries_language"),
            engine.begin() as conn,
        ):
            conn.execute(insert, {"m": memo_id, "r": run_id, "l": "fr"})
    finally:
        reset_digest_state()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        engine.dispose()


# --- the test-send language selector -----------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("de", "The Daily Dami Memos Mail — 20. Juni 2026"),
        ("es", "The Daily Dami Memos Mail — 20 de junio de 2026"),
        ("en", "The Daily Dami Memos Mail — June 20, 2026"),
    ],
)
async def test_a_test_send_renders_in_the_language_it_was_asked_for(env, language, expected):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    await _seed_photo_memos(subject, data_root, 1)
    transport = FakeTransport()
    count = await digest_runner.test_send(
        address="admin@x", language=language, transport=transport, now_utc=NOW
    )
    assert count == 1
    assert transport.sent[0].message["Subject"] == expected


async def test_an_empty_test_send_speaks_the_asked_language(env):
    transport = FakeTransport()
    await digest_runner.test_send(
        address="admin@x", empty=True, language="es", transport=transport, now_utc=NOW
    )
    assert i18n.text("es", "mail.empty") in plain_part(transport.sent[0].message)
