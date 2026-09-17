"""One-shot backfill: the coordinates the existing library's originals already carry.

Every attachment uploaded before Phase 1 has `location_source IS NULL` — never read. This reads
each original's header (never its pixels, never its `mdat`) and stores what it says. It writes
nothing else and re-derives nothing: the derived tiers on disk are untouched, so the run costs one
header read per file and no transcode.

`location_source` is the resume ledger, which is why a file that says nothing still gets a value:
`'none'` means read and silent, `'unreadable'` means the read failed (missing original, timeout,
parse error) and is the only state `--retry-unreadable` picks up again. A killed run therefore
resumes where it stopped, having cost at most one re-read.

Two safety properties are worth stating because the rehearsal depends on them:

* **The dry run names no location column.** The release train rehearses it against the PRE-migration
  production database, so the dry-run SELECT lists only columns that existed before this migration.
* **The thermal gate reads the EXECUTING host's sysfs** (jupiter's, via `media.too_hot`) and
  silently permits work when there is no thermal zone to read. Saturn — which serves the originals
  over NFS and is the host a storm of reads would actually heat — is protected by the header-only
  reads and by `--sleep`, and the bytes-read counter in the summary is the evidence that the reads
  really are header-sized.

No coordinate is ever printed or logged: the summary carries counts, sources, bytes and a ranking
of 1° cells whose identities are withheld. `--regions-out` writes those cells as GeoJSON, which is
the one artefact that does carry them — it is the basemap extraction's input, not a report.

    docker exec memos-ng-app python -m app.scripts.backfill_location                     # dry run
    docker exec memos-ng-app python -m app.scripts.backfill_location --execute --limit 1
    docker exec memos-ng-app python -m app.scripts.backfill_location --execute --sleep 0.2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from anyio import to_thread
from app.database import async_session
from app.models import Attachment, AttachmentStatus
from app.services import location
from app.services.media import too_hot
from sqlalchemy import Select, or_, select, update

logger = structlog.get_logger(__name__)

# A dead NFS mount fails every read at once. Without a stop, one such run would rewrite the whole
# library to 'unreadable' in seconds and the retry flag would be the only way back.
MAX_CONSECUTIVE_FAILURES = 20

# How long a hot host is given to cool before the row is reconsidered.
THERMAL_SLEEP_S = 30.0

CELL_DEGREES = 1
CELL_BUFFER_DEGREES = 0.5
TOP_CELLS = 10


@dataclass(frozen=True)
class Candidate:
    """One attachment to read — the pre-migration columns only, so the dry run can load it too."""

    id: int
    uid: str
    mime: str
    storage_path: str


@dataclass
class BackfillStats:
    candidates: int = 0
    processed: int = 0
    located: int = 0
    failed: int = 0
    deferred: int = 0
    bytes_read: int = 0
    stopped_after_failures: bool = False
    sources: Counter[str] = field(default_factory=Counter)
    cells: Counter[tuple[int, int]] = field(default_factory=Counter)

    def format(self, *, execute: bool) -> str:
        sources = " ".join(f"{name}={count}" for name, count in sorted(self.sources.items()))
        ranking = "\n".join(
            f"  #{rank} {count} pins"
            for rank, (_cell, count) in enumerate(self.cells.most_common(TOP_CELLS), start=1)
        )
        return (
            f"{'WROTE' if execute else 'DRY RUN'}: candidates={self.candidates} "
            f"processed={self.processed} located={self.located} failed={self.failed} "
            f"deferred={self.deferred}\n"
            f"sources: {sources or '-'}\n"
            f"bytes_read={self.bytes_read}\n"
            f"stopped_after_failures={self.stopped_after_failures}\n"
            f"pins per 1-degree cell, top cells\n"
            f"{ranking or '  -'}"
        )


def exit_code(stats: BackfillStats) -> int:
    """1 when the run gave up on a storm of read failures — look before retrying."""
    return 1 if stats.stopped_after_failures else 0


def candidate_statement(
    *, execute: bool, retry_unreadable: bool = False, limit: int | None = None
) -> Select[tuple[int, str, str, str | None]]:
    """The attachments to read, oldest first.

    In dry run the WHERE clause is deliberately blind to what has already been read: naming
    `location_source` would make the pre-migration rehearsal impossible, and a dry run that reads
    a file twice costs only the read.
    """
    stmt = select(Attachment.id, Attachment.uid, Attachment.mime, Attachment.storage_path).where(
        Attachment.status == AttachmentStatus.ready,
        Attachment.storage_path.is_not(None),
        or_(Attachment.mime.startswith("image/"), Attachment.mime.startswith("video/")),
    )
    if execute:
        already_read = Attachment.location_source.is_(None)
        if retry_unreadable:
            already_read = or_(
                already_read, Attachment.location_source == location.SOURCE_UNREADABLE
            )
        stmt = stmt.where(already_read)
    stmt = stmt.order_by(Attachment.id)
    return stmt if limit is None else stmt.limit(limit)


async def load_candidates(
    *, execute: bool, retry_unreadable: bool = False, limit: int | None = None
) -> list[Candidate]:
    """Read the worklist up front and RELEASE the session — the file loop holds no connection.

    The loop that follows is minutes to hours of NFS reads. A session held across it would sit idle
    on a pooled connection long enough for the server to reap it, and the first write after that
    would fail on a connection the pool still believed in.
    """
    async with async_session() as db:
        rows = await db.execute(
            candidate_statement(execute=execute, retry_unreadable=retry_unreadable, limit=limit)
        )
        # `storage_path` is nullable in the model and the statement already excludes the NULLs;
        # the comprehension's guard is what tells the type checker so.
        return [
            Candidate(id=id_, uid=uid, mime=mime, storage_path=path)
            for id_, uid, mime, path in rows.all()
            if path is not None
        ]


async def _store(candidate: Candidate, found: location.LocationResult | None, source: str) -> None:
    """One row, one transaction — a killed run keeps every row it has already read."""
    async with async_session() as db:
        await db.execute(
            update(Attachment)
            .where(Attachment.id == candidate.id)
            .values(
                latitude=found.latitude if found else None,
                longitude=found.longitude if found else None,
                location_source=source,
            )
        )
        await db.commit()


async def run_backfill(
    candidates: list[Candidate], *, execute: bool, sleep: float = 0.0
) -> BackfillStats:
    stats = BackfillStats(candidates=len(candidates))
    consecutive_failures = 0
    for candidate in candidates:
        # blocking-ok: a sysfs thermal-zone read, not the data volume.
        while too_hot():
            stats.deferred += 1
            await asyncio.sleep(THERMAL_SLEEP_S)

        stats.processed += 1
        found: location.LocationResult | None = None
        with location.measuring() as meter:
            try:
                found = await to_thread.run_sync(
                    location.read_location, candidate.storage_path, candidate.mime
                )
            except Exception as exc:
                source = location.SOURCE_UNREADABLE
                stats.failed += 1
                consecutive_failures += 1
                logger.warning(
                    "backfill_location.unreadable",
                    uid=candidate.uid,
                    mime=candidate.mime,
                    error=type(exc).__name__,
                )
            else:
                source = found.source if found else location.SOURCE_NONE
                consecutive_failures = 0
        stats.bytes_read += meter.bytes_read
        stats.sources[source] += 1
        # Counts and sources only: the value this run just read is the one thing a log may not
        # carry, because logs leave the machine the database never does.
        logger.info(
            "backfill_location.read",
            uid=candidate.uid,
            mime=candidate.mime,
            source=source,
            located=found is not None,
        )
        if found is not None:
            stats.located += 1
            stats.cells[cell_of(found)] += 1

        if execute:
            await _store(candidate, found, source)

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            stats.stopped_after_failures = True
            logger.error("backfill_location.read_storm", failures=consecutive_failures)
            break
        if sleep:
            await asyncio.sleep(sleep)
    return stats


def cell_of(found: location.LocationResult) -> tuple[int, int]:
    """The 1° cell a pin falls in, as (latitude, longitude) of its south-west corner."""
    return (
        math.floor(found.latitude / CELL_DEGREES) * CELL_DEGREES,
        math.floor(found.longitude / CELL_DEGREES) * CELL_DEGREES,
    )


def regions_geojson(stats: BackfillStats) -> dict:
    """The pin cells as one buffered MultiPolygon — the basemap extraction's region input.

    Buffered by half a cell so a pin sitting on a cell edge still has detail tiles around it.
    """
    polygons = []
    for lat, lon in sorted(stats.cells):
        west, east = lon - CELL_BUFFER_DEGREES, lon + CELL_DEGREES + CELL_BUFFER_DEGREES
        south, north = lat - CELL_BUFFER_DEGREES, lat + CELL_DEGREES + CELL_BUFFER_DEGREES
        polygons.append(
            [[[west, south], [east, south], [east, north], [west, north], [west, south]]]
        )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"cells": len(polygons)},
                "geometry": {"type": "MultiPolygon", "coordinates": polygons},
            }
        ],
    }


def write_regions(stats: BackfillStats, path: Path) -> None:
    path.write_text(json.dumps(regions_geojson(stats), indent=2) + "\n")


async def _amain(
    *,
    execute: bool,
    retry_unreadable: bool,
    limit: int | None,
    sleep: float,
    regions_out: Path | None,
) -> BackfillStats:
    candidates = await load_candidates(
        execute=execute, retry_unreadable=retry_unreadable, limit=limit
    )
    stats = await run_backfill(candidates, execute=execute, sleep=sleep)
    if regions_out is not None:
        await to_thread.run_sync(write_regions, stats, regions_out)
    return stats


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Backfill attachment coordinates from the originals' metadata."
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="actually write; the default is a dry run that reads and reports only",
    )
    ap.add_argument("--limit", type=int, default=None, help="read at most N attachments")
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to pause between files (protects the NFS host serving the originals)",
    )
    ap.add_argument(
        "--retry-unreadable",
        action="store_true",
        help="also re-read rows a previous run could not read ('unreadable')",
    )
    ap.add_argument(
        "--regions-out",
        type=Path,
        default=None,
        help="write the GeoJSON of the buffered 1-degree cells holding pins to PATH",
    )
    args = ap.parse_args()
    stats = asyncio.run(
        _amain(
            execute=args.execute,
            retry_unreadable=args.retry_unreadable,
            limit=args.limit,
            sleep=args.sleep,
            regions_out=args.regions_out,
        )
    )
    print(stats.format(execute=args.execute))
    raise SystemExit(exit_code(stats))


if __name__ == "__main__":  # pragma: no cover
    main()
