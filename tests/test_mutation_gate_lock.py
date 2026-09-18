"""The mutation gate lock: one working tree, one mutation run or image build at a time.

`scripts/check-mutations.sh` applies each patch IN PLACE. For a few seconds at a time the checkout
on disk is mutated source, and anything else reading it — above all `docker build`, whose context
is the whole tree — reads the mutation. The uncommitted-changes refusal the script already had
cannot see this: it reads the tree once at the start, and a clean tree says nothing about what
another process is doing a second later.

NOTHING HERE TOUCHES THE CHECKOUT THE SUITE RUNS IN. Every case builds a throwaway git repository
in `tmp_path`, copies the WORKING-TREE scripts into it (so uncommitted edits are what is tested)
and runs them there: that repository has its own git directory and therefore its own lock. A test
that took, unlinked or contended for the real lock could defeat the very mechanism it is checking
— a `pytest` run beside a real gate or a real locked build would block, and replacing the lock
file's inode under a live holder would let a build and a gate both believe they hold it.

`uv` and `docker` are stubs on PATH inside the sandbox. The scripts themselves are never stubbed.
Synchronisation is by ready file and FIFO; nothing here sleeps to wait for something to happen.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECK_MUTATIONS = REPO / "scripts" / "check-mutations.sh"
BUILD_IMAGE = REPO / "scripts" / "build-image.sh"
RELEASING = REPO / "RELEASING.md"

MODULE = "tests/test_lock_protocol_concurrency.py"  # the script's default module must exist
TARGET = "app/thing.py"

_UV_STUB = r"""#!/bin/sh
# A stand-in for `uv`. It records, for each invocation, which kind it is and whether fd 9 — the
# mutation gate lock — is open in its own process, which is what the fd-inheritance tests read.
fd9=closed
[ -e "/proc/$$/fd/9" ] && fd9=open
kind=other
xml=
for arg in "$@"; do
    case "$arg" in --junitxml=*) xml="${arg#--junitxml=}" ; kind=mutation ;; esac
done
if [ "$kind" != mutation ]; then
    case "$1 $2" in
        "run pytest") kind=control ;;
        "run python") kind=parse ;;
    esac
fi
printf '%s\t%s\n' "$kind" "$fd9" >> "$UV_RECORD"
printf '%s\n' "$$" >> "$UV_PIDS"

case "$kind" in
    parse)
        shift 2
        exec python3 "$@"
        ;;
    control)
        if [ -n "${UV_GATE_READY:-}" ]; then
            [ -n "${UV_IGNORE_TERM:-}" ] && trap '' TERM
            : > "$UV_GATE_READY"
            read -r _ < "$UV_GATE_FIFO"
        fi
        printf '1 passed in 0.01s\n'
        exit 0
        ;;
    mutation)
        cat > "$xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
<testcase classname="tests.test_lock_protocol_concurrency" \
file="tests/test_lock_protocol_concurrency.py" name="test_probe">
<failure message="mutated">the mutation was caught</failure></testcase>
</testsuite></testsuites>
XML
        printf '1 failed in 0.01s\n'
        exit 1
        ;;
esac
printf 'stub uv: unexpected invocation: %s\n' "$*" >&2
exit 99
"""

_DOCKER_STUB = r"""#!/bin/sh
fd9=closed
[ -e "/proc/$$/fd/9" ] && fd9=open
printf '%s\t%s\n' "$fd9" "$*" >> "$DOCKER_RECORD"
if [ -n "${DOCKER_GATE_READY:-}" ]; then
    : > "$DOCKER_GATE_READY"
    read -r _ < "$DOCKER_GATE_FIFO"
