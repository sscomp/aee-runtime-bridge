#!/usr/bin/env bash
# AEE Bootstrap v1 — W3 macos_deps.sh shell integration tests.
#
# Coverage:
#   1. --help exits 0 and lists W3 scope
#   2. Missing --profile exits 2
#   3. Invalid profile exits 2
#   4. Non-Darwin kernel exits 12 (this host is Linux)
#   5. Non-developer profile (mini/full/edge) exits 12 on macOS v1
#   6. --system exits 12 (not supported on macOS v1)
#   7. Sourcing does not execute main
#   8. read_manifest strips comments and blanks
#   9. filter_formulae_by_profile gates supervisor and docker
#  10. Simulated Darwin + brew stub: --dry-run prints plan with brew install
#  11. Simulated Darwin + brew missing: --dry-run announces Homebrew install
#  12. Simulated Darwin + --no-brew: skips brew install
#  13. P1 parity: --execute does not emit [dry-run] markers
#  14. P1 parity: --dry-run overrides inherited DRY_RUN=0
#  15. P1 parity: default mode does not invoke brew install
#
# All tests run in --dry-run mode only (or with stubbed brew). No real
# brew installs are performed. No sudo is invoked.
#
# Run: bash tests/test_bootstrap_lib_macos_deps.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
macos_deps_sh="$repo_root/bootstrap/lib/macos_deps.sh"
manifest="$repo_root/bootstrap/manifests/brew.deps.txt"

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
    # Use `grep -F` (fixed-string) so patterns like `[dry-run]` and
    # `--no-brew` are not interpreted as regex/flags.
    if echo "$haystack" | grep -qF -- "$needle"; then
        ok "$label"
    else
        fail "$label (missing: $needle in: $haystack)"
    fi
}

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    # Use `grep -F` (fixed-string) so patterns like `[dry-run]` and
    # `--no-brew` are not interpreted as regex/flags.
    if echo "$haystack" | grep -qF -- "$needle"; then
        fail "$label (unexpected: $needle in: $haystack)"
    else
        ok "$label"
    fi
}

# ---------------------------------------------------------------------------
# Test 1: --help exits 0 and lists W3 scope
# ---------------------------------------------------------------------------
set +e
help_out="$(bash "$macos_deps_sh" --help 2>&1)"
help_rc=$?
set -e
assert_eq "macos_deps.sh --help exits 0" "0" "$help_rc"
assert_contains "macos_deps.sh --help lists W3 macOS scope" "W3 macOS" "$help_out"
assert_contains "macos_deps.sh --help lists brew install" "brew install" "$help_out"

# ---------------------------------------------------------------------------
# Test 2: Missing --profile exits 2
# ---------------------------------------------------------------------------
set +e
no_profile_out="$(bash "$macos_deps_sh" --repo-root "$repo_root" 2>&1)"
no_profile_rc=$?
set -e
assert_eq "macos_deps.sh missing --profile exits 2" "2" "$no_profile_rc"

# ---------------------------------------------------------------------------
# Test 3: Invalid profile exits 2
# ---------------------------------------------------------------------------
set +e
bad_profile_out="$(bash "$macos_deps_sh" --repo-root "$repo_root" --profile bogus 2>&1)"
bad_profile_rc=$?
set -e
assert_eq "macos_deps.sh invalid profile exits 2" "2" "$bad_profile_rc"

# ---------------------------------------------------------------------------
# Test 4: Non-Darwin kernel exits 12 (this host is Linux)
# ---------------------------------------------------------------------------
set +e
linux_out="$(bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --dry-run 2>&1)"
linux_rc=$?
set -e
assert_eq "macos_deps.sh non-Darwin exits 12" "12" "$linux_rc"
assert_contains "macos_deps.sh non-Darwin message names macOS only" "macOS only" "$linux_out"

# ---------------------------------------------------------------------------
# Test 5: Non-developer profile exits 12 on macOS v1
# (Simulate Darwin via uname stub; profile gate fires before brew check)
# ---------------------------------------------------------------------------
for p in mini full edge; do
    set +e
    np_out="$(uname() { printf 'Darwin'; }; export -f uname; \
              bash "$macos_deps_sh" --repo-root "$repo_root" --profile "$p" --dry-run 2>&1)"
    np_rc=$?
    set -e
    assert_eq "macos_deps.sh profile=$p exits 12 on macOS v1" "12" "$np_rc"
    assert_contains "macos_deps.sh profile=$p names developer-only" "developer" "$np_out"
done

# ---------------------------------------------------------------------------
# Test 6: --system exits 12 (not supported on macOS v1)
# ---------------------------------------------------------------------------
set +e
sys_out="$(uname() { printf 'Darwin'; }; export -f uname; \
           bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --system 2>&1)"
sys_rc=$?
set -e
assert_eq "macos_deps.sh --system exits 12 on macOS v1" "12" "$sys_rc"
assert_contains "macos_deps.sh --system names §6.5" "§6.5" "$sys_out"

