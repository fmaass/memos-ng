"""The hosted-media ownership ledger, against a real Postgres.

`digest_hosted_media` is not a cache of links keyed by attachment; it is the ONLY record that a
file exists on Dropbox. Everything here follows from that, and each property below is the answer
to a way the previous shape lost a file for good:

* the uploads used to be accumulated in memory and written in ONE batch after the whole hosting
  pass, so a crash after Dropbox accepted a file left an object no row named and no sweep could
  ever find;
* the row used to CASCADE with its attachment, so ordinary attachment reclamation deleted the sole
  locator of a file that was still there.

The two states a crash can now leave are both resolvable, and the sweep resolves them by asking
Dropbox rather than by guessing. The tests also pin the boundary rule that makes the whole thing
affordable: no database transaction is open while bytes are on the wire.
"""

import secrets
from datetime import UTC, datetime

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestTrigger, User, Visibility
from app.services.digest import runner as digest_runner
from app.services.reclaim import Revalidate, reclaim_attachment
from sqlalchemy import create_engine, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import SYNC_URL
from tests.digest_support import (
    FakeHost,
    FakeTransport,
    TrackingSessionFactory,
    add_recipients,
    age_hosted_claim,
    age_run,
    host_factory_for,
    hosted_rows,
    insert_hosted_row,
    insert_run,
    reset_digest_state,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_poster,
    write_thumb,
    write_web_mp4,
)

SENDER = "sender@example.com"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 Europe/Zurich


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients("a@example.com")
    set_digest_settings(
        enabled=True, start_date="2026-06-01", grace_minutes=30, hosting_enabled=True
    )
    subject = f"digest-ledger-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed_video_memo(subject: str, data_root, *, videos: int = 1) -> list[int]:
    """One public memo carrying `videos` transcoded clips, ready to host."""
    attachment_ids: list[int] = []
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo = await seed_memo(
            db, user, content="clip", visibility=Visibility.PUBLIC, created_at=NOW
        )
        for index in range(videos):
            clip = await seed_attachment(
                db, user, memo, mime="video/mp4", filename=f"clip{index}.mp4"
            )
            write_web_mp4(data_root, clip.uid)
            write_poster(data_root, clip.uid)
            write_thumb(data_root, clip.uid)
            attachment_ids.append(clip.id)
        await db.commit()
    return attachment_ids


def _rows_now() -> list[dict]:
    """The ledger read through an engine of its own — what another process would see."""
    return hosted_rows()


# --- the intention is committed before the bytes ------------------------------------------------


async def test_the_row_exists_as_pending_while_the_upload_is_still_running(env):
    """The property the whole reshape is for, observed from OUTSIDE the uploading transaction.

    The host reads the ledger through its own connection at the moment it is asked for bytes. If
    the row were written after the upload — or after the whole pass — this read would find nothing,
    which is precisely the window in which a crash used to strand a file for ever.
    """
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    observed: list[tuple[str, str | None]] = []

    class _ObservingHost(FakeHost):
        async def upload(self, local_path, dropbox_path):
            engine = create_engine(SYNC_URL)
            with engine.begin() as conn:
                row = conn.execute(
                    text(
                        "SELECT status, shared_link FROM digest_hosted_media "
                        "WHERE dropbox_path = :path"
                    ),
                    {"path": dropbox_path},
                ).one_or_none()
            engine.dispose()
            observed.append(row if row is None else (row[0], row[1]))
            return await super().upload(local_path, dropbox_path)

    host = _ObservingHost()
    result = await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
    )
    assert result is not None and result.status == digest_runner.SENT
    # Committed, visible to another connection, and promising nothing yet.
    assert observed == [("pending", None)]
    row = _rows_now()[0]
    assert row["status"] == "uploaded"
    assert row["shared_link"] and row["uploaded_at"] is not None


