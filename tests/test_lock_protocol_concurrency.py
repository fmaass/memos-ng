"""Deterministic two-actor probes for the upload-finalize lock protocol (MEMNG-267, phase 1).

The protocol these pin is the total order `M < U < J < A < R < F` with one globally sorted
acquisition per class per transaction (LOCK-PROTOCOL-DESIGN.md; `docs/LOCK-ORDER.md` is written in
phase 2). Every test here is a REAL race: two database sessions on one event loop, each running a
production route or service function, with one actor PARKED inside its transaction while the other
is proved — through `pg_stat_activity`, never through a sleep — to be waiting on a row lock.

Mechanics follow §5.1 of the design and reuse the patterns this suite already has:

* seeds are COMMITTED through the app's own engine (`logged_in_client` + `_async_client`), exactly
  as `tests/test_capture_concurrency.py:31-72` does — never the `adb` fixture, whose outer
  transaction is invisible to a second connection (`tests/conftest.py:65-75`);
* one session/connection per actor, both running the real path;
* park hooks claim their turn SYNCHRONOUSLY (the `calls += 1` pattern of
  `tests/test_capture_concurrency.py:105-115`) and every test asserts its hook actually fired, the
  control `tests/test_commit_race_reconcile.py:125` establishes;
* blocking is OBSERVED (`_wait_for_a_new_lock_wait`), never timed, and the coordinator releases the
  parked winner on ANY lock wait rather than on a named relation — under the protocol the second
  actor correctly blocks EARLIER than it did before, and a barrier both actors must reach would
  hang in correct code (§5.1, "the fixed-code accommodation");
* seeded rows are cleaned up in `finally`.

On the unmodified tree these probes deadlock: one actor dies with SQLSTATE `40P01` and
`_answers()` reports it by name. C5b and C10 are the two declared exceptions to red-on-baseline,
and each names the tree it IS red on — a mutation carried in `tests/mutations/` and applied only
under its own `MEMNG267_MUTATION` value.
"""

import asyncio
import hashlib
import io
import os
import time
from pathlib import Path

import pytest
from app import auth
from app.config import settings
from app.csrf import canonical_origin
from app.database import async_session
from app.database import engine as async_engine
from app.main import app
from app.models import Attachment, MediaJob
from app.routers import memos as memos_router
from app.scripts import backfill_media_metadata as backfill
from app.services import media as media_svc
from app.services import poster
from app.services import reclaim as reclaim_svc
from app.services import uploads as uploads_svc
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only

from tests.conftest import SYNC_URL

# The repo-carried mutation controls (tests/mutations/). Each declared exception to the
# red-on-baseline convention names the tree it IS red on, and that tree is a named patch applied
# once by hand — never by the suite, which must stay green on the tree it is run against.
MUTATION = os.environ.get("MEMNG267_MUTATION")

# Deliberately generous next to the milliseconds a granted lock needs: every one of these waits
# ends on an OBSERVED state change, so the ceiling only bounds a hang.
WAIT_TIMEOUT = 30.0
POLL_INTERVAL = 0.05

VIDEO_BYTES = bytes(range(256)) * 400  # unrecognisable bytes declared video/* — as in MEMNG-146


# --------------------------------------------------------------------------------------------
# seeds and readers
# --------------------------------------------------------------------------------------------


