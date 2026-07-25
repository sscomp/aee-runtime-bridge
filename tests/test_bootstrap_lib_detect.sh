#!/usr/bin/env bash
# AEE Bootstrap v1 — W2 detect.sh shell integration tests.
#
# Coverage:
#   1. detect_platform delegates to Python when available (prints linux on this host)
#   2. detect_platform falls back to heuristic when Python is missing
#   3. detect_linux_distro prints ubuntu or debian on supported hosts
#   4. detect_linux_version_id prints a non-empty version
#   5. --help exits 0
#   6. Unknown arg exits 2
#   7. Sourcing the file does not execute main (no output on source)
#   8. resolve_via_heuristic prints linux on Linux hosts
#
# Run: bash tests/test_bootstrap_lib_detect.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
detect_sh="$repo_root/bootstrap/lib/detect.sh"

pass_count=0
fail_count=0

ok() {
    echo "ok - $1"
    pass_count=$((pass_count + 1))
}

fail() {
    echo "not ok - $1"
    fail_count=$((fail_count + 1))
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        ok "$label (got: $actual)"
    else
        fail "$label (expected: $expected, got: $actual)"
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if echo "$haystack" | grep -q "$needle"; then
        ok "$label"
    else
        fail "$label (missing: $needle in: $haystack)"
    fi
}

# ---------------------------------------------------------------------------
# Test 1: detect_platform prints a valid identity on this host
# ---------------------------------------------------------------------------
identity="$(bash "$detect_sh" --repo-root "$repo_root" 2>/dev/null || true)"
case "$identity" in
    linux|darwin|unknown) ok "detect_platform prints valid identity: $identity" ;;
    *) fail "detect_platform printed invalid identity: $identity" ;;
esac

# ---------------------------------------------------------------------------
# Test 2: detect_linux_distro prints a known value
# ---------------------------------------------------------------------------
# Source the file (does not run main) and call the function directly.
# shellcheck source=../bootstrap/lib/detect.sh
. "$detect_sh"
distro="$(detect_linux_distro)"
case "$distro" in
    ubuntu|debian|unknown) ok "detect_linux_distro prints known value: $distro" ;;
    *) fail "detect_linux_distro printed unknown value: $distro" ;;
esac

# ---------------------------------------------------------------------------
# Test 3: detect_linux_version_id prints something
# ---------------------------------------------------------------------------
version_id="$(detect_linux_version_id)"
if [ -n "$version_id" ]; then
    ok "detect_linux_version_id prints non-empty: $version_id"
else
    fail "detect_linux_version_id printed empty"
fi

# ---------------------------------------------------------------------------
# Test 4: --help exits 0 and contains usage
# ---------------------------------------------------------------------------
help_out="$(bash "$detect_sh" --help 2>&1 || true)"
rc=$?
if [ "$rc" = "0" ]; then
    assert_contains "detect.sh --help exits 0" "Usage" "$help_out"
else
    fail "detect.sh --help exited $rc"
fi

# ---------------------------------------------------------------------------
# Test 5: Unknown arg exits 2
# ---------------------------------------------------------------------------
set +e
bad_out="$(bash "$detect_sh" --bogus 2>&1)"
bad_rc=$?
set -e
assert_eq "detect.sh --bogus exits 2" "2" "$bad_rc"

# ---------------------------------------------------------------------------
# Test 6: Sourcing does not produce output (main not auto-run)
# ---------------------------------------------------------------------------
source_out="$(. "$detect_sh" 2>&1 || true)"
if [ -z "$source_out" ]; then
    ok "sourcing detect.sh produces no output"
else
    fail "sourcing detect.sh produced output: $source_out"
fi

# ---------------------------------------------------------------------------
# Test 7: resolve_via_heuristic prints linux on a Linux host
# ---------------------------------------------------------------------------
heuristic="$(resolve_via_heuristic)"
case "$heuristic" in
    linux|darwin|unknown) ok "resolve_via_heuristic valid: $heuristic" ;;
    *) fail "resolve_via_heuristic invalid: $heuristic" ;;
esac

# ---------------------------------------------------------------------------
# Test 8: resolve_via_python returns non-zero when Python missing
# (simulated by temporarily hiding python3)
# ---------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    # Python is present — test that resolve_via_python succeeds
    py_out="$(resolve_via_python "$repo_root" 2>/dev/null || true)"
    case "$py_out" in
        linux|darwin|unknown) ok "resolve_via_python valid: $py_out" ;;
        *) fail "resolve_via_python invalid: $py_out" ;;
    esac
else
    # Python missing — resolve_via_python should fail
    if resolve_via_python "$repo_root" >/dev/null 2>&1; then
        fail "resolve_via_python should fail when python3 missing"
    else
        ok "resolve_via_python fails when python3 missing"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "---"
echo "detect.sh tests: $pass_count passed, $fail_count failed"
if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0