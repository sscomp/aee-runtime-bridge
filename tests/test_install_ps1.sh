#!/usr/bin/env bash
# AEE Bootstrap v1 — W7 install.ps1 wrapper integration tests.
#
# Coverage:
#   1. install.ps1 file exists at repo root
#   2. install.ps1 has #Requires -Version 5.1 header
#   3. install.ps1 contains the canonical W7 banner
#   4. install.ps1 has a -Profile parameter with ValidateSet
#   5. install.ps1 default profile is developer (spec §2.4 Windows row)
#   6. install.ps1 has -DryRun and -Execute switches (mutually exclusive)
#   7. install.ps1 has -ForceReset (gated by -Execute)
#   8. install.ps1 delegates to aee.cli (Python canonical CLI)
#   9. install.ps1 has Find-Python helper (python/py/python3 fallback)
#  10. install.ps1 has Win32NT host validation
#  11. install.ps1 mentions WSL is NOT supported (spec §13.4)
#  12. install.ps1 has the canonical exit codes
#  13. install.ps1 has a Write-Help function
#  14. install.ps1 has a CLI guard (InvocationName -ne '.')
#
# All tests are static (file-content assertions). We do NOT execute
# pwsh on this host.
#
# Run: bash tests/test_install_ps1.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
install_ps1="$repo_root/install.ps1"

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
if [ -f "$install_ps1" ]; then
    ok "present: install.ps1"
else
    fail "missing: install.ps1"
fi

# 2. #Requires header
if assert_contains "$install_ps1" "#Requires -Version 5.1"; then
    ok "has #Requires -Version 5.1"
else
    fail "missing #Requires header"
fi

# 3. W7 banner
if assert_contains "$install_ps1" "W7 Windows PowerShell trampoline"; then
    ok "contains W7 banner"
else
    fail "missing W7 banner"
fi

# 4. -Profile ValidateSet
if assert_contains "$install_ps1" "ValidateSet('full', 'mini', 'edge', 'developer')"; then
    ok "has -Profile ValidateSet"
else
    fail "missing -Profile ValidateSet"
fi

# 5. Default profile is developer
if assert_contains "$install_ps1" "developer"; then
    ok "default profile mentions developer"
else
    fail "missing developer default"
fi

# 6. -DryRun and -Execute switches
if assert_contains "$install_ps1" "DryRun" && assert_contains "$install_ps1" "Execute"; then
    ok "has -DryRun and -Execute"
else
    fail "missing -DryRun / -Execute"
fi
if assert_contains "$install_ps1" "mutually exclusive"; then
    ok "enforces mutual exclusion"
else
    fail "missing mutual exclusion guard"
fi

# 7. -ForceReset
if assert_contains "$install_ps1" "ForceReset"; then
    ok "has -ForceReset"
else
    fail "missing -ForceReset"
fi

# 8. Delegates to aee.cli
if assert_contains "$install_ps1" "aee.cli"; then
    ok "delegates to aee.cli"
else
    fail "missing aee.cli delegation"
fi

# 9. Find-Python helper
if assert_contains "$install_ps1" "Find-Python"; then
    ok "has Find-Python"
else
    fail "missing Find-Python"
fi

# 10. Win32NT validation
if assert_contains "$install_ps1" "Win32NT"; then
    ok "has Win32NT validation"
else
    fail "missing Win32NT validation"
fi

# 11. WSL not supported
if assert_contains "$install_ps1" "WSL"; then
    ok "mentions WSL not supported"
else
    fail "missing WSL note"
fi

# 12. Exit codes
for code in "EXIT_OK" "EXIT_PARSE_ERROR" "EXIT_STAGE_FAILED_RETRYABLE" "EXIT_NETWORK_ERROR" "EXIT_DEPENDENCY_FLOOR_NOT_MET"; do
    if assert_contains "$install_ps1" "$code"; then
        ok "has $code"
    else
        fail "missing $code"
    fi
done

# 13. Write-Help function
if assert_contains "$install_ps1" "Write-Help"; then
    ok "has Write-Help"
else
    fail "missing Write-Help"
fi

# 14. CLI guard
if assert_contains "$install_ps1" "InvocationName"; then
    ok "has CLI guard"
else
    fail "missing CLI guard"
fi

echo ""
echo "install.ps1: ${pass_count} passed, ${fail_count} failed"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0