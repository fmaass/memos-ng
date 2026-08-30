"""The calendar gate (MEMNG-234).

Pure, so every case is a direct call: no database, no clock, no worker loop. The DST cases are the
phase's FIRST verification (LSN-0040 — probe the DST edges before trusting a schedule), because a
gate that reads `now_utc.time()` instead of the LOCAL time is right for 363 days a year and sends
an hour early or not at all on the other two.
"""

from datetime import UTC, date, datetime, time

import pytest
from app.models import DigestTrigger
from app.services.digest import gate
from app.services.digest.domain import DigestConfig
from structlog.testing import capture_logs

from tests.digest_support import digest_config


def _settings(**overrides) -> DigestConfig:
    """A settings SNAPSHOT. The gate never touches the session — it reads a frozen value the
    caller loaded — so a plain dataclass is the honest input: it cannot accidentally read
    something the caller did not set, and it cannot lazily load anything either."""
    return digest_config(**overrides)


@pytest.fixture(autouse=True)
def _forget_reported_zones():
    gate.reset_reported_zones()
    yield
    gate.reset_reported_zones()


# --- the ordinary day -------------------------------------------------------------------------


def test_a_disabled_digest_is_never_due():
    settings = _settings(enabled=False)
    assert gate.digest_due(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), settings, None) is False


def test_before_send_time_is_not_due():
    # 04:59 UTC is 06:59 in Zurich in July — one minute short of 07:00.
    settings = _settings()
    assert gate.digest_due(datetime(2026, 7, 1, 4, 59, tzinfo=UTC), settings, None) is False


def test_at_send_time_is_due():
    settings = _settings()
    assert gate.digest_due(datetime(2026, 7, 1, 5, 0, tzinfo=UTC), settings, None) is True


def test_after_send_time_is_still_due_until_local_midnight():
    settings = _settings()
    assert gate.digest_due(datetime(2026, 7, 1, 21, 30, tzinfo=UTC), settings, None) is True


def test_a_run_already_recorded_for_today_suppresses_a_second():
    settings = _settings()
    now = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 Zurich
    assert gate.digest_due(now, settings, date(2026, 7, 1)) is False


def test_yesterdays_run_does_not_suppress_today():
    settings = _settings()
    now = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
    assert gate.digest_due(now, settings, date(2026, 6, 30)) is True


def test_a_failed_day_is_not_retried_automatically():
    """"At most one" is literal: the ledger date is what suppresses, not the run's outcome. The
    26 h alert and the admin's Run now are the recovery — which is exactly what a single daily
    timer offered."""
    settings = _settings()
    for hour in (6, 12, 21):
        now = datetime(2026, 7, 1, hour, tzinfo=UTC)
        assert gate.digest_due(now, settings, date(2026, 7, 1)) is False


def test_test_and_import_runs_are_not_part_of_the_day_ledger():
    """B7: importing on the morning of enablement must not swallow the first scheduled send."""
    assert set(gate.LEDGER_TRIGGERS) == {DigestTrigger.scheduled, DigestTrigger.manual}
    assert DigestTrigger.test not in gate.LEDGER_TRIGGERS
    assert DigestTrigger.import_ not in gate.LEDGER_TRIGGERS


# --- the timezone is the local clock, not UTC --------------------------------------------------


def test_a_non_default_zone_is_honoured():
    """The existing suite only ever exercised Europe/Zurich, which is +2 from UTC in July — a gate
    that compared UTC times would pass every one of those tests."""
    settings = _settings(timezone="America/New_York")
    # 10:59 UTC is 06:59 in New York (EDT); 11:01 UTC is 07:01.
    assert gate.digest_due(datetime(2026, 7, 1, 10, 59, tzinfo=UTC), settings, None) is False
    assert gate.digest_due(datetime(2026, 7, 1, 11, 1, tzinfo=UTC), settings, None) is True


def test_a_zone_change_moves_the_window():
    at = datetime(2026, 7, 1, 5, 30, tzinfo=UTC)
    assert gate.digest_due(at, _settings(timezone="Europe/Zurich"), None) is True
    assert gate.digest_due(at, _settings(timezone="America/New_York"), None) is False


# --- the DST folds (LSN-0040) ------------------------------------------------------------------


def test_spring_forward_2026_03_29_send_time_inside_the_lost_hour():
    """02:00 → 03:00: a 02:30 send_time NEVER occurs locally that day.

    The gate must still fire, because it asks "is the local time at or past send_time", not "is it
    exactly send_time" — the first poll after the jump reads 03:00 local, which is past 02:30.
    A gate written as an equality would silently skip this day every year.
    """
    settings = _settings(send_time=time(2, 30))
    # 00:59 UTC = 01:59 CET, before the jump and before send_time.
    assert gate.digest_due(datetime(2026, 3, 29, 0, 59, tzinfo=UTC), settings, None) is False
    # 01:00 UTC = 03:00 CEST, immediately after the jump: past 02:30 local.
    assert gate.digest_due(datetime(2026, 3, 29, 1, 0, tzinfo=UTC), settings, None) is True