fi
exit 0
"""

_PATCH = """--- a/app/thing.py
+++ b/app/thing.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    """Wait for a condition, with a bound. Never used to wait out a duration."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_is_held(lock: Path) -> bool:
    return subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0


def _release(fifo: Path) -> None:
    """Let a gated stub continue. O_NONBLOCK, so a dead reader raises instead of hanging."""

    def opened() -> bool:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENXIO:  # nobody is reading (yet)
                return False
            raise
        os.write(fd, b"go\n")
        os.close(fd)
        return True

    assert _wait_until(opened, 30), f"no reader on {fifo}"


@dataclass
class Sandbox:
    """A throwaway repository with the real scripts in it and stubbed externals on PATH."""

    root: Path
    bindir: Path
    env: dict[str, str]

    @property
    def lock(self) -> Path:
        gitdir = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(gitdir) / "mutation-gate.lock"

    @property
    def target(self) -> Path:
        return self.root / TARGET

    def uv_record(self) -> list[tuple[str, str]]:
        raw = Path(self.env["UV_RECORD"]).read_text().splitlines()
        return [(line.split("\t")[0], line.split("\t")[1]) for line in raw if line]

    def docker_record(self) -> list[tuple[str, str]]:
        path = Path(self.env["DOCKER_RECORD"])
        raw = path.read_text().splitlines() if path.exists() else []
        return [(line.split("\t")[0], line.split("\t")[1]) for line in raw if line]

    def gate_on_control(self) -> tuple[Path, Path]:
        """Make the control run block, so a test has a deterministic window with the lock held."""
        ready = self.root.parent / f"{self.root.name}-uv-ready"
        fifo = self.root.parent / f"{self.root.name}-uv-fifo"
        os.mkfifo(fifo)
        self.env["UV_GATE_READY"] = str(ready)
        self.env["UV_GATE_FIFO"] = str(fifo)
        return ready, fifo

    def gate_on_docker(self) -> tuple[Path, Path]:
        ready = self.root.parent / f"{self.root.name}-docker-ready"
        fifo = self.root.parent / f"{self.root.name}-docker-fifo"
        os.mkfifo(fifo)
        self.env["DOCKER_GATE_READY"] = str(ready)
        self.env["DOCKER_GATE_FIFO"] = str(fifo)
        return ready, fifo

    def run(self, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args), cwd=self.root, env=self.env, capture_output=True, text=True, **kwargs
        )

    def popen(self, *args: str, **kwargs) -> subprocess.Popen[str]:
        return subprocess.Popen(
            list(args),
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _sandbox(tmp_path: Path, name: str = "repo") -> Sandbox:
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "tests" / "mutations").mkdir(parents=True)
    (root / "app").mkdir()

    for script in (CHECK_MUTATIONS, BUILD_IMAGE):
        copy = root / "scripts" / script.name
        copy.write_text(script.read_text())
        copy.chmod(0o755)
    (root / MODULE).write_text("def test_probe():\n    pass\n")
    (root / TARGET).write_text("VALUE = 1\n")
    (root / "tests" / "mutations" / "one.patch").write_text(_PATCH)
    (root / "tests" / "mutations" / "expected.tsv").write_text(
        "# marker\ttest\ntests/mutations/one.patch\tone\ttest_probe\n"
    )
    (root / "Dockerfile").write_text("FROM scratch\n")

    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "sandbox", cwd=root)

    bindir = tmp_path / f"{name}-bin"
    bindir.mkdir()
    for stub, body in (("uv", _UV_STUB), ("docker", _DOCKER_STUB)):
        path = bindir / stub
        path.write_text(body)
        path.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["UV_RECORD"] = str(tmp_path / f"{name}-uv.log")
    env["UV_PIDS"] = str(tmp_path / f"{name}-uv.pids")
    env["DOCKER_RECORD"] = str(tmp_path / f"{name}-docker.log")
    Path(env["UV_RECORD"]).touch()
    Path(env["UV_PIDS"]).touch()
    for leaked in ("UV_GATE_READY", "UV_GATE_FIFO", "DOCKER_GATE_READY", "DOCKER_GATE_FIFO"):
        env.pop(leaked, None)
    return Sandbox(root=root, bindir=bindir, env=env)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return _sandbox(tmp_path)


@pytest.fixture
def holder(sandbox: Sandbox) -> Iterator[Sandbox]:
    """A process standing in for a build: it holds the sandbox's lock until the fixture lets go."""
    program = (
        "import fcntl, sys\n"
        "handle = open(sys.argv[1], 'w')\n"
        "fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "open(sys.argv[2], 'w').close()\n"
        "sys.stdin.read()\n"
    )
    ready = sandbox.root.parent / "holder-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", program, str(sandbox.lock), str(ready)],
        stdin=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_until(ready.exists, 30), "the holder never signalled that it holds the lock"
        yield sandbox
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)


