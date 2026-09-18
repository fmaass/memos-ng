import asyncio
import secrets

import pytest
from app import worker
from app.config import Settings, settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobKind, JobLane, JobState, MediaJob
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from tests.conftest import SYNC_URL

BOTH_LANES = [JobLane.fast, JobLane.slow]


def test_parse_worker_lanes_single():
    assert worker.parse_worker_lanes("slow") == [JobLane.slow]
    assert worker.parse_worker_lanes("fast") == [JobLane.fast]


def test_parse_worker_lanes_both_and_default():
    assert worker.parse_worker_lanes("fast,slow") == [JobLane.fast, JobLane.slow]
    assert worker.parse_worker_lanes("") == [JobLane.fast, JobLane.slow]  # unset → both
    assert worker.parse_worker_lanes("  FAST , slow ") == [JobLane.fast, JobLane.slow]  # tolerant
    assert worker.parse_worker_lanes("slow,slow") == [JobLane.slow]  # de-duplicated


def test_parse_worker_lanes_rejects_unknown():
    with pytest.raises(ValueError, match="Invalid WORKER_LANES"):
        worker.parse_worker_lanes("turbo")


async def _seed(adb, lane: JobLane) -> MediaJob:
    att = Attachment(
        uid=secrets.token_urlsafe(8),
        filename="x",
        mime="video/mp4" if lane is JobLane.slow else "image/jpeg",
        size=1,
        status=AttachmentStatus.processing,
        storage_path="/tmp/x",
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=lane, state=JobState.queued)
    adb.add(job)
    await adb.flush()
    return job


async def test_claim_one_only_claims_its_lane(adb):
    fast_job = await _seed(adb, JobLane.fast)
    slow_job = await _seed(adb, JobLane.slow)

    # A slow-only worker must claim the slow job and leave the fast one untouched.
    claimed = await worker.claim_one(adb, lanes=[JobLane.slow])
    assert claimed is not None and claimed.id == slow_job.id
    assert claimed.state is JobState.processing
    await adb.refresh(fast_job)
    assert fast_job.state is JobState.queued  # the other lane is not stolen


async def test_claim_one_skips_when_no_job_in_lane(adb):
    await _seed(adb, JobLane.fast)  # only a fast job exists
    claimed = await worker.claim_one(adb, lanes=[JobLane.slow])
    assert claimed is None  # slow worker finds nothing → idle, doesn't grab the fast job


# ---------------------------------------------------------------------------
# Claim CLASSES (transcode / light) — a claim-time predicate, not a stored column.
# ---------------------------------------------------------------------------


async def _seed_job(
    adb,
    *,
    lane: JobLane,
    kind: str = JobKind.derive,
    mime: str = "image/jpeg",
    orphan: bool = False,
    state: JobState = JobState.queued,
    attachment_id: int | None = None,
) -> MediaJob:
    """Seed ONE queue row, optionally with no attachment row behind it.

    `orphan=True` reproduces the row shape a deleted attachment leaves behind: media_jobs carries
    no foreign key, so such a row really can exist and really is claimed (and then discarded) by
    the worker.
    """
    if attachment_id is None:
        if orphan:
            attachment_id = 2_000_000_000 + secrets.randbelow(100_000_000)
        else:
            att = Attachment(
                uid=secrets.token_urlsafe(8),
                filename="x",
                mime=mime,
                size=1,
                status=AttachmentStatus.processing,
                storage_path="/tmp/x",
            )
            adb.add(att)
            await adb.flush()
            attachment_id = att.id
    job = MediaJob(attachment_id=attachment_id, lane=lane, kind=kind, state=state)
    adb.add(job)
    await adb.flush()
    return job


