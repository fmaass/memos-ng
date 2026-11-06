"""One-shot import of the retired daily-dami satellite's state into memos-ng's digest tables.

The satellite kept everything it knew in two files: a ``state.json`` registry of what it had
already mailed, and a ``.env`` holding its recipients and its configuration. This script moves all
three of those into the database, so the first memos-ng digest neither re-mails 190 old memos nor
runs with the migration's default settings.

Three imports, in one transaction::

    1. Registry   sent_uids -> memos.uid -> digest_deliveries, all referencing ONE digest_runs row
                  with trigger='import' (find-or-create under uq_digest_runs_import).
    2. Recipients DAILY_MAIL_RECIPIENTS, normalised and de-duplicated (LSN-0026).
    3. Settings   the satellite's real configuration into digest_settings row 1 — ONE-SHOT.

Dry run by default: it reads, maps, and reports, and writes nothing at all — not even the run row.
``--execute`` writes.

Three decisions this script had to make, stated rather than buried:

* **``digest_deliveries.sent_at`` is the IMPORT time, not the registry's value.** The registry maps
  each uid to the memo's ``createTime`` — when the memo was WRITTEN, not when it was mailed — so
  stamping it into a column named ``sent_at`` would record a delivery time the satellite never
  recorded. The import time is what these rows' provenance actually is, and their run's
  ``trigger='import'`` already tells a reader they were not sent from here.
* **The run row is written with its terminal ``status`` in the INSERT.** ``digest_runs`` carries
  ``uq_digest_runs_single_inflight`` — ``UNIQUE ((1)) WHERE status IS NULL`` — so a committed
  status-less row IS the global single-flight lock, and a crash would leave it held forever with
  nothing to release it. An import has no in-flight window worth publishing, so it never occupies
  that index.
* **The settings write claims the row** by setting ``updated_by``. That is what makes the guard
  one-shot: the second ``--execute`` finds a non-NULL ``updated_by`` and reports
  ``settings: skipped (modified by …)`` instead of writing the same values again.

``--only-uid`` and ``--limit`` restrict the REGISTRY only: the recipients and the settings are
imported in full on any ``--execute``, because neither is per-memo and both are guarded on their
own (``ON CONFLICT DO NOTHING`` and the one-shot latch). The one-victim pass is therefore a probe
of the delivery mapping, which is the part with 190 rows and the only part that can be wrong
per-memo.

It prints COUNTS and COLUMN NAMES only — never a recipient address, never a uid, never a value out
of the ``.env``. Unmapped uids are named where an operator can act on them without the terminal
seeing them: the run's ``error`` field and a debug-level log line. They are never guessed at.

Run it inside the app container, which has the database::

    docker exec memos-ng-app python -m app.scripts.import_digest_state \
        --state /tmp/digest-import/state.json --env /tmp/digest-import/satellite.env
    docker exec memos-ng-app python -m app.scripts.import_digest_state \
        --state … --env … --execute --only-uid <one uid>   # the one-victim pass
    docker exec memos-ng-app python -m app.scripts.import_digest_state \
        --state … --env … --execute                        # the full pass
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from app.database import async_session
from app.models import (
    DIGEST_DEFAULT_LANGUAGE,
    DigestDelivery,
    DigestGroupDelivery,
    DigestRecipient,
    DigestRun,
    DigestRunStatus,
    DigestSettings,
    DigestTrigger,
    DigestVisibilityScope,
    Memo,
)
from app.services.digest import ledger as digest_ledger
from app.services.digest.domain import (
    SETTINGS_ID,
    SettingsMissing,
    normalise_email,
    settings_row,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# The single digest_runs row this import owns, and the writer stamped on every row it creates.
# `updated_by` doubles as the one-shot latch on digest_settings, so the string is part of the
# behaviour, not decoration: the second pass recognises the first pass by it.
IMPORT_TRIGGER = DigestTrigger.import_
IMPORTED_BY = "satellite-import"
# Deliveries the import writes belong to no mailed part. Real runs number their parts from 1.
IMPORT_PART_INDEX = 0


class ImportInputError(Exception):
    """The operator's input cannot be used: a file that cannot be read or parsed, or a
    ``--only-uid`` the registry does not contain.

    Deliberately distinct from an empty registry (LSN-0027): "the file could not be read" and "the
    file holds no entries" are different outcomes and must not share an exit path, or a
    misconfigured path reads as a completed import of nothing.
    """


# --- the satellite's files ---------------------------------------------------------------------


@dataclass(frozen=True)
class Registry:
    """``state.json`` as the satellite wrote it."""

    sent_uids: dict[str, str]
    last_run: str | None
    hosted_media: dict[str, object]


def parse_env(content: str) -> dict[str, str]:
    """``KEY=VALUE`` lines into a dict. Blank lines and ``#`` comments are skipped, an optional
    ``export`` prefix is dropped, and one matching pair of surrounding quotes is stripped.

    A line without ``=`` is not a KEY=VALUE line and is ignored rather than guessed at.
    """
    parsed: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def load_env(path: Path) -> dict[str, str]:
    """Read and parse the satellite's ``.env``, or raise ``ImportInputError``."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImportInputError(f"env file unreadable: {type(exc).__name__}") from exc
    return parse_env(content)


