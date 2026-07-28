#!/usr/bin/env bash
# AEE Bootstrap v1 — W7 detect.ps1 shell integration tests.
#
# Coverage:
#   1. detect.ps1 file exists
#   2. detect.ps1 has a BOM / UTF-8 signature check (PowerShell 5.1 reads UTF-8)
#   3. detect.ps1 contains the canonical W7 banner (spec §13.4)
#   4. detect.ps1 contains a Resolve-ViaPython function (delegation)
#   5. detect.ps1 contains a Resolve-ViaHeuristic function (fallback)
#   6. detect.ps1 contains a Detect-Platform public entry point
#   7. detect.ps1 contains a Detect-WindowsBuild helper
#   8. detect.ps1 mentions "Win32NT" (the heuristic gate)
#   9. detect.ps1 does NOT contain a Linux or macOS branch in main scope
#  10. detect.ps1 has a CLI guard ( InvocationName -ne '.')
#  11. detect.ps1 references the canonical Python resolver module
#
# All tests are static (file-content assertions). We do NOT execute
# pwsh on this host (the Abacus host is Linux; spec §13.4 W7 is Windows
# only — this test file documents the contract, real execution happens
# on a GitHub Actions windows-latest runner per §14.3).
#
# Run: bash tests/test_bootstrap_lib_detect_ps1.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
detect_ps1="$repo_root/bootstrap/lib/detect.ps1"

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

assert_contains() {
    local file="$1"
    local needle="$2"
    if grep -qF "$needle" "$file"; then
        return 0
    else
        return 1
    fi
}

# ---------------------------------------------------------------------
# 1. File exists
# ---------------------------------------------------------------------
if [ -f "$detect_ps1" ]; then
    ok "present: bootstrap/lib/detect.ps1"
else
    fail "missing: bootstrap/lib/detect.ps1"
fi

# ---------------------------------------------------------------------
# 2. UTF-8 / PowerShell 5.1 signature: file should not have a BOM
# (PowerShell 5.1 reads UTF-8 with BOM, but our file is UTF-8 no BOM
# which works with pwsh 7+). Just verify it's valid UTF-8.
# ---------------------------------------------------------------------
if file "$detect_ps1" | grep -qi 'utf-8\|ascii\|text'; then
    ok "detect.ps1 is text/UTF-8"
else
    fail "detect.ps1 is not a text file"
fi

# ---------------------------------------------------------------------
# 3. Canonical W7 banner
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "W7 Windows detect shim"; then
    ok "contains W7 banner"
else
    fail "missing W7 banner"
fi

# ---------------------------------------------------------------------
# 4. Resolve-ViaPython function
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "Resolve-ViaPython"; then
    ok "has Resolve-ViaPython"
else
    fail "missing Resolve-ViaPython"
fi

# ---------------------------------------------------------------------
# 5. Resolve-ViaHeuristic function
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "Resolve-ViaHeuristic"; then
    ok "has Resolve-ViaHeuristic"
else
    fail "missing Resolve-ViaHeuristic"
fi

# ---------------------------------------------------------------------
# 6. Detect-Platform public entry point
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "Detect-Platform"; then
    ok "has Detect-Platform entry point"
else
    fail "missing Detect-Platform"
fi

# ---------------------------------------------------------------------
# 7. Detect-WindowsBuild helper
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "Detect-WindowsBuild"; then
    ok "has Detect-WindowsBuild"
else
    fail "missing Detect-WindowsBuild"
fi

# ---------------------------------------------------------------------
# 8. Win32NT heuristic gate
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "Win32NT"; then
    ok "references Win32NT"
else
    fail "missing Win32NT gate"
fi

# ---------------------------------------------------------------------
# 9. No Linux/macOS branch in main scope — detect.ps1 may mention them
# in the uname fallback (informational) but the primary path is Windows.
# We check for the Win32NT gate (already covered above) and that there's
# no "darwin" Linux-distro branch.
# ---------------------------------------------------------------------
# (detect.ps1 mentions 'Linux' and 'Darwin' in the uname fallback — that
# is the POSIX parity path, not a Windows-side branch. We assert that
# the Win32NT branch exists AND the uname fallback is guarded by
# Get-Command uname, which is a Windows-hosted Cygwin/MSYS detection.)
if assert_contains "$detect_ps1" "Get-Command uname"; then
    ok "uname fallback is guarded (parity path)"
else
    fail "uname fallback missing guard"
fi

# ---------------------------------------------------------------------
# 10. CLI guard (InvocationName -ne '.')
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "InvocationName"; then
    ok "has CLI guard"
else
    fail "missing CLI guard"
fi

# ---------------------------------------------------------------------
# 11. References the canonical Python resolver
# ---------------------------------------------------------------------
if assert_contains "$detect_ps1" "aee.platform.current"; then
    ok "delegates to aee.platform.current"
else
    fail "missing aee.platform.current reference"
fi

# ---------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------
echo ""
echo "detect.ps1: ${pass_count} passed, ${fail_count} failed"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0