async def test_an_upload_that_succeeded_is_recorded_before_the_pass_ends(env):
    """Interruption after ONE upload, before the hosting pass finishes.

    The second file's upload dies with an error the pipeline does not catch, which is what a killed
    process looks like from the ledger's point of view. The first file must already be `uploaded`:
    under the batch-at-the-end shape it would have been recorded nowhere at all.
    """
    subject, data_root = env
    set_digest_settings(max_videos_per_memo=2)
    await _seed_video_memo(subject, data_root, videos=2)

    class _DiesOnSecond(FakeHost):
        async def upload(self, local_path, dropbox_path):
            if len(self.uploads) == 1:
                raise RuntimeError("simulated process death mid-pass")
            return await super().upload(local_path, dropbox_path)

    host = _DiesOnSecond()
    with pytest.raises(RuntimeError):
        await digest_runner.run_digest(
            transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
        )
    rows = _rows_now()
    assert [row["status"] for row in rows] == ["uploaded", "pending"]
    assert rows[0]["shared_link"] and rows[0]["uploaded_at"] is not None
    # The second row NAMES the path the interrupted upload was writing to, which is the whole
    # point: whatever happened to that object, something can still find it.
    assert rows[1]["dropbox_path"].endswith(".mp4")
    assert rows[1]["shared_link"] is None


async def test_a_refused_upload_leaves_a_pending_row_rather_than_nothing(env):
    """A Dropbox error degrades one card and the run continues — but the intention stays on the
    books, because the request may still have landed on the far side."""
    subject, data_root = env
    await _seed_video_memo(subject, data_root)

    class _AlwaysRefuses(FakeHost):
        async def upload(self, local_path, dropbox_path):
            from app.services.digest.hosting import DropboxError

            raise DropboxError("simulated 503")

    result = await digest_runner.run_digest(
        transport=FakeTransport(),
        host_factory=host_factory_for(_AlwaysRefuses()),
        now_utc=NOW,
    )
    assert result is not None and result.status == digest_runner.SENT
    assert [row["status"] for row in _rows_now()] == ["pending"]


# --- the row outlives the attachment ------------------------------------------------------------


async def test_the_ledger_row_survives_the_attachment_and_is_still_swept(env):
    """Driven through the REAL reclaim path, which is where the cascade used to bite.

    `reclaim_attachment` deletes the attachment row; the FK from the ledger is now
    `ON DELETE SET NULL`, never CASCADE. A cascade here deletes the sole locator of a file that is
    still on Dropbox — the retention sweep discovers remote objects only through these rows, so
    the object would be unowned for ever. The tombstone survives with a NULL reference, and the
    sweep still deletes exactly what it names.
    """
    subject, data_root = env
    (attachment_id,) = await _seed_video_memo(subject, data_root)
    host = FakeHost()
    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), now_utc=NOW
    )
    dropbox_path = _rows_now()[0]["dropbox_path"]

    async with async_session() as db:
        result = await reclaim_attachment(
            db,
            attachment_id,
            Revalidate.IDENTITY,
            unlink=lambda _paths: [],  # the files are the tmp_path fixture's, not the point here
        )
    assert result.outcome == "reclaimed"

    row = _rows_now()[0]
    assert row["attachment_id"] is None  # the reference went; the row did not
    assert row["status"] == "uploaded"
    assert row["dropbox_path"] == dropbox_path

    # Aged through the ledger id, because there is no attachment left to age it by — which is
    # exactly the state under test.
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE digest_hosted_media SET uploaded_at = now() - interval '31 days'")
        )
    engine.dispose()
    swept_host = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(swept_host))
        == 1
    )
    assert swept_host.deletes == [dropbox_path]
    assert _rows_now()[0]["status"] == "swept"


# --- reconciling a stuck pending row ------------------------------------------------------------