def load_registry(path: Path) -> Registry:
    """Read and parse ``state.json``, or raise ``ImportInputError``.

    A file that parses but whose ``sent_uids`` is not an object of uid -> string is unreadable, not
    empty — the shape is part of being readable.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImportInputError(f"state file unreadable: {type(exc).__name__}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImportInputError("state file unreadable: not valid JSON") from exc
    if not isinstance(document, dict):
        raise ImportInputError("state file unreadable: top level is not an object")
    sent_uids = document.get("sent_uids", {})
    if not isinstance(sent_uids, dict) or not all(isinstance(k, str) for k in sent_uids):
        raise ImportInputError("state file unreadable: sent_uids is not an object of uids")
    last_run = document.get("last_run")
    hosted_media = document.get("hosted_media", {})
    return Registry(
        sent_uids={k: str(v) for k, v in sent_uids.items()},
        last_run=last_run if isinstance(last_run, str) else None,
        hosted_media=hosted_media if isinstance(hosted_media, dict) else {},
    )


# --- the settings mapping ----------------------------------------------------------------------


def _coerce_text(limit: int) -> Callable[[str], str | None]:
    def coerce(raw: str) -> str | None:
        value = raw.strip()
        # Empty is "the satellite did not configure this", and a value too wide for the column is
        # not this script's to truncate — both leave the column alone.
        return value if value and len(value) <= limit else None

    return coerce


def _coerce_nonnegative_int(raw: str) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _coerce_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _coerce_date(raw: str) -> date | None:
    value = raw.strip().removesuffix("Z")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)
    except ValueError:
        return None


def _coerce_visibility(raw: str) -> DigestVisibilityScope | None:
    """The satellite's filter as a SET of visibilities, mapped onto the two-valued scope enum.

    The satellite is decommissioned and its source is gone, so the spelling it used cannot be read
    back; both the set form (``PUBLIC,PROTECTED``) and memos-ng's own enum name are accepted. Any
    other spelling maps to nothing and leaves ``visibility_scope`` alone — a scope is what decides
    who receives a memo, and guessing it wrong mails private writing to six external addresses.
    """
    tokens = {token for token in re.split(r"[,+|\s]+", raw.strip().upper()) if token}
    if tokens == {"PUBLIC"}:
        return DigestVisibilityScope.PUBLIC
    if tokens in ({"PUBLIC", "PROTECTED"}, {"PUBLIC_PROTECTED"}):
        return DigestVisibilityScope.PUBLIC_PROTECTED
    return None


def _coerce_email(raw: str) -> str | None:
    return normalise_email(raw)


# env key -> (settings column, coercion). Order is the order the report names them in.
SETTINGS_MAP: tuple[tuple[str, str, Callable[[str], object | None]], ...] = (
    ("SMTP_FROM_NAME", "from_name", _coerce_text(255)),
    ("SMTP_FROM_EMAIL", "from_email", _coerce_email),
    ("IMAGE_GRACE_MINUTES", "grace_minutes", _coerce_nonnegative_int),
    ("MAX_EMAIL_SIZE_BYTES", "max_email_bytes", _coerce_nonnegative_int),
    ("MAIL_EPOCH", "start_date", _coerce_date),
    ("MEMOS_VISIBILITY_FILTER", "visibility_scope", _coerce_visibility),
    ("DROPBOX_FOLDER", "dropbox_folder", _coerce_text(255)),
    ("DROPBOX_RETENTION_DAYS", "retention_days", _coerce_nonnegative_int),
    ("LINK_IMAGES_TO_DROPBOX", "link_images_full", _coerce_bool),
)
# Hosting is live iff the satellite held a refresh token. Presence only — the value is a secret and
# is never read for anything but its length.
HOSTING_TOKEN_KEY = "DROPBOX_REFRESH_TOKEN"
RECIPIENTS_KEY = "DAILY_MAIL_RECIPIENTS"


def plan_settings(env: dict[str, str]) -> tuple[dict[str, object], tuple[str, ...]]:
    """The column values the ``.env`` asks for, plus the names of the keys that could not be used.

    A key the satellite never set is simply absent — it is not "unusable" and its column keeps
    whatever the migration seeded. A key that IS set but whose value does not fit its column is
    reported by KEY NAME (never by value) and its column is left alone.
    """
    values: dict[str, object] = {}
    unusable: list[str] = []
    for key, column, coerce in SETTINGS_MAP:
        if key not in env:
            continue
        coerced = coerce(env[key])
        if coerced is None:
            unusable.append(key)
            continue
        values[column] = coerced
    values["hosting_enabled"] = bool(env.get(HOSTING_TOKEN_KEY, "").strip())
    return values, tuple(unusable)


def plan_recipients(env: dict[str, str]) -> tuple[tuple[str, ...], int, int]:
    """``(addresses, candidates, rejected)`` — normalised, de-duplicated, first spelling wins.

    ``normalise_email`` is the API's own function and is reused rather than re-derived, so the
    import and the panel can never disagree about which stored string an address becomes.
    """
    raw = env.get(RECIPIENTS_KEY, "")
    candidates = [part for part in raw.split(",") if part.strip()]
    seen: dict[str, None] = {}
    rejected = 0
    for candidate in candidates:
        normalised = normalise_email(candidate)
        if normalised is None:
            rejected += 1
            continue
        seen.setdefault(normalised, None)
    return tuple(seen), len(candidates), rejected


# --- the report --------------------------------------------------------------------------------


@dataclass
class RegistryCounts:
    entries_in_file: int = 0
    considered: int = 0  # after --only-uid / --limit
    mapped: int = 0
    unmapped: int = 0
    inserted: int = 0
    already_present: int = 0
    hosted_media_entries: int = 0  # reported, never imported — digest_hosted_media is not seeded


@dataclass
class RecipientCounts:
    candidates: int = 0
    rejected: int = 0
    unique: int = 0
    inserted: int = 0
    already_present: int = 0


@dataclass
class SettingsOutcome:
    columns: tuple[str, ...] = ()
    unusable_keys: tuple[str, ...] = ()
    written: bool = False
    skipped_reason: str | None = None


@dataclass
class ImportReport:
    registry: RegistryCounts = field(default_factory=RegistryCounts)
    recipients: RecipientCounts = field(default_factory=RecipientCounts)
    settings: SettingsOutcome = field(default_factory=SettingsOutcome)
    run_id: int | None = None
    run_memos_sent: int | None = None

    def format(self, *, execute: bool) -> str:
        mode = "EXECUTE" if execute else "DRY RUN — nothing written"
        reg, rec, sets = self.registry, self.recipients, self.settings
        lines = [f"[{mode}]"]
        if reg.entries_in_file == 0:
            lines.append("registry: no entries in the state file (the file was read)")
        lines.append(
            f"registry: entries={reg.entries_in_file} considered={reg.considered} "
            f"mapped={reg.mapped} unmapped={reg.unmapped} "
            f"inserted={reg.inserted} already_present={reg.already_present}"
        )
        if reg.hosted_media_entries:
            lines.append(
                f"registry: hosted_media entries={reg.hosted_media_entries} (not imported)"
            )
        lines.append(
            f"recipients: candidates={rec.candidates} unique={rec.unique} "
            f"rejected={rec.rejected} inserted={rec.inserted} "
            f"already_present={rec.already_present}"
        )
        if sets.skipped_reason:
            lines.append(f"settings: skipped ({sets.skipped_reason})")
        else:
            verb = "wrote" if sets.written else "would write"
            lines.append(f"settings: {verb} {len(sets.columns)} columns: {', '.join(sets.columns)}")
        if sets.unusable_keys:
            lines.append(
                f"settings: unusable keys (left unchanged): {', '.join(sets.unusable_keys)}"
            )
        run = "none (dry run)" if self.run_id is None else str(self.run_id)
        lines.append(f"run: trigger={IMPORT_TRIGGER} id={run} memos_sent={self.run_memos_sent}")
        return "\n".join(lines)


# --- the import ---------------------------------------------------------------------------------


def _local_date(timezone: str | None) -> date:
    try:
        zone = ZoneInfo(timezone) if timezone else UTC
    except Exception:  # an unparseable tz in the row must not abort the import
        zone = UTC
    return datetime.now(zone).date()


# The run's record of what the registry named and the database lacks, written in this shape so a
# later pass can read its own earlier passes back and add to them rather than overwrite them.
_UNMAPPED_RE = re.compile(r"unmapped uid\(s\):\s*(?P<uids>.*)\Z", re.DOTALL)


def parse_unmapped(recorded: str | None) -> list[str]:
    """The uids a previous pass recorded in ``digest_runs.error``, in the order it wrote them."""
    if not recorded:
        return []
    match = _UNMAPPED_RE.search(recorded)
    if match is None:
        return []
    return [uid for uid in (part.strip() for part in match["uids"].split(",")) if uid]


def merge_unmapped(
    recorded: str | None, found: Sequence[str], *, authoritative: bool
) -> str | None:
    """What ``digest_runs.error`` should say after a pass that met ``found``.

    An UNRESTRICTED pass looked at the whole registry, so what it found IS the set and it may
    replace what is there. A restricted pass (``--only-uid`` / ``--limit``) saw a slice: it must
    still record what it met — an unmapped uid is the one thing an operator has to act on — but it
    may only ADD, never clear the record of a uid it did not look at, and never name one twice.
    """
    uids = list(found) if authoritative else [*parse_unmapped(recorded), *found]
    unique = list(dict.fromkeys(uids))
    return f"{len(unique)} unmapped uid(s): {', '.join(unique)}" if unique else None


def format_settings_skip(updated_by: str | None, updated_at: datetime) -> str:
    """Why the settings write was refused, WITHOUT quoting the stored `updated_by`.

    Who last touched the row is a person's name out of the database; the operator needs to know
    that someone did and when, not to have the value echoed into a terminal. The one distinction
    worth drawing is between an earlier pass of this same import and anybody else — reporting the
    import's own latch as "an admin" would be false.
    """
    who = "this import" if updated_by == IMPORTED_BY else "an admin"
    return f"modified by {who} at {updated_at.isoformat()}"


def positive_int(raw: str) -> int:
    """``--limit`` as a count. Zero and negatives are a usage error, not a silent slice: Python
    would read ``[:0]`` as "nothing" and ``[:-1]`` as "all but the last", and neither is what an
    operator typing a limit meant."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


