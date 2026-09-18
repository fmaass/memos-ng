"""The last mile of the Places privacy invariant: no coordinate in any RENDERED log line.

`hide_parameters=True` silences SQLAlchemy's own parameter echo, but PostgreSQL puts the
offending row into its own error message (`DETAIL:  Failing row contains (…)`) and no client
setting suppresses that. A `_store`-style guard closes one call site; an exception that escapes
FastAPI is rendered by Uvicorn's stdlib logger, which no call site controls. So the scrub lives
where an exception becomes TEXT — one pure function, two hooks — and this file pins both hooks
plus the shapes the two drivers actually produce (captured from the test database, see the
`DETAIL` lines below).
"""

import io
import logging
import subprocess
import sys

import app.logging_privacy as logging_privacy
import pytest
import structlog
from app.logging_privacy import (
    ROW_DETAIL_REDACTION,
    RowDetailFilter,
    configure_logging,
    redact_processor,
    redact_row_details,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

# Captured verbatim from memosng_test_p4 (asyncpg); psycopg2 renders the same DETAIL lines.
ROW_VIOLATION = (
    "(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) <class"
    " 'asyncpg.exceptions.CheckViolationError'>: new row for relation \"attachments\" violates"
    ' check constraint "ck_attachments_location_pair"\n'
    "DETAIL:  Failing row contains (1767, probe-a, 56, a.jpg, image/jpeg, 1, null, ready,"
    " /dev/null, 0, 12.5, null, null).\n"
    "[SQL: UPDATE attachments SET latitude = $1 WHERE id = $2]\n"
    "[SQL parameters hidden due to hide_parameters=True]\n"
)
KEY_VIOLATION = (
    "(psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint"
    ' "ix_attachments_uid"\n'
    "DETAIL:  Key (uid, latitude)=(probe-a, 12.5) already exists.\n"
)


def test_a_failing_row_detail_is_replaced_and_the_constraint_survives():
    scrubbed = redact_row_details(ROW_VIOLATION)
    assert ROW_DETAIL_REDACTION in scrubbed
    assert "Failing row contains" not in scrubbed
    assert "12.5" not in scrubbed
    assert "probe-a" not in scrubbed
    # What a reader needs to act on the failure precedes the detail and is untouched.
    assert "ck_attachments_location_pair" in scrubbed
    assert "new row for relation" in scrubbed
    # Everything AFTER the detail goes with it: no textual boundary can tell a diagnostic field
    # from a value that merely spells one.
    assert "[SQL:" not in scrubbed
    assert scrubbed.endswith(ROW_DETAIL_REDACTION)


def test_a_unique_violation_key_detail_is_replaced_the_same_way():
    scrubbed = redact_row_details(KEY_VIOLATION)
    assert ROW_DETAIL_REDACTION in scrubbed
    assert "12.5" not in scrubbed
    assert "probe-a" not in scrubbed
    assert "ix_attachments_uid" in scrubbed


def test_text_without_a_detail_line_is_returned_unchanged():
    plain = "Traceback (most recent call last):\n  File 'x.py', line 1\nValueError: nope\n"
    assert redact_row_details(plain) == plain
    assert redact_row_details("") == ""


def test_the_detail_is_scrubbed_inside_a_multiline_traceback_and_the_frames_survive():
    rendered = (
        "Traceback (most recent call last):\n"
        '  File "sqlalchemy/engine/default.py", line 952, in do_execute\n'
        "    cursor.execute(statement, parameters)\n"
        "sqlalchemy.exc.IntegrityError: " + ROW_VIOLATION
    )
    scrubbed = redact_row_details(rendered)
    assert "12.5" not in scrubbed
    assert "in do_execute" in scrubbed
    assert "cursor.execute(statement, parameters)" in scrubbed
    assert scrubbed.count(ROW_DETAIL_REDACTION) == 1
    assert "[SQL:" not in scrubbed


def test_the_detail_is_scrubbed_when_a_repr_escaped_it_onto_one_line():
    # `repr(exc)` and a traceback's SOURCE line both spell the newline as a literal backslash-n,
    # so a line-anchored scrub that only knows real newlines would miss the row entirely.
    one_line = (
        'IntegrityError(\'new row violates check constraint "ck_attachments_location_pair"\\n'
        "DETAIL:  Failing row contains (1767, probe-a, 12.5, null).')"
    )
    scrubbed = redact_row_details(one_line)
    assert "12.5" not in scrubbed
    assert "probe-a" not in scrubbed
    assert "ck_attachments_location_pair" in scrubbed


async def _location_violation(adb) -> SQLAlchemyError:
    """A REAL check-constraint failure carrying a coordinate — half a location pair."""
    owner = (
        await adb.execute(
            text(
                "INSERT INTO users (subject, display_name, role, created_at)"
                " VALUES ('log-privacy', 'log-privacy', 'user', now()) RETURNING id"
            )
        )
    ).scalar_one()
    memo = (
        await adb.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at,"
                " updated_at) VALUES ('log-privacy-memo', :o, '', 'PRIVATE', false, now(), now())"
                " RETURNING id"
            ),
            {"o": owner},
        )
    ).scalar_one()
    attachment = (
        await adb.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status,"
                " storage_path, position, created_at) VALUES ('log-privacy-att', :o, :m, 'a.jpg',"
                " 'image/jpeg', 1, 'ready', '/dev/null', 0, now()) RETURNING id"
            ),
            {"o": owner, "m": memo},
        )
    ).scalar_one()
    with pytest.raises(SQLAlchemyError) as caught:
        await adb.execute(
            text("UPDATE attachments SET latitude = :lat WHERE id = :id"),
            {"lat": 12.5, "id": attachment},
        )
    return caught.value


