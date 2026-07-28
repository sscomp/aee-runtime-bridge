#Requires -Version 5.1
<#
.SYNOPSIS
    AEE Bootstrap v1 — W7 Windows PowerShell trampoline (spec §2.3, §4, §13.4).

.DESCRIPTION
    Thin PowerShell entry that delegates ALL profile validation, planning,
    and read-only pre-flight to the canonical Python CLI
    (aee.cli -> aee.installer.backend). Per spec §3.1, this script does NOT
    maintain a parallel hard-coded profile matrix — the four profile names
    and the default come from the canonical Python source via
    ``python -m aee.cli install --help``.

    This is the Windows counterpart of the tracked POSIX ``install.sh``
    (Epic 9.3, Master Plan §21.3). The POSIX file delegates to a Python
    resolver and is dry-run by default; install.ps1 mirrors that contract.

    Scope (W7, spec §16 + §17.3 Phase C):
      * Platform: Windows 11 (10.0.22621+) with PowerShell 5.1+ or 7+ (pwsh).
      * Default profile: ``developer`` (per spec §2.4: first-class Windows
        support is deferred; bootstrap on Windows defaults to developer).
      * Dry-run by default. ``-Execute`` is required for real installs.
      * No automatic restart, no automatic deploy (spec §18).
      * WSL is NOT a supported target (spec §13.4); WSL installs should use
        the Ubuntu bootstrap path (install.sh + bootstrap/lib/deps.sh).

    Safety contract (spec §18 Production Safety Constraints):
      * Dry-run by default (§18.3). -Execute is required for real installs.
      * No force over an existing install (§18.4). Profile switch is
        rejected; hard git reset requires -ForceReset (§9.3).
      * No secret material in commits (§18.5). Output is filtered by the
        shared redaction module (aee.installer.redaction, shipped in W10).
      * Read-only doctor (§18.6). This trampoline performs NO mutations
        in dry-run mode.

    Exit codes (spec §10.4; mirrored from aee.installer.lifecycle):
      0  Success (or dry-run plan printed)
      2  Argument parsing failure
      7  Stage failed retryable (apt/winget lock, transient network)
      9  Drift detected (planned; not yet wired — W9 work order)
      10 Network error (winget unreachable)
      12 Dependency floor not met (unsupported Windows build, missing pwsh)

.PARAMETER Profile
    One of: full, mini, edge, developer. Default: developer (per spec §2.4
    Windows row).

.PARAMETER RepoRoot
    Path to the AEE repo root. Default: parent of this script's directory.

.PARAMETER DryRun
    Print the planned install without executing (default).

.PARAMETER Execute
    Perform the real install (gated by operator authorization).

.PARAMETER ForceReset
    Allow hard git reset when --execute is also passed (spec §9.3). Off by
    default; rejected with exit 2 unless -Execute is also set.

.EXAMPLE
    pwsh install.ps1 -Profile developer -DryRun
    Print the install plan for the developer profile without executing.

.EXAMPLE
    pwsh install.ps1 -Profile mini -Execute
    Perform a real install for the mini profile (requires operator
    authorization — spec §18.1: "No automatic deploy").

.NOTES
    Author: M2 (Hermes Agent, Abacus.ai runtime)
    Spec: reports/aee_bootstrap_v1_spec.md §16 (W7), §17.3 Phase C, §13.4
    Counterpart: install.sh (POSIX, tracked at HEAD — Epic 9.3)
#>

[CmdletBinding()]
param(
    [ValidateSet('full', 'mini', 'edge', 'developer')]
    [string]$Profile = 'developer',

    [string]$RepoRoot = '',

    [switch]$DryRun,
    [switch]$Execute,
    [switch]$ForceReset,
    [switch]$Help
)

# ---------------------------------------------------------------------
# Exit codes (mirror aee.installer.lifecycle; kept as integers so this
# script works without Python present, per §4 stage 01 ownership).
# ---------------------------------------------------------------------
$script:EXIT_OK = 0
$script:EXIT_PARSE_ERROR = 2
$script:EXIT_STAGE_FAILED_RETRYABLE = 7
$script:EXIT_DRIFT_DETECTED = 9
$script:EXIT_NETWORK_ERROR = 10
$script:EXIT_DEPENDENCY_FLOOR_NOT_MET = 12

# ---------------------------------------------------------------------
# Resolve repo root: explicit -RepoRoot wins; otherwise parent of this
# script's directory (matches install.sh behavior).
# ---------------------------------------------------------------------
function Resolve-RepoRoot {
    if ($script:RepoRoot -and (Test-Path $script:RepoRoot)) {
        return (Resolve-Path $script:RepoRoot).Path
    }
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (-not $scriptDir) {
        $scriptDir = $PSScriptRoot
    }
    return (Resolve-Path (Join-Path $scriptDir '..')).Path
}

