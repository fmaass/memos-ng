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

A TEST THAT ERRORS DOES NOT COUNT AS A FAILING PROBE. Only a `<failure>` — the test ran and its
assertion did not hold — can satisfy an expectation. Any `<error>` (setup, teardown or
collection) makes the whole mutation run INVALID and is reported as `FAIL — <n> error(s): <ids>`,
because a probe that never executed proves nothing about the mutation.

NODE IDS COME FROM THE JUNIT REPORT, not from the terminal output: the run is given
`-o junit_family=xunit1 --junitxml=<file>` and each failed or errored `<testcase>` is read with
stdlib `xml.etree`, its id rebuilt as `<file>::<class>::<nested class>::<name>`. The `name`
attribute carries the parameter bracket verbatim, so an id containing ` - `, `::` or a space
survives it — the terminal `FAILED <nodeid> - <message>` line cannot be parsed back without
ambiguity. As a second, independent guard the number of failures read from the report is
cross-checked against pytest's own summary line, which must report that many failed and zero
errors; a run whose summary line cannot be found is failed, not waved through.

Before the loop the module is run ONCE with MEMNG267_MUTATION removed from the environment (not
merely unset in the manifest) as the positive control; it must exit 0.
One line is printed per mutation (`<marker>: PASS|FAIL — <reason>`), then a summary line.
The exit status is non-zero if the control, any row, or any revert failed.

The manifest is tab-separated: patch<TAB>marker<TAB>expected_failed<TAB>sqlstate. expected_failed
is a space-separated list of test ids — a bare id is read as a node of the module above, and an
id containing `::` is read as a full node id. sqlstate is optional. Blank lines and lines
starting with `#` are ignored.

The pytest output of every run is kept in a temporary directory whose path is printed, so a FAIL
can be read afterwards; the JUnit reports themselves are deleted on exit, having been read.

The DATABASE comes from the caller's environment: export both DATABASE_URL_SYNC (psycopg2) and
DATABASE_URL (asyncpg) at a disposable local database before running — tests/db_guard.py refuses
anything else. This script neither creates, migrates nor drops a database.

Options:
  --manifest <path>   read the rows from <path> instead of tests/mutations/expected.tsv
  --selftest          prove the checker can fail: a parse leg (parameter brackets containing
                      ` - `, `::` and spaces, and a nested class, must all reconstruct), an
                      errored-probe leg (an `<error>` must never satisfy an expectation), a
                      manifest naming a wrong expected set (must report FAIL), a run with
                      MEMNG267_MUTATION exported (the control must still pass), an INT sent while
                      a patch is applied (the tree must come back identical), and the real
                      manifest (must report PASS); non-zero if any is violated
  -h, --help          print this text
USAGE
}

die() {
    printf 'check-mutations: %s\n' "$1" >&2
    exit 2
}

# One `<kind>\t<node id>` line per failed or errored testcase of a pytest JUnit report, in report
# order. A case that both failed and errored is reported as an error: the run is not clean.
PARSE_PY='
import sys
import xml.etree.ElementTree as ET

for case in ET.parse(sys.argv[1]).iter("testcase"):
    if case.find("error") is not None:
        kind = "error"
    elif case.find("failure") is not None:
        kind = "failure"
    else:
        continue
    path = case.get("file") or ""
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
    tail = classname[len(module):].lstrip(".") if classname.startswith(module) else ""
    classes = tail.split(".") if tail else []
    print(kind + "\t" + "::".join(part for part in [path, *classes, name] if part))
'

parse_junit_cases() {
    uv run python -c "$PARSE_PY" "$1"
}

# pytest's own count of failures and errors, from its summary line only — an unanchored search of
# the tail would happily read a number out of a traceback.
summary_line() {
    grep -E '^(=+ )?[0-9]+ [a-z]+' "$1" | grep -E ' in [0-9]+\.[0-9]+s' | tail -1
}