@pytest.mark.asyncio
async def test_a_structlog_event_carrying_the_exception_renders_without_the_row(adb):
    exc = await _location_violation(adb)
    assert "12.5" in str(exc)  # positive control: the exception really does carry the coordinate

    configure_logging()
    saved = structlog.get_config()
    buffer = io.StringIO()
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buffer))
    try:
        structlog.get_logger("test").error(
            "location_write_failed", uid="log-privacy-att", exc_info=exc
        )
    finally:
        structlog.configure(**saved)

    rendered = buffer.getvalue()
    assert "location_write_failed" in rendered  # the event was logged at all
    assert "ck_attachments_location_pair" in rendered  # and is still diagnosable
    assert "12.5" not in rendered
    assert "Failing row contains" not in rendered
    assert ROW_DETAIL_REDACTION in rendered


@pytest.mark.asyncio
async def test_the_uvicorn_error_logger_renders_the_same_exception_without_the_row(adb):
    exc = await _location_violation(adb)
    assert "12.5" in str(exc)  # positive control

    configure_logging()
    logger = logging.getLogger("uvicorn.error")
    # alembic's `fileConfig` — run by the schema fixture — disables every logger that already
    # existed. Uvicorn's own dictConfig re-enables its loggers at startup; this stands in for it,
    # so what this case exercises is the rendering, not the enablement.
    logger.disabled = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.error("Exception in ASGI application", exc_info=exc)
    finally:
        logger.removeHandler(handler)

    rendered = buffer.getvalue()
    assert "Exception in ASGI application" in rendered
    assert "ck_attachments_location_pair" in rendered
    assert "12.5" not in rendered
    assert "Failing row contains" not in rendered
    assert ROW_DETAIL_REDACTION in rendered


def test_a_record_whose_message_carries_the_detail_is_scrubbed_too():
    # Not every leak arrives as exc_info: a call site that formats the error into its message
    # hands the same text to the same handler.
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "write failed: %s", (ROW_VIOLATION,), None
    )
    assert RowDetailFilter().filter(record) is True
    rendered = record.getMessage()
    assert "12.5" not in rendered
    assert ROW_DETAIL_REDACTION in rendered


def test_configure_logging_registers_both_hooks_and_is_idempotent():
    configure_logging()
    configure_logging()
    processors = structlog.get_config()["processors"]
    assert processors.count(redact_processor) == 1
    # The record factory is the hook that reaches every logger and every handler; installing it
    # twice would make it wrap itself.
    assert logging.getLogRecordFactory() is logging_privacy._record_factory
    assert logging_privacy._original_factory is not logging_privacy._record_factory
    assert processors[-1] is redact_processor  # after the renderer, i.e. after exception formatting
    for name in ("", "uvicorn", "uvicorn.error"):
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, RowDetailFilter)]
        assert len(filters) == 1, f"{name!r} carries {filters}"


