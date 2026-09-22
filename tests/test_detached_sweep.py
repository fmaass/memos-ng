"""The rows nothing else looks for: attachments that left their memo and stayed gone.

`reconcile.py` requires a memo, and the leaked-orphan path predicate only matches an original under
a dated `originals/YYYY/MM/<memo-folder>/` — an original imported from 0.29.1 stays FLAT, so no path
predicate can tell a detached import from media nobody's memo ever owned. What can tell them apart
is the detach instant, and these tests hold the sweep to it: old enough by THAT clock and nothing
has touched it since, or it is left alone.

The interesting half is under the row lock. A pass selects a row, and by the time the reclaim takes
its lock the row may have been adopted by a dedup (its instant cleared) or relinked and detached
again (a newer one). Both belong to whoever touched them last, so both are refused against the very
cutoff the pass selected on.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob, User
from app.services.reclaim import Revalidate, reclaim_attachment
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.conftest import SYNC_URL

GRACE = timedelta(seconds=worker.REAP_INTERVAL_SECONDS)


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for table in ("media_jobs", "attachment_renditions", "attachments", "memos", "users"):
            c.execute(text(f"DELETE FROM {table}"))
    eng.dispose()


def _rows(sql: str, **params) -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text(sql), params)]
    eng.dispose()
    return out


def _seed(
    tmp_path: Path,
    uid: str,
    *,
    detached_at: datetime | None,
    created_at: datetime | None = None,
    flat: bool = False,
) -> tuple[int, Path]:
    """One owner-owned attachment with a real file, committed. Returns its id and its original."""
    original = (
        tmp_path / "originals" / f"{uid}.png"
        if flat
        else tmp_path / "originals" / "2026" / "08" / f"2026-08-25_1200_{uid}" / f"{uid}.png"
    )
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"png")
    eng = create_engine(SYNC_URL)
    with Session(eng) as seed:
        owner = seed.execute(text("SELECT id FROM users LIMIT 1")).scalar_one_or_none()
        if owner is None:
            user = User(subject=f"sweep_owner_{uid}", role="user")
            seed.add(user)
            seed.flush()
            owner = user.id
        att = Attachment(
            uid=uid,
            owner_id=owner,
            memo_id=None,
            filename=f"{uid}.png",
            mime="image/png",
            size=3,
            status=AttachmentStatus.ready,
            storage_path=str(original),
            detached_at=detached_at,
        )
        seed.add(att)
        seed.flush()
        if created_at is not None:
            seed.execute(
                text("UPDATE attachments SET created_at = :c WHERE id = :a"),
                {"c": created_at, "a": att.id},
            )
        att_id = att.id
        seed.commit()
    eng.dispose()
    return att_id, original


def _stamp(att_id: int, value: datetime | None) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text("UPDATE attachments SET detached_at = :d WHERE id = :a"),
            {"d": value, "a": att_id},
        )
    eng.dispose()


# --- the sweep's selection ---------------------------------------------------------------------


async def test_a_detachment_older_than_the_grace_is_reclaimed(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "old", detached_at=datetime.now(UTC) - 2 * GRACE)
        assert original.exists()

        assert await worker.sweep_detached_attachments() == 1

        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert not original.exists()
    finally:
        _cleanup()


async def test_a_detachment_inside_the_grace_is_left_alone(db_engine, monkeypatch, tmp_path):
    """The control that keeps the sweep off a request still cleaning up after its own commit."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "fresh", detached_at=datetime.now(UTC))

        assert await worker.sweep_detached_attachments() == 0

        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()
    finally:
        _cleanup()


async def test_a_row_that_was_never_detached_is_not_a_candidate(db_engine, monkeypatch, tmp_path):
    """A standalone upload and a detach that predates the column both carry NULL, and neither is
    anybody's garbage."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "never", detached_at=None)

        assert await worker.sweep_detached_attachments() == 0

        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()
    finally:
        _cleanup()


async def test_a_flat_imported_original_is_swept_once_its_detachment_is_old(
    db_engine, monkeypatch, tmp_path
):
    """The row the path predicate cannot see: an import keeps its original in the FLAT `originals/`
    bucket, so what makes it garbage is that something detached it, not where its file sits."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(
            tmp_path, "imported", detached_at=datetime.now(UTC) - 2 * GRACE, flat=True
        )

        assert await worker.sweep_detached_attachments() == 1

        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert not original.exists()
    finally:
        _cleanup()


