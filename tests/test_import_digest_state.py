"""Tests for the one-shot satellite → digest-tables import.

Two layers, and neither of them mocks the unit under test:

* the pure file/mapping functions (``parse_env``, ``load_registry``, ``plan_recipients``,
  ``plan_settings``) are exercised directly, one case per input CATEGORY — a heuristic with only a
  happy-path test is a heuristic that can regress silently;
* ``run_import`` runs against the real migrated schema through the ``adb`` session, so the partial
  unique index that makes the run row a find-or-create, the ``ON CONFLICT DO NOTHING`` that makes a
  second pass a no-op, and the settings latch are proven by the database, not by a fake.

The fixtures under ``tests/fixtures/`` are SYNTHETIC: uids the tests seed themselves (plus one the
database deliberately lacks), and ``example.test`` addresses. Nothing here reads a production path.
"""

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.database import engine as async_engine
from app.models import (
    DigestDelivery,
    DigestRecipient,
    DigestRun,
    DigestRunStatus,
    DigestSettings,
    DigestTrigger,
    DigestVisibilityScope,
    Memo,
    User,
    Visibility,
)
from app.scripts import import_digest_state as importer
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATE_FIXTURE = FIXTURES / "digest-state.json"
ENV_FIXTURE = FIXTURES / "digest-satellite.env"

MAPPED_UIDS = (
    "aaaa1111bbbb2222",
    "cccc3333dddd4444",
    "eeee5555ffff6666",
    "gggg7777hhhh8888iiii99",
)
UNMAPPED_UID = "zzzz0000notinthedb0000"
RECIPIENT_ADDRESSES = ("one@example.test", "two@example.test", "three@example.test")


@pytest.fixture
def registry() -> importer.Registry:
    return importer.load_registry(STATE_FIXTURE)


@pytest.fixture
def env() -> dict[str, str]:
    return importer.load_env(ENV_FIXTURE)


async def _seed_memos(adb, uids: tuple[str, ...] = MAPPED_UIDS) -> dict[str, int]:
    """The memos the registry's uids resolve to. `UNMAPPED_UID` is deliberately never seeded."""
    user = User(subject="importer-fixture", role="user")
    adb.add(user)
    await adb.flush()
    seeded: dict[str, int] = {}
    for index, uid in enumerate(uids):
        memo = Memo(
            uid=uid, owner_id=user.id, content=f"memo {index}", visibility=Visibility.PUBLIC
        )
        adb.add(memo)
        await adb.flush()
        seeded[uid] = memo.id
    return seeded


async def _import_run(adb) -> DigestRun | None:
    """The import run row, re-read from the database.

    `expire_all` first: the run and settings rows are written with SQL-side UPDATEs, which leave
    any instance already in the identity map holding the values it was loaded with. Without this
    the assertions would be reading this session's memory instead of the row.
    """
    adb.expire_all()
    return (
        await adb.execute(select(DigestRun).where(DigestRun.trigger == DigestTrigger.import_))
    ).scalar_one_or_none()


async def _settings(adb) -> DigestSettings:
    adb.expire_all()
    row = await adb.get(DigestSettings, importer.SETTINGS_ID)
    assert row is not None
    return row


async def _count(adb, model) -> int:
    return (await adb.execute(select(func.count()).select_from(model))).scalar_one()


# --- the satellite's files ----------------------------------------------------------------------


def test_parse_env_skips_comments_and_blanks_and_strips_quotes_and_export():
    parsed = importer.parse_env(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                'QUOTED="quoted value"',
                "SINGLE='single value'",
                "export EXPORTED=exported",
                "SPACED =  padded  ",
                "EMPTY=",
                "not a key=value line either",
                "NOEQUALSIGN",
            ]
        )
    )
    assert parsed["PLAIN"] == "value"
    assert parsed["QUOTED"] == "quoted value"
    assert parsed["SINGLE"] == "single value"
    assert parsed["EXPORTED"] == "exported"
    assert parsed["SPACED"] == "padded"
    assert parsed["EMPTY"] == ""
    assert "NOEQUALSIGN" not in parsed


def test_the_fixture_env_parses_into_the_keys_the_import_maps(env):
    assert env["MEMOS_VISIBILITY_FILTER"] == "PUBLIC,PROTECTED"
    assert env["LINK_IMAGES_TO_DROPBOX"] == "true"
    assert env[importer.HOSTING_TOKEN_KEY]