def test_importing_the_app_installs_the_filter_before_any_request():
    # A subprocess, because the in-process loggers were already configured by another test: what
    # must hold is that importing the app is enough.
    probe = (
        "import app.main, logging;"
        "from app.logging_privacy import RowDetailFilter;"
        "print(all(any(isinstance(f, RowDetailFilter) for f in logging.getLogger(n).filters)"
        " for n in ('', 'uvicorn.error')))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=180
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("True"), done.stdout + done.stderr[-2000:]


# --- a row is redacted WHOLE, whatever its values contain -------------------------------------
# Values are stored verbatim: a filename may hold a newline, a `).`, a `%`. A line-anchored scrub
# stops at the first of those and leaves the coordinates standing on the next line.

MULTILINE_ROW = (
    'new row for relation "attachments" violates check constraint'
    ' "ck_attachments_location_pair"\n'
    "DETAIL:  Failing row contains (1767, photo\nname.jpg, image/jpeg, 12.5, 8.1).\n"
    "[SQL: UPDATE attachments SET latitude = $1 WHERE id = $2]\n"
)


def test_a_value_with_a_real_newline_does_not_end_the_scrub_early():
    scrubbed = redact_row_details(MULTILINE_ROW)
    assert "12.5" not in scrubbed
    assert "8.1" not in scrubbed
    assert "name.jpg" not in scrubbed
    assert "ck_attachments_location_pair" in scrubbed
    assert "[SQL:" not in scrubbed  # deliberately lost with everything after the detail


def test_a_value_with_an_escaped_newline_does_not_end_the_scrub_early():
    one_line = MULTILINE_ROW.replace("\n", "\\n")
    scrubbed = redact_row_details(one_line)
    assert "12.5" not in scrubbed
    assert "8.1" not in scrubbed
    assert "ck_attachments_location_pair" in scrubbed


def test_a_value_containing_a_closing_paren_and_period_does_not_end_the_scrub_early():
    text = (
        'violates check constraint "ck_attachments_location_pair"\n'
        "DETAIL:  Failing row contains (1767, holiday (2024).jpg, image/jpeg, 12.5, 8.1).\n"
        "[SQL: UPDATE attachments SET latitude = $1]\n"
    )
    scrubbed = redact_row_details(text)
    assert "12.5" not in scrubbed
    assert "8.1" not in scrubbed
    assert "holiday" not in scrubbed
    assert "[SQL:" not in scrubbed


def test_a_chained_traceback_after_the_detail_goes_with_it():
    text = (
        "Traceback (most recent call last):\n"
        '  File "asyncpg/protocol.py", line 1, in execute\n'
        "asyncpg.exceptions.CheckViolationError: new row violates check constraint"
        ' "ck_attachments_location_pair"\n'
        "DETAIL:  Failing row contains (1767, a.jpg, 12.5, 8.1).\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "app/services/location.py", line 42, in store\n'
        "sqlalchemy.exc.IntegrityError: (asyncpg.exceptions.CheckViolationError)\n"
    )
    scrubbed = redact_row_details(text)
    assert "12.5" not in scrubbed
    assert "8.1" not in scrubbed
    # A row value may spell any of these itself, so what follows the detail cannot be trusted to
    # be diagnostics; the second traceback is the price.
    assert "The above exception was the direct cause of the following exception:" not in scrubbed
    assert "app/services/location.py" not in scrubbed
    # Everything BEFORE it — the failing frame, the constraint — is what a reader needs.
    assert "asyncpg/protocol.py" in scrubbed
    assert "ck_attachments_location_pair" in scrubbed


def test_a_multiline_row_is_scrubbed_through_the_installed_uvicorn_filter():
    # The reviewers reproduced the leak here, not on the pure function: the record path is what
    # a request failure actually travels.
    configure_logging()
    logger = logging.getLogger("uvicorn.error")
    logger.disabled = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.error(MULTILINE_ROW)
    finally:
        logger.removeHandler(handler)
    rendered = buffer.getvalue()
    assert "12.5" not in rendered
    assert "8.1" not in rendered
    assert "ck_attachments_location_pair" in rendered


# --- the record's ARGS, not just its message ---------------------------------------------------


def test_an_exception_object_passed_as_a_format_arg_is_scrubbed():
    # `logger.error("write failed: %s", exc)` renders `str(exc)` at FORMAT time, i.e. after any
    # filter that only looked at strings has run.
    configure_logging()
    logger = logging.getLogger("uvicorn.error")
    logger.disabled = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.error("write failed: %s", ValueError(ROW_VIOLATION))
    finally:
        logger.removeHandler(handler)
    rendered = buffer.getvalue()
    assert "write failed:" in rendered
    assert "12.5" not in rendered
    assert "probe-a" not in rendered
    assert "ck_attachments_location_pair" in rendered


def test_a_percent_sign_in_the_scrubbed_text_does_not_break_formatting():
    # Scrubbing renders the message, so the result must no longer be treated as a format string.
    configure_logging()
    logger = logging.getLogger("uvicorn.error")
    logger.disabled = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger.addHandler(handler)
    try:
        logger.error("100%% done, then %s", ValueError("DETAIL:  Failing row contains (12.5)."))
    finally:
        logger.removeHandler(handler)
    rendered = buffer.getvalue()
    assert "100% done" in rendered
    assert "12.5" not in rendered


# --- every logger, every handler, including handlers installed later ---------------------------


def test_a_child_logger_reaching_a_root_handler_is_scrubbed():
    # A filter attached to a LOGGER never sees records propagated from its children, so the root
    # filter alone protects nothing that `app.*`, `sqlalchemy.*` or `alembic` emit.
    configure_logging()
    root = logging.getLogger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root.addHandler(handler)
    child = logging.getLogger("sqlalchemy.engine.Engine.probe")
    child.disabled = False
    previous = root.level
    root.setLevel(logging.ERROR)
    try:
        child.error("write failed: %s", ValueError(ROW_VIOLATION))
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    rendered = buffer.getvalue()
    assert "write failed:" in rendered
    assert "12.5" not in rendered
    assert "ck_attachments_location_pair" in rendered


def test_a_handler_attached_after_configure_logging_is_covered_too():
    # Uvicorn configures its handlers BEFORE it imports the app; the worker and the backfill
    # configure their own. Nothing may depend on the order.
    configure_logging()
    logger = logging.getLogger("app.probe.late")
    logger.disabled = False
    logger.propagate = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)  # attached after the hooks were installed
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        logger.error(MULTILINE_ROW)
    finally:
        logger.removeHandler(handler)
    rendered = buffer.getvalue()
    assert "12.5" not in rendered
    assert "ck_attachments_location_pair" in rendered


