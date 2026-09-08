#!/usr/bin/env bash
# Run the repo-carried mutation controls and require each to fail exactly its named probes.
set -u
# Immune to an inherited `bash -e` / `bash -o pipefail`: every mutation run is EXPECTED to exit 1,
# and every status this script judges is captured explicitly.
set +e
set +o pipefail

MODULE="tests/test_lock_protocol_concurrency.py"
MANIFEST_DEFAULT="tests/mutations/expected.tsv"
MANIFEST="$MANIFEST_DEFAULT"
MARKER_VAR="MEMNG267_MUTATION"
SELFTEST=0

usage() {
    cat <<'USAGE'
scripts/check-mutations.sh — run the repo-carried mutation controls (tests/mutations/).

THIS RUN MUTATES THE WORKING TREE it is run in: each patch is applied in place with `git apply`,
the module is run, and the patch is reversed again, one at a time. Nothing else may read or write
that checkout while it runs — another reader sees mutated source, another writer loses its edits.
The patch is reversed on interrupt too (EXIT/INT/TERM), and every revert is proved by sha256; if
one cannot be proved the script stops and names the file to restore by hand.

Run from the repository root. For every row of tests/mutations/expected.tsv the script:

  * refuses to start if the file that row's patch touches has uncommitted changes, so the
    revert can never clobber an author's edits;
  * records that file's sha256, applies the patch, runs
    `uv run pytest -q tests/test_lock_protocol_concurrency.py` with MEMNG267_MUTATION set to
    the row's marker, then reverses the patch and compares the sha256 — a mismatch stops the run;
  * requires pytest to exit 1 and the set of FAILED node ids to equal the row's expected_failed
    EXACTLY — no more, no fewer — and, when the row names a sqlstate, that the sqlstate appears
    in the run's log.

Before the loop the module is run ONCE with MEMNG267_MUTATION removed from the environment (not
merely unset in the manifest) as the positive control; it must exit 0.
One line is printed per mutation (`<marker>: PASS|FAIL — <reason>`), then a summary line.
The exit status is non-zero if the control, any row, or any revert failed.

The manifest is tab-separated: patch<TAB>marker<TAB>expected_failed<TAB>sqlstate. expected_failed
is a space-separated list of test ids — a bare id is read as a node of the module above, and an
id containing `::` is read as a full node id. sqlstate is optional. Blank lines and lines
starting with `#` are ignored.

The DATABASE comes from the caller's environment: export both DATABASE_URL_SYNC (psycopg2) and
DATABASE_URL (asyncpg) at a disposable local database before running — tests/db_guard.py refuses
anything else. This script neither creates, migrates nor drops a database.

Options:
  --manifest <path>   read the rows from <path> instead of tests/mutations/expected.tsv
  --selftest          prove the checker can fail: a parse leg (two distinct FAILED nodes must not
                      collapse onto one id), a manifest naming a wrong expected set (must report
                      FAIL), a run with MEMNG267_MUTATION exported (the control must still pass),
                      and the real manifest (must report PASS); non-zero if any is violated
  -h, --help          print this text
USAGE
}

die() {
    printf 'check-mutations: %s\n' "$1" >&2
    exit 2
}