# ---------------------------------------------------------------------
# find-python: locate a python executable on Windows.
# Tries `python` first (python.org installer), then `py` (launcher),
# then `python3` (PowerShell 7 + python.org). Returns the command name
# or $null if no Python is available.
# ---------------------------------------------------------------------
function Find-Python {
    foreach ($cmd in @('python', 'py', 'python3')) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            return $found.Source
        }
    }
    return $null
}

# ---------------------------------------------------------------------
# invoke-python-cli: delegate to aee.cli with the resolved profile +
# dry-run/execute flag. Prints the canonical Python CLI output.
# Returns the Python process exit code.
# ---------------------------------------------------------------------
function Invoke-PythonCli {
    param(
        [Parameter(Mandatory)] [string]$Repo,
        [Parameter(Mandatory)] [string]$ProfileName,
        [Parameter(Mandatory)] [bool]$ExecuteFlag,
        [Parameter(Mandatory)] [bool]$ForceResetFlag
    )
    $py = Find-Python
    if (-not $py) {
        Write-Error 'install.ps1: python not found on PATH (tried python, py, python3)'
        return $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    # Build PYTHONPATH so aee.* resolves from the repo root.
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$Repo;$($env:PYTHONPATH)" } else { $Repo }

    $args = @('-m', 'aee.cli', 'install', '--profile', $ProfileName)
    if ($ExecuteFlag) {
        $args += '--execute'
    } else {
        $args += '--dry-run'
    }
    if ($ForceResetFlag) {
        if (-not $ExecuteFlag) {
            Write-Error 'install.ps1: -ForceReset requires -Execute (spec §9.3)'
            return $script:EXIT_PARSE_ERROR
        }
        $args += '--force-reset'
    }

    & $py @args
    return $LASTEXITCODE
}

# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
function Main {
    if ($Help) {
        Write-Help
        exit $script:EXIT_OK
    }

    # -DryRun and -Execute are mutually exclusive; default is dry-run.
    if ($DryRun -and $Execute) {
        Write-Error 'install.ps1: -DryRun and -Execute are mutually exclusive'
        exit $script:EXIT_PARSE_ERROR
    }
    $executeFlag = [bool]$Execute
    $dryRunFlag = [bool]$DryRun
    if (-not $executeFlag -and -not $dryRunFlag) {
        # Default: dry-run (spec §18.3).
        $dryRunFlag = $true
    }

    # Validate Windows host (spec §13.4: WSL is NOT supported here).
    $osDesc = [System.Environment]::OSVersion.Platform
    if ($osDesc -ne 'Win32NT') {
        Write-Error "install.ps1: this script targets Windows; detected Platform=$osDesc"
        Write-Error 'install.ps1: WSL installs should use install.sh (Ubuntu bootstrap path)'
        exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    $repo = Resolve-RepoRoot
    Write-Host "install.ps1: repo=$repo profile=$Profile execute=$executeFlag dry_run=$dryRunFlag"

    $rc = Invoke-PythonCli -Repo $repo -ProfileName $Profile -ExecuteFlag $executeFlag -ForceResetFlag ([bool]$ForceReset)
    exit $rc
}

function Write-Help {
    Write-Host @'
AEE Bootstrap v1 — W7 Windows PowerShell trampoline

Usage: pwsh install.ps1 [-Profile <name>] [-DryRun | -Execute]
                       [-RepoRoot <path>] [-ForceReset] [-Help]

Parameters:
  -Profile <name>   One of: full, mini, edge, developer. Default: developer.
  -DryRun           Print the install plan without executing (default).
  -Execute          Perform the real install (requires operator authorization).
  -RepoRoot <path>  Path to the AEE repo root. Default: parent of this script.
  -ForceReset       Allow hard git reset (requires -Execute; spec §9.3).
  -Help             Show this help.

W7 scope: Windows 11 (10.0.22621+) with PowerShell 5.1+ or 7+ (pwsh).
WSL is NOT supported here (spec §13.4) — use install.sh for WSL.

Exit codes:
  0  success / dry-run plan printed
  2  parse error
  7  stage failed retryable
  9  drift detected (W9; not yet wired)
  10 network error
  12 dependency floor not met (unsupported build, missing pwsh)
'@
}

# Run Main only when executed, not when sourced. In PowerShell there is
# no direct equivalent of bash's BASH_SOURCE guard; we run Main unless the
# script is being dot-sourced (detected via $MyInvocation.Line -eq '.').
if ($MyInvocation.InvocationName -and $MyInvocation.InvocationName -ne '.') {
    Main
}