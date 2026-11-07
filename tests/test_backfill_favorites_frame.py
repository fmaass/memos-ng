"""The one-off favorites backfill and roll-forward replay (plan
PLAN_2026-11-06_favorites-to-aura-frame.md, B3; check R2-2).

One idempotent upsert over the pictures that carry the `favorites` tag NOW: a missing row is
inserted due at once, a `skipped`/`failed` row is re-armed to `queued`, and `queued`, `claimed` and
`sent` rows are never touched. The script never uploads; the worker's pace spaces the sends.
"""

import secrets

import pytest
from app.scripts import backfill_favorites_frame as bf
from app.services import frame_share as fs

from tests import test_collections_api as coll_api
from tests.test_attachments_taken import _sql
from tests.test_favorite_frame import _configure, _pic, _tag_only

lib = coll_api.lib


def _rows() -> dict[int, tuple]:
    return {
        r[0]: tuple(r[1:])
        for r in _sql(
            "SELECT attachment_id, state::text, attempts, error, claimed_by, next_attempt_at, "
            "user_id FROM favorite_frame_jobs"
        )
    }


def _seed_job(att_id: int, state: str) -> None:
    _sql(
        "INSERT INTO favorite_frame_jobs (attachment_id, state, attempts, error, claimed_by, "
        "claimed_at, next_attempt_at) VALUES (:a, CAST(:s AS frame_share_state), 3, 'old', "
        "CASE WHEN :s='claimed' THEN 'w' END, CASE WHEN :s='claimed' THEN now() END, "
        "now() + interval '1 hour')",
        {"a": att_id, "s": state},
    )


def _owner_id(att_id: int) -> int:
    return _sql("SELECT owner_id FROM attachments WHERE id=:a", {"a": att_id})[0][0]


@pytest.fixture
def library(lib, monkeypatch, tmp_path):
    _client, me = lib
    _configure(monkeypatch)
    memo_id = coll_api._memo(f"bm-{secrets.token_hex(3)}", me)
    fresh = [_pic(tmp_path, me, memo_id), _pic(tmp_path, me, None)]  # one memo-less
    missing = _pic(tmp_path, me, memo_id)
    _sql("UPDATE attachments SET storage_path='/nowhere/gone.jpg' WHERE id=:a", {"a": missing})
    video = _pic(tmp_path, me, memo_id, mime="video/mp4")
    for att in [*fresh, missing, video]:
        _tag_only(att)
    return fresh, missing, video, me


async def test_a_dry_run_counts_and_writes_nothing(library):
    fresh, missing, video, _me = library
    stats = await bf.run_backfill(execute=False)
    assert _rows() == {}
    assert (stats.favorites, stats.eligible, stats.no_memo) == (4, 2, 1)
    assert (stats.missing_original, stats.not_frame_image) == (1, 1)
    assert (stats.inserted, stats.reset) == (0, 0)
    assert stats.would_insert == 2 and stats.would_reset == 0
    assert "DRY RUN" in stats.format(execute=False)


async def test_execute_writes_one_job_per_eligible_favorite_and_a_rerun_writes_none(library):
    fresh, missing, video, _me = library
    stats = await bf.run_backfill(execute=True)
    rows = _rows()
    assert set(rows) == set(fresh)  # no row for the missing original or the video
    for att in fresh:
        state, attempts, _err, _by, not_before, user_id = rows[att]
        assert (state, attempts, not_before, user_id) == ("queued", 0, None, _owner_id(att))
    assert (stats.inserted, stats.reset) == (2, 0)
    again = await bf.run_backfill(execute=True)
    assert (again.inserted, again.reset) == (0, 0)
    assert _rows() == rows


async def test_replay_touches_only_skipped_and_failed_rows_of_current_favorites(
    lib, monkeypatch, tmp_path
):
    _client, me = lib
    _configure(monkeypatch)
    by_state = {s: _pic(tmp_path, me) for s in ("queued", "claimed", "sent", "skipped", "failed")}
    gone = _pic(tmp_path, me)  # a skipped row whose picture is no longer favorited
    for state, att in by_state.items():
        _tag_only(att)
        _seed_job(att, state)
    _seed_job(gone, "skipped")
    before = _rows()

    stats = await bf.run_backfill(execute=True)
    after = _rows()
    for state in ("queued", "claimed", "sent"):
        assert after[by_state[state]] == before[by_state[state]], state  # untouched
    assert after[gone] == before[gone]
    for state in ("skipped", "failed"):
        new_state, attempts, err, by, not_before, _uid = after[by_state[state]]
        assert (new_state, attempts, err, by) == ("queued", 0, None, None)
        age = _sql("SELECT EXTRACT(EPOCH FROM now() - :t)", {"t": not_before})[0][0]
        assert 0 <= float(age) < 5  # due now by the DB clock, not the seeded hour ahead
    assert (stats.inserted, stats.reset) == (0, 2)
    rerun = await bf.run_backfill(execute=True)
    assert (rerun.inserted, rerun.reset) == (0, 0)


async def test_the_dry_run_reports_existing_rows_by_state(lib, monkeypatch, tmp_path):
    _client, me = lib
    _configure(monkeypatch)
    for state in ("sent", "skipped"):
        att = _pic(tmp_path, me)
        _tag_only(att)
        _seed_job(att, state)
    stats = await bf.run_backfill(execute=False)
    assert stats.existing_by_state == {"sent": 1, "skipped": 1}
    assert (stats.would_insert, stats.would_reset) == (0, 1)


async def test_the_script_never_uploads(library, monkeypatch):
    async def no_upload(*_a, **_kw):
        raise AssertionError("the backfill must never upload")

    monkeypatch.setattr(fs, "upload_to_album", no_upload)
    await bf.run_backfill(execute=True)


async def test_execute_refuses_without_the_favorites_album(library, monkeypatch):
    _configure(monkeypatch, favorites=0)
    with pytest.raises(SystemExit):
        await bf._amain(execute=True)
    assert _rows() == {}
