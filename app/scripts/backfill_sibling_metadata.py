"""Backfill: enqueue the img-manager `sibling` lookup for attachments uploaded before it existed.

The script only ENQUEUES `sibling` media jobs; the WORKER drains them through the very code path an
upload's finalize uses (`app/services/sibling.py`). The script itself never asks img-manager, never
writes an attachment column and never opens an original.

Three populations, each reached only by its own switch and DISJOINT by construction:

* default — image attachments, `status='ready'`, owned by a `SIBLING_OWNER_IDS` member, whose own
  file left the capture date (neither instant nor civil time) or the location unsaid, with
  `sibling_outcome IS NULL` and NO `sibling` job that is queued, claimed, processing or failed. A
  `done` job with a NULL outcome is an anomaly the job forbids; such a row is re-enqueued here
  rather than stranded.
* `--retry-failed` — `sibling_outcome IS NULL` and the attachment's most recent `sibling` job is
  `failed` (img-manager was unavailable for every attempt), with none live.
* `--include-ambiguous` — `sibling_outcome = 'ambiguous'` with no live `sibling` job, for a later
  change of the election rule.

A second default `--execute` enqueues nothing: every row the first one selected now has a queued
job, and later an outcome.

    docker exec memos-ng-app python -m app.scripts.backfill_sibling_metadata             # dry run
    docker exec memos-ng-app python -m app.scripts.backfill_sibling_metadata --execute --limit 1
    docker exec memos-ng-app python -m app.scripts.backfill_sibling_metadata --execute

Add `--retry-failed` or `--include-ambiguous` to reach the other two populations instead.

`--audit` is a fourth, READ-ONLY mode for a change of the plausibility rule (MEMNG-843): it
re-judges every date the lookup already borrowed (`capture_source = 'sibling'`) against
`sibling.plausible`, with the same anchors the job uses, and lists the rows the rule would have
refused. It also opens each such row's ORIGINAL — the one place this script reads a file — and
lists the rows whose own metadata now dates them (MEMNG-844: the XMP packet of a Lightroom export),
which a fill of their own should replace. It enqueues nothing, writes nothing and refuses
`--execute`; correcting a listed row is a separate, reviewed step.

    docker exec memos-ng-app python -m app.scripts.backfill_sibling_metadata --audit
"""

from __future__ import annotations

import argparse
import asyncio
import enum
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anyio import to_thread
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus, JobKind, JobLane, JobState, MediaJob, Memo
from app.services import capture, sibling
from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import aliased

LIVE_STATES = (JobState.queued, JobState.claimed, JobState.processing)
DAY = timedelta(days=1)


class Population(enum.StrEnum):
    default = "default"
    retry_failed = "retry-failed"
    include_ambiguous = "include-ambiguous"


def _sibling_job_in(states: tuple[JobState, ...]):
    job = aliased(MediaJob)
    return exists().where(
        job.attachment_id == Attachment.id,
        job.kind == JobKind.sibling,
        job.state.in_(states),
    )


def _latest_sibling_job_failed():
    job = aliased(MediaJob)
    latest_state = (
        select(job.state)
        .where(job.attachment_id == Attachment.id, job.kind == JobKind.sibling)
        .order_by(job.id.desc())
        .limit(1)
        .correlate(Attachment)
        .scalar_subquery()
    )
    return latest_state == JobState.failed


