"""A row is only reused if the files it names are still there.

An attachment whose bytes have been removed under it — the shape a partial unlink leaves behind
when the rest of the removal rolls back — is still a perfectly good dedup candidate by every
column: same owner, same sha256, no memo. Reusing it hands the memo a picture that cannot be
served and throws away the upload that still had the bytes. So every route that ADOPTS an existing
row stats what that row names, under the row's own lock, before the upload is consumed.

Regenerable tiers are deliberately not stat'ed: a thumb a worker has not written yet is not a
reason to refuse, and refusing on it would break dedup for every fresh upload.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentRendition, AttachmentStatus, User, Visibility
from app.services import asset_paths, poster
from app.services import memos as memo_svc
from app.services import uploads as up_svc
from app.services.reclaim import Revalidate, reclaim_attachment
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"
BYTES = PNG + b"twin" * 600
DIGEST = hashlib.sha256(BYTES).hexdigest()
_AN_HOUR_AGO = datetime.now(UTC) - timedelta(hours=1)


async def _owner(adb, subject: str) -> User:
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


def _file(root: Path, name: str, data: bytes = BYTES) -> Path:
    path = root / "originals" / "2026" / "09" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _candidate(
    adb, owner, root: Path, uid: str, *, mime: str = "image/png", detached=None
) -> Attachment:
    """An unlinked attachment holding BYTES, with its original really on disk.

    Exactly what both dedup paths look for, so the only thing a probe below changes is which of
    the files it names is missing.
    """
    att = Attachment(
        uid=uid,
        owner_id=owner.id,
        memo_id=None,
        detached_at=detached,
        filename=f"{uid}.png",
        mime=mime,
        size=len(BYTES),
        sha256=DIGEST,
        status=AttachmentStatus.ready,
        storage_path=str(_file(root, f"{uid}.png")),
    )
    adb.add(att)
    await adb.flush()
    return att


async def _upload_of(db, owner_id: int, name: str) -> object:
    upload = await up_svc.create_upload(
        db, owner_id=owner_id, filename=name, mime="image/png", size=len(BYTES)
    )
    await up_svc.append_chunk(db, upload, 0, BYTES)
    return upload


async def _setup(adb, monkeypatch, tmp_path, subject: str, **kwargs):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    owner = await _owner(adb, subject)
    att = await _candidate(adb, owner, tmp_path, subject, **kwargs)
    return owner, att


def _cleanup() -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for table in ("media_jobs", "attachment_renditions", "uploads", "attachments", "users"):
            c.execute(text(f"DELETE FROM {table}"))
    eng.dispose()


def _rows(sql: str, **params) -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text(sql), params)]
    eng.dispose()
    return out


def _seed_committed(tmp_path: Path, subject: str) -> tuple[int, int, str, Path]:
    """An owner and one detached attachment holding BYTES, committed. Returns ids, uid, original.

    The reclaim below runs a transaction of its own and unwinds it, so none of this can live in
    the `adb` fixture's transaction: what the failing reclaim leaves behind has to survive that
    rollback and be readable by the finalize that comes after it.
    """
    original = _file(tmp_path, f"{subject}.png")
    eng = create_engine(SYNC_URL)
    with Session(eng) as seed:
        user = User(subject=subject, role="user")
        seed.add(user)
        seed.flush()
        att = Attachment(
            uid=subject,
            owner_id=user.id,
            memo_id=None,
            detached_at=_AN_HOUR_AGO,
            filename=f"{subject}.png",
            mime="image/png",
            size=len(BYTES),
            sha256=DIGEST,
            status=AttachmentStatus.ready,
            storage_path=str(original),
        )
        seed.add(att)
        seed.flush()
        ids = (user.id, att.id, att.uid)
        seed.commit()
    eng.dispose()
    return (*ids, original)


async def _reclaim_that_unwinds(att_id: int, original: Path, *, removing: bool) -> None:
    """One reclaim whose unlink fails, leaving the row exactly as production leaves it.

    `removing` decides WHEN the failure lands: after the original is gone — the half-deleted row
    this module is about — or before the seam touches anything, which is the control. Either way the
    reclaim fails closed, the whole transaction unwinds, and the row is still there; the only
    difference is whether it still names bytes.
    """

    def failing_unlink(paths: list[Path]) -> list[str]:
        assert original in paths, "the reclaim never named the original"
        if removing:
            original.unlink()
        return [str(original.parent / "a-file-the-mount-would-not-give-up")]

    async with async_session() as db:
        result = await reclaim_attachment(
            db,
            att_id,
            Revalidate.DETACHED,
            unlink=failing_unlink,
            detached_before=datetime.now(UTC),
        )
    assert result.outcome == "failed_unlink"
    assert original.exists() is not removing
    assert _rows("SELECT memo_id FROM attachments WHERE id=:a", a=att_id) == [(None,)]


# --- the half-deleted row is not reused, in BOTH dedup paths --------------------------------------


async def test_the_batch_path_keeps_its_own_bytes_when_a_failed_reclaim_took_the_original(
    db_engine, monkeypatch, tmp_path
):
    """The half-deleted row is made the only way production makes one: a reclaim removed the
    original, then its unlink failed and the whole transaction unwound, so the surviving
    `memo_id IS NULL` row names bytes that are gone. An identical upload keeps its own."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        owner_id, att_id, att_uid, original = _seed_committed(tmp_path, "batch_gone")
        await _reclaim_that_unwinds(att_id, original, removing=True)

        async with async_session() as db:
            upload = await _upload_of(db, owner_id, "again.png")
            await db.commit()
            out = await up_svc.finalize_pending(db, [upload.upload_uid], owner_id)
            await db.commit()

        assert out != [att_uid], "the upload was deduped onto a row whose original is gone"
        [(made_path,)] = _rows("SELECT storage_path FROM attachments WHERE uid=:u", u=out[0])
        assert Path(made_path).exists()
    finally:
        _cleanup()