def _png(marker: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + marker + b"0" * 3000


def _jpeg(colour: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _cleanup() -> None:
    """DELETE, not TRUNCATE — TRUNCATE takes ACCESS EXCLUSIVE and would queue behind any reader
    still holding a transaction on these tables (tests/test_commit_race_reconcile.py:46)."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for table in (
            "media_job_progress",
            "media_jobs",
            "attachment_renditions",
            "memo_edit_keys",
            "attachments",
            "memos",
            "uploads",
        ):
            c.execute(text(f"DELETE FROM {table}"))
    eng.dispose()


def _sql(query: str, **params):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(text(query), params).all()
    eng.dispose()
    return rows


def _one(query: str, **params):
    rows = _sql(query, **params)
    assert len(rows) == 1, rows
    return rows[0]


def _upload(client, name: str, data: bytes, mime: str = "image/png") -> str:
    created = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert created.status_code == 201, created.text
    uid = created.json()["upload_uid"]
    appended = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert appended.status_code == 200, appended.text
    return uid


def _unlinked_attachment(client, name: str, data: bytes, mime: str = "image/png") -> str:
    """An attachment holding `data`, owned by the caller and linked to no memo — the dedup target
    every cycle in this file shares. Made the way the API makes one, so its sha256, its file and
    its `memo_id IS NULL` are all real."""
    uid = _upload(client, name, data, mime)
    done = client.post(f"/api/uploads/{uid}/finalize")
    assert done.status_code == 200, done.text
    return done.json()["uid"]


def _memo(client, content: str = "seed", **body) -> dict:
    created = client.post("/api/memos", json={"content": content, "visibility": "PRIVATE", **body})
    assert created.status_code == 201, created.text
    return created.json()


def _async_client(client) -> AsyncClient:
    """An async client speaking with the SAME session as the TestClient fixture
    (tests/test_capture_concurrency.py:58-72): the login flow leaves more than one cookie of that
    name in the jar, so the LAST one is the live session."""
    sid = [c.value for c in client.cookies.jar if c.name == auth.SID_COOKIE][-1]
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": canonical_origin()},
        cookies={auth.SID_COOKIE: sid or ""},
    )


def _memo_attachment_media(memo_uid: str) -> list[tuple[str, bytes]]:
    """(filename, the bytes actually on disk) for each attachment, in the memo's SAVED order.

    §5.1 requires rows AND bytes: a probe that counts rows passes just as happily when the media
    the memo promises is a zero-length file or somebody else's picture.
    """
    rows = _sql(
        "SELECT a.filename, a.storage_path FROM attachments a JOIN memos m ON m.id = a.memo_id "
        "WHERE m.uid = :u ORDER BY a.position NULLS LAST, a.id",
        u=memo_uid,
    )
    return [(name, Path(path).read_bytes()) for name, path in rows]


def _memo_attachment_uids(memo_uid: str) -> list[str]:
    return [
        r[0]
        for r in _sql(
            "SELECT a.uid FROM attachments a JOIN memos m ON m.id = a.memo_id "
            "WHERE m.uid = :u ORDER BY a.position NULLS LAST, a.id",
            u=memo_uid,
        )
    ]


# --------------------------------------------------------------------------------------------
# the harness: parking, and PROVING a wait
# --------------------------------------------------------------------------------------------


def _lock_waiters() -> list[int]:
    """Every backend in THIS database genuinely waiting on a heavyweight lock.

    Read from `pg_stat_activity`, which is Postgres's own account of the wait — a `pg_sleep` or a
    bare `asyncio.sleep` is never the proof (§5.1). The polling connection is its own backend and
    is never itself waiting, so it cannot answer for the actors.
    """
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as c:
            return list(
                c.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                ).scalars()
            )
    finally:
        eng.dispose()


async def _wait_for_a_new_lock_wait(before: set[int], ceiling: float = WAIT_TIMEOUT) -> int:
    """Block until a backend that was NOT already waiting starts waiting on a lock; return its pid.

    Deliberately not scoped to a relation: after the protocol lands the second actor blocks
    EARLIER than it used to (at U rather than at A), and a coordinator that waited for the old,
    later wait would hang on correct code — §5.1's mandatory accommodation.
    """
    deadline = time.monotonic() + ceiling
    while time.monotonic() < deadline:
        fresh = [pid for pid in await asyncio.to_thread(_lock_waiters) if pid not in before]
        if fresh:
            return fresh[0]
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError("no actor ever blocked on a row lock — this test proves nothing")


class Park:
    """A one-shot park with a synchronous claim.

    `claim()` increments before any await, so two coroutines interleaving at an await point cannot
    both believe they are the parked one — the same rule the production one-shot answers follow.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.parked = 0

    def claim(self) -> bool:
        self.calls += 1
        return self.calls == 1

    async def hold(self) -> None:
        self.parked += 1
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=WAIT_TIMEOUT)

    def fired(self) -> None:
        assert self.parked == 1, (
            f"the park never happened ({self.parked}) — this test proves nothing"
        )


def _park_finalize(monkeypatch, park: Park, when) -> None:
    """Park an actor AFTER `finalize` has taken its upload row and its dedup attachment row.

    `when(upload_uid)` picks the actor: both actors of most probes reach `finalize`, and a park
    that could not tell them apart would be a coin toss rather than a schedule.
    """
    real = uploads_svc.finalize

    async def parking(db, upload, *args, **kwargs):
        mine = park.claim() if when(upload.upload_uid) else False
        attachment = await real(db, upload, *args, **kwargs)
        if mine:
            await park.hold()
        return attachment

    monkeypatch.setattr(uploads_svc, "finalize", parking)


def _park_ensure_capture(monkeypatch, park: Park, when) -> None:
    """Park an actor holding its UPLOAD row and nothing else.

    `ensure_capture` is where `finalize` takes that row (`app/services/uploads.py:768`), so
    returning from it is exactly "U held, A not yet requested". The hash that follows is local I/O
    against a file this lock already fences, so parking here rather than a few statements later
    changes which locks are held not at all.
    """
    real = uploads_svc.ensure_capture

    async def parking(db, upload):
        mine = park.claim() if when(upload.upload_uid) else False
        locked = await real(db, upload)
        if mine:
            await park.hold()
        return locked

    monkeypatch.setattr(uploads_svc, "ensure_capture", parking)


def _park_after_hash(monkeypatch, park: Park, when) -> None:
    """Park an actor holding its UPLOAD row with its bytes already hashed — the last instant before
    `finalize` asks for a dedup attachment (`app/services/uploads.py:1016-1035`).

    Parking one step earlier (at `ensure_capture`) is not equivalent on the unmodified tree: the
    interactive attach takes no upload lock there, so it would MOVE the part file out from under
    the parked hash and the two would collide on the filesystem before they could collide on a
    row. The seam is the real `to_thread.run_sync` call that computes the digest — delegated to,
    never replaced.
    """
    real_run_sync = uploads_svc.to_thread.run_sync

    class _ParkingThreadRunner:
        @staticmethod
        async def run_sync(func, *args, **kwargs):
            result = await real_run_sync(func, *args, **kwargs)
            if func is uploads_svc._sha256_sync and when(args[0]) and park.claim():
                await park.hold()
            return result

    monkeypatch.setattr(uploads_svc, "to_thread", _ParkingThreadRunner)


def _park_after_statement(monkeypatch, park: Park, match) -> None:
    """Park an actor immediately after ONE real statement of its own has run.

    Pass 0 has two seams that are not function boundaries — the unlocked candidate read and the
    ascending attachment acquisition — and both are exactly where a second actor has to be let in.
    The hook delegates to the real `AsyncSession.execute` and parks after it returns, so the
    statement itself, its locks and its result are entirely the production ones.
    """
    real = AsyncSession.execute

    async def parking(self, statement, *args, **kwargs):
        result = await real(self, statement, *args, **kwargs)
        if match(" ".join(str(statement).split())) and park.claim():
            await park.hold()
        return result

    monkeypatch.setattr(AsyncSession, "execute", parking)


def _is_unlocked_candidate_read(sql: str) -> bool:
    return "attachments.sha256 IN" in sql and "FOR UPDATE" not in sql


def _is_ascending_attachment_lock(sql: str) -> bool:
    return (
        sql.startswith("SELECT attachments.")
        and "ORDER BY attachments.id" in sql
        and "FOR UPDATE" in sql
    )


def _park_finalize_pending(monkeypatch, park: Park) -> None:
    """Park a batch BEFORE it has locked anything at all — the schedule the continuation probes
    need, where the other actor gets a clean run at the upload the batch is about to ask for."""
    real = uploads_svc.finalize_pending

    async def parking(*args, **kwargs):
        if park.claim():
            await park.hold()
        return await real(*args, **kwargs)

    monkeypatch.setattr(uploads_svc, "finalize_pending", parking)


def _answers(results: list) -> list:
    """Both actors' answers — or a failure that NAMES the SQLSTATE.

    A deadlock is the thing these probes exist to detect, so it is reported as itself instead of
    as an opaque traceback: on the unmodified tree this is the line that records `40P01`.
    """
    for result in results:
        if isinstance(result, BaseException):
            state = getattr(getattr(result, "orig", None), "sqlstate", None)
            raise AssertionError(
                f"an actor failed with SQLSTATE {state}: {type(result).__name__}: {result}"
            ) from result
    return results


async def _both(first, second) -> list:
    return _answers(list(await asyncio.gather(first, second, return_exceptions=True)))


# --------------------------------------------------------------------------------------------
# C1 — create ∥ standalone finalize, sharing a dedup target
# --------------------------------------------------------------------------------------------


async def test_create_and_standalone_finalize_sharing_a_dedup_target_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C1. The create holds the dedup attachment and still wants its second upload; the standalone
    holds that upload and wants the same attachment. Before pass 0 the create takes U and A
    alternating, one upload at a time, so the two can hold each other's rows."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _png(b"C1")
        target = _unlinked_attachment(client, "target.png", shared)
        first = _upload(client, "one.png", shared)
        second = _upload(client, "two.png", shared)

        park = Park()
        _park_finalize(monkeypatch, park, lambda uid: uid == first)

        async with _async_client(client) as creator, _async_client(client) as standalone:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={"content": "c1", "visibility": "PRIVATE", "upload_uids": [first, second]},
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            solo = asyncio.create_task(standalone.post(f"/api/uploads/{second}/finalize"))
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            created, finalized = await _both(create, solo)

        park.fired()
        assert created.status_code == 201, created.text
        # Exactly one of the two legitimate shapes (§5.2 C1): here the create owns both upload
        # sessions before it touches an attachment, so the standalone's session is consumed and a
        # consumed session answers exactly as an unknown uid does.
        assert finalized.status_code == 404, finalized.text
        assert _memo_attachment_uids(created.json()["uid"]) == [target]
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C2 / C2b — two batches visiting two dedup targets in opposite orders
# --------------------------------------------------------------------------------------------


async def _two_batches_in_opposite_orders(client, monkeypatch, tmp_path, *, second_is_create):
    x, y = _png(b"C2X"), _png(b"C2Y")
    a = _unlinked_attachment(client, "a.png", x)
    b = _unlinked_attachment(client, "b.png", y)
    memo_one = _memo(client, "one")
    memo_two = _memo(client, "two") if not second_is_create else None
    one_x = _upload(client, "1x.png", x)
    one_y = _upload(client, "1y.png", y)
    two_y = _upload(client, "2y.png", y)
    two_x = _upload(client, "2x.png", x)

    park = Park()
    _park_finalize(monkeypatch, park, lambda uid: uid == one_x)

    async with _async_client(client) as first, _async_client(client) as second:
        left = asyncio.create_task(
            first.patch(f"/api/memos/{memo_one['uid']}", json={"upload_uids": [one_x, one_y]})
        )
        await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
        waiting = set(_lock_waiters())
        if second_is_create:
            right = asyncio.create_task(
                second.post(
                    "/api/memos",
                    json={"content": "two", "visibility": "PRIVATE", "upload_uids": [two_y, two_x]},
                )
            )
        else:
            right = asyncio.create_task(
                second.patch(f"/api/memos/{memo_two['uid']}", json={"upload_uids": [two_y, two_x]})
            )
        await _wait_for_a_new_lock_wait(waiting)
        park.release.set()
        left_res, right_res = await _both(left, right)

    park.fired()
    assert left_res.status_code == 200, left_res.text
    assert right_res.status_code == (201 if second_is_create else 200), right_res.text
    # The winner keeps both dedup targets, in the order it declared them; the loser's revalidation
    # finds them linked and it mints its own rows rather than stealing (X2).
    assert _memo_attachment_uids(memo_one["uid"]) == [a, b]
    # The winner's rows are the dedup TARGETS (a dedup hit hands back the pre-existing
    # attachment), so their filenames are the targets' — but the bytes must be the ones the
    # uploads carried, in the declared order.
    assert _memo_attachment_media(memo_one["uid"]) == [("a.png", x), ("b.png", y)]
    other_uid = right_res.json()["uid"]
    other = _memo_attachment_uids(other_uid)
    assert len(other) == 2, other
    assert a not in other and b not in other
    # "each memo keeps its own media and its DECLARED order" (§5.2, C2). The loser declared
    # [2y, 2x] — the opposite visiting order that made this a cycle — so that is what it must read
    # back as, with the bytes the client actually uploaded behind each row.
    assert _memo_attachment_media(other_uid) == [("2y.png", y), ("2x.png", x)]
    assert _one("SELECT count(*) FROM uploads")[0] == 0


async def test_two_patches_visiting_two_dedup_targets_in_opposite_orders_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C2. Two edits of DIFFERENT memos, each deduping onto the other's target first."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        await _two_batches_in_opposite_orders(
            client, monkeypatch, tmp_path, second_is_create=False
        )
    finally:
        _cleanup()


async def test_create_and_patch_in_opposite_dedup_orders_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C2b. The same construction with a create on the other side."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        await _two_batches_in_opposite_orders(client, monkeypatch, tmp_path, second_is_create=True)
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C3 — batch dedup ∥ legacy attachment linking
# --------------------------------------------------------------------------------------------


async def test_batch_dedup_and_legacy_attachment_linking_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C3. The batch holds the high-id dedup target and wants the memo's low-id attachment; the
    back-compat create holds the low-id one (its `attachment_uids` set is id-ordered) and wants the
    high-id one."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        low_bytes, high_bytes = _png(b"C3L"), _png(b"C3H")
        memo = _memo(
            client, "carries the low id", upload_uids=[_upload(client, "low.png", low_bytes)]
        )
        low = _memo_attachment_uids(memo["uid"])[0]
        high = _unlinked_attachment(client, "high.png", high_bytes)
        assert _one("SELECT id FROM attachments WHERE uid = :u", u=low)[0] < _one(
            "SELECT id FROM attachments WHERE uid = :u", u=high
        )[0]
        dedup_upload = _upload(client, "again.png", high_bytes)

        park = Park()
        _park_finalize(monkeypatch, park, lambda uid: uid == dedup_upload)

        async with _async_client(client) as editor, _async_client(client) as creator:
            edit = asyncio.create_task(
                editor.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [dedup_upload]})
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={
                        "content": "legacy",
                        "visibility": "PRIVATE",
                        "attachment_uids": [low, high],
                    },
                )
            )
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            edited, created = await _both(edit, create)

        park.fired()
        assert edited.status_code == 200, edited.text
        assert created.status_code == 201, created.text
        # The steal guard still refuses both rows: each is linked to the edited memo, and a create
        # may never re-point a linked attachment (`app/services/memos.py:312`).
        assert sorted(_memo_attachment_uids(memo["uid"])) == sorted([low, high])
        assert _memo_attachment_uids(created.json()["uid"]) == []
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C4 — interactive rendition attach ∥ standalone finalize
# --------------------------------------------------------------------------------------------