count_in_summary() {
    # $1 summary line, $2 word ("failed" / "error")
    printf '%s\n' "$1" | grep -oE "[0-9]+ $2" | head -1 | awk '{print $1}'
}

# Prints `<marker>: PASS|FAIL — …` and returns 0 only for a run that behaved as the row names it.
judge_run() {
    local marker="$1" rc="$2" log="$3" xml="$4" expected="$5" sqlstate="$6"
    local reasons=() failure_ids=() error_ids=() case_list=() entry kind id
    local actual_ids expected_ids extra missing summary failed_n errors_n n

    if [ ! -s "$xml" ]; then
        reasons+=("pytest wrote no JUnit report, so its failures cannot be identified")
        actual_ids=""
    else
        mapfile -t case_list < <(parse_junit_cases "$xml")
        for entry in "${case_list[@]}"; do
            [ -n "$entry" ] || continue
            kind=${entry%%$'\t'*}
            id=${entry#*$'\t'}
            case "$kind" in
                error) error_ids+=("$id") ;;
                failure) failure_ids+=("$id") ;;
            esac
        done
        # An errored probe never ran: it can satisfy nothing, and it invalidates the whole run.
        if [ "${#error_ids[@]}" -gt 0 ]; then
            reasons+=("${#error_ids[@]} error(s): ${error_ids[*]}")
        fi
        actual_ids=$(printf '%s\n' "${failure_ids[@]}" | grep . | LC_ALL=C sort -u)

        summary=$(summary_line "$log")
        if [ -z "$summary" ]; then
            reasons+=("no pytest summary line in $log, so the report cannot be cross-checked")
        else
            failed_n=$(count_in_summary "$summary" failed)
            errors_n=$(count_in_summary "$summary" error)
            failed_n=${failed_n:-0}
            errors_n=${errors_n:-0}
            if [ "${#failure_ids[@]}" -ne "$failed_n" ]; then
                reasons+=("the JUnit report holds ${#failure_ids[@]} failure(s) but pytest summarised $failed_n failed")
            fi
            if [ "$errors_n" -ne 0 ]; then
                reasons+=("pytest summarised $errors_n error(s); a valid mutation run has none")
            fi
        fi
    fi

    if [ "$rc" -ne 1 ]; then
        reasons+=("pytest exited $rc, expected 1")
    fi

    expected_ids=$(
        # shellcheck disable=SC2086  # expected_failed is a space-separated list; splitting is the point
        for id in $expected; do
            case "$id" in *::*) printf '%s\n' "$id" ;; *) printf '%s::%s\n' "$MODULE" "$id" ;; esac
        done | LC_ALL=C sort -u
    )
    if [ "$actual_ids" != "$expected_ids" ]; then
        extra=$(comm -13 <(printf '%s\n' "$expected_ids") <(printf '%s\n' "$actual_ids") | paste -sd' ' -)
        missing=$(comm -23 <(printf '%s\n' "$expected_ids") <(printf '%s\n' "$actual_ids") | paste -sd' ' -)
        reasons+=("failed set differs${missing:+; did not fail: $missing}${extra:+; also failed: $extra}")
    fi
    if [ -n "$sqlstate" ] && ! grep -qF -- "$sqlstate" "$log"; then
        reasons+=("sqlstate $sqlstate absent from the log")
    fi

    if [ "${#reasons[@]}" -eq 0 ]; then
        n=$(printf '%s\n' "$expected_ids" | grep -c .)
        echo "$marker: PASS — exit 1, exactly $n expected failed node id(s)${sqlstate:+, sqlstate $sqlstate present}"
        return 0
    fi
    printf '%s: FAIL — ' "$marker"
    printf '%s' "${reasons[0]}"
    for entry in "${reasons[@]:1}"; do printf '; %s' "$entry"; done
    printf ' (see %s)\n' "$log"
    return 1
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
    fake_xml=$(mktemp) || die "mktemp failed"
    fake_log=$(mktemp) || die "mktemp failed"
    trap 'rm -f "$wrong" "$one_row" "$run_out" "$fake_xml" "$fake_log"' EXIT

    grep -v '^#' "$MANIFEST_DEFAULT" | grep -m1 'c5b_merge_answers' > "$one_row"
    [ -s "$one_row" ] || die "no c5b_merge_answers row in $MANIFEST_DEFAULT"
    IFS=$'\t' read -r one_patch _ one_expected _ < "$one_row"
    one_target=$(grep -m1 '^+++ b/' "$one_patch" | sed 's#^+++ b/##')
    [ -f "$one_target" ] || die "$one_patch targets $one_target, which does not exist"

    # --- leg 1: ids the terminal output cannot round-trip, and a nested class -----------------
    echo "selftest leg 1: parameter ids containing ' - ', '::' and spaces, and a nested class"
    cat > "$fake_xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="4" tests="4">