async def test_a_stale_pending_row_whose_object_landed_is_recorded_as_uploaded(env):
    """The crash happened between the bytes arriving and the success being written. Dropbox is the
    only authority on that, so the sweep asks it — and the object becomes owned and link-bearing
    rather than being deleted on a guess."""
    ledger_id = insert_hosted_row(
        kind="video", dropbox_path="/memos-mail/run/orphan.mp4", status="pending"
    )
    age_hosted_claim(ledger_id, minutes=90)
    host = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 0
    )
    assert host.probes == ["/memos-mail/run/orphan.mp4"]
    assert host.deletes == []
    row = _rows_now()[0]
    assert row["status"] == "uploaded"
    assert row["shared_link"] == "https://dropbox.example/orphan.mp4?raw=1"
    assert row["uploaded_at"] is not None


async def test_a_stale_pending_row_whose_object_never_landed_is_swept(env):
    """Absent means the upload never happened, so there is nothing to delete — the row is closed
    rather than left to be probed every hour for ever."""
    ledger_id = insert_hosted_row(
        kind="video", dropbox_path="/memos-mail/run/never.mp4", status="pending"
    )
    age_hosted_claim(ledger_id, minutes=90)
    host = FakeHost(absent={"/memos-mail/run/never.mp4"})
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 1
    )
    assert host.deletes == []  # nothing to delete: it was never there
    row = _rows_now()[0]
    assert row["status"] == "swept"
    assert row["swept_at"] is not None


async def test_a_fresh_pending_row_is_left_alone(env):
    """A live upload is a pending row too. Reconciling one out from under a running upload session
    (600 s per operation is legitimate) would be the sweep inventing the very orphan it exists to
    prevent, so the threshold sits well above the longest single operation."""
    insert_hosted_row(
        kind="video", dropbox_path="/memos-mail/run/inflight.mp4", status="pending"
    )
    host = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 0
    )
    assert host.probes == []
    assert _rows_now()[0]["status"] == "pending"


# --- the transitions are write-once -------------------------------------------------------------


async def test_an_uploaded_row_is_not_talked_back_into_pending_or_marked_twice(env):
    """Both transitions are a CAS on the previous status, in the statement.

    Re-claiming the same path must not blank the link of a row that already promises a live
    object, and marking a row twice must not rewrite what the first mark recorded.
    """
    ledger = digest_runner.SessionLedger(async_session)
    ledger_id = await ledger.claim(
        attachment_id=None, kind="video", dropbox_path="/memos-mail/run/once.mp4"
    )
    await ledger.mark_uploaded(ledger_id, shared_link="https://first", size=11)

    again = await ledger.claim(
        attachment_id=None, kind="video", dropbox_path="/memos-mail/run/once.mp4"
    )
    assert again == ledger_id  # the same row, still the claim on that path
    await ledger.mark_uploaded(ledger_id, shared_link="https://second", size=22)

    row = _rows_now()[0]
    assert row["status"] == "uploaded"
    assert row["shared_link"] == "https://first"
    assert row["bytes"] == 11


async def test_a_pending_row_is_re_opened_by_a_later_claim_on_the_same_path(env):
    """The other side of the same rule: a previous attempt that died mid-upload left the path
    claimed but unresolved, and the attempt that takes it over restarts its clock rather than
    inheriting an age that would send it straight to reconciliation."""
    ledger = digest_runner.SessionLedger(async_session)
    ledger_id = await ledger.claim(
        attachment_id=None, kind="video", dropbox_path="/memos-mail/run/retry.mp4"
    )
    age_hosted_claim(ledger_id, minutes=90)
    again = await ledger.claim(
        attachment_id=None, kind="video", dropbox_path="/memos-mail/run/retry.mp4"
    )
    assert again == ledger_id
    host = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 0
    )
    assert host.probes == []  # the re-claim reset the clock; nothing is stale


