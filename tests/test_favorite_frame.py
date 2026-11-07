"""Hearted pictures to the favorites album (plan PLAN_2026-11-06_favorites-to-aura-frame.md, B2).

A heart upserts one `favorite_frame_jobs` row due one debounce after the DATABASE clock; a re-heart
restarts that window and resurrects any row short of `sent`. The worker claims a due row, re-reads
the tag, and either uploads to FRAME_FAVORITES_ALBUM_ID or cancels through ONE conditional UPDATE
that only matches while the tag is absent and its own lease stands. Committed sessions throughout:
the interleavings need separate connections to see each other.
"""

import asyncio
import secrets
from pathlib import Path

import httpx
import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import Attachment, FavoriteFrameJob, User, Visibility
from app.services import frame_share as fs
from app.services import memos as memos_svc
from PIL import Image
from sqlalchemy import select, text

from tests import test_collections_api as coll_api
from tests.test_attachments_taken import _sql

lib = coll_api.lib

CURATED_ALBUM = 5
FAVORITES_ALBUM = 13
UNFAVORITED = "unfavorited before send"


def _configure(monkeypatch, *, curated: int = CURATED_ALBUM, favorites: int = FAVORITES_ALBUM):
    monkeypatch.setattr(settings, "FRAME_API_URL", "http://aura-frames.test")
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", "tok")
    monkeypatch.setattr(settings, "FRAME_ALBUM_ID", curated)
    monkeypatch.setattr(settings, "FRAME_FAVORITES_ALBUM_ID", favorites)


def _album(monkeypatch, captured: list, *, photo_id: int = 4242, during=None):
    """aura-frames at the HTTP boundary: answers 201 with an album photo id. `during` (async) runs
    while the upload is in flight — the re-heart-while-uploading interleaving."""

    async def handler(request):
        captured.append(request)
        if during is not None:
            await during()
        return httpx.Response(201, json={"status": "created", "id": photo_id})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        fs.httpx, "AsyncClient", lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    )


def _pic(tmp_path: Path, owner: str, memo_id: int | None = None, *, mime="image/jpeg") -> int:
    f = tmp_path / f"{secrets.token_hex(4)}.jpg"
    Image.new("RGB", (40, 30), (10, 20, 30)).save(f, "JPEG")
    return _sql(
        "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
        "storage_path) VALUES (:u,(SELECT id FROM users WHERE subject=:o),:m,'pic.jpg',:mime,1,"
        "CAST('ready' AS attachment_status),:p) RETURNING id",
        {"u": secrets.token_urlsafe(8), "o": owner, "m": memo_id, "mime": mime, "p": str(f)},
    )[0][0]


def _uid(att_id: int) -> str:
    return _sql("SELECT uid FROM attachments WHERE id=:a", {"a": att_id})[0][0]


def _tag_only(att_id: int, name: str = "favorites") -> None:
    """The tag row alone, as a heart whose enqueue never ran would leave it."""
    tid = _sql(
        "INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=:n RETURNING id",
        {"n": name},
    )[0][0]
    _sql(
        "INSERT INTO attachment_tags (attachment_id, tag_id) VALUES (:a,:t) ON CONFLICT DO NOTHING",
        {"a": att_id, "t": tid},
    )


def _untag(att_id: int) -> None:
    _sql(
        "DELETE FROM attachment_tags WHERE attachment_id=:a "
        "AND tag_id IN (SELECT id FROM tags WHERE name='favorites')",
        {"a": att_id},
    )


def _job(att_id: int) -> dict | None:
    rows = _sql(
        "SELECT state::text AS state, attempts, error, claimed_by, aura_album_photo_id, sent_at, "
        "EXTRACT(EPOCH FROM next_attempt_at - now()) AS due_in "
        "FROM favorite_frame_jobs WHERE attachment_id=:a",
        {"a": att_id},
    )
    return dict(rows[0]._mapping) if rows else None


def _make_due(att_id: int) -> None:
    _sql(
        "UPDATE favorite_frame_jobs SET next_attempt_at = now() - interval '1 second' "
        "WHERE attachment_id=:a",
        {"a": att_id},
    )