async def test_rendition_attach_and_standalone_finalize_sharing_bytes_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C4. The standalone holds the upload and wants the attachment; before R5 the attach holds the
    attachment and wants that same upload (it deletes the row it consumed)."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _jpeg((10, 20, 30))
        target = _unlinked_attachment(client, "target.jpg", shared, "image/jpeg")
        upload = _upload(client, "master.jpg", shared, "image/jpeg")

        park = Park()
        _park_after_hash(monkeypatch, park, lambda part: part == uploads_svc.part_path(upload))

        async with _async_client(client) as standalone, _async_client(client) as attacher:
            solo = asyncio.create_task(standalone.post(f"/api/uploads/{upload}/finalize"))
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            attach = asyncio.create_task(
                attacher.post(
                    f"/api/attachments/{target}/renditions", json={"upload_uid": upload}
                )
            )
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            finalized, attached = await _both(solo, attach)

        park.fired()
        # The only outcome U-before-A can produce: the parked standalone owns the upload session,
        # so the attach never reaches the attachment at all and answers as an unknown uid does.
        assert finalized.status_code == 200, finalized.text
        assert finalized.json()["uid"] == target  # a dedup hit, not a second row
        assert attached.status_code == 404, attached.text
        assert _one("SELECT count(*) FROM attachment_renditions")[0] == 0
        assert _one("SELECT count(*) FROM attachments")[0] == 1
    finally:
        _cleanup()


async def test_rendition_attach_and_standalone_finalize_do_not_cycle_with_the_attach_first(
    logged_in_client, monkeypatch, tmp_path
):
    """C4, mirrored. The attach gets there first, so it is the standalone whose session is gone."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _jpeg((40, 50, 60))
        target = _unlinked_attachment(client, "target.jpg", shared, "image/jpeg")
        upload = _upload(client, "master.jpg", shared, "image/jpeg")

        park = Park()
        real_attach = uploads_svc.attach_print_master

        async def parking(*args, **kwargs):
            # Parked at the placement, i.e. AFTER the route has taken every row it takes.
            if park.claim():
                await park.hold()
            return await real_attach(*args, **kwargs)

        monkeypatch.setattr(uploads_svc, "attach_print_master", parking)

        async with _async_client(client) as attacher, _async_client(client) as standalone:
            attach = asyncio.create_task(
                attacher.post(
                    f"/api/attachments/{target}/renditions", json={"upload_uid": upload}
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            solo = asyncio.create_task(standalone.post(f"/api/uploads/{upload}/finalize"))
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            attached, finalized = await _both(attach, solo)

        park.fired()
        assert attached.status_code == 201, attached.text
        assert finalized.status_code == 404, finalized.text
        assert _one("SELECT count(*) FROM attachment_renditions")[0] == 1
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C5 / C5b — poster consumption, and the two continuations that must not merge
# --------------------------------------------------------------------------------------------


async def test_poster_consumption_and_standalone_finalize_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C5, batch first. The batch holds the image's dedup attachment and then wants to delete the
    poster upload; the standalone holds that poster and wants the same attachment."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _jpeg((70, 80, 90))
        target = _unlinked_attachment(client, "target.jpg", shared, "image/jpeg")
        image = _upload(client, "photo.jpg", shared, "image/jpeg")
        video = _upload(client, "clip.mov", VIDEO_BYTES, "video/quicktime")
        poster_uid = _upload(client, "poster.jpg", shared, "image/jpeg")

        park = Park()
        _park_finalize(monkeypatch, park, lambda uid: uid == image)

        async with _async_client(client) as creator, _async_client(client) as standalone:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={
                        "content": "c5",
                        "visibility": "PRIVATE",
                        "upload_uids": [image, video],
                        "video_posters": {video: poster_uid},
                    },
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            solo = asyncio.create_task(standalone.post(f"/api/uploads/{poster_uid}/finalize"))
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            created, finalized = await _both(create, solo)

        park.fired()
        assert created.status_code == 201, created.text
        assert finalized.status_code == 404, finalized.text
        linked = _memo_attachment_uids(created.json()["uid"])
        assert target in linked and len(linked) == 2
        video_uid = next(uid for uid in linked if uid != target)
        assert poster.poster_path(video_uid).exists()  # the override landed where the tier is read
        assert not poster.poster_path(target).exists()  # …and displaced nobody else's
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


async def test_a_lost_poster_upload_answers_422_not_409(logged_in_client, monkeypatch, tmp_path):
    """C5, standalone first. A poster upload consumed under the batch is a `video_posters` problem
    — 422 in FastAPI's own validation shape — and never the 409 an `upload_uids` member gets.

    Green on every tree, like C5b, because it pins a CONTINUATION rather than a cycle. Its red tree
    is NOT `c5b_merge_answers.patch`, which moves only the missing attachment-upload answer and
    leaves this one alone: it is a one-off mutation that answers the poster problem with the 409
    instead (recorded in the phase ledger under "Round 2 / one-off mutations"). The two mutations
    are the two directions the merge can go, and C5b and this test are what refuse each of them.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _jpeg((11, 22, 33))
        _unlinked_attachment(client, "target.jpg", shared, "image/jpeg")
        image = _upload(client, "photo.jpg", _jpeg((1, 2, 3)), "image/jpeg")
        video = _upload(client, "clip.mov", VIDEO_BYTES, "video/quicktime")
        poster_uid = _upload(client, "poster.jpg", shared, "image/jpeg")

        park = Park()
        _park_finalize_pending(monkeypatch, park)

        async with _async_client(client) as creator, _async_client(client) as standalone:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={
                        "content": "c5-mirror",
                        "visibility": "PRIVATE",
                        "upload_uids": [image, video],
                        "video_posters": {video: poster_uid},
                    },
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            finalized = await standalone.post(f"/api/uploads/{poster_uid}/finalize")
            assert finalized.status_code == 200, finalized.text
            park.release.set()
            created = await create

        park.fired()
        assert created.status_code == 422, created.text
        detail = created.json()["detail"]
        assert detail[0]["type"] == "video_poster.poster_missing", detail
        assert detail[0]["loc"] == ["body", "video_posters", video], detail
        # Nothing moved: the memo was not created and both remaining uploads are still re-postable.
        assert _one("SELECT count(*) FROM memos")[0] == 0
        assert sorted(r[0] for r in _sql("SELECT upload_uid FROM uploads")) == sorted(
            [image, video]
        )
        # Rows AND bytes (§5.1). A surviving upload row is a PROMISE about a file; the refusal is
        # only free if the file is still whole. `_is_complete_sync` reads the row's declared size,
        # so this is the same completeness the next attempt will be judged by.
        assert uploads_svc.part_path(image).read_bytes() == _jpeg((1, 2, 3))
        assert uploads_svc.part_path(video).read_bytes() == VIDEO_BYTES
        stored = [r[0] for r in _sql("SELECT uid FROM attachments")]
        assert not any(poster.poster_path(uid).exists() for uid in stored)
        # …and the promise is honoured: the identical request minus the lost poster succeeds, which
        # is the whole point of refusing before the first side effect.
        repost = client.post(
            "/api/memos",
            json={
                "content": "c5-mirror re-posted",
                "visibility": "PRIVATE",
                "upload_uids": [image, video],
            },
        )
        assert repost.status_code == 201, repost.text
        assert _memo_attachment_media(repost.json()["uid"]) == [
            ("photo.jpg", _jpeg((1, 2, 3))),
            ("clip.mov", VIDEO_BYTES),
        ]
    finally:
        _cleanup()