async def claim_settings(db: AsyncSession, values: dict[str, object], *, force: bool) -> int:
    """Write the settings columns and claim the row, CONDITIONALLY, in one statement.

    The refusal lives in the WHERE clause rather than in a preceding read, so there is no window
    between deciding and writing for an admin's save to fall into: a row somebody else has already
    claimed simply matches nothing. Returns the number of rows affected — 0 means refused.
    """
    statement = (
        update(DigestSettings)
        .where(DigestSettings.id == SETTINGS_ID)
        .values(**values, updated_by=IMPORTED_BY, updated_at=func.now())
    )
    if not force:
        statement = statement.where(DigestSettings.updated_by.is_(None))
    # RETURNING rather than `rowcount`: the ids come back typed, and "which rows did this write"
    # is the question being asked.
    return len((await db.execute(statement.returning(DigestSettings.id))).scalars().all())


async def read_settings_latch(db: AsyncSession) -> tuple[str | None, datetime] | None:
    """``(updated_by, updated_at)`` read straight off the row, or None when there is no row.

    Read as COLUMNS rather than off a loaded object: `updated_at` carries an `onupdate`, so any
    earlier write in this session leaves the attribute expired, and touching it would then be a
    lazy load in async code (MissingGreenlet).
    """
    row = (
        await db.execute(
            select(DigestSettings.updated_by, DigestSettings.updated_at).where(
                DigestSettings.id == SETTINGS_ID
            )
        )
    ).one_or_none()
    return None if row is None else (row.updated_by, row.updated_at)