async def test_the_standalone_path_keeps_its_own_bytes_when_a_failed_reclaim_took_the_original(
    db_engine, monkeypatch, tmp_path
):
    """The same half-deleted row, put to the other route: the two paths reach the candidate
    differently, so a guard in one does not cover the other."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        owner_id, att_id, _uid, original = _seed_committed(tmp_path, "solo_gone")
        await _reclaim_that_unwinds(att_id, original, removing=True)

        async with async_session() as db:
            upload = await _upload_of(db, owner_id, "again.png")
            await db.commit()
            made = await up_svc.finalize(db, upload, owner_id)
            await db.commit()
            made_id, made_path = made.id, made.storage_path

        assert made_id != att_id
        assert Path(made_path).exists()
    finally:
        _cleanup()


async def test_the_candidate_is_still_reused_when_the_failed_reclaim_left_its_bytes(
    db_engine, monkeypatch, tmp_path
):
    """The control both probes need, and it differs from them in one thing only: this reclaim
    failed BEFORE it removed anything, so the row it left still has its original. Dedup itself is
    untouched, and the adopted row is no longer something a sweep may take."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        owner_id, att_id, _uid, original = _seed_committed(tmp_path, "solo_ok")
        await _reclaim_that_unwinds(att_id, original, removing=False)

        async with async_session() as db:
            upload = await _upload_of(db, owner_id, "again.png")
            await db.commit()
            made = await up_svc.finalize(db, upload, owner_id)
            await db.commit()
            made_id, made_detached = made.id, made.detached_at

        assert made_id == att_id
        assert made_detached is None  # adopted rows are nobody's garbage
    finally:
        _cleanup()


# --- it is the SET of non-regenerable paths, not just the original --------------------------------


@pytest.mark.parametrize("case", ["legacy_print", "later_rendition", "video_poster"])
async def test_a_candidate_missing_any_non_regenerable_file_is_not_reused(
    adb, monkeypatch, tmp_path, case
):
    """The original survives in each case; one of the other files the row owns does not.

    `later_rendition` removes the SECOND of two masters, so a guard that checks only the first
    cannot pass it.
    """
    mime = "video/mp4" if case == "video_poster" else "image/png"
    owner, att = await _setup(adb, monkeypatch, tmp_path, f"c19_{case}", mime=mime)
    if case == "legacy_print":
        att.print_path = str(_file(tmp_path, f"{att.uid}-print.png"))
        Path(att.print_path).unlink()
    elif case == "later_rendition":
        for index, present in ((0, True), (1, False)):
            path = _file(tmp_path, f"{att.uid}-r{index}.png")
            if not present:
                path.unlink()
            adb.add(
                AttachmentRendition(
                    attachment_id=att.id,
                    variant_key=f"print:{index}",
                    filename=path.name,
                    mime="image/png",
                    size=len(BYTES),
                    storage_path=str(path),
                )
            )
    else:
        pass  # a video's poster is required, and this one has none on disk
    await adb.flush()
    await adb.refresh(att)
    upload = await _upload_of(adb, owner.id, "again.png")

    made = await up_svc.finalize(adb, upload, owner.id)

    assert made.id != att.id, f"{case}: adopted a row that cannot be served whole"


async def test_a_video_with_its_poster_on_disk_is_reused(adb, monkeypatch, tmp_path):
    """The video case needs its own control: the poster is what makes the difference, not
    the mime."""
    owner, att = await _setup(adb, monkeypatch, tmp_path, "c19_video_ok", mime="video/mp4")
    dest = poster.poster_path(att.uid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"poster")
    upload = await _upload_of(adb, owner.id, "again.png")

    made = await up_svc.finalize(adb, upload, owner.id)

    assert made.id == att.id


# --- the lock boundary is the subject ---------------------------------------------------------


