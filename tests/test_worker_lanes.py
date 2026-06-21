import secrets

import pytest
from app import worker
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob


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