# --- a value may SPELL a boundary; only "everything after DETAIL:" is safe ---------------------
# Values are stored verbatim, so a filename can contain "HINT:", "[SQL:", or a whole traceback.
# A scrub that stops at the first boundary-looking text stops inside the row, which is how the
# reviewers got "HINT: name.jpg, image/jpeg, 12.5, 8.1)." back out of a log line.

CONSTRAINT = 'violates check constraint "ck_attachments_location_pair"'
BOUNDARY_LOOKALIKES = {
    "hint_after_a_real_newline": (
        f'new row for relation "attachments" {CONSTRAINT}\n'
        "DETAIL:  Failing row contains (1767, photo\nHINT: name.jpg, image/jpeg, 12.5, 8.1).\n"
        "[SQL: UPDATE attachments SET latitude = $1]\n"
    ),
    "hint_after_an_escaped_newline": (
        f'new row for relation "attachments" {CONSTRAINT}\\n'
        "DETAIL:  Failing row contains (1767, holiday\\nHINT:photo.jpg, 12.5, 8.1).\\n"
    ),
    "sql_echo_inside_a_value": (
        f'new row for relation "attachments" {CONSTRAINT}\n'
        "DETAIL:  Failing row contains (1767, [SQL: select 1].png, image/jpeg, 12.5, 8.1).\n"
    ),
    "chained_traceback_after_the_detail": (
        f"asyncpg.exceptions.CheckViolationError: new row {CONSTRAINT}\n"
        "DETAIL:  Failing row contains (1767, a.jpg, 12.5, 8.1).\n"
        "\nThe above exception was the direct cause of the following exception:\n\n"
        "Traceback (most recent call last):\n"
        '  File "app/services/location.py", line 42, in store\n'
    ),
}


def _through_the_record_factory(text: str) -> str:
    """A record built the ordinary way, on a logger that carries no filter of its own."""
    configure_logging()
    logger = logging.getLogger("app.probe.boundary")
    logger.disabled = False
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger.addHandler(handler)
    try:
        logger.error(text)
    finally:
        logger.removeHandler(handler)
    return buffer.getvalue()


def _through_the_uvicorn_filter(text: str) -> str:
    """A record built WITHOUT the factory — `makeLogRecord` calls the class directly — so this
    exercises the filter on its own."""
    configure_logging()
    logger = logging.getLogger("uvicorn.error")
    logger.disabled = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.handle(
            logging.makeLogRecord(
                {
                    "name": "uvicorn.error",
                    "levelno": logging.ERROR,
                    "levelname": "ERROR",
                    "msg": text,
                }
            )
        )
    finally:
        logger.removeHandler(handler)
    return buffer.getvalue()


def _through_the_structlog_processor(text: str) -> str:
    configure_logging()
    return str(redact_processor(None, "error", f"[error] write_failed uid=probe-a\n{text}"))


@pytest.mark.parametrize(
    "render",
    [_through_the_record_factory, _through_the_uvicorn_filter, _through_the_structlog_processor],
    ids=["record_factory", "uvicorn_filter", "structlog_processor"],
)
@pytest.mark.parametrize("case", sorted(BOUNDARY_LOOKALIKES))
def test_a_boundary_spelled_inside_a_value_does_not_end_the_scrub(render, case):
    rendered = render(BOUNDARY_LOOKALIKES[case])
    assert "12.5" not in rendered
    assert "8.1" not in rendered
    assert "HINT:" not in rendered
    assert "[SQL:" not in rendered
    # Nothing at all survives the detail, so nothing that follows can be a value.
    assert rendered.rstrip().endswith(ROW_DETAIL_REDACTION)
    # The failure is still named by what precedes the detail.
    assert "ck_attachments_location_pair" in rendered