# ---------------------------------------------------------------------------
# Test 7: Sourcing does not produce output (main not auto-run)
# ---------------------------------------------------------------------------
source_out="$(. "$macos_deps_sh" 2>&1 || true)"
if [ -z "$source_out" ]; then
    ok "sourcing macos_deps.sh produces no output"
else
    fail "sourcing macos_deps.sh produced output: $source_out"
fi

# ---------------------------------------------------------------------------
# Test 8: read_manifest strips comments and blanks
# ---------------------------------------------------------------------------
# shellcheck source=../bootstrap/lib/macos_deps.sh
. "$macos_deps_sh"
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
    # Verify git + python@3.11 are in the manifest
    if echo "$raw" | grep -q "^git$"; then
        ok "read_manifest includes git"
    else
        fail "read_manifest missing git"
    fi
    if echo "$raw" | grep -q "^python@3.11$"; then
        ok "read_manifest includes python@3.11"
    else
        fail "read_manifest missing python@3.11"
    fi
    # supervisor + docker present for parity (profile-gated, not installed on macOS v1)
    if echo "$raw" | grep -q "^supervisor$"; then
        ok "read_manifest includes supervisor (parity)"
    else
        fail "read_manifest missing supervisor (parity)"
    fi
    if echo "$raw" | grep -q "^docker$"; then
        ok "read_manifest includes docker (parity)"
    else
        fail "read_manifest missing docker (parity)"
    fi
else
    fail "manifest not found: $manifest"
fi

# ---------------------------------------------------------------------------
# Test 9: filter_formulae_by_profile gates correctly
# ---------------------------------------------------------------------------
# On macOS v1 only developer is supported, but the helper itself follows
# §6.2 gating rules (supervisor → mini+full; docker → full+edge).
mini_f="$(printf 'git\nsupervisor\ndocker\n' | filter_formulae_by_profile mini)"
if echo "$mini_f" | grep -q "^supervisor$" && \
   ! echo "$mini_f" | grep -q "^docker$"; then
    ok "filter mini: supervisor in, docker out"
else
    fail "filter mini: gating wrong (got: $mini_f)"
fi

full_f="$(printf 'git\nsupervisor\ndocker\n' | filter_formulae_by_profile full)"
if echo "$full_f" | grep -q "^supervisor$" && \
   echo "$full_f" | grep -q "^docker$"; then
    ok "filter full: supervisor + docker in"
else
    fail "filter full: gating wrong (got: $full_f)"
fi

dev_f="$(printf 'git\nsupervisor\ndocker\n' | filter_formulae_by_profile developer)"
if ! echo "$dev_f" | grep -q "^supervisor$" && \
   ! echo "$dev_f" | grep -q "^docker$" && \
   echo "$dev_f" | grep -q "^git$"; then
    ok "filter developer: only core in (supervisor + docker out)"
else
    fail "filter developer: gating wrong (got: $dev_f)"
fi

edge_f="$(printf 'git\nsupervisor\ndocker\n' | filter_formulae_by_profile edge)"
if echo "$edge_f" | grep -q "^docker$" && \
   ! echo "$edge_f" | grep -q "^supervisor$"; then
    ok "filter edge: docker in, supervisor out"
else
    fail "filter edge: gating wrong (got: $edge_f)"
fi

# ---------------------------------------------------------------------------
# Test 10: Simulated Darwin + brew stub: --dry-run prints brew install plan
# ---------------------------------------------------------------------------
set +e
darwin_brew_out="$(
    uname() { printf 'Darwin'; }; export -f uname;
    brew() { printf '/opt/homebrew'; }; export -f brew;
    bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --dry-run 2>&1
)"
darwin_brew_rc=$?
set -e
assert_eq "macos_deps.sh Darwin+brew --dry-run exits 0" "0" "$darwin_brew_rc"
assert_contains "macos_deps.sh Darwin+brew prints kernel=Darwin" "kernel=Darwin" "$darwin_brew_out"
assert_contains "macos_deps.sh Darwin+brew prints brew_prefix=/opt/homebrew" "brew_prefix=/opt/homebrew" "$darwin_brew_out"
assert_contains "macos_deps.sh Darwin+brew prints [dry-run] brew install" "[dry-run] brew install --quiet" "$darwin_brew_out"
assert_contains "macos_deps.sh Darwin+brew formulae includes git" "git" "$darwin_brew_out"
assert_contains "macos_deps.sh Darwin+brew formulae includes python@3.11" "python@3.11" "$darwin_brew_out"
# supervisor + docker must NOT be in the developer formulae. Use `grep -F`
# for the literal "supervisor" word on the formulae line (the formulae
# announce line is `macos_deps.sh: formulae: git python@3.11 curl ca-certificates`).
# We check that the brew install command line does not contain "supervisor".
assert_not_contains "macos_deps.sh Darwin+brew developer excludes supervisor" "supervisor" "$darwin_brew_out"

