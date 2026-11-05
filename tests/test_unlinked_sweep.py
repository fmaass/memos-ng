"""MEMNG-968 — attachments a standalone finalize made and no memo ever took.

`POST /api/uploads/{uid}/finalize` stages its original in `originals/_unfiled/` and leaves the row
with no memo; only `link_attachments` moves it out. A row still there a day later is counted, and
three days later it is reclaimed — but `memo_id IS NULL` alone also matches the 0.29.1 imports
(flat `originals/`) and the leftovers of old memo deletes (dated memo folders), which are household
archive media this sweep may never touch. These tests hold the sweep to the positive discriminator
and to the exclusions: a row the owner has put in an album, used as a cover, tagged, given a
print rendition or hosted in a digest is kept at any age.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app import worker
from app.config import settings
from app.services import upload_queue_metrics as qm
from app.services.reclaim import Revalidate, reclaim_attachment
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.conftest import SYNC_URL

H = timedelta(hours=1)
PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def _never_linked_sweep_on(monkeypatch):
    """Every test here runs with the deleting half switched on (MEMNG-1203): these are the aging
    rules the sweep applies once the owner turns it on, and the hourly wrapper skips it while
    `NEVER_LINKED_SWEEP_ENABLED` is off. The off state has its own tests in
    test_never_linked_switch.py."""
    monkeypatch.setattr(settings, "NEVER_LINKED_SWEEP_ENABLED", True)


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "TRUNCATE digest_hosted_media, collection_covers, collection_members, collections, "
                "attachment_tags, tags, attachment_renditions, media_jobs, attachments, memo_tags, "
                "reactions, memos, uploads RESTART IDENTITY CASCADE"
            )
        )
    eng.dispose()


def _rows(sql: str, **params) -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text(sql), params)]
    eng.dispose()
    return out


def _exec(sql: str, **params) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text(sql), params)
    eng.dispose()


def _owner(subject: str = "unlinked_owner") -> int:
    eng = create_engine(SYNC_URL)
    with Session(eng) as s:
        found = s.execute(
            text("SELECT id FROM users WHERE subject = :s"), {"s": subject}
        ).scalar_one_or_none()
        if found is None:
            found = s.execute(
                text("INSERT INTO users (subject, role) VALUES (:s, 'user') RETURNING id"),
                {"s": subject},
            ).scalar_one()
            s.commit()
    eng.dispose()
    return found


def _seed(
    root: Path,
    uid: str,
    *,
    age: timedelta,
    where: str = "unfiled",
    detached_at: datetime | None = None,
    sha256: str | None = None,
    owner: int | None = None,
) -> tuple[int, Path]:
    """One memo-less attachment with a real original, its `created_at` set `age` in the past.

    `where` is the bucket its original sits in: `unfiled` (a standalone finalize), `flat` (a 0.29.1
    import), `memo` (a dated memo folder), or a literal path relative to `root`."""
    rel = {
        "unfiled": Path("originals") / "_unfiled" / f"{uid}_shot.png",
        "flat": Path("originals") / f"{uid}.png",
        "memo": Path("originals") / "2026" / "08" / f"2026-08-25_1200_{uid}" / f"{uid}.png",
    }.get(where, Path(where))
    original = root / rel
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(PNG + uid.encode())
    owner_id = owner if owner is not None else _owner()
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        att_id = c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
                "storage_path, detached_at, sha256, created_at) VALUES (:u, :o, NULL, :f, "
                "'image/png', 3, 'ready', :p, :d, :h, :c) RETURNING id"
            ),
            {
                "u": uid,
                "o": owner_id,
                "f": f"{uid}.png",
                "p": str(original),
                "d": detached_at,
                "h": sha256,
                "c": datetime.now(UTC) - age,
            },
        ).scalar_one()
    eng.dispose()
    return att_id, original


def _exists(att_id: int) -> bool:
    return _rows("SELECT id FROM attachments WHERE id = :a", a=att_id) == [(att_id,)]


async def _gauge() -> tuple[int, int]:
    state = await qm.read_state()
    return state.orphaned_counted, state.orphaned_reclaimable


# --- the ticket's ages -------------------------------------------------------------------------


async def test_a_never_linked_row_at_25h_is_counted_and_kept(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "day", age=25 * H)

        assert await _gauge() == (1, 0)
        assert await worker.sweep_unlinked_attachments() == 0

        assert _exists(att_id)
        assert original.exists()
    finally:
        _cleanup()


async def test_a_never_linked_row_at_73h_is_reclaimed(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "old", age=73 * H)

        assert await _gauge() == (1, 1)
        assert await worker.sweep_unlinked_attachments() == 1

        assert not _exists(att_id)
        assert not original.exists()
        assert await _gauge() == (0, 0)
    finally:
        _cleanup()


async def test_a_fresh_row_is_neither_counted_nor_reclaimed(db_engine, monkeypatch, tmp_path):
    """Boundary: inside the first day the row is a compose in progress, not an orphan."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, _ = _seed(tmp_path, "fresh", age=23 * H)

        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0
        assert _exists(att_id)
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("other_owner", "other_sha", "kept"),
    [
        pytest.param(False, False, True, id="same-owner-same-sha"),
        pytest.param(True, False, False, id="other-owner-same-sha"),
        pytest.param(False, True, False, id="same-owner-other-sha"),
    ],
)
async def test_an_open_session_for_the_same_bytes_holds_the_row(
    db_engine, monkeypatch, tmp_path, other_owner, other_sha, kept
):
    """A same-owner session declaring these bytes may be about to stage onto or dedup into this row
    (MEMNG-959), so the row is no orphan while it is open. Another owner's session, or the same
    owner's session for other bytes, says nothing about it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        sha = "a" * 64
        att_id, original = _seed(tmp_path, "held", age=73 * H, sha256=sha)
        session_owner = _owner("someone_else") if other_owner else _owner()
        _exec(
            "INSERT INTO uploads (upload_uid, owner_id, filename, mime, declared_size, "
            "bytes_received, sha256, capture_state, expires_at) VALUES ('open-1', :o, 'x.png', "
            "'image/png', 3, 0, :h, 'pending', now() + interval '1 day')",
            o=session_owner,
            h="b" * 64 if other_sha else sha,
        )

        assert await _gauge() == ((0, 0) if kept else (1, 1))
        assert await worker.sweep_unlinked_attachments() == (0 if kept else 1)
        assert _exists(att_id) is kept
        assert original.exists() is kept

        if kept:
            # Not vacuous: the session ending is all that stood between the row and the sweep.
            _exec("DELETE FROM uploads")
            assert await worker.sweep_unlinked_attachments() == 1
            assert not _exists(att_id)
    finally:
        _cleanup()


# --- what the sweep may never touch ------------------------------------------------------------


@pytest.mark.parametrize("where", ["flat", "memo"])
async def test_a_migration_or_memo_folder_orphan_is_never_touched(
    db_engine, monkeypatch, tmp_path, where
):
    """The 0.29.1 imports sit FLAT in `originals/`; a pre-MEMNG-170 leftover sits in a dated memo
    folder. Both carry `memo_id` NULL and `detached_at` NULL, and neither is this sweep's — at any
    age."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, f"archive-{where}", age=1000 * 24 * H, where=where)

        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0

        assert _exists(att_id)
        assert original.exists()
    finally:
        _cleanup()


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("originals/_unfiled/nested/deep.png", id="below-unfiled"),
        pytest.param("originals/Xunfiled/like-wildcard.png", id="like-underscore-wildcard"),
        pytest.param("originals/x_unfiled_name.png", id="unfiled-inside-a-flat-name"),
        pytest.param(
            "elsewhere/originals/_unfiled/other-root.png", id="unfiled-under-another-root"
        ),
    ],
)
async def test_only_a_file_directly_in_unfiled_under_the_data_root_matches(
    db_engine, monkeypatch, tmp_path, rel
):
    """The discriminator is `storage.is_unfiled` — the PARENT is `<DATA_ROOT>/originals/_unfiled`.
    The token `_unfiled` inside a name, below that folder or under another root is not it."""
    root = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_ROOT", str(root))
    try:
        target = (tmp_path / rel) if rel.startswith("elsewhere/") else (root / rel)
        att_id, original = _seed(root, "lookalike", age=100 * 24 * H, where=str(target))

        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0
        assert _exists(att_id)
        assert original.exists()
    finally:
        _cleanup()


