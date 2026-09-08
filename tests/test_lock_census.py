"""The explicit-lock census, as an executable check (MEMNG-267 §1.5).

`docs/lock-census.tsv` lists every place in `app/` and `scripts/` that asks Postgres for a row
lock. The point of committing it is that adding a lock site becomes a diff to a reviewed file
instead of a line nobody notices: this test walks the same trees with `ast` and fails when the two
disagree in either direction.

Three properties make it a gate rather than a decoration, and all three are asserted here:

* it compares MULTIPLICITIES, not sets. Twenty-eight sites collapse to twenty-three distinct
  `path/def/mode` triples — `attach_rendition` alone takes two — so a set comparison would let a
  SECOND acquisition inside an already-listed function land unnoticed, which is exactly the shape
  of change the protocol cares about;
* a POSITIVE control: the scan must find the two sites the protocol is written around. A scanner
  that has quietly stopped parsing reports "no new sites" just as cheerfully as a clean tree, and
  those two are the ones whose absence proves it broke;
* a MUTATION control: a throwaway module with one lock in it, planted on the scan path, must come
  back as an uncensused site. A checker that cannot fail is not a check.

Stated limitation, repeated in `docs/LOCK-ORDER.md`: this covers EXPLICIT acquisitions only. The
implicit writers — UPDATE, DELETE, autoflush, FK and unique arbitration — are covered by review
and by the concurrency probes of `tests/test_lock_protocol_concurrency.py`, not by any grep.

Regenerate the TSV after a deliberate lock change (never by hand):

    uv run python -m tests.test_lock_census > docs/lock-census.tsv
"""

import ast
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("app", "scripts")
CENSUS = REPO / "docs" / "lock-census.tsv"
HEADER = ("path", "enclosing def", "mode")

Site = tuple[str, str, str]


class _LockSiteVisitor(ast.NodeVisitor):
    """Collect `(line, enclosing def, mode)` for every explicit lock request in one module.

    Two spellings, deliberately kept apart in the census because they behave differently: a
    `.with_for_update(...)` on a select is an ACQUISITION of whatever that statement returns, while
    `refresh(obj, [...], with_for_update=True)` is a locking re-read of ONE already-known row.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str, str]] = []
        self._scope: list[str] = []

    def _in_scope(self, node) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _in_scope
    visit_AsyncFunctionDef = _in_scope
    visit_ClassDef = _in_scope

    def _where(self) -> str:
        return ".".join(self._scope) if self._scope else "<module>"

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "with_for_update":
            self.found.append((node.lineno, self._where(), "for_update"))
        for keyword in node.keywords:
            if keyword.arg == "with_for_update":
                self.found.append((node.lineno, self._where(), "refresh"))
        self.generic_visit(node)


def scan(roots: list[Path], base: Path) -> Counter[Site]:
    """Every explicit lock site under `roots`, keyed by `(path relative to base, def, mode)`.

    A Counter rather than a set: the count IS part of the census (see the module docstring).
    """
    sites: Counter[Site] = Counter()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            visitor = _LockSiteVisitor()
            visitor.visit(ast.parse(path.read_text(), filename=str(path)))
            rel = path.relative_to(base).as_posix()
            for _line, where, mode in visitor.found:
                sites[(rel, where, mode)] += 1
    return sites


def scan_rows(roots: list[Path], base: Path) -> list[Site]:
    """The same sites as `scan`, one row per site, ordered by path then line — the TSV's order."""
    rows: list[tuple[str, int, str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            visitor = _LockSiteVisitor()
            visitor.visit(ast.parse(path.read_text(), filename=str(path)))
            rel = path.relative_to(base).as_posix()
            rows.extend((rel, line, where, mode) for line, where, mode in visitor.found)
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(path, where, mode) for path, _line, where, mode in rows]


def _tree_sites() -> Counter[Site]:
    return scan([REPO / root for root in SCAN_ROOTS], REPO)


def _census_sites() -> Counter[Site]:
    lines = CENSUS.read_text().splitlines()
    assert tuple(lines[0].split("\t")) == HEADER, lines[0]
    sites: Counter[Site] = Counter()
    for line in lines[1:]:
        if not line.strip():
            continue
        path, where, mode = line.split("\t")
        sites[(path, where, mode)] += 1
    return sites


def test_the_census_scanner_finds_the_known_sites():
    """Positive control. These two are the sites the protocol is written around — the memo lock
    every edit starts at, and reclaim's job-then-attachment pair. If the scanner cannot see them
    it is broken, and this test says SO rather than reporting a clean tree."""
    found = _tree_sites()
    assert found[("app/services/memos.py", "lock_memo_for_write", "for_update")] == 1, (
        "the scanner did not find lock_memo_for_write — it is broken, not the tree"
    )
    assert found[("app/services/reclaim.py", "reclaim_attachment", "for_update")] == 2, (
        "the scanner did not find reclaim_attachment's job and attachment locks — it is broken"
    )


def test_every_explicit_lock_site_is_in_the_census():
    """The census and the tree agree, site for site AND count for count."""
    found = _tree_sites()
    census = _census_sites()
    assert found - census == Counter(), (
        "lock sites the census does not name (add them to docs/lock-census.tsv and say in "
        f"docs/LOCK-ORDER.md where they sit in the order): {sorted(found - census)}"
    )
    assert census - found == Counter(), (
        f"census rows with no lock site behind them any more: {sorted(census - found)}"
    )


def test_the_census_scanner_finds_a_planted_site(tmp_path):
    """Mutation control. One extra lock site, planted in a throwaway package on the scan path,
    must come back from the SAME comparison the gate above runs — as that site and nothing else."""
    planted = tmp_path / "planted_pkg"
    planted.mkdir()
    (planted / "__init__.py").write_text("")
    (planted / "uncensused_site.py").write_text(
        "from sqlalchemy import select\n"
        "\n"
        "from app.models import Memo\n"
        "\n"
        "\n"
        "async def takes_a_lock(db, memo_id):\n"
        "    return await db.execute(select(Memo).where(Memo.id == memo_id).with_for_update())\n"
    )
    found = _tree_sites() + scan([planted], tmp_path)
    census = _census_sites()
    assert found - census == Counter(
        {("planted_pkg/uncensused_site.py", "takes_a_lock", "for_update"): 1}
    ), sorted(found - census)
    assert census - found == Counter()


if __name__ == "__main__":
    print("\t".join(HEADER))
    for row in scan_rows([REPO / root for root in SCAN_ROOTS], REPO):
        print("\t".join(row))
