"""MEMNG-170 — the reconcile script's selection predicate, which decides what gets deleted.

Two kinds of row carry `memo_id IS NULL` on this instance and they must NOT be treated alike:
media the 0.29.1 migration brought over (legitimate orphans, originals in the FLAT `originals/`
bucket) and attachments a deleted memo left behind (originals under a dated
`originals/YYYY/MM/<memo-folder>/`, where only `link_attachments` ever writes). Getting that
predicate wrong deletes a household's migrated photos, so it is pinned against seeded rows of both
classes before the script is ever pointed at production.
"""

import pytest
import scripts.reconcile_orphan_attachments as script
from app.config import settings
from app.models import (
    Attachment,
    AttachmentStatus,
    JobLane,
    JobState,
    MediaJob,
    Memo,
    User,
    Visibility,
)
from app.services.asset_paths import all_asset_paths
from app.services.reclaim import Revalidate, reclaim_attachment_sync
from scripts.reconcile_orphan_attachments import leaked_orphans, main, read_back
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from tests.conftest import SYNC_URL


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                # `users` too: each test here seeds its own owner by a fixed subject, and the
                # unique index on it would collide with the previous test's leftover row.
                "TRUNCATE attachment_renditions, media_jobs, attachments, memo_tags, "
                "reactions, memos, uploads, users RESTART IDENTITY CASCADE"
            )
        )
    eng.dispose()


def _session(db_engine) -> Session:
    return Session(bind=db_engine)


def _att(session, owner, uid, storage_path, memo=None) -> Attachment:
    att = Attachment(
        uid=uid,
        owner_id=owner.id,
        memo_id=None if memo is None else memo.id,
        filename=f"{uid}.png",
        mime="image/png",
        size=3,
        status=AttachmentStatus.ready,
        storage_path=str(storage_path),
    )
    session.add(att)
    session.flush()
    return att


def _seed(session, tmp_path):
    """One row of each class, all three with real files on disk."""
    owner = User(subject="reconcile_owner", role="user")
    session.add(owner)
    session.flush()
    memo = Memo(uid="keepme", owner_id=owner.id, content="x", visibility=Visibility.PUBLIC)
    session.add(memo)
    session.flush()

    dated = tmp_path / "originals" / "2026" / "08" / "2026-08-25_1200_gone" / "leaked.png"
    flat = tmp_path / "originals" / "migrated.png"
    linked = tmp_path / "originals" / "2026" / "08" / "2026-08-25_1300_keepme" / "linked.png"
    for path in (dated, flat, linked):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    return {
        "leaked": _att(session, owner, "leaked", dated),
        "migrated": _att(session, owner, "migrated", flat),
        "linked": _att(session, owner, "linked", linked, memo=memo),
        "paths": {"dated": dated, "flat": flat, "linked": linked},
    }


