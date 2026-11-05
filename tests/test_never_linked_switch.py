"""MEMNG-1203 — the never-linked sweep's deleting half is behind a switch, off by default.

MEMNG-968's sweep deletes attachments, and its first run waits on the owner's GO. Until that is
given, `NEVER_LINKED_SWEEP_ENABLED` keeps the hourly pass from reclaiming a single never-linked row,
while everything that only READS stays on: the orphan count the metric exports still counts the row
the pass declined to delete. The gate sits in the hourly wrapper, `sweep_detached_attachments_safe`,
not in `sweep_unlinked_attachments` itself, so a direct call (MEMNG-968's aging tests) keeps its
meaning. The detached sweep shares that wrapper and is not the switch's business: it behaves the
same in both states.

These tests drive the real wrapper against a real row and a real original file; nothing in the
sweep is mocked.
"""

import asyncio
from datetime import UTC, datetime

import pytest
import structlog
from app import worker
from app.config import Settings, settings

from tests.test_unlinked_sweep import H, _cleanup, _exists, _gauge, _seed

SWITCH = "NEVER_LINKED_SWEEP_ENABLED"


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry["event"] == name]


def test_the_switch_is_declared_and_off_by_default():
    """Off is the shipped state: the first deleting run is the owner's call, not a deploy's."""
    field = Settings.model_fields.get(SWITCH)
    assert field is not None, f"{SWITCH} is not a setting"
    assert field.default is False


async def test_off_the_hourly_pass_keeps_a_73h_row_and_still_counts_it(
    db_engine, monkeypatch, tmp_path
):
    """The ticket's first AC, under the process's settings as loaded — the default. A row three
    days past its standalone finalize would be reclaimed with the switch on; off, the hourly pass
    leaves the row and its original alone, the metric still counts it as reclaimable, and the pass
    says on the record that it skipped rather than logging a count of nothing."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        att_id, original = _seed(tmp_path, "held_by_switch", age=73 * H)
        assert await _gauge() == (1, 1)

        with structlog.testing.capture_logs() as logs:
            assert await worker.sweep_detached_attachments_safe() == 0

        assert _exists(att_id)
        assert original.exists()
        assert await _gauge() == (1, 1)
        assert _events(logs, "unlinked_attachments_sweep_disabled") == [
            {"event": "unlinked_attachments_sweep_disabled", "setting": SWITCH, "log_level": "info"}
        ]
        # Off is not a pass that found nothing: no count is logged for a sweep that never ran.
        assert _events(logs, "unlinked_attachments_swept") == []
        # The detached half still ran and still reports its own count.
        assert _events(logs, "detached_attachments_swept") == [
            {"event": "detached_attachments_swept", "count": 0, "log_level": "info"}
        ]
    finally:
        _cleanup()


async def test_on_the_hourly_pass_reclaims_the_73h_row(db_engine, monkeypatch, tmp_path):
    """The same row with the switch on is MEMNG-968's ordinary case: reclaimed, original and all,
    the metric drops to zero, and the pass logs its count instead of the skip."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, SWITCH, True)
    try:
        att_id, original = _seed(tmp_path, "released_by_switch", age=73 * H)
        assert await _gauge() == (1, 1)

        with structlog.testing.capture_logs() as logs:
            assert await worker.sweep_detached_attachments_safe() == 1

        assert not _exists(att_id)
        assert not original.exists()
        assert await _gauge() == (0, 0)
        assert _events(logs, "unlinked_attachments_swept") == [
            {"event": "unlinked_attachments_swept", "count": 1, "log_level": "info"}
        ]
        assert _events(logs, "unlinked_attachments_sweep_disabled") == []
    finally:
        _cleanup()


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
async def test_the_detached_sweep_is_the_same_in_both_states(
    db_engine, monkeypatch, tmp_path, enabled
):
    """The switch gates the never-linked half only. A row detached past its grace is reclaimed and
    a row detached just now is kept, with the same count logged, whichever way it is set."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, SWITCH, enabled)
    try:
        now = datetime.now(UTC)
        past, past_file = _seed(tmp_path, "detached_past", age=100 * H, detached_at=now - 2 * H)
        fresh, fresh_file = _seed(tmp_path, "detached_fresh", age=100 * H, detached_at=now)

        with structlog.testing.capture_logs() as logs:
            assert await worker.sweep_detached_attachments_safe() == 1

        assert not _exists(past)
        assert not past_file.exists()
        assert _exists(fresh)
        assert fresh_file.exists()
        assert _events(logs, "detached_attachments_swept") == [
            {"event": "detached_attachments_swept", "count": 1, "log_level": "info"}
        ]
        # The skip is logged on every off pass, an empty never-linked population included.
        assert len(_events(logs, "unlinked_attachments_sweep_disabled")) == (0 if enabled else 1)
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("0", False), ("false", False), ("1", True), ("true", True)],
)
def test_the_switch_reads_the_environment_as_a_boolean(monkeypatch, raw, parsed):
    """It is turned on from the stack's environment, where every value is a string: "0" and
    "false" must read as off, not as a non-empty — truthy — string."""
    monkeypatch.setenv(SWITCH, raw)
    assert getattr(Settings(), SWITCH) is parsed


async def test_off_a_failing_detached_sweep_still_skips_the_never_linked_one(
    db_engine, monkeypatch, tmp_path
):
    """An error in the detached half is caught and the pass carries on — which must not carry it
    past the switch: the aged row stays and the skip is still on the record."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, SWITCH, False)

    async def boom(**_kwargs) -> int:
        raise RuntimeError("detached sweep down")

    monkeypatch.setattr(worker, "sweep_detached_attachments", boom)
    try:
        att_id, original = _seed(tmp_path, "held_past_an_error", age=73 * H)

        with structlog.testing.capture_logs() as logs:
            assert await worker.sweep_detached_attachments_safe() == 0

        assert _exists(att_id)
        assert original.exists()
        assert len(_events(logs, "detached_attachments.sweep_error")) == 1
        assert len(_events(logs, "unlinked_attachments_sweep_disabled")) == 1
        assert _events(logs, "unlinked_attachments_swept") == []
    finally:
        _cleanup()


async def test_off_two_racing_passes_both_keep_the_row(db_engine, monkeypatch, tmp_path):
    """Two hourly passes at once (a slow pass overlapping the next tick) each read the switch on
    their own and each skip: the row survives both, and each logs its own skip."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, SWITCH, False)
    try:
        att_id, original = _seed(tmp_path, "held_by_both", age=73 * H)

        with structlog.testing.capture_logs() as logs:
            first, second = await asyncio.gather(
                worker.sweep_detached_attachments_safe(), worker.sweep_detached_attachments_safe()
            )

        assert (first, second) == (0, 0)
        assert _exists(att_id)
        assert original.exists()
        assert len(_events(logs, "unlinked_attachments_sweep_disabled")) == 2
    finally:
        _cleanup()