async def test_a_missing_attachment_upload_still_answers_409_when_a_poster_is_also_named(
    logged_in_client, monkeypatch, tmp_path
):
    """C5b. The mirror of the probe above, and the pair is what proves pass 0 did not merge the two
    continuations: a consumed `upload_uids` member is still a 409 `uploads_unlinkable`, even in a
    batch that also carries `video_posters`.

    Green on every tree by construction (§5.2). Its RED tree is
    `tests/mutations/c5b_merge_answers.patch`, which merges the answers by sending every absent uid
    through the poster continuation; run once under `MEMNG267_MUTATION=c5b_merge_answers`.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        image = _upload(client, "photo.jpg", _jpeg((4, 5, 6)), "image/jpeg")
        video = _upload(client, "clip.mov", VIDEO_BYTES, "video/quicktime")
        poster_uid = _upload(client, "poster.jpg", _jpeg((7, 8, 9)), "image/jpeg")

        park = Park()
        _park_finalize_pending(monkeypatch, park)

        async with _async_client(client) as creator, _async_client(client) as standalone:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={
                        "content": "c5b",
                        "visibility": "PRIVATE",
                        "upload_uids": [image, video],
                        "video_posters": {video: poster_uid},
                    },
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            finalized = await standalone.post(f"/api/uploads/{image}/finalize")
            assert finalized.status_code == 200, finalized.text
            park.release.set()
            created = await create

        park.fired()
        assert created.status_code == 409, created.text
        detail = created.json()["detail"]
        assert detail["code"] == "uploads_unlinkable", detail
        assert detail["missing_upload_uids"] == [image], detail
        assert detail["incomplete_upload_uids"] == [], detail
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C6 — an in-batch print rendition ∥ standalone finalize
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("preexisting_rendition", [False, True], ids=["insert", "no-op"])
async def test_in_batch_print_rendition_and_standalone_finalize_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path, preexisting_rendition
):
    """C6. The batch holds the screen's dedup attachment and then wants to delete the print upload
    it consumed; the standalone holds that print upload and wants the same attachment. Both
    branches of `finalize_rendition` are exercised — the insert and the write-once no-op (X8)."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _jpeg((90, 100, 110))
        target = _unlinked_attachment(client, "photo.jpg", shared, "image/jpeg")
        if preexisting_rendition:
            occupied = _upload(client, "earlier.jpg", _jpeg((1, 1, 1)), "image/jpeg")
            seeded = client.post(
                f"/api/attachments/{target}/renditions", json={"upload_uid": occupied}
            )
            assert seeded.status_code == 201, seeded.text
        before = _sql("SELECT variant_key, sha256 FROM attachment_renditions ORDER BY id")

        screen = _upload(client, "photo.jpg", shared, "image/jpeg")
        print_upload = _upload(client, "photo - print.jpg", shared, "image/jpeg")

        park = Park()
        _park_finalize(monkeypatch, park, lambda uid: uid == screen)

        async with _async_client(client) as creator, _async_client(client) as standalone:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={
                        "content": "c6",
                        "visibility": "PRIVATE",
                        "upload_uids": [screen, print_upload],
                    },
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            solo = asyncio.create_task(standalone.post(f"/api/uploads/{print_upload}/finalize"))
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            created, finalized = await _both(create, solo)

        park.fired()
        assert created.status_code == 201, created.text
        assert finalized.status_code == 404, finalized.text
        assert _memo_attachment_uids(created.json()["uid"]) == [target]
        after = _sql("SELECT variant_key, sha256 FROM attachment_renditions ORDER BY id")
        if preexisting_rendition:
            assert after == before, (before, after)  # write-once: the re-dump changed nothing
        else:
            assert len(after) == 1 and after[0][0] == "print", after
            # Rows AND bytes: the master the memo now carries has to BE the file that was uploaded,
            # at the path the row names. A count and a variant_key say nothing about either.
            path, size = _one(
                "SELECT storage_path, size FROM attachment_renditions WHERE variant_key = 'print'"
            )
            assert Path(path).read_bytes() == shared
            assert size == len(shared)
            assert after[0][1] == hashlib.sha256(shared).hexdigest()
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C7 — a print bound to a memo sibling ∥ standalone finalize
# --------------------------------------------------------------------------------------------


async def test_bound_sibling_print_and_standalone_finalize_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C7. The edit dedups a screen onto an unlinked attachment, then binds an orphan print onto
    the memo's ALREADY-LINKED sibling — deleting the print's upload row, which the standalone
    holds while waiting for the dedup target."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        sibling_bytes = _jpeg((21, 31, 41))
        shared = _jpeg((51, 61, 71))
        memo = _memo(
            client,
            "carries the screen",
            upload_uids=[_upload(client, "photo.jpg", sibling_bytes, "image/jpeg")],
        )
        sibling = _memo_attachment_uids(memo["uid"])[0]
        target = _unlinked_attachment(client, "unrelated.jpg", shared, "image/jpeg")
        other = _upload(client, "other.jpg", shared, "image/jpeg")
        print_upload = _upload(client, "photo - print.jpg", shared, "image/jpeg")

        park = Park()
        _park_finalize(monkeypatch, park, lambda uid: uid == other)

        async with _async_client(client) as editor, _async_client(client) as standalone:
            edit = asyncio.create_task(
                editor.patch(
                    f"/api/memos/{memo['uid']}", json={"upload_uids": [other, print_upload]}
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            solo = asyncio.create_task(standalone.post(f"/api/uploads/{print_upload}/finalize"))
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            edited, finalized = await _both(edit, solo)

        park.fired()
        assert edited.status_code == 200, edited.text
        assert finalized.status_code == 404, finalized.text
        assert sorted(_memo_attachment_uids(memo["uid"])) == sorted([sibling, target])
        print_path = _one("SELECT print_path FROM attachments WHERE uid = :u", u=sibling)[0]
        assert print_path is not None
        assert Path(print_path).exists()  # exactly one master, and it is on disk
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C9 — the metadata backfill ∥ batch finalization
# --------------------------------------------------------------------------------------------


async def test_metadata_backfill_and_batch_finalize_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C9. The backfill walks attachments ascending under one run-long transaction; the batch holds
    the high-id dedup target and then wants the memo's low-id attachment."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        low_bytes, high_bytes = _jpeg((12, 13, 14)), _jpeg((15, 16, 17))
        memo = _memo(
            client, "low", upload_uids=[_upload(client, "low.jpg", low_bytes, "image/jpeg")]
        )
        low = _memo_attachment_uids(memo["uid"])[0]
        high = _unlinked_attachment(client, "high.jpg", high_bytes, "image/jpeg")
        dedup_upload = _upload(client, "again.jpg", high_bytes, "image/jpeg")

        park = Park()
        real_fill = backfill.write_fill

        async def parking(db, attachment, computed, fields):
            mine = park.claim()
            filled = await real_fill(db, attachment, computed, fields)
            if mine:
                await park.hold()
            return filled

        monkeypatch.setattr(backfill, "write_fill", parking)

        async def run_backfill():
            async with async_session() as db:
                stats = await backfill.run_backfill(db, execute=True)
                await db.commit()
                return stats

        async with _async_client(client) as editor:
            sweep = asyncio.create_task(run_backfill())
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            edit = asyncio.create_task(
                editor.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [dedup_upload]})
            )
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            stats, edited = await _both(sweep, edit)

        park.fired()
        assert edited.status_code == 200, edited.text
        assert stats.processed == 2, stats
        assert sorted(_memo_attachment_uids(memo["uid"])) == sorted([low, high])
        # The conditional UPDATE still filled what was NULL — the backfill was not merely survived.
        for uid in (low, high):
            row = _one("SELECT width, height FROM attachments WHERE uid = :u", u=uid)
            assert row[0] == 64 and row[1] == 48, (uid, row)
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C10 — stale-claim recovery ∥ a two-attachment PATCH
# --------------------------------------------------------------------------------------------