# (label, lane, kind, attachment mime, orphan, EXPECTED CLASS AS A LITERAL).
# The expected class is written out per row on purpose: asserting only that the two classes are
# disjoint and exhaustive would also pass for a wrong predicate paired with its exact negation.
CLASS_TABLE = [
    ("fast/derive/jpeg", JobLane.fast, JobKind.derive, "image/jpeg", False, "light"),
    # The poster job a video gets on the fast lane — light work about a video.
    ("fast/derive/video", JobLane.fast, JobKind.derive, "video/mp4", False, "light"),
    ("slow/derive/heic", JobLane.slow, JobKind.derive, "image/heic", False, "light"),
    ("slow/derive/avif", JobLane.slow, JobKind.derive, "image/avif", False, "light"),
    ("slow/derive/mp4", JobLane.slow, JobKind.derive, "video/mp4", False, "transcode"),
    ("slow/derive/mov", JobLane.slow, JobKind.derive, "video/quicktime", False, "transcode"),
    ("slow/printready/jpeg", JobLane.slow, JobKind.printready, "image/jpeg", False, "light"),
    ("fast/printready/jpeg", JobLane.fast, JobKind.printready, "image/jpeg", False, "light"),
    # Neither image nor video: the mime term is a positive `video/%` test, so anything else is
    # light by the same rule that makes a HEIC light — stated as its own category, not inferred.
    ("slow/derive/pdf", JobLane.slow, JobKind.derive, "application/pdf", False, "light"),
    ("slow/derive/audio", JobLane.slow, JobKind.derive, "audio/mpeg", False, "light"),
    ("orphan slow/derive", JobLane.slow, JobKind.derive, None, True, "light"),
]

# Every kind that is NOT `derive`, on the slow lane, with a video attachment. Generated from the
# enum so a kind added later arrives here with no test edit, and the expected value stays the
# LITERAL `light`: whatever else a job derives, it is not the H.264 transcode. These rows are NOT a
# shape any path in app/ produces — enqueue_printready is image-only and process_printready_job
# raises on a non-image, saying in so many words that reaching it means a hand-made row. They are
# seeded solely to make the `kind = derive` conjunct falsifiable: with that conjunct removed they
# flip to `transcode`.
CLASS_TABLE += [
    (f"slow/{kind}/video", JobLane.slow, kind, "video/mp4", False, "light")
    for kind in JobKind
    if kind is not JobKind.derive
]
CLASS_TABLE_IDS = [row[0] for row in CLASS_TABLE]


@pytest.mark.parametrize("label,lane,kind,mime,orphan,expected", CLASS_TABLE, ids=CLASS_TABLE_IDS)
async def test_claim_classes_route_each_job_shape_to_its_named_class(
    adb, label, lane, kind, mime, orphan, expected
):
    job = await _seed_job(adb, lane=lane, kind=kind, mime=mime or "image/jpeg", orphan=orphan)
    other = "light" if expected == "transcode" else "transcode"

    # The class this row does NOT belong to must not see it at all...
    assert await worker.claim_one(adb, lanes=BOTH_LANES, job_class=other) is None
    # ...and the class it DOES belong to claims exactly it.
    claimed = await worker.claim_one(adb, lanes=BOTH_LANES, job_class=expected)
    assert claimed is not None and claimed.id == job.id
    assert claimed.state is JobState.processing


@pytest.mark.parametrize(
    "lanes",
    [BOTH_LANES, [JobLane.fast], [JobLane.slow]],
    ids=["lanes=fast,slow", "lanes=fast", "lanes=slow"],
)
async def test_the_two_classes_partition_the_whole_queue_in_every_lane_setting(adb, lanes):
    """Disjoint AND exhaustive, on top of the per-row literals — and lane-scoped.

    A job outside this worker's lanes must be claimed by NEITHER class; the classes must never
    both see one row, and together they must leave nothing claimable behind.
    """
    seeded = {}
    for _label, lane, kind, mime, orphan, expected in CLASS_TABLE:
        job = await _seed_job(adb, lane=lane, kind=kind, mime=mime or "image/jpeg", orphan=orphan)
        seeded[job.id] = (lane, expected)

    drained: dict[int, str] = {}
    for job_class in ("transcode", "light"):
        while (job := await worker.claim_one(adb, lanes=lanes, job_class=job_class)) is not None:
            assert job.id not in drained  # no overlap: the other class already took it
            drained[job.id] = job_class

    in_lane = {jid for jid, (lane, _) in seeded.items() if lane in lanes}
    assert set(drained) == in_lane  # exhaustive within the lanes, and nothing outside them
    for jid, (_lane, expected) in seeded.items():
        if jid in in_lane:
            assert drained[jid] == expected


