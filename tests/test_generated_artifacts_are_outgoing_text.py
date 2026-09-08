"""Everything `openapi.json` exports is outgoing text, and so is what it generates.

The document is served to clients and vendored verbatim into the native client, and the SPA's
`frontend/src/api/schema.d.ts` is generated from that same file — so a description written for a
maintainer travels straight out of the estate. The mechanism is easy to trip over: a Pydantic
model's docstring becomes its component `description` and a route function's docstring becomes the
operation `description`. Rationale belongs in ordinary `#` comments beside the code; only text
written FOR a client belongs in a docstring.

This test is the gate on that class. It reads the two committed artifacts and refuses anything that
only means something inside the estate — a tracker key, an internal hostname, a filesystem path, or
a POINTER to where a rule was decided (a decision number, an ADR id, a phase or a review round). A
description states the behaviour the client sees; where that behaviour was settled is a comment's
business. The test also asserts it actually read the files, because a pattern that matched nothing
because it opened the wrong path proves nothing at all.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI = REPO_ROOT / "openapi.json"
SCHEMA_D_TS = REPO_ROOT / "frontend" / "src" / "api" / "schema.d.ts"

# One rule, spelled out as an alphabet: an exported description may name only what the client can
# see — a route, a field, a header, a status or an error code. Everything below is something it
# cannot: a tracker key, an internal hostname or path, a record where a decision was written down,
# a source file, or a private identifier that exists only in this repository.
#
# Deliberately NOT here: "session", which is this API's own vocabulary (the `sessionCookie` scheme,
# a browser session, an upload session), and snake_case FIELD names, which a client sends and
# receives — only identifiers that begin with an underscore are private by convention.
FORBIDDEN = re.compile(
    r"MEMNG-|ARCH-|INFRA-|\bADR\b|Decision [0-9]|[Dd]esign [0-9]+|[Oo]ption [0-9]"
    r"|[Pp]hase [0-9]|round [0-9]|§"
    r"|BookStack|windshift|orchestrat|handover|estate|reviewer|ticket"
    r"|tests/|scripts/|app/|\bsee _|\b_[a-z]"
    r"|discomarder|/home/fabian|jupiter|saturn|merkur"
)


def _offending_lines(path: Path) -> list[str]:
    return [
        f"{path.name}:{number}: {line.strip()[:160]}"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if FORBIDDEN.search(line)
    ]


def _offending_unwrapped(path: Path) -> list[str]:
    """The same alphabet, read across line breaks.

    A generated comment wraps the description at spaces, so a two-word form — "design 3285", "see
    `_helper`" — can straddle two lines and pass a line-at-a-time scan. Runs of whitespace and the
    comment's own `*` gutter collapse to ONE space, which rejoins those without gluing a path back
    together across a wrap that never happens mid-token.
    """
    flat = re.sub(r"[\s*]+", " ", path.read_text())
    return [
        f"{path.name}: …{flat[max(0, m.start() - 60) : m.end() + 60]}…"
        for m in FORBIDDEN.finditer(flat)
    ]


def test_the_served_schema_carries_no_internal_reference():
    assert '"paths"' in OPENAPI.read_text()  # positive control: the document was really read
    offenders = _offending_lines(OPENAPI)
    assert offenders == [], (
        f"{len(offenders)} exported description(s) name something internal — rewrite the model or "
        f"route docstring as client-facing prose and move the rationale into a `#` comment, then "
        f"regenerate: {offenders[:3]}"
    )


def test_neither_artifact_hides_an_internal_reference_across_a_line_break():
    for path, control in ((OPENAPI, '"paths"'), (SCHEMA_D_TS, "export interface paths")):
        assert control in path.read_text()  # positive control
        offenders = _offending_unwrapped(path)
        assert offenders == [], (
            f"{len(offenders)} internal reference(s) in {path.name} spanning a wrapped line: "
            f"{offenders[:3]}"
        )


def test_the_generated_frontend_types_carry_no_internal_reference():
    assert "export interface paths" in SCHEMA_D_TS.read_text()  # positive control
    offenders = _offending_lines(SCHEMA_D_TS)
    assert offenders == [], (
        f"{len(offenders)} generated type comment(s) name something internal — the source is a "
        f"docstring in app/, not this file; fix it there and regenerate both artifacts: "
        f"{offenders[:3]}"
    )
