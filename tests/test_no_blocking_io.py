"""Guard: no async function under app/ performs blocking filesystem I/O inline (MEMNG-42).

A blocking `stat`/`mkdir`/`unlink`/`open` on the request loop stalls EVERY in-flight request,
and this deployment's data volume is a bind-mounted share where each of those is a network
round-trip. The project convention is to name a `_..._sync` helper and run it through
`from anyio import to_thread` → `to_thread.run_sync(...)`; this test fails when a new inline
site appears, so the class cannot silently come back one call at a time.

The scan is an AST walk, not a grep, so it sees three things a grep cannot:

* **scope** — a blocking call inside a nested `def`/`lambda` (i.e. the body of a helper that is
  itself handed to `run_sync`) is correctly NOT a violation;
* **offloading** — `to_thread.run_sync(_sniff_mime, part, mime)` passes a *reference*, so no call
  node exists to flag, while a bare `_sniff_mime(part, mime)` is flagged;
* **transitivity** — an async function that calls a same-module sync helper which blocks is just
  as stalled as one that blocks directly (this is how `_serve_file`'s two `exists()` calls hid
  from the original ticket's line citations).

Deliberately kept inline sites are exempted by a `# blocking-ok: <reason>` marker on the call's
own line or anywhere in the contiguous comment block directly above it. The reason is mandatory
(a bare marker does not exempt), and every exemption is additionally pinned in
EXPECTED_EXEMPTIONS below — so silencing a new site takes a visible edit here, and the escape
hatch cannot be used to quietly re-open the class.

Known bounds, stated honestly rather than papered over:
* transitivity is resolved WITHIN a module only; a blocking helper imported from another module
  and called inline would not be caught (no such site exists today — app.services.storage,
  app.services.frame_share and app.services.asset_urls expose only offloaded or pure helpers).
* `.read()`/`.write()`/`.close()` on file objects are not patterns here: `UploadFile.read` is
  *async* in Starlette, so matching them would be noise, and the file-object work in this codebase
  already lives inside `_..._sync` helpers.
* `Path.replace` is not a pattern either — `str.replace` is far more common and indistinguishable
  in an AST. `os.replace` (the form this codebase uses) is matched instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Path methods that hit the filesystem. Names shared with common non-Path objects (`replace`,
# `read`, `write`, `close`) are excluded — see the module docstring.
PATH_METHODS = frozenset(
    {
        "mkdir",
        "touch",
        "stat",
        "lstat",
        "exists",
        "is_file",
        "is_dir",
        "unlink",
        "rmdir",
        "rename",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "open",
        "iterdir",
        "glob",
        "rglob",
        "resolve",
        "samefile",
        "chmod",
        "symlink_to",
        "hardlink_to",
    }
)
OS_FUNCS = frozenset(
    {
        "replace",
        "rename",
        "remove",
        "unlink",
        "mkdir",
        "makedirs",
        "rmdir",
        "stat",
        "lstat",
        "listdir",
        "scandir",
        "walk",
        "chmod",
        "utime",
        "link",
        "symlink",
        "readlink",
        "truncate",
        "fsync",
        "access",
        "open",
    }
)
OS_PATH_FUNCS = frozenset({"exists", "isfile", "isdir", "getsize", "getmtime", "islink"})
SHUTIL_FUNCS = frozenset(
    {"copy", "copy2", "copyfile", "copytree", "move", "rmtree", "disk_usage", "copyfileobj"}
)
# `select(...).where(...).exists()` is a SQL construct, not a Path — the shared method name is the
# one real false positive in this codebase (app/services/media.py sweep_stale_progress).
SQL_ROOTS = frozenset({"select", "insert", "update", "delete", "exists", "text", "sa_delete"})

MARKER = "blocking-ok:"

# Every deliberately-inline site, as (module, enclosing async function, call) with its
# MULTIPLICITY — a plain set would collapse repeated identities, letting a new marked call that
# shares an existing tuple pass without a registry edit. Pinned so that adding a `# blocking-ok:`
# marker anywhere else fails this test rather than passing silently.
EXPECTED_EXEMPTIONS: dict[tuple[str, str, str], int] = dict(
    {
        # --- worker process only: app/worker.py and app/services/media.py are imported by nothing
        # else under app/ (verified), so none of this runs on the request loop. worker.run() is no
        # longer a single-job loop — it is the SUPERVISOR, and concurrent consumer tasks now share
        # its event loop — so the old justification ("no concurrent tasks, delays nobody") is
        # retired. The stamp stays inline for two reasons that survive the split: the supervisor
        # holds no job work of its own, so the write delays only the supervisor's own next pass;
        # and a heartbeat deferred to a thread can be reordered behind the very stall it exists to
        # make visible (see beat()). The consumers' own CPU-bound blocking-ok sites below DO now
        # delay the other consumer, and that back-pressure is the accepted trade of the split.
        ("app/worker.py", "run", "beat"): 3,
        ("app/services/media.py", "guard_publish", "discard_outputs"): 1,
        ("app/services/media.py", "process_slow", "too_hot"): 1,
        ("app/services/media.py", "process_slow", "mkdir"): 1,
        # TWO: the partial's unlink in the run's `finally`, and the finished temp's on the stop
        # path at the publish checkpoint — both synchronous for the same reason, that an await
        # there can be interrupted before the unlink ever happens.
        ("app/services/media.py", "process_slow", "unlink"): 2,
        ("app/services/media.py", "process_slow", "os.replace"): 1,
        ("app/services/media.py", "process_slow", "_stamp_mtime"): 1,
        # ONE, not two: only the FAST lane's Pillow decode is still inline. The slow lane's heavy
        # image (HEIC/AVIF) decode goes through to_thread.run_sync — it is the light consumer's
        # longest hold, and "nothing else is queued on this loop" is exactly the argument the
        # consumer split retired above.
        ("app/services/media.py", "process_job", "process_image"): 1,
        ("app/services/media.py", "process_job", "process_video_poster"): 1,
        # The print encode itself is NO LONGER exempt — it goes through to_thread.run_sync, so it
        # produces no marker and must not be declared here. Only the thermal guard stays inline:
        # one sysfs read (microseconds), before any work is claimed.
        ("app/services/media.py", "process_printready_job", "too_hot"): 1,
        ("app/services/media.py", "_await_group_exit", "_group_alive"): 1,
        ("app/services/media.py", "_stop_transcode", "_group_alive"): 1,
        # --- not filesystem calls at all: `ZipFile.open(...)` opens a zip ENTRY over the export's
        # in-memory sink. The scanner matches on the attribute name (`open`), which it cannot
        # distinguish from `Path.open`; the real files in that module are opened and read off-loop.
        ("app/services/export.py", "_media_entry", "open"): 1,
        ("app/services/export.py", "_manifest_entry", "open"): 1,
        # Worker only: a rolled-back trip filing walks its files back to staging inline, for the
        # same reason as the memo router's `_restore_moved` — a cancellation may be unwinding.
        # Twice: the rolled-back filing, and the interrupted COMMIT read back as not committed.
        ("app/services/trip_ingest.py", "_file_part", "_restore"): 2,
    }
)


def _chain_root(node: ast.AST) -> ast.AST:
    """The base expression an attribute/call chain hangs off: `select(x).where(y).exists()` → the
    `select(x)` Call; `part.stat()` → the `part` Name."""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return node


def _is_sql_chain(func: ast.Attribute) -> bool:
    root = _chain_root(func.value)
    return isinstance(root, ast.Name) and root.id in SQL_ROOTS


def _blocking_label(node: ast.Call) -> str | None:
    """The blocking-fs label for a call node, or None if it isn't one."""
    f = node.func
    if isinstance(f, ast.Name):
        return "open" if f.id == "open" else None
    if not isinstance(f, ast.Attribute):
        return None
    base = f.value
    if isinstance(base, ast.Name):
        if base.id == "os" and f.attr in OS_FUNCS:
            return f"os.{f.attr}"
        if base.id == "shutil" and f.attr in SHUTIL_FUNCS:
            return f"shutil.{f.attr}"
    if (
        isinstance(base, ast.Attribute)
        and base.attr == "path"
        and isinstance(base.value, ast.Name)
        and base.value.id == "os"
        and f.attr in OS_PATH_FUNCS
    ):
        return f"os.path.{f.attr}"
    if f.attr in PATH_METHODS and not _is_sql_chain(f):
        return f.attr
    return None


