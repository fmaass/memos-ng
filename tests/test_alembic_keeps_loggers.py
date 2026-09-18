"""alembic's env.py runs inside whatever process asks for a migration — the app included — so it
must leave the loggers that process already created alone. `fileConfig` disables every one of
them by default, and the privacy filters in app/logging_privacy.py hang off exactly such loggers."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_logger_created_before_env_py_still_emits_after_it(db_engine) -> None:
    # db_engine, not for its tables: it is what hands env.py the suite's DSN when none is exported.
    # Named outside every logger alembic.ini configures (root, sqlalchemy, alembic), so the only
    # thing fileConfig can do to it is the disable this test is about.
    logger = logging.getLogger("memosng_created_before_alembic")
    handler = _Collect()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        command.current(cfg)  # read-only, and it loads env.py the way every alembic command does

        logger.info("still here")

        assert logger.disabled is False
        assert handler.messages == ["still here"]
    finally:
        logger.removeHandler(handler)
        logger.disabled = False
