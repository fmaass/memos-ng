"""Filename-convention parsing for the screen/print variant framework.

A logical photo can be dumped as several physical files distinguished by a strict filename suffix:

    _AR55821.png                  → screen (the web master; a normal picture)
    _AR55821 - screen.png         → screen (explicit; identical to no-suffix)
    _AR55821 - print (glossy).png → print rendition, label "glossy"  → variant_key "print:glossy"
    _AR55821 - print.png          → print rendition, no label        → variant_key "print"
    _AR55821 - glossy.png         → print rendition, label "glossy"  → variant_key "print:glossy"
    _AR55821 - matte.png          → print rendition, label "matte"   → variant_key "print:matte"

The bare `- glossy` / `- matte` suffixes are the household Lightroom export convention (a screen
export and a print export dumped side by side as `X - screen` / `X - glossy`); they are exactly
equivalent to `- print (glossy)` / `- print (matte)`. Only these KNOWN labels are treated this way
— an arbitrary `- sunset` stays a visible screen, so a genuine title is never hidden by accident.

Only the `print` role is hidden. ANYTHING that does not match the strict print suffix — no suffix,
` - screen`, an unknown bare label like ` - sunset`, a near-miss like `Trip - print shop`, or a
malformed `… - print ()` — is a normal screen picture. A misparse must never silently hide a file
from the web, so the fallback is always the visible screen variant. Pure + unit-tested; no I/O.
"""

import os
import re
from dataclasses import dataclass

LABEL_MAXLEN = 48  # bounds the slug so variant_key (String(96)) always fits

# Bare print labels the household export convention emits WITHOUT the ` - print (…)` wrapper. Kept a
# closed set on purpose: a bare ` - <word>` only hides the file when <word> is a known print finish,
# so an ordinary title ending ` - <word>` stays a visible screen (the safe failure). Extend here.
KNOWN_PRINT_LABELS = ("glossy", "matte")

# Suffix grammar, anchored at the END of the extension-stripped stem (so ` - print` must sit
# immediately before the extension — `Trip - print shop.png` is a screen, not a print). `\Z` (not
# `$`) so a stem ending in a stray newline isn't matched as a print (`$` matches before a final \n).
# IGNORECASE on the `print`/`screen` keyword so a title-case ` - Print (Glossy)` from a Lightroom
# preset is still hidden — a case slip must NOT silently expose a print as a visible photo. The
# label is matched verbatim then slugged (so its case/spacing is normalized separately).
_PRINT_LABELED = re.compile(r"^(?P<base>.+) - print \((?P<label>[^()]+)\)\Z", re.IGNORECASE)
_PRINT_BARE = re.compile(r"^(?P<base>.+) - print\Z", re.IGNORECASE)
# A bare known-finish suffix (` - glossy` / ` - matte`), anchored like the print suffixes: the label
# must sit immediately before the extension, so `Sunset - glossy morning.png` is a screen, not a
# print. IGNORECASE mirrors the print/screen keywords — a `- Glossy` preset must still be hidden.
_PRINT_KNOWN_LABEL = re.compile(
    r"^(?P<base>.+) - (?P<label>" + "|".join(KNOWN_PRINT_LABELS) + r")\Z", re.IGNORECASE
)
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
    m = _PRINT_KNOWN_LABEL.match(stem)
    if m:
        # A known finish is a real label (glossy/matte slug to non-empty), so it never degrades to a
        # bare print; slug_label only normalizes case (` - Glossy` → print:glossy).
        slug = slug_label(m.group("label"))
        return ParsedName(
            base=m.group("base"), is_print=True, label=slug, variant_key=f"print:{slug}"
        )
    m = _SCREEN_EXPLICIT.match(stem)
    if m:
        return ParsedName(base=m.group("base"), is_print=False, label=None, variant_key=None)
    return ParsedName(base=stem, is_print=False, label=None, variant_key=None)