async def _heart(att_id: int) -> None:
    """A full heart in its own committed transaction: tag + enqueue, like the PUT."""
    async with async_session() as db:
        att = await db.get(Attachment, att_id)
        assert att is not None
        user = await db.get(User, att.owner_id)
        assert user is not None
        _tag_only(att_id)
        assert await fs.enqueue_favorite(db, att, user)
        await db.commit()


async def _once() -> str:
    async with async_session() as db:
        return await worker.run_favorite_frame_once(db)


# --- enqueue: the heart is the only sink -------------------------------------------------------


def test_a_heart_enqueues_a_job_due_one_debounce_after_the_db_clock(lib, monkeypatch, tmp_path):
    client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    job = _job(att)
    assert job is not None and job["state"] == "queued" and job["attempts"] == 0
    assert 55 <= float(job["due_in"]) <= 65


def test_a_re_heart_restarts_the_window_from_the_latest_heart(lib, monkeypatch, tmp_path):
    client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    # 30 s later the row is 30 s from due; a re-heart moves it back to a full window.
    _sql(
        "UPDATE favorite_frame_jobs SET next_attempt_at = now() + interval '30 seconds' "
        "WHERE attachment_id=:a",
        {"a": att},
    )
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    job = _job(att)
    assert job is not None and 55 <= float(job["due_in"]) <= 65


@pytest.mark.parametrize("state", ["skipped", "failed", "claimed"])
def test_a_re_heart_resurrects_any_row_short_of_sent(lib, monkeypatch, tmp_path, state):
    client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    _sql(
        "UPDATE favorite_frame_jobs SET state=CAST(:s AS frame_share_state), attempts=3, "
        "error='old', claimed_by='w', claimed_at=now() WHERE attachment_id=:a",
        {"s": state, "a": att},
    )
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    job = _job(att)
    assert job is not None
    assert (job["state"], job["attempts"], job["error"], job["claimed_by"]) == (
        "queued",
        0,
        None,
        None,
    )
    assert 55 <= float(job["due_in"]) <= 65


def test_a_re_heart_of_a_sent_row_changes_nothing(lib, monkeypatch, tmp_path):
    client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    _sql(
        "UPDATE favorite_frame_jobs SET state='sent', sent_at=now(), next_attempt_at=NULL, "
        "aura_album_photo_id=7 WHERE attachment_id=:a",
        {"a": att},
    )
    assert client.delete(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    job = _job(att)
    assert job is not None
    assert (job["state"], job["due_in"], job["aura_album_photo_id"]) == ("sent", None, 7)


def test_another_tag_name_enqueues_nothing(lib, monkeypatch, tmp_path):
    client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/holiday").status_code == 204
    assert _job(att) is None


def test_favorites_unconfigured_enqueues_nothing_but_the_heart_still_lands(
    lib, monkeypatch, tmp_path
):
    client, me = lib
    _configure(monkeypatch, favorites=0)
    att = _pic(tmp_path, me)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    assert _job(att) is None
    assert _sql("SELECT count(*) FROM attachment_tags WHERE attachment_id=:a", {"a": att})[0][0]


def test_a_queue_error_leaves_the_heart_204_and_the_tag_set(lib, monkeypatch, tmp_path):
    client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)

    async def boom(db, *_a, **_kw):
        # A real statement error on the heart's own connection: without the savepoint it aborts
        # the whole transaction and the tag write is lost with it.
        await db.execute(text("INSERT INTO favorite_frame_jobs (no_such_column) VALUES (1)"))

    monkeypatch.setattr(fs, "enqueue_favorite", boom)
    assert client.put(f"/api/attachments/{_uid(att)}/tags/favorites").status_code == 204
    assert _job(att) is None
    assert _sql("SELECT count(*) FROM attachment_tags WHERE attachment_id=:a", {"a": att})[0][0]


async def test_a_memo_hashtag_favorites_enqueues_nothing(lib, monkeypatch, tmp_path):
    _client, me = lib
    _configure(monkeypatch)
    att = _pic(tmp_path, me)
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.subject == me))).scalar_one()
        await memos_svc.create_memo(
            db,
            user,
            content="#favorites",
            visibility=Visibility.PRIVATE,
            attachment_uids=[_uid(att)],
        )
        await db.commit()
    assert _job(att) is None