async def test_an_old_file_detached_just_now_is_not_swept(db_engine, monkeypatch, tmp_path):
    """Age since UPLOAD is a different age: a picture from weeks ago, detached seconds ago, is
    exactly what a `created_at` clock would delete out from under the request that detached it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(
            tmp_path,
            "elderly",
            detached_at=datetime.now(UTC),
            created_at=datetime.now(UTC) - timedelta(days=21),
        )

        assert await worker.sweep_detached_attachments() == 0

        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()
    finally:
        _cleanup()


async def test_the_sweep_takes_its_rows_by_id(db_engine, monkeypatch, tmp_path):
    """Ascending, like every other acquisition here, and it holds no lock while it discovers."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        stale = datetime.now(UTC) - 2 * GRACE
        first, one = _seed(tmp_path, "batch-a", detached_at=stale)
        second, two = _seed(tmp_path, "batch-b", detached_at=stale)
        assert first < second

        assert await worker.sweep_detached_attachments() == 2

        assert _rows("SELECT id FROM attachments") == []
        assert not one.exists() and not two.exists()
    finally:
        _cleanup()


# --- the re-check under the row lock -------------------------------------------------------------


async def test_a_candidate_redetached_before_the_lock_is_refused(db_engine, monkeypatch, tmp_path):
    """Relinked and detached again between the pass and the lock: the row is a candidate by the
    cutoff this pass selected on, but the instant under the lock is a NEW one, inside the grace,
    and it belongs to whoever wrote it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "redetached", detached_at=datetime.now(UTC) - 2 * GRACE)
        cutoff = datetime.now(UTC) - GRACE
        _stamp(att_id, datetime.now(UTC))  # the second detach, after this pass selected the row

        async with async_session() as db:
            result = await reclaim_attachment(
                db, att_id, Revalidate.DETACHED, detached_before=cutoff
            )

        assert result.outcome == "skipped_ineligible"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()
    finally:
        _cleanup()


async def test_a_candidate_adopted_before_the_lock_is_refused(db_engine, monkeypatch, tmp_path):
    """A standalone finalize adopting the row clears its instant while `memo_id` stays NULL, so
    linkage alone cannot tell this apart from garbage — the missing instant can."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "adopted", detached_at=datetime.now(UTC) - 2 * GRACE)
        cutoff = datetime.now(UTC) - GRACE
        _stamp(att_id, None)  # the adoption, which clears the instant

        async with async_session() as db:
            result = await reclaim_attachment(
                db, att_id, Revalidate.DETACHED, detached_before=cutoff
            )

        assert result.outcome == "skipped_ineligible"
        assert _rows("SELECT id, memo_id, detached_at FROM attachments WHERE id=:a", a=att_id) == [
            (att_id, None, None)
        ]
        assert original.exists()
    finally:
        _cleanup()


async def test_a_sweep_pass_skips_the_row_an_upload_adopted(db_engine, monkeypatch, tmp_path):
    """The whole pass, not just the reclaim: an adopted row is still `memo_id IS NULL`, and the
    upload that adopted it gets to keep its files."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "readopted", detached_at=datetime.now(UTC) - 2 * GRACE)
        _stamp(att_id, None)

        assert await worker.sweep_detached_attachments() == 0

        assert _rows("SELECT id, detached_at FROM attachments WHERE id=:a", a=att_id) == [
            (att_id, None)
        ]
        assert original.exists()
    finally:
        _cleanup()


async def test_an_orphaned_reclaim_is_judged_against_the_instant_that_issued_it(
    db_engine, monkeypatch, tmp_path
):
    """The after-commit loop's fence, per category: the instant it detached with (go ahead), a
    cleared one (adopted), a newer one (somebody else's detach), and no instant at all (the callers
    that predate the fence, whose behaviour must not change)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        issued = datetime.now(UTC) - timedelta(seconds=5)
        adopted, adopted_file = _seed(tmp_path, "orph-adopted", detached_at=issued)
        _stamp(adopted, None)
        newer, newer_file = _seed(tmp_path, "orph-newer", detached_at=datetime.now(UTC))
        unfenced, _ = _seed(tmp_path, "orph-unfenced", detached_at=issued)
        mine, mine_file = _seed(tmp_path, "orph-mine", detached_at=issued)

        async with async_session() as db:
            skipped_adopted = await reclaim_attachment(
                db, adopted, Revalidate.ORPHANED, detached_at=issued
            )
        async with async_session() as db:
            skipped_newer = await reclaim_attachment(
                db, newer, Revalidate.ORPHANED, detached_at=issued
            )
        async with async_session() as db:
            no_fence = await reclaim_attachment(db, unfenced, Revalidate.ORPHANED)
        async with async_session() as db:
            claimed = await reclaim_attachment(db, mine, Revalidate.ORPHANED, detached_at=issued)

        assert skipped_adopted.outcome == "skipped_ineligible"
        assert adopted_file.exists()
        assert skipped_newer.outcome == "skipped_ineligible"
        assert newer_file.exists()
        assert no_fence.outcome == "reclaimed"  # today's callers, unchanged
        assert claimed.outcome == "reclaimed"
        assert not mine_file.exists()
    finally:
        _cleanup()


