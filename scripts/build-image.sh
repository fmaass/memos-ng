#!/usr/bin/env bash
# Build the deploy image from this checkout, under the mutation gate lock.
#
# WHY THIS IS NOT A BARE `docker build`. The build context is the whole working tree, and
# scripts/check-mutations.sh patches tracked source in place while it runs. During the 1.7.0
# release a build read the tree while app/services/uploads.py was patched, and only a file-by-file
# sha256 comparison afterwards established that the image was clean.
#
# Two things are therefore checked, and they are not the same thing:
#
#   * the LOCK answers "is a mutation run patching this tree right now". This script WAITS for it,
#     saying so once on stderr, because a build that starts a minute later is still a correct
#     build. (The gate refuses instead of waiting, because a gate that blocks reads as a hang.)
#   * the CLEAN-TREE check answers "did one leave a patch behind". A run killed with SIGKILL runs
#     no trap: it cannot revert, and the kernel frees its lock anyway. So the lock going free is
#     no proof at all that the tree is intact, and only this second check catches that case.
#
# Usage: scripts/build-image.sh <version-string>      (run from the repository root)
#   e.g. scripts/build-image.sh 1.7.1+abc1234
# APP_VERSION must also be set to the same string in the stack compose — see RELEASING.md.
#
# IMAGE_TAG=memos-ng:<name> builds under another tag. A build that only proves the slim image still
# compiles must not move `memos-ng:local`: that tag is what the stack runs on its next recreate, and
# the rollback step is keyed on it still naming the running image.
set -uo pipefail

die() {
    printf 'build-image: %s\n' "$1" >&2
    exit 2
}

case "${1:-}" in
    -h | --help)
        printf 'usage: scripts/build-image.sh <version-string>   # e.g. 1.7.1+abc1234\n'
        exit 0
        ;;
esac

[ "$#" -eq 1 ] && [ -n "$1" ] || die "usage: scripts/build-image.sh <version-string>"
version="$1"

tag="${IMAGE_TAG:-memos-ng:local}"
# docker's own tag grammar, spelled out: anything else is refused here, before the lock is taken
# and long before docker sees it, rather than left to whatever docker makes of it.
[[ "$tag" =~ ^memos-ng:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] ||
    die "IMAGE_TAG must name a memos-ng:<tag> image with a plain docker tag, not '$tag'"

toplevel=$(git rev-parse --show-toplevel 2> /dev/null) || die "not a git repository"
[ "$toplevel" = "$PWD" ] || die "run from the repository root ($toplevel), not $PWD"

# The same lock, resolved the same way as in scripts/check-mutations.sh: per WORKING TREE, because
# that is what a patch lands in and what this build reads as its context. A build in another
# worktree is reading other files and must not queue behind this one.
gitdir=$(git rev-parse --absolute-git-dir 2> /dev/null) || die "cannot locate the git directory"
LOCKFILE="$gitdir/mutation-gate.lock"
exec 9> "$LOCKFILE" || die "cannot open the mutation gate lock at $LOCKFILE"
if ! flock -n 9; then
    # Who else could be holding it: a mutation run (which has this checkout patched right now) or
    # another build of the same checkout. Both are reasons to wait rather than to read the tree.
    printf 'build-image: waiting for the mutation gate lock (%s) — a mutation run or another build holds it\n' \
        "$LOCKFILE" >&2
    flock 9 || die "could not take the mutation gate lock at $LOCKFILE"
fi

# Under the lock, so what is read here cannot change underneath: a tracked file that differs from
# HEAD is what a patch left behind looks like. Untracked files are not checked — they are not in
# HEAD, and .dockerignore decides what reaches the context.
dirty=$(git diff --name-only HEAD --) || die "cannot compare the working tree with HEAD"
if [ -n "$dirty" ]; then
    printf 'build-image: these tracked files differ from HEAD, so the image would not be built from HEAD:\n' >&2
    # shellcheck disable=SC2086  # one line per path; git printed them newline-separated
    printf '  %s\n' $dirty >&2
    printf 'build-image: if a mutation run was killed, restore them (git checkout -- <file>) and run this again.\n' >&2
    exit 1
fi

# exec, deliberately: docker inherits fd 9 and therefore the lock, so the lock lasts at least as
# long as the build reads the context — it is the docker client's whole lifetime, of which the
# context transfer is the part that matters here. A `docker build` started as a child and waited
# for would be equivalent only as long as nobody ever adds an early `exit` above it.
exec docker build --build-arg APP_VERSION="$version" -t "$tag" .