# --- claim, send, cancel -----------------------------------------------------------------------


async def test_the_claim_waits_for_the_not_before(lib, monkeypatch, tmp_path):
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    att = _pic(tmp_path, me)
    await _heart(att)
    assert await _once() == "idle"
    assert _job(att)["state"] == "queued" and captured == []  # type: ignore[index]


async def test_a_due_favorite_uploads_to_the_favorites_album_and_keeps_the_id(
    lib, monkeypatch, tmp_path
):
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured, photo_id=9001)
    memo_id = coll_api._memo(f"fm-{secrets.token_hex(3)}", me)
    att = _pic(tmp_path, me, memo_id)
    await _heart(att)
    _make_due(att)
    assert await _once() == "processed"
    job = _job(att)
    assert job is not None and job["state"] == "sent" and job["aura_album_photo_id"] == 9001
    assert [r.url.path for r in captured] == [f"/api/albums/{FAVORITES_ALBUM}/photos"]


async def test_a_memo_less_picture_sends(lib, monkeypatch, tmp_path):
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    att = _pic(tmp_path, me, None)
    await _heart(att)
    _make_due(att)
    assert await _once() == "processed"
    assert _job(att)["state"] == "sent" and len(captured) == 1  # type: ignore[index]


async def test_an_unhearted_picture_is_skipped_by_the_conditional_update_and_never_sent(
    lib, monkeypatch, tmp_path
):
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    att = _pic(tmp_path, me)
    await _heart(att)
    _untag(att)
    _make_due(att)
    assert await _once() == "processed"
    job = _job(att)
    assert job is not None and (job["state"], job["error"]) == ("skipped", UNFAVORITED)
    assert job["aura_album_photo_id"] is None and captured == []


async def test_the_second_favorite_waits_for_the_pace(lib, monkeypatch, tmp_path):
    _client, me = lib
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "FRAME_FAVORITE_PACE_SECONDS", 10)
    captured: list = []
    _album(monkeypatch, captured)
    first, second = _pic(tmp_path, me), _pic(tmp_path, me)
    for att in (first, second):
        await _heart(att)
        _make_due(att)
    assert await _once() == "processed"
    assert await _once() == "deferred"
    assert sorted(j["state"] for j in (_job(first), _job(second))) == ["queued", "sent"]  # type: ignore[index]
    _sql(
        "UPDATE favorite_frame_jobs SET sent_at = now() - interval '11 seconds' WHERE state='sent'"
    )
    assert await _once() == "processed"
    assert len(captured) == 2


# --- routing: which queue drains under which config (one case per category) ---------------------


def _curated(att_id: int) -> None:
    _sql(
        "INSERT INTO frame_share_jobs (attachment_id, memo_id, user_id, state, attempts) "
        "SELECT a.id, a.memo_id, a.owner_id, 'queued', 0 FROM attachments a WHERE a.id=:a",
        {"a": att_id},
    )
    tid = _sql(
        "INSERT INTO tags (name) VALUES ('curated') "
        "ON CONFLICT (name) DO UPDATE SET name='curated' RETURNING id"
    )[0][0]
    _sql(
        "INSERT INTO memo_tags (memo_id, tag_id) SELECT memo_id, :t FROM attachments WHERE id=:a "
        "ON CONFLICT DO NOTHING",
        {"t": tid, "a": att_id},
    )


def _curated_state(att_id: int) -> str:
    return _sql("SELECT state::text FROM frame_share_jobs WHERE attachment_id=:a", {"a": att_id})[
        0
    ][0]