# --------------------------------------------------------------------------------------------
# the gate refuses while something else holds the lock
# --------------------------------------------------------------------------------------------


def test_the_gate_refuses_while_another_process_holds_the_lock(holder: Sandbox) -> None:
    before = _sha(holder.target)

    run = holder.run("scripts/check-mutations.sh", timeout=120)

    assert run.returncode == 2, f"expected a refusal, got {run.returncode}: {run.stdout}"
    assert "mutation gate lock" in run.stderr
    assert "nothing was patched" in run.stderr
    assert "control:" not in run.stdout  # it refused before doing any work at all
    assert _sha(holder.target) == before
    assert holder.uv_record() == []


def test_a_build_waits_for_the_lock_and_says_so(sandbox: Sandbox) -> None:
    """The other direction, for real: the gate holds the lock and the build queues behind it."""
    ready, fifo = sandbox.gate_on_control()
    gate = sandbox.popen("scripts/check-mutations.sh")
    try:
        assert _wait_until(ready.exists, 60), "the gate never reached its control run"
        assert _lock_is_held(sandbox.lock)

        build = sandbox.popen("scripts/build-image.sh", "1.7.1+abc1234")
        try:
            # It must not reach docker while the gate holds the lock. Proven by the gate's own
            # release below being what unblocks it, not by waiting out a duration.
            assert sandbox.docker_record() == []
            _release(fifo)
            assert gate.wait(timeout=180) == 0
            assert build.wait(timeout=180) == 0
        finally:
            if build.poll() is None:
                build.kill()
                build.wait(timeout=60)
        stderr = build.stderr.read() if build.stderr else ""
        assert "waiting for the mutation gate lock" in stderr
        assert [argv for _, argv in sandbox.docker_record()] == [
            "build --build-arg APP_VERSION=1.7.1+abc1234 -t memos-ng:local ."
        ]
    finally:
        if gate.poll() is None:
            gate.kill()
            gate.wait(timeout=60)


# --------------------------------------------------------------------------------------------
# no child of the gate inherits the lock
# --------------------------------------------------------------------------------------------


def test_no_uv_child_of_the_gate_inherits_the_lock(sandbox: Sandbox) -> None:
    """Every kind of `uv` child, not just the two pytest runs: a third site that kept fd 9 would
    hold the lock after a kill just as surely, and the docstring would be a lie for it."""
    run = sandbox.run("scripts/check-mutations.sh", timeout=180)

    assert run.returncode == 0, run.stdout + run.stderr
    record = sandbox.uv_record()
    kinds = {kind for kind, _ in record}
    assert kinds == {"control", "mutation", "parse"}, record
    assert [kind for kind, fd9 in record if fd9 != "closed"] == [], record


def test_a_child_that_outlives_a_killed_run_does_not_keep_the_lock(sandbox: Sandbox) -> None:
    """The kill case, with only the script killed — not the process group, which would take the
    child with it and prove nothing. The child ignores TERM and is still running at the assert.

    SIGKILL, and deliberately: bash defers a trapped signal until the foreground child returns, so
    a TERM here would not end the run at all while the stub sits in its gate. SIGKILL is exactly
    the case where no trap of ours runs and the lock's release is the kernel's doing alone — which
    is the claim under test.
    """
    ready, fifo = sandbox.gate_on_control()
    sandbox.env["UV_IGNORE_TERM"] = "1"
    gate = sandbox.popen("scripts/check-mutations.sh")
    try:
        assert _wait_until(ready.exists, 60), "the gate never reached its control run"
        assert _lock_is_held(sandbox.lock)

        gate.send_signal(signal.SIGKILL)  # the script alone, by pid
        gate.wait(timeout=120)

        child_pid = int(Path(sandbox.env["UV_PIDS"]).read_text().split()[0])
        os.kill(child_pid, 0)  # control: the child really did outlive the run
        assert _wait_until(lambda: not _lock_is_held(sandbox.lock), 60), (
            "a surviving child is still holding the lock of a run that is gone"
        )
    finally:
        _release(fifo)
        if gate.poll() is None:
            gate.kill()
            gate.wait(timeout=60)