async def test_a_detached_row_in_unfiled_is_left_to_the_detached_sweep(
    db_engine, monkeypatch, tmp_path
):
    """Scope: a row with a detach instant belongs to `sweep_detached_attachments`, which judges it
    by THAT clock; this sweep's creation-age clock must not reach it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, _ = _seed(tmp_path, "detached", age=100 * H, detached_at=datetime.now(UTC))

        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0
        assert _exists(att_id)
    finally:
        _cleanup()


def _album(att_id: int) -> int:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        made = c.execute(
            text(
                "INSERT INTO collections (uid, title, kind) "
                "VALUES ('alb-' || :a, 'Album', 'album') RETURNING id"
            ),
            {"a": att_id},
        ).scalar_one()
    eng.dispose()
    return made


def _exclude(kind: str, att_id: int, root: Path) -> None:
    if kind == "collection_member":
        _exec(
            "INSERT INTO collection_members (collection_id, attachment_id) VALUES (:c, :a)",
            c=_album(att_id),
            a=att_id,
        )
    elif kind == "collection_cover":
        _exec(
            "INSERT INTO collection_covers (kind, key, attachment_id) VALUES ('year', '2026', :a)",
            a=att_id,
        )
    elif kind == "album_cover":
        _exec(
            "UPDATE collections SET cover_attachment_id = :a WHERE id = :c",
            a=att_id,
            c=_album(att_id),
        )
    elif kind == "attachment_tag":
        _exec("INSERT INTO tags (name) VALUES ('kept')")
        _exec(
            "INSERT INTO attachment_tags (attachment_id, tag_id) "
            "SELECT :a, id FROM tags WHERE name = 'kept'",
            a=att_id,
        )
    elif kind == "rendition":
        print_master = root / "originals" / "_unfiled" / "print.tif"
        print_master.write_bytes(b"tif")
        _exec(
            "INSERT INTO attachment_renditions (attachment_id, variant_key, filename, mime, size, "
            "storage_path) VALUES (:a, 'print', 'print.tif', 'image/tiff', 3, :p)",
            a=att_id,
            p=str(print_master),
        )
    elif kind == "print_path":
        _exec(
            "UPDATE attachments SET print_path = :p WHERE id = :a", p=str(root / "p.tif"), a=att_id
        )
    elif kind == "digest_hosted":
        _exec(
            "INSERT INTO digest_hosted_media (attachment_id, kind, dropbox_path, status) "
            "VALUES (:a, 'image', '/digest/' || :a, 'uploaded')",
            a=att_id,
        )
    else:  # pragma: no cover - a typo in the parametrization
        raise AssertionError(kind)


EXCLUSIONS = [
    "collection_member",
    "collection_cover",
    "album_cover",
    "attachment_tag",
    "rendition",
    "print_path",
    "digest_hosted",
]


@pytest.mark.parametrize("kind", EXCLUSIONS)
async def test_a_row_the_owner_kept_is_never_counted_or_reclaimed(
    db_engine, monkeypatch, tmp_path, kind
):
    """The library lists an owner's orphans, so a never-linked row can still be a picture somebody
    chose to keep: put in an album, used as a cover, tagged, given a print master, hosted in a
    digest. Any of those takes it out of the population for good."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, f"kept-{kind}", age=1000 * H)
        _exclude(kind, att_id, tmp_path)

        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0
        assert _exists(att_id)
        assert original.exists()
    finally:
        _cleanup()


