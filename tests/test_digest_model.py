"""The digest schema, read back off the database itself (LSN-0025).

`alembic_version` says a migration ran; it says nothing about what it built. Every claim the
Phase-1 plan makes about these tables — which defaults exist, which column deliberately has none,
which index makes single-flight a guarantee rather than a race — is asserted here against
`information_schema`/`pg_indexes` or by making the database refuse the write it is supposed to
refuse.
"""

from datetime import UTC, date, datetime, time

import pytest
from alembic import command
from alembic.config import Config
from app.models import (
    DigestDelivery,
    DigestRecipient,
    DigestRun,
    DigestSettings,
    DigestTrigger,
    StatusAlreadyTerminal,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import SYNC_URL

DIGEST_TABLES = {
    "digest_settings",
    "digest_recipients",
    "digest_runs",
    "digest_deliveries",
    "digest_hosted_media",
}

# The revision immediately BELOW the first digest migration — i.e. the schema as it stood before
# this feature existed. Named rather than expressed as `-1`, because the digest now owns more than
# one migration (Phase 2 added `digest_runs.heartbeat_at`) and a relative target would silently
# stop testing the table-creating one. A LATER digest migration must not change this constant: the
# round trip below is meant to unwind the whole feature and build it again.
BEFORE_DIGEST_REVISION = "e4a91c07b5d2"


def _alembic_config() -> Config:
    from pathlib import Path

    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


# --- what the migration built -----------------------------------------------------------------


def test_the_migration_creates_exactly_the_five_digest_tables(db_engine):
    with db_engine.connect() as conn:
        names = set(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'digest%'"
                )
            ).scalars()
        )
    assert names == DIGEST_TABLES


