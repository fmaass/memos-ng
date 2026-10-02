"""Every patch tests/mutations/expected.tsv names still applies to this checkout (MEMNG-500).

`scripts/check-mutations.sh` runs only at a release, so a merge that rewrote a patch's context
lines used to surface days later, as a failure of a commit that never touched the mutated
function. This check is `git apply --check` alone — no database, nothing written to the tree — so
it can sit in the ordinary gate and turn the MERGING change red instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tests" / "mutations" / "expected.tsv"


def manifest_rows(text: str) -> list[tuple[str, str]]:
    """(patch, marker) per row, read the way scripts/check-mutations.sh reads the manifest:
    one row per line, fields split on TAB, blank lines and lines starting with `#` ignored."""
    rows = []
    for line in text.split("\n"):
        if line == "" or line.startswith("#"):
            continue
        fields = (line.split("\t") + ["", ""])[:2]
        rows.append((fields[0], fields[1]))
    return rows


ROWS = manifest_rows(MANIFEST.read_text())


def test_the_manifest_names_mutations() -> None:
    """An unreadable manifest must not pass as "every one of zero patches applies"."""
    assert ROWS, f"{MANIFEST} names no mutations"


def test_the_manifest_is_read_like_the_script_reads_it() -> None:
    text = (
        "# tests/mutations/commented.patch\tcommented\ttest_x\n"
        "\n"
        "tests/mutations/a.patch\ta\ttest_x\t\ttests/test_other.py\n"
        "tests/mutations/b.patch\tb\ttest_y # not a comment\n"
    )

    assert manifest_rows(text) == [
        ("tests/mutations/a.patch", "a"),
        ("tests/mutations/b.patch", "b"),
    ]


@pytest.mark.parametrize(("patch", "marker"), ROWS, ids=[marker for _, marker in ROWS])
def test_the_patch_still_applies(patch: str, marker: str) -> None:
    assert patch and marker, f"malformed manifest row for {marker or patch!r}"
    assert (REPO / patch).is_file(), f"{marker}: no patch file {patch}"

    check = subprocess.run(
        ["git", "apply", "--check", patch], cwd=REPO, capture_output=True, text=True
    )

    assert check.returncode == 0, (
        f"{marker}: {patch} no longer applies — its context drifted; refresh the patch so the "
        f"mutation still lands where it did.\n{check.stderr}"
    )
