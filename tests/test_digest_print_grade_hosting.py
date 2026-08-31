"""The digest's hosted "Open full size" copies are the PRINT profile's renditions (MEMNG-240).

The hosted copies are temporary — the digest's own `retention_days`, default 30 — so they may
carry photobook quality, and a household member who opens one gets the picture rather than a
2048 px screen tier. The rendition is derived IN THE RUN when it is not already on disk, because
the media queue structurally cannot serve this case: a memo is one-shot
(`select.load_candidates` anti-joins `digest_deliveries`), so a job enqueued here would finish
after the mail had gone, for a memo no later run will look at again.

That exception is bounded, and every bound is asserted here rather than described: a per-run
encode budget, a host-temperature check before each encode, and a cache that is REUSED rather
than rebuilt. All three degrade the same way — the copy falls back to the screen tier the digest
hosted before this existed. **None of them ever defers a memo or drops a link.**
"""

import secrets
from datetime import UTC, datetime

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import MailSubsampling, User, Visibility
from app.services import media, printready
from app.services.digest import hosting as digest_hosting
from app.services.digest import runner as digest_runner
from sqlalchemy import delete
from structlog.testing import capture_logs

from tests.digest_support import (
    FakeHost,
    FakeTransport,
    add_recipients,
    host_factory_for,
    hosted_rows,
    jpeg_facts,
    reset_digest_state,
    reset_export_settings,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_mobile,
    write_original,
)

RECIPIENTS = ["a@example.com", "b@example.com"]
SENDER = "sender@example.com"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    """An armed digest with hosting and `link_images_full` ON — the setting is off by default, so
    every test here has to ask for it."""
    reset_digest_state()
    reset_export_settings()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients(*RECIPIENTS)
    set_digest_settings(
        enabled=True,
        start_date="2026-06-01",
        grace_minutes=30,
        hosting_enabled=True,
        link_images_full=True,
    )
    subject = f"digest-print-grade-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        reset_export_settings()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed_photos(subject: str, data_root, count: int, *, size=(5000, 3000)) -> list[str]:
    """`count` public photo memos, each with a real ORIGINAL on disk and its screen tier.

    Both files matter and they are different pictures: the mail inlines the mobile tier, and the
    print rendition is derived from the original — never from a screen tier, which is the whole
    reason `printready.source_path` exists.
    """
    uids: list[str] = []
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        for index in range(count):
            memo = await seed_memo(
                db,
                user,
                content=f"memo number {index}",
                visibility=Visibility.PUBLIC,
                created_at=datetime(2026, 6, 20, 10, index, tzinfo=UTC),
            )
            uid = f"photo-{index}-{secrets.token_hex(3)}"
            original = write_original(data_root, uid, size=size)
            await seed_attachment(
                db, user, memo, mime="image/jpeg", uid=uid, storage_path=str(original)
            )
            write_mobile(data_root, uid, size=(600, 400))
            uids.append(uid)
        await db.commit()
    return uids


def _uploaded_paths(host: FakeHost) -> list[str]:
    return [local for local, _ in host.uploads]


def _print_path(uid: str) -> str:
    return str(printready.printready_path(uid, printready.DEFAULT_PROFILE))


def _mobile_path(data_root, uid: str) -> str:
    return str(data_root / "assets" / "mobile" / f"{uid}.jpg")


async def _run(host: FakeHost) -> FakeTransport:
    transport = FakeTransport()
    await digest_runner.run_digest(
        transport=transport, host_factory=host_factory_for(host), now_utc=NOW
    )
    return transport


# --- the copy that gets hosted --------------------------------------------------------------------


async def test_the_hosted_full_size_copy_is_the_print_profiles_rendition(env):
    """AC 2: the file the host receives is the print rendition, and its BYTES say so."""
    subject, data_root = env
    (uid,) = await _seed_photos(subject, data_root, 1)
    host = FakeHost()
    await _run(host)

    assert _uploaded_paths(host) == [_print_path(uid)]
    facts = jpeg_facts(printready.printready_path(uid, printready.DEFAULT_PROFILE).read_bytes())
    # The shipped print profile: 4096 px long edge, 4:4:4 (chroma artefacts are visible in print),
    # sRGB embedded. Every one of those differs from the mail profile the same run inlined with.
    assert facts.long_edge == printready.MAX_EDGE
    assert facts.subsampling == MailSubsampling.C444
    assert facts.has_icc is True


