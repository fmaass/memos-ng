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
No test waits out a fixed duration: every wait is for a named condition, polled at 10 ms with a
bound, or a FIFO a stub is already blocked on. There is exactly one bounded negative window, in
`test_a_build_waits_for_the_lock_and_says_so`, where the thing being proved is that something does
NOT happen while the lock is held; it is opened only after contention has been confirmed on the
build's own stderr, and it is short.

Every process a test starts, and every stub blocked on a FIFO, is torn down in a `finally` that
runs whatever the body did — processes first, by pid, then the FIFO — so a failing assertion fails
the test instead of hanging the suite behind a reader nobody woke.
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
        # A file changed by someone else while the run holds the lock, after the dry check.
        [ -n "${UV_CONTROL_REWRITES:-}" ] && printf 'VALUE = 5\n' > "$UV_CONTROL_REWRITES"
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
# DOCKER_MARKER, when set, is a file a test creates at the moment it lets the lock go. Recording
# whether it existed WHEN DOCKER RAN is what distinguishes "the build waited" from "the build ran
# and the assertions happened to look afterwards".
marker=absent
[ -n "${DOCKER_MARKER:-}" ] && [ -e "$DOCKER_MARKER" ] && marker=present
printf '%s\t%s\t%s\n' "$fd9" "$marker" "$*" >> "$DOCKER_RECORD"
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


def _release(fifo: Path, timeout: float = 30, required: bool = True) -> bool:
    """Let a gated stub continue. O_NONBLOCK, so a dead reader raises instead of hanging."""

    def opened() -> bool:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENXIO:  # nobody is reading (yet)
                return False
            raise
        try:
            os.write(fd, b"go\n")
        finally:
            os.close(fd)
        return True

    woke = _wait_until(opened, timeout)
    assert woke or not required, f"no reader on {fifo}"
    return woke


class Reaper:
    """Everything a test starts, torn down by a finalizer that runs whatever the body did.

    The order below is the order the code runs, and each step is independent of the last.

      1. the processes the test started are killed by pid, each bounded, each in its own `try`;
      2. every gated FIFO is written, so a stub already blocked on a read that nobody was ever
         going to answer is woken and can leave on its own;
      3. every grandchild that recorded its pid is SIGKILLed — but only after its /proc cmdline
         is read and found to name this sandbox (see `_cmdline_names`), because a recorded pid
         may since have been recycled and must never be a stranger's process;
      4. ONE bounded wait for exactly those killed pids.

    Step 3 is what makes this a teardown rather than a hope: a stub that published its readiness
    and was descheduled before opening its FIFO is not woken by step 2 at all — it opens the FIFO
    afterwards and blocks for the rest of the session. Only a signal ends it. Steps 2 and 3 are
    both kept because they answer different cases, and step 2 first costs nothing.
    """

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[str]] = []
        self.fifos: list[Path] = []
        self.pid_files: list[tuple[Path, Path]] = []

    def watch(self, proc: subprocess.Popen[str]) -> subprocess.Popen[str]:
        self.procs.append(proc)
        return proc

    def gate(self, fifo: Path) -> Path:
        self.fifos.append(fifo)
        return fifo

    def children(self, pid_file: Path, root: Path) -> Path:
        """A file the stubs append their own pids to: grandchildren this process cannot wait on.

        `root` is the sandbox those pids belong to. It is passed in, never guessed from the cwd,
        and it is what every recorded pid is checked against before it is signalled.
        """
        self.pid_files.append((pid_file, root))
        return pid_file

    def close(self, wait_timeout: float = 30) -> None:
        """Steps 1-4 of the class docstring, in that order. `wait_timeout` bounds step 4 only."""
        for proc in self.procs:  # 1
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=60)
            except Exception:  # noqa: BLE001 — one stuck process must not skip the rest
                pass
        for fifo in self.fifos:  # 2
            try:
                _release(fifo, timeout=1, required=False)
            except Exception:  # noqa: BLE001 — cleanup, not an assertion
                pass
        killed: list[int] = []
        for pid_file, root in self.pid_files:  # 3
            for pid in _recorded_pids(pid_file):
                if not _cmdline_names(pid, root):
                    continue  # gone, or recycled into something that is not ours
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except OSError:  # noqa: PERF203 — it exited between the check and the signal
                    pass
        _wait_until(lambda: not any(_alive(pid) for pid in killed), wait_timeout)  # 4