def _seed_stale_terminal_job(attachment_uid: str, *, seconds_stale: int = 10) -> int:
    """A `processing` derive job for this attachment, one attempt short of the ceiling and older
    than the stale horizon — what `recover_stale_media_claims` takes terminally."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            attachment_id = c.execute(
                text("SELECT id FROM attachments WHERE uid = :u"), {"u": attachment_uid}
            ).scalar_one()
            c.execute(
                text("DELETE FROM media_jobs WHERE attachment_id = :a"), {"a": attachment_id}
            )
            return c.execute(
                text(
                    "INSERT INTO media_jobs "
                    "(attachment_id, lane, kind, state, attempts, progress_pct, updated_at) "
                    "VALUES (:a, 'slow', 'derive', 'processing', :n, 0, "
                    "now() - make_interval(secs => :s)) RETURNING id"
                ),
                {
                    "a": attachment_id,
                    "n": settings.MEDIA_MAX_ATTEMPTS - 1,
                    "s": settings.MEDIA_CLAIM_STALE_SECONDS + seconds_stale,
                },
            ).scalar_one()
    finally:
        eng.dispose()


async def _recovery_beside_a_print_only_patch(client, monkeypatch, *, capture_sql):
    """The C10 schedule, shared by its two probes. Returns (sweep result, PATCH response, SQL)."""
    from app import worker

    memo = _memo(
        client,
        "two attachments",
        upload_uids=[
            _upload(client, "aaa.jpg", _jpeg((2, 4, 6)), "image/jpeg"),
            _upload(client, "photo.jpg", _jpeg((8, 10, 12)), "image/jpeg"),
        ],
    )
    low, high = _memo_attachment_uids(memo["uid"])
    assert _one("SELECT id FROM attachments WHERE uid = :u", u=low)[0] < _one(
        "SELECT id FROM attachments WHERE uid = :u", u=high
    )[0]
    # The HIGH-id attachment's job is inserted first, so it carries the LOWER media_jobs.id and the
    # sweep reaches it first — the inversion the protocol has to survive.
    high_job = _seed_stale_terminal_job(high)
    low_job = _seed_stale_terminal_job(low)
    assert high_job < low_job

    print_upload = _upload(client, "photo - print.jpg", _jpeg((14, 16, 18)), "image/jpeg")

    park = Park()
    real_fail = worker._fail_attachment

    async def parking(db, job):
        mine = park.claim()
        await real_fail(db, job)
        # Flush so the attachment UPDATE this iteration decided is actually ON THE WIRE before the
        # park: without it the row is only dirty in the identity map and holds no lock at all.
        await db.flush()
        if mine:
            await park.hold()

    monkeypatch.setattr(worker, "_fail_attachment", parking)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    async def sweep():
        async with async_session() as db:
            recovered = await worker.recover_stale_media_claims(db)
            await db.commit()
            return recovered

    if capture_sql:
        event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        async with _async_client(client) as editor:
            recovery = asyncio.create_task(sweep())
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            edit = asyncio.create_task(
                editor.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [print_upload]})
            )
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            recovered, edited = await _both(recovery, edit)
    finally:
        if capture_sql:
            event.remove(async_engine.sync_engine, "before_cursor_execute", record)

    park.fired()
    return recovered, edited, statements, memo, low, high


async def test_stale_claim_recovery_and_a_two_attachment_patch_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C10. The sweep writes its attachments in JOB order; phase 1's hoisted acquisition makes the
    PATCH lock the memo's whole attachment set ASCENDING, so without R7a's pre-loop acquisition the
    two invert.

    Three trees (§5.2, and C10 is the ONLY probe exempt from red-on-baseline): (a) unmodified —
    both complete, because a print-only PATCH locks the sibling alone; (b) phase 1 with
    `tests/mutations/no_r7a.patch` — deterministic `40P01`; (c) phase 1 — both complete.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        recovered, edited, _, memo, low, high = await _recovery_beside_a_print_only_patch(
            client, monkeypatch, capture_sql=False
        )
        assert edited.status_code == 200, edited.text
        assert recovered == 2, recovered
        # The print bound onto the same-base sibling rather than becoming a second visible photo.
        assert _memo_attachment_uids(memo["uid"]) == [low, high]
        assert _one("SELECT print_path FROM attachments WHERE uid = :u", u=high)[0] is not None
        assert [r[0] for r in _sql("SELECT state FROM media_jobs ORDER BY id")] == [
            "failed",
            "failed",
        ]
        assert sorted(r[0] for r in _sql("SELECT status FROM attachments")) == ["failed", "failed"]
    finally:
        _cleanup()


async def test_the_recovery_sweep_locks_its_terminal_attachments_ascending_before_it_writes(
    logged_in_client, monkeypatch, tmp_path
):
    """C10's ordering half, split out so the completion assertion above stays clean on the
    unmodified tree (§5.2's three-tree note). The emitted SQL must show ONE ascending
    `FOR UPDATE` over the terminal attachment set before the first `UPDATE attachments`."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, edited, statements, _, _, _ = await _recovery_beside_a_print_only_patch(
            client, monkeypatch, capture_sql=True
        )
        assert edited.status_code == 200, edited.text
        # The sweep's own acquisition, told apart from the PATCH's by its projection: R7a selects
        # `attachments.id` alone, because it locks the set rather than reading it.
        acquisitions = [
            i
            for i, s in enumerate(statements)
            if s.startswith("SELECT attachments.id FROM attachments")
            and "ORDER BY attachments.id" in s
            and "FOR UPDATE" in s
        ]
        updates = [i for i, s in enumerate(statements) if s.startswith("UPDATE attachments")]
        assert any("FROM media_jobs" in s for s in statements)  # positive control on the capture
        assert acquisitions, statements
        assert updates, statements
        assert min(acquisitions) < min(updates), (acquisitions, updates)
    finally:
        _cleanup()


@pytest.mark.skipif(
    MUTATION != "no_r7a",
    reason=(
        "C10's negative control, tree (b): `git apply tests/mutations/no_r7a.patch` and run with "
        "MEMNG267_MUTATION=no_r7a. Without it the probe above could pass for the wrong reason on "
        "every tree it ever sees."
    ),
)
async def test_stale_claim_recovery_deadlocks_without_the_pre_loop_acquisition(
    logged_in_client, monkeypatch, tmp_path
):
    """C10, tree (b). Phase 1 MINUS R7a's pre-loop acquisition, and nothing else: the sweep is back
    to writing its attachments in job order while the PATCH holds the memo's set ascending. One
    actor must die with SQLSTATE `40P01` — the measurement behind "phase 1 is safe alone"."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        with pytest.raises(AssertionError, match="SQLSTATE 40P01"):
            await _recovery_beside_a_print_only_patch(client, monkeypatch, capture_sql=False)
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# F1 / F2 (round 2) — pass 0's revalidation, and its complete attachment set
# --------------------------------------------------------------------------------------------


async def test_a_candidate_deleted_under_pass_zero_is_dropped_not_reused(
    logged_in_client, monkeypatch, tmp_path
):
    """R3.5's revalidation must judge the rows the LOCKING select actually returned.

    The candidates come from an unlocked read. `populate_existing` refreshes what the locking
    statement returns — it can say nothing about a row that is ABSENT from the result, which is
    exactly what a row deleted in between looks like. Judging the stale identity-map object then
    passes `owner_id == owner_id and memo_id is None` on a row that no longer exists, and the
    finalize hands back a nonexistent attachment: the memo is stored with NO media, the upload row
    is deleted and its part file reaped. This probe is the deletion, and it asserts rows AND bytes.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _png(b"F1")
        target = _unlinked_attachment(client, "target.png", shared)
        target_id = _one("SELECT id FROM attachments WHERE uid = :u", u=target)[0]
        upload = _upload(client, "again.png", shared)

        park = Park()
        _park_after_statement(monkeypatch, park, _is_unlocked_candidate_read)

        async def reclaim() -> object:
            async with async_session() as db:
                return await reclaim_svc.reclaim_attachment(
                    db, target_id, reclaim_svc.Revalidate.IDENTITY
                )

        async with _async_client(client) as creator:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={"content": "f1", "visibility": "PRIVATE", "upload_uids": [upload]},
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            # The real reclaim path, on its own connection, committing before the batch resumes.
            reclaimed = await asyncio.wait_for(reclaim(), timeout=WAIT_TIMEOUT)
            park.release.set()
            created = await create

        park.fired()
        assert reclaimed.outcome == "reclaimed", reclaimed
        assert created.status_code == 201, created.text
        # The candidate is gone, so the upload mints its OWN attachment and its own file.
        media = _memo_attachment_media(created.json()["uid"])
        assert media == [("again.png", shared)], media
        assert _one("SELECT count(*) FROM attachments")[0] == 1
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


async def _create_with_a_named_attachment_beside_the_recovery_sweep(client, monkeypatch):
    """The F2 schedule: a create that names one attachment AND dedups onto another, beside the
    stale-claim sweep whose terminal set is both of them. Returns everything the two probes assert.
    """
    from app import worker

    low_bytes, high_bytes = _png(b"F2LOW"), _png(b"F2HIGH")
    named = _unlinked_attachment(client, "named.png", low_bytes)
    dedup_target = _unlinked_attachment(client, "target.png", high_bytes)
    named_id = _one("SELECT id FROM attachments WHERE uid = :u", u=named)[0]
    target_id = _one("SELECT id FROM attachments WHERE uid = :u", u=dedup_target)[0]
    assert named_id < target_id  # A10 below B20, so ascending means named first
    # The HIGH-id attachment's job is inserted first, so it carries the LOWER media_jobs.id: the
    # sweep would reach B20 before A10 if it took its attachments in job order.
    high_job = _seed_stale_terminal_job(dedup_target)
    low_job = _seed_stale_terminal_job(named)
    assert high_job < low_job
    upload = _upload(client, "again.png", high_bytes)

    park = Park()
    _park_after_statement(monkeypatch, park, _is_ascending_attachment_lock)

    locks: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(statement.split())
        if _is_ascending_attachment_lock(flat):
            locks.append((flat, parameters))

    async def sweep() -> int:
        async with async_session() as db:
            recovered = await worker.recover_stale_media_claims(db)
            await db.commit()
            return recovered

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        async with _async_client(client) as creator:
            create = asyncio.create_task(
                creator.post(
                    "/api/memos",
                    json={
                        "content": "f2",
                        "visibility": "PRIVATE",
                        "attachment_uids": [named],
                        "upload_uids": [upload],
                    },
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            recovery = asyncio.create_task(sweep())
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            created, recovered = await _both(create, recovery)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)

    park.fired()
    return (
        created,
        recovered,
        locks,
        named,
        dedup_target,
        named_id,
        target_id,
        high_bytes,
        low_bytes,
    )


async def test_a_create_naming_an_attachment_and_deduping_onto_another_does_not_cycle_with_recovery(
    logged_in_client, monkeypatch, tmp_path
):
    """R3.4's attachment set has to be COMPLETE, the create's named `attachment_uids` included.

    Without them pass 0 locks only the dedup target, and `link_attachments` acquires the named row
    afterwards — a second attachment-class acquisition in the same transaction, in an order nothing
    else shares. R7a's sweep takes its terminal set ascending, so the two invert: the sweep holds
    the low-id named attachment and waits for the dedup target the create is holding, while the
    create waits for the named one.

    Green on the unmodified tree by construction — there the sweep takes its attachments in JOB
    order, which reaches the dedup target first and never waits on the create. Its red tree is
    `tests/mutations/no_named_arm.patch`, run once under `MEMNG267_MUTATION=no_named_arm`.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        (
            created,
            recovered,
            _locks,
            named,
            dedup_target,
            _named_id,
            _target_id,
            high_bytes,
            low_bytes,
        ) = await _create_with_a_named_attachment_beside_the_recovery_sweep(client, monkeypatch)
        assert created.status_code == 201, created.text
        assert recovered == 2, recovered
        assert _memo_attachment_uids(created.json()["uid"]) == [named, dedup_target]
        assert _memo_attachment_media(created.json()["uid"]) == [
            ("named.png", low_bytes),
            ("target.png", high_bytes),
        ]
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


async def test_pass_zero_locks_the_named_and_the_dedup_attachment_in_one_ascending_statement(
    logged_in_client, monkeypatch, tmp_path
):
    """The ordering half of F2, split out so the completion probe above stays readable: the FIRST
    ascending attachment `FOR UPDATE` the create emits must already carry BOTH ids, and every later
    attachment-class acquisition is a re-lock of rows it already holds."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        (
            created,
            _recovered,
            locks,
            _named,
            _dedup_target,
            named_id,
            target_id,
            _high,
            _low,
        ) = await _create_with_a_named_attachment_beside_the_recovery_sweep(client, monkeypatch)
        assert created.status_code == 201, created.text
        assert locks, "no ascending attachment FOR UPDATE was emitted at all"
        # Positive control on the capture: the ids really do appear in a statement's parameters,
        # so an id-not-found below is a missing lock and not a listener that saw nothing.
        values = [set(p.values()) if isinstance(p, dict) else set(p) for _, p in locks]
        assert any(named_id in v for v in values), locks
        first = values[0]
        assert named_id in first and target_id in first, (named_id, target_id, values)
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# preservation probes — already-safe pairs that must STAY safe (no manufactured baseline failure)
# --------------------------------------------------------------------------------------------


