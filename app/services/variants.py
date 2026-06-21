"""Filename-convention parsing for the screen/print variant framework.

A logical photo can be dumped as several physical files distinguished by a strict filename suffix:

    _AR55821.png                  → screen (the web master; a normal picture)
    _AR55821 - screen.png         → screen (explicit; identical to no-suffix)
    _AR55821 - print (glossy).png → print rendition, label "glossy"  → variant_key "print:glossy"
    _AR55821 - print.png          → print rendition, no label        → variant_key "print"

Only the `print` role is hidden. ANYTHING that does not match the strict print suffix — no suffix,
` - screen`, a near-miss like `Trip - print shop`, or a malformed `… - print ()` — is a normal
screen picture. A misparse must never silently hide a file from the web, so the fallback is always
the visible screen variant. Pure + unit-tested; no I/O.
"""

import os
import re
from dataclasses import dataclass

LABEL_MAXLEN = 48  # bounds the slug so variant_key (String(96)) always fits

# Suffix grammar, anchored at the END of the extension-stripped stem (so ` - print` must sit
# immediately before the extension — `Trip - print shop.png` is a screen, not a print). `\Z` (not
# `$`) so a stem ending in a stray newline isn't matched as a print (`$` matches before a final \n).
# IGNORECASE on the `print`/`screen` keyword so a title-case ` - Print (Glossy)` from a Lightroom
# preset is still hidden — a case slip must NOT silently expose a print as a visible photo. The
# label is matched verbatim then slugged (so its case/spacing is normalized separately).
_PRINT_LABELED = re.compile(r"^(?P<base>.+) - print \((?P<label>[^()]+)\)\Z", re.IGNORECASE)
_PRINT_BARE = re.compile(r"^(?P<base>.+) - print\Z", re.IGNORECASE)
_SCREEN_EXPLICIT = re.compile(r"^(?P<base>.+) - screen\Z", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedName:
    base: str  # groups a print to its screen (both yield the same base)
    is_print: bool
    label: str | None  # slugified sub-name (glossy/matte); None for a bare print or a screen
    variant_key: str | None  # "print" / "print:<slug>" for a print; None for a screen


def slug_label(label: str) -> str:
    """Normalize a print label to a path/URL-safe slug: trim, lowercase, collapse any run of
    non-[a-z0-9] to a single '-', strip leading/trailing '-', and bound the length. May return ''
    (e.g. a label of only punctuation) — the caller treats an empty slug as a bare `print`."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug[:LABEL_MAXLEN].rstrip("-")


def _stem(filename: str) -> str:
    """The basename with a single trailing extension removed (dots in the base are preserved)."""
    return os.path.splitext(os.path.basename(filename))[0]


def parse_variant(filename: str) -> ParsedName:
    """Classify a dumped filename. Print iff it matches the strict ` - print`/` - print (label)`
    suffix; everything else (incl. malformed) is a visible screen."""
    stem = _stem(filename)
    m = _PRINT_LABELED.match(stem)
    if m:
        slug = slug_label(m.group("label"))
        key = f"print:{slug}" if slug else "print"
        return ParsedName(base=m.group("base"), is_print=True, label=slug or None, variant_key=key)
    m = _PRINT_BARE.match(stem)
    if m:
        return ParsedName(base=m.group("base"), is_print=True, label=None, variant_key="print")
    m = _SCREEN_EXPLICIT.match(stem)
    if m:
        return ParsedName(base=m.group("base"), is_print=False, label=None, variant_key=None)
    return ParsedName(base=stem, is_print=False, label=None, variant_key=None)