def _recorded_pids(pid_file: Path) -> list[int]:
    try:
        return [int(line) for line in pid_file.read_text().split()]
    except (OSError, ValueError):
        return []


def _cmdline_names(pid: int, root: Path) -> bool:
    """Does this pid's argv name a path in the throwaway tree belonging to `root`?

    The tree is the sandbox repository at `root` plus its sibling stub directory `<root>-bin`,
    where the `uv` and `docker` stubs live — so an argument counts when it starts with `<root>/`
    or `<root>-`. Both live under pytest's `tmp_path` for this test alone, so nothing outside it
    can match: not another test's sandbox, not a recycled pid belonging to a stranger, and not
    pytest itself, whose own argv names none of these paths.
    """
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return False  # it is already gone, which is the outcome we wanted anyway
    prefix = str(root)
    return any(arg.startswith((prefix + "/", prefix + "-")) for arg in argv)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.fixture
def reaper() -> Iterator[Reaper]:
    reap = Reaper()
    try:
        yield reap
    finally:
        reap.close()


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

    def docker_record(self) -> list[tuple[str, str, str]]:
        """One (fd-9 state, release-marker state, argv) triple per docker invocation."""
        path = Path(self.env["DOCKER_RECORD"])
        raw = path.read_text().splitlines() if path.exists() else []
        return [tuple(line.split("\t", 2)) for line in raw if line]  # type: ignore[misc]

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

    def popen(
        self, *args: str, stderr: object = subprocess.PIPE, **kwargs
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            list(args),
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=stderr,
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
    # IMAGE_TAG too: an exported tag in the developer's shell must not decide what "the default" is.
    for leaked in (
        "UV_GATE_READY",
        "UV_GATE_FIFO",
        "DOCKER_GATE_READY",
        "DOCKER_GATE_FIFO",
        "IMAGE_TAG",
        "UV_CONTROL_REWRITES",
    ):
        env.pop(leaked, None)
    return Sandbox(root=root, bindir=bindir, env=env)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return _sandbox(tmp_path)


@pytest.fixture
def holder(sandbox: Sandbox, reaper: Reaper) -> Sandbox:
    """A process standing in for a build: it holds the sandbox's lock until the reaper kills it."""
    program = (
        "import fcntl, sys\n"
        "handle = open(sys.argv[1], 'w')\n"
        "fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "open(sys.argv[2], 'w').close()\n"
        "sys.stdin.read()\n"
    )
    ready = sandbox.root.parent / "holder-ready"
    reaper.watch(
        subprocess.Popen(
            [sys.executable, "-c", program, str(sandbox.lock), str(ready)],
            stdin=subprocess.PIPE,  # an open pipe is what keeps its read — and the lock — blocking
            text=True,
        )
    )
    assert _wait_until(ready.exists, 30), "the holder never signalled that it holds the lock"
    return sandbox


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


def test_a_build_waits_for_the_lock_and_says_so(sandbox: Sandbox, reaper: Reaper) -> None:
    """The other direction, and the wait itself: the gate holds the lock, the build queues.

    The shape matters. Looking once and finding docker unused proves nothing — a build that took
    no lock at all would also be unlikely to have finished by then. So: contention is CONFIRMED
    first, on the build's own stderr, and only then is a short negative window opened. That window
    is the one place in this file where the absence of an event is asserted over a duration, and
    it is sound only because the lock is demonstrably held for all of it: correct code cannot run
    docker there, and a build that skipped the blocking wait runs it immediately.

    The marker closes the last gap: the stub records whether it existed when docker ran, so "the
    build went ahead early and the assertions happened to look later" is a distinguishable answer.
    """
    ready, fifo = sandbox.gate_on_control()
    reaper.gate(fifo)
    reaper.children(Path(sandbox.env["UV_PIDS"]), sandbox.root)
    gate = reaper.watch(sandbox.popen("scripts/check-mutations.sh"))
    assert _wait_until(ready.exists, 60), "the gate never reached its control run"
    assert _lock_is_held(sandbox.lock)

    released = sandbox.root.parent / "lock-released"
    sandbox.env["DOCKER_MARKER"] = str(released)
    build_err = sandbox.root.parent / "build.err"
    with build_err.open("w") as err:
        build = reaper.watch(sandbox.popen("scripts/build-image.sh", "1.7.1+abc1234", stderr=err))

    def contended() -> bool:
        return "waiting for the mutation gate lock" in build_err.read_text()

    assert _wait_until(contended, 60), "the build never reported that it was waiting"

    # The bounded negative window, opened only now: the build has told us it is blocked, the gate
    # still holds the lock, and nothing may reach docker until we let go.
    assert not _wait_until(lambda: sandbox.docker_record() != [], 0.5), sandbox.docker_record()
    assert _lock_is_held(sandbox.lock)

    released.touch()
    _release(fifo)
    assert gate.wait(timeout=180) == 0
    assert build.wait(timeout=180) == 0

    record = sandbox.docker_record()
    assert [argv for _, _, argv in record] == [
        "build --build-arg APP_VERSION=1.7.1+abc1234 -t memos-ng:local ."
    ]
    assert [marker for _, marker, _ in record] == ["present"], "docker ran before the lock was free"


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


def test_a_child_that_outlives_a_killed_run_does_not_keep_the_lock(
    sandbox: Sandbox, reaper: Reaper
) -> None:
    """The kill case, with only the script killed — not the process group, which would take the
    child with it and prove nothing. The child ignores TERM and is still running at the assert.

    SIGKILL, and deliberately: bash defers a trapped signal until the foreground child returns, so
    a TERM here would not end the run at all while the stub sits in its gate. SIGKILL is exactly
    the case where no trap of ours runs and the lock's release is the kernel's doing alone — which
    is the claim under test.
    """
    ready, fifo = sandbox.gate_on_control()
    reaper.gate(fifo)
    reaper.children(Path(sandbox.env["UV_PIDS"]), sandbox.root)
    sandbox.env["UV_IGNORE_TERM"] = "1"
    gate = reaper.watch(sandbox.popen("scripts/check-mutations.sh"))
    assert _wait_until(ready.exists, 60), "the gate never reached its control run"
    assert _lock_is_held(sandbox.lock)

    gate.send_signal(signal.SIGKILL)  # the script alone, by pid
    gate.wait(timeout=120)

    child_pid = int(Path(sandbox.env["UV_PIDS"]).read_text().split()[0])
    os.kill(child_pid, 0)  # control: the child really did outlive the run
    assert _wait_until(lambda: not _lock_is_held(sandbox.lock), 60), (
        "a surviving child is still holding the lock of a run that is gone"
    )


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


def test_the_build_holds_the_lock_for_as_long_as_docker_reads_the_context(
    sandbox: Sandbox, reaper: Reaper
) -> None:
    ready, fifo = sandbox.gate_on_docker()
    reaper.gate(fifo)
    build = reaper.watch(sandbox.popen("scripts/build-image.sh", "1.7.1+abc1234"))
    assert _wait_until(ready.exists, 60), "docker was never reached"
    assert _lock_is_held(sandbox.lock), "the build dropped the lock before the build ran"
    _release(fifo)
    assert build.wait(timeout=180) == 0

    assert sandbox.docker_record() == [
        ("open", "absent", "build --build-arg APP_VERSION=1.7.1+abc1234 -t memos-ng:local .")
    ]
    assert not _lock_is_held(sandbox.lock)


# --------------------------------------------------------------------------------------------
# scope, and the procedure on record
# --------------------------------------------------------------------------------------------


def test_a_run_in_one_worktree_does_not_block_a_run_in_another(
    tmp_path: Path, reaper: Reaper
) -> None:
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
    reaper.gate(fifo)
    reaper.children(Path(first.env["UV_PIDS"]), first.root)
    reaper.watch(first.popen("scripts/check-mutations.sh"))
    assert _wait_until(ready.exists, 60), "the first run never reached its control run"
    assert _lock_is_held(first.lock)

    other = second.run("scripts/check-mutations.sh", timeout=180)
    assert other.returncode == 0, other.stdout + other.stderr
    assert "mutation gate lock" not in other.stderr


def _built_tags(sandbox: Sandbox) -> list[str]:
    """EVERY `-t` value docker was handed: a second tag beside the asked-for one must show."""
    tags = []
    for _, _, argv in sandbox.docker_record():
        words = argv.split()
        tags.extend(words[i + 1] for i, word in enumerate(words) if word == "-t")
    return tags


def test_the_build_tags_the_deploy_image_unless_told_otherwise(sandbox: Sandbox) -> None:
    run = sandbox.run("scripts/build-image.sh", "1.7.1+abc1234", timeout=120)

    assert run.returncode == 0, run.stdout + run.stderr
    assert _built_tags(sandbox) == ["memos-ng:local"]


def test_a_compile_check_builds_under_its_own_tag_and_leaves_the_deploy_tag_alone(
    sandbox: Sandbox,
) -> None:
    sandbox.env["IMAGE_TAG"] = "memos-ng:compile-check"

    run = sandbox.run("scripts/build-image.sh", "1.7.1+abc1234", timeout=120)

    assert run.returncode == 0, run.stdout + run.stderr
    assert _built_tags(sandbox) == ["memos-ng:compile-check"]


@pytest.mark.parametrize(
    "tag",
    ["", "memos-ng:", "other/image:local", "memos-ng:a b", "memos-ng:x;rm", "memos-ng:-x"],
)
def test_the_build_refuses_a_tag_outside_its_own_image(sandbox: Sandbox, tag: str) -> None:
    sandbox.env["IMAGE_TAG"] = tag

    run = sandbox.run("scripts/build-image.sh", "1.7.1+abc1234", timeout=120)

    if tag == "":  # unset and empty are the same to the script: the deploy tag
        assert run.returncode == 0, run.stdout + run.stderr
        assert _built_tags(sandbox) == ["memos-ng:local"]
        return
    assert run.returncode == 2
    assert "IMAGE_TAG" in run.stderr
    assert sandbox.docker_record() == [], "it built anyway"


def test_the_release_procedure_builds_through_the_script(sandbox: Sandbox) -> None:
    """A documented `docker build` that bypasses the script bypasses both guards with it."""
    releasing = RELEASING.read_text()
    assert "scripts/build-image.sh <version>+<short-sha>" in releasing
    assert re.search(r"docker build[^\n]*-t memos-ng:local", releasing) is None
    assert BUILD_IMAGE.stat().st_mode & 0o111, "the documented script is not executable"


# --------------------------------------------------------------------------------------------
# a patch that no longer applies is STALE, not a surviving mutant (MEMNG-500)
# --------------------------------------------------------------------------------------------
# A neighbouring change that rewrites a patch's context lines says nothing about the mutated
# function, yet it used to end the run exactly like a lost mutation. The sandbox is the same one
# the lock tests use: the real script, a uv stub whose mutation run fails `test_probe`.

_STALE_PATCH = """--- a/app/thing.py
+++ b/app/thing.py
@@ -1 +1 @@
-VALUE = 7
+VALUE = 8
"""


def _stale_patch(sandbox: Sandbox, name: str) -> str:
    """An untracked patch whose context the target no longer has; returns its manifest path."""
    rel = f"tests/mutations/{name}.patch"
    (sandbox.root / rel).write_text(_STALE_PATCH)
    return rel


def _manifest(sandbox: Sandbox, *rows: str) -> None:
    (sandbox.root / "tests" / "mutations" / "expected.tsv").write_text(
        "# marker\ttest\n" + "".join(f"{row}\n" for row in rows)
    )


def _stale_line(marker: str, patch: str) -> str:
    return (
        f"{marker}: STALE — {patch} no longer applies to {TARGET}"
        " (context drift, not a surviving mutant)"
    )


GOOD_ROW = "tests/mutations/one.patch\tone\ttest_probe"


def test_a_stale_patch_is_reported_as_stale_and_not_as_a_surviving_mutant(
    sandbox: Sandbox,
) -> None:
    stale = _stale_patch(sandbox, "drifted")
    _manifest(sandbox, GOOD_ROW, f"{stale}\tdrifted\ttest_probe")
    before = _sha(sandbox.target)

    run = sandbox.run("scripts/check-mutations.sh", timeout=180)

    assert run.returncode == 3, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    assert _stale_line("drifted", stale) in lines, run.stdout
    assert [line for line in lines if ": FAIL" in line] == [], run.stdout
    assert any(line.startswith("one: PASS") for line in lines), run.stdout
    summary = [line for line in lines if line.startswith("SUMMARY:")]
    assert len(summary) == 1 and "1 STALE" in summary[0], run.stdout
    assert "did NOT behave" not in summary[0], run.stdout
    assert _sha(sandbox.target) == before


def test_every_stale_patch_is_named_before_any_mutation_runs(sandbox: Sandbox) -> None:
    first = _stale_patch(sandbox, "drift_a")
    second = _stale_patch(sandbox, "drift_b")
    _manifest(sandbox, f"{first}\tdrift_a\ttest_probe", GOOD_ROW, f"{second}\tdrift_b\ttest_probe")

    run = sandbox.run("scripts/check-mutations.sh", timeout=180)

    assert run.returncode == 3, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    first_control = next(i for i, line in enumerate(lines) if line.startswith("control:"))
    for marker, patch in (("drift_a", first), ("drift_b", second)):
        assert _stale_line(marker, patch) in lines[:first_control], run.stdout
        assert lines.count(_stale_line(marker, patch)) == 1, run.stdout
    assert "2 STALE" in lines[-1], run.stdout


def test_a_judged_failure_wins_over_a_stale_patch(sandbox: Sandbox) -> None:
    stale = _stale_patch(sandbox, "drifted")
    # A real, applicable mutation whose stub run fails test_probe, not the named test_other.
    _manifest(
        sandbox,
        "tests/mutations/one.patch\twrong\ttest_other",
        f"{stale}\tdrifted\ttest_probe",
    )

    run = sandbox.run("scripts/check-mutations.sh", timeout=180)

    assert run.returncode == 1, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    assert any(line.startswith("wrong: FAIL — ") for line in lines), run.stdout
    assert _stale_line("drifted", stale) in lines, run.stdout
    assert "did NOT behave" in lines[-1] and "1 STALE" in lines[-1], run.stdout


def test_a_patch_file_the_manifest_does_not_name_is_not_checked(sandbox: Sandbox) -> None:
    _stale_patch(sandbox, "unlisted")

    run = sandbox.run("scripts/check-mutations.sh", timeout=180)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "STALE" not in run.stdout + run.stderr


def test_a_run_refused_for_the_lock_reports_no_stale_patch(holder: Sandbox) -> None:
    """The dry check reads the tree, so it runs only once the lock is ours."""
    stale = _stale_patch(holder, "drifted")
    _manifest(holder, f"{stale}\tdrifted\ttest_probe")

    run = holder.run("scripts/check-mutations.sh", timeout=120)

    assert run.returncode == 2, run.stdout + run.stderr
    assert "STALE" not in run.stdout + run.stderr
    assert holder.uv_record() == []


def test_a_patch_that_passed_the_dry_check_but_does_not_apply_is_a_failure(
    sandbox: Sandbox,
) -> None:
    """Drift is what the tree held BEFORE the locked run; a target that changes during it (here
    under the positive control) is a runtime failure, and must not pass as exit 3."""
    sandbox.env["UV_CONTROL_REWRITES"] = str(sandbox.target)

    run = sandbox.run("scripts/check-mutations.sh", timeout=180)

    assert run.returncode == 1, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    assert (
        "one: FAIL — tests/mutations/one.patch passed the pre-check but did not apply in the"
        f" loop: {TARGET} changed during the locked run"
    ) in lines, run.stdout
    assert "STALE" not in run.stdout
    assert "did NOT behave" in lines[-1], run.stdout
    assert sandbox.target.read_text() == "VALUE = 5\n"  # the other writer's content, untouched


# --------------------------------------------------------------------------------------------
# the teardown itself, which the rest of this file depends on
# --------------------------------------------------------------------------------------------


def _blocker(root: Path) -> Path:
    """A stand-in for a stub that published its pid and then blocked opening a FIFO for ever.

    It is started detached — an intermediate shell backgrounds it and exits — so the process the
    reaper has to deal with is an orphan and not a child of pytest. That matters: a child killed
    but not waited on stays a zombie, and a zombie still answers signal 0, so `_alive` would say
    "alive" for a pid the reaper had already dealt with and this test would prove nothing.
    """
    script = root / "blocker.sh"
    script.write_text('#!/bin/sh\nprintf \'%s\\n\' "$$" >> "$1"\nread -r _ < "$2"\n')
    script.chmod(0o755)
    return script


def _start_detached(script: Path, pid_file: Path, fifo: Path) -> None:
    subprocess.run(
        ["sh", "-c", '"$1" "$2" "$3" &', "sh", str(script), str(pid_file), str(fifo)],
        check=True,
        timeout=60,
    )


def _kill_recorded(pid_file: Path) -> None:
    for pid in _recorded_pids(pid_file):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:  # noqa: PERF203 — already gone
            pass


def test_the_reaper_kills_a_recorded_grandchild_it_never_started(sandbox: Sandbox) -> None:
    """The teardown's own test. Nothing here is registered with `watch()` and the FIFO is
    registered with nobody, so the only thing that can end this process is the verified kill —
    which is exactly the case that hung the suite before: a stub that recorded its readiness, was
    descheduled, and opened its FIFO only after the release had already come and gone."""
    pid_file = sandbox.root.parent / "blocker.pids"
    pid_file.touch()
    fifo = sandbox.root.parent / "blocker-fifo"
    os.mkfifo(fifo)
    reap = Reaper()
    reap.children(pid_file, sandbox.root)
    try:
        _start_detached(_blocker(sandbox.root), pid_file, fifo)
        assert _wait_until(lambda: _recorded_pids(pid_file) != [], 30), "it never recorded a pid"
        pid = _recorded_pids(pid_file)[0]
        assert _alive(pid), "the control: it is running before the teardown"

        reap.close(wait_timeout=1)

        assert not _alive(pid), "the reaper left a blocked grandchild behind"
    finally:
        _kill_recorded(pid_file)


def test_the_reaper_leaves_a_recorded_pid_that_is_not_this_sandbox_alone(sandbox: Sandbox) -> None:
    """The other half: the kill is verified, not blanket. A pid file is written by processes that
    have already exited, and pids are recycled — signalling one whose argv does not name this
    sandbox would eventually kill a stranger's process, which is far worse than a leaked stub."""
    pid_file = sandbox.root.parent / "outsider.pids"
    outsider = subprocess.Popen(["sleep", "60"])  # started outside the sandbox, reaped below
    try:
        pid_file.write_text(f"{outsider.pid}\n")
        reap = Reaper()
        reap.children(pid_file, sandbox.root)

        reap.close(wait_timeout=1)

        assert outsider.poll() is None, "the reaper killed a process that was not its own"
    finally:
        outsider.kill()
        outsider.wait(timeout=60)