async def test_the_transcode_claim_uses_a_correlated_exists_and_locks_only_media_jobs(adb):
    """The class predicate must stay a subquery in WHERE, never a join.

    Postgres applies a locking clause only to rows from the outer query's FROM list, so an EXISTS
    in WHERE takes no lock on `attachments` at all — which is what keeps the job-then-attachment
    lock order (and the lock census) untouched. A join would pull `attachments` into that FROM.

    The statement is captured as it reaches the driver rather than recompiled here, so this pins
    what Postgres was really asked to do and not a second construction of it.
    """
    await _seed_job(adb, lane=JobLane.slow, mime="video/mp4")
    captured: list[tuple[str, object]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        await worker.claim_one(adb, lanes=BOTH_LANES, job_class="transcode")
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    sql, params = next((s, p) for s, p in captured if "FOR UPDATE" in s)
    flat = " ".join(sql.split())

    # Named on the ATTACHMENTS subquery specifically: the order guard contributes an EXISTS of its
    # own over media_jobs, so a bare "EXISTS is present" assertion would pass with the class term
    # gone entirely.
    assert "EXISTS (SELECT 1 FROM attachments" in flat
    assert "attachments.id = media_jobs.attachment_id" in flat
    assert "attachments.mime LIKE" in flat
    values = list(params.values()) if isinstance(params, dict) else list(params)
    assert "video/%" in values  # the prefix match, not an equality on one container

    assert "FOR UPDATE OF media_jobs" in flat
    assert "SKIP LOCKED" in flat
    assert "JOIN" not in flat.upper()


def _seed_committed(mime: str, lane: JobLane, kind: str = JobKind.derive) -> tuple[int, int]:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, filename, mime, size, status, storage_path) "
                "VALUES (:u,'x',:m,1,CAST('processing' AS attachment_status),'/tmp/x') RETURNING id"
            ),
            {"u": secrets.token_urlsafe(8), "m": mime},
        ).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct) "
                "VALUES (:a, CAST(:l AS job_lane), :k, CAST('queued' AS job_state), 0, 0) "
                "RETURNING id"
            ),
            {"a": aid, "l": str(lane), "k": str(kind)},
        ).scalar_one()
    eng.dispose()
    return aid, jid


def _cleanup_committed(aids: list[int], jids: list[int]) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for jid in jids:
            c.execute(text("DELETE FROM media_jobs WHERE id=:j"), {"j": jid})
        for aid in aids:
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


def test_two_class_claimers_never_take_each_others_rows():
    """Two live sessions, one per class, over a seeded mix.

    This is a ROUTING assertion and nothing more: complementary predicates never contend for one
    row, so it proves no `SKIP LOCKED` property. Both sessions stay open and uncommitted, so each
    claim really does hold its row lock while the other claimer runs.
    """
    seeds = [
        ("video/mp4", JobLane.slow, JobKind.derive, "transcode"),
        ("image/heic", JobLane.slow, JobKind.derive, "light"),
        ("video/quicktime", JobLane.slow, JobKind.derive, "transcode"),
        ("image/jpeg", JobLane.fast, JobKind.derive, "light"),
        ("video/mp4", JobLane.fast, JobKind.derive, "light"),
        ("image/jpeg", JobLane.slow, JobKind.printready, "light"),
    ]
    rows = [(_seed_committed(mime, lane, kind), expected) for mime, lane, kind, expected in seeds]
    aids = [aid for (aid, _jid), _ in rows]
    jids = [jid for (_aid, jid), _ in rows]
    expected_by_id = {jid: expected for (_aid, jid), expected in rows}

    async def _go():
        got: dict[str, set[int]] = {"transcode": set(), "light": set()}
        async with async_session() as db_t, async_session() as db_l:
            while True:
                jt = await worker.claim_one(db_t, lanes=BOTH_LANES, job_class="transcode")
                jl = await worker.claim_one(db_l, lanes=BOTH_LANES, job_class="light")
                if jt is None and jl is None:
                    break
                if jt is not None:
                    got["transcode"].add(jt.id)
                if jl is not None:
                    got["light"].add(jl.id)
            await db_t.rollback()
            await db_l.rollback()
        return got

    try:
        got = asyncio.run(_go())
        mine = {jid for jid in jids}
        transcoded = got["transcode"] & mine
        lit = got["light"] & mine
        assert not (transcoded & lit)  # disjoint id sets
        assert transcoded | lit == mine  # between them they drained the seeded mix
        for jid in mine:
            actual = "transcode" if jid in transcoded else "light"
            assert actual == expected_by_id[jid]
    finally:
        _cleanup_committed(aids, jids)