# pytest's short summary line is `FAILED <nodeid> - <message>`, and the message may be absent.
# The node id is taken WHOLE up to the first ` - `: a parameter id may contain spaces and `::`,
# so cutting on either collapses distinct nodes onto one name
# (`…::test_other[x::test_probe y]` would read as `test_probe`).
parse_failed_ids() {
    sed -n 's/^FAILED \(.*\)$/\1/p' "$1" | sed 's/ - .*$//'
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
    one_row=$(mktemp) || die "mktemp failed"
    run_out=$(mktemp) || die "mktemp failed"
    fake_log=$(mktemp) || die "mktemp failed"
    trap 'rm -f "$wrong" "$one_row" "$run_out" "$fake_log"' EXIT

    # --- leg 1: two distinct failed nodes must not collapse onto one expected id ------------
    echo "selftest leg 1: a parameterised node whose id contains '::' and a space"
    {
        printf 'FAILED %s::test_probe - assertion\n' "$MODULE"
        printf 'FAILED %s::test_other[x::test_probe y] - assertion\n' "$MODULE"
    } > "$fake_log"
    parsed=$(parse_failed_ids "$fake_log")
    parsed_n=$(printf '%s\n' "$parsed" | grep -c .)
    printf '%s\n' "$parsed" | sed 's/^/  parsed: /'
    if [ "$parsed_n" -ne 2 ]; then
        echo "SELFTEST FAIL: parsed $parsed_n node id(s) from 2 FAILED lines"
        exit 1
    fi
    if [ "$(printf '%s\n' "$parsed" | LC_ALL=C sort -u | grep -c .)" -ne 2 ]; then
        echo "SELFTEST FAIL: the two distinct nodes collapsed onto one id"
        exit 1
    fi
    if [ "$(printf '%s\n' "$parsed" | LC_ALL=C sort -u)" = "$MODULE::test_probe" ]; then
        echo "SELFTEST FAIL: the parsed set equals the single expected id"
        exit 1
    fi
    echo "selftest leg 1: PASS — 2 distinct node ids, neither set equal to $MODULE::test_probe"

    # --- leg 2: a wrong expected set must be caught ------------------------------------------
    # A real mutation with a real test id that it does NOT fail: the run still exits 1, so only
    # the set comparison can catch it.
    printf 'tests/mutations/c5b_merge_answers.patch\tc5b_merge_answers\ttest_pass_zero_locks_the_named_and_the_dedup_attachment_in_one_ascending_statement\t\n' > "$wrong"
    echo "selftest leg 2: expecting FAIL from a manifest with a wrong expected set"
    "$SCRIPT_PATH" --manifest "$wrong" 2>&1 | tee "$run_out"
    wrong_rc=${PIPESTATUS[0]}
    echo "selftest leg 2: run exited $wrong_rc (expected non-zero)"
    if [ "$wrong_rc" -eq 0 ]; then
        echo "SELFTEST FAIL: the checker passed a manifest naming the wrong expected set"
        exit 1
    fi
    # Non-zero alone is not proof: a refusal (exit 2) would also be non-zero. The run has to have
    # reached the mutation and judged it.
    if ! grep -q '^c5b_merge_answers: FAIL ' "$run_out"; then
        echo "SELFTEST FAIL: the wrong-manifest run exited $wrong_rc without judging c5b_merge_answers FAIL"
        exit 1
    fi
    echo "selftest leg 2: PASS"

    # --- leg 3: an inherited marker must not reach the positive control ----------------------
    grep -v '^#' "$MANIFEST_DEFAULT" | grep -m1 'c5b_merge_answers' > "$one_row"
    [ -s "$one_row" ] || die "no c5b_merge_answers row in $MANIFEST_DEFAULT"
    echo "selftest leg 3: expecting PASS with $MARKER_VAR=no_r7a exported by the caller"
    ( export "$MARKER_VAR=no_r7a" && "$SCRIPT_PATH" --manifest "$one_row" ) 2>&1 | tee "$run_out"
    marker_rc=${PIPESTATUS[0]}
    echo "selftest leg 3: run exited $marker_rc (expected 0)"
    if [ "$marker_rc" -ne 0 ] || ! grep -q '^control: PASS' "$run_out"; then
        echo "SELFTEST FAIL: an exported $MARKER_VAR reached the positive control"
        exit 1
    fi
    echo "selftest leg 3: PASS"

    # --- leg 4: the real manifest must pass ---------------------------------------------------
    echo "selftest leg 4: expecting PASS from $MANIFEST_DEFAULT"
    "$SCRIPT_PATH" --manifest "$MANIFEST_DEFAULT"
    real_rc=$?
    echo "selftest leg 4: run exited $real_rc (expected 0)"
    if [ "$real_rc" -ne 0 ]; then
        echo "SELFTEST FAIL: the checker did not pass the real manifest"
        exit 1
    fi
    echo "SELFTEST PASS: parse leg clean, wrong manifest -> FAIL ($wrong_rc), exported marker -> PASS ($marker_rc), real manifest -> PASS ($real_rc)"
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
# an applied patch is reverted even if the run is interrupted or the caller set errexit
# ------------------------------------------------------------------------------------------
applied_patch=""
applied_target=""
applied_sha=""

revert_applied() {
    # Echoes the outcome and returns 0 only when the target is provably back to `applied_sha`.
    [ -n "$applied_patch" ] || return 0
    local patch="$applied_patch" target="$applied_target" before="$applied_sha" note="" now
    applied_patch=""
    git apply -R "$patch" || note="git apply -R failed"
    now=$(sha256sum "$target" | cut -d' ' -f1)
    if [ "$now" != "$before" ]; then
        echo "check-mutations: $target was NOT restored (sha256 $before -> $now${note:+; $note}) — restore it by hand" >&2
        return 1
    fi
    [ -z "$note" ] || {
        echo "check-mutations: $note, though $target matches its recorded sha256" >&2
        return 1
    }
    return 0
}

on_signal() {
    local sig="$1"
    trap - EXIT INT TERM
    if [ -n "$applied_patch" ]; then
        echo "" >&2
        echo "check-mutations: interrupted ($sig) with $applied_patch applied — reverting" >&2
    fi
    revert_applied
    case "$sig" in INT) exit 130 ;; TERM) exit 143 ;; *) exit 1 ;; esac
}