class _CallCollector(ast.NodeVisitor):
    """Calls made directly in a function body — NOT those inside a nested def/lambda, whose body
    is by definition somewhere else's execution context (typically a `run_sync` payload)."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def _skip_nested(self, node: ast.AST) -> None:
        return  # a nested function's body belongs to that function, not this one

    visit_FunctionDef = _skip_nested
    visit_AsyncFunctionDef = _skip_nested
    visit_Lambda = _skip_nested

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)


def _direct_calls(fn: ast.AST) -> list[ast.Call]:
    collector = _CallCollector()
    for stmt in getattr(fn, "body", []):
        collector.visit(stmt)
    return collector.calls


def _called_names(node: ast.Call) -> str | None:
    """The bare name of a plain `helper(...)` call, else None."""
    return node.func.id if isinstance(node.func, ast.Name) else None


def _blocking_sync_functions(tree: ast.Module) -> set[str]:
    """Module-level sync functions that block, resolved to a fixpoint through their own calls."""
    sync_defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    blocking = {
        name
        for name, fn in sync_defs.items()
        if any(_blocking_label(c) is not None for c in _direct_calls(fn))
    }
    changed = True
    while changed:
        changed = False
        for name, fn in sync_defs.items():
            if name in blocking:
                continue
            for call in _direct_calls(fn):
                callee = _called_names(call)
                if callee in blocking:
                    blocking.add(name)
                    changed = True
                    break
    return blocking


def _exempt(lines: list[str], lineno: int) -> bool:
    """Whether a `# blocking-ok: <reason>` covers the call on `lineno`.

    The marker may sit on the call's own line or anywhere in the contiguous comment block
    immediately above it — this codebase explains itself in multi-line comments, and requiring
    the marker on the last line of one would read backwards. The block scan stops at the first
    non-comment line, so a marker can never reach across unrelated code. The reason after the
    colon is mandatory: a bare marker exempts nothing.
    """
    idx = lineno - 1  # 0-based index of the call's own line
    while idx >= 0:
        _, sep, tail = lines[idx].partition(MARKER)
        if sep and tail.strip():
            return True
        idx -= 1
        if idx < 0 or not lines[idx].strip().startswith("#"):
            return False
    return False


Violation = tuple[str, str, str, int]
Exemption = tuple[str, str, str]


def scan_source(source: str, module: str) -> tuple[list[Violation], list[Exemption]]:
    """Return (violations, exemptions) for one module's source.

    Each entry is (module, enclosing async function, call label[, line]).
    """
    tree = ast.parse(source, module)
    lines = source.splitlines()
    blocking_helpers = _blocking_sync_functions(tree)
    violations: list[Violation] = []
    exemptions: list[Exemption] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in _direct_calls(node):
            label = _blocking_label(call)
            if label is None:
                callee = _called_names(call)
                if callee is None or callee not in blocking_helpers:
                    continue
                label = callee
            if _exempt(lines, call.lineno):
                exemptions.append((module, node.name, label))
            else:
                violations.append((module, node.name, label, call.lineno))
    return violations, exemptions


def _scan_app() -> tuple[list[Violation], list[Exemption]]:
    violations: list[Violation] = []
    exemptions: list[Exemption] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        module = f"app/{path.relative_to(APP_DIR).as_posix()}"
        v, e = scan_source(path.read_text(), module)
        violations.extend(v)
        exemptions.extend(e)
    return violations, exemptions


def test_no_inline_blocking_fs_calls_in_async_functions() -> None:
    """Every blocking fs call under app/ is either offloaded or explicitly justified."""
    violations, _ = _scan_app()
    assert not violations, "inline blocking filesystem I/O in async functions:\n" + "\n".join(
        f"  {mod}:{line} {fn}() → {label}" for mod, fn, label, line in violations
    )


def test_exemptions_are_exactly_the_pinned_set() -> None:
    """A new `# blocking-ok:` marker must be declared here — the escape hatch stays visible."""
    from collections import Counter

    _, exemptions = _scan_app()
    found = Counter(exemptions)
    expected = Counter(EXPECTED_EXEMPTIONS)
    assert found == expected, (
        f"undeclared exemptions: {sorted((found - expected).elements())}\n"
        f"stale declarations: {sorted((expected - found).elements())}"
    )


# --- the detector's own tests: these fail if the scanner stops detecting, which is what keeps
# --- the guard above from silently degrading into an assertion that always passes.

_INLINE = """
from pathlib import Path