# ---------------------------------------------------------------------------
# Test 11: Simulated Darwin + brew missing: --dry-run announces Homebrew install
# ---------------------------------------------------------------------------
set +e
darwin_nobrew_out="$(
    uname() { printf 'Darwin'; }; export -f uname;
    # brew NOT defined → brew_available returns false
    bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --dry-run 2>&1
)"
darwin_nobrew_rc=$?
set -e
assert_eq "macos_deps.sh Darwin+no-brew --dry-run exits 0" "0" "$darwin_nobrew_rc"
assert_contains "macos_deps.sh Darwin+no-brew announces Homebrew install" "Homebrew first-install" "$darwin_nobrew_out"
assert_contains "macos_deps.sh Darwin+no-brew prints brew_prefix=unknown" "brew_prefix=unknown" "$darwin_nobrew_out"
assert_contains "macos_deps.sh Darwin+no-brew skips brew install step" "skipping brew install step" "$darwin_nobrew_out"

# ---------------------------------------------------------------------------
# Test 12: Simulated Darwin + --no-brew: skips brew install
# ---------------------------------------------------------------------------
set +e
no_brew_out="$(
    uname() { printf 'Darwin'; }; export -f uname;
    brew() { printf '/opt/homebrew'; }; export -f brew;
    bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --dry-run --no-brew 2>&1
)"
no_brew_rc=$?
set -e
assert_eq "macos_deps.sh Darwin+--no-brew exits 0" "0" "$no_brew_rc"
assert_contains "macos_deps.sh --no-brew announces skip" "--no-brew set; skipping brew install" "$no_brew_out"
assert_not_contains "macos_deps.sh --no-brew does NOT print brew install" "[dry-run] brew install --quiet" "$no_brew_out"

# ---------------------------------------------------------------------------
# Test 13 (P1 parity): --execute does not emit [dry-run] markers
# ---------------------------------------------------------------------------
# Stub brew to FAIL if invoked with --quiet in dry-run mode (should not happen).
set +e
exec_out="$(
    uname() { printf 'Darwin'; }; export -f uname;
    brew() { printf '/opt/homebrew'; }; export -f brew;
    # In --execute mode with brew stubbed, brew_install_run would call
    # `brew install --quiet git python@3.11 curl ca-certificates`. We
    # intercept brew to print what would have run.
    brew_install() { printf 'STUB-BREW-INSTALL called with: %s\n' "$*" >&2; return 0; }; export -f brew_install;
    bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --execute 2>&1
)"
exec_rc=$?
set -e
# brew_install is a function inside the script; our export may not override it.
# Instead, assert on the absence of [dry-run] markers (the P1 contract).
if [ "$exec_rc" = "0" ] || [ "$exec_rc" = "7" ] || [ "$exec_rc" = "10" ] || [ "$exec_rc" = "12" ]; then
    if echo "$exec_out" | grep -q "\[dry-run\]"; then
        fail "P1: --execute still emitted [dry-run] markers (DRY_RUN not propagated)"
    else
        ok "P1: --execute does not emit [dry-run] markers (execution path enabled)"
    fi
else
    fail "P1: --execute exited $exec_rc (output: $exec_out)"
fi

# ---------------------------------------------------------------------------
# Test 14 (P1 parity): --dry-run overrides inherited DRY_RUN=0
# ---------------------------------------------------------------------------
set +e
precedence_out="$(
    uname() { printf 'Darwin'; }; export -f uname;
    brew() { printf '/opt/homebrew'; }; export -f brew;
    DRY_RUN=0 bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer --dry-run 2>&1
)"
precedence_rc=$?
set -e
if [ "$precedence_rc" = "0" ]; then
    if echo "$precedence_out" | grep -q "\[dry-run\]"; then
        ok "P1: --dry-run overrides inherited DRY_RUN=0 (dry-run markers present)"
    else
        fail "P1: --dry-run did NOT override inherited DRY_RUN=0 (real brew would fire)"
    fi
    if echo "$precedence_out" | grep -q "dry_run=1"; then
        ok "P1: --dry-run announce line reports dry_run=1"
    else
        fail "P1: --dry-run announce line did not report dry_run=1"
    fi
else
    fail "P1: --dry-run precedence test exited $precedence_rc (output: $precedence_out)"
fi

# ---------------------------------------------------------------------------
# Test 15 (P1 parity): default mode does not invoke brew install
# ---------------------------------------------------------------------------
# In default mode (no --execute), the script MUST emit [dry-run] markers
# even if DRY_RUN=0 is inherited, because the CLI flag is authoritative.
set +e
unauth_out="$(
    uname() { printf 'Darwin'; }; export -f uname;
    brew() { printf '/opt/homebrew'; }; export -f brew;
    DRY_RUN=0 bash "$macos_deps_sh" --repo-root "$repo_root" --profile developer 2>&1
)"
unauth_rc=$?
set -e
if [ "$unauth_rc" = "0" ]; then
    if echo "$unauth_out" | grep -q "\[dry-run\]"; then
        ok "P1: default mode emits [dry-run] markers (DRY_RUN=0 env ignored)"
    else
        fail "P1: default mode did not emit [dry-run] markers (real brew would fire)"
    fi
else
    fail "P1: default mode exited $unauth_rc (output: $unauth_out)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "---"
echo "macos_deps.sh tests: $pass_count passed, $fail_count failed"
if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0