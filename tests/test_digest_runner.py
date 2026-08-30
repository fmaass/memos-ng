"""The run: the lock, the claim CAS, the per-part commit, and the sweeps (MEMNG-234).

These tests COMMIT, because everything they are about is a commit boundary. Nothing here is
mocked except the two external systems — SMTP (`FakeTransport`) and Dropbox (`FakeHost`). The
store, the runner, the splitter and the renderer are the real ones; substituting any of them would
prove nothing about the behaviour under test.

The load-bearing group is the fencing. Four properties are asserted against a real Postgres rather
than reasoned about:

* two concurrent runs insert ONE row and the loser sends nothing;
* a claim that loses its CAS aborts BEFORE SMTP, not after;
* the stale-run sweep is one atomic statement, so a runner that beats between a would-be SELECT
  and the UPDATE survives — a select-then-update would kill it while it was still sending;
* a swept run's still-alive task cannot advance the row it no longer owns.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestTrigger, User, Visibility
from app.services.digest import runner as digest_runner
from sqlalchemy import delete, text
from structlog.testing import capture_logs

from tests.digest_support import (
    FakeHost,
    FakeTransport,
    add_recipients,
    age_hosted_media,
    age_run,
    delivery_rows,
    host_factory_for,
    hosted_rows,
    html_part,
    reset_digest_state,
    run_rows,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_poster,
    write_thumb,
    write_web_mp4,
)

RECIPIENTS = ["a@example.com", "b@example.com", "c@example.com"]
SENDER = "sender@example.com"
MEMOS_HOST = "memos.discomarder.live"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 Europe/Zurich


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    """A configured, armed digest with three recipients and a clean set of digest tables."""
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients(*RECIPIENTS)
    set_digest_settings(enabled=True, start_date="2026-06-01", grace_minutes=30)
    subject = f"digest-run-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        # LIKE, not equality: a test that seeds a second user (`<subject>-b`) and then fails an
        # assertion would otherwise leak its memos into every later test in this file, and the
        # cascade of failures would hide the one real one.
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed_photo_memos(subject: str, data_root, count: int, *, thumb_bytes: int = 500):
    """`count` public memos, each with one photo whose thumb is on disk, one minute apart."""
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
            write_thumb(data_root, attachment.uid, b"T" * thumb_bytes)
            memo_ids.append(memo.id)
        await db.commit()
    return memo_ids


# --- a whole run, end to end -----------------------------------------------------------------


async def test_a_run_sends_one_part_and_registers_every_memo(env):
    subject, data_root = env
    memo_ids = await _seed_photo_memos(subject, data_root, 3)
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None
    assert result.status == digest_runner.SENT
    assert result.parts == 1 and result.parts_sent == 1 and result.memos_sent == 3
    assert len(transport.sent) == 1
    assert transport.sent[0].recipients == RECIPIENTS
    assert transport.sent[0].sender == SENDER

    rows = run_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == digest_runner.SENT
    assert rows[0]["trigger"] == DigestTrigger.scheduled
    assert rows[0]["parts_claimed"] == 1 and rows[0]["parts_sent"] == 1
    assert rows[0]["recipients"] == 3
    assert rows[0]["error"] is None
    assert rows[0]["finished_at"] is not None
    assert rows[0]["heartbeat_at"] is not None
    assert {row["memo_id"] for row in delivery_rows()} == set(memo_ids)
    assert {row["part_index"] for row in delivery_rows()} == {1}


async def test_a_single_part_subject_is_byte_identical_to_the_satellites(env):
    """The household has been receiving this exact line for months."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert transport.sent[0].message["Subject"] == "The Daily Dami Memos Mail — June 20, 2026"


async def test_a_multi_part_subject_names_the_part(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    set_digest_settings(max_email_bytes=3000)
    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert [mail.message["Subject"] for mail in transport.sent] == [
        "The Daily Dami Memos Mail — June 20, 2026 (Part 1 of 2)",
        "The Daily Dami Memos Mail — June 20, 2026 (Part 2 of 2)",
    ]



async def test_the_sent_mail_carries_no_link_back_to_memos_ng(env):
    """AC 4, end to end and against a fixture memo that CONTAINS one — the production corpus was
    measured on 2026-08-30 as 0 of 245 memos containing any http(s) URL, so an assertion over the
    corpus would pass with the stripping deleted."""
    subject, data_root = env
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        await seed_memo(
            db,
            user,
            content=f"read it [here](https://{MEMOS_HOST}/m/abc123)",
            visibility=Visibility.PUBLIC,
            created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        )
        await db.commit()
    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)
    body = transport.sent[0].message.as_string()
    assert MEMOS_HOST not in body
    assert "read it" in html_part(transport.sent[0].message)