<testcase classname="tests.test_lock_protocol_concurrency" name="test_probe[a]" file="$MODULE" line="1"><failure message="assertion">boom</failure></testcase>
<testcase classname="tests.test_lock_protocol_concurrency" name="test_probe[a - extra]]" file="$MODULE" line="1"><failure message="assertion">boom</failure></testcase>
<testcase classname="tests.test_lock_protocol_concurrency" name="test_other[x::test_probe y]" file="$MODULE" line="1"><failure message="assertion">boom</failure></testcase>
<testcase classname="tests.test_lock_protocol_concurrency.TestOuter.TestInner" name="test_deep" file="$MODULE" line="1"><failure message="assertion">boom</failure></testcase>
</testsuite></testsuites>
XML
    parsed=$(parse_junit_cases "$fake_xml")
    printf '%s\n' "$parsed" | sed 's/^/  parsed: /'
    if [ "$(printf '%s\n' "$parsed" | grep -c .)" -ne 4 ] ||
        [ "$(printf '%s\n' "$parsed" | LC_ALL=C sort -u | grep -c .)" -ne 4 ]; then
        echo "SELFTEST FAIL: 4 distinct testcases did not parse as 4 distinct entries"
        exit 1
    fi
    for want in \
        "failure	$MODULE::test_probe[a]" \
        "failure	$MODULE::test_probe[a - extra]]" \
        "failure	$MODULE::TestOuter::TestInner::test_deep"; do
        if [ "$(printf '%s\n' "$parsed" | grep -cFx "$want")" -ne 1 ]; then
            echo "SELFTEST FAIL: '$want' is not exactly one of the parsed entries"
            exit 1
        fi
    done
    echo "selftest leg 1: PASS — 4 distinct entries; 'test_probe[a]' and 'test_probe[a - extra]]' apart, TestOuter::TestInner::test_deep joined with '::'"

    # --- leg 2: an errored probe must never satisfy an expectation ----------------------------
    echo "selftest leg 2: the expected probe reported as an <error>, not a <failure>"
    cat > "$fake_xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="0" tests="2">
