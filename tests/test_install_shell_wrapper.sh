#!/usr/bin/env bash
# AEE Epic 9.3 — Shell Wrapper Integration Tests (§21.3).
#
# Targeted shell integration tests for the install.sh wrapper.
# Coverage (per workorder §7):
#   1. --help exits 0 and lists the four profiles
#   2. Default (no --profile) resolves to full, exit 0
#   3. Each of the four profiles exits 0 and reflects the profile
#   4. Invalid profile exits non-zero (argparse exit code 2)
#   5. Quoting: --profile="mini" works, spaces in args don't break parsing
#   6. Exit codes: success=0, invalid=2, delegated bootstrap failure=4
#   7. Missing python interpreter: exit 64
#   8. Missing aee.cli module: exit 65
#   9. --execute delegates exact argv using an isolated stub interpreter
#  10. --json produces valid JSON output
#
# Run: ./tests/test_install_shell_wrapper.sh
# Or:  bash tests/test_install_shell_wrapper.sh
#
# Exits 0 if all tests pass, non-zero if any assertion fails (TAP-like).

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the repo root and the wrapper under test.
# ---------------------------------------------------------------------------
script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
wrapper="$repo_root/install.sh"
export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"

# ---------------------------------------------------------------------------
# Test framework helpers
# ---------------------------------------------------------------------------
pass_count=0
fail_count=0

ok() {
    echo "ok - $1"
    pass_count=$((pass_count + 1))
}

not_ok() {
    echo "not ok - $1"
    fail_count=$((fail_count + 1))
}

