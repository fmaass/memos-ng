"""The tag-only-line rule, held to the SAME vectors as the SPA (MEMNG-275).

Every expectation comes out of `fixtures/tag-only-lines.json` — the one normative artefact the
SPA's vitest suite produces and this port obeys. Nothing restates a rule locally: a vector changes
in the artefact or it does not change (the pattern `tests/test_capture.py` established for
`fixtures/auto-memo-date.json`).

The unicode cases are the load-bearing ones. JavaScript's `\\w` is ASCII, Python's is not, so a
port compiled without `re.ASCII` would strip `#café` and `#旅行` — lines the SPA keeps as prose —
and the digest would silently lose a memo's text. They are what makes a green run mean the two
implementations agree rather than merely both run.
"""

import json
import re
from pathlib import Path

import pytest
from app.services import tag_lines

ROOT = Path(__file__).resolve().parent.parent
ARTEFACT = json.loads((ROOT / "fixtures" / "tag-only-lines.json").read_text(encoding="utf-8"))
CASES = ARTEFACT["cases"]
PREDICATE = ARTEFACT["predicate"]


def _ids(cases: list[dict]) -> list[str]:
    return [case["id"] for case in cases]


def test_the_artefact_is_the_document_this_suite_thinks_it_is():
    """A loader that read the wrong file, or an emptied one, would make every case below pass
    vacuously — so the shape is asserted before anything is derived from it."""
    assert ARTEFACT["schema"] == 1
    assert len(CASES) >= 15
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert {"unicode-accented-tag-kept", "unicode-cjk-tag-kept"} <= set(_ids(CASES))
    assert len(PREDICATE) >= 11
    assert "predicate-trailing-lf" in set(_ids(PREDICATE))


def test_the_compiled_rule_is_the_one_the_artefact_records():
    """The regex SOURCE, character for character — the same equality the vitest spec asserts, so
    the two implementations cannot drift apart without this going red."""
    assert tag_lines.TAG_ONLY_LINE.pattern == ARTEFACT["rule"]
    assert tag_lines.TAG_ONLY_LINE.flags & re.ASCII


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_strip_tag_only_lines_matches_the_artefact(case: dict):
    assert tag_lines.strip_tag_only_lines(case["input"]) == case["stripped"]


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_is_tag_only_line_decides_every_line_of_every_case(case: dict):
    """The per-line predicate, composed back into the whole-source result the artefact records —
    so the predicate is pinned line by line and not only through `strip_tag_only_lines`."""
    kept = [line for line in case["input"].split("\n") if not tag_lines.is_tag_only_line(line)]
    assert "\n".join(kept) == case["stripped"]


def test_a_source_of_only_tags_is_emptied_and_prose_is_untouched():
    """Literal cases, so a broken artefact loader cannot make this file pass by testing nothing at
    all."""
    assert tag_lines.strip_tag_only_lines("#curated #trip") == ""
    assert tag_lines.strip_tag_only_lines("a paragraph") == "a paragraph"
    assert tag_lines.strip_tag_only_lines("") == ""


@pytest.mark.parametrize("case", PREDICATE, ids=_ids(PREDICATE))
def test_the_predicate_answers_every_whole_string_as_the_spa_does(case: dict):
    """Whole strings, not lines of a split: the predicate is exported and a caller may hand it a
    string carrying its own terminator. `"#curated\\n"` is the one that separates a `$` anchor
    from a full match, and the SPA's answer for each of these was recorded by running it."""
    assert tag_lines.is_tag_only_line(case["line"]) is case["tag_only"]


def test_a_trailing_newline_is_not_a_tag_only_line():
    """The literal form of the vector above, so an emptied `predicate` section cannot make the
    parametrised test disappear silently."""
    assert tag_lines.is_tag_only_line("#curated") is True
    assert tag_lines.is_tag_only_line("#curated\n") is False