# --- under the row lock ------------------------------------------------------------------------


@pytest.mark.parametrize("kind", EXCLUSIONS)
async def test_an_exclusion_that_lands_after_selection_is_honoured_under_the_lock(
    db_engine, monkeypatch, tmp_path, kind
):
    """The pass selects without a lock; the reclaim re-asks the WHOLE predicate under the row's
    lock against the pass's own cutoff, so an album add between the two keeps the row."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, f"late-{kind}", age=100 * H)
        _exclude(kind, att_id, tmp_path)
        from app.database import async_session

        async with async_session() as db:
            result = await reclaim_attachment(
                db,
                att_id,
                Revalidate.NEVER_LINKED,
                created_before=datetime.now(UTC) - 72 * H,
            )
        assert result.outcome == "skipped_ineligible"
        assert _exists(att_id)
        assert original.exists()
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("cutoff_age", "outcome"),
    [
        pytest.param(None, "skipped_ineligible", id="no-cutoff-fails-closed"),
        pytest.param(110 * H, "skipped_ineligible", id="younger-than-the-cutoff"),
        pytest.param(72 * H, "reclaimed", id="older-than-the-cutoff"),
    ],
)
async def test_the_never_linked_mode_judges_the_cutoff_it_is_handed(
    db_engine, monkeypatch, tmp_path, cutoff_age, outcome
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, _ = _seed(tmp_path, "cutoff", age=100 * H)
        from app.database import async_session

        async with async_session() as db:
            result = await reclaim_attachment(
                db,
                att_id,
                Revalidate.NEVER_LINKED,
                created_before=None if cutoff_age is None else datetime.now(UTC) - cutoff_age,
            )
        assert result.outcome == outcome
        assert _exists(att_id) is (outcome != "reclaimed")
    finally:
        _cleanup()


async def test_a_failed_unlink_keeps_the_row(db_engine, monkeypatch, tmp_path):
    """Error path: the reclaim fails closed and the sweep does not count the row as reclaimed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "stuck", age=100 * H)

        def refuse(paths: list[Path]) -> list[str]:
            return [str(p) for p in paths]

        assert await worker.sweep_unlinked_attachments(unlink=refuse) == 0
        assert _exists(att_id)
        assert original.exists()
    finally:
        _cleanup()


