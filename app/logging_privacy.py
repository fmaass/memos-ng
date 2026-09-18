"""Scrub PostgreSQL's row details wherever an exception becomes a LOG LINE.

`hide_parameters=True` (app/database.py) removes SQLAlchemy's own echo of the bound parameters,
but the server sends the offending row itself inside the error message — `DETAIL:  Failing row
contains (…, 12.5, …)` for a check violation, `DETAIL:  Key (…)=(…)` for a unique one — and no
client setting suppresses that. A guard at a call site (`backfill_location._store`) closes one
path; an exception that escapes FastAPI is rendered by Uvicorn's own stdlib logger, which no call
site controls. The invariant is "no coordinate value in any log line", so the scrub sits at the
boundary where an exception is RENDERED, once, for every path:

* structlog — a processor appended AFTER the renderer, so it sees the finished line including the
  formatted traceback (a chain that renders to a dict is scrubbed value by value);
* stdlib logging — a filter on the root and uvicorn loggers, which pre-renders `exc_info` into
  `record.exc_text` (the Formatter then reuses that text instead of re-formatting) and scrubs the
  message and its args too.

Both call the same pure function, so there is one pattern to review and one to test.
"""

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import structlog

ROW_DETAIL_REDACTION = "DETAIL: [row hidden]"
# From the first `DETAIL:` to the END of the text. Not a line, not "up to the next diagnostic
# field": a value is stored verbatim, so a filename can contain a newline, a `).`, `HINT:`,
# `[SQL:`, or a whole traceback, and NO textual rule can tell the row's contents from the
# server's own fields that follow it. Every boundary this could key on is a string a value may
# spell itself, so the only sound answer is to drop everything after the detail begins.
_DETAIL = re.compile(r"DETAIL:.*\Z", re.DOTALL)


def redact_row_details(text: str) -> str:
    """Replace the detail, and everything after it, with one marker.

    Kept, because it all precedes the detail: the primary message, the constraint name, the
    SQLSTATE, and any traceback frames rendered above it — which is what a reader acts on.

    Deliberately lost with the row: SQLAlchemy's `[SQL: …]` statement echo (whose values
    `hide_parameters=True` has already stripped, so what is lost is the statement text alone) and
    any chained traceback rendered after the detail. That is the price of not trusting a boundary.
    """
    if "DETAIL:" not in text:
        return text
    return _DETAIL.sub(ROW_DETAIL_REDACTION, text, count=1)


def redact_processor(logger: Any, method_name: str, event: Any) -> Any:
    """structlog processor for the END of the chain, where `event` is the rendered line."""
    if isinstance(event, str):
        return redact_row_details(event)
    if isinstance(event, bytes):
        return redact_row_details(event.decode("utf-8", "replace")).encode("utf-8")
    if isinstance(event, Mapping):
        # A chain rendering to a dict (JSONRenderer's input, or no renderer at all) still has to
        # be scrubbed, value by value.
        return {k: redact_row_details(v) if isinstance(v, str) else v for k, v in event.items()}
    return event


def _carries_detail(record: logging.LogRecord) -> bool:
    """Cheap pre-check, so an ordinary record keeps its lazy `%`-formatting untouched."""
    if "DETAIL:" in str(record.msg):
        return True
    args = record.args
    if isinstance(args, Mapping):
        args = tuple(args.values())
    elif not isinstance(args, tuple):
        args = () if args is None else (args,)
    return any("DETAIL:" in str(a) for a in args)


def scrub_record(record: logging.LogRecord) -> None:
    """Take the row out of a stdlib record, message, args, traceback and stack alike.

    The message is RENDERED before it is scrubbed: `logger.error("failed: %s", exc)` keeps the
    exception OBJECT in `record.args`, and `getMessage()` would spell its DETAIL out at format
    time — after any scrub that only looked at strings. With the args emptied afterwards
    `getMessage()` returns the message verbatim, so a `%` left in the rendered text is safe.
    """
    if _carries_detail(record):
        record.msg = redact_row_details(record.getMessage())
        record.args = ()
    if record.exc_info and not record.exc_text:
        rendered = logging.Formatter().formatException(record.exc_info)
        if "DETAIL:" in rendered:
            # Only then: a record whose traceback is clean keeps the handler's own formatting.
            # `Formatter.format` reuses `exc_text` when it is set, which is what lets a scrub
            # outside the formatter reach a traceback at all.
            record.exc_text = redact_row_details(rendered)
    elif record.exc_text:
        record.exc_text = redact_row_details(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_row_details(record.stack_info)


class RowDetailFilter(logging.Filter):
    """The scrub as a filter, for records that reach a handler without passing the factory."""

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        return True


_original_factory: Callable[..., logging.LogRecord] | None = None


def _record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """Scrub at CREATION, which is the only point every record passes.

    A filter on a logger is consulted only for records logged directly to it — never for records
    that propagate up from `app.*`, `sqlalchemy.*` or `alembic` — and a filter on a handler
    misses every handler installed after us (uvicorn configures its handlers before it imports
    the app). The record factory has neither blind spot.
    """
    factory = _original_factory or logging.LogRecord
    record = factory(*args, **kwargs)
    scrub_record(record)
    return record


# Belt for the factory's braces: the loggers Uvicorn logs an escaped request exception through,
# plus the root, in case a record arrives already built (a socket handler, `makeLogRecord`).
_FILTERED_LOGGERS = ("", "uvicorn", "uvicorn.error")


def configure_logging() -> None:
    """Install every hook. Idempotent — importing the app twice must not stack them."""
    global _original_factory
    processors = list(structlog.get_config()["processors"])
    if redact_processor not in processors:
        processors.append(redact_processor)
        structlog.configure(processors=processors)
    if _original_factory is None:
        _original_factory = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_record_factory)
    for name in _FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RowDetailFilter) for f in logger.filters):
            logger.addFilter(RowDetailFilter())
