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

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from app import auth, worker
from app.config import settings
from app.csrf import canonical_origin
from app.database import async_session
from app.main import app
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob, User
from app.routers import memos as memos_router
from app.services import reclaim
from app.services.reclaim import Revalidate, reclaim_attachment, unlink_paths
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.conftest import SYNC_URL

GRACE = timedelta(seconds=worker.REAP_INTERVAL_SECONDS)
# Generous next to the milliseconds a released park needs: every wait below ends on an OBSERVED
# state change, so this only bounds a hang.
SEAM_TIMEOUT = 30.0


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for table in (
            "media_jobs",
            "attachment_renditions",
            "uploads",
            "attachments",
            "memos",
            "users",
        ):
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


def _left_for_the_sweep(att_uid: str, detached_at: datetime) -> None:
    """Put a route-made attachment where the sweep finds it: off its memo, stamped, memo gone.

    This is the state a delete leaves behind when its after-commit reclaim never completed — the
    crash window the hourly sweep exists for. It cannot be reached by waiting out the grace in a
    test, so the row is aged here; everything the sweep then meets, including the adoption, is real.
    """
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text("UPDATE attachments SET memo_id = NULL, detached_at = :d WHERE uid = :u"),
            {"d": detached_at, "u": att_uid},
        )
        c.execute(text("DELETE FROM memos"))
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
    """Ascending, like every other acquisition here, and it holds no lock while it discovers.

    The order is OBSERVED rather than inferred from the outcome: both rows are gone whichever way
    round the pass took them, so only the sequence of the reclaims themselves says anything. The
    sweep's own unlink seam is where that sequence shows — one call per row, inside its lock.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        stale = datetime.now(UTC) - 2 * GRACE
        first, one = _seed(tmp_path, "batch-a", detached_at=stale)
        second, two = _seed(tmp_path, "batch-b", detached_at=stale)
        assert first < second
        row_of = {one: first, two: second}
        taken: list[int] = []

        def watch(paths: list[Path]) -> list[str]:
            taken.extend(row_of[path] for path in paths if path in row_of)
            return unlink_paths(paths)

        assert await worker.sweep_detached_attachments(unlink=watch) == 2

        assert taken == [first, second]
        assert _rows("SELECT id FROM attachments") == []
        assert not one.exists() and not two.exists()
    finally:
        _cleanup()


# --- the re-check under the row lock -------------------------------------------------------------


async def test_a_candidate_redetached_before_the_lock_is_refused(db_engine, monkeypatch, tmp_path):
    """The row is a candidate by the cutoff this call carries, but the instant under the lock is a
    newer one, inside the grace, and it belongs to whoever wrote it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "redetached", detached_at=datetime.now(UTC) - 2 * GRACE)
        cutoff = datetime.now(UTC) - GRACE
        _stamp(att_id, datetime.now(UTC))  # newer than the cutoff the call below carries

        async with async_session() as db:
            result = await reclaim_attachment(
                db, att_id, Revalidate.DETACHED, detached_before=cutoff
            )

        assert result.outcome == "skipped_ineligible"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
        assert original.exists()
    finally:
        _cleanup()


# --- the real adoption, driven through the routes -----------------------------------------------