def test_only_an_orphan_under_a_dated_memo_folder_is_selected(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    session = _session(db_engine)
    try:
        seeded = _seed(session, tmp_path)
        session.commit()
        picked = leaked_orphans(session)
        # The migration orphan is NULL-memo too and must never be picked; the linked row sits in a
        # dated folder and must never be picked either. Only the intersection is a leak.
        assert [a.uid for a in picked] == ["leaked"]
        assert seeded["migrated"].memo_id is None  # the control: it really is an orphan as well
    finally:
        session.close()
        _cleanup()


def test_reclaim_takes_the_row_its_jobs_and_its_files_and_reads_back(
    db_engine, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    session = _session(db_engine)
    try:
        seeded = _seed(session, tmp_path)
        leaked = seeded["leaked"]
        session.add(MediaJob(attachment_id=leaked.id, lane=JobLane.fast, state=JobState.queued))
        session.commit()
        leaked_id = leaked.id

        paths = all_asset_paths(leaked)
        assert seeded["paths"]["dated"] in paths
        done = reclaim_attachment_sync(session, leaked_id, Revalidate.LEAKED_ORPHAN)
        assert done.outcome == "reclaimed"
        assert read_back(session, leaked_id, paths) is True

        assert not seeded["paths"]["dated"].exists()
        assert session.execute(select(MediaJob.id)).scalars().all() == []
        # Everything else is untouched — this is the blast-radius assertion.
        assert sorted(session.execute(select(Attachment.uid)).scalars().all()) == [
            "linked",
            "migrated",
        ]
        assert seeded["paths"]["flat"].exists()
        assert seeded["paths"]["linked"].exists()
    finally:
        session.close()
        _cleanup()


def test_it_refuses_to_run_without_an_explicit_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_SYNC", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert "DATABASE_URL_SYNC" in str(exc.value)


def test_a_victim_that_is_not_a_listed_orphan_is_refused(db_engine, monkeypatch, tmp_path, capsys):
    """The one-victim rule only means something if the id has to be one the run itself listed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL_SYNC", SYNC_URL)
    session = _session(db_engine)
    try:
        seeded = _seed(session, tmp_path)
        session.commit()
        assert main(["--victim", str(seeded["linked"].id)]) == 2
        assert seeded["paths"]["linked"].exists()  # nothing was touched
        assert main([]) == 0  # …and the default run is a dry run that changes nothing
        assert seeded["paths"]["dated"].exists()
        assert "DRY RUN" in capsys.readouterr().out
    finally:
        session.close()
        _cleanup()


# MEMNG-174 — the exit code of a `--victim` run is what a caller acts on: it is the proof step
# between a dry run and `--apply`. Every outcome in which the victim is STILL THERE has to say so
# on stderr and exit nonzero, or a run that reclaimed nothing reads as success.


def test_a_victim_run_with_no_leaked_orphans_at_all_exits_nonzero(
    db_engine, monkeypatch, tmp_path, capsys
):
    """An empty list is the case a caller is least able to notice: nothing was reclaimed, and the
    id it named was never even considered."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL_SYNC", SYNC_URL)
    session = _session(db_engine)
    try:
        _cleanup()
        assert leaked_orphans(session) == []  # the control: the list really is empty
        code = main(["--victim", "4242"])
        out, err = capsys.readouterr()
        assert code != 0, out + err
        assert "4242" in err and "NOT reclaimed" in err
    finally:
        session.close()
        _cleanup()


def test_a_victim_outside_a_non_empty_list_exits_nonzero_with_a_reason(
    db_engine, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL_SYNC", SYNC_URL)
    session = _session(db_engine)
    try:
        seeded = _seed(session, tmp_path)
        session.commit()
        code = main(["--victim", str(seeded["linked"].id)])
        out, err = capsys.readouterr()
        assert code != 0, out + err
        assert str(seeded["linked"].id) in err and "NOT reclaimed" in err
        assert seeded["paths"]["linked"].exists()
    finally:
        session.close()
        _cleanup()


def test_a_victim_another_run_already_took_is_skipped_and_exits_nonzero(
    db_engine, monkeypatch, tmp_path, capsys
):
    """The row was a leaked orphan when this run listed it and is gone by the time the reclaim
    locks it — a second reconcile got there first. Nothing was reclaimed HERE, and a run whose
    whole job was to reclaim one named row must not report that as success."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL_SYNC", SYNC_URL)
    session = _session(db_engine)
    try:
        seeded = _seed(session, tmp_path)
        session.commit()
        leaked_id = seeded["leaked"].id

        real_reclaim = script.reclaim_attachment_sync

        def take_it_first(sess, att_id, mode, **kwargs):
            # A concurrent reconcile claims the row after this run listed it, on its own
            # connection — the race the reclaim re-reads the row under a lock for.
            other = create_engine(SYNC_URL)
            with other.begin() as conn:
                conn.execute(text("DELETE FROM attachments WHERE id = :att"), {"att": att_id})
            other.dispose()
            return real_reclaim(sess, att_id, mode, **kwargs)

        monkeypatch.setattr(script, "reclaim_attachment_sync", take_it_first)

        code = main(["--victim", str(leaked_id)])
        out, err = capsys.readouterr()
        assert code != 0, out + err
        assert str(leaked_id) in err and "NOT reclaimed" in err
        # This run unlinked nothing: it never got as far as owning the row.
        assert seeded["paths"]["dated"].exists()
    finally:
        session.close()
        _cleanup()


def test_a_victim_whose_files_cannot_be_removed_exits_nonzero_with_a_reason(
    db_engine, monkeypatch, tmp_path, capsys
):
    """A failed unlink keeps the row on purpose, so the files stay findable. That is the outcome a
    caller most needs the exit code for."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL_SYNC", SYNC_URL)
    session = _session(db_engine)
    holding_dir = None
    try:
        seeded = _seed(session, tmp_path)
        session.commit()
        leaked_id = seeded["leaked"].id
        holding_dir = seeded["paths"]["dated"].parent
        holding_dir.chmod(0o500)  # a real unlink failure: the directory is not writable

        code = main(["--victim", str(leaked_id)])
        out, err = capsys.readouterr()
        assert code != 0, out + err
        assert str(leaked_id) in err and "NOT reclaimed" in err
        assert seeded["paths"]["dated"].exists()  # kept, so the bytes stay findable
        assert (
            session.execute(select(Attachment.id).where(Attachment.id == leaked_id)).scalar_one()
            == leaked_id
        )
    finally:
        if holding_dir is not None:
            holding_dir.chmod(0o700)
        session.close()
        _cleanup()