async def test_the_hosted_media_ledger_records_the_print_grade_upload(env):
    """The ledger is what owns the object on Dropbox — an upload it does not name is the one
    outcome that whole mechanism exists to prevent, print-grade or not."""
    subject, data_root = env
    (uid,) = await _seed_photos(subject, data_root, 1)
    host = FakeHost()
    await _run(host)

    rows = [row for row in hosted_rows() if row["kind"] == digest_hosting.IMAGE_FULL]
    assert len(rows) == 1
    assert rows[0]["status"] == "uploaded"
    assert rows[0]["shared_link"]
    assert rows[0]["dropbox_path"].endswith(f"{uid}.jpg")


async def test_a_photo_with_no_original_still_gets_its_link_from_the_screen_tier(env):
    """Nothing to derive from is not a reason to drop a link: the fallback is the tier the digest
    hosted before this feature existed."""
    subject, data_root = env
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db,
            user,
            content="no master",
            visibility=Visibility.PUBLIC,
            created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        )
        uid = f"no-master-{secrets.token_hex(3)}"
        await seed_attachment(db, user, memo, mime="image/jpeg", uid=uid)  # no storage_path
        write_mobile(data_root, uid, size=(600, 400))
        await db.commit()

    host = FakeHost()
    await _run(host)
    assert _uploaded_paths(host) == [_mobile_path(data_root, uid)]


# --- the three bounds ---------------------------------------------------------------------------


async def test_the_run_derives_no_more_than_its_budget_and_the_rest_fall_back(env, monkeypatch):
    """Mitigation 1. The shipped budget is `MAX_PRINTREADY_DERIVES_PER_RUN` (12); the mechanism is
    asserted at 2 so the test states its own arithmetic instead of encoding a constant."""
    subject, data_root = env
    uids = await _seed_photos(subject, data_root, 4, size=(1200, 800))
    monkeypatch.setattr(digest_runner, "MAX_PRINTREADY_DERIVES_PER_RUN", 2)

    host = FakeHost()
    with capture_logs() as logs:
        await _run(host)

    uploaded = _uploaded_paths(host)
    assert len(uploaded) == 4  # control: every photo is still hosted — nothing was deferred
    assert uploaded[:2] == [_print_path(uid) for uid in uids[:2]]
    assert uploaded[2:] == [_mobile_path(data_root, uid) for uid in uids[2:]]
    assert [log["event"] for log in logs].count("digest.printready_budget_spent") == 1
    # And the encodes really did not happen for the last two.
    assert not printready.printready_path(uids[3], printready.DEFAULT_PROFILE).exists()


async def test_the_budget_counts_ATTEMPTS_so_failing_encodes_cannot_run_away(env, monkeypatch):
    """The bound has to be on work STARTED, not work that worked.

    The expensive half of a print derive — the 40-60 MP decode and the LANCZOS reduction — is paid
    before anything can fail, so a budget that only counted successes would let a folder of
    corrupt masters do unbounded Pillow work while reporting that it derived nothing. Every
    encode here raises, and the gate must still trip after `allowance` of them.
    """
    subject, data_root = env
    uids = await _seed_photos(subject, data_root, 4, size=(1200, 800))
    monkeypatch.setattr(digest_runner, "MAX_PRINTREADY_DERIVES_PER_RUN", 2)

    attempts: list[int] = []

    def always_failing(attachment, profile=None):
        attempts.append(1)
        raise OSError("simulated decode failure")

    monkeypatch.setattr(media, "process_printready", always_failing)

    host = FakeHost()
    with capture_logs() as logs:
        await _run(host)

    assert len(attempts) == 2  # the gate tripped on attempts; it would be 4 on successes-only
    # Every photo is still hosted, all four from the screen tier — nothing deferred, nothing lost.
    assert _uploaded_paths(host) == [_mobile_path(data_root, uid) for uid in uids]
    events = [log["event"] for log in logs]
    assert events.count("digest.printready_derive_failed") == 2
    assert events.count("digest.printready_budget_spent") == 1


