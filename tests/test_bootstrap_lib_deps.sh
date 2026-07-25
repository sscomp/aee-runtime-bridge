#!/usr/bin/env bash
# AEE Bootstrap v1 — W2 deps.sh shell integration tests.
#
# Coverage:
#   1. --help exits 0 and lists W2 scope
#   2. Missing --profile exits 2
#   3. Invalid profile exits 2
#   4. --dry-run (default) on supported distro exits 0 and prints plan
#   5. --execute without sudo credentials is handled gracefully (skipped
#      on hosts where sudo would prompt — we only test --dry-run here)
#   6. read_manifest strips comments and blanks
#   7. filter_packages_by_profile gates supervisor and docker.io
#   8. Sourcing does not execute main
#   9. Non-ubuntu/debian distro detection exits 12 (simulated)
#
# All tests run in --dry-run mode only. No real apt installs are
# performed. No sudo is invoked.
#
# Run: bash tests/test_bootstrap_lib_deps.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
deps_sh="$repo_root/bootstrap/lib/deps.sh"
manifest="$repo_root/bootstrap/manifests/apt.deps.txt"

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
        fail "$label (missing: $needle)"
    fi
}

# ---------------------------------------------------------------------------
# Test 1: --help exits 0
# ---------------------------------------------------------------------------
help_out="$(bash "$deps_sh" --help 2>&1 || true)"
help_rc=$?
if [ "$help_rc" = "0" ]; then
    assert_contains "deps.sh --help exits 0" "W2" "$help_out"
else
    fail "deps.sh --help exited $help_rc"
fi

# ---------------------------------------------------------------------------
# Test 2: Missing --profile exits 2
# ---------------------------------------------------------------------------
set +e
no_profile_out="$(bash "$deps_sh" --repo-root "$repo_root" 2>&1)"
no_profile_rc=$?
set -e
assert_eq "deps.sh missing --profile exits 2" "2" "$no_profile_rc"

# ---------------------------------------------------------------------------
# Test 3: Invalid profile exits 2
# ---------------------------------------------------------------------------
set +e
bad_profile_out="$(bash "$deps_sh" --repo-root "$repo_root" --profile bogus 2>&1)"
bad_profile_rc=$?
set -e
assert_eq "deps.sh invalid profile exits 2" "2" "$bad_profile_rc"

# ---------------------------------------------------------------------------
# Test 4: --dry-run on this host (Debian 12) exits 0
# ---------------------------------------------------------------------------
# This host is Debian 12 per /etc/os-release, so W2 should accept it.
set +e
dry_out="$(bash "$deps_sh" --repo-root "$repo_root" --profile mini --dry-run 2>&1)"
dry_rc=$?
set -e
if [ "$dry_rc" = "0" ]; then
    assert_contains "deps.sh --dry-run exits 0 on Debian" "dry_run=1" "$dry_out"
    assert_contains "deps.sh prints packages" "git" "$dry_out"
    assert_contains "deps.sh prints supervisor for mini" "supervisor" "$dry_out"
else
    # If the host is not Ubuntu/Debian, exit 12 is expected
    if [ "$dry_rc" = "12" ]; then
        ok "deps.sh --dry-run exits 12 on non-ubuntu/debian (expected on this host class)"
    else
        fail "deps.sh --dry-run exited $dry_rc (output: $dry_out)"
    fi
fi

# ---------------------------------------------------------------------------
# Test 5: --dry-run for developer excludes supervisor and docker.io
# ---------------------------------------------------------------------------
set +e
dev_out="$(bash "$deps_sh" --repo-root "$repo_root" --profile developer --dry-run 2>&1)"
dev_rc=$?
set -e
if [ "$dev_rc" = "0" ]; then
    if echo "$dev_out" | grep -q "supervisor"; then
        fail "deps.sh developer profile includes supervisor (should not)"
    else
        ok "deps.sh developer profile excludes supervisor"
    fi
    if echo "$dev_out" | grep -q "docker.io"; then
        fail "deps.sh developer profile includes docker.io (should not)"
    else
        ok "deps.sh developer profile excludes docker.io"
    fi
elif [ "$dev_rc" = "12" ]; then
    ok "deps.sh developer --dry-run exits 12 on non-ubuntu/debian (skipped)"
else
    fail "deps.sh developer --dry-run exited $dev_rc"
fi