async def ensure_import_run(db: AsyncSession, *, local_date: date) -> int:
    """The id of the one ``trigger='import'`` run row, found or created.

    ``uq_digest_runs_import`` is a partial unique index, so the INSERT infers it and does nothing
    when the row already exists — find-or-create in one statement rather than a check-then-insert
    two callers could both win. ``status`` is written terminally IN the insert; see the module
    docstring for why a status-less import row must never be committed.

    It returns the ID and not the row: every later write to this run goes through a locked,
    SQL-side UPDATE, which would leave a loaded ORM object silently stale.
    """
    now = datetime.now(UTC)
    await db.execute(
        pg_insert(DigestRun)
        .values(
            trigger=IMPORT_TRIGGER,
            started_at=now,
            finished_at=now,
            status=DigestRunStatus.sent,
            local_date=local_date,
        )
        .on_conflict_do_nothing(index_elements=["trigger"], index_where=text("trigger = 'import'"))
    )
    return (
        await db.execute(select(DigestRun.id).where(DigestRun.trigger == IMPORT_TRIGGER))
    ).scalar_one()


async def map_uids(db: AsyncSession, uids: list[str]) -> dict[str, int]:
    """``uid -> memos.id`` for the uids that exist. A uid with no memo is simply absent."""
    if not uids:
        return {}
    rows = (await db.execute(select(Memo.uid, Memo.id).where(Memo.uid.in_(uids)))).all()
    return {uid: memo_id for uid, memo_id in rows}


