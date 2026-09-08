"""The SPA's tag-only-line rule, ported so the digest mail reads like every other client.

A memo whose first line is nothing but hashtags (`#curated`) shows that line as the memo's TITLE
in the digest, because the mail renders the raw Markdown and `#curated` is a level-1 heading to
any Markdown parser. The SPA and the iOS client have never shown it: both drop whole tag-only
lines before rendering (`frontend/src/lib/markdown.ts`) and show the tags as chips below the body
instead. This is that rule, and nothing else — the digest is NOT a second Markdown projection of
the SPA's, and python-markdown's other divergences (task lists, autolinks) are deliberately out of
scope.

Two details are the whole reason this is a module and not a one-liner at the call site:

* **`re.ASCII`.** JavaScript's `\\w` is ASCII-only; Python's matches every unicode word character.
  Without the flag `#café` and `#旅行` would be tag-only lines HERE and prose in the SPA, so the
  mail would drop text every other client shows. The shared vector file carries both as witnesses.
* **A FULL match.** Python's `$` matches just before a trailing newline and JavaScript's
  does not, so anchoring on `$` alone would make `"#curated\\n"` tag-only here and prose in
  the SPA. The artefact's `predicate` section carries that vector.
* **Splitting on `\\n` only.** A CRLF source leaves its `\\r` on the line, and a line ending in
  `\\r` is therefore not tag-only in either implementation — the same behaviour, kept because
  matching it is cheaper than explaining a divergence to a reader comparing two mails.

The rule, its cases and the SPA's own output for each are `fixtures/tag-only-lines.json`, which
both `tests/test_tag_lines.py` and the SPA's `markdown.tagstrip.spec.ts` read. Changing the
pattern here without changing that file is red on both sides.
"""

import re

# The regex source is character-for-character `TAG_ONLY_LINE` in frontend/src/lib/markdown.ts, and
# the artefact records it as data so the two cannot drift silently.
TAG_ONLY_LINE = re.compile(r"^[ \t]*#\w+(?:[ \t]+#\w+)*[ \t]*$", re.ASCII)


def is_tag_only_line(line: str) -> bool:
    """True for a line `strip_tag_only_lines` drops.

    `#word` requires no space after the `#`, so a real Markdown heading (`# Heading`, `## Sub`) is
    NOT one of these, and an inline tag in prose ("see #curated here") is not either.
    """
    # `fullmatch`, not `match`: Python's `$` ALSO matches just before a trailing newline, while
    # JavaScript's (no `m` flag) matches only at the very end of the string — so `"#curated\\n"`
    # would be tag-only here and prose in the SPA. The artefact's `predicate` vectors are that
    # divergence's witnesses; the PATTERN stays character-for-character the SPA's.
    return TAG_ONLY_LINE.fullmatch(line) is not None


def strip_tag_only_lines(text: str) -> str:
    """Drop whole lines that consist solely of hashtags; every other line survives verbatim.

    Whole LINES, not the tags inside them: a memo that says "see #curated here" keeps its sentence.
    """
    return "\n".join(line for line in text.split("\n") if not is_tag_only_line(line))