def test_the_fixture_registry_carries_the_uids_the_tests_seed(registry):
    assert set(registry.sent_uids) == {*MAPPED_UIDS, UNMAPPED_UID}
    assert registry.last_run == "2026-08-25T05:01:23Z"
    assert registry.hosted_media == {}


def test_an_absent_state_file_is_refused_as_unreadable(tmp_path):
    with pytest.raises(importer.ImportInputError, match="state file unreadable"):
        importer.load_registry(tmp_path / "nothing-here.json")


def test_a_state_file_that_is_not_json_is_refused_as_unreadable(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(importer.ImportInputError, match="not valid JSON"):
        importer.load_registry(path)


def test_a_state_file_whose_sent_uids_is_the_wrong_shape_is_refused_as_unreadable(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"sent_uids": ["a", "b"]}', encoding="utf-8")
    with pytest.raises(importer.ImportInputError, match="sent_uids is not an object"):
        importer.load_registry(path)


def test_an_empty_registry_is_read_successfully_and_is_not_an_unreadable_file(tmp_path):
    """LSN-0027: "no entries" and "could not be read" are different outcomes.

    The distinction is the whole point — a mistyped path must not read as an import that found
    nothing to do."""
    path = tmp_path / "state.json"
    path.write_text('{"sent_uids": {}, "last_run": null, "hosted_media": {}}', encoding="utf-8")
    empty = importer.load_registry(path)
    assert empty.sent_uids == {}


def test_an_absent_env_file_is_refused_as_unreadable(tmp_path):
    with pytest.raises(importer.ImportInputError, match="env file unreadable"):
        importer.load_env(tmp_path / "nothing-here.env")


# --- the recipient and settings mappings ---------------------------------------------------------


def test_plan_recipients_normalises_dedupes_and_rejects(env):
    addresses, candidates, rejected = importer.plan_recipients(env)
    # one@ appears twice in two spellings; "not-an-address" is not an address.
    assert candidates == 5
    assert rejected == 1
    assert addresses == RECIPIENT_ADDRESSES


def test_plan_recipients_on_an_env_without_the_key():
    assert importer.plan_recipients({}) == ((), 0, 0)


def test_plan_settings_maps_every_key_the_satellite_set(env):
    values, unusable = importer.plan_settings(env)
    assert unusable == ()
    assert values == {
        "from_name": "The Test Digest Mail",
        "from_email": "digest@example.test",
        "grace_minutes": 45,
        "max_email_bytes": 20971520,
        "start_date": datetime(2026, 6, 1, tzinfo=UTC).date(),
        "visibility_scope": DigestVisibilityScope.PUBLIC_PROTECTED,
        "dropbox_folder": "/test-mail",
        "retention_days": 14,
        "link_images_full": True,
        "hosting_enabled": True,
    }


def test_hosting_enabled_is_false_without_a_refresh_token(env):
    values, _ = importer.plan_settings({**env, importer.HOSTING_TOKEN_KEY: "   "})
    assert values["hosting_enabled"] is False
    without_token = {k: v for k, v in env.items() if k != importer.HOSTING_TOKEN_KEY}
    values, _ = importer.plan_settings(without_token)
    assert values["hosting_enabled"] is False


def test_a_key_the_satellite_never_set_leaves_its_column_alone():
    values, unusable = importer.plan_settings({})
    assert values == {"hosting_enabled": False}
    assert unusable == ()


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("IMAGE_GRACE_MINUTES", "half an hour"),
        ("IMAGE_GRACE_MINUTES", "-5"),
        ("MAX_EMAIL_SIZE_BYTES", "18 MB"),
        ("MAIL_EPOCH", "the first of June"),
        ("MEMOS_VISIBILITY_FILTER", "EVERYTHING"),
        ("LINK_IMAGES_TO_DROPBOX", "maybe"),
        ("DROPBOX_FOLDER", ""),
        ("SMTP_FROM_NAME", "x" * 256),
        ("SMTP_FROM_EMAIL", "not-an-address"),
    ],
)
def test_a_value_that_does_not_fit_its_column_is_reported_by_key_and_never_guessed(
    env, key: str, bad_value: str
):
    values, unusable = importer.plan_settings({**env, key: bad_value})
    column = next(col for env_key, col, _ in importer.SETTINGS_MAP if env_key == key)
    assert unusable == (key,)
    assert column not in values


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PUBLIC", DigestVisibilityScope.PUBLIC),
        ("public", DigestVisibilityScope.PUBLIC),
        ("PUBLIC,PROTECTED", DigestVisibilityScope.PUBLIC_PROTECTED),
        ("PROTECTED PUBLIC", DigestVisibilityScope.PUBLIC_PROTECTED),
        ("PUBLIC_PROTECTED", DigestVisibilityScope.PUBLIC_PROTECTED),
        ("PRIVATE", None),
        ("PUBLIC,PRIVATE", None),
        ("", None),
    ],
)
def test_visibility_is_mapped_per_input_category(raw: str, expected):
    assert importer._coerce_visibility(raw) == expected