async def test_a_reclaim_that_unwound_after_discovery_is_still_caught(
    db_engine, monkeypatch, tmp_path
):
    """The candidate is whole when the unlocked discovery reads it, and half-deleted by the time
    the acquisition takes its lock: the reclaim removes the original and unwinds inside that gap.
    Only the stat under the row's own lock can answer this — a check made during discovery would
    have been taken about a row that was still whole."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        owner_id, att_id, att_uid, original = _seed_committed(tmp_path, "gap_gone")
        fired = 0
        real_execute = AsyncSession.execute

        async def reclaiming_in_the_gap(self, statement, *args, **kwargs):
            nonlocal fired
            sql = str(statement)
            # In the gap: the unlocked discovery has read this row and named it a candidate, and
            # the acquisition that will judge it under the lock has not run yet.
            if "FROM attachments" in sql and "FOR UPDATE" in sql and not fired:
                fired += 1
                await _reclaim_that_unwinds(att_id, original, removing=True)
            return await real_execute(self, statement, *args, **kwargs)

        async with async_session() as db:
            upload = await _upload_of(db, owner_id, "again.png")
            await db.commit()
            monkeypatch.setattr(AsyncSession, "execute", reclaiming_in_the_gap)
            out = await up_svc.finalize_pending(db, [upload.upload_uid], owner_id)
            await db.commit()

        assert fired == 1, "the acquisition never ran — this test proves nothing"
        assert out != [att_uid]
        # A different uid is not yet the answer this upload needed: what it needed is its own
        # bytes, on disk, under the row it was given back.
        [(made_uid, made_path)] = _rows(
            "SELECT uid, storage_path FROM attachments WHERE uid = :u", u=out[0]
        )
        assert made_uid != att_uid
        assert Path(made_path).read_bytes() == BYTES
        # And the half-deleted row is still there, still unlinked: the guard refused it, it did
        # not delete it — the reclaim that half-removed it is the only thing that may finish it.
        assert _rows("SELECT memo_id FROM attachments WHERE uid = :u", u=att_uid) == [(None,)]
    finally:
        _cleanup()


# --- the direct-link route ------------------------------------------------------------------------


async def test_direct_linking_refuses_a_row_whose_bytes_are_gone(adb, monkeypatch, tmp_path):
    """At the seam. Both shapes refuse: a row something detached (stamped) and one a partial
    unlink damaged in place (unstamped — nothing ever took it off a memo).

    The uids come back to the caller so a route with a typed contract can answer `missing` for
    them; the back-compat create path, which has always skipped what it cannot link, skips them.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    owner = await _owner(adb, "link_gone")
    stamped = await _candidate(adb, owner, tmp_path, "link_stamped", detached=_AN_HOUR_AGO)
    damaged = await _candidate(adb, owner, tmp_path, "link_damaged")
    whole = await _candidate(adb, owner, tmp_path, "link_whole", detached=_AN_HOUR_AGO)
    for att in (stamped, damaged):
        Path(att.storage_path).unlink()
    memo = await memo_svc.create_memo(adb, owner, content="pics", visibility=Visibility.PUBLIC)

    refused: list[str] = []
    await memo_svc.link_attachments(
        adb, owner.id, memo, [a.uid for a in (stamped, damaged, whole)], refused_out=refused
    )

    assert refused == ["link_stamped", "link_damaged"]
    assert (await _by_uid(adb, "link_stamped")).memo_id is None
    assert (await _by_uid(adb, "link_damaged")).memo_id is None
    linked = await _by_uid(adb, "link_whole")
    assert linked.memo_id == memo.id
    assert linked.detached_at is None  # linking is an adoption: the stamp goes with the link


async def test_a_freshly_finalized_attachment_is_linked_without_a_second_look(
    adb, monkeypatch, tmp_path
):
    """What this transaction just minted or already adopted is not re-judged — a video uploaded a
    moment ago has no poster yet (the worker derives it), and refusing it would refuse the very
    upload that created it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    owner = await _owner(adb, "link_fresh")
    fresh = await _candidate(adb, owner, tmp_path, "link_fresh_video", mime="video/mp4")
    memo = await memo_svc.create_memo(adb, owner, content="pics", visibility=Visibility.PUBLIC)

    refused: list[str] = []
    await memo_svc.link_attachments(
        adb, owner.id, memo, [fresh.uid], vouched={fresh.uid}, refused_out=refused
    )

    assert refused == []
    assert (await _by_uid(adb, "link_fresh_video")).memo_id == memo.id


# --- the guard itself -------------------------------------------------------------------------


async def test_adoptable_ignores_the_regenerable_tiers(adb, monkeypatch, tmp_path):
    """Stat'ing everything an attachment CAN own would refuse every adoption: the derived tiers of
    a photo uploaded seconds ago are written by a worker that has not run yet."""
    _, seeded = await _setup(adb, monkeypatch, tmp_path, "regenerable")
    att = await _by_uid(adb, seeded.uid)  # as every adoption route reaches it: read, not built
    assert await asset_paths.adoptable(att) is True
    assert not (tmp_path / "assets").exists()  # nothing about a thumb was even asked


async def _by_uid(adb, uid: str) -> Attachment:
    return (await adb.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one()
