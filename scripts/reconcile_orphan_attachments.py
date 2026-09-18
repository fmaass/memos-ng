#!/usr/bin/env python3
"""Reclaim the attachments that memo deletion left behind before MEMNG-170.

`delete_memo` used to delete the memo and nothing else: the `attachments` relationship carries no
cascade and the FK is ON DELETE SET NULL, so every attachment of a deleted memo survived with
`memo_id` NULL, kept appearing in the media library, and kept its files on disk for ever. The code
fix stops new ones; this script cleans up the rows already stranded.

WHICH ROWS. `memo_id IS NULL` on its own is the wrong predicate — this instance also holds
intentional orphans from the 0.29.1 migration, media that never belonged to a memo. The two are
told apart by where the original lives: only `link_attachments` writes a file under
`originals/YYYY/MM/<memo-folder>/`, and it does that only for an attachment being linked to a memo.
So a NULL `memo_id` on a row whose original sits in a dated memo folder means that memo is gone.
A migration-era orphan sits in the FLAT `originals/` bucket and never matches.

HOW TO RUN IT. Dry run first (the default: it prints and changes nothing), then ONE victim with a
read-back, and only then the batch:

    export DATABASE_URL_SYNC=postgresql+psycopg2://USER@HOST:PORT/DB
    uv run python scripts/reconcile_orphan_attachments.py
    uv run python scripts/reconcile_orphan_attachments.py --victim 123
    uv run python scripts/reconcile_orphan_attachments.py --apply

`DATABASE_URL_SYNC` must be set explicitly — the app's config carries a development default, and a
maintenance script that deletes rows must never fall back to "whatever database was handy".
`DATA_ROOT` must point at the same data volume the app serves, or the files will not be found
(the run says so per row rather than deleting a row whose bytes it could not see).
"""

import argparse
import os
import sys
from pathlib import Path

from app.models import Attachment

# The predicate, the path set and the DELETE ORDER all come from the service — this script must
# not carry a second opinion about which rows are garbage or how they are taken apart.
from app.services.asset_paths import all_asset_paths
from app.services.reclaim import Revalidate, leaked_orphans_stmt, reclaim_attachment_sync
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


def leaked_orphans(session: Session) -> list[Attachment]:
    return list(session.execute(leaked_orphans_stmt()).scalars().all())


def describe(att: Attachment) -> str:
    """What this row owns, as it looks right now. A dry run reports only what is ON DISK, because
    that is what a human is deciding about; the reclaim itself unlinks the whole derivable set."""
    files = [path for path in all_asset_paths(att) if path.exists()]
    total = sum(path.stat().st_size for path in files)
    head = f"  id={att.id} uid={att.uid} {att.mime} {att.filename!r} row_size={att.size}"
    body = "\n".join(f"      {path}  ({path.stat().st_size} bytes)" for path in files)
    return f"{head}\n      on disk: {len(files)} file(s), {total} bytes\n{body}".rstrip()


def reclaim_one(session: Session, att_id: int) -> str:
    """Reclaim one row through the shared seam and say what happened, in one line.

    The row is re-selected FOR UPDATE and revalidated inside that transaction, so a row a
    concurrent upload has dedup-claimed since this run listed it is SKIPPED rather than deleted —
    the difference between tidying garbage and destroying a live memo's photo.
    """
    result = reclaim_attachment_sync(session, att_id, Revalidate.LEAKED_ORPHAN)
    if result.outcome == "skipped_ineligible":
        print(f"      SKIPPED id={att_id}: no longer a leaked orphan (re-linked, or moved)")
    elif result.outcome == "vanished":
        print(f"      SKIPPED id={att_id}: already gone")
    elif result.outcome == "failed_unlink":
        print(
            f"      FAILED id={att_id}: could not remove {len(result.failed_paths)} file(s) — the "
            f"row was KEPT so they stay findable: {list(result.failed_paths)}",
            file=sys.stderr,
        )
    return result.outcome


