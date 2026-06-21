import pytest
from app.services.variants import LABEL_MAXLEN, parse_variant, slug_label


def test_no_suffix_is_screen():
    p = parse_variant("_AR55821.png")
    assert p.base == "_AR55821" and not p.is_print
    assert p.label is None and p.variant_key is None


def test_explicit_screen_suffix():
    p = parse_variant("_AR55821 - screen.png")
    assert p.base == "_AR55821" and not p.is_print and p.variant_key is None


def test_print_bare():
    p = parse_variant("_AR55821 - print.png")
    assert p.base == "_AR55821" and p.is_print
    assert p.label is None and p.variant_key == "print"


def test_print_labeled():
    p = parse_variant("_AR55821 - print (glossy).png")
    assert p.base == "_AR55821" and p.is_print
    assert p.label == "glossy" and p.variant_key == "print:glossy"


def test_screen_and_print_share_a_base():
    # the grouping key: a screen and its print must yield the SAME base
    assert parse_variant("_AR55821.png").base == parse_variant("_AR55821 - print (matte).png").base
    assert parse_variant("X - screen.png").base == parse_variant("X - print.png").base


def test_near_miss_is_a_screen_not_a_print():
    # ' - print' must sit immediately before the extension; mid-name occurrences don't count
    for name in ("Trip - print shop mockup.png", "my - print - notes.png", "printout.png"):
        p = parse_variant(name)
        assert not p.is_print, name
        assert p.base == name.rsplit(".", 1)[0]


def test_malformed_print_falls_back_to_screen():
    # empty parens / unbalanced → never silently hidden; treated as a visible screen
    for name in ("X - print ().png", "X - print (a) (b).png", "X - print (.png"):
        assert not parse_variant(name).is_print, name


def test_trailing_newline_is_not_a_print():
    # `$` matches before a final \n in Python; `\Z` must not, or a crafted name would hide silently
    for name in ("X - print (matte)\n.png", "X - print\n.png"):
        assert not parse_variant(name).is_print, repr(name)


def test_label_is_slugified():
    assert parse_variant("X - print (Glossy Finish).png").variant_key == "print:glossy-finish"
    assert parse_variant("X - print (MATTE!!).png").variant_key == "print:matte"


def test_over_length_label_is_truncated():
    long = "a" * 100
    key = parse_variant(f"X - print ({long}).png").variant_key
    assert key is not None
    slug = key.split(":", 1)[1]
    assert len(slug) == LABEL_MAXLEN and slug == "a" * LABEL_MAXLEN


def test_punctuation_only_label_degrades_to_bare_print():
    # a label that slugs to '' → bare print key (not "print:")
    p = parse_variant("X - print (___).png")
    assert p.variant_key == "print" and p.label is None


def test_base_dots_and_basename_are_handled():
    # only the final extension is stripped; a path basename is taken
    assert parse_variant("a.b.c - print (x).png").base == "a.b.c"
    assert parse_variant("folder/sub/_AR1 - print (x).png").base == "_AR1"
    assert parse_variant("noext - print (x)").base == "noext"  # no extension at all


def test_role_keyword_is_case_insensitive():
    # a title-case / upper-case keyword (e.g. a Lightroom preset emitting ' - Print (Glossy)') must
    # still be hidden — a case slip must never silently expose a print as a visible photo
    for name in (
        "_AR1 - Print (Glossy).png",
        "_AR1 - PRINT (glossy).png",
        "_AR1 - print (GLOSSY).png",
    ):
        p = parse_variant(name)
        assert p.is_print and p.variant_key == "print:glossy", name
    assert parse_variant("X - PRINT.png").is_print
    assert parse_variant("X - Print.png").variant_key == "print"
    assert not parse_variant("X - Screen.png").is_print  # explicit screen, any case → visible


def test_extension_is_case_and_format_agnostic():
    # only the final extension is stripped, regardless of case/format (TIFF/DNG/JPEG raws etc.)
    for ext in (".png", ".PNG", ".tiff", ".TIFF", ".dng", ".jpeg"):
        p = parse_variant(f"_AR1 - print (glossy){ext}")
        assert p.is_print and p.base == "_AR1" and p.variant_key == "print:glossy", ext


def test_strict_structure_missing_space_stays_a_visible_screen():
    # the convention requires ' - print (label)' with the spaces; a near-miss is treated as a
    # visible screen (the SAFE failure — never hide on an ambiguous name), not a print
    for name in ("X - print(glossy).png", "X-print (glossy).png", "X -print (glossy).png"):
        assert not parse_variant(name).is_print, name


def test_base_may_contain_its_own_separator():
    # the base is greedy, so a name whose base legitimately contains ' - ' still groups correctly
    p = parse_variant("Mom - birthday - print (glossy).png")
    assert p.is_print and p.base == "Mom - birthday" and p.variant_key == "print:glossy"
    # and the screen of the same logical photo shares that base
    assert parse_variant("Mom - birthday.png").base == p.base


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("glossy", "glossy"),
        ("  Glossy  ", "glossy"),
        ("Matte Finish", "matte-finish"),
        ("a/b:c", "a-b-c"),
        ("--x--", "x"),
        ("###", ""),
    ],
)
def test_slug_label(raw, expected):
    assert slug_label(raw) == expected