async def test_a_failing_detached_sweep_does_not_skip_the_unlinked_one(
    db_engine, monkeypatch, tmp_path
):
    """The two passes share one bounded step; an error in the first is isolated, not a reason to
    skip the second."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    async def boom(**_kwargs) -> int:
        raise RuntimeError("detached sweep down")

    monkeypatch.setattr(worker, "sweep_detached_attachments", boom)
    try:
        att_id, _ = _seed(tmp_path, "second", age=100 * H)

        assert await worker.sweep_detached_attachments_safe() == 1
        assert not _exists(att_id)
    finally:
        _cleanup()


async def test_two_passes_racing_reclaim_the_row_once(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "raced", age=100 * H)

        first, second = await asyncio.gather(
            worker.sweep_unlinked_attachments(), worker.sweep_unlinked_attachments()
        )

        assert first + second == 1
        assert not _exists(att_id)
        assert not original.exists()
    finally:
        _cleanup()


# --- the real route, end to end ----------------------------------------------------------------


def _finalized_orphan(client, name: str, data: bytes) -> str:
    opened = client.post(
        "/api/uploads", json={"filename": name, "mime": "image/png", "size": len(data)}
    )
    assert opened.status_code == 201, opened.text
    upload_uid = opened.json()["upload_uid"]
    sent = client.patch(f"/api/uploads/{upload_uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    done = client.post(f"/api/uploads/{upload_uid}/finalize")
    assert done.status_code == 200, done.text
    return done.json()["uid"]


def _age(uid: str, age: timedelta) -> None:
    _exec("UPDATE attachments SET created_at = :c WHERE uid = :u", c=datetime.now(UTC) - age, u=uid)


async def test_a_create_memo_retried_at_48h_with_its_key_links_the_row(
    logged_in_client, monkeypatch, tmp_path
):
    """The ticket's AC on the real route: a standalone-finalized row is still there at 48 h, a
    createMemo naming it with an idempotency key succeeds, and its retry with the same key answers
    with the same memo. A sibling orphan the same client left at 73 h is reclaimed in the same
    pass — which also proves the predicate matches what the route actually writes."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        kept = _finalized_orphan(client, "kept.png", PNG + b"kept" * 300)
        dropped = _finalized_orphan(client, "dropped.png", PNG + b"dropped" * 300)
        dropped_path = Path(
            _rows("SELECT storage_path FROM attachments WHERE uid = :u", u=dropped)[0][0]
        )
        _age(kept, 48 * H)
        _age(dropped, 73 * H)

        assert await _gauge() == (2, 1)
        assert await worker.sweep_unlinked_attachments() == 1
        assert _rows("SELECT uid FROM attachments WHERE uid = :u", u=dropped) == []
        assert not dropped_path.exists()

        body = {
            "content": "two days later",
            "visibility": "PRIVATE",
            "attachment_uids": [kept],
            "idempotency_key": "retry-at-48h-key",
        }
        first = client.post("/api/memos", json=body)
        assert first.status_code == 201, first.text
        again = client.post("/api/memos", json=body)
        assert again.status_code in (200, 201), again.text
        assert again.json()["uid"] == first.json()["uid"]
        assert [a["uid"] for a in again.json()["attachments"]] == [kept]

        linked = _rows("SELECT memo_id, storage_path FROM attachments WHERE uid = :u", u=kept)
        assert linked[0][0] is not None
        assert "/_unfiled/" not in linked[0][1]
        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0
    finally:
        _cleanup()