async def test_a_swept_row_is_never_re_opened_by_a_later_claim(env):
    """The lifecycle is write-once FORWARD: `pending → uploaded → swept`, and only a `pending` row
    may be retried.

    A `swept` row is the record that a file was DELETED from Dropbox. Re-opening it would erase the
    only evidence that the deletion happened — the retention history stops being auditable, and a
    row that says "pending" for an object nobody is uploading is exactly the state the
    reconciliation sweep then has to go and probe about.
    """
    ledger = digest_runner.SessionLedger(async_session)
    ledger_id = await ledger.claim(
        attachment_id=None, kind="video", dropbox_path="/memos-mail/run/gone.mp4"
    )
    await ledger.mark_uploaded(ledger_id, shared_link="https://gone", size=7)
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE digest_hosted_media SET status = 'swept', swept_at = now() "
                "WHERE id = :id"
            ),
            {"id": ledger_id},
        )
    engine.dispose()

    again = await ledger.claim(
        attachment_id=None, kind="video", dropbox_path="/memos-mail/run/gone.mp4"
    )
    assert again == ledger_id  # the same row: it is still the claim on that path
    row = _rows_now()[0]
    assert row["status"] == "swept"
    assert row["swept_at"] is not None
    assert row["shared_link"] == "https://gone"


# --- a pending row is judged by its owning run, not by the clock --------------------------------


async def test_a_pending_row_under_a_live_run_is_never_reconciled(env):
    """The failure this closes: an upload that legitimately takes longer than the settling floor.

    One upload-session operation allows 600 s and a whole run can last hours. An age-only rule
    would probe this row mid-upload, find nothing there yet, stamp it `swept` — and the upload's
    own `mark_uploaded` CAS would then find no `pending` row to advance and decline. The bytes
    would land on Dropbox with the ledger claiming they had been deleted: a permanent orphan,
    created by the sweep that exists to prevent them.
    """
    run_id = insert_run(status=None)  # in flight, and beating as of now
    ledger_id = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/run/slow.mp4",
        status="pending",
        run_id=run_id,
    )
    age_hosted_claim(ledger_id, minutes=180)  # far past the settling floor
    host = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 0
    )
    assert host.probes == []  # not even asked about
    assert _rows_now()[0]["status"] == "pending"


async def test_a_pending_row_under_a_dead_run_is_reconciled(env):
    """The same row, with its owner silent past the stale bar the run sweep uses. Nobody is
    uploading it any more, so the remote is now the only authority on what happened."""
    run_id = insert_run(status=None)
    age_run(run_id, heartbeat_minutes=digest_runner.STALE_RUN_MINUTES + 5, started_minutes=200)
    ledger_id = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/run/dead.mp4",
        status="pending",
        run_id=run_id,
    )
    age_hosted_claim(ledger_id, minutes=180)
    host = FakeHost(absent={"/memos-mail/run/dead.mp4"})
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 1
    )
    assert host.probes == ["/memos-mail/run/dead.mp4"]
    assert _rows_now()[0]["status"] == "swept"


async def test_a_pending_row_under_a_finished_run_is_reconciled(env):
    """A run that reached a terminal status is not going to upload anything else, whatever its
    heartbeat says — so a row it left `pending` is genuinely stuck rather than in progress."""
    run_id = insert_run(status="sent")
    ledger_id = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/run/left.mp4",
        status="pending",
        run_id=run_id,
    )
    age_hosted_claim(ledger_id, minutes=180)
    host = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host)) == 0
    )
    assert host.probes == ["/memos-mail/run/left.mp4"]
    assert _rows_now()[0]["status"] == "uploaded"  # the object was there after all


async def test_a_probe_that_cannot_answer_leaves_the_row_exactly_as_it_was(env):
    """"Unknown" is not "absent". A 409 with some other tag, a 5xx or a timeout says nothing about
    whether the object exists, and resolving the row on that guess is how a live file stops being
    findable. The row is left for the next pass, and the failure is logged once."""
    from structlog.testing import capture_logs

    run_id = insert_run(status="sent")
    ledger_id = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/run/unknown.mp4",
        status="pending",
        run_id=run_id,
    )
    age_hosted_claim(ledger_id, minutes=180)

    class _UnknownProbe(FakeHost):
        async def remote_exists(self, dropbox_path: str) -> bool:
            from app.services.digest.hosting import DropboxError

            self.probes.append(dropbox_path)
            raise DropboxError("metadata lookup failed with HTTP 409")

    host = _UnknownProbe()
    with capture_logs() as logs:
        assert (
            await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host))
            == 0
        )
    assert host.probes == ["/memos-mail/run/unknown.mp4"]
    assert _rows_now()[0]["status"] == "pending"
    probe_failures = [
        entry for entry in logs if entry["event"] == "digest.hosted_media_probe_failed"
    ]
    assert len(probe_failures) == 1