# --- failures ------------------------------------------------------------------------------------


async def test_a_transient_unlink_failure_leaves_the_row_for_the_next_pass(
    db_engine, monkeypatch, tmp_path
):
    """`missing_ok` is what makes the retry whole: the second pass re-derives the full set and
    tolerates whatever the first one already removed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "flaky", detached_at=datetime.now(UTC) - 2 * GRACE)
        attempts: list[int] = []

        def flaky(paths: list[Path]) -> list[str]:
            attempts.append(1)
            if len(attempts) == 1:
                return [str(paths[0])]
            for path in paths:
                path.unlink(missing_ok=True)
            return []

        assert await worker.sweep_detached_attachments(unlink=flaky) == 0
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()  # the row still names its bytes

        assert await worker.sweep_detached_attachments(unlink=flaky) == 1
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert not original.exists()
    finally:
        _cleanup()


async def test_a_persistent_unlink_failure_keeps_the_row_every_pass(
    db_engine, monkeypatch, tmp_path
):
    """The honest half: a sweep does not fix a read-only mount. The row and its files survive, and
    every pass says so — which is the only thing that makes the failure visible."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "stuck", detached_at=datetime.now(UTC) - 2 * GRACE)

        def always_fails(paths: list[Path]) -> list[str]:
            return [str(path) for path in paths]

        with structlog.testing.capture_logs() as logs:
            assert await worker.sweep_detached_attachments(unlink=always_fails) == 0
            assert await worker.sweep_detached_attachments(unlink=always_fails) == 0

        assert [entry["event"] for entry in logs].count("reclaim.unlink_failed") == 2
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()
    finally:
        _cleanup()


async def test_the_sweep_takes_the_rows_media_jobs_with_it(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, _ = _seed(tmp_path, "withjobs", detached_at=datetime.now(UTC) - 2 * GRACE)
        eng = create_engine(SYNC_URL)
        with Session(eng) as seed:
            seed.add(MediaJob(attachment_id=att_id, lane=JobLane.fast, state=JobState.queued))
            seed.commit()
        eng.dispose()

        assert await worker.sweep_detached_attachments() == 1

        assert _rows("SELECT id FROM media_jobs WHERE attachment_id=:a", a=att_id) == []
    finally:
        _cleanup()


# --- what the deploy reads back ------------------------------------------------------------------


async def test_every_pass_logs_its_count_even_when_it_reclaimed_nothing(
    db_engine, monkeypatch, tmp_path
):
    """A sweep that found nothing and a worker that never reached its hourly block look the same in
    the log otherwise."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        with structlog.testing.capture_logs() as logs:
            assert await worker.sweep_detached_attachments_safe() == 0
        counted = [entry for entry in logs if entry["event"] == "detached_attachments_swept"]
        assert counted == [{"event": "detached_attachments_swept", "count": 0, "log_level": "info"}]
    finally:
        _cleanup()


async def test_an_error_inside_the_sweep_cannot_reach_the_supervisor(monkeypatch):
    """The boundary guard every periodic sweep gets: media processing must not stop because a sweep
    could not read its own table."""

    async def boom(**kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(worker, "sweep_detached_attachments", boom)
    assert await worker.sweep_detached_attachments_safe() == 0
