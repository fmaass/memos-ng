"""Every `date-time` the API publishes has ONE shape: `YYYY-MM-DDTHH:MM:SSZ` (MEMNG-106).

Production used to emit two shapes in a single response — `2026-08-20T20:36:00Z` next to
`2026-08-21T20:41:49.443000Z` — because pydantic's default datetime serialisation prints
microseconds only when the stored value has them, and `now()` from Postgres almost always has
them while a backdated memo almost never does. A strict RFC 3339 reader that accepts one form
rejects the other, which is why the iOS client carries a tolerant transcoder
(`Packages/MemosAPI/Sources/MemosAPI/RFC3339DateTranscoder.swift`) purely to work around it.

The chosen shape is second precision with a `Z` suffix, matching what `/api/v1` (the memos 0.29.1
compat surface, `app.routers.compat._iso`) has always emitted. See app/api_types.py for why.

Three things are asserted here, and all three are needed:

* the AC itself — a whole-second row and a microsecond-bearing row come out identically shaped
  through a real request;
* a SWEEP over every response model the app publishes, so a datetime field added to a new model
  fails this test instead of quietly reintroducing the second shape;
* the serialisation JSON Schema still says `format: date-time`, because the serializer that fixes
  the shape also erases that keyword unless it is put back — and a response schema that says only
  `type: string` makes swift-openapi-generator emit `String` where the client wants `Date`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, get_args

import pytest
from app.api_types import Rfc3339Datetime, to_rfc3339
from app.main import app
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# What the API is allowed to publish: UTC, second precision, `Z`.
CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Anything a reader would take for a timestamp, in any RFC 3339 spelling — the net that catches a
# field still serialising the old way.
TIMESTAMP_ISH = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)
# Carries microseconds on purpose: the value that used to serialise differently from a whole one.
SAMPLE = datetime(2026, 8, 21, 20, 41, 49, 443210, tzinfo=UTC)


def _timestamps(node: Any, path: str = "$") -> list[tuple[str, str]]:
    """Every timestamp-looking string in a JSON body, with the pointer that reaches it."""
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _timestamps(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _timestamps(v, f"{path}[{i}]")]
    if isinstance(node, str) and TIMESTAMP_ISH.match(node):
        return [(path, node)]
    return []


def _assert_all_canonical(body: Any, *, at_least: int) -> list[tuple[str, str]]:
    found = _timestamps(body)
    # Positive control: an assertion over an empty list passes for the wrong reason.
    assert len(found) >= at_least, f"only found {found}, expected at least {at_least} timestamps"
    offenders = [(p, v) for p, v in found if not CANONICAL.match(v)]
    assert offenders == [], f"non-canonical timestamps: {offenders}"
    return found


# --- the formatter itself -------------------------------------------------------------------


def test_microseconds_are_dropped_and_whole_seconds_are_unchanged():
    assert to_rfc3339(SAMPLE) == "2026-08-21T20:41:49Z"
    assert to_rfc3339(SAMPLE.replace(microsecond=0)) == "2026-08-21T20:41:49Z"


def test_a_non_utc_offset_is_converted_rather_than_relabelled():
    berlin = SAMPLE.astimezone(timezone(timedelta(hours=2)))
    assert berlin.utcoffset() == timedelta(hours=2)
    assert to_rfc3339(berlin) == "2026-08-21T20:41:49Z"


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (1, "0001-01-02T03:04:05Z"),
        (999, "0999-01-02T03:04:05Z"),
        (1000, "1000-01-02T03:04:05Z"),
        (2026, "2026-01-02T03:04:05Z"),
    ],
)
def test_the_year_is_always_four_digits(year: int, expected: str):
    """RFC 3339's `date-fullyear` is exactly four digits, and `strftime("%Y")` does not pad to
    four on this runtime (glibc renders year 999 as `999`). Nothing bounds how far back a memo can
    be dated — `MemoCreate.created_at` takes any datetime — so a year below 1000 is reachable
    input, and a three-digit year is the same broken promise as a stray `.443210`."""
    rendered = to_rfc3339(datetime(year, 1, 2, 3, 4, 5, 123456, tzinfo=UTC))
    assert rendered == expected
    assert CANONICAL.match(rendered)


def test_a_naive_value_is_read_as_utc_not_as_the_servers_local_zone():
    """The store is `timestamptz` and every row comes back aware, so this is the defensive branch —
    but reading a naive value in the container's local zone would silently shift the timestamp,
    and a wrong instant is worse than an ugly one."""
    assert to_rfc3339(SAMPLE.replace(tzinfo=None)) == "2026-08-21T20:41:49Z"


# --- the acceptance criterion: two rows, one shape ------------------------------------------


def test_a_whole_second_memo_and_a_microsecond_memo_render_the_same_shape(logged_in_client):
    """The defect, reproduced through the real surface: `created_at` is caller-supplied (and here
    whole-second on one memo, microsecond-bearing on the other) while `updated_at` is stamped by
    Postgres `now()` and carries microseconds — so a single feed page used to hold both shapes."""
    client, _ = logged_in_client
    whole = client.post(
        "/api/memos",
        json={"content": "whole second", "created_at": "2026-08-20T20:36:00Z"},
    )
    assert whole.status_code == 201, whole.text
    fractional = client.post(
        "/api/memos",
        json={"content": "microseconds", "created_at": "2026-08-21T20:41:49.443210Z"},
    )
    assert fractional.status_code == 201, fractional.text

    assert whole.json()["created_at"] == "2026-08-20T20:36:00Z"
    assert fractional.json()["created_at"] == "2026-08-21T20:41:49Z"

    feed = client.get("/api/memos?limit=50")
    assert feed.status_code == 200, feed.text
    items = [m for m in feed.json()["items"] if m["content"] in ("whole second", "microseconds")]
    assert len(items) == 2, feed.json()
    # 2 memos x created_at + updated_at; archived_at is null on both.
    found = _assert_all_canonical(items, at_least=4)
    lengths = {len(v) for _, v in found}
    assert lengths == {len("2026-08-20T20:36:00Z")}, f"more than one shape in one response: {found}"


def test_a_memo_dated_before_the_year_1000_still_renders_a_four_digit_year(logged_in_client):
    """The same guarantee through a real response model, not just the formatter: nothing between
    the request and `MemoOut.created_at` bounds the year, so the rendered shape has to hold for a
    backdate this far out."""
    client, _ = logged_in_client
    created = client.post(
        "/api/memos", json={"content": "medieval", "created_at": "0999-01-02T03:04:05.123456Z"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["created_at"] == "0999-01-02T03:04:05Z"
    _assert_all_canonical(created.json(), at_least=2)  # created_at + updated_at

    fetched = client.get(f"/api/memos/{created.json()['uid']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["created_at"] == "0999-01-02T03:04:05Z"


def test_the_token_surface_publishes_the_same_shape(logged_in_client):
    """A second surface, with the three nullable stamps a memo does not have. `created_at` is a
    Postgres `now()` default and `expires_at` is a Python `datetime.now(UTC)` — both microsecond
    bearing, from two different clocks."""
    client, _ = logged_in_client
    created = client.post("/api/pats", json={"name": "device", "expires_in_days": 30})
    assert created.status_code == 201, created.text
    _assert_all_canonical(created.json(), at_least=2)  # created_at + expires_at; last_used is null
    listed = client.get("/api/pats")
    assert listed.status_code == 200, listed.text
    _assert_all_canonical(listed.json(), at_least=2)


# --- the compat surface is unchanged --------------------------------------------------------


def test_the_v1_compat_surface_keeps_the_shape_it_always_had():
    """`/api/v1` is pinned to captured memos 0.29.1 responses (fixtures/live-0.29.1, gitignored —
    see tests/test_compat.py). It already emitted second precision with a `Z`; this test exists so
    the fix above cannot be mistaken for having changed it, and so a later refactor that routes
    compat through the shared serializer is proved not to move the contract."""
    subject = "dtshape_compat_user"
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            owner = c.execute(
                text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
                {"s": subject},
            ).scalar_one()
            # Microsecond-bearing on purpose: the row shape the defect used to show up on.
            c.execute(
                text(
                    "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, "
                    "updated_at) VALUES ('dtshapecompat',:o,'compat',"
                    "CAST('PUBLIC' AS visibility), false, :t, :t)"
                ),
                {"o": owner, "t": datetime(2026, 8, 21, 20, 41, 49, 443210, tzinfo=UTC)},
            )
        with TestClient(app) as client:
            body = client.get("/api/v1/memos?pageSize=50").json()
        mine = [m for m in body["memos"] if m["name"] == "memos/dtshapecompat"]
        assert len(mine) == 1, body
        assert mine[0]["createTime"] == "2026-08-21T20:41:49Z"
        assert mine[0]["updateTime"] == "2026-08-21T20:41:49Z"
        _assert_all_canonical(mine, at_least=2)
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": subject})
        eng.dispose()


# --- the sweep: no response model may bypass the shared serializer ---------------------------


def _reachable_models() -> dict[str, type[BaseModel]]:
    """Every pydantic model a published route can put on the wire, nested ones included."""
    found: dict[str, type[BaseModel]] = {}

    def walk(annotation: Any) -> None:
        stack = [annotation]
        while stack:
            node = stack.pop()
            if isinstance(node, type) and issubclass(node, BaseModel):
                if node.__name__ in found:
                    continue
                found[node.__name__] = node
                stack.extend(f.rebuild_annotation() for f in node.model_fields.values())
                continue
            stack.extend(get_args(node))

    for route in app.routes:
        if isinstance(route, APIRoute) and route.response_model is not None:
            walk(route.response_model)
        if isinstance(route, APIRoute):
            for response in (route.responses or {}).values():
                model = response.get("model") if isinstance(response, dict) else None
                if model is not None:
                    walk(model)
    return found


def _datetime_fields() -> list[tuple[str, str, Any]]:
    """(model, field, rebuilt annotation) for every field whose type mentions `datetime`."""
    out: list[tuple[str, str, Any]] = []
    for name, model in sorted(_reachable_models().items()):
        for field_name, field in model.model_fields.items():
            annotation = field.rebuild_annotation()
            stack, mentions = [annotation], False
            while stack:
                node = stack.pop()
                if node is datetime:
                    mentions = True
                    break
                stack.extend(get_args(node))
            if mentions:
                out.append((name, field_name, annotation))
    return out


# The fields that exist today. Pinned so that a route or model disappearing from the sweep is a
# failure rather than a silently shrinking (and therefore vacuous) parametrisation.
EXPECTED_DATETIME_FIELDS = {
    ("AttachmentMeta", "captured_at"),
    ("AttachmentOut", "captured_at"),
    ("ManageGrantOut", "expires_at"),
    ("MemoOut", "archived_at"),
    ("MemoOut", "created_at"),
    ("MemoOut", "updated_at"),
    ("NativeExchangeOut", "expires_at"),
    ("NativeTokenOut", "created_at"),
    ("NativeTokenOut", "expires_at"),
    ("NativeTokenOut", "last_used"),
    ("PatCreated", "created_at"),
    ("PatCreated", "expires_at"),
    ("PatCreated", "last_used"),
    ("PatOut", "created_at"),
    ("PatOut", "expires_at"),
    ("PatOut", "last_used"),
}


def test_the_sweep_still_covers_every_datetime_field_the_api_publishes():
    assert {(m, f) for m, f, _ in _datetime_fields()} == EXPECTED_DATETIME_FIELDS


@pytest.mark.parametrize(
    ("model", "field", "annotation"),
    _datetime_fields(),
    ids=[f"{m}.{f}" for m, f, _ in _datetime_fields()],
)
def test_every_published_datetime_field_serialises_through_the_shared_shape(
    model: str, field: str, annotation: Any
):
    """Fails for a new response-model field declared as a bare `datetime` — the bypass this
    ticket is about. The check is behavioural: the field's own annotation is asked to serialise a
    microsecond-bearing instant, which a bare `datetime` renders with the microseconds."""
    rendered = TypeAdapter(annotation).dump_python(SAMPLE, mode="json")
    assert isinstance(rendered, str), (
        f"{model}.{field} did not serialise to a string — if this field is a container of "
        f"datetimes, extend this test to feed it the right sample"
    )
    assert CANONICAL.match(rendered), (
        f"{model}.{field} serialises as {rendered!r}; annotate it "
        f"`app.api_types.Rfc3339Datetime` instead of a bare `datetime`"
    )


def test_a_bare_datetime_field_would_fail_the_sweep():
    """The sweep's own negative control: without the shared annotation the check goes red, so a
    green sweep above is evidence of the annotation and not of a check that always passes."""

    class Bypass(BaseModel):
        stamp: datetime

    rendered = TypeAdapter(
        Bypass.model_fields["stamp"].rebuild_annotation()
    ).dump_python(SAMPLE, mode="json")
    assert not CANONICAL.match(rendered), rendered


def test_the_serialisation_schema_still_declares_format_date_time():
    """`PlainSerializer(return_type=str)` replaces the serialisation schema with a plain string.
    Every generated client reads THAT schema for a response, so without the `WithJsonSchema` in
    app/api_types.py the Swift client would model these fields as `String`."""
    schema = TypeAdapter(Rfc3339Datetime).json_schema(mode="serialization")
    assert schema == {"type": "string", "format": "date-time"}


def test_the_committed_openapi_still_publishes_date_time_for_every_swept_field():
    """Read off the committed snapshot, which is what the generators actually consume."""
    import json
    from pathlib import Path

    schemas = json.loads(
        (Path(__file__).resolve().parent.parent / "openapi.json").read_text()
    )["components"]["schemas"]
    for model, field in sorted(EXPECTED_DATETIME_FIELDS):
        published = schemas[model]["properties"][field]
        assert published["format"] == "date-time", (model, field, published)