def _open_session(client, data: bytes, sha256: str) -> dict:
    opened = client.post(
        "/api/uploads",
        json={"filename": "again.png", "mime": "image/png", "size": len(data), "sha256": sha256},
    )
    assert opened.status_code == 201, opened.text
    return opened.json()


async def test_a_session_opened_on_the_route_for_the_same_bytes_holds_the_row(
    logged_in_client, monkeypatch, tmp_path
):
    """The hold the session clause promises, through `POST /api/uploads` itself: a session that
    staged onto this owner's row (MEMNG-959) records the digest it was checked against, and the
    aged row is neither reclaimable nor reclaimed while that session is open."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = PNG + b"held-by-route" * 300
        uid = _finalized_orphan(client, "held.png", data)
        _age(uid, 73 * H)
        digest = _rows("SELECT sha256 FROM attachments WHERE uid = :u", u=uid)[0][0]

        session = _open_session(client, data, digest)
        assert session["bytes_received"] == len(data)  # a staging hit, not an ordinary session

        assert await _gauge() == (0, 0)
        assert await worker.sweep_unlinked_attachments() == 0
        assert _rows("SELECT uid FROM attachments WHERE uid = :u", u=uid) == [(uid,)]

        # Not vacuous: once the session is gone the same row is the sweep's.
        _exec("DELETE FROM uploads")
        assert await worker.sweep_unlinked_attachments() == 1
        assert _rows("SELECT uid FROM attachments WHERE uid = :u", u=uid) == []
    finally:
        _cleanup()


async def test_an_unmatched_claim_is_not_recorded_as_a_digest(
    logged_in_client, monkeypatch, tmp_path
):
    """A claim no stored bytes back is an ordinary empty session and stores no digest: the column
    holds only what something checked."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        session = _open_session(client, PNG + b"nobody-has-these", "c" * 64)
        assert session["bytes_received"] == 0
        stored = _rows("SELECT sha256 FROM uploads WHERE upload_uid = :u", u=session["upload_uid"])
        assert stored == [(None,)]
    finally:
        _cleanup()