class Seam:
    """A one-shot park for `memos_router._after_commit_seam`, with a SYNCHRONOUS claim.

    `calls` is incremented before any await, so if a second request ever reached the seam the two
    could not both believe they were the parked one — the rule every one-shot answer here follows.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.parked = 0

    async def hold(self) -> None:
        self.calls += 1
        if self.calls > 1:
            return
        self.parked += 1
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=SEAM_TIMEOUT)

    def fired(self) -> None:
        assert self.parked == 1, (
            f"the request never parked between its commit and its reclaim ({self.parked}) — "
            "this test proves nothing"
        )


def _png(marker: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\x08" + marker + b"0" * 3000


def _upload(client, name: str, data: bytes) -> str:
    created = client.post(
        "/api/uploads",
        json={
            "filename": name,
            "mime": "image/png",
            "size": len(data),
            "last_modified": 1609459200000,
        },
    )
    assert created.status_code == 201, created.text
    uid = created.json()["upload_uid"]
    appended = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert appended.status_code == 200, appended.text
    return uid


def _async_client(client) -> AsyncClient:
    """An async client on the SAME session as the TestClient fixture: the login flow leaves more
    than one cookie of that name in the jar, so the LAST one is the live session."""
    sid = [c.value for c in client.cookies.jar if c.name == auth.SID_COOKIE][-1]
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": canonical_origin()},
        cookies={auth.SID_COOKIE: sid or ""},
    )


def _the_attachment() -> tuple[str, Path]:
    """The one attachment in the database, and where its original lives."""
    rows = _rows("SELECT uid, storage_path FROM attachments")
    assert len(rows) == 1, rows
    return rows[0][0], Path(rows[0][1])


async def _adopted_in_the_gap(client, monkeypatch, tmp_path, request):
    """Run `request(async_client, memo_uid)` and let a REAL standalone finalize adopt the row it
    detaches, in the gap between its commit and its after-commit reclaim.

    Returns `(the request's response, the attachment uid, its original, the captured logs)`.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    shared = _png(b"adopt")
    memo = client.post(
        "/api/memos",
        json={
            "content": "pics",
            "visibility": "PRIVATE",
            "upload_uids": [_upload(client, "shot.png", shared)],
        },
    )
    assert memo.status_code == 201, memo.text
    att_uid, original = _the_attachment()
    assert original.exists()
    # The adopter's own upload session, holding bytes identical to the attachment's — which is what
    # makes finalize dedup onto that row instead of minting a new one.
    twin = _upload(client, "shot.png", shared)

    seam = Seam()
    monkeypatch.setattr(memos_router, "_after_commit_seam", seam.hold)
    with structlog.testing.capture_logs() as logs:
        async with _async_client(client) as detacher, _async_client(client) as adopter:
            detaching = asyncio.create_task(request(detacher, memo.json()["uid"]))
            await asyncio.wait_for(seam.entered.wait(), timeout=SEAM_TIMEOUT)
            # The detach has COMMITTED and its reclaim has not run: this is the window.
            adopted = await adopter.post(f"/api/uploads/{twin}/finalize")
            assert adopted.status_code == 200, adopted.text
            assert adopted.json()["uid"] == att_uid, "the finalize did not dedup onto the row"
            seam.release.set()
            answer = await detaching
    seam.fired()
    return answer, att_uid, original, logs


def _still_adopted(att_uid: str, original: Path) -> None:
    assert _rows("SELECT uid, memo_id, detached_at FROM attachments WHERE uid=:u", u=att_uid) == [
        (att_uid, None, None)
    ], "the reclaim took the row the adopter now owns"
    assert original.exists(), "the reclaim took the bytes the adopter now owns"


def _not_reclaimed(logs, event: str, memo_uid: str) -> None:
    refusals = [entry for entry in logs if entry["event"] == event]
    assert [entry["outcome"] for entry in refusals] == ["skipped_ineligible"], refusals
    # Once the memo is gone no table maps the attachment back to it, so the warning is the one
    # record left of which memo the row came off; a warning without it names nothing findable.
    assert [entry["memo_uid"] for entry in refusals] == [memo_uid], refusals