async def run_import(
    db: AsyncSession,
    *,
    registry: Registry,
    env: dict[str, str],
    execute: bool,
    limit: int | None = None,
    only_uid: str | None = None,
    force_settings: bool = False,
) -> ImportReport:
    """Perform (or, without ``execute``, project) the three imports. Does not commit."""
    report = ImportReport()
    timezone = (
        await db.execute(select(DigestSettings.timezone).where(DigestSettings.id == SETTINGS_ID))
    ).scalar_one_or_none()

    # --- 1. registry ---------------------------------------------------------------------------
    uids = list(registry.sent_uids)
    report.registry.entries_in_file = len(uids)
    report.registry.hosted_media_entries = len(registry.hosted_media)
    if limit is not None and limit < 1:
        # The CLI refuses this at parse time; a caller reaching run_import directly gets the same
        # answer rather than a negative slice quietly importing "all but the last".
        raise ImportInputError("--limit must be 1 or more")
    if only_uid is not None:
        if only_uid not in registry.sent_uids:
            raise ImportInputError("the uid given to --only-uid is not in the registry")
        uids = [only_uid]
    if limit is not None:
        uids = uids[:limit]
    report.registry.considered = len(uids)

    mapping = await map_uids(db, uids)
    unmapped = [uid for uid in uids if uid not in mapping]
    report.registry.mapped = len(mapping)
    report.registry.unmapped = len(unmapped)
    for uid in unmapped:
        # Named where an operator can act on it; the terminal only ever sees the count.
        logger.debug("digest_import.unmapped_uid", uid=uid)

    if execute:
        run_id = await ensure_import_run(db, local_date=_local_date(timezone))
        report.run_id = run_id
        if mapping:
            inserted = (
                await db.execute(
                    pg_insert(DigestDelivery)
                    .values(
                        [
                            {
                                "memo_id": memo_id,
                                "run_id": run_id,
                                "part_index": IMPORT_PART_INDEX,
                                "incomplete": False,
                            }
                            for memo_id in mapping.values()
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["memo_id"])
                    .returning(DigestDelivery.memo_id)
                )
            ).all()
            report.registry.inserted = len(inserted)
            # The satellite mailed ONE group, today's single language: its history goes into
            # the per-group ledger too, or that group's next run would mail it all again.
            await db.execute(
                pg_insert(DigestGroupDelivery)
                .values(
                    [
                        {
                            "memo_id": memo_id,
                            "language": DIGEST_DEFAULT_LANGUAGE.value,
                            "run_id": run_id,
                            "part_index": IMPORT_PART_INDEX,
                            "incomplete": False,
                        }
                        for memo_id in mapping.values()
                    ]
                )
                .on_conflict_do_nothing(index_elements=["memo_id", "language"])
            )
        report.registry.already_present = report.registry.mapped - report.registry.inserted
        # The run row aggregates rows another pass may be inserting at the same time, so it is
        # LOCKED before it is read and rewritten, and the count is computed IN the UPDATE. A total
        # that travelled through this process's memory could be written back over a larger one by
        # whichever pass committed last.
        recorded_error = (
            await db.execute(
                select(DigestRun.error).where(DigestRun.id == run_id).with_for_update()
            )
        ).scalar_one()
        delivered = (
            select(func.count())
            .select_from(DigestDelivery)
            .where(DigestDelivery.run_id == run_id)
            .scalar_subquery()
        )
        await db.execute(
            update(DigestRun)
            .where(DigestRun.id == run_id)
            .values(
                memos_sent=delivered,
                finished_at=func.now(),
                error=merge_unmapped(
                    recorded_error, unmapped, authoritative=only_uid is None and limit is None
                ),
            )
        )
        report.run_memos_sent = (
            await db.execute(select(DigestRun.memos_sent).where(DigestRun.id == run_id))
        ).scalar_one()
    else:
        report.registry.already_present = 0

    # --- 2. recipients -------------------------------------------------------------------------
    addresses, candidates, rejected = plan_recipients(env)
    report.recipients.candidates = candidates
    report.recipients.rejected = rejected
    report.recipients.unique = len(addresses)
    if execute and addresses:
        before = await digest_ledger.active_languages(db)
        inserted_rows = (
            await db.execute(
                pg_insert(DigestRecipient)
                .values([{"email": address, "added_by": IMPORTED_BY} for address in addresses])
                .on_conflict_do_nothing(index_elements=["email"])
                .returning(DigestRecipient.email)
            )
        ).all()
        report.recipients.inserted = len(inserted_rows)
        await db.flush()
        await digest_ledger.seed_newly_active(db, before)
        report.recipients.already_present = len(addresses) - report.recipients.inserted

    # --- 3. settings ---------------------------------------------------------------------------
    values, unusable = plan_settings(env)
    report.settings.columns = tuple(values)
    report.settings.unusable_keys = unusable
    if execute:
        try:
            # The domain's own helper, and its `for_update` is a mutex rather than a fresh read:
            # it holds the singleton for the rest of this transaction, so an admin's save cannot
            # land between this decision and the write below. It is the DOMAIN's and not the
            # router's because this process serves no HTTP — catching an `HTTPException` in a CLI
            # is a layering mistake that reads as an ordinary one.
            await settings_row(db, for_update=True)
        except SettingsMissing:
            # The migration seeds the row; its absence is a broken install, not something an
            # import may repair by INSERTing the second row the CHECK exists to forbid.
            report.settings.skipped_reason = f"no digest_settings row {SETTINGS_ID}"
        else:
            if await claim_settings(db, values, force=force_settings):
                report.settings.written = True
            else:
                latch = await read_settings_latch(db)
                report.settings.skipped_reason = (
                    format_settings_skip(*latch)
                    if latch is not None
                    else f"no digest_settings row {SETTINGS_ID}"
                )
    else:
        latch = await read_settings_latch(db)
        if latch is None:
            report.settings.skipped_reason = f"no digest_settings row {SETTINGS_ID}"
        elif latch[0] is not None and not force_settings:
            report.settings.skipped_reason = format_settings_skip(*latch)
    return report


async def _amain(
    *,
    registry: Registry,
    env: dict[str, str],
    execute: bool,
    limit: int | None,
    only_uid: str | None,
    force_settings: bool,
) -> ImportReport:
    async with async_session() as db:
        report = await run_import(
            db,
            registry=registry,
            env=env,
            execute=execute,
            limit=limit,
            only_uid=only_uid,
            force_settings=force_settings,
        )
        if execute:
            await db.commit()
        return report


def main() -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(
        description=(
            "One-shot import of the daily-dami satellite's registry, recipients and settings "
            "(idempotent; dry run unless --execute)."
        )
    )
    parser.add_argument("--state", required=True, type=Path, help="the satellite's state.json")
    parser.add_argument("--env", required=True, type=Path, help="the satellite's .env")
    parser.add_argument(
        "--execute", action="store_true", help="write; the default reads and reports only"
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="map only the first N registry entries, N >= 1 (the registry only)",
    )
    parser.add_argument(
        "--only-uid",
        help="map only this registry uid — the one-victim pass (the registry only)",
    )
    parser.add_argument(
        "--force-settings",
        action="store_true",
        help="write the settings even though the row was modified after the migration seeded it",
    )
    args = parser.parse_args()
    try:
        registry = load_registry(args.state)
        env = load_env(args.env)
        report = asyncio.run(
            _amain(
                registry=registry,
                env=env,
                execute=args.execute,
                limit=args.limit,
                only_uid=args.only_uid,
                force_settings=args.force_settings,
            )
        )
    except ImportInputError as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        return 2
    print(report.format(execute=args.execute))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