async def test_worker_publication_and_reclaim_keep_the_jobs_then_attachment_order(
    logged_in_client, monkeypatch, tmp_path
):
    """R7/X9: publication and reclaim are both J → A, and neither reverses. The publish guard is
    parked between the two — holding its job row, about to take the attachment — while a reclaim of
    that same attachment runs its own J-then-A transaction beside it. The reclaim's SKIP LOCKED job
    sweep steps around the claimed row instead of queueing on it, which is the property MEMNG-41
    bought and this protocol must not spend."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        target = _unlinked_attachment(client, "clip.jpg", _jpeg((3, 6, 9)), "image/jpeg")
        attachment_id = _one("SELECT id FROM attachments WHERE uid = :u", u=target)[0]

        park = Park()
        real_exists = media_svc.attachment_still_exists

        async def parking(db, wanted_id):
            # Parked between guard_publish's two acquisitions: the job row is held, the attachment
            # row is the very next statement.
            if park.claim():
                await park.hold()
            return await real_exists(db, wanted_id)

        monkeypatch.setattr(media_svc, "attachment_still_exists", parking)

        async def publish_guard():
            async with async_session() as db:
                job = (
                    (
                        await db.execute(
                            select(MediaJob).where(MediaJob.attachment_id == attachment_id)
                        )
                    )
                    .scalars()
                    .first()
                )
                attachment = (
                    await db.execute(select(Attachment).where(Attachment.id == attachment_id))
                ).scalar_one()
                try:
                    await media_svc.guard_publish(db, job, attachment)
                    return "published"
                except media_svc.AttachmentGone:
                    return "discarded"
                finally:
                    await db.rollback()

        async def reclaim():
            async with async_session() as db:
                return await reclaim_svc.reclaim_attachment(
                    db, attachment_id, reclaim_svc.Revalidate.IDENTITY
                )

        guard = asyncio.create_task(publish_guard())
        await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
        # No wait to observe, and that IS the assertion: the reclaim must finish while the guard
        # holds the job row, because its job cleanup is SKIP LOCKED and its attachment lock is free.
        result = await asyncio.wait_for(reclaim(), timeout=WAIT_TIMEOUT)
        park.release.set()
        outcome = _answers([await asyncio.wait_for(guard, timeout=WAIT_TIMEOUT)])[0]

        park.fired()
        assert result.outcome == "reclaimed", result
        assert outcome == "discarded", outcome  # the delete won; the job publishes nothing
        assert _one("SELECT count(*) FROM attachments")[0] == 0
        # The claimed job row SURVIVES: reclaim's cleanup is SKIP LOCKED, so it leaves the row its
        # worker is holding rather than queueing on it (MEMNG-41, X6). That is the property, not a
        # leak — the worker discards the job on finding the attachment gone.
        assert _one("SELECT count(*) FROM media_jobs")[0] == 1
    finally:
        _cleanup()


async def _finalize_parked_on(client, monkeypatch, uid: str) -> tuple[Park, object]:
    """Start a standalone finalize and hold it inside its transaction on `uid`'s upload row."""
    park = Park()
    _park_ensure_capture(monkeypatch, park, lambda held: held == uid)
    holder = _async_client(client)
    task = asyncio.create_task(holder.post(f"/api/uploads/{uid}/finalize"))
    await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
    return park, (holder, task)


async def _release(park: Park, handle) -> object:
    holder, task = handle
    park.release.set()
    try:
        return _answers([await asyncio.wait_for(task, timeout=WAIT_TIMEOUT)])[0]
    finally:
        await holder.aclose()


async def test_the_append_writer_serializes_behind_a_parked_finalize_holding_its_row(
    logged_in_client, monkeypatch, tmp_path
):
    """§5.2's preservation row, quoted: "append, capture GET and `reap_expired` each run beside a
    parked finalize holding U (the census's 'every writer beside a parked conflicting actor'
    requirement)". The conflicting actor holds THIS upload's row, so the append must queue on it —
    a probe aimed at some other row could not fail if the serialization were removed.

    `append_stream` re-reads the row FOR UPDATE precisely so the merge decides against the
    committed offset, so the append blocks until the finalize commits, and then finds the session
    consumed. What it answers then is the documented one (MEMNG-278, R6a): a lock acquisition may
    lose its row, and losing it is an ordinary 404 — the same status and the same body an unknown
    uid gets from `_get_upload` (`app/routers/uploads.py:90-91`), never a 500 out of
    `.scalar_one()`.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = _png(b"APPEND")
        uid = _upload(client, "part.png", data)

        park, handle = await _finalize_parked_on(client, monkeypatch, uid)
        waiting = set(_lock_waiters())
        async with _async_client(client) as neighbour:
            # An EMPTY body at the current offset: the one append shape that reaches the row lock
            # without changing the bytes (a client re-syncing after a lost response). A body with
            # content would be refused by the receive-time ceiling before it ever queued, and a
            # short upload cannot be the parked finalize's row at all.
            append = asyncio.create_task(
                neighbour.patch(
                    f"/api/uploads/{uid}",
                    content=b"",
                    headers={"Upload-Offset": str(len(data))},
                )
            )
            # THE assertion of this probe: the writer is genuinely queued on the parked row.
            await _wait_for_a_new_lock_wait(waiting)
            finalized = await _release(park, handle)
            appended = list(await asyncio.gather(append, return_exceptions=True))[0]

        park.fired()
        assert finalized.status_code == 200, finalized.text  # the finalize consumed the session
        # The append reached its FOR UPDATE after the row was gone. It must ANSWER, not raise:
        # exactly 404, with the body every other missing-upload answer on this router carries.
        assert not isinstance(appended, BaseException), repr(appended)
        assert appended.status_code == 404, appended.text
        assert appended.json() == {"detail": "Upload not found"}, appended.text
        assert _one("SELECT count(*) FROM uploads")[0] == 0
        assert _one("SELECT count(*) FROM attachments")[0] == 1
    finally:
        _cleanup()


async def test_the_capture_reader_serializes_behind_a_parked_finalize_holding_its_row(
    logged_in_client, monkeypatch, tmp_path
):
    """Same row, the capture GET. Its documented answer for a session that vanished while it waited
    is a 404 (`app/routers/uploads.py:216-218`), and that is what it must still give."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _upload(client, "held.png", _png(b"CAPTURE"))
        park, handle = await _finalize_parked_on(client, monkeypatch, uid)
        waiting = set(_lock_waiters())
        async with _async_client(client) as neighbour:
            capture = asyncio.create_task(neighbour.get(f"/api/uploads/{uid}/capture"))
            await _wait_for_a_new_lock_wait(waiting)
            finalized = await _release(park, handle)
            captured = await capture

        park.fired()
        assert finalized.status_code == 200, finalized.text
        assert captured.status_code == 404, captured.text  # the session is somebody else's now
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