<testcase classname="tests.test_lock_protocol_concurrency" name="$one_expected" file="$MODULE" line="1"><error message="failed on setup with &quot;Exception&quot;">boom</error></testcase>
<testcase classname="tests.test_lock_protocol_concurrency" name="test_something_else" file="$MODULE" line="2"/>
</testsuite></testsuites>
XML
    printf '1 error, 22 passed, 2 warnings in 20.00s\n' > "$fake_log"
    judge_out=$(judge_run c5b_error_probe 1 "$fake_log" "$fake_xml" "$one_expected" "")
    judge_rc=$?
    echo "  $judge_out"
    if [ "$judge_rc" -eq 0 ]; then
        echo "SELFTEST FAIL: an errored probe satisfied its expectation"
        exit 1
    fi
    case "$judge_out" in
        "c5b_error_probe: FAIL — 1 error(s): $MODULE::$one_expected"*) ;;
        *)
            echo "SELFTEST FAIL: the errored run was not reported as an error"
            exit 1
            ;;
    esac
    echo "selftest leg 2: PASS — reported as an error, not as a satisfied expectation"

    # --- leg 3: a wrong expected set must be caught -------------------------------------------
    # A real mutation with a real test id that it does NOT fail: the run still exits 1, so only
    # the set comparison can catch it.
    printf 'tests/mutations/c5b_merge_answers.patch\tc5b_merge_answers\ttest_pass_zero_locks_the_named_and_the_dedup_attachment_in_one_ascending_statement\t\n' > "$wrong"
    echo "selftest leg 3: expecting FAIL from a manifest with a wrong expected set"
    "$SCRIPT_PATH" --manifest "$wrong" 2>&1 | tee "$run_out"
    wrong_rc=${PIPESTATUS[0]}
    echo "selftest leg 3: run exited $wrong_rc (expected non-zero)"
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
    echo "selftest leg 3: PASS"

    # --- leg 4: an inherited marker must not reach the positive control -----------------------
    echo "selftest leg 4: expecting PASS with $MARKER_VAR=no_r7a exported by the caller"
    ( export "$MARKER_VAR=no_r7a" && "$SCRIPT_PATH" --manifest "$one_row" ) 2>&1 | tee "$run_out"
    marker_rc=${PIPESTATUS[0]}
    echo "selftest leg 4: run exited $marker_rc (expected 0)"
    if [ "$marker_rc" -ne 0 ] || ! grep -q '^control: PASS' "$run_out"; then
        echo "SELFTEST FAIL: an exported $MARKER_VAR reached the positive control"
        exit 1
    fi
    echo "selftest leg 4: PASS"

    # --- leg 5: an INT while the patch is applied must still restore the tree -----------------
    echo "selftest leg 5: INT while $one_patch is applied; $one_target must come back identical"
    clean_sha=$(sha256sum "$one_target" | cut -d' ' -f1)
    # Job control for the launch: a non-interactive shell sets SIGINT to IGNORE in an
    # asynchronous command, and a trap cannot reset a signal inherited as ignored — the INT
    # would never reach the handler this leg exists to prove.
    set -m
    "$SCRIPT_PATH" --manifest "$one_row" > "$run_out" 2>&1 &
    victim=$!
    set +m
    applied=0
    for _ in $(seq 1 1200); do
        if [ "$(sha256sum "$one_target" | cut -d' ' -f1)" != "$clean_sha" ]; then
            applied=1
            break
        fi
        kill -0 "$victim" 2> /dev/null || break
        sleep 0.1
    done
    if [ "$applied" -ne 1 ]; then
        kill -TERM "$victim" 2> /dev/null
        wait "$victim"
        echo "SELFTEST FAIL: the run never had $one_target mutated, so the INT proves nothing"
        exit 1
    fi
    echo "  $one_target is mutated; sending INT to pid $victim"
    kill -INT "$victim"
    wait "$victim"
    int_rc=$?
    sed 's/^/  /' "$run_out" | tail -3
    after_sha=$(sha256sum "$one_target" | cut -d' ' -f1)
    echo "  interrupted run exited $int_rc; $one_target sha256 $after_sha (clean $clean_sha)"
    if [ "$int_rc" -ne 130 ]; then
        echo "SELFTEST FAIL: the interrupted run exited $int_rc, expected 130 — the INT did not reach the handler"
        exit 1
    fi
    if [ "$after_sha" != "$clean_sha" ] || ! git diff --quiet -- "$one_target"; then
        echo "SELFTEST FAIL: $one_target was left mutated by the interrupted run"
        exit 1
    fi
    echo "selftest leg 5: PASS — tree identical after INT"

    # --- leg 6: the real manifest must pass ---------------------------------------------------
    echo "selftest leg 6: expecting PASS from $MANIFEST_DEFAULT"
    "$SCRIPT_PATH" --manifest "$MANIFEST_DEFAULT"
    real_rc=$?
    echo "selftest leg 6: run exited $real_rc (expected 0)"
    if [ "$real_rc" -ne 0 ]; then
        echo "SELFTEST FAIL: the checker did not pass the real manifest"
        exit 1
    fi
    echo "SELFTEST PASS: parse leg clean, errored probe -> FAIL, wrong manifest -> FAIL ($wrong_rc), exported marker -> PASS ($marker_rc), INT while applied -> tree restored ($int_rc), real manifest -> PASS ($real_rc)"
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
xmldir=$(mktemp -d) || die "mktemp -d failed"
echo "check-mutations: manifest $MANIFEST, ${#patches[@]} mutation(s), pytest logs in $logdir"
echo "check-mutations: JUnit reports go to $xmldir and are deleted on exit, having been read"

