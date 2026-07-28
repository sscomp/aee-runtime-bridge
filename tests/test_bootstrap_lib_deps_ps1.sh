#!/usr/bin/env bash
# AEE Bootstrap v1 — W7 deps.ps1 shell integration tests.
#
# Coverage:
#   1. deps.ps1 file exists
#   2. deps.ps1 contains the canonical W7 banner (spec §13.4)
#   3. deps.ps1 dot-sources detect.ps1
#   4. deps.ps1 has Read-Manifest function
#   5. deps.ps1 has Filter-PackagesByProfile function
#   6. deps.ps1 has Invoke-WingetInstall function
#   7. deps.ps1 has a -Profile parameter with ValidateSet
#   8. deps.ps1 rejects -System (not supported on Windows v1)
#   9. deps.ps1 has Windows build floor check (22621)
#  10. deps.ps1 has winget availability check
#  11. deps.ps1 references pwsh.deps.txt manifest
#  12. deps.ps1 has a dry-run default (no -Execute → dry-run)
#  13. deps.ps1 has the canonical exit codes (0/2/7/10/12)
#  14. deps.ps1 CLI guard (InvocationName -ne '.')
#  15. pwsh.deps.txt manifest exists and lists core deps
#
# All tests are static (file-content assertions). We do NOT execute
# pwsh on this host.
#
# Run: bash tests/test_bootstrap_lib_deps_ps1.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
deps_ps1="$repo_root/bootstrap/lib/deps.ps1"
manifest="$repo_root/bootstrap/manifests/pwsh.deps.txt"

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
    if grep -qF "$2" "$1"; then return 0; else return 1; fi
}

# 1. File exists
if [ -f "$deps_ps1" ]; then
    ok "present: bootstrap/lib/deps.ps1"
else
    fail "missing: bootstrap/lib/deps.ps1"
fi

# 2. Canonical W7 banner
if assert_contains "$deps_ps1" "W7 Windows dependency installer"; then
    ok "contains W7 banner"
else
    fail "missing W7 banner"
fi

# 3. Dot-sources detect.ps1
if assert_contains "$deps_ps1" "detect.ps1"; then
    ok "references detect.ps1"
else
    fail "missing detect.ps1 dot-source"
fi

# 4. Read-Manifest function
if assert_contains "$deps_ps1" "Read-Manifest"; then
    ok "has Read-Manifest"
else
    fail "missing Read-Manifest"
fi

# 5. Filter-PackagesByProfile function
if assert_contains "$deps_ps1" "Filter-PackagesByProfile"; then
    ok "has Filter-PackagesByProfile"
else
    fail "missing Filter-PackagesByProfile"
fi

# 6. Invoke-WingetInstall function
if assert_contains "$deps_ps1" "Invoke-WingetInstall"; then
    ok "has Invoke-WingetInstall"
else
    fail "missing Invoke-WingetInstall"
fi

# 7. -Profile parameter with ValidateSet
if assert_contains "$deps_ps1" "ValidateSet('full', 'mini', 'edge', 'developer')"; then
    ok "has -Profile ValidateSet"
else
    fail "missing -Profile ValidateSet"
fi

# 8. Rejects -System
if assert_contains "$deps_ps1" "NOT supported on Windows v1"; then
    ok "rejects -System"
else
    fail "missing -System rejection"
fi

# 9. Windows build floor check
if assert_contains "$deps_ps1" "22621"; then
    ok "has build floor 22621"
else
    fail "missing build floor"
fi

# 10. winget availability check
if assert_contains "$deps_ps1" "Get-Command winget"; then
    ok "has winget check"
else
    fail "missing winget check"
fi

# 11. References pwsh.deps.txt manifest
if assert_contains "$deps_ps1" "pwsh.deps.txt"; then
    ok "references pwsh.deps.txt"
else
    fail "missing pwsh.deps.txt reference"
fi

# 12. Dry-run default
if assert_contains "$deps_ps1" "dry-run plan printed" && assert_contains "$deps_ps1" "default"; then
    ok "has dry-run default"
else
    fail "missing dry-run default"
fi

