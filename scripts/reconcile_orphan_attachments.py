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

from app.models import Attachment, MediaJob

# The tier map lives in exactly one place; a copy here would drift the moment a tier is added.
from app.routers.assets import all_asset_paths
from sqlalchemy import create_engine, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session

# A dated memo folder: `originals/YYYY/MM/…`. Deliberately NOT anchored — the stored path is
# absolute and its prefix is the deployment's DATA_ROOT.
LEAKED_PATH_RE = r"originals/[0-9]{4}/[0-9]{2}/"


def leaked_orphans_stmt():
    """The attachments a deleted memo left behind, and only those — see the module docstring."""
    return (
        select(Attachment)
        .where(Attachment.memo_id.is_(None), Attachment.storage_path.op("~")(LEAKED_PATH_RE))
        .order_by(Attachment.id)
    )


def leaked_orphans(session: Session) -> list[Attachment]:
    return list(session.execute(leaked_orphans_stmt()).scalars().all())


def files_of(att: Attachment) -> list[Path]:
    """Every file this attachment owns that is actually on disk right now."""
    return [path for path in all_asset_paths(att) if path.exists()]


def describe(att: Attachment) -> str:
    files = files_of(att)
    total = sum(path.stat().st_size for path in files)
    head = f"  id={att.id} uid={att.uid} {att.mime} {att.filename!r} row_size={att.size}"
    body = "\n".join(f"      {path}  ({path.stat().st_size} bytes)" for path in files)
    return f"{head}\n      on disk: {len(files)} file(s), {total} bytes\n{body}".rstrip()


def reclaim(session: Session, att: Attachment) -> tuple[int, list[Path]]:
    """Delete one orphan's jobs and row, commit, then unlink its files. Returns (id, paths).

    Rows first and files after the commit, the same order the app's own deletes use: a worker that
    publishes a derived tier while this runs publishes it BEFORE the row disappears, so the sweep
    below still covers it, and nothing it writes afterwards survives.
    """
    att_id = att.id
    paths = files_of(att)
    unlocked = (
        select(MediaJob.id)
        .where(MediaJob.attachment_id == att_id)
        .with_for_update(skip_locked=True)
    )
    session.execute(
        sa_delete(MediaJob).where(MediaJob.id.in_(unlocked)).execution_options(
            synchronize_session=False
        )
    )
    session.delete(att)
    session.commit()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # reported, never fatal: the row is already gone
            print(f"      ! could not unlink {path}: {exc}", file=sys.stderr)
    return att_id, paths


def read_back(session: Session, att_id: int, paths: list[Path]) -> bool:
    """Prove the reclaim: the row is gone from the database and so are its files."""
    row = session.execute(select(Attachment.id).where(Attachment.id == att_id)).scalar_one_or_none()
    left = [path for path in paths if path.exists()]
    print(f"      read-back: row={'GONE' if row is None else f'STILL THERE ({row})'}", end="")
    print(f"  files_left={len(left)}" + ("" if not left else f" {left}"))
    return row is None and not left


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
        "--victim", type=int, metavar="ID", help="reclaim exactly this attachment id, with a "
        "row-and-file read-back — run this before --apply"
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
                return 0

            if args.victim is not None:
                chosen = next((a for a in found if a.id == args.victim), None)
                if chosen is None:
                    print(
                        f"\nid {args.victim} is not in the list above — refusing to touch it.",
                        file=sys.stderr,
                    )
                    return 2
                print(f"\nreclaiming ONE victim: id={chosen.id}")
                att_id, paths = reclaim(session, chosen)
                return 0 if read_back(session, att_id, paths) else 1

            if args.apply:
                print(f"\nreclaiming all {len(found)}:")
                failures = 0
                for att in found:
                    print(f"  id={att.id}")
                    att_id, paths = reclaim(session, att)
                    if not read_back(session, att_id, paths):
                        failures += 1
                return 1 if failures else 0

            print("\nDRY RUN — nothing was changed. Re-run with --victim <id>, then --apply.")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