on_exit() {
    local rc=$?
    trap - EXIT INT TERM
    if [ -n "$applied_patch" ]; then
        echo "check-mutations: exiting with $applied_patch applied — reverting" >&2
        revert_applied || rc=1
    fi
    exit "$rc"
}

trap 'on_exit' EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

# ------------------------------------------------------------------------------------------
# positive control: the module is green on the tree as it stands
# ------------------------------------------------------------------------------------------
echo "control: running $MODULE with $MARKER_VAR removed from the environment (must exit 0)"
env -u "$MARKER_VAR" uv run pytest -q "$MODULE" > "$logdir/control.log" 2>&1 < /dev/null
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
    # Recorded BEFORE the apply, so the trap can revert whatever the apply managed to write.
    applied_patch="$patch"
    applied_target="$target"
    applied_sha="$before"

    git apply "$patch"
    apply_rc=$?
    if [ "$apply_rc" -ne 0 ]; then
        revert_applied || exit 1
        applied_patch=""
        echo "$marker: FAIL — $patch does not apply to $target"
        failures=$((failures + 1))
        continue
    fi

    env "$MARKER_VAR=$marker" uv run pytest -q "$MODULE" > "$log" 2>&1 < /dev/null
    rc=$?

    if ! revert_applied; then
        echo "$marker: FAIL — $target was not provably restored"
        echo "SUMMARY: aborted at $marker with the tree modified (logs in $logdir)"
        exit 1
    fi

    mapfile -t actual_list < <(parse_failed_ids "$log")
    actual_ids=$(printf '%s\n' "${actual_list[@]}" | grep . | LC_ALL=C sort -u)
    expected_ids=$(
        # shellcheck disable=SC2086  # expected_failed is a space-separated list; splitting is the point
        for id in $expected; do
            case "$id" in *::*) printf '%s\n' "$id" ;; *) printf '%s::%s\n' "$MODULE" "$id" ;; esac
        done | LC_ALL=C sort -u
    )
    parsed_n=$(printf '%s\n' "${actual_list[@]}" | grep -c .)
    reported_n=$(tail -3 "$log" | grep -oE '[0-9]+ failed' | tail -1 | awk '{print $1}')
    reported_n=${reported_n:-0}

    reasons=()
    if [ "$rc" -ne 1 ]; then
        reasons+=("pytest exited $rc, expected 1")
    fi
    if [ "$parsed_n" -ne "$reported_n" ]; then
        reasons+=("parsed $parsed_n FAILED line(s) but pytest reported $reported_n failed — the log did not parse")
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
        n=$(printf '%s\n' "$expected_ids" | grep -c .)
        echo "$marker: PASS — exit 1, exactly $n expected FAILED node id(s)${sqlstate:+, sqlstate $sqlstate present}"
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
