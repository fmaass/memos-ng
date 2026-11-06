"""Selection: scope, start date, the delivery registry, and the readiness grace (MEMNG-234).

The decision itself is a pure function over "which of these paths exist", so the grace rules are
tested without a filesystem and the query rules are tested against the real database. Both halves
matter: the query is what makes "already mailed" mean the same thing as "has a
`digest_deliveries` row", and the decision is what makes the satellite's `break` — the rule that
keeps the digest in order — something a reader can check.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.config import settings as app_settings
from app.models import DigestVisibilityScope, Tag, Visibility, memo_tags
from app.services.digest import select as digest_select
from app.services.digest.domain import DigestConfig
from sqlalchemy import text

from tests.digest_support import (
    borrowed_session_factory,
    digest_config,
    seed_attachment,
    seed_memo,
    seed_user,
    write_mobile,
    write_web_mp4,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> DigestConfig:
    """The settings SNAPSHOT the selection reads — a frozen value, not a mapped row, because that
    is what the pipeline is handed once its session has closed."""
    return digest_config(**overrides)


def _candidate(uid: str, *, minutes_old: int = 120, images=(), videos=()):
    return digest_select.Candidate(
        memo_id=abs(hash(uid)) % 100000,
        uid=uid,
        content="",
        created_at=NOW - timedelta(minutes=minutes_old),
        images=[
            digest_select.MediaRef(
                attachment_id=i, uid=f"{uid}-img{i}", filename="p.jpg", mime="image/jpeg"
            )
            for i in images
        ],
        videos=[
            digest_select.MediaRef(
                attachment_id=i, uid=f"{uid}-vid{i}", filename="c.mp4", mime="video/mp4"
            )
            for i in videos
        ],
    )


# --- the readiness decision (pure) ---------------------------------------------------------------


def test_a_memo_whose_media_is_all_present_is_included(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    candidate = _candidate("a", images=(1,))
    existing = set(digest_select.readiness_paths(candidate))
    selection = digest_select.decide_selection([candidate], existing, NOW, 30)
    assert [c.uid for c in selection.included] == ["a"]
    assert selection.deferred == []


def test_a_memo_with_no_media_at_all_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    selection = digest_select.decide_selection([_candidate("a")], set(), NOW, 30)
    assert [c.uid for c in selection.included] == ["a"]


def test_a_memo_inside_its_grace_is_deferred_and_STOPS_the_scan(tmp_path, monkeypatch):
    """The satellite's `break`. Skipping past a not-ready memo would mail a LATER one first and
    then mail this one tomorrow — the digest would deliver the day out of order, permanently."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    not_ready = _candidate("older", minutes_old=10, images=(1,))
    ready = _candidate("newer", minutes_old=5)
    selection = digest_select.decide_selection([not_ready, ready], set(), NOW, 30)
    assert selection.included == []
    assert [d.uid for d in selection.deferred] == ["older", "newer"]
    assert all(d.reason == digest_select.DEFERRED_MEDIA_NOT_READY for d in selection.deferred)


def test_a_memo_past_its_grace_goes_out_flagged_incomplete(tmp_path, monkeypatch):
    """A thumb that never arrives must not hold the digest open forever: past the grace the memo
    is mailed with the note, and its delivery row keeps it out of every later selection."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    stale = _candidate("older", minutes_old=90, images=(1,))
    ready = _candidate("newer", minutes_old=5)
    selection = digest_select.decide_selection([stale, ready], set(), NOW, 30)
    assert [c.uid for c in selection.included] == ["older", "newer"]
    assert selection.included[0].incomplete is True
    assert selection.included[1].incomplete is False
    assert selection.deferred == []


def test_a_video_without_its_transcoded_mp4_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    candidate = _candidate("a", minutes_old=5, videos=(1,))
    selection = digest_select.decide_selection([candidate], set(), NOW, 30)
    assert selection.included == []
    assert [d.uid for d in selection.deferred] == ["a"]


def test_readiness_does_not_wait_for_a_poster(tmp_path, monkeypatch):
    """A video with an mp4 but no poster still mails — it renders as a play button. Holding the
    whole scan for a file the template can do without would defer the day for nothing."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    candidate = _candidate("a", minutes_old=5, videos=(1,))
    paths = digest_select.readiness_paths(candidate)
    assert [p.name for p in paths] == ["a-vid1.mp4"]
    assert not any(p.name.endswith(".poster.jpg") for p in paths)
    selection = digest_select.decide_selection([candidate], set(paths), NOW, 30)
    assert [c.uid for c in selection.included] == ["a"]


