<#
.SYNOPSIS
    AEE Bootstrap v1 — W13 Windows E2E harness (spec §16 W13, §14.3).

.DESCRIPTION
    Container/VM E2E harness for Windows. This is a *harness shell* — it
    documents and validates the test plan for a Windows VM E2E run without
    claiming to actually spin up a Windows VM in this environment (the
    Abacus host is Linux; spec §13.4 assumes a Windows host or VM reachable
    via PowerShell).

    Spec §16 W13 deliverable: "Windows E2E (experimental)".
    Spec §14.3 testing strategy: container/VM tests.
    Spec §17.3 Phase C: W13 lands alongside W7; Windows marked experimental
    until the Windows adapter (§13.4) is implemented.

    What this harness DOES (on a Windows host with pwsh):
      1. Verifies the bootstrap v1 Phase C surface is present on disk:
         - install.ps1 (repo root)
         - bootstrap/lib/{detect,deps}.ps1
         - bootstrap/manifests/pwsh.deps.txt
         - aee/installer/redaction.py (W10 shared module)
      2. Runs the Python integration tests (Windows PowerShell contracts).
      3. Reports a summary line: "windows-e2e: N passed, M failed".

    What this harness DOES NOT do:
      * Spin up a Windows VM (no Hyper-V / Docker-in-Docker on Abacus).
      * Perform a real winget install (deps.ps1 -Execute requires UAC).
      * Perform a real git clone (network + auth not configured here).

    The harness is honest about this: it exits 0 only when the on-host
    Phase C surface + Python contract tests all pass. A real Windows VM
    E2E would extend this script with GitHub Actions windows-latest
    runner steps; that is a CI-runner responsibility, not a Phase C
    deliverable (spec §14.3).

    Run: pwsh tests/e2e/windows.ps1
    Exits 0 if all checks pass, non-zero on first failure.
#>

[CmdletBinding()]
param()

$scriptPath = $MyInvocation.MyCommand.Path
if (-not $scriptPath) { $scriptPath = $PSScriptRoot }
$repoRoot = (Resolve-Path (Join-Path $scriptPath '..\..')).Path

$passCount = 0
$failCount = 0

function Ok {
    param([string]$msg)
    Write-Host "ok - $msg"
    $script:passCount++
}

function Fail {
    param([string]$msg)
    Write-Host "not ok - $msg"
    $script:failCount++
}

# ---------------------------------------------------------------------
# 1. Phase C surface presence check
# ---------------------------------------------------------------------
Write-Host "# Windows E2E harness — Phase C surface presence"

$expected = @(
    'install.ps1',
    'bootstrap\lib\detect.ps1',
    'bootstrap\lib\deps.ps1',
    'bootstrap\manifests\pwsh.deps.txt',
    'aee\installer\redaction.py',
    'aee\tests\test_bootstrap_windows_ps1.py'
)

foreach ($f in $expected) {
    $full = Join-Path $repoRoot $f
    if (Test-Path $full) {
        Ok "present: $f"
    } else {
        Fail "missing: $f"
    }
}

# ---------------------------------------------------------------------
# 2. Python contract tests (Windows PowerShell integration)
# ---------------------------------------------------------------------
Write-Host "# Python contract tests"

$py = $null
foreach ($cmd in @('python', 'py', 'python3')) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) { $py = $found.Source; break }
}

if ($py) {
    $env:PYTHONPATH = $repoRoot
    $out = & $py -m unittest aee.tests.test_bootstrap_windows_ps1 2>&1
    $rc = $LASTEXITCODE
    if ($rc -eq 0) {
        Ok "test_bootstrap_windows_ps1.py passes"
    } else {
        Fail "test_bootstrap_windows_ps1.py failed (exit $rc)"
        Write-Host $out
    }
} else {
    Fail "python not found on PATH (cannot run Python contract tests)"
}

# ---------------------------------------------------------------------
# 3. Summary
# ---------------------------------------------------------------------
Write-Host ""
Write-Host "windows-e2e: $passCount passed, $failCount failed"

if ($failCount -gt 0) { exit 1 }
exit 0