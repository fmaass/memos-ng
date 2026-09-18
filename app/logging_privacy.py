"""Scrub PostgreSQL's row details wherever an exception becomes a LOG LINE.

`hide_parameters=True` (app/database.py) removes SQLAlchemy's own echo of the bound parameters,
but the server sends the offending row itself inside the error message — `DETAIL:  Failing row
contains (…, 12.5, …)` for a check violation, `DETAIL:  Key (…)=(…)` for a unique one — and no
client setting suppresses that. A guard at a call site (`backfill_location._store`) closes one
path; an exception that escapes FastAPI is rendered by Uvicorn's own stdlib logger, which no call
site controls. The invariant is "no coordinate value in any log line", so the scrub sits at the
boundary where an exception is RENDERED, once, for every path:

* structlog — a processor appended AFTER the renderer, so it sees the finished line including the
  formatted traceback;
* stdlib logging — a filter on the root and uvicorn loggers, which pre-renders `exc_info` into
  `record.exc_text` (the Formatter then reuses that text instead of re-formatting) and scrubs the
  message and its args too.

Both call the same pure function, so there is one pattern to review and one to test.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

import structlog

ROW_DETAIL_REDACTION = "DETAIL: [row hidden]"
KEY_DETAIL_REDACTION = "DETAIL: [key hidden]"

# Each pattern eats the WHOLE detail: greedily to the last `).`/`.` on the line, or, when a
# truncated message has neither, to the end of the line. Stopping at the first `)` would leave
# the tail of a row visible whenever a value contained one — a row is redacted whole or not at
# all. The line ends at a real newline OR at a literal backslash-n, because `repr(exc)` and a
# traceback's source line both spell it that way.
_LINE = r"(?:(?!\\n)[^\n])"
_ROW_DETAIL = re.compile(rf"DETAIL:[ \t]*Failing row contains \((?:{_LINE}*\)\.|{_LINE}*)")
_KEY_DETAIL = re.compile(rf"DETAIL:[ \t]*Key \((?:{_LINE}*\.|{_LINE}*)")


def redact_row_details(text: str) -> str:
    """Replace every PostgreSQL row/key DETAIL in `text` with a fixed marker.

    Everything else — the constraint name, the SQLSTATE, the statement, the traceback frames —
    is left exactly as it was: the point is to keep the failure diagnosable while the values are
    gone.
    """
    if "DETAIL:" not in text:
        return text
    text = _ROW_DETAIL.sub(ROW_DETAIL_REDACTION, text)
    return _KEY_DETAIL.sub(KEY_DETAIL_REDACTION, text)


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


class RowDetailFilter(logging.Filter):
    """Scrub a stdlib record before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_row_details(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_row_details(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.args, Mapping):
            record.args = {
                k: redact_row_details(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        if record.exc_info and not record.exc_text:
            # Formatting it here is what lets a Filter reach a traceback at all: `Formatter.format`
            # reuses a record's `exc_text` rather than re-rendering `exc_info`.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_row_details(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_row_details(record.stack_info)
        return True


# The loggers Uvicorn logs an escaped request exception through, plus the root for everything
# that reaches a handler by propagation from an unnamed call site.
_FILTERED_LOGGERS = ("", "uvicorn", "uvicorn.error")


def configure_logging() -> None:
    """Install both hooks. Idempotent — importing the app twice must not stack filters."""
    processors = list(structlog.get_config()["processors"])
    if redact_processor not in processors:
        processors.append(redact_processor)
        structlog.configure(processors=processors)
    for name in _FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RowDetailFilter) for f in logger.filters):
            logger.addFilter(RowDetailFilter())