async def test_the_run_a_ledger_row_names_is_the_run_that_claimed_it(env):
    """Positive control on the join: without the FK being written, every liveness test above would
    pass for the wrong reason (a NULL owner is treated as "not alive")."""
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    result = await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(FakeHost()), now_utc=NOW
    )
    assert result is not None
    assert [row["run_id"] for row in _rows_now()] == [result.run_id]


# --- the interruption between the bytes and the record ------------------------------------------


async def test_a_crash_between_the_upload_and_the_mark_is_reconciled_without_re_uploading(env):
    """The exact window the ledger exists for, end to end.

    The upload SUCCEEDS and the process dies before `mark_uploaded` — so Dropbox holds an object
    and the ledger holds a `pending` row naming its path. Nothing is lost: once the owning run is
    no longer alive, the sweep asks Dropbox, finds the object, and records it. No orphan, and no
    second upload of a file that is already there.
    """
    subject, data_root = env
    await _seed_video_memo(subject, data_root)

    class _DiesAfterTheUpload(FakeHost):
        async def upload(self, local_path, dropbox_path):
            await super().upload(local_path, dropbox_path)
            raise RuntimeError("simulated process death between the upload and the mark")

    crashing = _DiesAfterTheUpload()
    with pytest.raises(RuntimeError):
        await digest_runner.run_digest(
            transport=FakeTransport(), host_factory=host_factory_for(crashing), now_utc=NOW
        )
    stranded = _rows_now()
    assert [row["status"] for row in stranded] == ["pending"]
    assert len(crashing.uploads) == 1  # the bytes DID land
    dropbox_path = stranded[0]["dropbox_path"]

    # The run that died is still in flight, so nothing is reconciled while it might be alive…
    quiet = FakeHost()
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(quiet)) == 0
    )
    assert quiet.probes == []

    # …until the stale-run sweep closes it, which is the same 15 minutes of silence.
    age_run(
        stranded[0]["run_id"],
        heartbeat_minutes=digest_runner.STALE_RUN_MINUTES + 5,
        started_minutes=60,
    )
    age_hosted_claim(stranded[0]["id"], minutes=90)
    assert await digest_runner.sweep_stale_runs() == 1

    recovering = FakeHost()  # `remote_exists` answers True: the object is there
    assert (
        await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(recovering))
        == 0
    )
    assert recovering.probes == [dropbox_path]
    assert recovering.uploads == []  # nothing is uploaded a second time
    row = _rows_now()[0]
    assert row["status"] == "uploaded"
    assert row["shared_link"] and row["uploaded_at"] is not None


# --- the rollback refuses to destroy a tombstone ------------------------------------------------


