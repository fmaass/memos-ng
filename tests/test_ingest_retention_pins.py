"""Retention on the ingest host (MEMNG-997), pinned as built in P2 (`service_api.purge`): an acked
batch goes once it has been acked for MORE than `INGEST_RETENTION_DAYS` (7 by default), never an
unacked or a quarantined one, and a file's bytes go only when no remaining batch names its hash.

These pin the existing behaviour; they change none of it. Every instant is passed to `purge`
explicitly, so the boundary is exact rather than a race with the clock."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.config import Settings, settings
from app.ingest import service_api

from tests.ingest_support import (
    HEIC,
    JPEG,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
)
from tests.test_attachments_taken import _sql
from tests.test_ingest_service import _batch

NOW = datetime(2031, 6, 1, 12, 0, tzinfo=UTC)


def _set(batch: str, *, acked_at: datetime | None = None, state: str | None = None) -> None:
    if acked_at is not None:
        _sql("UPDATE ingest_batches SET acked_at = :a WHERE uid = :u", {"a": acked_at, "u": batch})
    if state is not None:
        _sql("UPDATE ingest_batches SET state = :s WHERE uid = :u", {"s": state, "u": batch})


def _uids() -> set[str]:
    return {r[0] for r in _sql("SELECT uid FROM ingest_batches")}


def _blob(root, data: bytes):
    sha = hashlib.sha256(data).hexdigest()
    return root / "blobs" / sha[:2] / sha


def test_the_retention_window_defaults_to_seven_days():
    assert Settings.model_fields["INGEST_RETENTION_DAYS"].default == 7


async def test_the_seven_day_boundary_is_strict(door, home_iface, ingest_env):  # noqa: F811
    assert settings.INGEST_RETENTION_DAYS == 7
    at_edge = await _batch(door, home_iface, JPEG)
    past_edge = await _batch(door, home_iface, HEIC, "h.heic")
    cutoff = NOW - timedelta(days=7)
    _set(at_edge, acked_at=cutoff, state="acked")  # exactly 7 days: kept
    _set(past_edge, acked_at=cutoff - timedelta(microseconds=1), state="acked")
    result = await service_api.purge(now=NOW)
    assert result["batches"] == 1
    assert _uids() == {at_edge}
    assert not _blob(ingest_env, HEIC).exists()
    assert _blob(ingest_env, JPEG).read_bytes() == JPEG


async def test_unacked_and_quarantined_batches_survive_any_age(door, home_iface, ingest_env):  # noqa: F811
    unacked = await _batch(door, home_iface, JPEG)
    quarantined = await _batch(door, home_iface, HEIC, "h.heic")
    ancient = NOW - timedelta(days=400)
    _sql("UPDATE ingest_batches SET completed_at = :a, created_at = :a", {"a": ancient})
    # A quarantine after an ack keeps the old acked_at: the state alone decides.
    _set(quarantined, acked_at=ancient, state="quarantined")
    result = await service_api.purge(now=NOW)
    assert result["batches"] == 0 and result["files"] == 0
    assert _uids() == {unacked, quarantined}
    assert _blob(ingest_env, JPEG).read_bytes() == JPEG
    assert _blob(ingest_env, HEIC).read_bytes() == HEIC


async def test_shared_bytes_go_only_with_the_last_batch_naming_them(door, home_iface, ingest_env):  # noqa: F811
    first = await _batch(door, home_iface, JPEG)
    second = await _batch(door, home_iface, JPEG)  # the same bytes from another pass's guest
    blob = _blob(ingest_env, JPEG)
    _set(first, acked_at=NOW - timedelta(days=8), state="acked")
    _set(second, acked_at=NOW - timedelta(days=6), state="acked")
    assert await service_api.purge(now=NOW) == {
        "batches": 1,
        "sessions": 0,
        "files": 0,
        "strays": 0,
    }
    assert blob.read_bytes() == JPEG  # `second` still names the hash
    assert await service_api.purge(now=NOW + timedelta(days=2)) == {
        "batches": 1,
        "sessions": 0,
        "files": 1,
        "strays": 0,
    }
    assert not blob.exists()
    assert _uids() == set()