def read_back(session: Session, att_id: int, paths: list[Path]) -> bool:
    """Prove the reclaim: the row is gone from the database and so are its files."""
    row = session.execute(select(Attachment.id).where(Attachment.id == att_id)).scalar_one_or_none()
    left = [path for path in paths if path.exists()]
    print(f"      read-back: row={'GONE' if row is None else f'STILL THERE ({row})'}", end="")
    print(f"  files_left={len(left)}" + ("" if not left else f" {left}"))
    return row is None and not left


def not_reclaimed(victim: int, reason: str, code: int) -> int:
    """Say on stderr that the victim is still there, and hand back a nonzero code.

    A `--victim` run is the proof step between a dry run and `--apply`, so its caller acts on the
    exit code: every outcome in which the named row was NOT reclaimed has to be one of these, or a
    run that reclaimed nothing reads as success.
    """
    print(f"\nvictim id {victim} was NOT reclaimed: {reason}", file=sys.stderr)
    return code


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL_SYNC")
    if not url:
        sys.exit(
            "DATABASE_URL_SYNC is not set. This script deletes rows: point it at a database "
            "explicitly rather than letting the app config's development default answer."
        )
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--victim",
        type=int,
        metavar="ID",
        help="reclaim exactly this attachment id, with a "
        "row-and-file read-back — run this before --apply",
    )
    mode.add_argument("--apply", action="store_true", help="reclaim every listed orphan")
    args = parser.parse_args(argv)

    engine = create_engine(_database_url())
    try:
        with Session(engine) as session:
            found = leaked_orphans(session)
            print(f"{len(found)} leaked orphan attachment(s) under a dated memo folder:")
            for att in found:
                print(describe(att))
            if not found:
                if args.victim is not None:
                    return not_reclaimed(
                        args.victim, "nothing here is a leaked orphan — the list is empty", 2
                    )
                return 0

            if args.victim is not None:
                chosen = next((a for a in found if a.id == args.victim), None)
                if chosen is None:
                    return not_reclaimed(
                        args.victim, "it is not in the list above — refusing to touch it", 2
                    )
                # Read the id and the path set BEFORE the reclaim: it commits, which expires every
                # instance, and re-reading a column of a row that is now gone raises rather than
                # answering — the reason a report about it has to be built from values held here.
                victim_id, paths = chosen.id, all_asset_paths(chosen)
                print(f"\nreclaiming ONE victim: id={victim_id}")
                outcome = reclaim_one(session, victim_id)
                if outcome == "failed_unlink":
                    # The row was kept on purpose; its files are still on disk.
                    return not_reclaimed(victim_id, "its files could not be removed", 1)
                if outcome != "reclaimed":
                    # Nothing was touched; the row is not this run's to take.
                    return not_reclaimed(victim_id, f"the reclaim {outcome}", 3)
                return 0 if read_back(session, victim_id, paths) else 1

            if args.apply:
                print(f"\nreclaiming all {len(found)}:")
                failures = skipped = 0
                for att in found:
                    print(f"  id={att.id}")
                    # Read the path set BEFORE the reclaim: the row is expired once it is deleted,
                    # and the read-back needs to know what should be gone. The reclaim itself
                    # re-reads it under its own lock — this copy is only for the report.
                    paths = all_asset_paths(att)
                    outcome = reclaim_one(session, att.id)
                    if outcome == "failed_unlink":
                        failures += 1  # its files are still there and so is its row
                        continue
                    if outcome != "reclaimed":
                        skipped += 1  # locked+revalidated away from us; not a failure
                        continue
                    if not read_back(session, att.id, paths):
                        failures += 1
                print(
                    f"\nreclaimed={len(found) - skipped - failures} skipped={skipped} "
                    f"failed={failures}"
                )
                return 1 if failures else 0

            print("\nDRY RUN — nothing was changed. Re-run with --victim <id>, then --apply.")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