# --- the import against the real schema -----------------------------------------------------------


async def test_a_dry_run_writes_nothing_at_all(adb, registry, env):
    await _seed_memos(adb)
    report = await importer.run_import(adb, registry=registry, env=env, execute=False)

    assert report.registry.entries_in_file == 5
    assert report.registry.considered == 5
    assert report.registry.mapped == 4
    assert report.registry.unmapped == 1
    assert report.registry.inserted == 0
    assert report.recipients.unique == 3
    assert report.settings.written is False
    assert report.run_id is None

    assert await _count(adb, DigestRun) == 0
    assert await _count(adb, DigestDelivery) == 0
    assert await _count(adb, DigestRecipient) == 0
    settings = await _settings(adb)
    assert settings.updated_by is None
    assert settings.hosting_enabled is False


async def test_execute_imports_registry_recipients_and_settings(adb, registry, env):
    seeded = await _seed_memos(adb)
    report = await importer.run_import(adb, registry=registry, env=env, execute=True)
    await adb.flush()

    assert (report.registry.mapped, report.registry.inserted, report.registry.unmapped) == (4, 4, 1)
    assert report.registry.already_present == 0
    assert (report.recipients.inserted, report.recipients.rejected) == (3, 1)
    assert report.settings.written is True

    run = await _import_run(adb)
    assert run is not None
    assert run.trigger == DigestTrigger.import_
    assert run.status == DigestRunStatus.sent
    assert run.finished_at is not None
    assert run.memos_sent == 4
    # The unmapped uid is recorded where an operator can act on it — never on stdout, never guessed.
    assert run.error is not None
    assert UNMAPPED_UID in run.error

    deliveries = (await adb.execute(select(DigestDelivery))).scalars().all()
    assert {d.memo_id for d in deliveries} == set(seeded.values())
    assert all(d.run_id == run.id for d in deliveries)
    assert all(d.part_index == importer.IMPORT_PART_INDEX for d in deliveries)
    assert all(d.incomplete is False for d in deliveries)
    assert all(d.sent_at is not None for d in deliveries)

    recipients = (await adb.execute(select(DigestRecipient))).scalars().all()
    assert sorted(r.email for r in recipients) == sorted(RECIPIENT_ADDRESSES)
    assert all(r.added_by == importer.IMPORTED_BY for r in recipients)

    settings = await _settings(adb)
    assert settings.from_name == "The Test Digest Mail"
    assert settings.from_email == "digest@example.test"
    assert settings.grace_minutes == 45
    assert settings.max_email_bytes == 20971520
    assert settings.visibility_scope == DigestVisibilityScope.PUBLIC_PROTECTED
    assert settings.dropbox_folder == "/test-mail"
    assert settings.retention_days == 14
    assert settings.link_images_full is True
    assert settings.hosting_enabled is True
    assert settings.updated_by == importer.IMPORTED_BY


async def test_a_second_execute_inserts_nothing_and_writes_no_settings(adb, registry, env):
    await _seed_memos(adb)
    await importer.run_import(adb, registry=registry, env=env, execute=True)
    await adb.flush()

    second = await importer.run_import(adb, registry=registry, env=env, execute=True)
    await adb.flush()

    assert second.registry.inserted == 0
    assert second.registry.already_present == 4
    assert second.recipients.inserted == 0
    assert second.recipients.already_present == 3
    assert second.settings.written is False
    assert second.settings.skipped_reason is not None
    # B4b: the stored `updated_by` is never quoted back, not even the import's own latch value.
    assert "modified by this import at" in second.settings.skipped_reason
    assert importer.IMPORTED_BY not in second.settings.skipped_reason
    # Exactly one import run row, ever — the partial unique index makes it a find-or-create.
    assert (
        await adb.execute(
            select(func.count())
            .select_from(DigestRun)
            .where(DigestRun.trigger == DigestTrigger.import_)
        )
    ).scalar_one() == 1
    assert await _count(adb, DigestDelivery) == 4
    assert await _count(adb, DigestRecipient) == 3
    assert second.run_memos_sent == 4