def candidate_statement(
    population: Population, *, limit: int | None = None
) -> Select[tuple[int, str]]:
    """The attachments to enqueue a `sibling` job for, lowest id first."""
    stmt = select(Attachment.id, Attachment.mime)
    if population is Population.default:
        nothing_said = and_(
            Attachment.capture_instant.is_(None), Attachment.capture_civil.is_(None)
        )
        stmt = stmt.where(
            Attachment.mime.startswith("image/"),
            Attachment.status == AttachmentStatus.ready,
            Attachment.owner_id.in_(sorted(settings.sibling_owner_ids)),
            or_(nothing_said, Attachment.latitude.is_(None)),
            Attachment.sibling_outcome.is_(None),
            ~_sibling_job_in((*LIVE_STATES, JobState.failed)),
        )
    elif population is Population.retry_failed:
        stmt = stmt.where(
            Attachment.sibling_outcome.is_(None),
            _latest_sibling_job_failed(),
            ~_sibling_job_in(LIVE_STATES),
        )
    else:
        stmt = stmt.where(
            Attachment.sibling_outcome == "ambiguous",
            ~_sibling_job_in(LIVE_STATES),
        )
    stmt = stmt.order_by(Attachment.id)
    return stmt if limit is None else stmt.limit(limit)


@dataclass
class Report:
    population: Population
    execute: bool
    selected: int = 0
    enqueued: int = 0
    by_mime: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)

    def format(self) -> str:
        mode = "EXECUTE" if self.execute else "DRY RUN (nothing written)"
        lines = [
            f"backfill_sibling_metadata — {mode}, population={self.population.value}",
            f"  selected: {self.selected}",
            f"  enqueued: {self.enqueued}",
            "  selected by mime:",
            *(f"    {mime}: {n}" for mime, n in sorted(self.by_mime.items())),
            "  sibling_outcome across all attachments (before this run):",
            *(f"    {outcome}: {n}" for outcome, n in sorted(self.outcomes.items())),
        ]
        return "\n".join(lines)


async def run(population: Population, *, execute: bool, limit: int | None = None) -> Report:
    report = Report(population=population, execute=execute)
    async with async_session() as db:
        for outcome, n in (
            await db.execute(
                select(Attachment.sibling_outcome, func.count(Attachment.id)).group_by(
                    Attachment.sibling_outcome
                )
            )
        ).all():
            report.outcomes[outcome or "NULL"] = n
        rows = (await db.execute(candidate_statement(population, limit=limit))).all()
        report.selected = len(rows)
        report.by_mime.update(mime for _, mime in rows)
        if execute:
            # One transaction for the batch: the population is re-evaluated by the next run, so a
            # run that fails part-way leaves nothing half-enqueued to reason about.
            db.add_all(
                MediaJob(
                    attachment_id=attachment_id,
                    lane=JobLane.fast,
                    kind=JobKind.sibling,
                    state=JobState.queued,
                )
                for attachment_id, _ in rows
            )
            await db.commit()
            report.enqueued = len(rows)
        else:
            await db.rollback()
    return report


@dataclass(frozen=True)
class Refusal:
    attachment_id: int
    memo_id: int | None
    borrowed: datetime
    reason: str  # 'after-export' | 'before-window'
    days_off: float


@dataclass(frozen=True)
class SelfDated:
    attachment_id: int
    memo_id: int | None
    borrowed: datetime
    source: str
    instant: datetime | None
    civil: str | None


@dataclass
class AuditReport:
    sibling_dated: int = 0
    location_only: int = 0
    unreadable: int = 0
    refused: list[Refusal] = field(default_factory=list)
    self_dated: list[SelfDated] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            "backfill_sibling_metadata — AUDIT (read-only, nothing written)",
            f"  rows dated by a sibling: {self.sibling_dated}",
            f"  refused by the plausibility window (MEMNG-843): {len(self.refused)}",
            *(
                f"    attachment {r.attachment_id} memo {r.memo_id}: borrowed"
                f" {r.borrowed.astimezone(UTC).isoformat()} — {r.reason} by {r.days_off:.1f} days"
                for r in self.refused
            ),
            f"  dated by their own file now (MEMNG-844): {len(self.self_dated)}",
            *(
                f"    attachment {d.attachment_id} memo {d.memo_id}: {d.source}"
                f" {d.instant.isoformat() if d.instant else f'{d.civil} (civil)'}"
                f" — borrowed {d.borrowed.astimezone(UTC).isoformat()}"
                for d in self.self_dated
            ),
            f"  originals not readable here (own metadata not checked): {self.unreadable}",
            f"  rows with only a borrowed location (no candidate date kept): {self.location_only}",
        ]
        return "\n".join(lines)