def test_the_downgrade_refuses_while_an_ownership_tombstone_exists(db_engine):
    """A rollback must never silently delete the sole record of a file on Dropbox.

    The pre-ledger shape keyed the row on `attachment_id`, so it has nowhere to put a row whose
    attachment is gone — and that row is exactly the tombstone the retention sweep still needs.
    The downgrade therefore refuses, before it touches anything, and names what to do first.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    ledger_id = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/run/tombstone.mp4",
        status="uploaded",
        shared_link="https://dropbox.example/tombstone.mp4?raw=1",
        uploaded_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    try:
        with pytest.raises(RuntimeError, match="refusing to downgrade"):
            command.downgrade(cfg, "-1")
        # The refusal is a REFUSAL: the schema is untouched, so the row is still readable.
        assert [row["id"] for row in _rows_now()] == [ledger_id]
    finally:
        engine = create_engine(SYNC_URL)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM digest_hosted_media WHERE id = :id"), {"id": ledger_id})
        engine.dispose()


# --- no transaction crosses an external boundary ------------------------------------------------


async def test_no_transaction_is_open_during_an_upload_or_an_smtp_send(env):
    """The bounded-pool rule, asserted at the moment of the call rather than reasoned about.

    A SQLAlchemy `SELECT` autobegins a transaction, so a session merely left open across an upload
    pins a pooled connection and an idle transaction for as long as the transfer takes — which for
    one upload-session operation alone is up to ten minutes. The fakes ask the session factory,
    while they are being called, how many of the runner's sessions are holding one.
    """
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    sessions = TrackingSessionFactory()
    host = FakeHost(sessions=sessions)
    transport = FakeTransport(sessions=sessions)
    result = await digest_runner.run_digest(
        session_factory=sessions,
        transport=transport,
        host_factory=host_factory_for(host),
        now_utc=NOW,
    )
    assert result is not None and result.status == digest_runner.SENT
    # Positive control: the assertions above only mean something if the boundaries were reached.
    assert len(host.uploads) == 1
    assert len(transport.sent) == 1


async def test_no_transaction_is_open_during_a_retention_delete_or_a_probe(env):
    """The same rule for the sweep, which used to select every due row and delete each one
    remotely inside that one transaction."""
    sessions = TrackingSessionFactory()
    kept = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/run/old.mp4",
        status="uploaded",
        shared_link="https://dropbox.example/old.mp4?raw=1",
        uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stale = insert_hosted_row(
        kind="video", dropbox_path="/memos-mail/run/stuck.mp4", status="pending"
    )
    age_hosted_claim(stale, minutes=90)
    host = FakeHost(sessions=sessions)
    swept = await digest_runner.sweep_hosted_media_safe(
        session_factory=sessions, host_factory=host_factory_for(host)
    )
    assert swept == 1
    assert host.deletes == ["/memos-mail/run/old.mp4"]
    assert host.probes == ["/memos-mail/run/stuck.mp4"]
    assert {row["id"]: row["status"] for row in _rows_now()} == {
        kept: "swept",
        stale: "uploaded",
    }


async def test_no_transaction_is_open_during_a_test_send(env):
    """A test-send is bounded — no uploads, one recipient — so it stays synchronous and answers in
    the request that asked. That is about how long it takes, not a licence to hold a pooled
    connection through an SMTP conversation with a remote server."""
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    sessions = TrackingSessionFactory()
    transport = FakeTransport(sessions=sessions)
    count = await digest_runner.test_send(
        sessions, address="admin@x", transport=transport, now_utc=NOW
    )
    assert count == 1  # control: it really rendered and sent something
    assert len(transport.sent) == 1


async def test_the_tracking_factory_can_actually_see_a_held_transaction(env):
    """Positive control for the mechanism the two tests above rely on: a factory that reported 0
    unconditionally would make both of them pass with the boundary rule deleted."""
    sessions = TrackingSessionFactory()
    session: AsyncSession = sessions()
    async with session as db:
        await db.execute(text("SELECT 1"))
        assert sessions.live_transactions() == 1
        with pytest.raises(AssertionError, match="open across"):
            sessions.assert_no_transaction("a probe")
    assert sessions.live_transactions() == 0


# --- what a probe must never do -----------------------------------------------------------------


async def test_a_test_send_writes_no_ledger_row_at_all(env):
    """A probe that claimed a path would leave a `pending` row naming an object that will never
    exist, and the reconciliation sweep would then go and ask Dropbox about it every hour."""
    subject, data_root = env
    await _seed_video_memo(subject, data_root)
    transport = FakeTransport()
    await digest_runner.test_send(address="admin@x", transport=transport, now_utc=NOW)
    assert len(transport.sent) == 1
    assert _rows_now() == []


async def test_a_manual_claim_writes_no_ledger_row_either(env):
    """The 202 path commits a run row and nothing else — the hosting pass belongs to the worker."""
    async with async_session() as db:
        await digest_runner.open_run(
            db, trigger=DigestTrigger.manual, local_date=NOW.date()
        )
    assert _rows_now() == []