# 13. Exit codes
for code in "EXIT_OK" "EXIT_PARSE_ERROR" "EXIT_STAGE_FAILED_RETRYABLE" "EXIT_NETWORK_ERROR" "EXIT_DEPENDENCY_FLOOR_NOT_MET"; do
    if assert_contains "$deps_ps1" "$code"; then
        ok "has $code"
    else
        fail "missing $code"
    fi
done

# 14. CLI guard
if assert_contains "$deps_ps1" "InvocationName"; then
    ok "has CLI guard"
else
    fail "missing CLI guard"
fi

# 15. Manifest exists and lists core deps
if [ -f "$manifest" ]; then
    ok "present: bootstrap/manifests/pwsh.deps.txt"
    if assert_contains "$manifest" "Git.Git"; then
        ok "manifest lists Git.Git"
    else
        fail "manifest missing Git.Git"
    fi
    if assert_contains "$manifest" "Python.Python.3.11"; then
        ok "manifest lists Python.Python.3.11"
    else
        fail "manifest missing Python.Python.3.11"
    fi
else
    fail "missing: bootstrap/manifests/pwsh.deps.txt"
fi

# 16. H1: Invoke-WingetInstall inspects $LASTEXITCODE
if assert_contains "$deps_ps1" '$LASTEXITCODE'; then
    ok "H1: Invoke-WingetInstall inspects \$LASTEXITCODE"
else
    fail "H1: missing \$LASTEXITCODE inspection in Invoke-WingetInstall"
fi

# 17. H1: winget exit-code classifier present
if assert_contains "$deps_ps1" "Get-WingetExitCategory"; then
    ok "H1: has Get-WingetExitCategory classifier"
else
    fail "H1: missing Get-WingetExitCategory classifier"
fi

# 18. H1: winget AlreadyInstalled code (-1978335045) handled
if grep -qF -- "-1978335045" "$deps_ps1"; then
    ok "H1: handles winget AlreadyInstalled (-1978335045)"
else
    fail "H1: missing winget AlreadyInstalled code"
fi

# 19. H1: winget network error codes mapped to exit 10
if grep -qF -- "-1978335015" "$deps_ps1" && grep -qF -- "-1978335034" "$deps_ps1"; then
    ok "H1: maps winget network codes (-1978335015 / -1978335034) to exit 10"
else
    fail "H1: missing winget network error code mappings"
fi

# 20. H2: supervisor filtered out (continue) on every profile
# Check that the supervisor branch contains `continue` and does NOT
# emit $pkg (the old behavior). We extract the switch block for the
# supervisor case and verify `continue` is present.
supervisor_block=$(sed -n "/'\^supervisor\$'/,/^            }/p" "$deps_ps1")
if echo "$supervisor_block" | grep -q "continue"; then
    ok "H2: supervisor uses 'continue' (filtered out on all profiles)"
else
    fail "H2: supervisor not filtered out with continue"
fi

# 21. H2: supervisor NOT gated to mini+full anymore
if grep -qE "'\^supervisor\$'\s*\)\s*\{\s*if\s+\(\$ProfileName\s+-in\s+@\('mini',\s*'full'\)" "$deps_ps1"; then
    fail "H2: supervisor still gated to mini+full (would fail on winget)"
else
    ok "H2: supervisor no longer gated to mini+full"
fi

# 22. H2: manifest documents supervisor is pip package on Windows
if assert_contains "$manifest" "pip install supervisor" && assert_contains "$manifest" "not a winget"; then
    ok "H2: manifest documents supervisor as pip package, not winget"
else
    fail "H2: manifest missing supervisor-is-pip documentation"
fi

# 23. H3: RepoRoot auto-resolved from script dir when not supplied
if assert_contains "$deps_ps1" "if (-not \$RepoRoot)" && assert_contains "$deps_ps1" "Join-Path \$scriptDir '..'"; then
    ok "H3: RepoRoot auto-resolved from \$scriptDir parent"
else
    fail "H3: RepoRoot not derived from script location"
fi

echo ""
echo "deps.ps1: ${pass_count} passed, ${fail_count} failed"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0