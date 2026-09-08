#!/usr/bin/env bash
# Run the repo-carried mutation controls and require each to fail exactly its named probes.
set -u

MODULE="tests/test_lock_protocol_concurrency.py"
MANIFEST_DEFAULT="tests/mutations/expected.tsv"
MANIFEST="$MANIFEST_DEFAULT"
SELFTEST=0

usage() {
    cat <<'USAGE'
scripts/check-mutations.sh — run the repo-carried mutation controls (tests/mutations/).

Run from the repository root. For every row of tests/mutations/expected.tsv the script:

  * refuses to start if the file that row's patch touches has uncommitted changes, so the
    revert can never clobber an author's edits;
  * records that file's sha256, applies the patch with `git apply`, runs
    `uv run pytest -q tests/test_lock_protocol_concurrency.py` with MEMNG267_MUTATION set to
    the row's marker, then reverses the patch with `git apply -R` and compares the sha256 —
    a mismatch is a hard failure and stops the run;
  * requires pytest to exit 1 and the set of FAILED test ids to equal the row's
    expected_failed EXACTLY — no more, no fewer — and, when the row names a sqlstate, that
    the sqlstate appears in the run's log.

Before the loop the module is run ONCE with no marker as the positive control; it must exit 0.
One line is printed per mutation (`<marker>: PASS|FAIL — <reason>`), then a summary line.
The exit status is non-zero if the control, any row, or any revert failed.

The manifest is tab-separated: patch<TAB>marker<TAB>expected_failed<TAB>sqlstate. expected_failed
is a space-separated list of test ids; sqlstate is optional. Blank lines and lines starting with
`#` are ignored.

The DATABASE comes from the caller's environment: export both DATABASE_URL_SYNC (psycopg2) and
DATABASE_URL (asyncpg) at a disposable local database before running — tests/db_guard.py refuses
anything else. This script neither creates, migrates nor drops a database.

Options:
  --manifest <path>   read the rows from <path> instead of tests/mutations/expected.tsv
  --selftest          prove the checker can fail: run it against a temporary manifest naming a
                      wrong expected set for a real mutation (must report FAIL) and against the
                      real manifest (must report PASS); non-zero if either expectation is violated
  -h, --help          print this text
USAGE
}

die() {
    printf 'check-mutations: %s\n' "$1" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --selftest)
            SELFTEST=1
            shift
            ;;
        --manifest)
            [ $# -ge 2 ] || die "--manifest needs a path"
            MANIFEST="$2"
            shift 2
            ;;
        *) die "unknown argument '$1' (see --help)" ;;
    esac
done

SCRIPT_PATH="$0"

# ------------------------------------------------------------------------------------------
# --selftest: the checker must be able to report FAIL, not only PASS.
# ------------------------------------------------------------------------------------------
if [ "$SELFTEST" -eq 1 ]; then
    [ "$MANIFEST" = "$MANIFEST_DEFAULT" ] || die "--selftest takes no --manifest; it uses both its own and the real one"
    [ -f "$MANIFEST_DEFAULT" ] || die "no $MANIFEST_DEFAULT — run from the repository root"
    wrong=$(mktemp) || die "mktemp failed"
    wrong_out=$(mktemp) || die "mktemp failed"
    trap 'rm -f "$wrong" "$wrong_out"' EXIT
    # A real mutation with a real test id that it does NOT fail: the run still exits 1, so only
    # the set comparison can catch it.
    printf 'tests/mutations/c5b_merge_answers.patch\tc5b_merge_answers\ttest_pass_zero_locks_the_named_and_the_dedup_attachment_in_one_ascending_statement\t\n' > "$wrong"

    echo "selftest: expecting FAIL from a manifest with a wrong expected set"
    "$SCRIPT_PATH" --manifest "$wrong" 2>&1 | tee "$wrong_out"
    wrong_rc=${PIPESTATUS[0]}
    echo "selftest: wrong-manifest run exited $wrong_rc (expected non-zero)"
    if [ "$wrong_rc" -eq 0 ]; then
        echo "SELFTEST FAIL: the checker passed a manifest naming the wrong expected set"
        exit 1
    fi
    # Non-zero alone is not proof: a refusal (exit 2) would also be non-zero. The run has to have
    # reached the mutation and judged it.
    if ! grep -q '^c5b_merge_answers: FAIL ' "$wrong_out"; then
        echo "SELFTEST FAIL: the wrong-manifest run exited $wrong_rc without judging c5b_merge_answers FAIL"
        exit 1
    fi

    echo "selftest: expecting PASS from $MANIFEST_DEFAULT"
    "$SCRIPT_PATH" --manifest "$MANIFEST_DEFAULT"
    real_rc=$?
    echo "selftest: real-manifest run exited $real_rc (expected 0)"
    if [ "$real_rc" -ne 0 ]; then
        echo "SELFTEST FAIL: the checker did not pass the real manifest"
        exit 1
    fi
    echo "SELFTEST PASS: wrong manifest -> FAIL ($wrong_rc), real manifest -> PASS ($real_rc)"
    exit 0
fi

# ------------------------------------------------------------------------------------------
# preflight
# ------------------------------------------------------------------------------------------
toplevel=$(git rev-parse --show-toplevel 2> /dev/null) || die "not a git repository"
[ "$toplevel" = "$PWD" ] || die "run from the repository root ($toplevel), not $PWD"
[ -f "$MANIFEST" ] || die "no manifest at $MANIFEST"
[ -f "$MODULE" ] || die "no $MODULE"
command -v uv > /dev/null 2>&1 || die "uv is not on PATH"

rows=()
while IFS= read -r line; do
    case "$line" in '' | '#'*) continue ;; esac
    rows+=("$line")