async def handler(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
"""

_OFFLOADED = """
from anyio import to_thread
from pathlib import Path

def _mkdir_sync(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

async def handler(p: Path) -> None:
    await to_thread.run_sync(_mkdir_sync, p)
"""

_TRANSITIVE = """
from pathlib import Path

def _helper(p: Path) -> bool:
    return p.exists()

def _outer(p: Path) -> bool:
    return _helper(p)

async def handler(p: Path) -> None:
    _outer(p)
"""

_SQL_EXISTS = """
from sqlalchemy import select

async def handler(db) -> None:
    live = select(Job.id).where(Job.id == 1).exists()
    await db.execute(live)
"""

_MARKED = """
from pathlib import Path

async def handler(p: Path) -> None:
    p.unlink(missing_ok=True)  # blocking-ok: runs in a finally that must survive cancellation
"""

_MARKED_NO_REASON = """
from pathlib import Path

async def handler(p: Path) -> None:
    p.unlink(missing_ok=True)  # blocking-ok:
"""

_NESTED_SYNC_BODY = """
from anyio import to_thread
from pathlib import Path

async def handler(p: Path) -> None:
    def _work() -> None:
        p.mkdir(parents=True, exist_ok=True)
    await to_thread.run_sync(_work)
"""


def test_detects_a_reinlined_call() -> None:
    """The mutation this guard exists to catch: an offloaded call put back inline."""
    violations, _ = scan_source(_INLINE, "t.py")
    assert [(v[1], v[2]) for v in violations] == [("handler", "mkdir")]


def test_offloaded_call_is_clean() -> None:
    assert scan_source(_OFFLOADED, "t.py") == ([], [])


def test_detects_blocking_reached_through_a_sync_helper() -> None:
    """Transitivity: `_outer` blocks only via `_helper`, and the async caller still stalls."""
    violations, _ = scan_source(_TRANSITIVE, "t.py")
    assert [(v[1], v[2]) for v in violations] == [("handler", "_outer")]


def test_sqlalchemy_exists_is_not_a_filesystem_call() -> None:
    """The one real false positive in this codebase stays out (media.sweep_stale_progress)."""
    assert scan_source(_SQL_EXISTS, "t.py") == ([], [])


def test_marker_exempts_and_is_reported_as_an_exemption() -> None:
    violations, exemptions = scan_source(_MARKED, "t.py")
    assert violations == []
    assert exemptions == [("t.py", "handler", "unlink")]


def test_marker_without_a_reason_does_not_exempt() -> None:
    violations, exemptions = scan_source(_MARKED_NO_REASON, "t.py")
    assert exemptions == []
    assert [(v[1], v[2]) for v in violations] == [("handler", "unlink")]


def test_body_of_a_nested_sync_helper_is_not_a_violation() -> None:
    """The `run_sync` payload blocks by design — in a worker thread, not on the loop."""
    assert scan_source(_NESTED_SYNC_BODY, "t.py") == ([], [])
