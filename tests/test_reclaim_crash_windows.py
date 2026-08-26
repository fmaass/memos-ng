"""Crash windows around deleting an attachment: every one must leave a row that still names its
bytes, or a row whose bytes are already gone — never bytes with no row.

An attachment row IS the journal for its files (their paths are derived from it), so a row deleted
before its bytes takes the only map to them with it. `app/services/reclaim.py` states the principle
and owns the order; these tests interrupt that order at each seam and check which of the two
allowed states comes out.

The interruptions are injected at real seams — a stubbed unlink, a stubbed reclaim, a concurrent
session — never timed, so nothing here depends on how long a rename takes.
"""

import ast
from pathlib import Path

from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob, Memo, User
from app.services import poster, reclaim
from app.services.asset_paths import all_asset_paths
from app.services.reclaim import (
    leaked_orphans_stmt,
    reclaim_attachment,
    reclaim_attachment_sync,
    unlink_paths,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"
ONE = PNG + b"one" * 700


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for table in ("media_jobs", "attachment_renditions", "attachments", "memos", "uploads"):
            c.execute(text(f"DELETE FROM {table}"))
    eng.dispose()


def _rows(sql: str, **params) -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text(sql), params)]
    eng.dispose()
    return out


def _upload(client, name: str, data: bytes) -> str:
    opened = client.post(
        "/api/uploads", json={"filename": name, "mime": "image/png", "size": len(data)}
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    return uid


def _memo_with_one_image(client) -> dict:
    up = _upload(client, "p.png", ONE)
    made = client.post(
        "/api/memos", json={"content": "pic", "visibility": "PUBLIC", "upload_uids": [up]}
    )
    assert made.status_code == 201, made.text
    return made.json()


def _orphan_rows() -> list[tuple]:
    """What the reconcile predicate can see, read through its own statement."""
    eng = create_engine(SYNC_URL)
    with Session(eng) as session:
        out = [(a.id, a.uid) for a in session.execute(leaked_orphans_stmt()).scalars().all()]
    eng.dispose()
    return out


# --- (i) crash between the memo commit and the reclaim ---------------------------------------


def test_a_crash_before_the_reclaim_leaves_a_reconcilable_orphan(
    logged_in_client, monkeypatch, tmp_path
):
    """The memo delete commits first ON PURPOSE, leaving its attachments orphaned rather than
    deleted. Interrupted here, the row is still there naming its own files — which is precisely
    what the reconcile predicate selects, so the leak is recoverable instead of invisible."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with_one_image(client)
        uid = memo["attachments"][0]["uid"]
        original = Path(_rows("SELECT storage_path FROM attachments WHERE uid=:u", u=uid)[0][0])
        assert original.exists()

        async def crash(*args, **kwargs):
            raise RuntimeError("the process died between the commit and the reclaim")

        monkeypatch.setattr(reclaim, "reclaim_attachment", crash)
        gone = client.delete(f"/api/memos/{memo['uid']}")
        assert gone.status_code == 500  # the request died; the memo delete is already committed

        assert _rows("SELECT id FROM memos") == []  # phase 1 landed
        orphans = _rows("SELECT uid, memo_id FROM attachments")
        assert orphans == [(uid, None)]  # …and left the journal behind, not a hole
        assert original.exists()  # bytes still named by that row
        assert [row[1] for row in _orphan_rows()] == [uid]  # the sweep can find it
    finally:
        _cleanup()


# --- (ii) crash between the unlink and the row delete ----------------------------------------


def test_a_crash_between_the_unlink_and_the_row_delete_leaves_a_dangling_row(
    logged_in_client, monkeypatch, tmp_path
):
    """The other side of the same window, and the one the ordering deliberately chooses: files go
    first, so an interruption leaves a row whose bytes are already gone. Harmless, visible, and
    finished off by simply running the reclaim again."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with_one_image(client)
        uid = memo["attachments"][0]["uid"]
        original = Path(_rows("SELECT storage_path FROM attachments WHERE uid=:u", u=uid)[0][0])

        real_unlink = unlink_paths

        def unlink_then_die(paths):
            real_unlink(paths)  # the bytes really do go — this is not a no-op stub
            raise RuntimeError("the process died after unlinking, before the row delete")

        monkeypatch.setattr(reclaim, "unlink_paths", unlink_then_die)
        died = client.delete(f"/api/memos/{memo['uid']}")
        assert died.status_code == 500

        assert not original.exists()  # bytes gone
        assert _rows("SELECT uid FROM attachments") == [(uid,)]  # …row survives: state (b)
        assert [row[1] for row in _orphan_rows()] == [uid]

        # …and a retry finishes the job rather than tripping over the missing file.
        monkeypatch.undo()
        monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
        att_id = _rows("SELECT id FROM attachments")[0][0]
        eng = create_engine(SYNC_URL)
        with Session(eng) as session:
            assert reclaim_attachment_sync(session, att_id) == "reclaimed"
        eng.dispose()
        assert _rows("SELECT id FROM attachments") == []
    finally:
        _cleanup()


# --- (iii) a row claimed by a concurrent write between selection and delete -------------------


def _seed_orphan(session: Session, tmp_path, uid: str, memo_id=None) -> Attachment:
    original = tmp_path / "originals" / "2026" / "08" / "2026-08-26_1200_x" / f"{uid}.png"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(ONE)
    att = Attachment(
        uid=uid,
        owner_id=session.execute(select(User.id)).scalars().first(),
        memo_id=memo_id,
        filename=f"{uid}.png",
        mime="image/png",
        size=len(ONE),
        status=AttachmentStatus.ready,
        storage_path=str(original),
    )
    session.add(att)
    session.flush()
    return att


def test_a_row_relinked_after_selection_is_skipped_not_deleted(db_engine, monkeypatch, tmp_path):
    """The script lists orphans, then deletes them one at a time. A dedup-claiming upload can
    re-link one in between — and deleting it then destroys a LIVE memo's photo while the read-back
    happily reports success. The reclaim re-reads the row under its own lock and stands down."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as seed:
            owner = User(subject="relink_owner", role="user")
            seed.add(owner)
            seed.flush()
            memo = Memo(uid="livememo", owner_id=owner.id, content="x")
            seed.add(memo)
            seed.flush()
            att = _seed_orphan(seed, tmp_path, "claimed")
            seed.commit()
            att_id, memo_id = att.id, memo.id
            original = Path(att.storage_path)

        # It IS listed as garbage at selection time…
        with Session(eng) as reader:
            assert [a.id for a in reader.execute(leaked_orphans_stmt()).scalars().all()] == [att_id]

        # …and then a concurrent write claims it, exactly as finalize's dedup would.
        with Session(eng) as claimer:
            claimer.execute(
                text("UPDATE attachments SET memo_id = :m WHERE id = :a"),
                {"m": memo_id, "a": att_id},
            )
            claimer.commit()

        with Session(eng) as deleter:
            assert reclaim_attachment_sync(deleter, att_id) == "skipped_relinked"

        assert original.exists(), "a live memo's photo was deleted"
        assert _rows("SELECT memo_id FROM attachments WHERE id=:a", a=att_id) == [(memo_id,)]
    finally:
        eng.dispose()
        _cleanup()


def test_a_row_whose_path_left_the_predicate_is_also_skipped(db_engine, monkeypatch, tmp_path):
    """The path half of the same revalidation: a blind scan's selection is the oldest thing it
    acts on, so both halves of the predicate are re-checked, not just `memo_id`."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as seed:
            seed.add(User(subject="repath_owner", role="user"))
            seed.flush()
            att = _seed_orphan(seed, tmp_path, "repathed")
            seed.commit()
            att_id = att.id
        with Session(eng) as mover:
            mover.execute(
                text("UPDATE attachments SET storage_path = :p WHERE id = :a"),
                {"p": str(tmp_path / "originals" / "flat.png"), "a": att_id},
            )
            mover.commit()
        with Session(eng) as deleter:
            assert reclaim_attachment_sync(deleter, att_id) == "skipped_relinked"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]

        # The app's own reclaim does NOT ask that question: it reclaims rows it just orphaned
        # itself, by id, and their paths are not the evidence it is going on.
        with Session(eng) as deleter:
            outcome = reclaim_attachment_sync(deleter, att_id, require_leaked_path=False)
        assert outcome == "reclaimed"
    finally:
        eng.dispose()
        _cleanup()


