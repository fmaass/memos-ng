"""The per-language-group delivery ledger's two write rules (MEMNG-1245).

1. **An accepted group's memos are written to BOTH ledgers.** `digest_group_deliveries` is what
   selection reads; `digest_deliveries` (one row per memo, `ON CONFLICT (memo_id) DO NOTHING`) is
   what the PREVIOUS image reads, so after a rollback it sees every memo any group has had and
   re-sends nothing.

2. **A group that comes alive inherits the history.** When a language goes from zero enabled
   recipients to one or more, every memo with a ledger row in ANY language gets a row for that
   language, in the same transaction as the recipient write. Without it the first run after
   "move the aunt to Spanish" would mail her the household's entire history — and, past
   `max_memos_per_run`, fail every morning instead. A recipient joining a group that is already
   active inherits nothing: the group's own ledger already says what it has had.
"""

from collections.abc import Iterable

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

_RECORD_GROUP = sa_text(
    "INSERT INTO digest_group_deliveries (memo_id, language, run_id, part_index, incomplete) "
    "VALUES (:memo_id, :language, :run_id, :part_index, :incomplete) "
    "ON CONFLICT (memo_id, language) DO NOTHING"
)
# The previous image's ledger, in the previous image's own insert shape.
_RECORD_LEGACY = sa_text(
    "INSERT INTO digest_deliveries (memo_id, run_id, part_index, incomplete) "
    "VALUES (:memo_id, :run_id, :part_index, :incomplete) "
    "ON CONFLICT (memo_id) DO NOTHING"
)
# One source row per memo (its first delivery), so the inherited row names a real run.
_SEED = sa_text(
    "INSERT INTO digest_group_deliveries "
    "(memo_id, language, run_id, sent_at, part_index, incomplete) "
    "SELECT DISTINCT ON (memo_id) memo_id, :language, run_id, sent_at, part_index, incomplete "
    "FROM digest_group_deliveries ORDER BY memo_id, sent_at, language "
    "ON CONFLICT (memo_id, language) DO NOTHING"
)


async def record(
    db: AsyncSession,
    *,
    run_id: int,
    language: str,
    part_index: int,
    memo_ids: Iterable[int],
    incomplete_ids: set[int],
) -> None:
    """Rule 1, inside the caller's transaction."""
    for memo_id in memo_ids:
        values = {
            "memo_id": memo_id,
            "language": language,
            "run_id": run_id,
            "part_index": part_index,
            "incomplete": memo_id in incomplete_ids,
        }
        await db.execute(_RECORD_GROUP, values)
        await db.execute(_RECORD_LEGACY, values)


async def active_languages(db: AsyncSession) -> set[str]:
    rows = await db.execute(
        sa_text("SELECT DISTINCT language FROM digest_recipients WHERE enabled IS TRUE")
    )
    return {row[0] for row in rows}


async def seed_languages(db: AsyncSession, languages: Iterable[str]) -> int:
    seeded = 0
    for language in sorted(set(languages)):
        result = await db.execute(_SEED, {"language": language})
        seeded += getattr(result, "rowcount", 0) or 0
    return seeded


async def seed_newly_active(db: AsyncSession, before: set[str]) -> int:
    """Rule 2: call with `active_languages` read BEFORE the recipient write, after the write is
    flushed and before the commit. Seeds exactly the languages that just came alive."""
    return await seed_languages(db, await active_languages(db) - before)
