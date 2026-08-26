"""MEMNG-170 — the reconcile script's selection predicate, which decides what gets deleted.

Two kinds of row carry `memo_id IS NULL` on this instance and they must NOT be treated alike:
media the 0.29.1 migration brought over (legitimate orphans, originals in the FLAT `originals/`
bucket) and attachments a deleted memo left behind (originals under a dated
`originals/YYYY/MM/<memo-folder>/`, where only `link_attachments` ever writes). Getting that
predicate wrong deletes a household's migrated photos, so it is pinned against seeded rows of both
classes before the script is ever pointed at production.
"""

import pytest
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