async def test_the_expiry_reaper_serializes_behind_a_parked_finalize_holding_its_row(
    logged_in_client, monkeypatch, tmp_path
):
    """Same row, `reap_expired`. It claims its expired set with one ascending `FOR UPDATE` before
    it touches a byte, so the CLAIM is what waits, and once the finalize has committed there is no
    row left to reap. The bytes the finalize placed must survive the sweep.

    The finalize is parked AFTER its placement here. The window BEFORE it — where the sweep used to
    unlink a part file its owner had not moved yet — is MEMNG-279 and has its own probe below; this
    one stays where it was so the pair covers both sides of the placement.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        payload = _png(b"REAP")
        uid = _upload(client, "held.png", payload)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(
                text(
                    "UPDATE uploads SET expires_at = now() - interval '1 day' "
                    "WHERE upload_uid = :u"
                ),
                {"u": uid},
            )
        eng.dispose()

        park = Park()
        _park_finalize(monkeypatch, park, lambda held: held == uid)
        holder = _async_client(client)
        finalize_task = asyncio.create_task(holder.post(f"/api/uploads/{uid}/finalize"))
        await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
        handle = (holder, finalize_task)
        waiting = set(_lock_waiters())

        async def reap() -> int:
            async with async_session() as db:
                reaped = await uploads_svc.reap_expired(db)
                await db.commit()
                return reaped

        sweep = asyncio.create_task(reap())
        await _wait_for_a_new_lock_wait(waiting)
        finalized = await _release(park, handle)
        reaped = _answers([await asyncio.wait_for(sweep, timeout=WAIT_TIMEOUT)])[0]

        park.fired()
        assert finalized.status_code == 200, finalized.text
        # `reap_expired` returns the size of the set it CLAIMED under the row lock, so 0 here says
        # it took nothing: by the time the lock was granted the row was the finalize's to delete
        # and had gone. (Before MEMNG-279 this read 1 — the size of an UNLOCKED select taken before
        # the wait, which counted a row the sweep never removed.)
        assert reaped == 0, reaped
        stored = _one("SELECT storage_path FROM attachments")[0]
        assert Path(stored).read_bytes() == payload  # rows AND bytes: the master survived the sweep
        assert _one("SELECT count(*) FROM uploads")[0] == 0
    finally:
        _cleanup()


async def test_the_expiry_reaper_does_not_unlink_a_part_the_finalize_has_not_moved_yet(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-279 — the window the probe above deliberately does NOT cover: the finalize is parked
    BEFORE its placement, holding U with its bytes still in the `.part` file.

    The sweep reaches that part first. Unlinking it before any lock is taken destroys the only copy
    of an upload another transaction already owns, and the finalize then dies on the missing bytes
    — the reaper must claim the row under its U lock before it touches the file (design §2.1's
    total order; §2.5 permits the unlink INSIDE that window because it is bounded local I/O under
    DATA_ROOT).

    Rows AND bytes, both directions: the finalize's master is on disk under its `storage_path`, and
    the sweep reaped nothing — the row it saw was the finalize's to delete.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        payload = _png(b"WINDOW")
        uid = _upload(client, "unmoved.png", payload)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(
                text(
                    "UPDATE uploads SET expires_at = now() - interval \'1 day\' "
                    "WHERE upload_uid = :u"
                ),
                {"u": uid},
            )
        eng.dispose()
        part = uploads_svc.part_path(uid)
        # The control for the byte assertions below: the bytes really are in the part file, and
        # really are still there when the sweep starts, so their survival is the fix's doing.
        assert part.read_bytes() == payload

        # Parked at `ensure_capture`: U held, NOTHING moved yet — the one instant this defect
        # needs. `_park_finalize` (the probe above) returns after the placement and cannot see it.
        park = Park()
        _park_ensure_capture(monkeypatch, park, lambda held: held == uid)
        holder = _async_client(client)
        finalize_task = asyncio.create_task(holder.post(f"/api/uploads/{uid}/finalize"))
        await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
        handle = (holder, finalize_task)
        assert part.read_bytes() == payload  # still unmoved while the finalize holds the row
        waiting = set(_lock_waiters())

        async def reap() -> int:
            async with async_session() as db:
                reaped = await uploads_svc.reap_expired(db)
                await db.commit()
                return reaped

        sweep = asyncio.create_task(reap())
        # Released on ANY observed wait (§5.1's accommodation): the sweep blocks at its DELETE
        # flush on the unfixed tree and at its own U acquisition on the fixed one.
        await _wait_for_a_new_lock_wait(waiting)
        finalized = await _release(park, handle)
        reaped = _answers([await asyncio.wait_for(sweep, timeout=WAIT_TIMEOUT)])[0]

        park.fired()
        assert finalized.status_code == 200, finalized.text
        stored = _one("SELECT storage_path FROM attachments")[0]
        assert Path(stored).read_bytes() == payload  # the bytes the reaper nearly destroyed
        # Nothing the finalize owned was reaped: after the wait the row is gone from the sweep's
        # own locked set, so it unlinks nothing and deletes nothing.
        assert reaped == 0, reaped
        assert _one("SELECT count(*) FROM uploads")[0] == 0
        assert not part.exists()  # consumed by the placement, not by the sweep
    finally:
        _cleanup()


async def test_rendition_deletion_and_multipart_print_run_beside_a_parked_attach(
    logged_in_client, monkeypatch, tmp_path
):
    """The rendition class's other writers — the API delete and the legacy multipart `/print` —
    beside an interactive attach that holds THIS attachment's row and THIS upload's row.

    The two writers are deliberately different shapes against the same held attachment: the
    rendition DELETE touches only the `attachment_renditions` row, which the parked attach does not
    hold yet, so it completes; the multipart `/print` takes the attachment's own row
    (`app/routers/assets.py:707`, a locking refresh), so it queues. Both must land, and the attach
    must then decide from state read THROUGH its lock — the rendition it was going to replace is
    gone, so it inserts rather than replacing (X3).
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        held_target = _unlinked_attachment(client, "held.jpg", _jpeg((5, 10, 15)), "image/jpeg")
        seeded = client.post(
            f"/api/attachments/{held_target}/renditions",
            json={"upload_uid": _upload(client, "m.jpg", _jpeg((35, 40, 45)), "image/jpeg")},
        )
        assert seeded.status_code == 201, seeded.text
        replacement = _jpeg((50, 55, 60))
        attach_upload = _upload(client, "hold.jpg", replacement, "image/jpeg")
        print_bytes = _jpeg((65, 70, 75))

        park = Park()
        real_attach = uploads_svc.attach_print_master

        async def parking(*args, **kwargs):
            # Parked with the upload row and the attachment row both held, before the placement.
            if park.claim():
                await park.hold()
            return await real_attach(*args, **kwargs)

        monkeypatch.setattr(uploads_svc, "attach_print_master", parking)

        async with _async_client(client) as attacher, _async_client(client) as neighbour:
            attach = asyncio.create_task(
                attacher.post(
                    f"/api/attachments/{held_target}/renditions",
                    json={"upload_uid": attach_upload},
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            deleted = await neighbour.delete(f"/api/attachments/{held_target}/rendition/print")
            waiting = set(_lock_waiters())
            printed = asyncio.create_task(
                neighbour.post(
                    f"/api/attachments/{held_target}/print",
                    files={"file": ("p.jpg", print_bytes, "image/jpeg")},
                )
            )
            # The multipart print takes the held attachment's row: it must queue, not race.
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            attached, printed_res = await _both(attach, printed)

        park.fired()
        assert deleted.status_code == 204, deleted.text
        assert attached.status_code == 201, attached.text
        assert printed_res.status_code == 200, printed_res.text
        # Rows AND bytes on both writers' results.
        path, key = _one(
            "SELECT r.storage_path, r.variant_key FROM attachment_renditions r "
            "JOIN attachments a ON a.id = r.attachment_id WHERE a.uid = :u",
            u=held_target,
        )
        assert key == "print"
        assert Path(path).read_bytes() == replacement  # the attach's file, not the deleted one
        print_path = _one(
            "SELECT print_path FROM attachments WHERE uid = :u", u=held_target
        )[0]
        assert print_path is not None
        assert Path(print_path).read_bytes() == print_bytes
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# C8 (phase 2, MEMNG-280) — memo DELETE ∥ PATCH of that memo, and the missing-memo continuation
# --------------------------------------------------------------------------------------------


def _commit_sql(query: str, **params) -> None:
    """Run one statement on its OWN connection and COMMIT it.

    The probes below need a memo to genuinely vanish under a parked reader, and §5.1 forbids the
    `adb` fixture for exactly that: its outer transaction is rolled back at teardown and is
    invisible to a second connection (`tests/conftest.py:65-75`). `_sql` opens a connection it
    never commits, so it cannot be the deleter.
    """
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text(query), params)
    eng.dispose()


def _park_before_the_memo_delete_statement(park: Park, statements: list[str]):
    """Park the deleting actor BETWEEN the ORM flush's `UPDATE attachments SET memo_id=NULL` and
    its `DELETE FROM memos` — the instant C8 is built on, where A is held and M is about to be
    asked for.

    Both statements come out of ONE `db.flush()` inside `delete_memo` (`app/services/memos.py`),
    so there is no Python boundary between them and the module's `AsyncSession.execute` wrapper
    never sees them: an ORM flush goes to the connection, not through `execute`.
    `before_cursor_execute` is the only seam that sits between the two, and `await_only` is how a
    coroutine is awaited from inside the greenlet SQLAlchemy runs that callback in. The seam lives
    here, in the test harness — nothing is added to `app/` for it.
    """

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))
        if statement.lstrip().upper().startswith("DELETE FROM MEMOS") and park.claim():
            await_only(park.hold())

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    return record