# ---------------------------------------------------------------------------
# Per-attachment order guard — restores, explicitly, the per-attachment FIFO that
# the single serial loop used to give for free.
# ---------------------------------------------------------------------------


async def _seed_importer_pair(
    adb, *, prior_lane: JobLane = JobLane.fast
) -> tuple[MediaJob, MediaJob]:
    """The pair the repository importer writes in one flush for an underived video.

    A fast poster `derive` job and a slow transcode `derive` job for ONE attachment, both queued,
    with no ordering between them — the exact shape that would let the two consumers claim both at
    once and race each other's attachment-status writes.
    """
    att = Attachment(
        uid=secrets.token_urlsafe(8),
        filename="x.mp4",
        mime="video/mp4",
        size=1,
        status=AttachmentStatus.processing,
        storage_path="/tmp/x.mp4",
    )
    adb.add(att)
    await adb.flush()
    prior = await _seed_job(adb, lane=prior_lane, mime="video/mp4", attachment_id=att.id)
    later = await _seed_job(adb, lane=JobLane.slow, mime="video/mp4", attachment_id=att.id)
    assert prior.id < later.id
    return prior, later


@pytest.mark.parametrize("blocking_state", [JobState.queued, JobState.processing])
async def test_the_order_guard_holds_a_successor_back_while_its_predecessor_is_in_flight(
    adb, blocking_state
):
    prior, later = await _seed_importer_pair(adb)
    prior.state = blocking_state
    await adb.flush()

    assert await worker.claim_one(adb, lanes=BOTH_LANES, job_class="transcode") is None


async def test_a_claimed_predecessor_does_not_block_its_successor(adb):
    """`claimed` must stay OUT of the guard's set, or the hold it creates has no bound.

    Nothing in the media queue ever writes `claimed` — the claim writes `processing` — and nothing
    would ever leave it: the claim selects `queued` and the stale sweep selects `processing`. A
    guard that waited on a state no path produces and no path clears would strand every later job
    of that attachment for good.
    """
    prior, later = await _seed_importer_pair(adb)
    prior.state = JobState.claimed
    await adb.flush()

    claimed = await worker.claim_one(adb, lanes=BOTH_LANES, job_class="transcode")
    assert claimed is not None and claimed.id == later.id


@pytest.mark.parametrize("terminal_state", [JobState.done, JobState.failed])
async def test_a_terminal_predecessor_never_blocks_its_successor(adb, terminal_state):
    prior, later = await _seed_importer_pair(adb)
    prior.state = terminal_state
    await adb.flush()

    claimed = await worker.claim_one(adb, lanes=BOTH_LANES, job_class="transcode")
    assert claimed is not None and claimed.id == later.id


async def test_the_order_guard_does_not_reach_across_attachments(adb):
    # A lower-id in-flight job for a DIFFERENT attachment must not hold this one back.
    await _seed_job(adb, lane=JobLane.fast, mime="video/mp4")
    later = await _seed_job(adb, lane=JobLane.slow, mime="video/mp4")

    claimed = await worker.claim_one(adb, lanes=BOTH_LANES, job_class="transcode")
    assert claimed is not None and claimed.id == later.id


async def test_the_order_guard_only_orders_against_lanes_this_worker_drains(adb):
    """The starvation answer, as a test.

    Without the lane filter a lower-id queued job in a lane nobody here drains would block its
    successor forever. This worker orders only against jobs it could itself claim.
    """
    prior, later = await _seed_importer_pair(adb, prior_lane=JobLane.fast)
    assert prior.state is JobState.queued

    claimed = await worker.claim_one(adb, lanes=[JobLane.slow], job_class="transcode")
    assert claimed is not None and claimed.id == later.id