async def test_only_uid_imports_exactly_one_victim(adb, registry, env):
    seeded = await _seed_memos(adb)
    victim = MAPPED_UIDS[0]
    report = await importer.run_import(
        adb, registry=registry, env=env, execute=True, only_uid=victim
    )
    await adb.flush()

    assert report.registry.considered == 1
    assert (report.registry.mapped, report.registry.inserted) == (1, 1)
    delivered = (await adb.execute(select(DigestDelivery.memo_id))).scalars().all()
    assert delivered == [seeded[victim]]
    assert report.run_memos_sent == 1


async def test_only_uid_the_registry_does_not_carry_is_refused(adb, registry, env):
    await _seed_memos(adb)
    with pytest.raises(importer.ImportInputError, match="not in the registry"):
        await importer.run_import(
            adb, registry=registry, env=env, execute=True, only_uid="neverheardofit"
        )


async def test_a_restricted_pass_never_erases_the_full_pass_record_of_unmapped_uids(
    adb, registry, env
):
    await _seed_memos(adb)
    await importer.run_import(adb, registry=registry, env=env, execute=True)
    await adb.flush()

    await importer.run_import(
        adb, registry=registry, env=env, execute=True, only_uid=MAPPED_UIDS[0]
    )
    await adb.flush()

    run = await _import_run(adb)
    assert run is not None
    assert run.error is not None and UNMAPPED_UID in run.error
    # memos_sent is recounted from the deliveries, so a restricted pass cannot shrink it either.
    assert run.memos_sent == 4


async def test_limit_considers_only_the_first_entries(adb, registry, env):
    await _seed_memos(adb)
    report = await importer.run_import(adb, registry=registry, env=env, execute=True, limit=2)
    await adb.flush()

    assert report.registry.entries_in_file == 5
    assert report.registry.considered == 2
    assert report.registry.inserted == 2
    assert await _count(adb, DigestDelivery) == 2


async def test_settings_are_skipped_once_a_person_has_modified_the_row(adb, registry, env):
    await _seed_memos(adb)
    await adb.execute(
        update(DigestSettings)
        .where(DigestSettings.id == importer.SETTINGS_ID)
        .values(updated_by="alice", grace_minutes=11)
    )

    report = await importer.run_import(adb, registry=registry, env=env, execute=True)
    await adb.flush()

    assert report.settings.written is False
    assert report.settings.skipped_reason is not None
    # B4b: who touched the row is a value out of the database and is masked.
    assert "modified by an admin at" in report.settings.skipped_reason
    assert "alice" not in report.settings.skipped_reason
    refreshed = await _settings(adb)
    assert refreshed.grace_minutes == 11  # the deliberate change survived the import
    assert refreshed.hosting_enabled is False
    # The registry and recipients are NOT gated by the settings latch.
    assert report.registry.inserted == 4
    assert report.recipients.inserted == 3


async def test_force_settings_overrides_the_modified_guard(adb, registry, env):
    await _seed_memos(adb)
    await adb.execute(
        update(DigestSettings)
        .where(DigestSettings.id == importer.SETTINGS_ID)
        .values(updated_by="alice", grace_minutes=11)
    )

    report = await importer.run_import(
        adb, registry=registry, env=env, execute=True, force_settings=True
    )
    await adb.flush()

    assert report.settings.written is True
    refreshed = await _settings(adb)
    assert refreshed.grace_minutes == 45
    assert refreshed.updated_by == importer.IMPORTED_BY


async def test_an_empty_registry_executes_without_a_single_delivery(adb, env, tmp_path):
    await _seed_memos(adb)
    path = tmp_path / "state.json"
    path.write_text('{"sent_uids": {}, "hosted_media": {}}', encoding="utf-8")
    empty = importer.load_registry(path)

    report = await importer.run_import(adb, registry=empty, env=env, execute=True)
    await adb.flush()

    assert report.registry.entries_in_file == 0
    assert report.registry.inserted == 0
    assert await _count(adb, DigestDelivery) == 0
    # The run row still exists and still says so: 0 memos, not "no import happened".
    run = await _import_run(adb)
    assert run is not None and run.memos_sent == 0
    assert "no entries in the state file" in report.format(execute=True)


