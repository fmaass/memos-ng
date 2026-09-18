"""The mutation gate's lock: one working tree, one mutation run or image build at a time.

`scripts/check-mutations.sh` applies each patch IN PLACE. For a few seconds at a time the
checkout on disk is mutated source, and anything else reading that checkout — above all a
`docker build`, whose context is the whole tree — reads the mutation. The uncommitted-changes
refusal the script already had cannot see this: it reads the tree once at the start, and a clean
tree says nothing about what another process is doing a second later.

These tests drive the real script. Nothing here sleeps to synchronise: the holder announces
itself with a ready file, and the run under test is waited for on the lock itself.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-mutations.sh"
RELEASING = REPO / "RELEASING.md"

# The lock path as the operator is told to spell it in RELEASING.md. Both sides must name the
# same file or the lock protects nothing, so it is written out here rather than derived.
DOCUMENTED_BUILD_PREFIX = 'flock "$(git rev-parse --absolute-git-dir)/mutation-gate.lock"'

_HOLDER = """
import fcntl, sys
handle = open(sys.argv[1], "w")
fcntl.flock(handle, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
sys.stdin.read()
"""


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    """Poll until `predicate` holds. The wait is on a condition, never on the clock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _lock_path() -> Path:
    return Path(_git("rev-parse", "--absolute-git-dir")) / "mutation-gate.lock"


def _lock_is_held(lock: Path) -> bool:
    return subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0


def _patch_targets() -> list[Path]:
    """Every file the manifest's patches rewrite — what a leaked mutation would be found in."""
    targets = set()
    for patch in sorted((REPO / "tests" / "mutations").glob("*.patch")):
        for line in patch.read_text().splitlines():
            if line.startswith("+++ b/"):
                targets.add(REPO / line[len("+++ b/") :].strip())
    assert targets, "no mutation patches found — this test would prove nothing"
    return sorted(targets)


def _digest(paths: list[Path]) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[Path]:
    """A process standing in for the build: it holds the lock until this fixture lets go."""
    lock = _lock_path()
    ready = tmp_path / "held"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock), str(ready)],
        stdin=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_until(ready.exists, 30), "the lock holder never signalled that it holds it"
        yield lock
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)


def test_the_gate_refuses_while_another_process_holds_the_lock(holder: Path) -> None:
    before = _digest(_patch_targets())

    run = subprocess.run([str(SCRIPT)], cwd=REPO, capture_output=True, text=True, timeout=180)

    assert run.returncode == 2, f"expected a refusal, got {run.returncode}: {run.stdout}"
    assert "mutation gate lock" in run.stderr
    assert "nothing was patched" in run.stderr
    # It refused instead of queueing, and it refused before doing any work at all.
    assert "control:" not in run.stdout
    assert _digest(_patch_targets()) == before


def test_a_build_taking_the_same_lock_waits_while_a_run_holds_it() -> None:
    lock = _lock_path()
    before = _digest(_patch_targets())
    # Its own session, so the kill below reaches any child the run has started by then.
    run = subprocess.Popen(
        [str(SCRIPT)],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        held = _wait_until(lambda: _lock_is_held(lock) or run.poll() is not None, 120)
        assert held and run.poll() is None, "the run never took the lock"

        # What the build's `flock` prefix does while the run holds it: the non-blocking probe
        # fails, and the blocking form the build actually uses waits rather than proceeding.
        assert subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0
        assert subprocess.run(["flock", "-w", "1", str(lock), "true"]).returncode == 1
    finally:
        os.killpg(run.pid, signal.SIGTERM)
        run.wait(timeout=180)

    assert run.returncode != 0
    # Released on a kill, by the kernel closing the fd — there is no release step to skip.
    assert _wait_until(lambda: not _lock_is_held(lock), 60), "the lock outlived the killed run"
    assert _digest(_patch_targets()) == before


def test_the_lock_is_released_when_the_run_dies_in_its_own_preflight(tmp_path: Path) -> None:
    lock = _lock_path()
    # Removing it is safe only because nobody holds it: a fresh open would otherwise get a new
    # inode and two runs would both think they had the lock.
    assert not _lock_is_held(lock)
    lock.unlink(missing_ok=True)

    run = subprocess.run(
        [str(SCRIPT), "--manifest", str(tmp_path / "absent.tsv")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.returncode == 2
    assert "no manifest" in run.stderr
    # It took the lock (the file is back) before deciding it had nothing to do, and gave it up.
    assert lock.exists(), "this exit path never took the lock"
    assert not _lock_is_held(lock)


def test_the_lock_is_per_working_tree_not_per_repository(tmp_path: Path) -> None:
    """Why `--absolute-git-dir`: a build in ANOTHER worktree reads other files and must not wait."""
    assert "rev-parse --absolute-git-dir" in SCRIPT.read_text()

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", str(main), cwd=tmp_path)
    (main / "seed").write_text("seed\n")
    _git("-c", "user.email=t@e", "-c", "user.name=t", "add", "seed", cwd=main)
    _git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed", cwd=main)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "other", str(linked), cwd=main)

    main_lock = Path(_git("rev-parse", "--absolute-git-dir", cwd=main))
    linked_lock = Path(_git("rev-parse", "--absolute-git-dir", cwd=linked))
    assert main_lock != linked_lock, "the two worktrees would share one lock and block each other"
    # …whereas the flag not chosen is one and the same file for both, which is exactly the
    # cross-worktree blocking this avoids. (It also prints a bare relative `.git` in the main
    # checkout, hence `--path-format=absolute` to compare the two at all.)
    common = "--git-common-dir"
    assert Path(_git("rev-parse", "--path-format=absolute", common, cwd=main)) == main_lock
    assert Path(_git("rev-parse", "--path-format=absolute", common, cwd=linked)) == main_lock


def test_the_build_command_on_record_takes_the_lock_the_script_takes() -> None:
    """A documented build line that names another path would leave the build unprotected."""
    releasing = RELEASING.read_text()
    assert DOCUMENTED_BUILD_PREFIX in releasing
    assert f"{DOCUMENTED_BUILD_PREFIX} docker build" in releasing
    script = SCRIPT.read_text()
    assert "gitdir=$(git rev-parse --absolute-git-dir" in script
    assert 'LOCKFILE="$gitdir/mutation-gate.lock"' in script