@pytest.mark.parametrize(
    ("curated", "favorites", "expect_curated", "expect_favorite", "paths"),
    [
        (CURATED_ALBUM, FAVORITES_ALBUM, "sent", "sent", {CURATED_ALBUM, FAVORITES_ALBUM}),
        (0, FAVORITES_ALBUM, "queued", "sent", {FAVORITES_ALBUM}),
        (CURATED_ALBUM, 0, "sent", "queued", {CURATED_ALBUM}),
        (0, 0, "queued", "queued", set()),
    ],
    ids=["both", "favorites-only", "curated-only", "neither"],
)
async def test_each_queue_drains_only_under_its_own_config_even_for_one_picture(
    lib, monkeypatch, tmp_path, curated, favorites, expect_curated, expect_favorite, paths
):
    # One picture that is both curated and hearted: two rows, two albums, no conflict.
    _client, me = lib
    _configure(monkeypatch, curated=CURATED_ALBUM, favorites=FAVORITES_ALBUM)
    captured: list = []
    _album(monkeypatch, captured)
    memo_id = coll_api._memo(f"rm-{secrets.token_hex(3)}", me)
    att = _pic(tmp_path, me, memo_id)
    _curated(att)
    await _heart(att)
    _make_due(att)
    _configure(monkeypatch, curated=curated, favorites=favorites)
    for _ in range(2):
        await worker.run_frame_share_drain()
    assert _curated_state(att) == expect_curated
    assert _job(att)["state"] == expect_favorite  # type: ignore[index]
    assert {int(r.url.path.split("/")[3]) for r in captured} == paths


# --- the skip / re-heart interleavings (Approach B, a / b / c) ---------------------------------


async def _claimed(att_id: int) -> FavoriteFrameJob:
    async with async_session() as db:
        job = await worker.claim_favorite_one(db)
        assert job is not None and job.attachment_id == att_id
        await db.commit()
        return job


async def _process(job_id: int) -> str:
    async with async_session() as db:
        job = await db.get(FavoriteFrameJob, job_id)
        assert job is not None
        outcome = await fs.process_claimed_favorite(db, job)
        await db.commit()
        return outcome


async def test_a_heart_committed_before_the_skip_wins_even_with_the_lease_intact(
    lib, monkeypatch, tmp_path
):
    # (a) The worker read the tag absent; the tag is back before its skip runs, and nothing reset
    # the lease (an enqueue that never ran). Only the NOT EXISTS predicate refuses the skip.
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    att = _pic(tmp_path, me)
    await _heart(att)
    _untag(att)
    _make_due(att)
    job = await _claimed(att)
    real = fs.revalidate_favorite

    async def then_heart(db, j):
        verdict = await real(db, j)
        _tag_only(att)
        return verdict

    monkeypatch.setattr(fs, "revalidate_favorite", then_heart)
    await _process(job.id)
    state = _job(att)
    assert state is not None and state["state"] == "queued", state
    assert captured == []


async def test_a_full_re_heart_before_the_skip_leaves_the_row_queued(lib, monkeypatch, tmp_path):
    # (a) with the real heart: tag and upsert commit together, so the lease is gone as well.
    _client, me = lib
    _configure(monkeypatch)
    _album(monkeypatch, [])
    att = _pic(tmp_path, me)
    await _heart(att)
    _untag(att)
    _make_due(att)
    job = await _claimed(att)
    real = fs.revalidate_favorite

    async def then_heart(db, j):
        verdict = await real(db, j)
        await _heart(att)
        return verdict

    monkeypatch.setattr(fs, "revalidate_favorite", then_heart)
    assert await _process(job.id) == "lease_lost"
    state = _job(att)
    assert state is not None and state["state"] == "queued" and state["claimed_by"] is None


async def test_a_re_heart_after_the_claim_never_uploads_under_the_lost_lease(
    lib, monkeypatch, tmp_path
):
    # A re-heart committed between the claim and the send resets the row to queued with a fresh
    # not-before. The tag is present, so revalidation says "send" — only a lease read before the
    # upload stops this worker from sending under a lease it no longer holds.
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    att = _pic(tmp_path, me)
    await _heart(att)
    _make_due(att)
    async with async_session() as db:  # one session, as the worker: the lease stays in memory
        job = await worker.claim_favorite_one(db)
        assert job is not None and job.attachment_id == att
        await db.commit()
        _untag(att)
        await _heart(att)
        assert await fs.process_claimed_favorite(db, job) == "lease_lost"
        await db.commit()
    assert captured == []
    state = _job(att)
    assert state is not None and (state["state"], state["claimed_by"]) == ("queued", None)
    assert float(state["due_in"]) > 50, state