def test_every_non_nullable_settings_column_carries_a_default(db_engine):
    """A settings column with no default would make a later expand-only migration unable to give
    the ONE existing row a value — and would make the seed below depend on a second list of
    literals agreeing with this one."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'digest_settings'"
            )
        ).all()
    assert rows, "read no columns at all — this test walked nothing"
    missing = [name for name, nullable, default in rows if nullable == "NO" and default is None]
    assert missing == [], f"non-nullable settings columns with no default: {missing}"
    # Positive control for the sweep above: the one nullable column, which needs no default.
    assert ("updated_by", "YES", None) in rows


def test_the_settings_table_has_no_max_video_attach_bytes_column(db_engine):
    """Dropped by the 2026-08-30 decision: video is poster + play badge + Dropbox link and nothing
    ever attaches an mp4, so the cap has no meaning and must not be settable."""
    with db_engine.connect() as conn:
        names = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'digest_settings'"
                )
            ).scalars()
        )
    assert "max_video_attach_bytes" not in names
    assert "max_videos_per_memo" in names  # control: the cap that DOES exist


def test_a_run_status_is_nullable_so_in_flight_is_expressible(db_engine):
    with db_engine.connect() as conn:
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'digest_runs' AND column_name = 'status'"
            )
        ).scalar_one()
    assert nullable == "YES"


def test_a_delivery_run_id_is_required_and_has_no_default(db_engine):
    """A default here would let an insert that forgot `run_id` attach the delivery to an arbitrary
    run instead of failing — destroying the audit relationship the column exists for."""
    with db_engine.connect() as conn:
        nullable, default = conn.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'digest_deliveries' AND column_name = 'run_id'"
            )
        ).one()
    assert nullable == "NO"
    assert default is None
    # Control: a bookkeeping column on the same table that DOES carry one.
    with db_engine.connect() as conn:
        part_default = conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'digest_deliveries' AND column_name = 'part_index'"
            )
        ).scalar_one()
    assert part_default is not None


def test_both_single_flight_indexes_exist_with_their_predicates(db_engine):
    with db_engine.connect() as conn:
        definitions = {
            name: definition
            for name, definition in conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'digest_runs'")
            ).all()
        }
    day_ledger = definitions["uq_digest_runs_scheduled_local_date"]
    assert "UNIQUE" in day_ledger and "local_date" in day_ledger
    assert "WHERE" in day_ledger and "scheduled" in day_ledger
    in_flight = definitions["uq_digest_runs_single_inflight"]
    assert "UNIQUE" in in_flight and "status IS NULL" in in_flight
    import_row = definitions["uq_digest_runs_import"]
    assert "UNIQUE" in import_row and "import" in import_row


def test_the_settings_singleton_is_seeded_with_the_documented_defaults(db_engine):
    with db_engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM digest_settings")).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == 1
    assert row["enabled"] is False
    assert row["hosting_enabled"] is False
    assert row["send_time"] == time(7, 0)
    assert row["timezone"] == "Europe/Zurich"
    assert row["visibility_scope"] == "PUBLIC_PROTECTED"
    assert row["start_date"] == date(2026, 6, 1)
    assert row["grace_minutes"] == 30
    assert row["max_email_bytes"] == 18874368
    assert row["max_videos_per_memo"] == 2
    assert row["max_memos_per_run"] == 40
    assert row["dropbox_folder"] == "/memos-mail"
    assert row["retention_days"] == 30
    assert row["link_images_full"] is False
    assert row["mail_title"] == "The Daily Dami Memos Mail"
    assert row["from_name"] == "The Daily Dami Memos Mail"
    assert row["from_email"] == ""
    assert row["updated_by"] is None


# --- what the database refuses ----------------------------------------------------------------


def test_a_second_settings_row_is_refused_by_the_check_constraint(db_session):
    with pytest.raises(IntegrityError):
        db_session.execute(text("INSERT INTO digest_settings (id) VALUES (2)"))
        db_session.flush()


def test_a_delivery_without_a_run_id_fails_loudly(db_session):
    memo_id = db_session.execute(text("SELECT id FROM memos LIMIT 1")).scalar_one_or_none()
    if memo_id is None:
        owner = db_session.execute(
            text("INSERT INTO users (subject, role) VALUES ('digest_fk_probe','user') RETURNING id")
        ).scalar_one()
        memo_id = db_session.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
                "VALUES ('digestfkprobe', :o, '', CAST('PUBLIC' AS visibility), false) RETURNING id"
            ),
            {"o": owner},
        ).scalar_one()
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("INSERT INTO digest_deliveries (memo_id) VALUES (:m)"), {"m": memo_id}
        )
        db_session.flush()


def _run(**overrides) -> DigestRun:
    fields = {
        "trigger": DigestTrigger.scheduled,
        "local_date": date(2026, 8, 30),
        "status": None,
    }
    fields.update(overrides)
    return DigestRun(**fields)


def test_only_one_in_flight_run_of_any_trigger_can_exist(db_session):
    """`UNIQUE ((1)) WHERE status IS NULL`. Without it "refuse while an in-flight run exists" is a
    check-then-insert race that two concurrent run-now requests both win — and both send."""
    db_session.add(_run(trigger=DigestTrigger.scheduled))
    db_session.flush()
    db_session.add(_run(trigger=DigestTrigger.manual, local_date=date(2026, 8, 31)))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_second_in_flight_run_is_admitted_once_the_first_went_terminal(db_session):
    """The other half of the index: it fences CONCURRENT runs, not consecutive ones."""
    first = _run(trigger=DigestTrigger.manual)
    db_session.add(first)
    db_session.flush()
    first.status = "sent"
    first.finished_at = datetime.now(UTC)
    db_session.flush()
    db_session.add(_run(trigger=DigestTrigger.manual, local_date=date(2026, 8, 31)))
    db_session.flush()  # must not raise


def test_a_second_scheduled_run_for_one_local_date_is_refused(db_session):
    """The day ledger: at most one scheduled attempt per local day, terminal or not."""
    first = _run(trigger=DigestTrigger.scheduled, local_date=date(2026, 8, 30))
    db_session.add(first)
    db_session.flush()
    first.status = "sent"
    db_session.flush()
    db_session.add(_run(trigger=DigestTrigger.scheduled, local_date=date(2026, 8, 30)))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_manual_run_is_not_bound_by_the_day_ledger(db_session):
    """Control for the test above: the day ledger's predicate is `trigger = 'scheduled'`, so a
    manual run on a day that already has a scheduled one is allowed."""
    scheduled = _run(trigger=DigestTrigger.scheduled, local_date=date(2026, 8, 30))
    db_session.add(scheduled)
    db_session.flush()
    scheduled.status = "sent"
    db_session.flush()
    db_session.add(_run(trigger=DigestTrigger.manual, local_date=date(2026, 8, 30)))
    db_session.flush()  # must not raise


def test_the_import_owns_exactly_one_run_row(db_session):
    first = _run(trigger=DigestTrigger.import_, local_date=date(2026, 8, 30))
    db_session.add(first)
    db_session.flush()
    first.status = "sent"
    db_session.flush()
    db_session.add(_run(trigger=DigestTrigger.import_, local_date=date(2026, 8, 31)))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_the_import_trigger_value_is_the_bare_word(db_session):
    """The member is spelled `import_` because `import` is a Python keyword; what reaches the
    database and the API must be `import`, or the partial index above matches nothing."""
    assert DigestTrigger.import_.value == "import"
    row = _run(trigger=DigestTrigger.import_, local_date=date(2026, 8, 30))
    db_session.add(row)
    db_session.flush()
    stored = db_session.execute(
        text("SELECT trigger FROM digest_runs WHERE id = :i"), {"i": row.id}
    ).scalar_one()
    assert stored == "import"


def test_a_duplicate_recipient_address_is_refused_by_the_unique_index(db_session):
    db_session.add(DigestRecipient(email="dupe@example.com"))
    db_session.flush()
    db_session.add(DigestRecipient(email="dupe@example.com"))
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- the one-shot status guard ----------------------------------------------------------------


def test_a_terminal_status_is_written_exactly_once():
    """`memosng_digest_runs_total{status}` is a DB-derived Counter and a Counter may not move
    backwards, so a terminal status may never be rewritten — not to a different value, and not to
    the same one."""
    run = _run()
    assert run.status is None
    run.status = "sent"
    with pytest.raises(StatusAlreadyTerminal):
        run.status = "failed"
    with pytest.raises(StatusAlreadyTerminal):
        run.status = "sent"
    with pytest.raises(StatusAlreadyTerminal):
        run.status = None
    assert run.status == "sent"


def test_the_guard_survives_a_database_round_trip(db_session):
    """Loading a row must not COUNT as the first write (or every terminal row would be
    unloadable), and must not RESET the guard either (or a run reloaded in a later session
    could be rewritten)."""
    run = _run(trigger=DigestTrigger.manual)
    db_session.add(run)
    db_session.flush()
    run.status = "sent"
    db_session.flush()
    db_session.expire_all()

    reloaded = db_session.execute(select(DigestRun).where(DigestRun.id == run.id)).scalar_one()
    assert reloaded.status == "sent"  # the load itself did not trip the guard
    with pytest.raises(StatusAlreadyTerminal):
        reloaded.status = "failed"


def test_an_in_flight_run_reloaded_can_still_receive_its_one_terminal_write(db_session):
    run = _run(trigger=DigestTrigger.manual)
    db_session.add(run)
    db_session.flush()
    db_session.expire_all()

    reloaded = db_session.execute(select(DigestRun).where(DigestRun.id == run.id)).scalar_one()
    assert reloaded.status is None
    reloaded.status = "failed"  # must not raise
    db_session.flush()
    assert db_session.execute(
        text("SELECT status FROM digest_runs WHERE id = :i"), {"i": run.id}
    ).scalar_one() == "failed"


# --- the migration against a POPULATED database -----------------------------------------------


def test_downgrade_and_upgrade_round_trip_on_a_populated_database(db_engine):
    """The Story's "applies to a fresh AND to a populated database".

    `conftest.db_engine` already proves the fresh path (it drops `public` and runs
    `alembic upgrade head`). This proves the other one against a database that holds real rows in
    the tables the migration does NOT own: they must still be there afterwards, and the five digest
    tables must come back with exactly one settings row.

    It unwinds EVERY digest migration, not the newest one — the whole feature comes out and goes
    back in, which is what a rollback of this feature would actually do.
    """
    config = _alembic_config()
    engine = create_engine(SYNC_URL)
    subject = "digest_roundtrip_user"
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (subject, role) VALUES (:s, 'user')"), {"s": subject}
            )
            before = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
            assert before >= 1

        command.downgrade(config, BEFORE_DIGEST_REVISION)
        with engine.connect() as conn:
            remaining = set(
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name LIKE 'digest%'"
                    )
                ).scalars()
            )
        assert remaining == set()

        command.upgrade(config, "head")
        with engine.connect() as conn:
            restored = set(
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name LIKE 'digest%'"
                    )
                ).scalars()
            )
            settings_rows = conn.execute(
                select(func.count()).select_from(DigestSettings)
            ).scalar_one()
            users_after = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
        assert restored == DIGEST_TABLES
        assert settings_rows == 1
        assert users_after == before, "the round trip disturbed a table it does not own"
    finally:
        # Leave the schema at head whatever happened, or every test after this one runs against a
        # database missing the tables.
        command.upgrade(config, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        engine.dispose()


def test_the_delivery_registry_survives_a_memo_delete_as_a_cascade(db_session):
    """`digest_deliveries.memo_id` cascades: a deleted memo takes its delivery row with it. The
    registry's job is "do not mail this memo again", and a memo that no longer exists cannot be."""
    owner = db_session.execute(
        text("INSERT INTO users (subject, role) VALUES ('digest_cascade','user') RETURNING id")
    ).scalar_one()
    memo_id = db_session.execute(
        text(
            "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
            "VALUES ('digestcascade', :o, '', CAST('PUBLIC' AS visibility), false) RETURNING id"
        ),
        {"o": owner},
    ).scalar_one()
    run = _run(trigger=DigestTrigger.manual)
    db_session.add(run)
    db_session.flush()
    db_session.add(DigestDelivery(memo_id=memo_id, run_id=run.id))
    db_session.flush()

    db_session.execute(text("DELETE FROM memos WHERE id = :m"), {"m": memo_id})
    db_session.flush()
    assert (
        db_session.execute(
            text("SELECT count(*) FROM digest_deliveries WHERE memo_id = :m"), {"m": memo_id}
        ).scalar_one()
        == 0
    )
