"""The release-version checker must actually catch each way the sources can disagree."""

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_release_version", ROOT / "scripts" / "check_release_version.py"
)
assert _spec is not None and _spec.loader is not None
crv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crv)


def _tree(
    root: pathlib.Path,
    *,
    pyproject: str = "1.4.0",
    package_json: str = "1.4.0",
    package_lock: str = "1.4.0",
    package_lock_root_entry: str | None = None,
    uv_lock: str = "1.4.0",
    readme: str = "1.4.0",
) -> pathlib.Path:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "memos-ng"\nversion = "{pyproject}"\n', encoding="utf-8"
    )
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text(
        json.dumps({"name": "memos-ng-frontend", "version": package_json}), encoding="utf-8"
    )
    (root / "frontend" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "memos-ng-frontend",
                "version": package_lock,
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "memos-ng-frontend",
                        "version": package_lock_root_entry or package_lock,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "alembic"\nversion = "1.14.0"\n\n'
        f'[[package]]\nname = "memos-ng"\nversion = "{uv_lock}"\nsource = {{ editable = "." }}\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"## Changelog\n\n### v{readme}\n\n- **Something.** happened\n", encoding="utf-8"
    )
    return root


def test_authoritative_version_is_pyproject(tmp_path):
    tree = _tree(
        tmp_path,
        pyproject="9.9.9",
        package_json="9.9.9",
        package_lock="9.9.9",
        uv_lock="9.9.9",
        readme="9.9.9",
    )
    assert crv.authoritative_version(tree) == "9.9.9"


def test_all_sources_agree(tmp_path):
    assert crv.disagreements(_tree(tmp_path)) == []


def test_package_json_mirror_out_of_step(tmp_path):
    found = crv.disagreements(_tree(tmp_path, package_json="1.3.0"))
    assert len(found) == 1
    assert "frontend/package.json" in found[0]
    assert "'1.4.0'" in found[0] and "'1.3.0'" in found[0]


def test_stale_package_lock_root_version(tmp_path):
    """npm ci does not police the lockfile's root version field — this check must."""
    tree = _tree(tmp_path, package_lock="1.3.0", package_lock_root_entry="1.4.0")
    found = crv.disagreements(tree)
    assert len(found) == 1
    assert "frontend/package-lock.json .version" in found[0]
    assert "'1.3.0'" in found[0]


def test_stale_package_lock_root_package_entry(tmp_path):
    found = crv.disagreements(_tree(tmp_path, package_lock_root_entry="1.3.0"))
    assert len(found) == 1
    assert 'frontend/package-lock.json .packages[""].version' in found[0]
    assert "'1.3.0'" in found[0]


def test_stale_uv_lock_project_version(tmp_path):
    found = crv.disagreements(_tree(tmp_path, uv_lock="1.3.0"))
    assert len(found) == 1
    assert "uv.lock" in found[0]
    assert "'1.3.0'" in found[0]
    assert "uv lock" in found[0]


def test_uv_lock_without_the_project_entry(tmp_path):
    root = _tree(tmp_path)
    (root / "uv.lock").write_text(
        '[[package]]\nname = "alembic"\nversion = "1.14.0"\n', encoding="utf-8"
    )
    found = crv.disagreements(root)
    assert len(found) == 1
    assert "uv.lock has no [[package]] entry named 'memos-ng'" in found[0]


def test_uv_lock_dependency_versions_are_not_confused_with_the_project(tmp_path):
    """Only the entry named like the project mirrors the release version — deps are free."""
    root = _tree(tmp_path)
    (root / "uv.lock").write_text(
        '[[package]]\nname = "alembic"\nversion = "1.3.0"\n\n'
        '[[package]]\nname = "memos-ng"\nversion = "1.4.0"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    assert crv.disagreements(root) == []


def test_readme_missing_the_changelog_section(tmp_path):
    found = crv.disagreements(_tree(tmp_path, readme="1.3.0"))
    assert len(found) == 1
    assert "README.md" in found[0]
    assert "### v1.4.0" in found[0]


def test_readme_section_must_be_its_own_heading(tmp_path):
    """A version mentioned in prose is not a changelog section."""
    root = _tree(tmp_path)
    (root / "README.md").write_text(
        "## Changelog\n\nsee ### v1.4.0 notes elsewhere\n", encoding="utf-8"
    )
    assert [e for e in crv.disagreements(root) if "README.md" in e]


def test_every_disagreement_is_reported_not_just_the_first(tmp_path):
    tree = _tree(
        tmp_path, package_json="1.3.0", package_lock="1.3.0", uv_lock="1.3.0", readme="1.3.0"
    )
    found = crv.disagreements(tree, tag="v1.2.0")
    # package.json, lock .version, lock .packages[""], uv.lock, README, tag
    assert len(found) == 6


def test_tag_matching_the_authority_passes(tmp_path):
    assert crv.disagreements(_tree(tmp_path), tag="v1.4.0") == []


def test_tag_disagreeing_with_the_authority_fails(tmp_path):
    found = crv.disagreements(_tree(tmp_path), tag="v1.5.0")
    assert len(found) == 1
    assert "'v1.5.0'" in found[0]
    assert "expected tag 'v1.4.0'" in found[0]


def test_no_tag_means_no_tag_check(tmp_path):
    assert crv.disagreements(_tree(tmp_path), tag=None) == []


def test_this_repository_agrees_with_itself():
    """The gate itself, as a test: the real files must always agree."""
    assert crv.disagreements(ROOT) == []
