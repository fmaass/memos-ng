"""The calendar gate: is today's digest due?

A pure function with no I/O and no clock of its own — `now_utc` is passed in — because it runs on
every worker poll (~10 s) and because "was it due at 06:59:59 on the day the clocks went back?" is
a question a test must be able to ask directly. The worker supplies the two facts it cannot know:
the settings row, and the local date of the most recent run that COUNTS as today's attempt.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.models import DigestTrigger
from app.services.digest.domain import DigestConfig

logger = structlog.get_logger(__name__)

# The triggers whose runs count as "today's attempt". A `test` send persists nothing and an
# `import` writes history, so neither may suppress the day's real send (B7): importing on the
# morning of enablement would otherwise make the import row today's run and silently swallow the
# very first scheduled digest.
LEDGER_TRIGGERS: frozenset[str] = frozenset({DigestTrigger.scheduled, DigestTrigger.manual})

# Fallback zone for a persisted value `ZoneInfo` cannot load. It is the household's zone and the
# column's own server default, so it is what the row would have held had nothing corrupted it.
FALLBACK_TIMEZONE = "Europe/Zurich"

# Zone strings already reported. The gate is polled every ~10 s, and a row carrying an unusable
# zone would otherwise emit a traceback-sized error six times a minute for as long as it stands,
# burying the one line that says what to fix. Process-local by design: a restart re-reports, which
# is the right behaviour for an operator who has just deployed.
_REPORTED_BAD_ZONES: set[str] = set()


def reset_reported_zones() -> None:
    """Forget which bad zones have been reported. For tests, and for nothing else."""
    _REPORTED_BAD_ZONES.clear()


def resolve_zone(name: str) -> ZoneInfo | None:
    """`ZoneInfo(name)`, or None with ONE error logged per distinct bad value.

    Returning None rather than raising is the whole point. This is called from the worker's poll
    loop; a raise there would spawn a failing task every ten seconds and bury the cause under a
    stream of identical tracebacks. The API refuses an unloadable zone at `PUT`
    (`app/routers/admin_digest.py`), so a row can only carry one when it predates that validation
    or was written straight into the database — a condition to report once and step around, not to
    crash the worker over.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name not in _REPORTED_BAD_ZONES:
            _REPORTED_BAD_ZONES.add(name)
            logger.error(
                "digest.timezone_unresolvable",
                timezone=name,
                fallback=FALLBACK_TIMEZONE,
                detail=(
                    "digest_settings.timezone cannot be loaded; the digest is treated as NOT DUE "
                    "until it is corrected via PUT /api/admin/digest/settings"
                ),
            )
        return None


def local_date_of(moment_utc: datetime, timezone: str) -> date:
    """The local calendar date `moment_utc` falls on in `timezone`.

    Falls back to `FALLBACK_TIMEZONE` when the zone will not load, so the run row a defensive
    caller is about to write still carries a plausible `local_date` rather than crashing the
    writer. The gate itself never gets that far — it has already answered not-due.
    """
    zone = resolve_zone(timezone) or ZoneInfo(FALLBACK_TIMEZONE)
    return moment_utc.astimezone(zone).date()


def start_of_local_day_utc(day: date, timezone: str) -> datetime:
    """Midnight at the start of `day` in `timezone`, as a UTC instant.

    Used to turn `digest_settings.start_date` — a LOCAL date the admin typed — into the bound a
    `created_at >= …` comparison against UTC-stored timestamps can use, which is what the satellite
    does at `app/__main__.py:37-39`.
    """
    zone = resolve_zone(timezone) or ZoneInfo(FALLBACK_TIMEZONE)
    return datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(ZoneInfo("UTC"))


def digest_due(
    now_utc: datetime,
    settings: DigestConfig,
    last_run_local_date: date | None,
) -> bool:
    """Whether the scheduled digest should start now.

    `last_run_local_date` is the local date of the most recent run whose trigger is in
    `LEDGER_TRIGGERS` — including one still in flight (`status IS NULL`), which is what makes a
    second worker's gate answer false while the first is still sending, before the unique index
    ever has to decide it.

    AT MOST ONE automatic attempt per local day, and "at most" is meant literally: a `failed` day
    is not retried automatically. The 26 h stale alert and the admin's Run now are the recovery,
    which is exactly what a single daily timer offered. If the worker is down for the whole window
    between `send_time` and local midnight, that day gets no attempt and none is back-filled —
    the case `MemosDigestStale` exists to surface.
    """
    if not settings.enabled:
        return False
    zone = resolve_zone(settings.timezone)
    if zone is None:
        return False
    local_now = now_utc.astimezone(zone)
    if local_now.time() < settings.send_time:
        return False
    if last_run_local_date is not None and last_run_local_date >= local_now.date():
        return False
    return True
