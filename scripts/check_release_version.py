#!/usr/bin/env python3
"""Assert the release version agrees across every source that carries it.

`pyproject.toml` `[project] version` is the single authority; everything else mirrors it:

  * `frontend/package.json`,
  * `frontend/package-lock.json`, which carries the version twice (root `.version` and
    `.packages[""].version`),
  * `uv.lock`, whose `[[package]]` entry for this project embeds it,
  * `README.md`, which must carry a `### v<version>` changelog section,
  * and, at release time, the annotated tag about to be created (`--tag vX.Y.Z`).

The lockfiles are checked here because nothing else checks them: `npm ci` compares the
*dependency* trees, not the root version field (verified — it exits 0 against a lockfile
whose version is stale), and `uv sync --frozen` deliberately skips lock freshness checks.
A bump that misses a lockfile would otherwise ship an image stamped with the old version.

CI runs this without `--tag` (the workflow never fires on tag refs); the release procedure
runs it *with* `--tag` immediately before creating the tag.

Deliberately stdlib-only: the release job installs no uv and calls the runner's `python3`.
"""

import argparse
import json
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _project_table(root: pathlib.Path) -> dict[str, object]:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def authoritative_version(root: pathlib.Path) -> str:
    """The one version of record: pyproject.toml `[project] version`."""
    return str(_project_table(root)["version"])


def disagreements(root: pathlib.Path, tag: str | None = None) -> list[str]:
    """Every way the mirrors disagree with the authority. Empty list == all agree."""
    project = _project_table(root)
    version = str(project["version"])
    name = str(project["name"])
    found: list[str] = []

    def mismatch(source: str, mirrored: object, fix: str) -> str:
        return (
            f"pyproject.toml [project] version = {version!r} but "
            f"{source} = {mirrored!r} (fix: {fix})"
        )

    npm_fix = f"cd frontend && npm version {version} --no-git-tag-version"

    package_json = root / "frontend" / "package.json"
    mirrored = json.loads(package_json.read_text(encoding="utf-8")).get("version")
    if mirrored != version:
        found.append(mismatch("frontend/package.json version", mirrored, npm_fix))

    package_lock = json.loads((root / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    root_package = package_lock.get("packages", {}).get("", {})
    for source, mirrored in (
        ("frontend/package-lock.json .version", package_lock.get("version")),
        ('frontend/package-lock.json .packages[""].version', root_package.get("version")),
    ):
        if mirrored != version:
            found.append(mismatch(source, mirrored, npm_fix))

    uv_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    entries = [p for p in uv_lock.get("package", []) if p.get("name") == name]
    if not entries:
        found.append(
            f"uv.lock has no [[package]] entry named {name!r} to carry version {version!r} "
            f"(fix: uv lock)"
        )
    for entry in entries:
        if entry.get("version") != version:
            found.append(
                mismatch(f"uv.lock [[package]] {name!r} version", entry.get("version"), "uv lock")
            )

    heading = f"### v{version}"
    readme = root / "README.md"
    lines = readme.read_text(encoding="utf-8").splitlines()
    if not any(line.strip() == heading for line in lines):
        found.append(
            f"pyproject.toml [project] version = {version!r} but README.md has no "
            f"{heading!r} changelog section (fix: add one under ## Changelog)"
        )

    if tag is not None and tag != f"v{version}":
        found.append(
            f"tag {tag!r} does not match pyproject.toml [project] version {version!r} "
            f"(expected tag 'v{version}')"
        )

    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the release version agrees across all sources."
    )
    parser.add_argument(
        "--tag",
        metavar="vX.Y.Z",
        help="the tag about to be created; must equal v<pyproject version>",
    )
    args = parser.parse_args(argv)

    found = disagreements(ROOT, args.tag)
    if found:
        print("Release version sources disagree:", file=sys.stderr)
        for line in found:
            print(f"  - {line}", file=sys.stderr)
        return 1

    version = authoritative_version(ROOT)
    scope = [
        "pyproject.toml",
        "frontend/package.json",
        "frontend/package-lock.json",
        "uv.lock",
        "README.md",
    ]
    if args.tag:
        scope.append(f"tag {args.tag}")
    print(f"Release version {version} agrees across: {', '.join(scope)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