async def test_nothing_to_send_writes_a_terminal_row_and_no_mail(env):
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.status == digest_runner.NOTHING_TO_SEND
    assert transport.sent == []
    assert run_rows()[0]["status"] == digest_runner.NOTHING_TO_SEND


async def test_everything_deferred_ends_the_run_deferred(env):
    subject, data_root = env
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db, user, visibility=Visibility.PUBLIC, created_at=datetime.now(UTC)
        )
        await seed_attachment(db, user, memo, mime="image/jpeg")  # no thumb on disk
        await db.commit()
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=datetime.now(UTC))
    assert result is not None and result.status == digest_runner.DEFERRED
    assert transport.sent == []
    assert delivery_rows() == []


# --- the per-part commit boundary ------------------------------------------------------------


async def test_each_part_commits_its_own_deliveries_with_its_part_index(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    set_digest_settings(max_email_bytes=3000)
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.parts == 2 and result.parts_sent == 2
    assert sorted(row["part_index"] for row in delivery_rows()) == [1, 2]


async def test_a_part_refused_by_ONE_recipient_still_commits(env):
    """The satellite raises on any non-empty `refused` dict. That rule is unbounded: one
    permanently refused address would leave the part uncommitted every morning and re-send it to
    the accepting recipients forever."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    set_digest_settings(max_email_bytes=3000)
    transport = FakeTransport(refusals_by_part={2: {"b@example.com": (550, b"no such user")}})
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None
    assert result.status == digest_runner.SENT
    assert result.parts_sent == 2
    assert len(delivery_rows()) == 2
    row = run_rows()[0]
    assert row["status"] == digest_runner.SENT
    assert row["error"] == "part 2: refused 1 recipient(s)"
    assert "b@example.com" not in (row["error"] or "")


async def test_a_part_NOBODY_accepted_is_a_failed_part(env):
    """AC 5 as amended: the earlier parts stay committed with their `part_index`, this part's do
    not, and the run reads `failed` with `parts_sent < parts`."""
    subject, data_root = env
    memo_ids = await _seed_photo_memos(subject, data_root, 2)
    set_digest_settings(max_email_bytes=3000)
    refusals = {address: (550, b"denied") for address in RECIPIENTS}
    transport = FakeTransport(refusals_by_part={2: refusals})
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None
    assert result.status == digest_runner.FAILED
    assert result.parts == 2 and result.parts_sent == 1
    committed = delivery_rows()
    assert len(committed) == 1 and committed[0]["part_index"] == 1
    assert committed[0]["memo_id"] == memo_ids[0]
    row = run_rows()[0]
    assert row["status"] == digest_runner.FAILED
    assert "refused by all 3" in (row["error"] or "")
    # The undelivered memo has no registry row, so tomorrow's run picks it up again.
    assert memo_ids[1] not in {r["memo_id"] for r in committed}


async def test_an_smtp_failure_on_a_part_leaves_that_part_unwritten(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    set_digest_settings(max_email_bytes=3000)
    transport = FakeTransport(raise_on_part=2)
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None
    assert result.status == digest_runner.FAILED and result.parts_sent == 1
    assert len(delivery_rows()) == 1
    assert "send failed" in (run_rows()[0]["error"] or "")


async def test_a_run_that_fails_its_first_part_writes_no_deliveries_at_all(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    transport = FakeTransport(raise_on_part=1)
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.status == digest_runner.FAILED
    assert result.parts_sent == 0
    assert delivery_rows() == []


# --- single-flight ---------------------------------------------------------------------------


async def test_two_concurrent_gate_passes_insert_exactly_one_run(env):
    """AC 6: the loser learns from the unique partial index, not from a pre-flight read — a
    check-then-insert is the TOCTOU race the index exists to close, and both callers would pass
    it."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    first, second = FakeTransport(delay=0.05), FakeTransport(delay=0.05)
    outcomes = await asyncio.gather(
        digest_runner.run_digest(transport=first, now_utc=NOW),
        digest_runner.run_digest(transport=second, now_utc=NOW),
        return_exceptions=True,
    )
    skipped = [o for o in outcomes if isinstance(o, digest_runner.RunSkipped)]
    completed = [o for o in outcomes if isinstance(o, digest_runner.RunResult)]
    assert len(skipped) == 1 and len(completed) == 1
    assert len(run_rows()) == 1
    assert len(first.sent) + len(second.sent) == 1  # the loser sent nothing


async def test_a_manual_run_conflicts_with_an_in_flight_scheduled_one(env):
    """The `UNIQUE ((1)) WHERE status IS NULL` index is what covers a manual run, which sits
    outside the scheduled-day ledger by design."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    async with async_session() as db:
        await digest_runner.open_run(
            db, trigger=DigestTrigger.scheduled, local_date=NOW.date()
        )
    transport = FakeTransport()
    with pytest.raises(digest_runner.RunSkipped):
        await digest_runner.run_digest(
            trigger=DigestTrigger.manual, transport=transport, now_utc=NOW
        )
    assert transport.sent == []
    assert len(run_rows()) == 1


async def test_two_concurrent_manual_runs_leave_one_in_flight_row(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    first, second = FakeTransport(delay=0.05), FakeTransport(delay=0.05)
    outcomes = await asyncio.gather(
        digest_runner.run_digest(
            trigger=DigestTrigger.manual, transport=first, now_utc=NOW
        ),
        digest_runner.run_digest(
            trigger=DigestTrigger.manual, transport=second, now_utc=NOW
        ),
        return_exceptions=True,
    )
    assert len([o for o in outcomes if isinstance(o, digest_runner.RunSkipped)]) == 1
    assert len(run_rows()) == 1
    assert len(first.sent) + len(second.sent) == 1


async def test_a_manual_run_is_possible_on_a_day_that_already_had_a_scheduled_one(env):
    """A manual run sits OUTSIDE the scheduled-day index, which is what makes "Run now" work
    after a failure."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    await digest_runner.run_digest(transport=FakeTransport(), now_utc=NOW)
    result = await digest_runner.run_digest(
        trigger=DigestTrigger.manual, transport=FakeTransport(), now_utc=NOW
    )
    assert result is not None
    assert [row["trigger"] for row in run_rows()] == ["scheduled", "manual"]


# --- the claim CAS ---------------------------------------------------------------------------


class _InterferingTransport(FakeTransport):
    """A transport that lets a test act BETWEEN parts, which is the only place the interleave the
    claim CAS defends against can be created from outside."""

    def __init__(self, after_part: int, hook, **kwargs) -> None:
        super().__init__(**kwargs)
        self._after_part = after_part
        self._hook = hook

    async def send(self, message, sender, recipients):
        refusals = await super().send(message, sender, recipients)
        if len(self.sent) == self._after_part:
            await self._hook()
        return refusals


async def _current_run_id() -> int:
    async with async_session() as db:
        found = await db.scalar(text("SELECT max(id) FROM digest_runs"))
    assert found is not None
    return int(found)


async def test_a_lost_claim_aborts_before_the_next_part_reaches_smtp(env):
    """AC 6's second case: once another actor has advanced `parts_claimed`, the runner's own claim
    updates 0 rows and it stops — with the fake transport recording no further send."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 3)
    set_digest_settings(max_email_bytes=3000)

    async def advance_parts_claimed():
        run_id = await _current_run_id()
        async with async_session() as db:
            await db.execute(
                text("UPDATE digest_runs SET parts_claimed = 99 WHERE id = :id"),
                {"id": run_id},
            )
            await db.commit()

    transport = _InterferingTransport(after_part=1, hook=advance_parts_claimed)
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is None  # aborted, and deliberately writes no terminal status
    assert len(transport.sent) == 1  # part 2 never reached SMTP
    assert len(delivery_rows()) == 1  # part 1's commit still landed
    assert run_rows()[0]["status"] is None  # the row belongs to whoever advanced it


async def test_a_swept_run_cannot_advance_or_send_again(env):
    """The stale sweep closes a run whose process looked dead; if that process is in fact ALIVE it
    must stop at its very next fenced write, before any further SMTP contact."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 3)
    set_digest_settings(max_email_bytes=3000)

    async def sweep_it():
        run_id = await _current_run_id()
        age_run(run_id, heartbeat_minutes=20, started_minutes=200)
        assert await digest_runner.sweep_stale_runs() == 1

    transport = _InterferingTransport(after_part=1, hook=sweep_it)
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is None
    assert len(transport.sent) == 1  # nothing was sent after the sweep
    assert delivery_rows() == []  # part 1's commit was refused too — the row is not ours
    row = run_rows()[0]
    assert row["status"] == digest_runner.FAILED
    assert row["error"] == digest_runner.ERROR_RUNNER_DIED


async def test_claim_part_refuses_once_the_status_is_terminal(env):
    """The predicate on its own, so the guard is visible without a whole run around it. The ORM
    validator cannot do this job: it guards one session's object, and the sweep commits from
    another connection entirely."""
    async with async_session() as db:
        run_id = await digest_runner.open_run(
            db, trigger=DigestTrigger.manual, local_date=NOW.date()
        )
        assert await digest_runner.claim_part(db, run_id, 1) is True
        await db.execute(
            text("UPDATE digest_runs SET status = 'failed' WHERE id = :id"), {"id": run_id}
        )
        await db.commit()
        assert await digest_runner.claim_part(db, run_id, 2) is False
        assert await digest_runner.heartbeat(db, run_id) is False
        assert await digest_runner.record_progress(db, run_id, parts=9, recipients=9) is False


async def test_the_terminal_write_never_overwrites_a_terminal_status(env):
    """`memosng_digest_runs_total{status}` is a Counter, and a Counter may not move backwards: a
    second terminal write would have to decrement a label."""
    async with async_session() as db:
        run_id = await digest_runner.open_run(
            db, trigger=DigestTrigger.manual, local_date=NOW.date()
        )
        assert await digest_runner.finish_run(db, run_id, status=digest_runner.FAILED) is True
        assert await digest_runner.finish_run(db, run_id, status=digest_runner.SENT) is False
    assert run_rows()[0]["status"] == digest_runner.FAILED


async def test_a_claim_out_of_order_is_refused(env):
    async with async_session() as db:
        run_id = await digest_runner.open_run(
            db, trigger=DigestTrigger.manual, local_date=NOW.date()
        )
        assert await digest_runner.claim_part(db, run_id, 2) is False  # part 1 was never claimed
        assert await digest_runner.claim_part(db, run_id, 1) is True


# --- the stale-run sweep ---------------------------------------------------------------------


async def _open(trigger: str = DigestTrigger.manual) -> int:
    async with async_session() as db:
        return await digest_runner.open_run(db, trigger=trigger, local_date=NOW.date())


async def test_a_run_silent_past_the_bar_is_marked_failed_exactly_once(env):
    run_id = await _open()
    age_run(run_id, heartbeat_minutes=20, started_minutes=25)
    assert await digest_runner.sweep_stale_runs() == 1
    row = run_rows()[0]
    assert row["status"] == digest_runner.FAILED
    assert row["error"] == digest_runner.ERROR_RUNNER_DIED
    assert row["finished_at"] is not None
    assert await digest_runner.sweep_stale_runs() == 0  # already terminal, left alone
    assert run_rows()[0]["error"] == digest_runner.ERROR_RUNNER_DIED


async def test_a_long_run_that_is_still_beating_is_NEVER_swept(env):
    """The case that would otherwise break single-flight: a legitimate run can exceed two hours
    (many memos, several videos each, Dropbox upload sessions with a 600 s per-operation timeout).
    Sweeping it would drop it out of `uq_digest_runs_single_inflight` and let a SECOND run mail the
    same memos beside it."""
    run_id = await _open()
    age_run(run_id, heartbeat_minutes=1, started_minutes=180)
    assert await digest_runner.sweep_stale_runs() == 0
    assert run_rows()[0]["status"] is None


async def test_a_run_that_never_beat_is_judged_on_started_at(env):
    run_id = await _open()
    age_run(run_id, heartbeat_minutes=None, started_minutes=20)
    assert await digest_runner.sweep_stale_runs() == 1
    assert run_rows()[0]["status"] == digest_runner.FAILED


async def test_a_young_run_that_never_beat_is_left_alone(env):
    run_id = await _open()
    age_run(run_id, heartbeat_minutes=None, started_minutes=2)
    assert await digest_runner.sweep_stale_runs() == 0
    assert run_rows()[0]["status"] is None


async def test_a_runner_that_beats_between_a_would_be_select_and_the_update_survives(env):
    """The interleave the ONE-STATEMENT sweep exists for.

    The heartbeat takes the row lock and holds it; the sweep's UPDATE blocks on that lock rather
    than proceeding on the snapshot it started with. When the heartbeat commits, READ COMMITTED
    makes the UPDATE re-evaluate its WHERE against the new row version — and the run survives. A
    select-then-update would have decided on the stale read and killed a live run mid-send.
    """
    run_id = await _open()
    age_run(run_id, heartbeat_minutes=30, started_minutes=40)
    async with async_session() as beater:
        await beater.execute(
            text("UPDATE digest_runs SET heartbeat_at = now() WHERE id = :id"), {"id": run_id}
        )  # transaction OPEN: the row lock is held
        sweeping = asyncio.create_task(digest_runner.sweep_stale_runs())
        await asyncio.sleep(0.3)
        assert not sweeping.done(), "the sweep did not contend for the row lock"
        await beater.commit()
        assert await sweeping == 0
    assert run_rows()[0]["status"] is None


async def test_the_sweep_only_touches_runs_that_are_still_in_flight(env):
    stale = await _open()
    age_run(stale, heartbeat_minutes=20, started_minutes=25)
    async with async_session() as db:
        await db.execute(
            text("UPDATE digest_runs SET status = 'sent', finished_at = now() WHERE id = :id"),
            {"id": stale},
        )
        await db.commit()
    assert await digest_runner.sweep_stale_runs() == 0
    assert run_rows()[0]["status"] == "sent"


# --- the guards ------------------------------------------------------------------------------


async def test_smtp_unconfigured_writes_a_failed_row_and_sends_nothing(env, monkeypatch):
    """LSN-0022: never claim work you structurally cannot complete. It writes a ROW rather than
    staying silent, so the failure is visible in the Runs table, in the metrics and to the alert —
    which is the silent-failure class this whole feature exists to end."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "")
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.status == digest_runner.FAILED
    assert result.error == digest_runner.ERROR_SMTP_UNCONFIGURED
    assert transport.sent == []
    assert run_rows()[0]["error"] == digest_runner.ERROR_SMTP_UNCONFIGURED


async def test_zero_enabled_recipients_writes_a_failed_row(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    add_recipients(*RECIPIENTS, enabled=False)
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.error == digest_runner.ERROR_NO_RECIPIENTS
    assert transport.sent == []


async def test_the_sender_being_a_recipient_refuses_the_run(env):
    """AC 7, strengthened past the satellite, which only WARNED and sent anyway: iCloud drops
    `From == To` with attachments, so a warned-through run reports success while delivering
    nothing."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    add_recipients(SENDER)
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.error == digest_runner.ERROR_SENDER_IS_RECIPIENT
    assert transport.sent == []
    assert run_rows()[0]["status"] == digest_runner.FAILED


async def test_a_selection_over_the_cap_fails_the_run_before_any_render(env):
    """A wrong scope or start_date must not mail the whole corpus to six external recipients in one
    morning."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 4)
    set_digest_settings(max_memos_per_run=3)
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.status == digest_runner.FAILED
    assert result.error == "selection 4 exceeds max_memos_per_run 3"
    assert transport.sent == []
    assert delivery_rows() == []


# --- hosting inside a run ---------------------------------------------------------------------


async def _seed_video_memo(subject: str, data_root):
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db,
            user,
            content="a clip",
            visibility=Visibility.PUBLIC,
            created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        )
        attachment = await seed_attachment(
            db, user, memo, mime="video/mp4", filename="clip.mp4"
        )
        await db.commit()
        write_web_mp4(data_root, attachment.uid, b"MP4" * 10)
        write_poster(data_root, attachment.uid, b"POSTER")
        return memo.id, attachment.id


async def test_a_hosted_video_is_recorded_and_never_attached(env):
    """Decision 4 end to end: poster + play badge + Dropbox link, and no `video/*` part anywhere in
    the built message."""
    subject, data_root = env
    _, attachment_id = await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True)
    host = FakeHost()
    transport = FakeTransport()
    result = await digest_runner.run_digest(
        transport=transport, host_factory=host_factory_for(host), now_utc=NOW
    )
    assert result is not None and result.status == digest_runner.SENT
    assert len(host.uploads) == 1
    rows = hosted_rows()
    assert len(rows) == 1 and rows[0]["attachment_id"] == attachment_id
    assert rows[0]["kind"] == "video"
    message = transport.sent[0].message
    assert not any(
        part.get_content_type().startswith("video/") for part in message.walk()
    )
    body = html_part(message)
    assert "dropbox.example" in body
    assert "cid:poster-" in body  # the poster and its play badge


async def test_hosting_switched_off_logs_nothing_about_it(env):
    """Not an incident: an admin who turned hosting off does not need a warning every morning.
    The line above is for hosting that was ASKED for and could not be had."""
    from structlog.testing import capture_logs as _capture

    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=False)
    with _capture() as logs:
        await digest_runner.run_digest(
            transport=FakeTransport(), host_factory=host_factory_for(None), now_utc=NOW
        )
    assert not [entry for entry in logs if entry["event"] == "digest.hosting_unavailable"]


async def test_hosting_unavailable_produces_an_inline_only_mail_and_a_log_line(env):
    """AC 8. `host_factory` returning None is what "hosting unconfigured or token refresh failing"
    reduces to — all three causes reach the same path."""
    from structlog.testing import capture_logs

    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True)
    transport = FakeTransport()
    with capture_logs() as logs:
        result = await digest_runner.run_digest(
            transport=transport, host_factory=host_factory_for(None), now_utc=NOW
        )
    assert result is not None and result.status == digest_runner.SENT
    assert hosted_rows() == []
    body = html_part(transport.sent[0].message)
    assert "video hosting not configured" in body
    assert "cid:poster-" not in body
    # The LINE, not a line: "logged, never fatal" is the contract, and asserting an unrelated
    # event (`digest.run_finished`, which every run emits) would make this test unfalsifiable.
    hosting_events = [
        entry for entry in logs if entry["event"] == "digest.hosting_unavailable"
    ]
    assert len(hosting_events) == 1, [entry["event"] for entry in logs]
    assert hosting_events[0]["run_id"] == result.run_id
    assert hosting_events[0]["log_level"] == "warning"


# --- the retention sweep -----------------------------------------------------------------------


async def test_the_retention_sweep_deletes_and_stamps(env):
    subject, data_root = env
    _, attachment_id = await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True, retention_days=30)
    host = FakeHost()
    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
    )
    dropbox_path = hosted_rows()[0]["dropbox_path"]

    assert await digest_runner.sweep_hosted_media_safe(
        host_factory=host_factory_for(host)
    ) == 0  # nothing is old enough yet
    age_hosted_media(attachment_id, days=31)
    assert await digest_runner.sweep_hosted_media_safe(
        host_factory=host_factory_for(host)
    ) == 1
    assert host.deletes == [dropbox_path]
    row = hosted_rows()[0]
    assert row["swept_at"] is not None
    # The ROW is kept: a later run must be able to tell "never hosted" from "hosted and swept".
    assert row["shared_link"]


async def test_a_swept_row_is_not_swept_again(env):
    subject, data_root = env
    _, attachment_id = await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True, retention_days=30)
    host = FakeHost()
    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
    )
    age_hosted_media(attachment_id, days=31)
    assert await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 1
    assert await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 0
    assert len(host.deletes) == 1


async def test_the_retention_sweep_does_nothing_without_a_host(env):
    subject, data_root = env
    _, attachment_id = await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True, retention_days=30)
    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(FakeHost()), now_utc=NOW
    )
    age_hosted_media(attachment_id, days=31)
    assert await digest_runner.sweep_hosted_media_safe(
        host_factory=host_factory_for(None)
    ) == 0
    assert hosted_rows()[0]["swept_at"] is None  # never stamped for a file still on Dropbox


# --- preview and test-send ---------------------------------------------------------------------


async def test_preview_persists_nothing(env):
    subject, data_root = env
    memo_ids = await _seed_photo_memos(subject, data_root, 2)
    async with async_session() as db:
        rendered, settings_row = await digest_runner.preview(db, now_utc=NOW)
    assert [c.memo_id for c in rendered.selected] == memo_ids
    assert len(rendered.parts) == 1
    assert run_rows() == []
    assert delivery_rows() == []
    assert hosted_rows() == []
    assert settings_row.mail_title == "The Daily Dami Memos Mail"


async def test_a_test_send_reaches_only_the_given_address_and_hosts_nothing(env):
    """The probe contract (A6): with several recipients configured and an unhosted video present,
    a test-send goes to ONE address, writes no `digest_hosted_media` row and makes no Dropbox
    call."""
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True)
    host = FakeHost()
    transport = FakeTransport()
    async with async_session() as db:
        count = await digest_runner.test_send(
            db, address="admin@x", transport=transport, now_utc=NOW
        )
    assert count == 1
    assert [mail.recipients for mail in transport.sent] == [["admin@x"]]
    for address in RECIPIENTS:
        assert address not in transport.sent[0].message.as_string()
    assert host.uploads == []
    assert hosted_rows() == []
    assert run_rows() == []
    assert delivery_rows() == []


async def test_a_test_send_uses_an_ALREADY_hosted_link_without_uploading(env):
    subject, data_root = env
    _, attachment_id = await _seed_video_memo(subject, data_root)
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO digest_hosted_media "
                "(attachment_id, kind, dropbox_path, shared_link, bytes) VALUES "
                "(:id, 'video', '/x/a.mp4', 'https://dropbox.example/a.mp4?raw=1', 10)"
            ),
            {"id": attachment_id},
        )
        await db.commit()
    transport = FakeTransport()
    async with async_session() as db:
        await digest_runner.test_send(
            db, address="admin@x", transport=transport, now_utc=NOW
        )
    assert "https://dropbox.example/a.mp4?raw=1" in html_part(transport.sent[0].message)
    assert len(hosted_rows()) == 1  # unchanged: nothing new was uploaded


async def test_a_test_send_always_sends_even_with_an_empty_selection(env):
    transport = FakeTransport()
    async with async_session() as db:
        count = await digest_runner.test_send(
            db, address="admin@x", transport=transport, now_utc=NOW
        )
    assert count == 0
    assert len(transport.sent) == 1
    assert "No memos selected." in html_part(transport.sent[0].message)


async def test_the_empty_flag_never_consults_the_selection(env):
    """The Phase 4 SMTP probe: a body with no settings write anywhere in it, and no dependency on
    what happens to be selectable that morning."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 3)
    transport = FakeTransport()
    async with async_session() as db:
        count = await digest_runner.test_send(
            db, address="admin@x", empty=True, transport=transport, now_utc=NOW
        )
    assert count == 0
    assert "No memos selected." in html_part(transport.sent[0].message)
    assert delivery_rows() == []


async def test_preview_and_test_send_honour_max_memos_per_run(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 4)
    set_digest_settings(max_memos_per_run=2)
    async with async_session() as db:
        rendered, _ = await digest_runner.preview(db, now_utc=NOW)
    assert len(rendered.selected) == 2


# --- the supervised worker entry point ----------------------------------------------------------


async def test_the_safe_entry_point_swallows_a_skip(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    async with async_session() as db:
        await digest_runner.open_run(
            db, trigger=DigestTrigger.manual, local_date=NOW.date()
        )
    assert await digest_runner.run_scheduled_digest_safe(now_utc=NOW) is None
    assert len(run_rows()) == 1


async def test_the_safe_entry_point_swallows_an_error(env, monkeypatch):
    """A digest failure must never be able to halt media processing — the same boundary guard
    `run_frame_share_drain` and `run_media_recover_safe` already apply."""

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr(digest_runner, "run_digest", boom)
    assert await digest_runner.run_scheduled_digest_safe(now_utc=NOW) is None


async def test_the_local_date_on_the_row_is_the_LOCAL_one(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    late = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)  # 00:30 on 2 July in Zurich
    await digest_runner.run_digest(transport=FakeTransport(), now_utc=late)
    assert run_rows()[0]["local_date"] == (late + timedelta(hours=2)).date()


# --- the day ledger, revalidated inside the scheduled INSERT (review B1) ----------------------


async def test_a_manual_run_today_blocks_todays_scheduled_run(env):
    """`uq_digest_runs_scheduled_local_date` covers `trigger = 'scheduled'` ONLY, so it cannot see
    a manual run — and the worker's gate read happens BEFORE the insert. A manual run that
    completes in that window would otherwise let a second digest go out the same day, to the same
    six mailboxes, carrying whatever the manual run did not reach.
    """
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    manual = FakeTransport()
    manual_result = await digest_runner.run_digest(
        trigger=DigestTrigger.manual, transport=manual, now_utc=NOW
    )
    assert manual_result is not None and manual_result.status == digest_runner.SENT

    scheduled = FakeTransport()
    with pytest.raises(digest_runner.RunSkipped):
        await digest_runner.run_digest(transport=scheduled, now_utc=NOW)
    assert scheduled.sent == []
    assert [row["trigger"] for row in run_rows()] == [DigestTrigger.manual]


async def test_a_scheduled_run_today_blocks_a_second_scheduled_one(env):
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    await digest_runner.run_digest(transport=FakeTransport(), now_utc=NOW)
    second = FakeTransport()
    with pytest.raises(digest_runner.RunSkipped):
        await digest_runner.run_digest(transport=second, now_utc=NOW)
    assert second.sent == []
    assert len(run_rows()) == 1


async def test_yesterdays_runs_do_not_block_today(env):
    """The predicate is the DAY's ledger, not "has anything ever run"."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 2)
    yesterday = NOW - timedelta(days=1)
    await digest_runner.run_digest(
        trigger=DigestTrigger.manual, transport=FakeTransport(), now_utc=yesterday
    )
    # Yesterday's manual run mailed both memos, so today has nothing left to send — what matters
    # is that today's run was ALLOWED to open at all, which is the property the ledger predicate
    # must not over-reach on.
    result = await digest_runner.run_digest(transport=FakeTransport(), now_utc=NOW)
    assert result is not None and result.status == digest_runner.NOTHING_TO_SEND
    assert sorted(row["trigger"] for row in run_rows()) == [
        DigestTrigger.manual,
        DigestTrigger.scheduled,
    ]


async def test_a_test_or_import_run_today_does_not_block_the_scheduled_one(env):
    """B7 again, now in the INSERT: a probe or the one-shot import must not swallow the day's real
    send, so the ledger predicate names `scheduled` and `manual` only."""
    subject, data_root = env
    await _seed_photo_memos(subject, data_root, 1)
    async with async_session() as db:
        for trigger in (DigestTrigger.test, DigestTrigger.import_):
            run_id = await digest_runner.open_run(
                db, trigger=trigger, local_date=NOW.date()
            )
            await digest_runner.finish_run(db, run_id, status=digest_runner.SENT)
    transport = FakeTransport()
    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)
    assert result is not None and result.status == digest_runner.SENT
    assert len(transport.sent) == 1


# --- the isolated stale sweep (review B2) ------------------------------------------------------


async def test_the_stale_sweep_isolates_a_database_error(env, monkeypatch):
    """An exception escaping the sweep propagates out of `worker.run()` and stops the worker —
    heartbeat, media drain and frame drain with it. The optional digest feature must never be able
    to take the media pipeline down."""

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(digest_runner, "sweep_stale_runs", boom)
    with capture_logs() as logs:
        assert await digest_runner.sweep_stale_runs_safe() == 0
    assert any(entry["event"] == "digest.stale_sweep_error" for entry in logs)


async def test_the_isolated_sweep_still_sweeps(env):
    """A positive control on the wrapper: it must not have become a no-op."""
    run_id = await _open()
    age_run(run_id, heartbeat_minutes=20, started_minutes=25)
    assert await digest_runner.sweep_stale_runs_safe() == 1
    assert run_rows()[0]["status"] == digest_runner.FAILED


# --- test-send honours the sender guard (review B3) --------------------------------------------


async def test_a_test_send_to_the_sender_address_is_refused(env):
    """iCloud DROPS From == To with attachments, so this probe would report a clean send and
    deliver nothing — a probe that lies is worse than no probe."""
    transport = FakeTransport()
    async with async_session() as db:
        with pytest.raises(digest_runner.SenderIsRecipient):
            await digest_runner.test_send(
                db, address=SENDER.upper(), transport=transport, now_utc=NOW
            )
    assert transport.sent == []


async def test_a_test_send_to_the_sender_address_is_refused_even_when_empty(env):
    """The `empty=True` probe takes an earlier branch; the guard must sit in front of both."""
    transport = FakeTransport()
    async with async_session() as db:
        with pytest.raises(digest_runner.SenderIsRecipient):
            await digest_runner.test_send(
                db, address=SENDER, empty=True, transport=transport, now_utc=NOW
            )
    assert transport.sent == []


# --- the Dropbox client is released on every path (review A1) -----------------------------------


async def test_the_media_host_is_closed_after_a_run(env):
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True)
    host = FakeHost()
    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
    )
    assert host.closed == 1


async def test_the_media_host_is_closed_even_when_the_run_raises(env, monkeypatch):
    """The leak this closes is once per DAY in a process that runs for weeks, and it only bites
    while something else is already going wrong — which is when it is hardest to see."""
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True)
    host = FakeHost()

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(digest_runner, "build_digest", boom)
    with pytest.raises(RuntimeError):
        await digest_runner.run_digest(
            transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
        )
    assert host.closed == 1


async def test_the_retention_sweep_closes_its_host(env):
    subject, data_root = env
    _, attachment_id = await _seed_video_memo(subject, data_root)
    set_digest_settings(hosting_enabled=True, retention_days=30)
    host = FakeHost()
    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
    )
    age_hosted_media(attachment_id, days=31)
    closed_before = host.closed
    assert await digest_runner.sweep_hosted_media_safe(
        host_factory=host_factory_for(host)
    ) == 1
    assert host.closed == closed_before + 1
