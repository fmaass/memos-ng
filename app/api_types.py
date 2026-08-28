"""The one shape every `date-time` leaves this API in (MEMNG-106).

pydantic's default renders a datetime with microseconds only when the value HAS microseconds, so
one response could carry `2026-08-20T20:36:00Z` (a backdated memo, whole second) next to
`2026-08-21T20:41:49.443000Z` (`updated_at`, stamped by Postgres `now()`). Both are valid
RFC 3339, but a reader with a single format string accepts one and rejects the other — and that
is not hypothetical: swift-openapi-runtime's default transcoder takes second precision only, so
the iOS client had to ship `RFC3339DateTranscoder` to try both spellings before every decode.

**Second precision with a `Z` suffix** is the shape, for three reasons:

1. It is already the shape of `/api/v1`, the memos 0.29.1 compat surface (`compat._iso`), which is
   pinned to captured live responses. Choosing it makes the whole server speak one dialect instead
   of two, and leaves that pinned contract untouched.
2. It is what a client's DEFAULT decoder handles — `JSONDecoder.dateDecodingStrategy = .iso8601`,
   `Date.ISO8601FormatStyle()`, swift-openapi-runtime's own transcoder. Fixed-width fractional
   digits would be equally consistent but would keep every one of those callers on a custom
   decoder, so the tolerant workaround could never be deleted; with this shape it can.
3. Nothing here needs sub-second resolution across the wire. Ordering and pagination ride the
   opaque keyset cursor (`app.services.cursors`), which carries the full-precision value inside
   its payload rather than reading it back off a rendered field, so truncating the rendered form
   changes no ordering and skips no page. The SPA parses with `new Date(...)` and buckets by day.

Truncation, not rounding: a rendered stamp never moves into the future, and it matches what
`compat._iso` has always done.

USE `Rfc3339Datetime` for every datetime field of every model an `/api/*` route can answer with —
a bare `datetime` reintroduces the split, and tests/test_datetime_shape.py fails when one appears.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema


def to_rfc3339(value: datetime) -> str:
    """`value` as `YYYY-MM-DDTHH:MM:SSZ`, in UTC, with any sub-second part dropped.

    A naive value is read as UTC rather than as the process's local zone: every column behind this
    is `timestamptz` and comes back aware, so naive only arrives from a caller that omitted the
    offset — and interpreting that in whatever zone the container happens to run in would move the
    instant, which is a worse failure than an ugly one.
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


Rfc3339Datetime = Annotated[
    datetime,
    # `when_used="json-unless-none"` keeps `model_dump()` (python mode) handing back real datetime
    # objects — internal callers dump models that way — and keeps None out of the formatter.
    # FastAPI renders a response through `model_dump(mode="json")`, so this is the path that runs
    # on the wire.
    PlainSerializer(to_rfc3339, return_type=str, when_used="json-unless-none"),
    # Load-bearing, not decoration: declaring `return_type=str` rewrites the SERIALISATION schema
    # to a bare `{"type": "string"}`, and the serialisation schema is exactly the one a generated
    # client reads for a response body. Without this, swift-openapi-generator emits `String` where
    # the client wants `Date`. The validation schema (request bodies) is unaffected either way.
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