async def test_hosted_media_entries_are_reported_and_never_imported(adb, env, tmp_path):
    await _seed_memos(adb)
    path = tmp_path / "state.json"
    path.write_text('{"sent_uids": {}, "hosted_media": {"9": {"kind": "video"}}}', encoding="utf-8")
    report = await importer.run_import(
        adb, registry=importer.load_registry(path), env=env, execute=True
    )
    assert report.registry.hosted_media_entries == 1
    assert "hosted_media entries=1 (not imported)" in report.format(execute=True)


async def test_the_report_prints_counts_and_column_names_only(adb, registry, env):
    await _seed_memos(adb)
    report = await importer.run_import(adb, registry=registry, env=env, execute=True)
    printed = report.format(execute=True)

    # Positive control: the line the 0-hit assertions below are searched in really is there.
    assert "recipients: candidates=5" in printed
    assert "registry: entries=5 considered=5 mapped=4 unmapped=1" in printed
    for uid in (*MAPPED_UIDS, UNMAPPED_UID):
        assert uid not in printed
    for address in (*RECIPIENT_ADDRESSES, "digest@example.test"):
        assert address not in printed
    for secret in (env["DROPBOX_REFRESH_TOKEN"], env["SMTP_PASSWORD"]):
        assert secret not in printed
    # No .env VALUE reaches the terminal — only the column names it would write.
    assert "The Test Digest Mail" not in printed
    assert "/test-mail" not in printed
    assert "from_name" in printed


async def test_the_dry_run_report_says_it_wrote_nothing(adb, registry, env):
    await _seed_memos(adb)
    report = await importer.run_import(adb, registry=registry, env=env, execute=False)
    printed = report.format(execute=False)
    assert "DRY RUN — nothing written" in printed
    assert "would write" in printed
    assert "id=none (dry run)" in printed


# --- the run's record of unmapped uids (B3) ------------------------------------------------------


def test_parse_unmapped_reads_back_what_merge_wrote():
    written = importer.merge_unmapped(None, ["a", "b"], authoritative=True)
    assert written == "2 unmapped uid(s): a, b"
    assert importer.parse_unmapped(written) == ["a", "b"]


@pytest.mark.parametrize("recorded", [None, "", "a run failure that is not a uid record"])
def test_parse_unmapped_finds_nothing_in_text_it_did_not_write(recorded):
    assert importer.parse_unmapped(recorded) == []


def test_an_unrestricted_pass_replaces_the_record_because_it_saw_everything():
    assert (
        importer.merge_unmapped("1 unmapped uid(s): old", ["new"], authoritative=True)
        == "1 unmapped uid(s): new"
    )
    assert importer.merge_unmapped("1 unmapped uid(s): old", [], authoritative=True) is None


def test_a_restricted_pass_adds_to_the_record_and_never_clears_or_repeats_it():
    assert (
        importer.merge_unmapped("1 unmapped uid(s): old", ["new"], authoritative=False)
        == "2 unmapped uid(s): old, new"
    )
    # met nothing: the record of what it did not look at survives
    assert (
        importer.merge_unmapped("1 unmapped uid(s): old", [], authoritative=False)
        == "1 unmapped uid(s): old"
    )
    # met the same one again: named once
    assert (
        importer.merge_unmapped("1 unmapped uid(s): old", ["old"], authoritative=False)
        == "1 unmapped uid(s): old"
    )


async def test_a_restricted_pass_records_the_unmapped_uid_it_met(adb, registry, env):
    await _seed_memos(adb)
    report = await importer.run_import(adb, registry=registry, env=env, execute=True, limit=5)
    assert report.registry.unmapped == 1

    run = await _import_run(adb)
    assert run is not None and run.error is not None
    assert UNMAPPED_UID in run.error

    # a restricted pass that meets none of them keeps the record, and meeting it again never
    # names it twice
    await importer.run_import(
        adb, registry=registry, env=env, execute=True, only_uid=MAPPED_UIDS[0]
    )
    await importer.run_import(adb, registry=registry, env=env, execute=True, limit=5)
    run = await _import_run(adb)
    assert run is not None and run.error is not None
    assert run.error.count(UNMAPPED_UID) == 1
    assert run.error.startswith("1 unmapped uid(s):")