def test_the_readiness_paths_come_from_the_repos_own_tier_map(tmp_path, monkeypatch):
    """Not a second copy of the layout: a rung added to `derived_tier_path` moves both."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    candidate = _candidate("a", images=(1,), videos=(2,))
    paths = digest_select.readiness_paths(candidate)
    # The MOBILE tier, because that is the file the mail is encoded from (MEMNG-240): readiness
    # that gated on the thumb would let a memo be selected and then lose its photo silently.
    assert Path(tmp_path / "assets" / "mobile" / "a-img1.jpg") in paths
    assert Path(tmp_path / "assets" / "thumbs" / "a-img1.jpg") not in paths
    assert Path(tmp_path / "assets" / "web" / "a-vid2.mp4") in paths


# --- the query (real database) -------------------------------------------------------------------


async def test_scope_public_protected_admits_both_and_never_private(adb):
    user = await seed_user(adb)
    public = await seed_memo(adb, user, visibility=Visibility.PUBLIC)
    protected = await seed_memo(adb, user, visibility=Visibility.PROTECTED)
    await seed_memo(adb, user, visibility=Visibility.PRIVATE)
    found = await digest_select.load_candidates(adb, _settings(), language="de")
    assert {c.memo_id for c in found} == {public.id, protected.id}


async def test_scope_public_admits_only_public(adb):
    user = await seed_user(adb)
    public = await seed_memo(adb, user, visibility=Visibility.PUBLIC)
    await seed_memo(adb, user, visibility=Visibility.PROTECTED)
    found = await digest_select.load_candidates(
        adb, _settings(visibility_scope=DigestVisibilityScope.PUBLIC), language="de"
    )
    assert {c.memo_id for c in found} == {public.id}


async def test_an_archived_memo_is_never_a_candidate(adb):
    user = await seed_user(adb)
    live = await seed_memo(adb, user)
    await seed_memo(adb, user, archived=True)
    found = await digest_select.load_candidates(adb, _settings(), language="de")
    assert {c.memo_id for c in found} == {live.id}


async def test_the_start_date_is_a_LOCAL_day_boundary(adb):
    """`start_date` is a date the admin typed in the panel; comparing it as a UTC midnight would
    admit (or exclude) the two hours either side of it in Europe/Zurich."""
    user = await seed_user(adb)
    just_before = await seed_memo(adb, user, created_at=datetime(2026, 5, 31, 21, 59, tzinfo=UTC))
    just_after = await seed_memo(adb, user, created_at=datetime(2026, 5, 31, 22, 1, tzinfo=UTC))
    found = {
        c.memo_id for c in await digest_select.load_candidates(adb, _settings(), language="de")
    }
    assert just_after.id in found
    assert just_before.id not in found


async def test_a_memo_with_a_delivery_row_is_excluded(adb):
    """The registry, and the whole "prevents any re-send" claim of the Epic."""
    user = await seed_user(adb)
    delivered = await seed_memo(adb, user)
    fresh = await seed_memo(adb, user)
    run_id = await adb.scalar(
        text(
            "INSERT INTO digest_runs (trigger, local_date) VALUES ('scheduled', "
            "'2026-07-01') RETURNING id"
        )
    )
    await adb.execute(
        text(
            "INSERT INTO digest_deliveries (memo_id, run_id, language) "
            "VALUES (:memo_id, :run_id, 'de')"
        ),
        {"memo_id": delivered.id, "run_id": run_id},
    )
    await adb.flush()
    found = {
        c.memo_id for c in await digest_select.load_candidates(adb, _settings(), language="de")
    }
    assert fresh.id in found
    assert delivered.id not in found


async def test_candidates_come_back_oldest_first(adb):
    user = await seed_user(adb)
    newest = await seed_memo(adb, user, created_at=datetime(2026, 6, 20, tzinfo=UTC))
    oldest = await seed_memo(adb, user, created_at=datetime(2026, 6, 2, tzinfo=UTC))
    middle = await seed_memo(adb, user, created_at=datetime(2026, 6, 10, tzinfo=UTC))
    found = await digest_select.load_candidates(adb, _settings(), language="de")
    assert [c.memo_id for c in found] == [oldest.id, middle.id, newest.id]


async def test_attachments_are_split_into_photos_and_videos(adb):
    user = await seed_user(adb)
    memo = await seed_memo(adb, user)
    photo = await seed_attachment(adb, user, memo, mime="image/jpeg")
    video = await seed_attachment(adb, user, memo, mime="video/mp4", filename="clip.mp4")
    await seed_attachment(adb, user, memo, mime="application/pdf", filename="doc.pdf")
    found = await digest_select.load_candidates(adb, _settings(), language="de")
    candidate = next(c for c in found if c.memo_id == memo.id)
    assert [ref.uid for ref in candidate.images] == [photo.uid]
    assert [ref.uid for ref in candidate.videos] == [video.uid]


async def test_select_for_run_reads_the_real_filesystem(adb, tmp_path, monkeypatch):
    """The one end-to-end pass: the query, the offloaded existence sweep and the decision, with
    real files on disk."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    user = await seed_user(adb)
    ready = await seed_memo(adb, user, created_at=datetime(2026, 6, 5, tzinfo=UTC))
    ready_photo = await seed_attachment(adb, user, ready, mime="image/jpeg")
    write_mobile(tmp_path, ready_photo.uid)
    with_video = await seed_memo(adb, user, created_at=datetime(2026, 6, 6, tzinfo=UTC))
    clip = await seed_attachment(adb, user, with_video, mime="video/mp4", filename="c.mp4")
    write_web_mp4(tmp_path, clip.uid)
    pending = await seed_memo(adb, user, created_at=datetime.now(UTC) - timedelta(minutes=5))
    await seed_attachment(adb, user, pending, mime="image/jpeg")  # no thumb on disk
    await adb.flush()

    selection = await digest_select.select_for_run(
        borrowed_session_factory(adb), _settings(), datetime.now(UTC), language="de"
    )
    assert [c.memo_id for c in selection.included] == [ready.id, with_video.id]
    assert [d.uid for d in selection.deferred] == [pending.uid]