async def test_the_order_guard_applies_with_no_class_given_too(adb):
    # The guard is not a class feature: the unclassed claim (today's call shape) gets it as well.
    prior, later = await _seed_importer_pair(adb)

    first = await worker.claim_one(adb, lanes=BOTH_LANES)
    assert first is not None and first.id == prior.id  # the predecessor, never the successor
    assert await worker.claim_one(adb, lanes=BOTH_LANES) is None  # successor still held back


# ---------------------------------------------------------------------------
# Slot / CPU-budget validator.
# ---------------------------------------------------------------------------


@pytest.fixture
def slot_settings(monkeypatch):
    """Put every value the validator reads on its documented default, then let a case move one."""

    def _set(**over):
        base = {
            "WORKER_TRANSCODE_SLOTS": 1,
            "WORKER_LIGHT_SLOTS": 1,
            "WORKER_TRANSCODE_CPU_COST": 3,
            "WORKER_CPU_BUDGET": 8,
            "FFMPEG_THREADS": 1,
            "WORKER_TRANSCODE_STALL_SECONDS": 3905,
            "WORKER_LIGHT_STALL_SECONDS": 900,
            "MEDIA_CLAIM_STALE_SECONDS": 7200,
        }
        base.update(over)
        for key, value in base.items():
            monkeypatch.setattr(settings, key, value)

    return _set


def test_the_documented_defaults_validate(slot_settings):
    slot_settings()
    worker.validate_worker_slots()  # 1x3 + 1 = 4 <= 8


def test_a_zero_slot_class_is_refused(slot_settings):
    slot_settings(WORKER_TRANSCODE_SLOTS=0)
    with pytest.raises(ValueError, match="starves"):
        worker.validate_worker_slots()

    slot_settings(WORKER_LIGHT_SLOTS=0)
    with pytest.raises(ValueError, match="starves"):
        worker.validate_worker_slots()


def test_more_than_one_slot_per_class_is_refused(slot_settings):
    slot_settings(WORKER_TRANSCODE_SLOTS=2)
    with pytest.raises(ValueError, match="not supported"):
        worker.validate_worker_slots()

    slot_settings(WORKER_LIGHT_SLOTS=2)
    with pytest.raises(ValueError, match="not supported"):
        worker.validate_worker_slots()


def test_consumers_that_do_not_fit_the_cpu_budget_are_refused(slot_settings):
    slot_settings(WORKER_CPU_BUDGET=3)  # 1x3 + 1 = 4 > 3
    with pytest.raises(ValueError, match="WORKER_CPU_BUDGET"):
        worker.validate_worker_slots()


def test_a_per_transcode_cost_below_the_encoder_thread_count_is_refused(slot_settings):
    # Otherwise the budget arithmetic is decorative: the nominal encoder threads alone already
    # exceed the cost the budget was computed from.
    slot_settings(WORKER_TRANSCODE_CPU_COST=1, FFMPEG_THREADS=4)
    with pytest.raises(ValueError, match="FFMPEG_THREADS"):
        worker.validate_worker_slots()


@pytest.mark.parametrize("key", ["WORKER_TRANSCODE_STALL_SECONDS", "WORKER_LIGHT_STALL_SECONDS"])
def test_a_stall_bound_below_the_poll_cadence_is_refused(slot_settings, key):
    slot_settings(**{key: worker.POLL_SECONDS - 1})
    with pytest.raises(ValueError, match="POLL_SECONDS"):
        worker.validate_worker_slots()


@pytest.mark.parametrize("key", ["WORKER_TRANSCODE_STALL_SECONDS", "WORKER_LIGHT_STALL_SECONDS"])
def test_a_stall_bound_at_or_past_the_stale_claim_horizon_is_refused(slot_settings, key):
    # Past the horizon the sweep would requeue a job the supervisor has not yet given up on.
    slot_settings(**{key: 7200})
    with pytest.raises(ValueError, match="MEDIA_CLAIM_STALE_SECONDS"):
        worker.validate_worker_slots()