# --- memos_sent is the database's count, not this process's (B2) ---------------------------------


async def test_two_sequential_restricted_passes_leave_the_union_count(adb, registry, env):
    await _seed_memos(adb)
    await importer.run_import(
        adb, registry=registry, env=env, execute=True, only_uid=MAPPED_UIDS[0]
    )
    await importer.run_import(
        adb, registry=registry, env=env, execute=True, only_uid=MAPPED_UIDS[1]
    )
    run = await _import_run(adb)
    assert run is not None
    assert run.memos_sent == 2
    assert await _count(adb, DigestDelivery) == 2

    # a third pass whose slice OVERLAPS the first two adds only what is new, and the aggregate is
    # the union rather than the last pass's own tally.
    await importer.run_import(adb, registry=registry, env=env, execute=True, limit=3)
    run = await _import_run(adb)
    assert run is not None
    assert run.memos_sent == 3
    assert await _count(adb, DigestDelivery) == 3


async def test_memos_sent_matches_the_delivery_count_the_database_holds(adb, registry, env):
    await _seed_memos(adb)
    await importer.run_import(adb, registry=registry, env=env, execute=True)
    run = await _import_run(adb)
    assert run is not None
    counted = (
        await adb.execute(
            select(func.count()).select_from(DigestDelivery).where(DigestDelivery.run_id == run.id)
        )
    ).scalar_one()
    assert run.memos_sent == counted == 4


# --- the settings write is serialised and conditional (B1) ---------------------------------------


async def _commit_in_another_session(statement) -> None:
    """Run one statement on a SEPARATE connection and COMMIT it — a genuinely concurrent writer,
    not this test's own transaction."""
    async with async_engine.begin() as conn:
        await conn.execute(statement)


async def _restore_settings() -> None:
    await _commit_in_another_session(
        update(DigestSettings)
        .where(DigestSettings.id == importer.SETTINGS_ID)
        .values(updated_by=None, grace_minutes=30)
    )


async def test_a_save_that_lands_between_the_read_and_the_write_is_not_overwritten(
    db_engine, registry, env
):
    """The check-then-write window, closed.

    The import reads the latch, an admin saves and COMMITS from another connection, and only then
    does the import write. The write must find nothing to write: the refusal is in the WHERE
    clause, so there is no instant at which a decision made earlier can still be acted on.

    This one runs its transaction by hand rather than on the `adb` fixture: the import holds the
    settings row FOR UPDATE until its transaction ends, so the restore at the end would block on a
    lock the fixture only releases during teardown. Rolling back here, in the test, is what makes
    the cleanup deterministic instead of a deadlock.
    """
    async with async_engine.connect() as conn:
        transaction = await conn.begin()
        session = AsyncSession(bind=conn)
        try:
            await _seed_memos(session)
            latch = await importer.read_settings_latch(session)
            assert latch is not None and latch[0] is None  # the read a naive import would act on

            await _commit_in_another_session(
                update(DigestSettings)
                .where(DigestSettings.id == importer.SETTINGS_ID)
                .values(updated_by="alice", grace_minutes=11)
            )

            report = await importer.run_import(session, registry=registry, env=env, execute=True)

            assert report.settings.written is False
            assert report.settings.skipped_reason is not None
            assert "modified by an admin at" in report.settings.skipped_reason
            assert "alice" not in report.settings.skipped_reason
            survived = await importer.read_settings_latch(session)
            assert survived is not None and survived[0] == "alice"
            assert (
                await session.execute(
                    select(DigestSettings.grace_minutes).where(
                        DigestSettings.id == importer.SETTINGS_ID
                    )
                )
            ).scalar_one() == 11
            # The registry and the recipients are not gated by the settings latch and still landed.
            assert report.registry.inserted == 4
        finally:
            await session.close()
            await transaction.rollback()
    await _restore_settings()


PROBE = text("SELECT 1 FROM digest_settings WHERE id = :id FOR UPDATE NOWAIT")
PROBE_PARAMS = {"id": importer.SETTINGS_ID}


