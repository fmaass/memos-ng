"""The digest's words and dates in the recipient's language (MEMNG-1245).

One JSON catalogue per language beside this module, shipped in the image, flat `key -> text` with
`{name}` placeholders: plain data, so the door (DD-2) reads the same files rather than a second
copy of the words. Memo text and tags are never translated — only what the mail itself says.

Dates go through Babel with the CLDR pattern each catalogue names, so a weekday and a month are
spelled the way the language spells them. The English patterns reproduce, byte for byte, what the
household has been receiving (`strftime("%A, %B %-d, %Y")` and the satellite's date range).
"""

import json
import string
from datetime import date
from functools import lru_cache
from pathlib import Path

from babel.dates import format_date

from app.models import DIGEST_DEFAULT_LANGUAGE, DIGEST_LANGUAGES

LANGUAGES: tuple[str, ...] = DIGEST_LANGUAGES
DEFAULT_LANGUAGE: str = DIGEST_DEFAULT_LANGUAGE.value
LOCALE_DIR = Path(__file__).parent / "locales"

Catalogues = dict[str, dict[str, str]]


@lru_cache(maxsize=1)
def _load() -> Catalogues:
    # Read once per process: the files ship inside the image and cannot change under it.
    return {
        language: json.loads((LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
        for language in LANGUAGES
    }


def load_catalogues() -> Catalogues:
    return _load()


def parity_gaps(catalogues: Catalogues) -> dict[str, set[str]]:
    """Per language, the keys some OTHER language has and this one lacks. Empty means parity."""
    every = set().union(*(set(entries) for entries in catalogues.values()))
    gaps = {language: every - set(entries) for language, entries in catalogues.items()}
    return {language: missing for language, missing in gaps.items() if missing}


def _placeholders(value: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(value) if name}


def placeholder_gaps(catalogues: Catalogues) -> dict[str, set[str]]:
    """Per language, the keys whose `{placeholders}` differ from the English entry's."""
    reference = catalogues["en"]
    gaps: dict[str, set[str]] = {}
    for language, entries in catalogues.items():
        wrong = {
            key
            for key, value in entries.items()
            if key in reference and _placeholders(value) != _placeholders(reference[key])
        }
        if wrong:
            gaps[language] = wrong
    return gaps


def checked_language(language: str) -> str:
    if language not in LANGUAGES:
        raise ValueError(f"unknown digest language {language!r}")
    return language


def text(language: str, key: str, **values: object) -> str:
    entry = _load()[checked_language(language)][key]
    return entry.format(**values) if values else entry


def _pattern(language: str, key: str) -> str:
    return _load()[checked_language(language)][key]


def _format(value: date, language: str, key: str) -> str:
    return format_date(value, format=_pattern(language, key), locale=language)


def day_label(value: date, language: str) -> str:
    return _format(value, language, "date.day")


def date_range(lo: date, hi: date, language: str) -> str:
    """The subject's date range: one day, a span inside one month, or a span across months."""
    if lo == hi:
        return _format(lo, language, "date.single")
    if lo.year == hi.year and lo.month == hi.month:
        prefix = "date.range_same_month"
        start, end = (
            _format(lo, language, f"{prefix}_start"),
            _format(hi, language, f"{prefix}_end"),
        )
        return f"{start}{_pattern(language, f'{prefix}_joiner')}{end}"
    start, end = _format(lo, language, "date.range_start"), _format(hi, language, "date.range_end")
    return f"{start}{_pattern(language, 'date.range_joiner')}{end}"


def summary(language: str, memo_count: int, day_count: int) -> str:
    """The HTML footer line, with the singular/plural each language needs."""
    memos = text(
        language, "mail.memos_one" if memo_count == 1 else "mail.memos_other", count=memo_count
    )
    days = text(language, "mail.days_one" if day_count == 1 else "mail.days_other", count=day_count)
    return text(language, "mail.summary", memos=memos, days=days)
