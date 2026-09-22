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
from app.services import memos as memo_service
from app.services import poster, reclaim
from app.services.asset_paths import all_asset_paths
from app.services.reclaim import (
    Revalidate,
    leaked_orphans_stmt,
    reclaim_attachment,
    reclaim_attachment_sync,
    unlink_paths,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, selectinload

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
            retried = reclaim_attachment_sync(session, att_id, Revalidate.LEAKED_ORPHAN)
        assert retried.outcome == "reclaimed"
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
            skipped = reclaim_attachment_sync(deleter, att_id, Revalidate.LEAKED_ORPHAN)
        assert skipped.outcome == "skipped_ineligible"

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
            skipped = reclaim_attachment_sync(deleter, att_id, Revalidate.LEAKED_ORPHAN)
        assert skipped.outcome == "skipped_ineligible"
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]

        # The app's own reclaim does NOT ask that question: it reclaims rows it just orphaned
        # itself, by id, and their paths are not the evidence it is going on.
        with Session(eng) as deleter:
            outcome = reclaim_attachment_sync(deleter, att_id, Revalidate.ORPHANED).outcome
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
            result = await reclaim_attachment(
                db, att_id, Revalidate.ORPHANED, unlink=publish_then_unlink
            )
        assert result.outcome == "reclaimed"
        assert not published.exists()  # taken with everything else
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
        assert _rows("SELECT id FROM media_jobs WHERE attachment_id=:a", a=att_id) == []
    finally:
        eng.dispose()
        _cleanup()


# --- the named delete route: linkage is its ordinary case, not a reason to stand down ----------


def test_deleting_a_linked_attachment_by_name_really_removes_it(
    logged_in_client, monkeypatch, tmp_path
):
    """`DELETE /api/attachments/{uid}` on an attachment that IS on a memo — the route's everyday
    case, and the one no test covered. It must remove the row and the bytes and leave the memo's
    text alone; answering 204 while quietly keeping both is the worst of all outcomes, because
    every retry looks just as successful."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with_one_image(client)
        uid = memo["attachments"][0]["uid"]
        original = Path(_rows("SELECT storage_path FROM attachments WHERE uid=:u", u=uid)[0][0])
        assert original.exists()
        assert _rows("SELECT memo_id FROM attachments WHERE uid=:u", u=uid)[0][0] is not None

        gone = client.delete(f"/api/attachments/{uid}")
        assert gone.status_code == 204, gone.text

        assert _rows("SELECT id FROM attachments WHERE uid=:u", u=uid) == []
        assert not original.exists()
        assert _rows("SELECT id FROM media_jobs") == []
        kept = client.get(f"/api/memos/{memo['uid']}")
        assert kept.status_code == 200 and kept.json()["attachments"] == []
    finally:
        _cleanup()


def test_deleting_an_unlinked_attachment_by_name_still_works(
    logged_in_client, monkeypatch, tmp_path
):
    """The other half of the route's contract, so the mode cannot be 'fixed' by inverting it."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        up = _upload(client, "loose.png", ONE)
        finalized = client.post(f"/api/uploads/{up}/finalize")
        assert finalized.status_code == 200, finalized.text
        uid = finalized.json()["uid"]
        original = Path(_rows("SELECT storage_path FROM attachments WHERE uid=:u", u=uid)[0][0])

        assert client.delete(f"/api/attachments/{uid}").status_code == 204
        assert _rows("SELECT id FROM attachments WHERE uid=:u", u=uid) == []
        assert not original.exists()
    finally:
        _cleanup()