done < "$MANIFEST"
[ "${#rows[@]}" -gt 0 ] || die "$MANIFEST names no mutations"

# Resolve each row and refuse before touching anything if a patched file is already modified.
patches=()
markers=()
expecteds=()
sqlstates=()
targets=()
for line in "${rows[@]}"; do
    IFS=$'\t' read -r patch marker expected sqlstate <<< "$line"
    sqlstate=${sqlstate:-}
    [ -n "${patch:-}" ] && [ -n "${marker:-}" ] && [ -n "${expected:-}" ] ||
        die "malformed row in $MANIFEST: $line"
    [ -f "$patch" ] || die "no patch file $patch (named in $MANIFEST)"
    target=$(grep -m1 '^+++ b/' "$patch" | sed 's#^+++ b/##')
    [ -n "$target" ] || die "$patch names no target file (+++ b/...)"
    [ -f "$target" ] || die "$patch targets $target, which does not exist"
    git diff --quiet -- "$target" ||
        die "$target has uncommitted changes; the revert of $patch would clobber them"
    git diff --quiet --cached -- "$target" ||
        die "$target has staged changes; the revert of $patch would clobber them"
    patches+=("$patch")
    markers+=("$marker")
    expecteds+=("$expected")
    sqlstates+=("$sqlstate")
    targets+=("$target")
done

logdir=$(mktemp -d) || die "mktemp -d failed"
echo "check-mutations: manifest $MANIFEST, ${#patches[@]} mutation(s), logs in $logdir"

failures=0

# ------------------------------------------------------------------------------------------
# positive control: the module is green on the tree as it stands
# ------------------------------------------------------------------------------------------
echo "control: running $MODULE with no marker (must exit 0)"
uv run pytest -q "$MODULE" > "$logdir/control.log" 2>&1 < /dev/null
control_rc=$?
if [ "$control_rc" -eq 0 ]; then
    echo "control: PASS — the unmutated module exits 0"
else
    echo "control: FAIL — the unmutated module exited $control_rc, so no mutation verdict is meaningful (see $logdir/control.log)"
    echo "SUMMARY: 0/${#patches[@]} mutations checked, control FAILED"
    exit 1
fi

# ------------------------------------------------------------------------------------------
# one mutation per row
# ------------------------------------------------------------------------------------------
for i in "${!patches[@]}"; do
    patch="${patches[$i]}"
    marker="${markers[$i]}"
    expected="${expecteds[$i]}"
    sqlstate="${sqlstates[$i]}"
    target="${targets[$i]}"
    log="$logdir/$marker.log"

    before=$(sha256sum "$target" | cut -d' ' -f1)

    if ! git apply "$patch"; then
        echo "$marker: FAIL — $patch does not apply to $target"
        failures=$((failures + 1))
        continue
    fi

    MEMNG267_MUTATION="$marker" uv run pytest -q "$MODULE" > "$log" 2>&1 < /dev/null
    rc=$?

    revert_note=""
    if ! git apply -R "$patch"; then
        revert_note="git apply -R failed"
    fi
    after=$(sha256sum "$target" | cut -d' ' -f1)
    if [ "$before" != "$after" ]; then
        echo "$marker: FAIL — $target was not restored (sha256 $before -> $after${revert_note:+; $revert_note})"
        echo "SUMMARY: aborted at $marker with the tree modified — restore $target by hand (logs in $logdir)"
        exit 1
    fi
    if [ -n "$revert_note" ]; then
        echo "$marker: FAIL — $revert_note"
        failures=$((failures + 1))
        continue
    fi

    actual_ids=$(grep '^FAILED ' "$log" | awk '{print $2}' | sed 's/.*:://' | LC_ALL=C sort -u)
    # shellcheck disable=SC2086  # expected_failed is a space-separated list; splitting is the point
    expected_ids=$(printf '%s\n' $expected | LC_ALL=C sort -u)

    reasons=()
    if [ "$rc" -ne 1 ]; then
        reasons+=("pytest exited $rc, expected 1")
    fi
    if [ "$actual_ids" != "$expected_ids" ]; then
        extra=$(comm -13 <(printf '%s\n' "$expected_ids") <(printf '%s\n' "$actual_ids") | paste -sd' ' -)
        missing=$(comm -23 <(printf '%s\n' "$expected_ids") <(printf '%s\n' "$actual_ids") | paste -sd' ' -)
        reasons+=("FAILED set differs${missing:+; did not fail: $missing}${extra:+; also failed: $extra}")
    fi
    if [ -n "$sqlstate" ] && ! grep -qF -- "$sqlstate" "$log"; then
        reasons+=("sqlstate $sqlstate absent from the log")
    fi

    if [ "${#reasons[@]}" -eq 0 ]; then
        n=$(printf '%s\n' "$expected_ids" | wc -l | tr -d ' ')
        echo "$marker: PASS — exit 1, exactly $n expected FAILED id(s)${sqlstate:+, sqlstate $sqlstate present}"
    else
        printf '%s: FAIL — ' "$marker"
        printf '%s' "${reasons[0]}"
        for r in "${reasons[@]:1}"; do printf '; %s' "$r"; done
        printf ' (see %s)\n' "$log"
        failures=$((failures + 1))
    fi
done

passed=$((${#patches[@]} - failures))
if [ "$failures" -eq 0 ]; then
    echo "SUMMARY: $passed/${#patches[@]} mutations behaved as $MANIFEST names them, control green"
    exit 0
fi
echo "SUMMARY: $failures/${#patches[@]} mutations did NOT behave as $MANIFEST names them (logs in $logdir)"
exit 1