def test_spring_forward_2026_03_29_normal_send_time():
    settings = _settings(send_time=time(7, 0))
    # 04:59 UTC = 06:59 CEST (the offset is already +2 after the jump).
    assert gate.digest_due(datetime(2026, 3, 29, 4, 59, tzinfo=UTC), settings, None) is False
    assert gate.digest_due(datetime(2026, 3, 29, 5, 0, tzinfo=UTC), settings, None) is True


def test_autumn_fold_2026_10_25_send_time_inside_the_repeated_hour():
    """03:00 → 02:00: local 02:30 happens TWICE, once at +2 and once at +1.

    Both passes are "due" on their own, and the day ledger is what stops the second one sending —
    which is the case the next test pins.
    """
    settings = _settings(send_time=time(2, 30))
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET, the repeat
    assert first.astimezone(gate.resolve_zone("Europe/Zurich")).time() == time(2, 30)
    assert second.astimezone(gate.resolve_zone("Europe/Zurich")).time() == time(2, 30)
    assert gate.digest_due(first, settings, None) is True
    assert gate.digest_due(second, settings, None) is True


def test_a_run_recorded_before_the_autumn_fold_suppresses_the_repeat():
    """The fold's real hazard: without a DATE ledger, the repeated hour would send twice."""
    settings = _settings(send_time=time(2, 30))
    repeat = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert gate.digest_due(repeat, settings, date(2026, 10, 25)) is False


def test_the_local_date_is_the_same_on_both_sides_of_the_autumn_fold():
    for moment in (
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 22, 30, tzinfo=UTC),
    ):
        assert gate.local_date_of(moment, "Europe/Zurich") == date(2026, 10, 25)


def test_late_evening_utc_is_already_tomorrow_in_zurich():
    """The local date is what the ledger keys on, so a UTC date would suppress the wrong day."""
    assert gate.local_date_of(
        datetime(2026, 7, 1, 22, 30, tzinfo=UTC), "Europe/Zurich"
    ) == date(2026, 7, 2)


# --- an unresolvable zone already in the row ---------------------------------------------------


def test_an_unresolvable_zone_makes_the_gate_not_due():
    settings = _settings(timezone="Europe/Zurichh")
    assert gate.digest_due(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), settings, None) is False


def test_an_unresolvable_zone_logs_exactly_once_across_many_polls():
    """A raise inside the gate would spawn a failing task on EVERY poll, ten seconds apart, and
    bury the real cause under identical tracebacks. Asserted across several polls, not one —
    a per-call log would pass a single-call test and still flood the journal."""
    settings = _settings(timezone="Europe/Zurichh")
    with capture_logs() as logs:
        for _ in range(10):
            assert gate.digest_due(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), settings, None) is False
    reported = [entry for entry in logs if entry["event"] == "digest.timezone_unresolvable"]
    assert len(reported) == 1, logs
    assert reported[0]["timezone"] == "Europe/Zurichh"
    assert reported[0]["log_level"] == "error"


def test_a_second_distinct_bad_zone_is_reported_separately():
    with capture_logs() as logs:
        gate.digest_due(datetime(2026, 7, 1, 12, tzinfo=UTC), _settings(timezone="Bad/One"), None)
        gate.digest_due(datetime(2026, 7, 1, 12, tzinfo=UTC), _settings(timezone="Bad/Two"), None)
    reported = [
        entry["timezone"]
        for entry in logs
        if entry["event"] == "digest.timezone_unresolvable"
    ]
    assert reported == ["Bad/One", "Bad/Two"]


def test_local_date_of_falls_back_rather_than_raising_on_a_bad_zone():
    """A defensive caller about to write a run row still needs a plausible `local_date`."""
    assert gate.local_date_of(
        datetime(2026, 7, 1, 6, 0, tzinfo=UTC), "Europe/Zurichh"
    ) == date(2026, 7, 1)


def test_start_of_local_day_is_the_utc_instant_the_local_day_begins():
    """`start_date` is a LOCAL date the admin typed, compared against UTC-stored `created_at`."""
    assert gate.start_of_local_day_utc(date(2026, 6, 1), "Europe/Zurich") == datetime(
        2026, 5, 31, 22, 0, tzinfo=UTC
    )
    assert gate.start_of_local_day_utc(date(2026, 1, 15), "Europe/Zurich") == datetime(
        2026, 1, 14, 23, 0, tzinfo=UTC
    )