async def test_the_async_skip_path_reports_instead_of_exploding(db_engine, monkeypatch, tmp_path):
    """The memo-delete reclaim meeting a row a concurrent upload has re-claimed: it must stand down
    and SAY so. Reading anything off the row after the rollback that releases it would refresh an
    expired column from an async session that cannot await here — MissingGreenlet, i.e. a 500,
    on the very path built to avoid touching live media."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as seed:
            owner = User(subject="asyncskip_owner", role="user")
            seed.add(owner)
            seed.flush()
            memo = Memo(uid="asyncskip", owner_id=owner.id, content="x")
            seed.add(memo)
            seed.flush()
            att = _seed_orphan(seed, tmp_path, "asyncskip", memo_id=memo.id)
            seed.commit()
            att_id = att.id
            original = Path(att.storage_path)

        async with async_session() as db:
            result = await reclaim_attachment(db, att_id, Revalidate.ORPHANED)
        assert result.outcome == "skipped_ineligible"
        assert original.exists()
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]
    finally:
        eng.dispose()
        _cleanup()


async def test_the_orphaned_reclaim_honours_a_relink_committed_after_the_memo_delete(
    db_engine, monkeypatch, tmp_path
):
    """MEMNG-297, the async half: the route's session ALREADY holds the attachment instance.

    `delete_memo` nulls the FK on the loaded children in this very session and the sessionmaker does
    not expire on commit, so `memo_id` is None on that instance whatever the database now says. A
    re-attach committed by another connection between the memo delete's commit and the locked
    re-select must still reach `_ineligible()` — otherwise the route deletes a photo that belongs to
    a live memo again, one identity-map hit away from the script's defect.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as seed:
            owner = User(subject="asyncrelink_owner", role="user")
            seed.add(owner)
            seed.flush()
            doomed = Memo(uid="asyncrelink_doomed", owner_id=owner.id, content="x")
            live = Memo(uid="asyncrelink_live", owner_id=owner.id, content="y")
            seed.add_all([doomed, live])
            seed.flush()
            att = _seed_orphan(seed, tmp_path, "asyncrelink", memo_id=doomed.id)
            seed.commit()
            att_id, doomed_id, live_id = att.id, doomed.id, live.id
            original = Path(att.storage_path)

        seen: list[int | None] = []
        real_ineligible = reclaim._ineligible

        def spy(att_arg, mode, **fences):  # the decision point, not just the end state
            seen.append(att_arg.memo_id)
            return real_ineligible(att_arg, mode, **fences)

        monkeypatch.setattr(reclaim, "_ineligible", spy)

        async with async_session() as db:
            memo = (
                await db.execute(
                    select(Memo).where(Memo.id == doomed_id).options(selectinload(Memo.attachments))
                )
            ).scalar_one()
            orphaned = await memo_service.delete_memo(db, memo)
            assert orphaned == [att_id]
            await db.commit()  # PHASE 1 exactly as app/routers/memos.py runs it

            with Session(eng) as claimer:  # …and the photo is re-attached, on its own connection
                claimer.execute(
                    text("UPDATE attachments SET memo_id = :m WHERE id = :a"),
                    {"m": live_id, "a": att_id},
                )
                claimer.commit()

            result = await reclaim.reclaim_attachment(db, att_id, reclaim.Revalidate.ORPHANED)

        assert seen == [live_id], "the revalidation decided about a memo_id that had passed"
        assert result.outcome == "skipped_ineligible"
        assert original.exists(), "a live memo's photo was deleted"
        assert _rows("SELECT memo_id FROM attachments WHERE id=:a", a=att_id) == [(live_id,)]
    finally:
        eng.dispose()
        _cleanup()


# --- an unlink that genuinely fails must not take the row with it -----------------------------


def test_an_unlink_failure_keeps_the_row_and_reports_it(logged_in_client, monkeypatch, tmp_path):
    """A read-only mount, EPERM, a transient I/O error: the files stay, so the row — the only
    record of where they are — must stay too. Deleting it here would strand them on precisely the
    failure the journal exists to survive."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with_one_image(client)
        uid = memo["attachments"][0]["uid"]
        original = Path(_rows("SELECT storage_path FROM attachments WHERE uid=:u", u=uid)[0][0])

        def refuse(paths):
            return [str(p) for p in paths if p.exists()]  # every real file "cannot be removed"

        monkeypatch.setattr(reclaim, "unlink_paths", refuse)
        refused = client.delete(f"/api/attachments/{uid}")
        assert refused.status_code == 500  # …and NOT a 204 claiming the media is gone

        assert _rows("SELECT id FROM attachments WHERE uid=:u", u=uid) != []  # journal kept
        assert original.exists()
        assert _rows("SELECT id FROM media_jobs") != []  # the whole transaction unwound
    finally:
        _cleanup()


async def test_a_partial_unlink_failure_keeps_the_row_and_the_retry_finishes_it(
    db_engine, monkeypatch, tmp_path
):
    """Some files removed, one refused. The row stays, so the retry re-derives the FULL set and
    ENOENT-tolerates everything the first attempt already took."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    try:
        with Session(eng) as seed:
            seed.add(User(subject="partial_owner", role="user"))
            seed.flush()
            att = _seed_orphan(seed, tmp_path, "partial")
            seed.commit()
            att_id, att_uid = att.id, att.uid
            original = Path(att.storage_path)

        stubborn = poster.poster_path(att_uid)
        stubborn.parent.mkdir(parents=True, exist_ok=True)
        stubborn.write_bytes(b"this one will not go")

        def unlink_all_but_one(paths: list[Path]) -> list[str]:
            failed = []
            for path in paths:
                if path == stubborn:
                    failed.append(str(path))
                    continue
                path.unlink(missing_ok=True)
            return failed

        async with async_session() as db:
            first = await reclaim_attachment(
                db, att_id, Revalidate.ORPHANED, unlink=unlink_all_but_one
            )
        assert first.outcome == "failed_unlink"
        assert first.failed_paths == (str(stubborn),)
        assert not original.exists()  # this one really did go
        assert stubborn.exists()
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == [(att_id,)]

        # The retry: the already-gone paths are absent, which is success, and the stubborn one is
        # removable this time. Nothing about the first attempt makes the second harder.
        async with async_session() as db:
            second = await reclaim_attachment(db, att_id, Revalidate.ORPHANED)
        assert second.outcome == "reclaimed"
        assert second.failed_paths == ()
        assert not stubborn.exists()
        assert _rows("SELECT id FROM attachments WHERE id=:a", a=att_id) == []
    finally:
        eng.dispose()
        _cleanup()