async def _settings_row_is_free() -> bool:
    """Ask another connection whether the settings row can be locked, without ever waiting.

    NOWAIT is what makes this a test and not a race: it answers immediately instead of blocking,
    so no sleep and no timeout is involved in either outcome.
    """
    async with async_engine.connect() as conn:
        try:
            await conn.execute(PROBE, PROBE_PARAMS)
        except DBAPIError:
            return False
        finally:
            await conn.rollback()
    return True


async def test_the_import_holds_the_settings_row_even_on_a_pass_that_writes_nothing(
    db_engine, registry, env
):
    """The mutex, isolated from the write.

    An UPDATE takes its own row lock, so a pass that writes proves nothing about the explicit
    lock. This one runs against a row an admin already claimed: the conditional write matches no
    rows and therefore locks nothing, so the row is held for the rest of the transaction only if
    the import took it FOR UPDATE on purpose.
    """
    await _commit_in_another_session(
        update(DigestSettings)
        .where(DigestSettings.id == importer.SETTINGS_ID)
        .values(updated_by="alice")
    )
    try:
        async with async_engine.connect() as conn:
            transaction = await conn.begin()
            session = AsyncSession(bind=conn)
            try:
                await _seed_memos(session)
                assert await _settings_row_is_free()  # positive control: free before the import

                report = await importer.run_import(
                    session, registry=registry, env=env, execute=True
                )
                assert report.settings.written is False  # no write, so no write-taken lock

                assert not await _settings_row_is_free()
            finally:
                await session.close()
                await transaction.rollback()
        assert await _settings_row_is_free()  # and released when the transaction ends
    finally:
        await _restore_settings()


async def test_the_conditional_write_reports_how_many_rows_it_claimed(adb):
    """`claim_settings` is the whole decision: one row when the latch is open, none when it is
    not, and `force` overrides."""
    assert await importer.claim_settings(adb, {"grace_minutes": 45}, force=False) == 1
    assert await importer.claim_settings(adb, {"grace_minutes": 46}, force=False) == 0
    assert await importer.claim_settings(adb, {"grace_minutes": 47}, force=True) == 1
    latched = await _settings(adb)
    assert latched.grace_minutes == 47


# --- --limit is a count (B5) ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "-1", "-99", "abc", ""])
def test_limit_below_one_is_a_usage_error(raw: str):
    with pytest.raises(argparse.ArgumentTypeError):
        importer.positive_int(raw)


def test_positive_int_accepts_a_count():
    assert importer.positive_int("3") == 3


async def test_run_import_refuses_a_limit_below_one_rather_than_slicing(adb, registry, env):
    for bad in (0, -1):
        with pytest.raises(importer.ImportInputError, match="must be 1 or more"):
            await importer.run_import(adb, registry=registry, env=env, execute=True, limit=bad)
    assert await _count(adb, DigestDelivery) == 0


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.scripts.import_digest_state", *args],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_the_cli_exits_2_on_a_limit_below_one(limit: str):
    result = _cli("--state", str(STATE_FIXTURE), "--env", str(ENV_FIXTURE), "--limit", limit)
    assert result.returncode == 2
    assert "1 or more" in result.stderr


def test_the_cli_runs_at_all():
    """Positive control for the two exit-2 assertions above: the same invocation path succeeds."""
    assert _cli("--help").returncode == 0


@pytest.fixture
def emitted_sql():
    """Every statement the app's engine actually sends, captured at the cursor.

    Not a mock of anything: the real import runs against the real database and this only watches
    what went over the wire — the one place a lock request and a SQL-side aggregate are visible.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: PLR0913
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)


async def test_the_run_row_is_locked_and_its_total_is_counted_in_sql(
    adb, registry, env, emitted_sql
):
    await _seed_memos(adb)
    await importer.run_import(adb, registry=registry, env=env, execute=True)

    # Positive control: the capture really saw this import's work.
    assert any("INSERT INTO digest_deliveries" in one for one in emitted_sql)

    locking = [one for one in emitted_sql if "digest_runs" in one and "FOR UPDATE" in one]
    assert locking, "the run row must be locked before its aggregate is rewritten"

    rewrites = [one for one in emitted_sql if one.lstrip().upper().startswith("UPDATE DIGEST_RUNS")]
    assert rewrites, "the run row must be rewritten with an UPDATE"
    assert any("count(" in one.lower() for one in rewrites), (
        "memos_sent must be counted in the UPDATE itself, never handed in from this process"
    )