failures=0

# ------------------------------------------------------------------------------------------
# an applied patch is reverted even if the run is interrupted or the caller set errexit
# ------------------------------------------------------------------------------------------
applied_patch=""
applied_target=""
applied_sha=""

revert_applied() {
    # Idempotent: reverses the patch only if it is still applied, always proves the sha256, and
    # clears the cleanup state ONLY once that proof holds — so a signal arriving anywhere in here
    # leaves a second call with everything it needs to finish the job.
    [ -n "$applied_patch" ] || return 0
    local patch="$applied_patch" target="$applied_target" before="$applied_sha" note="" now
    if git apply -R --check "$patch" 2> /dev/null; then
        git apply -R "$patch" || note="git apply -R failed"
    fi
    now=$(sha256sum "$target" | cut -d' ' -f1)
    if [ "$now" != "$before" ]; then
        echo "check-mutations: $target was NOT restored (sha256 $before -> $now${note:+; $note}) — restore it by hand" >&2
        return 1
    fi
    if [ -n "$note" ]; then
        echo "check-mutations: $note, though $target matches its recorded sha256" >&2
        return 1
    fi
    applied_patch=""
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
    rm -rf "$xmldir"
    case "$sig" in INT) exit 130 ;; TERM) exit 143 ;; *) exit 1 ;; esac
}

on_exit() {
    local rc=$?
    trap - EXIT INT TERM
    if [ -n "$applied_patch" ]; then
        echo "check-mutations: exiting with $applied_patch applied — reverting" >&2
        revert_applied || rc=1
        [ "$rc" -ne 0 ] || rc=1
    fi
    rm -rf "$xmldir"
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
    xml="$xmldir/$marker.xml"

    # Recorded BEFORE the apply, so the trap can revert whatever the apply managed to write.
    applied_patch="$patch"
    applied_target="$target"
    applied_sha=$(sha256sum "$target" | cut -d' ' -f1)

    git apply "$patch"
    apply_rc=$?
    if [ "$apply_rc" -ne 0 ]; then
        revert_applied || exit 1
        echo "$marker: FAIL — $patch does not apply to $target"
        failures=$((failures + 1))
        continue
    fi

    env "$MARKER_VAR=$marker" uv run pytest -q -o junit_family=xunit1 --junitxml="$xml" \
        "$MODULE" > "$log" 2>&1 < /dev/null
    rc=$?

    if ! revert_applied; then
        echo "$marker: FAIL — $target was not provably restored"
        echo "SUMMARY: aborted at $marker with the tree modified (logs in $logdir)"
        exit 1
    fi

    judge_run "$marker" "$rc" "$log" "$xml" "$expected" "$sqlstate" ||
        failures=$((failures + 1))
done

passed=$((${#patches[@]} - failures))
if [ "$failures" -eq 0 ]; then
    echo "SUMMARY: $passed/${#patches[@]} mutations behaved as $MANIFEST names them, control green"
    exit 0
fi
echo "SUMMARY: $failures/${#patches[@]} mutations did NOT behave as $MANIFEST names them (logs in $logdir)"
exit 1