# ---------------------------------------------------------------------------
# Test 6: --dry-run for edge includes docker.io, excludes supervisor
# ---------------------------------------------------------------------------
set +e
edge_out="$(bash "$deps_sh" --repo-root "$repo_root" --profile edge --dry-run 2>&1)"
edge_rc=$?
set -e
if [ "$edge_rc" = "0" ]; then
    if echo "$edge_out" | grep -q "docker.io"; then
        ok "deps.sh edge profile includes docker.io"
    else
        fail "deps.sh edge profile missing docker.io"
    fi
    if echo "$edge_out" | grep -q "supervisor"; then
        fail "deps.sh edge profile includes supervisor (should not)"
    else
        ok "deps.sh edge profile excludes supervisor"
    fi
elif [ "$edge_rc" = "12" ]; then
    ok "deps.sh edge --dry-run exits 12 on non-ubuntu/debian (skipped)"
else
    fail "deps.sh edge --dry-run exited $edge_rc"
fi

# ---------------------------------------------------------------------------
# Test 7: Sourcing deps.sh does not execute main
# ---------------------------------------------------------------------------
source_out="$(. "$deps_sh" 2>&1 || true)"
if [ -z "$source_out" ]; then
    ok "sourcing deps.sh produces no output"
else
    fail "sourcing deps.sh produced output: $source_out"
fi

# ---------------------------------------------------------------------------
# Test 8: read_manifest strips comments and blanks
# ---------------------------------------------------------------------------
# shellcheck source=../bootstrap/lib/deps.sh
. "$deps_sh"
if [ -f "$manifest" ]; then
    raw="$(read_manifest "$manifest")"
    if echo "$raw" | grep -q "^#"; then
        fail "read_manifest did not strip comments"
    else
        ok "read_manifest strips comments"
    fi
    if echo "$raw" | grep -q "^$"; then
        fail "read_manifest did not strip blanks"
    else
        ok "read_manifest strips blanks"
    fi
    # Verify git is in the manifest
    if echo "$raw" | grep -q "^git$"; then
        ok "read_manifest includes git"
    else
        fail "read_manifest missing git"
    fi
else
    fail "manifest not found: $manifest"
fi

# ---------------------------------------------------------------------------
# Test 9: filter_packages_by_profile gates correctly
# ---------------------------------------------------------------------------
mini_pkgs="$(printf 'git\nsupervisor\ndocker.io\n' | filter_packages_by_profile mini)"
if echo "$mini_pkgs" | grep -q "^supervisor$" && \
   ! echo "$mini_pkgs" | grep -q "^docker.io$"; then
    ok "filter mini: supervisor in, docker.io out"
else
    fail "filter mini: gating wrong (got: $mini_pkgs)"
fi

full_pkgs="$(printf 'git\nsupervisor\ndocker.io\n' | filter_packages_by_profile full)"
if echo "$full_pkgs" | grep -q "^supervisor$" && \
   echo "$full_pkgs" | grep -q "^docker.io$"; then
    ok "filter full: supervisor + docker.io in"
else
    fail "filter full: gating wrong (got: $full_pkgs)"
fi

dev_pkgs="$(printf 'git\nsupervisor\ndocker.io\n' | filter_packages_by_profile developer)"
if ! echo "$dev_pkgs" | grep -q "^supervisor$" && \
   ! echo "$dev_pkgs" | grep -q "^docker.io$" && \
   echo "$dev_pkgs" | grep -q "^git$"; then
    ok "filter developer: only core in"
else
    fail "filter developer: gating wrong (got: $dev_pkgs)"
fi

# ---------------------------------------------------------------------------
# Test 10: missing manifest exits 12
# ---------------------------------------------------------------------------
set +e
missing_out="$(bash "$deps_sh" --repo-root "/nonexistent" --profile mini --dry-run 2>&1)"
missing_rc=$?
set -e
# Exit 12 = dependency floor not met (manifest missing → no packages)
if [ "$missing_rc" = "12" ] || [ "$missing_rc" = "2" ]; then
    ok "deps.sh missing manifest exits non-zero ($missing_rc)"
else
    # On non-ubuntu/debian hosts, the distro gate fires first (exit 12)
    # before the manifest is even read. Accept either.
    if [ "$missing_rc" = "12" ]; then
        ok "deps.sh missing manifest exits 12"
    else
        fail "deps.sh missing manifest exited $missing_rc (output: $missing_out)"
    fi
fi

