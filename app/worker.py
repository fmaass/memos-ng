"""memos-ng media worker entrypoint.

Same image as the app, started with `command: worker`. The queue claim loop
(`SELECT … FOR UPDATE SKIP LOCKED` on media_jobs), fast lane (pyvips) and slow
lane (ffmpeg) land in Phase 2 (T2.2/T2.3). This stub keeps the container alive
and idle on an empty queue so T0.4's "worker idles on empty queue" AC holds.
"""

import asyncio

import structlog

from app.version import __version__

logger = structlog.get_logger(__name__)


async def run() -> None:
    logger.info("worker_started", version=__version__)
    # Phase 2 replaces this idle wait with the media_jobs claim loop + LISTEN/NOTIFY.
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
