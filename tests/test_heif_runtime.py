"""The shipped HEIC decoder is pi-heif, and the GPLv2 x265 encoder in pillow-heif never ships.

pillow-heif's wheel bundles x265 (GPLv2); its decode-only sibling pi-heif bundles libheif and
libde265 and nothing else. The server only decodes HEIC, so the runtime closure carries pi-heif and
pillow-heif lives in the dev group as the encoder test fixtures need. Each check below reads
structure (the lockfile graph, the import AST, Pillow's plugin registry), never text, because the
words "pillow-heif" legitimately appear in comments explaining exactly this.
"""

import ast
import tomllib
from pathlib import Path

from app.services import media  # noqa: F401  (registers the runtime HEIF opener)
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
HEIC_CORPUS = sorted((REPO / "fixtures").rglob("*.heic"))


def _runtime_closure() -> set[str]:
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    name = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["name"]
    packages = {p["name"]: p for p in lock["package"]}
    seen: set[str] = set()
    todo = [name]
    while todo:
        pkg = packages[todo.pop()]
        if pkg["name"] in seen:
            continue
        seen.add(pkg["name"])
        for dep in pkg.get("dependencies", []):
            todo.append(dep["name"])
            for extra in dep.get("extra", []):
                todo.extend(
                    d["name"] for d in packages[dep["name"]]["optional-dependencies"][extra]
                )
    return seen


def _imported_modules(root: Path) -> list[tuple[str, str]]:
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Import):
                found += [(str(path), a.name.split(".")[0]) for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.append((str(path), node.module.split(".")[0]))
    return found


def test_runtime_closure_carries_pi_heif_and_not_pillow_heif():
    closure = _runtime_closure()
    assert "pi-heif" in closure
    assert "pillow-heif" not in closure


def test_app_never_imports_pillow_heif():
    imports = _imported_modules(REPO / "app")
    assert any(mod == "pi_heif" for _, mod in imports)  # control: the scan sees the real import
    assert [p for p, mod in imports if mod == "pillow_heif"] == []


def test_no_test_registers_the_pillow_heif_opener():
    # Registering pillow-heif's opener anywhere in the suite would silently route every later
    # decode through the encoder build, so the runtime pi-heif path would go untested.
    calls = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_heif_opener"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pillow_heif"
            ):
                calls.append(f"{path}:{node.lineno}")
    assert calls == []


def test_registered_heif_opener_is_pi_heif():
    assert Image.registered_extensions()[".heic"] == "HEIF"
    assert Image.OPEN["HEIF"][0].__module__.startswith("pi_heif")


def test_committed_heic_corpus_decodes_through_pi_heif():
    assert HEIC_CORPUS  # an empty corpus would pass every loop below vacuously
    for path in HEIC_CORPUS:
        with Image.open(path) as im:
            im.load()
            assert type(im).__module__.startswith("pi_heif"), path
            assert im.size[0] > 0 and im.size[1] > 0, path