async def test_unresolved_attachment_ids_covers_photos_and_videos(adb):
    user = await seed_user(adb)
    memo = await seed_memo(adb, user)
    photo = await seed_attachment(adb, user, memo, mime="image/jpeg")
    video = await seed_attachment(adb, user, memo, mime="video/mp4", filename="c.mp4")
    found = await digest_select.load_candidates(adb, _settings(), language="de")
    ids = digest_select.unresolved_attachment_ids(
        digest_select.Selection(included=found, deferred=[])
    )
    assert set(ids) >= {photo.id, video.id}


@pytest.mark.parametrize(
    "scope", [DigestVisibilityScope.PUBLIC, DigestVisibilityScope.PUBLIC_PROTECTED]
)
def test_no_scope_admits_a_private_memo(scope):
    """A category-per-input check, not just the happy path: PRIVATE is out in EVERY scope, because
    the digest goes to six mailboxes outside the household."""
    assert Visibility.PRIVATE not in digest_select.SCOPE_VISIBILITIES[scope]


async def test_a_candidate_carries_its_memos_tags_sorted(adb):
    """The chip row and the plain part's tag line both read `Candidate.tags`, so selection is
    where they have to arrive — and sorted, because the mail must not reorder a memo's tags
    between two runs. `Memo.tags` is `lazy="selectin"`, so this costs the loader no extra query.
    """
    user = await seed_user(adb)
    memo = await seed_memo(adb, user, content="#trip\nphotos")
    untagged = await seed_memo(adb, user, content="no tags here")
    trip, curated = Tag(name="trip"), Tag(name="curated")
    adb.add_all([trip, curated])
    await adb.flush()
    # The association rows written directly, in the order the sort has to undo — appending to
    # `memo.tags` would lazy-load the collection on an object this session has not refreshed.
    await adb.execute(
        memo_tags.insert(),
        [{"memo_id": memo.id, "tag_id": trip.id}, {"memo_id": memo.id, "tag_id": curated.id}],
    )
    await adb.flush()
    adb.expire(memo)
    found = {
        c.memo_id: c for c in await digest_select.load_candidates(adb, _settings(), language="de")
    }
    assert found[memo.id].tags == ("curated", "trip")
    assert found[untagged.id].tags == ()