async def test_a_re_heart_while_uploading_converges_to_sent(lib, monkeypatch, tmp_path):
    # (b) / R2-5: the re-heart lands while the upload is in flight. It revokes the lease, so the
    # worker's `sent` is refused and the row waits a new window; the next pass sends it again
    # (aura answers a duplicate) and the row ends `sent` — never `skipped`, never stuck `claimed`.
    _client, me = lib
    _configure(monkeypatch)
    calls: list = []

    async def re_heart():
        if len(calls) == 1:
            _untag(att)
            await _heart(att)

    _album(monkeypatch, calls, photo_id=55, during=re_heart)
    att = _pic(tmp_path, me)
    await _heart(att)
    _make_due(att)
    assert await _once() == "processed"
    mid = _job(att)
    assert mid is not None and mid["state"] == "queued" and float(mid["due_in"]) > 50
    _make_due(att)
    assert await _once() == "processed"
    end = _job(att)
    assert end is not None and (end["state"], end["aura_album_photo_id"]) == ("sent", 55)
    assert len(calls) == 2


async def test_a_skip_before_the_uncommitted_heart_is_resurrected_by_it(lib, monkeypatch, tmp_path):
    # (c, skip first) The heart's tag insert is uncommitted when the worker skips; the heart's
    # upsert then meets a `skipped` row and requeues it.
    _client, me = lib
    _configure(monkeypatch)
    _album(monkeypatch, [])
    att = _pic(tmp_path, me)
    await _heart(att)
    _untag(att)
    _make_due(att)
    job = await _claimed(att)
    async with async_session() as heart:
        a = await heart.get(Attachment, att)
        u = await heart.get(User, a.owner_id)  # type: ignore[union-attr]
        tid = (await heart.execute(text("SELECT id FROM tags WHERE name='favorites'"))).scalar_one()
        await heart.execute(
            text("INSERT INTO attachment_tags (attachment_id, tag_id) VALUES (:a,:t)"),
            {"a": att, "t": tid},
        )
        assert await _process(job.id) == "skipped"
        assert await fs.enqueue_favorite(heart, a, u)  # type: ignore[arg-type]
        await heart.commit()
    state = _job(att)
    assert state is not None and state["state"] == "queued"


async def test_a_skip_behind_the_uncommitted_heart_waits_and_then_matches_nothing(
    lib, monkeypatch, tmp_path
):
    # (c, heart first) The heart holds the row lock with its upsert; the skip waits on it, then
    # re-evaluates against the committed row (lease cleared) and matches nothing.
    _client, me = lib
    _configure(monkeypatch)
    _album(monkeypatch, [])
    att = _pic(tmp_path, me)
    await _heart(att)
    _untag(att)
    _make_due(att)
    job = await _claimed(att)
    async with async_session() as heart:
        a = await heart.get(Attachment, att)
        u = await heart.get(User, a.owner_id)  # type: ignore[union-attr]
        tid = (await heart.execute(text("SELECT id FROM tags WHERE name='favorites'"))).scalar_one()
        await heart.execute(
            text("INSERT INTO attachment_tags (attachment_id, tag_id) VALUES (:a,:t)"),
            {"a": att, "t": tid},
        )
        assert await fs.enqueue_favorite(heart, a, u)  # type: ignore[arg-type]
        worker_task = asyncio.create_task(_process(job.id))
        await asyncio.sleep(0.5)
        assert not worker_task.done()  # blocked on the heart's row lock
        await heart.commit()
    assert await worker_task == "lease_lost"
    state = _job(att)
    assert state is not None and state["state"] == "queued"


# --- one picture, two albums: the same bytes on both paths --------------------------------------