async def test_a_hot_host_hosts_screen_tiers_and_says_so_once(env, monkeypatch, tmp_path):
    """Mitigation 2, against a FAKED SENSOR rather than a patched predicate: the run reads the
    same thermal zone the media worker does, so this exercises `media.too_hot` itself."""
    subject, data_root = env
    uids = await _seed_photos(subject, data_root, 2, size=(1200, 800))
    zone = tmp_path / "temp1_input"
    zone.write_text(str((app_settings.THERMAL_MAX_C + 25) * 1000))
    monkeypatch.setattr(app_settings, "THERMAL_ZONE", str(zone))
    assert media.too_hot() is True  # control: the fake sensor really does read as too hot

    host = FakeHost()
    with capture_logs() as logs:
        await _run(host)

    assert _uploaded_paths(host) == [_mobile_path(data_root, uid) for uid in uids]
    assert [log["event"] for log in logs].count("digest.printready_too_hot") == 1
    assert not printready.printready_path(uids[0], printready.DEFAULT_PROFILE).exists()


async def test_a_cool_host_with_the_same_photos_does_derive_them(env, monkeypatch, tmp_path):
    """The positive control on the test above: with the sensor reading cool, the identical run
    produces print-grade copies. Without this, a heat test would pass on a broken derive."""
    subject, data_root = env
    uids = await _seed_photos(subject, data_root, 2, size=(1200, 800))
    zone = tmp_path / "temp1_input"
    zone.write_text(str((app_settings.THERMAL_MAX_C - 25) * 1000))
    monkeypatch.setattr(app_settings, "THERMAL_ZONE", str(zone))

    host = FakeHost()
    await _run(host)
    assert _uploaded_paths(host) == [_print_path(uid) for uid in uids]


async def test_a_rendition_already_on_disk_is_reused_and_never_encoded_twice(env, monkeypatch):
    """Mitigation 3. The rendition is written through the NORMAL profile-versioned cache path, so
    it is work the photo book and every later run inherit — counted, because "it was reused" is
    exactly the claim a second encode would satisfy just as invisibly."""
    subject, data_root = env
    (uid,) = await _seed_photos(subject, data_root, 1, size=(1200, 800))
    encodes: list[int] = []
    real = media.process_printready

    def counting(attachment, profile=None):
        encodes.append(1)
        return real(attachment, profile)

    monkeypatch.setattr(media, "process_printready", counting)

    first = FakeHost()
    await _run(first)
    assert _uploaded_paths(first) == [_print_path(uid)]
    assert len(encodes) == 1

    # A second run over the SAME photo: the delivery registry and the hosted-media ledger are
    # cleared (that is what makes the memo eligible again), the cache on disk is not.
    reset_digest_state()
    add_recipients(*RECIPIENTS)
    set_digest_settings(
        enabled=True,
        start_date="2026-06-01",
        grace_minutes=30,
        hosting_enabled=True,
        link_images_full=True,
    )
    second = FakeHost()
    await _run(second)
    assert _uploaded_paths(second) == [_print_path(uid)]
    assert len(encodes) == 1  # the expensive thing happened once, for two runs


async def test_an_unreadable_master_costs_one_copy_its_quality_and_not_the_run(env):
    """A corrupt original must degrade ONE card to the screen tier, never fail the digest."""
    subject, data_root = env
    uids = await _seed_photos(subject, data_root, 2, size=(1200, 800))
    (data_root / "originals" / f"{uids[0]}.jpg").write_bytes(b"not a jpeg at all")

    host = FakeHost()
    transport = await _run(host)
    assert len(transport.sent) == 1  # the run went out
    assert _uploaded_paths(host) == [
        _mobile_path(data_root, uids[0]),
        _print_path(uids[1]),
    ]


# --- the probes must never derive ---------------------------------------------------------------


async def test_a_preview_derives_nothing(env):
    """A preview reaches the pipeline with `host=None` by contract. Spending the host's CPU on
    renditions for files nothing will send is exactly what that contract is for."""
    subject, data_root = env
    uids = await _seed_photos(subject, data_root, 1, size=(1200, 800))
    rendered, _ = await digest_runner.preview(now_utc=NOW)
    assert rendered.selected  # control: the preview really did render this memo
    assert not printready.printready_path(uids[0], printready.DEFAULT_PROFILE).exists()