# --- (iv) a tier published after the path set is collected ------------------------------------


async def test_a_tier_published_during_the_reclaim_is_still_unlinked(
    db_engine, monkeypatch, tmp_path
):
    """The path set is DERIVABLE, not observed. A worker that publishes a poster between the
    collection and the unlink would survive a set filtered by `exists()` — and survive with no row,
    the one forbidden outcome. Publishing from inside the unlink seam is that race, made exact."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as seed:
            seed.add(User(subject="latetier_owner", role="user"))
            seed.flush()
            att = _seed_orphan(seed, tmp_path, "latetier")
            seed.add(MediaJob(attachment_id=att.id, lane=JobLane.fast, state=JobState.queued))
            seed.commit()
            att_id, att_uid = att.id, att.uid

        published = poster.poster_path(att_uid)
        assert not published.exists()  # nothing there when the reclaim collects its paths

        def publish_then_unlink(paths: list[Path]) -> list[str]:
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_bytes(b"a tier the worker published just now")
            assert published in paths, "the collected set was filtered by existence"
            return unlink_paths(paths)

        # A real, independently-committing session — not the rolled-back `adb` fixture: the
        # reclaim commits its own transaction, and that commit has to actually release the row
        # locks it took, which is half of what this test is checking.
        async with async_session() as db:
            outcome = await reclaim_attachment(db, att_id, unlink=publish_then_unlink)
        assert outcome == "reclaimed"
        assert not published.exists()  # taken with everything else
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert _rows("SELECT id FROM media_jobs WHERE attachment_id=:a", a=att_id) == []
    finally:
        eng.dispose()
        _cleanup()


# --- the class, not just these sites ----------------------------------------------------------


APP_DIR = Path(__file__).resolve().parent.parent / "app"


def test_only_the_reclaim_seam_deletes_an_attachment_row():
    """Every attachment-row deletion in the app goes through `reclaim`, so the ordering above is
    the ONLY ordering in the codebase. A new `db.delete(some_attachment)` elsewhere is how the
    forbidden state comes back, and it fails here rather than in production."""
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = str(path.relative_to(APP_DIR))
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "delete" and node.args:
                target = node.args[0]
                name = target.id if isinstance(target, ast.Name) else None
                if name in {"att", "attachment"} and rel != "services/reclaim.py":
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        f"attachment row deleted outside the reclaim seam: {offenders} — route it through "
        "app/services/reclaim.py so its files go first"
    )


def test_the_derivable_path_set_is_not_filtered_by_what_exists():
    """The property the late-tier case rests on, asserted directly: `all_asset_paths` answers with
    every path an attachment COULD own, including the ones that are empty right now."""
    att = Attachment(uid="nofiles", filename="x.png", mime="image/png", size=1, storage_path=None)
    paths = all_asset_paths(att)
    assert paths, "an attachment with no files on disk still owns a derivable set"
    assert not any(path.exists() for path in paths)


def test_reclaiming_a_row_that_is_already_gone_is_not_an_error(db_engine):
    """Idempotence at the other end: two sweeps racing the same orphan, or a retry after a
    successful-but-uncommitted-looking run."""
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as session:
            assert reclaim_attachment_sync(session, 987654321) == "vanished"
    finally:
        eng.dispose()