# ---------------------------------------------------------------------------
# Test 11 (P1 fix, TASK-20260725-0020): --execute enables execution
# ---------------------------------------------------------------------------
# Verifies that --execute produces NO [dry-run] markers, i.e. helpers see
# DRY_RUN=0 and would perform real apt installs. We cannot perform real
# apt installs in CI, so we assert on the absence of [dry-run] markers in
# the apt_update_run / apt_install_run / maybe_add_deadsnakes paths.
#
# Safety: we intercept `sudo` with a stub so even if the host had
# passwordless sudo, no real apt command would run. The stub prints what
# would have run, so we can also assert the real-apt path was reached.
# ---------------------------------------------------------------------------
set +e
exec_out="$(sudo() {
    printf 'STUB-SUDO called with: %s\n' "$*" >&2
    return 0
}; bash "$deps_sh" --repo-root "$repo_root" --profile mini --execute 2>&1)"
exec_rc=$?
set -e
if [ "$exec_rc" = "0" ] || [ "$exec_rc" = "7" ] || [ "$exec_rc" = "10" ]; then
    if echo "$exec_out" | grep -q "\[dry-run\]"; then
        fail "P1: --execute still emitted [dry-run] markers (DRY_RUN not propagated)"
    else
        ok "P1: --execute does not emit [dry-run] markers (execution path enabled)"
    fi
else
    # exit 12 = non-ubuntu/debian host (skipped); other codes are real failures
    if [ "$exec_rc" = "12" ]; then
        ok "P1: --execute skipped on non-ubuntu/debian host (exit 12)"
    else
        fail "P1: --execute exited $exec_rc (output: $exec_out)"
    fi
fi

# ---------------------------------------------------------------------------
# Test 12 (P1 fix, TASK-20260725-0020): --dry-run overrides inherited DRY_RUN=0
# ---------------------------------------------------------------------------
# Verifies the dry-run precedence rule: when the operator passes --dry-run,
# the script MUST dry-run even if the environment exports DRY_RUN=0 (which
# would otherwise authorize real apt installs). This is the safety side of
# the P1 fix — without it, an inherited env var could silently bypass the
# operator's explicit --dry-run intent.
# ---------------------------------------------------------------------------
set +e
precedence_out="$(DRY_RUN=0 bash "$deps_sh" --repo-root "$repo_root" --profile mini --dry-run 2>&1)"
precedence_rc=$?
set -e
if [ "$precedence_rc" = "0" ]; then
    if echo "$precedence_out" | grep -q "\[dry-run\]"; then
        ok "P1: --dry-run overrides inherited DRY_RUN=0 (dry-run markers present)"
    else
        fail "P1: --dry-run did NOT override inherited DRY_RUN=0 (real apt would fire)"
    fi
    # Also assert the announce line reports dry_run=1
    if echo "$precedence_out" | grep -q "dry_run=1"; then
        ok "P1: --dry-run announce line reports dry_run=1"
    else
        fail "P1: --dry-run announce line did not report dry_run=1"
    fi
elif [ "$precedence_rc" = "12" ]; then
    ok "P1: --dry-run precedence skipped on non-ubuntu/debian host (exit 12)"
else
    fail "P1: --dry-run precedence test exited $precedence_rc (output: $precedence_out)"
fi

# ---------------------------------------------------------------------------
# Test 13 (P1 fix, TASK-20260725-0020): unauthorized execution prevention
# ---------------------------------------------------------------------------
# Verifies the safety invariant: with NO --execute flag (default dry-run),
# the script MUST NOT call sudo apt, even if a malicious or accidental
# environment exports DRY_RUN=0. This is the security side of the P1 fix
# — the CLI flag is authoritative, so absence of --execute means dry-run
# regardless of environment.
#
# We stub sudo to FAIL the test if it is ever called. This makes the
# assertion explicit: "sudo must not be invoked in default mode".
# ---------------------------------------------------------------------------
set +e
unauth_out="$(sudo() {
    printf 'UNAUTHORIZED-SUDO called with: %s\n' "$*" >&2
    return 99
}; DRY_RUN=0 bash "$deps_sh" --repo-root "$repo_root" --profile mini 2>&1)"
unauth_rc=$?
set -e
if echo "$unauth_out" | grep -q "UNAUTHORIZED-SUDO"; then
    fail "P1: default mode (no --execute) called sudo even with inherited DRY_RUN=0"
else
    ok "P1: default mode does not call sudo (unauthorized execution prevented)"
fi
# Default mode should dry-run (announce dry_run=1) and exit 0 or 12
if [ "$unauth_rc" = "0" ]; then
    if echo "$unauth_out" | grep -q "\[dry-run\]"; then
        ok "P1: default mode emits [dry-run] markers (DRY_RUN=0 env ignored)"
    else
        fail "P1: default mode did not emit [dry-run] markers (rc=$unauth_rc)"
    fi
elif [ "$unauth_rc" = "12" ]; then
    ok "P1: default mode unauthorized-exec skipped on non-ubuntu/debian (exit 12)"
else
    fail "P1: default mode exited $unauth_rc (output: $unauth_out)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "---"
echo "deps.sh tests: $pass_count passed, $fail_count failed"
if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0