def test_the_dead_worker_concurrency_setting_is_gone(monkeypatch):
    # It was read nowhere. Removing it cannot break a startup that still carries the stale env key,
    # because the settings model ignores extras.
    assert "WORKER_CONCURRENCY" not in Settings.model_fields
    monkeypatch.setenv("WORKER_CONCURRENCY", "4")
    Settings()  # extra="ignore" — a leftover env key is not a startup failure


def _add_committed_job(aid: int, lane: JobLane, kind: str = JobKind.derive) -> int:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct) "
                "VALUES (:a, CAST(:l AS job_lane), :k, CAST('queued' AS job_state), 0, 0) "
                "RETURNING id"
            ),
            {"a": aid, "l": str(lane), "k": str(kind)},
        ).scalar_one()
    eng.dispose()
    return jid


def test_an_uncommitted_claim_already_holds_the_next_job_of_that_attachment():
    """The "no window" claim, tested across two real sessions rather than asserted in a comment.

    Session A claims the earlier job and does NOT commit, so session B — reading its own snapshot —
    still sees that row as `queued`. `queued` is in the guard's set precisely so that this instant,
    between a claim and its commit, is covered: B must not take the later job of that attachment,
    while an unrelated attachment's job stays available to it.
    """
    aid_x, job1 = _seed_committed("video/mp4", JobLane.fast)  # the poster job — light class
    job2 = _add_committed_job(aid_x, JobLane.slow)  # its transcode job, higher id
    aid_y, job3 = _seed_committed("video/mp4", JobLane.slow)  # an unrelated transcode job

    async def _go():
        async with async_session() as db_a, async_session() as db_b:
            held = None
            for _ in range(50):  # walk past any row another test left behind
                j = await worker.claim_one(db_a, lanes=BOTH_LANES, job_class="light")
                if j is None:
                    break
                if j.id == job1:
                    held = j.id
                    break
            transcode_ids = set()
            for _ in range(50):
                j = await worker.claim_one(db_b, lanes=BOTH_LANES, job_class="transcode")
                if j is None:
                    break
                transcode_ids.add(j.id)
            await db_a.rollback()  # nothing is ever committed: the window stays open throughout
            await db_b.rollback()
            return held, transcode_ids

    try:
        held, transcode_ids = asyncio.run(_go())
        assert held == job1  # session A is holding the earlier job, uncommitted
        assert job2 not in transcode_ids  # the successor is held back across the window
        assert job3 in transcode_ids  # a different attachment is not caught by the guard
    finally:
        _cleanup_committed([aid_x, aid_y], [job1, job2, job3])


@pytest.mark.parametrize("value", [0, -5])
def test_a_nonpositive_transcode_cpu_cost_is_refused(slot_settings, value):
    # A negative cost would be SUBTRACTED from the demand and let any configuration through.
    slot_settings(WORKER_TRANSCODE_CPU_COST=value)
    with pytest.raises(ValueError, match=f"WORKER_TRANSCODE_CPU_COST={value} must be at least 1"):
        worker.validate_worker_slots()


@pytest.mark.parametrize("value", [0, -5])
def test_a_nonpositive_cpu_budget_is_refused(slot_settings, value):
    slot_settings(WORKER_CPU_BUDGET=value)
    with pytest.raises(ValueError, match=f"WORKER_CPU_BUDGET={value} must be at least 1"):
        worker.validate_worker_slots()


@pytest.mark.parametrize("value", [0, -5])
def test_a_nonpositive_ffmpeg_thread_count_is_refused(slot_settings, value):
    # 0 is ffmpeg's "pick it yourself", which leaves the budget with no number to reason from.
    slot_settings(FFMPEG_THREADS=value)
    with pytest.raises(ValueError, match=f"FFMPEG_THREADS={value} must be at least 1"):
        worker.validate_worker_slots()


def test_the_deployed_configuration_validates():
    # The real production shape, read off the deployed environment: both lanes on one worker, one
    # encoder thread, and every setting this change adds left on its in-code default.
    assert worker.parse_worker_lanes(settings.WORKER_LANES) == [JobLane.fast, JobLane.slow]
    assert settings.FFMPEG_THREADS == 1
    worker.validate_worker_slots()
