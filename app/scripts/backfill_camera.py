"""One-shot, resumable backfill: the camera EXIF the existing library's originals already carry.

Every attachment that predates MEMNG-865 has `camera_source IS NULL` — never visited. This reads
each image original's EXIF header (never its pixels) through `services/camera.py`, the same
reader the upload path uses, and stores the eight camera columns AND `camera_source` for every
row it visits. It writes nothing else: no capture, location or derived-tier column is in the
UPDATE.

`camera_source` is the resume ledger, so a visit always leaves a value: `'exif'` = at least one
field read, `'none'` = read and nothing usable, `'unreadable'` = the read failed or timed out (the
only state `--retry-unreadable` picks up again), `'not-image'` = classified by mime and never
read. A killed run resumes where it stopped, having cost at most one re-read.

The rules the production run leans on (fold F2 of the story):

* **The candidates are materialized ONCE**, after the ledger predicate, `--uid`, the id ordering
  and `--limit`; the list's length is the `candidates` count. The population is every attachment
  row — no status, mime or path filter — and a non-image is classified without a read.
* **The dry run** (the default) applies the same predicate, reads and classifies, and issues no
  UPDATE.
* **One row, one transaction, compare-and-set**: the UPDATE re-asserts the ledger predicate, so a
  row another writer settled after the SELECT keeps that writer's answer; the miss is counted in
  `failed`.
* **The counters**: each visited candidate increments exactly one of `exif` / `none` /
  `unreadable` / `not-image`; a write that did not land is NOT an extraction outcome and counts in
  `failed` alone; a run that stops early reports the rest as `unvisited`. The summary asserts
  `exif + none + unreadable + not-image + unvisited == candidates` and
  `failed <= exif + none + unreadable + not-image`, and the run exits 1 when that fails or when
  it stopped on a storm of failures. A run whose counts hold but whose `failed` is not zero exits
  3, so a one-victim run whose single write did not land never reads as a success.

No camera value is ever printed or logged — counts, uids, mimes and outcome classes only.

Production (after the deploy and the schema read-back, MEMNG-866) — the container supplies
DATA_ROOT, the originals mount and DATABASE_URL, so there is no host-side override:

    docker exec memos-ng-app python -m app.scripts.backfill_camera                      # dry run
    docker exec memos-ng-app python -m app.scripts.backfill_camera --execute --uid <uid>
    # exit 0 AND the victim row read back from the database before the full run:
    #   SELECT camera_source, camera_make IS NOT NULL, iso IS NOT NULL
    #     FROM attachments WHERE uid = '<uid>';
    # camera_source must be set and be the class the dry run reported for that row.
    docker exec memos-ng-app python -m app.scripts.backfill_camera --execute
    docker exec memos-ng-app python -m app.scripts.backfill_camera --execute --retry-unreadable
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from anyio import fail_after, to_thread
from app.database import async_session
from app.logging_privacy import configure_logging
from app.models import Attachment
from app.services import camera
from app.services.media import too_hot
from app.services.uploads import CAMERA_READ_TIMEOUT_S
from sqlalchemy import ColumnElement, Select, select, update
from sqlalchemy.exc import SQLAlchemyError

configure_logging()
logger = structlog.get_logger(__name__)

# A dead NFS mount fails every read at once; without a stop one run would settle the whole library
# as 'unreadable' in seconds (the location backfill's number).
MAX_CONSECUTIVE_FAILURES = 20

# How long a hot host is given to cool before the row is reconsidered.
THERMAL_SLEEP_S = 30.0

# The upload path's deadline: a stalled NFS read raises nothing, so without it `run_sync` waits
# forever and neither the per-row commit nor the stop above ever runs.
READ_TIMEOUT_S = CAMERA_READ_TIMEOUT_S

type Reader = Callable[[str, str], camera.CameraRead]


@dataclass(frozen=True)
class Candidate:
    id: int
    uid: str
    mime: str
    storage_path: str | None
    # The ledger value the row was SELECTED with. The write is conditional on it still being that,
    # so the compare-and-set travels with the candidate and no caller can pair a worklist with the
    # wrong predicate.
    camera_source: str | None


@dataclass
class BackfillStats:
    candidates: int = 0
    exif: int = 0
    none: int = 0
    unreadable: int = 0
    not_image: int = 0
    unvisited: int = 0
    failed: int = 0  # orthogonal: visited rows whose write did not land
    deferred: int = 0  # thermal waits, not rows
    stopped_after_failures: bool = False

    @property
    def visited(self) -> int:
        return self.exif + self.none + self.unreadable + self.not_image

    def count(self, source: str) -> None:
        if source == camera.SOURCE_EXIF:
            self.exif += 1
        elif source == camera.SOURCE_NONE:
            self.none += 1
        elif source == camera.SOURCE_UNREADABLE:
            self.unreadable += 1
        else:
            self.not_image += 1

    def partition_holds(self) -> bool:
        return self.visited + self.unvisited == self.candidates and self.failed <= self.visited

    def format(self, *, execute: bool) -> str:
        return (
            f"{'WROTE' if execute else 'DRY RUN'}: candidates={self.candidates} "
            f"exif={self.exif} none={self.none} unreadable={self.unreadable} "
            f"not-image={self.not_image} unvisited={self.unvisited} failed={self.failed}\n"
            f"partition={'ok' if self.partition_holds() else 'BROKEN'} "
            f"deferred={self.deferred} stopped_after_failures={self.stopped_after_failures}"
        )


EXIT_OK = 0
EXIT_BROKEN = 1  # the counters do not partition the candidates, or the run gave up on a storm
# Not 2: argparse exits 2 on a usage error, and the two must never be mistaken for each other.
EXIT_WRITES_FAILED = 3


def exit_code(stats: BackfillStats) -> int:
    """`EXIT_BROKEN` before `EXIT_WRITES_FAILED`: a broken count is the louder of the two."""
    if not stats.partition_holds() or stats.stopped_after_failures:
        return EXIT_BROKEN
    if stats.failed:
        return EXIT_WRITES_FAILED
    return EXIT_OK


def ledger_predicate(*, retry_unreadable: bool) -> ColumnElement[bool]:
    """Which rows a run is for: never visited, or (`--retry-unreadable`) ONLY the failed reads."""
    if retry_unreadable:
        return Attachment.camera_source == camera.SOURCE_UNREADABLE
    return Attachment.camera_source.is_(None)


def candidate_statement(
    *, retry_unreadable: bool = False, uid: str | None = None, limit: int | None = None
) -> Select[tuple[int, str, str, str | None, str | None]]:
    stmt = select(
        Attachment.id,
        Attachment.uid,
        Attachment.mime,
        Attachment.storage_path,
        Attachment.camera_source,
    ).where(ledger_predicate(retry_unreadable=retry_unreadable))
    if uid is not None:
        stmt = stmt.where(Attachment.uid == uid)
    stmt = stmt.order_by(Attachment.id)
    return stmt if limit is None else stmt.limit(limit)


async def load_candidates(
    *, retry_unreadable: bool = False, uid: str | None = None, limit: int | None = None
) -> list[Candidate]:
    """The worklist, read ONCE up front, and the session RELEASED: the file loop holds no
    connection across minutes of NFS reads."""
    async with async_session() as db:
        rows = await db.execute(
            candidate_statement(retry_unreadable=retry_unreadable, uid=uid, limit=limit)
        )
        return [
            Candidate(id=id_, uid=uid_, mime=mime, storage_path=path, camera_source=selected)
            for id_, uid_, mime, path, selected in rows.all()
        ]


async def _store(candidate: Candidate, found: camera.CameraInfo | None, source: str) -> bool:
    """One row, one transaction, the eight camera columns and the ledger — and only if the row is
    still what this run selected it as. False when the write did not land (an error, or a row
    another writer settled in between)."""
    try:
        async with async_session() as db:
            result = await db.execute(
                update(Attachment)
                .where(
                    Attachment.id == candidate.id,
                    Attachment.camera_source.is_not_distinct_from(candidate.camera_source),
                )
                .values(**camera.columns(found), camera_source=source)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except SQLAlchemyError as exc:
        logger.warning("backfill_camera.write_failed", uid=candidate.uid, error=type(exc).__name__)
        return False
    if getattr(result, "rowcount", 0) != 1:
        logger.warning("backfill_camera.superseded", uid=candidate.uid)
        return False
    return True


async def _visit(candidate: Candidate, reader: Reader) -> tuple[camera.CameraInfo | None, str]:
    """One candidate's outcome. A non-image is classified without a read, a path-less image is
    unreadable without one, and a stall or an exception is unreadable too."""
    if not camera.is_image(candidate.mime):
        return None, camera.SOURCE_NOT_IMAGE
    if candidate.storage_path is None:
        return None, camera.SOURCE_UNREADABLE
    try:
        with fail_after(READ_TIMEOUT_S):
            found = await to_thread.run_sync(
                reader, candidate.storage_path, candidate.mime, abandon_on_cancel=True
            )
    except TimeoutError:
        logger.warning("backfill_camera.read_timeout", uid=candidate.uid, mime=candidate.mime)
        return None, camera.SOURCE_UNREADABLE
    except Exception as exc:  # noqa: BLE001 — a reader failure is an outcome, not a crash
        logger.warning(
            "backfill_camera.unreadable",
            uid=candidate.uid,
            mime=candidate.mime,
            error=type(exc).__name__,
        )
        return None, camera.SOURCE_UNREADABLE
    source = camera.source_of(candidate.mime, found)
    return (found if isinstance(found, camera.CameraInfo) else None), source


async def run_backfill(
    candidates: list[Candidate],
    *,
    execute: bool,
    sleep: float = 0.0,
    reader: Reader = camera.read_camera,
) -> BackfillStats:
    stats = BackfillStats(candidates=len(candidates))
    consecutive_failures = 0
    for candidate in candidates:
        # blocking-ok: a sysfs thermal-zone read, not the data volume.
        while too_hot():
            stats.deferred += 1
            await asyncio.sleep(THERMAL_SLEEP_S)

        found, source = await _visit(candidate, reader)
        stats.count(source)
        logger.info("backfill_camera.read", uid=candidate.uid, mime=candidate.mime, source=source)
        row_failed = source == camera.SOURCE_UNREADABLE
        if execute and not await _store(candidate, found, source):
            stats.failed += 1
            row_failed = True
        consecutive_failures = consecutive_failures + 1 if row_failed else 0

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            stats.stopped_after_failures = True
            logger.error("backfill_camera.failure_storm", failures=consecutive_failures)
            break
        if sleep:
            await asyncio.sleep(sleep)
    stats.unvisited = stats.candidates - stats.visited
    return stats


async def _amain(
    *, execute: bool, retry_unreadable: bool, uid: str | None, limit: int | None, sleep: float
) -> BackfillStats:
    candidates = await load_candidates(retry_unreadable=retry_unreadable, uid=uid, limit=limit)
    return await run_backfill(candidates, execute=execute, sleep=sleep)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Backfill attachment camera EXIF from the originals' metadata."
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="actually write; the default is a dry run that reads, classifies and reports only",
    )
    ap.add_argument("--limit", type=int, default=None, help="visit at most N attachments")
    ap.add_argument("--uid", default=None, help="visit only this attachment (the one-victim run)")
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to pause between files (protects the NFS host serving the originals)",
    )
    ap.add_argument(
        "--retry-unreadable",
        action="store_true",
        help="visit ONLY rows a previous run could not read ('unreadable')",
    )
    args = ap.parse_args(argv)
    stats = asyncio.run(
        _amain(
            execute=args.execute,
            retry_unreadable=args.retry_unreadable,
            uid=args.uid,
            limit=args.limit,
            sleep=args.sleep,
        )
    )
    print(stats.format(execute=args.execute))
    return exit_code(stats)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