def _park_before_the_memo_lock(monkeypatch, park: Park) -> None:
    """Park an actor at the instant BEFORE it queues for the memo row lock — the window R6a is
    about: the caller has read the memo unlocked, and by the time the lock is granted the row it
    read may be gone."""
    real = memos_router.svc.lock_memo_for_write

    async def parking(db, memo):
        if park.claim():
            await park.hold()
        return await real(db, memo)

    monkeypatch.setattr(memos_router.svc, "lock_memo_for_write", parking)


def _park_after_require_owned(monkeypatch, park: Park) -> None:
    """Park a route between its unlocked ownership read and everything that follows.

    This is the one seam that exists on BOTH trees for the DELETE probe: before R6 the route goes
    straight from here into `svc.delete_memo`, after R6 it goes into `lock_memo_for_write`, and
    the probe must schedule the same race against each.
    """
    real = memos_router._require_owned

    async def parking(db, user, uid):
        memo = await real(db, user, uid)
        if park.claim():
            await park.hold()
        return memo

    monkeypatch.setattr(memos_router, "_require_owned", parking)


async def test_memo_delete_and_patch_of_that_memo_do_not_cycle(
    logged_in_client, monkeypatch, tmp_path
):
    """C8. The DELETE's ORM flush takes A (`UPDATE attachments SET memo_id=NULL`) and then wants M;
    a PATCH of the same memo holds M and wants the memo's attachment set. Before R6 the DELETE
    route takes no memo lock at all, so the two invert.

    The loser is not merely "not a deadlock": the PATCH's `lock_memo_for_write` is granted a memo
    that no longer exists, which before R6a was a `NoResultFound` out of `.scalar_one()` — a 500
    on the very interleaving R6 exists to make safe.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    statements: list[str] = []
    park = Park()
    listener = None
    try:
        memo = _memo(client, "delete me", upload_uids=[_upload(client, "c8.png", _png(b"C8"))])
        added = _upload(client, "c8b.png", _png(b"C8b"))
        # The two files this race is judged on (§5.1 wants rows AND bytes): the master the winning
        # delete has to sweep, and the part file the losing PATCH must leave exactly as it found
        # it. Read now, so the after-picture is compared against real bytes and not against
        # whatever the assertions themselves could invent.
        master = Path(_one("SELECT storage_path FROM attachments")[0])
        assert master.read_bytes() == _png(b"C8")
        pending = uploads_svc.part_path(added)
        assert pending.read_bytes() == _png(b"C8b")
        listener = _park_before_the_memo_delete_statement(park, statements)

        async with _async_client(client) as deleter, _async_client(client) as editor:
            delete = asyncio.create_task(deleter.delete(f"/api/memos/{memo['uid']}"))
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            waiting = set(_lock_waiters())
            edit = asyncio.create_task(
                editor.patch(
                    f"/api/memos/{memo['uid']}",
                    json={"upload_uids": [added], "edit_key": "c8-lost-memo-key"},
                )
            )
            await _wait_for_a_new_lock_wait(waiting)
            park.release.set()
            deleted, edited = await _both(delete, edit)

        park.fired()
        # The implicit order this cycle rests on, read off the wire rather than assumed: without
        # this the probe could pass for a reason that has nothing to do with the ORM's flush order.
        upper = [s.upper() for s in statements]
        updates = [i for i, s in enumerate(upper) if s.startswith("UPDATE ATTACHMENTS")]
        deletes = [i for i, s in enumerate(upper) if s.startswith("DELETE FROM MEMOS")]
        assert any("FOR UPDATE" in s for s in upper), statements  # capture control
        assert updates and deletes, statements
        assert min(updates) < min(deletes), (updates, deletes)

        assert deleted.status_code == 204, deleted.text
        assert edited.status_code == 404, edited.text
        assert edited.json() == {"detail": "Memo not found"}
        # No edit key was consumed: it is recorded only after the memo lock is granted, so the
        # client's key is still free for the retry it will make against a memo that exists.
        assert _one("SELECT count(*) FROM memo_edit_keys")[0] == 0
        # The memo is gone, its attachment was orphaned and then reclaimed, and the PATCH's upload
        # never became one — the loser moved no bytes.
        assert _sql("SELECT id FROM memos WHERE uid = :u", u=memo["uid"]) == []
        assert _sql("SELECT id FROM attachments") == []
        # BYTES, not only rows. Reclaim ran inside the winning request (its phase 2), so the
        # master is gone with the row — a reclaim that dropped the row and left the file is the
        # one outcome app/services/reclaim.py exists to prevent, and it would satisfy every row
        # assertion above.
        assert not master.exists(), "the deleted memo's master was left behind"
        # The loser moved nothing: its upload is still pending and still holds its own bytes, so
        # the client's retry against a memo that exists has something to retry with. Compared by
        # CONTENT — an emptied or truncated part file exists just as happily as an intact one.
        assert _one("SELECT count(*) FROM uploads WHERE upload_uid = :u", u=added)[0] == 1
        assert pending.read_bytes() == _png(b"C8b")
    finally:
        if listener is not None:
            event.remove(async_engine.sync_engine, "before_cursor_execute", listener)
        _cleanup()


async def test_a_patch_that_loses_its_memo_answers_404_not_500(
    logged_in_client, monkeypatch, tmp_path
):
    """R6a on the PATCH path, without a deadlock in the way: park the edit before it queues for M,
    delete the memo on a second connection and commit, then let the lock be granted.

    Before R6a `lock_memo_for_write` ended in `.scalar_one()`, so this answered with a
    `NoResultFound` escaping the route — a 500 for an outcome the API already has a word for.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo(client, "patch me", upload_uids=[_upload(client, "p404.png", _png(b"P4"))])
        master = Path(_one("SELECT storage_path FROM attachments")[0])
        assert master.read_bytes() == _png(b"P4")
        added = _upload(client, "p404b.png", _png(b"P4b"))
        pending = uploads_svc.part_path(added)
        assert pending.read_bytes() == _png(b"P4b")
        park = Park()
        _park_before_the_memo_lock(monkeypatch, park)

        async with _async_client(client) as editor:
            edit = asyncio.create_task(
                editor.patch(
                    f"/api/memos/{memo['uid']}",
                    json={
                        "content": "edited",
                        "upload_uids": [added],
                        "edit_key": "lost-memo",
                    },
                )
            )
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            await asyncio.to_thread(_commit_sql, "DELETE FROM memos WHERE uid = :u", u=memo["uid"])
            park.release.set()
            edited = await edit

        park.fired()
        assert edited.status_code == 404, edited.text
        assert edited.json() == {"detail": "Memo not found"}
        # Raised inside `attempt()` BEFORE `record_edit_key`, so the key is untouched.
        assert _one("SELECT count(*) FROM memo_edit_keys")[0] == 0
        # And before any byte, which only the files can say: the edit's own upload is still
        # pending with its bytes intact, and the memo's master — orphaned by the delete, not
        # reclaimed by it — was not touched by the request that lost.
        assert pending.read_bytes() == _png(b"P4b")
        assert master.read_bytes() == _png(b"P4")
    finally:
        _cleanup()


async def test_a_delete_that_loses_its_memo_answers_404_not_500(
    logged_in_client, monkeypatch, tmp_path
):
    """R6a's DELETE sibling. Two deleters of one memo: the first is parked after its unlocked
    ownership read, the second commits, and the first is then let go.

    Before R6 the route went straight from that read into `db.delete(memo)`, and its flush matched
    no row: SQLAlchemy logged `DELETE statement on table 'memos' expected to delete 1 row(s); 0
    were matched` as a WARNING and the route answered **204** — success for work it did not do,
    and for a memo it never held. Under R6 the memo lock is taken first and answers what actually
    happened: the row is gone, and that is a 404.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo(
            client, "delete me twice", upload_uids=[_upload(client, "d404.png", _png(b"D4"))]
        )
        master = Path(_one("SELECT storage_path FROM attachments")[0])
        assert master.read_bytes() == _png(b"D4")
        park = Park()
        _park_after_require_owned(monkeypatch, park)

        async with _async_client(client) as deleter:
            request = asyncio.create_task(deleter.delete(f"/api/memos/{memo['uid']}"))
            await asyncio.wait_for(park.entered.wait(), timeout=WAIT_TIMEOUT)
            await asyncio.to_thread(_commit_sql, "DELETE FROM memos WHERE uid = :u", u=memo["uid"])
            park.release.set()
            answered = await request

        park.fired()
        assert answered.status_code == 404, answered.text
        assert answered.json() == {"detail": "Memo not found"}
        assert _sql("SELECT id FROM memos WHERE uid = :u", u=memo["uid"]) == []
        # The loser reclaimed NOTHING. The winner here deleted the memo row alone, so the
        # attachment is an orphan the sweep will collect — and the request that answered 404 must
        # not have unlinked its master on the way out, which no row assertion can tell.
        assert _one("SELECT count(*) FROM attachments")[0] == 1
        assert master.read_bytes() == _png(b"D4")
    finally:
        _cleanup()
