"""Schema gate for the media worker (MEMNG-86).

`docker compose up -d --force-recreate` recreates the app and worker together, but only the APP
entrypoint runs `alembic upgrade head` (docker-entrypoint.sh) — the worker path just execs
`python -m app.worker`. So on a deploy that adds a migration the worker starts on the NEW image
while the schema is still OLD; entering its loop immediately runs the startup recovery sweep
against a column its code expects but the DB does not yet have (the v1.3.2 deploy:
`UndefinedColumnError: claimed_at` until the app finished migrating). This gate makes the worker
WAIT until the live schema reaches the head its own image was built with before it drains anything.

Design — why polling `alembic_version`, not "run alembic in the worker too":

* **Single migrator, no double-run.** The worker never runs alembic; the app entrypoint stays the
  sole migrator. So the "two containers run `alembic upgrade` at once" race the ticket worries
  about cannot occur — there is only ever one writer, no advisory lock needed.
* **Crash-safe.** The gate is a READ-ONLY poll of `alembic_version`: it holds no locks and writes
  nothing, so a crash while waiting simply restarts and re-polls.
* **Fails safe.** If the schema does not reach the expected head within the budget, the gate RAISES
  (the worker exits nonzero and Docker restarts it) rather than draining against a schema its code
  does not expect. A genuinely broken/slow migration therefore surfaces as a visible restart, not a
  silent UndefinedColumnError swallowed by the loop's boundary guards.

Assumption: the app is present to migrate (the shipped topology is one in-stack worker recreated
atomically with the app — see worker.run). A worker deployed with no migrating app would wait out
the budget and exit-loop, which is the correct visible signal for that misconfiguration.
"""

import asyncio
from pathlib import Path

import structlog
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import async_session

logger = structlog.get_logger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


class SchemaGateTimeout(RuntimeError):
    """The live schema never reached the code's expected head within the gate's budget."""


def known_revisions() -> set[str]:
    """Every revision id THIS image's migration scripts describe.

    A head outside this set was written by a migration this image does not carry, which — since
    revisions only ever move forward — means a NEWER image migrated the database. That is exactly
    the rollback case, and it is the only thing ROLLBACK_MODE lets through.
    """
    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    return {revision.revision for revision in script.walk_revisions()}


def expected_head() -> str:
    """The migration head THIS image was built with (the newest revision in alembic/versions).

    Sync on purpose: it scans the bundled migration scripts (a filesystem walk) and runs once at
    startup, off the request loop entirely (this module is worker-only)."""
    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    head = script.get_current_head()
    if head is None:  # an image with no migrations at all — a packaging error, fail loud
        raise RuntimeError("no alembic head found — alembic/versions is empty or unreadable")
    return head


async def current_db_head() -> str | None:
    """The revision the live DB is migrated to, or None when it cannot be confirmed at head yet.

    None covers both "no `alembic_version` table" (an un-migrated schema) and a transient DB error
    (a connection blip during a deploy) — either way the gate keeps polling rather than crashing on
    the first miss."""
    try:
        async with async_session() as db:
            return (
                await db.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except SQLAlchemyError:
        return None


async def wait_for_migrated_schema(
    *, budget_s: float | None = None, interval: float | None = None
) -> str:
    """Block until the live schema is at this image's expected head; raise SchemaGateTimeout on
    expiry. Returns the head once reached. Budget/interval come from settings unless overridden
    (tests pass short values)."""
    want = expected_head()
    budget = settings.WORKER_SCHEMA_GATE_TIMEOUT_S if budget_s is None else budget_s
    step = settings.WORKER_SCHEMA_GATE_INTERVAL_S if interval is None else interval
    announced_wait = False
    try:
        async with asyncio.timeout(budget):
            while True:
                have = await current_db_head()
                if have == want:
                    if announced_wait:
                        logger.info("worker_schema_gate_cleared", head=want)
                    return want
                if have is not None and settings.ROLLBACK_MODE and have not in known_revisions():
                    # A rollback: the schema is AHEAD of this image, and the expand-only migration
                    # it carries leaves every column this code reads in place. Waiting would pin
                    # the worker off for the whole rollback window for nothing. Announced once,
                    # loudly, because it is the one state in which the gate proves nothing.
                    logger.warning("worker_schema_gate_rollback_mode", have=have, want=want)
                    return have
                if not announced_wait:  # log the wait once, not every poll
                    logger.info("worker_schema_gate_waiting", have=have, want=want, budget_s=budget)
                    announced_wait = True
                await asyncio.sleep(step)
    except TimeoutError as exc:
        raise SchemaGateTimeout(f"schema did not reach {want!r} within {budget}s") from exc