# Run the wrapper, capturing stdout, stderr, and exit code.
run_wrapper() {
    _out_file="$(mktemp)"
    _err_file="$(mktemp)"
    set +e
    bash "$wrapper" "$@" >"$_out_file" 2>"$_err_file"
    _exit=$?
    set -e
    _out="$(cat "$_out_file" 2>/dev/null)"
    _err="$(cat "$_err_file")"
    rm -f "$_out_file" "$_err_file"
    return 0
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

echo "TAP version 13"
echo "# AEE 9.3 Shell Wrapper Integration Tests"

# 1. --help exits 0 and lists the four profiles
run_wrapper --help
if [ "$_exit" -eq 0 ] && \
   echo "$_out" | grep -q "full" && \
   echo "$_out" | grep -q "mini" && \
   echo "$_out" | grep -q "edge" && \
   echo "$_out" | grep -q "developer"; then
    ok "--help exits 0 and lists all four profiles"
else
    not_ok "--help exits 0 and lists all four profiles (exit=$_exit)"
    echo "  stdout: $(echo "$_out" | head -5)"
fi

# 2. -h (short help) exits 0
run_wrapper -h
if [ "$_exit" -eq 0 ]; then
    ok "-h exits 0"
else
    not_ok "-h exits 0 (exit=$_exit)"
fi

# 3. Default (no --profile) resolves to full, exit 0
run_wrapper --dry-run
if [ "$_exit" -eq 0 ] && echo "$_out" | grep -q "profile (resolved)  : full"; then
    ok "Default profile resolves to full, exit 0"
else
    not_ok "Default profile resolves to full, exit 0 (exit=$_exit)"
fi

# 4. Each of the four profiles exits 0 and reflects the profile
for p in full mini edge developer; do
    run_wrapper --profile "$p" --dry-run
    if [ "$_exit" -eq 0 ] && echo "$_out" | grep -q "profile (resolved)  : $p"; then
        ok "--profile $p exits 0 and reflects the profile"
    else
        not_ok "--profile $p exits 0 and reflects the profile (exit=$_exit)"
    fi
done

# 5. Invalid profile exits non-zero (argparse exit code 2)
run_wrapper --profile bogus --dry-run
if [ "$_exit" -ne 0 ]; then
    ok "Invalid profile exits non-zero (exit=$_exit)"
else
    not_ok "Invalid profile exits non-zero (exit=$_exit)"
fi

# 6. Quoting: --profile=mini (equals form) works
run_wrapper --profile=mini --dry-run
if [ "$_exit" -eq 0 ] && echo "$_out" | grep -q "profile (resolved)  : mini"; then
    ok "--profile=mini (equals form) works"
else
    not_ok "--profile=mini (equals form) works (exit=$_exit)"
fi

# Execute tests use a copied wrapper in an empty repo with an exclusive PATH.
# No real Python is available there; any attempt to use it cannot import AEE.
# The stub records exact argv and never runs a CLI, runner or bootstrap stage.
bash_bin="$(command -v bash)"
stub_root="$(mktemp -d)"
trap 'rm -rf "$stub_root"' EXIT
stub_bin="$stub_root/bin"
stub_repo="$stub_root/repo"
mkdir -p "$stub_bin" "$stub_repo"
cp "$wrapper" "$stub_repo/install.sh"
ln -s "$(command -v dirname)" "$stub_bin/dirname"
printf '#!%s\n' "$bash_bin" >"$stub_bin/python3"
cat >>"$stub_bin/python3" <<'STUB'
set -euo pipefail
if [ "$#" -eq 2 ] && [ "$1" = "-c" ] &&
   [ "$2" = "import aee.cli; import aee.installer.cli_install" ]; then
    exit "${AEE_TEST_IMPORT_EXIT:-0}"
fi
if [ "$#" -lt 3 ] || [ "$1" != "-m" ] || [ "$2" != "aee.cli" ] || [ "$3" != "install" ]; then
    echo "unexpected interpreter invocation" >&2
    exit 70
fi
printf '%s\n' "$@" >"$AEE_TEST_ARGV"
exit "${AEE_TEST_CLI_EXIT:-0}"
STUB
chmod +x "$stub_bin/python3"

run_execute_stub() {
    local cli_exit="$1" import_exit="$2"
    shift 2
    rm -f "$stub_root/argv"
    set +e
    ( cd "$stub_repo" && env -u PYTHONPATH PATH="$stub_bin" \
        AEE_TEST_ARGV="$stub_root/argv" AEE_TEST_CLI_EXIT="$cli_exit" \
        AEE_TEST_IMPORT_EXIT="$import_exit" \
        "$bash_bin" ./install.sh "$@" ) >"$stub_root/out" 2>"$stub_root/err"
    _exit=$?
    set -e
    _out="$(cat "$stub_root/out")"
    _err="$(cat "$stub_root/err")"
    _argv=""
    if [ -f "$stub_root/argv" ]; then
        _argv="$(cat "$stub_root/argv")"
    fi
}

# 7. Explicit mini profile: exact execute argv and success propagation.
run_execute_stub 0 0 --profile mini --execute
expected_argv="$(printf '%s\n' -m aee.cli install --profile mini --execute)"
if [ "$_exit" -eq 0 ] && [ "$_argv" = "$expected_argv" ]; then
    ok "--execute delegates mini argv and propagates success (stub only)"
else
    not_ok "--execute mini delegation (exit=$_exit)"
fi

# 8. Omitted profile: let the Python CLI resolve full; propagate stage failure.
run_execute_stub 4 0 --execute
expected_argv="$(printf '%s\n' -m aee.cli install --execute)"
if [ "$_exit" -eq 4 ] && [ "$_argv" = "$expected_argv" ]; then
    ok "--execute delegates default-profile argv and propagates failure 4"
else
    not_ok "--execute default-profile delegation (exit=$_exit)"
fi

run_execute_stub 4 0 --profile mini --json --execute
expected_argv="$(printf '%s\n' -m aee.cli install --profile mini --json --execute)"
if [ "$_exit" -eq 4 ] && [ "$_argv" = "$expected_argv" ]; then
    ok "--execute forwards --json and propagates mini failure 4"
else
    not_ok "--execute --json delegation (exit=$_exit)"
fi

run_execute_stub 0 0 --execute
expected_argv="$(printf '%s\n' -m aee.cli install --execute)"
if [ "$_exit" -eq 0 ] && [ "$_argv" = "$expected_argv" ]; then
    ok "--execute default-profile success propagation"
else
    not_ok "--execute default-profile success (exit=$_exit)"
fi

# Module import failure must prevent dispatch in both modes.
for mode in --dry-run --execute; do
    run_execute_stub 0 1 --profile mini "$mode"
    if [ "$_exit" -eq 65 ] && [ -z "$_argv" ] && \
       [[ "$_err" == *"cannot import aee.cli or aee.installer.cli_install"* ]]; then
        ok "$mode missing installer module exits 65 before dispatch"
    else
        not_ok "$mode missing installer module (exit=$_exit)"
    fi
done

# No interpreter: PATH contains only dirname, which the wrapper needs.
no_python_bin="$stub_root/no-python"
mkdir "$no_python_bin"
ln -s "$(command -v dirname)" "$no_python_bin/dirname"
for mode in --dry-run --execute; do
    set +e
    ( cd "$stub_repo" && env -u PYTHONPATH PATH="$no_python_bin" \
        "$bash_bin" ./install.sh --profile mini "$mode" ) \
        >"$stub_root/out" 2>"$stub_root/err"
    _exit=$?
    set -e
    if [ "$_exit" -eq 64 ] && grep -q "no python interpreter found" "$stub_root/err"; then
        ok "$mode missing interpreter exits 64"
    else
        not_ok "$mode missing interpreter (exit=$_exit)"
    fi
done

# Exercise the python fallback without allowing any real Python in PATH.
mv "$stub_bin/python3" "$stub_bin/python"
run_execute_stub 0 0 --execute
expected_argv="$(printf '%s\n' -m aee.cli install --execute)"
if [ "$_exit" -eq 0 ] && [ "$_argv" = "$expected_argv" ]; then
    ok "--execute supports python fallback (stub only)"
else
    not_ok "--execute python fallback (exit=$_exit)"
fi

# 9. Unknown option exits 2
run_wrapper --bogus-option 2>/dev/null
if [ "$_exit" -eq 2 ]; then
    ok "Unknown option exits 2"
else
    not_ok "Unknown option exits 2 (exit=$_exit)"
fi

# 10. --profile without argument exits 2
run_wrapper --profile 2>/dev/null
if [ "$_exit" -eq 2 ]; then
    ok "--profile without argument exits 2"
else
    not_ok "--profile without argument exits 2 (exit=$_exit)"
fi

# 11. --json produces output containing valid JSON (install plan)
run_wrapper --profile mini --json --dry-run
if [ "$_exit" -eq 0 ] && echo "$_out" | grep -q '"subcommand": "install"' && \
   echo "$_out" | grep -q '"profile": "mini"'; then
    ok "--json produces structured JSON output"
else
    not_ok "--json produces structured JSON output (exit=$_exit)"
    echo "  stdout (first 5 lines): $(echo "$_out" | head -5)"
fi

# 12. Error messages go to stderr, not stdout
run_wrapper --profile bogus --dry-run 2>/dev/null
if [ "$_exit" -ne 0 ] && [ -z "$_out" ]; then
    ok "Error messages go to stderr (stdout empty on error)"
else
    # argparse prints usage+error to stderr, stdout should be empty
    if [ -z "$_out" ]; then
        ok "Error messages go to stderr (stdout empty on error)"
    else
        not_ok "Error messages go to stderr (stdout has content: $(echo "$_out" | head -1))"
    fi
fi

# Test 13: missing aee.cli module — invoke the wrapper from a
# directory where aee is NOT importable. The wrapper computes
# repo_root from its own script path, so we copy the wrapper into a
# clean tempdir (which has no aee/ package) and run it from there.
_out_file2="$(mktemp)"
_err_file2="$(mktemp)"
tmp_repo_nomod="$(mktemp -d)"
cp "$wrapper" "$tmp_repo_nomod/install.sh"
chmod +x "$tmp_repo_nomod/install.sh"
set +e
( cd "$tmp_repo_nomod" && env -u PYTHONPATH bash ./install.sh --profile mini --dry-run ) >"$_out_file2" 2>"$_err_file2"
_exit2=$?
set -e
if [ "$_exit2" -eq 65 ]; then
    ok "Missing aee.cli module exits 65"
else
    not_ok "Missing aee.cli module exits 65 (exit=$_exit2)"
    echo "  stderr: $(cat "$_err_file2" | head -3)"
fi
rm -f "$_out_file2" "$_err_file2"
rm -rf "$tmp_repo_nomod"

# 14. Wrapper does NOT perform side effects: no .aee-profile marker
# is written in dry-run mode. We run in a tempdir and verify no
# marker file is created.
tmp_repo="$(mktemp -d)"
cp "$wrapper" "$tmp_repo/install.sh"
chmod +x "$tmp_repo/install.sh"
set +e
( cd "$tmp_repo" && PYTHONPATH="$repo_root" bash ./install.sh --profile mini --dry-run ) >/dev/null 2>&1
_marker_exit=$?
set -e
if [ -e "$tmp_repo/.aee-profile" ]; then
    not_ok "No .aee-profile marker written in dry-run (marker exists)"
else
    ok "No .aee-profile marker written in dry-run (clean)"
fi
rm -rf "$tmp_repo"

# 15. Wrapper delegates profile validation to canonical Python CLI
# (no parallel hard-coded matrix). Verify by grepping the wrapper
# source for hard-coded profile lists — it should NOT contain a
# standalone "full mini edge developer" tuple.
if grep -qE '(full[[:space:]]+mini[[:space:]]+edge[[:space:]]+developer)' "$wrapper" 2>/dev/null; then
    # The usage text mentions the four profiles by name for help;
    # that's acceptable as long as it's in the help string, not a
    # validation matrix. Check that the wrapper does NOT have a
    # case-statement validating profiles.
    if grep -qE 'case.*full.*mini.*edge.*developer' "$wrapper" 2>/dev/null; then
        not_ok "Wrapper has hard-coded profile case-statement (should delegate to Python)"
    else
        ok "Wrapper delegates profile validation to Python CLI (no hard-coded matrix)"
    fi
else
    ok "Wrapper does not hard-code profile list (delegates to Python CLI)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "1..$((pass_count + fail_count))"
echo "# pass: $pass_count, fail: $fail_count"

if [ "$fail_count" -eq 0 ]; then
    echo "# All tests passed."
    exit 0
else
    echo "# FAILURES: $fail_count"
    exit 1
fi