async def test_a_delete_leaves_the_row_a_finalize_adopted_after_its_commit(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """DELETE's after-commit loop, against a REAL adoption in its own gap.

    `memo_id` is NULL either way — the adoption clears the instant, it does not relink — so linkage
    cannot tell the adopter's row from this delete's garbage. Only the epoch can, which is why the
    loop has to carry the instant its own transaction stamped.
    """
    client, _ = logged_in_client
    try:
        asked: list[str] = []

        async def delete(async_client, memo_uid):
            asked.append(memo_uid)
            return await async_client.delete(f"/api/memos/{memo_uid}")

        answer, att_uid, original, logs = await _adopted_in_the_gap(
            client, monkeypatch, tmp_path, delete
        )

        assert answer.status_code == 204, answer.text
        assert _rows("SELECT id FROM memos") == []
        _still_adopted(att_uid, original)
        _not_reclaimed(logs, "memo.delete.attachment_not_reclaimed", asked[0])
    finally:
        _cleanup()


async def test_a_patch_leaves_the_row_a_finalize_adopted_after_its_commit(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """The same experiment on the other caller: a picture list that drops its only picture detaches
    exactly as a delete does, and its after-commit loop owes the adopter the same fence."""
    client, _ = logged_in_client
    try:
        asked: list[str] = []

        async def patch(async_client, memo_uid):
            asked.append(memo_uid)
            return await async_client.patch(f"/api/memos/{memo_uid}", json={"attachment_uids": []})

        answer, att_uid, original, logs = await _adopted_in_the_gap(
            client, monkeypatch, tmp_path, patch
        )

        assert answer.status_code == 200, answer.text
        assert answer.json()["attachments"] == []
        _still_adopted(att_uid, original)
        _not_reclaimed(logs, "memo.patch.attachment_not_reclaimed", asked[0])
    finally:
        _cleanup()


class SweepGap:
    """A one-shot park in the sweep's own gap: candidates SELECTED, no row locked yet.

    It wraps `reclaim.reclaim_row_shielded` — the attribute the sweep looks up on the reclaim
    module for every row — rather than replacing it, so the pass, the cutoff it carries and the
    reclaim it calls are all the real ones; the wrapper only holds the call before the reclaim's
    own task and session exist, and records what each row's reclaim answered. The claim is
    synchronous, like `Seam`'s, so a second row could not also believe it was the parked one.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.parked = 0
        self.outcomes: list[str] = []

    async def paused(
        self,
        attachment_id: int,
        mode: Revalidate,
        *,
        unlink=None,
        detached_at: datetime | None = None,
        detached_before: datetime | None = None,
    ) -> object:
        self.calls += 1
        if self.calls == 1:
            self.parked += 1
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=SEAM_TIMEOUT)
        result = await self._real(
            attachment_id,
            mode,
            unlink=unlink,
            detached_at=detached_at,
            detached_before=detached_before,
        )
        self.outcomes.append(result.outcome)
        return result

    def fired(self) -> None:
        assert self.parked == 1, (
            f"the sweep never parked between its selection and its lock ({self.parked}) — "
            "this test proves nothing"
        )


async def test_the_sweep_leaves_the_row_an_upload_adopted_in_its_gap(
    db_engine, logged_in_client, monkeypatch, tmp_path
):
    """The sweep and a byte-identical upload, interleaved: selection, then the adoption, then the
    lock.

    The row is a candidate by this pass's cutoff and is adopted by a REAL standalone finalize
    before the pass gets to lock it, so the only thing standing between the adopter and a deleted
    picture is the revalidation under the lock, against the cutoff the pass came with.
    """
    client, _ = logged_in_client
    try:
        monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
        shared = _png(b"swept")
        memo = client.post(
            "/api/memos",
            json={
                "content": "pics",
                "visibility": "PRIVATE",
                "upload_uids": [_upload(client, "shot.png", shared)],
            },
        )
        assert memo.status_code == 201, memo.text
        att_uid, original = _the_attachment()
        _left_for_the_sweep(att_uid, datetime.now(UTC) - 2 * GRACE)
        # The adopter's own upload session, holding bytes identical to the attachment's.
        twin = _upload(client, "shot.png", shared)

        gap = SweepGap(reclaim.reclaim_row_shielded)
        monkeypatch.setattr(reclaim, "reclaim_row_shielded", gap.paused)
        with structlog.testing.capture_logs() as logs:
            sweeping = asyncio.create_task(worker.sweep_detached_attachments())
            await asyncio.wait_for(gap.entered.wait(), timeout=SEAM_TIMEOUT)
            async with _async_client(client) as adopter:
                adopted = await adopter.post(f"/api/uploads/{twin}/finalize")
            assert adopted.status_code == 200, adopted.text
            assert adopted.json()["uid"] == att_uid, "the finalize did not dedup onto the row"
            gap.release.set()
            reclaimed = await asyncio.wait_for(sweeping, timeout=SEAM_TIMEOUT)
        gap.fired()

        assert reclaimed == 0
        assert gap.outcomes == ["skipped_ineligible"]
        assert [entry["reason"] for entry in logs if entry["event"] == "reclaim.skipped"] == [
            "adopted_or_redetached"
        ]
        _still_adopted(att_uid, original)
        assert original.read_bytes() == shared, "the sweep took the bytes the adopter now owns"
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