# --------------------------------------------------------------------------------------------
# the build script's own guard: the lock cannot see a patch a SIGKILLed run left behind
# --------------------------------------------------------------------------------------------


def test_the_build_refuses_while_a_tracked_file_differs_from_head(sandbox: Sandbox) -> None:
    sandbox.target.write_text("VALUE = 2\n")  # what a killed run leaves behind

    run = sandbox.run("scripts/build-image.sh", "1.7.1+abc1234", timeout=120)

    assert run.returncode != 0
    assert TARGET in run.stderr
    assert "differ from HEAD" in run.stderr
    assert sandbox.docker_record() == [], "it built anyway"


def test_the_build_holds_the_lock_for_as_long_as_docker_reads_the_context(sandbox: Sandbox) -> None:
    ready, fifo = sandbox.gate_on_docker()
    build = sandbox.popen("scripts/build-image.sh", "1.7.1+abc1234")
    try:
        assert _wait_until(ready.exists, 60), "docker was never reached"
        assert _lock_is_held(sandbox.lock), "the build dropped the lock before the build ran"
        _release(fifo)
        assert build.wait(timeout=180) == 0
    finally:
        if build.poll() is None:
            build.kill()
            build.wait(timeout=60)

    assert sandbox.docker_record() == [
        ("open", "build --build-arg APP_VERSION=1.7.1+abc1234 -t memos-ng:local .")
    ]
    assert not _lock_is_held(sandbox.lock)


# --------------------------------------------------------------------------------------------
# scope, and the procedure on record
# --------------------------------------------------------------------------------------------


def test_a_run_in_one_worktree_does_not_block_a_run_in_another(tmp_path: Path) -> None:
    """Why the lock is taken on `--absolute-git-dir` and not on `--git-common-dir`: the hazard is
    one WORKING TREE. A gate in another worktree is patching files this one cannot see, and making
    the two queue behind each other would be pure cost. Driven, not argued: one run holds its lock
    while the other runs to completion."""
    first = _sandbox(tmp_path, "first")
    second_root = tmp_path / "second"
    _git("worktree", "add", "-q", "-b", "other", str(second_root), cwd=first.root)
    second = Sandbox(root=second_root, bindir=first.bindir, env=dict(first.env))
    second.env["UV_RECORD"] = str(tmp_path / "second-uv.log")
    second.env["UV_PIDS"] = str(tmp_path / "second-uv.pids")
    Path(second.env["UV_RECORD"]).touch()
    Path(second.env["UV_PIDS"]).touch()

    assert first.lock != second.lock
    ready, fifo = first.gate_on_control()
    held = first.popen("scripts/check-mutations.sh")
    try:
        assert _wait_until(ready.exists, 60), "the first run never reached its control run"
        assert _lock_is_held(first.lock)

        other = second.run("scripts/check-mutations.sh", timeout=180)
        assert other.returncode == 0, other.stdout + other.stderr
        assert "mutation gate lock" not in other.stderr
    finally:
        _release(fifo)
        if held.poll() is None:
            held.kill()
        held.wait(timeout=120)


def test_the_release_procedure_builds_through_the_script(sandbox: Sandbox) -> None:
    """A documented `docker build` that bypasses the script bypasses both guards with it."""
    releasing = RELEASING.read_text()
    assert "scripts/build-image.sh <version>+<short-sha>" in releasing
    assert re.search(r"docker build[^\n]*-t memos-ng:local", releasing) is None
    assert BUILD_IMAGE.stat().st_mode & 0o111, "the documented script is not executable"