def _refusal(row) -> Refusal | None:
    existed_by, posted_at = sibling.time_anchors(
        row.captured_at, row.created_at, row.memo_created_at
    )
    taken = row.capture_instant
    if sibling.plausible(taken, existed_by=existed_by, posted_at=posted_at):
        return None
    ceiling = existed_by + sibling.CAPTURE_SKEW
    if taken > ceiling:
        return Refusal(row.id, row.memo_id, taken, "after-export", (taken - ceiling) / DAY)
    floor = posted_at - sibling.SIBLING_WINDOW
    return Refusal(row.id, row.memo_id, taken, "before-window", (floor - taken) / DAY)


def _own_capture(storage_path: str | None, mime: str) -> capture.CaptureResult | None:
    """What the original's own metadata says, or None when there is no original to ask — which
    `read_capture` alone would report as a file that says nothing."""
    if not storage_path or not Path(storage_path).is_file():
        return None
    return capture.read_capture(storage_path, mime)


async def audit() -> AuditReport:
    report = AuditReport()
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    Attachment.id,
                    Attachment.memo_id,
                    Attachment.capture_instant,
                    Attachment.captured_at,
                    Attachment.created_at,
                    Attachment.storage_path,
                    Attachment.mime,
                    Memo.created_at.label("memo_created_at"),
                )
                .outerjoin(Memo, Memo.id == Attachment.memo_id)
                .where(Attachment.capture_source == sibling.SOURCE_SIBLING)
                .order_by(Attachment.id)
            )
        ).all()
        report.sibling_dated = len(rows)
        report.refused = [r for r in map(_refusal, rows) if r is not None]
        for row in rows:
            own = await to_thread.run_sync(_own_capture, row.storage_path, row.mime)
            if own is None:
                report.unreadable += 1
            elif own.capture_source is not None:
                report.self_dated.append(
                    SelfDated(
                        row.id,
                        row.memo_id,
                        row.capture_instant,
                        own.capture_source,
                        own.capture_instant,
                        own.capture_civil,
                    )
                )
        report.location_only = (
            await db.execute(
                select(func.count(Attachment.id)).where(
                    Attachment.location_source == sibling.SOURCE_SIBLING,
                    or_(
                        Attachment.capture_source.is_(None),
                        Attachment.capture_source != sibling.SOURCE_SIBLING,
                    ),
                )
            )
        ).scalar_one()
        await db.rollback()
    return report


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Enqueue img-manager sibling lookups for undated or unlocated photos."
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="actually enqueue; the default is a dry run that selects and reports only",
    )
    ap.add_argument("--limit", type=int, default=None, help="enqueue at most N attachments")
    which = ap.add_mutually_exclusive_group()
    which.add_argument(
        "--retry-failed",
        action="store_true",
        help="ONLY rows whose latest sibling job failed (img-manager was unavailable)",
    )
    which.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="ONLY rows recorded as ambiguous (after a change of the election rule)",
    )
    which.add_argument(
        "--audit",
        action="store_true",
        help="READ-ONLY: list the sibling-borrowed dates the plausibility rule would refuse",
    )
    args = ap.parse_args()
    if args.audit:
        if args.execute or args.limit is not None:
            ap.error("--audit is read-only and takes neither --execute nor --limit")
        print(asyncio.run(audit()).format())
        return
    if args.execute and not settings.sibling_enabled:
        # Every job enqueued now could only fail: the worker re-queues a disabled lookup.
        raise SystemExit("IMGMANAGER_URL, IMGMANAGER_TOKEN and SIBLING_OWNER_IDS are not all set")
    population = (
        Population.retry_failed
        if args.retry_failed
        else Population.include_ambiguous
        if args.include_ambiguous
        else Population.default
    )
    report = asyncio.run(run(population, execute=args.execute, limit=args.limit))
    print(report.format())


if __name__ == "__main__":  # pragma: no cover
    main()