async def test_a_curated_and_favorited_picture_uploads_identical_bytes_to_both_albums(
    lib, monkeypatch, tmp_path
):
    # aura-frames dedupes per frame on the sha256 of ITS re-render, so a frame following both
    # albums shows the picture once only if memos sends the same bytes on both paths: the same
    # render and the same DateTimeOriginal stamp (the memo's date). The source has no EXIF date and
    # its capture instant differs from the memo date, so a path that stamped anything else differs.
    import hashlib

    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    memo_id = coll_api._memo(f"im-{secrets.token_hex(3)}", me)
    att = _pic(tmp_path, me, memo_id)
    _sql(
        "UPDATE attachments SET capture_instant='2011-02-03T04:05:06Z', "
        "created_at='2012-03-04T05:06:07Z' WHERE id=:a",
        {"a": att},
    )
    _curated(att)
    await _heart(att)
    _make_due(att)
    await worker.run_frame_share_drain()
    by_album = {
        int(r.url.path.split("/")[3]): hashlib.sha256(r.content).hexdigest() for r in captured
    }
    assert set(by_album) == {CURATED_ALBUM, FAVORITES_ALBUM}
    assert by_album[CURATED_ALBUM] == by_album[FAVORITES_ALBUM]


async def test_both_paths_send_the_attachment_source_ref(lib, monkeypatch, tmp_path):
    # Phase D1: aura-frames dedupes a frame's photos on this, so curated and favorites must name
    # the same source for the same picture.
    _client, me = lib
    _configure(monkeypatch)
    captured: list = []
    _album(monkeypatch, captured)
    memo_id = coll_api._memo(f"sr-{secrets.token_hex(3)}", me)
    att = _pic(tmp_path, me, memo_id)
    _curated(att)
    await _heart(att)
    _make_due(att)
    await worker.run_frame_share_drain()
    refs = {int(r.url.path.split("/")[3]): r.headers.get("X-Source-Ref") for r in captured}
    assert refs == {
        CURATED_ALBUM: f"memos-attachment:{att}",
        FAVORITES_ALBUM: f"memos-attachment:{att}",
    }


# --- stale-claim recovery and concurrent claims on the favorites queue --------------------------


async def test_stale_favorite_claims_recover_only_on_the_favorites_queue(
    lib, monkeypatch, tmp_path
):
    _client, me = lib
    _configure(monkeypatch)
    stale, fresh = _pic(tmp_path, me), _pic(tmp_path, me)
    for att in (stale, fresh):
        await _heart(att)
        _make_due(att)
        await _claimed(att)
    _sql(
        "UPDATE favorite_frame_jobs SET claimed_at = now() - interval '20 minutes' "
        "WHERE attachment_id=:a",
        {"a": stale},
    )
    async with async_session() as db:  # the curated default must not reach the favorites table
        assert await fs.recover_stale_claimed(db) == 0
        await db.commit()
    assert _job(stale)["state"] == "claimed"  # type: ignore[index]
    async with async_session() as db:
        assert await fs.recover_stale_claimed(db, model=FavoriteFrameJob) == 1
        await db.commit()
    got = _job(stale)
    assert got is not None
    assert (got["state"], got["claimed_by"], got["attempts"]) == ("queued", None, 1), got
    assert _job(fresh)["state"] == "claimed"  # type: ignore[index]


@pytest.mark.parametrize("due", [1, 2])
async def test_two_concurrent_favorite_claims_never_share_a_row(lib, monkeypatch, tmp_path, due):
    # Both claims are open at once (neither committed): SKIP LOCKED hands the second worker the
    # other due row, or nothing when only one is due — never the row the first one holds.
    _client, me = lib
    _configure(monkeypatch)
    atts = [_pic(tmp_path, me) for _ in range(due)]
    for att in atts:
        await _heart(att)
        _make_due(att)
    async with async_session() as first, async_session() as second:
        # Both sessions live in this one coroutine, so a claim that waits on the first one's row
        # lock would wait forever: a lock_timeout turns that regression into a failure, not a hang.
        await second.execute(text("SET LOCAL lock_timeout = '2s'"))
        a = await worker.claim_favorite_one(first)
        b = await worker.claim_favorite_one(second)
        assert a is not None
        if due == 1:
            assert b is None
        else:
            assert b is not None and b.id != a.id
        await first.commit()
        await second.commit()
    states = sorted(_job(att)["state"] for att in atts)  # type: ignore[index]
    assert states == ["claimed"] * due