# --- the class, not just these sites ----------------------------------------------------------


APP_DIR = Path(__file__).resolve().parent.parent / "app"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SEAM = ("app/services/reclaim.py",)


def _attachment_deleters(root: Path, package: str, *, exempt: tuple[str, ...] = SEAM) -> list[str]:
    """Every place a tree deletes an Attachment row, found by the ENTITY rather than by what the
    variable happens to be called.

    Two shapes count: `session.delete(x)` where `x` was bound from something naming `Attachment`
    (a `select(Attachment)` result, an `Attachment(...)`, or a parameter annotated `Attachment`),
    and a bulk `delete(Attachment)` statement. Matching the entity is what makes a rename — or a
    bulk DELETE, or a new deleter under scripts/ — unable to slip past this.

    Known bound: it matches the IDENTIFIER `Attachment`, so aliasing the import
    (`from app.models import Attachment as A`) would evade it. Nothing in this codebase aliases a
    model import, and the shape the advisory named — the same entity under a different local
    variable — is caught.
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = f"{package}/{path.relative_to(root)}"
        if rel in exempt:
            continue  # the seam itself
        tree = ast.parse(path.read_text(), filename=str(path))
        for scope in [tree, *[n for n in ast.walk(tree) if isinstance(n, FUNCTION_NODES)]]:
            bound = _names_bound_to_attachment(scope)
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func, arg = node.func, node.args[0]
                is_orm_delete = isinstance(func, ast.Attribute) and func.attr == "delete"
                is_bulk_delete = isinstance(func, ast.Name) and func.id in {"delete", "sa_delete"}
                names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                if is_bulk_delete and "Attachment" in names:
                    offenders.append(f"{rel}:{node.lineno}")
                elif is_orm_delete and isinstance(arg, ast.Name) and arg.id in bound:
                    offenders.append(f"{rel}:{node.lineno}")
    return sorted(set(offenders))


FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _names_bound_to_attachment(scope: ast.AST) -> set[str]:
    """Local names that hold an Attachment: assigned from an expression naming `Attachment`, or a
    parameter annotated with it. `AttachmentRendition` is a DIFFERENT entity and must not match, so
    this compares identifiers rather than searching text."""
    bound: set[str] = set()
    if isinstance(scope, FUNCTION_NODES):
        for arg in [*scope.args.args, *scope.args.kwonlyargs]:
            names = (
                {n.id for n in ast.walk(arg.annotation) if isinstance(n, ast.Name)}
                if (arg.annotation is not None)
                else set()
            )
            if "Attachment" in names:
                bound.add(arg.arg)
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and node.value is not None:
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if "Attachment" in names:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound.add(target.id)
    return bound


def test_only_the_reclaim_seam_deletes_an_attachment_row():
    """Every attachment-row deletion goes through `reclaim`, so the ordering it documents is the
    ONLY ordering in the codebase — app AND the maintenance scripts. A new deleter elsewhere is how
    the forbidden state comes back, and it fails here rather than in production."""
    offenders = _attachment_deleters(APP_DIR, "app") + _attachment_deleters(SCRIPTS_DIR, "scripts")
    assert offenders == [], (
        f"attachment row deleted outside the reclaim seam: {offenders} — route it through "
        "app/services/reclaim.py so its files go first"
    )


def test_the_deleter_guard_actually_recognises_a_deletion():
    """The positive control: without it, a guard that matched nothing would also pass. The seam's
    own module contains exactly the deletions this scan is built to find."""
    seam = _attachment_deleters(APP_DIR, "app", exempt=())  # the same scan, exemption removed
    assert [line for line in seam if "reclaim.py" in line], (
        "the scan no longer recognises the seam's own attachment deletes — it has stopped working"
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
            gone = reclaim_attachment_sync(session, 987654321, Revalidate.ORPHANED)
        assert gone.outcome == "vanished"
    finally:
        eng.dispose()
