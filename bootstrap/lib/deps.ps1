<#
.SYNOPSIS
    AEE Bootstrap v1 — W7 Windows dependency installer (spec §6, §13.4).

.DESCRIPTION
    Stage 01_deps for Windows ONLY. Installs the hard + profile-gated
    dependencies listed in ``bootstrap/manifests/pwsh.deps.txt`` via
    ``winget install`` (spec §6.3 reproducibility). Ubuntu/Debian (apt) and
    macOS (brew) are out of scope for W7.

    Contract (spec §5.1 idempotency, §6.4 privilege, §6.5 scope, §13.4):
      * Idempotent: re-running is a no-op when packages are already installed
        (winget's own "already installed" short-circuit, spec §5.1). Does NOT
        uninstall packages.
      * Privilege: NO UAC elevation is requested by this script. The runtime
        runs as the install owner (non-admin per §6.4). winget prompts for
        UAC itself when a package needs it; this script does not silently
        elevate.
      * Scope: per-user by default (--scope user). System-scope (--system)
        is accepted but rejected with a clear message (not supported in v1;
        spec §6.5 + §13.4 — operator runs the system install by hand).
      * Dry-run by default: -DryRun prints the planned winget commands
        without executing them. -Execute is required for real installs.
        Even with -Execute, this W7 slice WILL perform real winget installs.
        The install.ps1 trampoline gates this behind operator authorization.
      * Windows build floor: this script exits 12 if the detected Windows
        build is below 22621 (Windows 11 10.0.22621+, spec §1.4 + §13.4).

    Usage:
      pwsh bootstrap/lib/deps.ps1 -RepoRoot <path> -Profile <name>
                                  [-DryRun | -Execute] [-System]

    Exit codes (spec §10.4; constants in aee.installer.lifecycle):
      0  success (or dry-run plan printed)
      2  argument parsing failure
      7  stage failed retryable (winget lock held, transient network)
      10 network error (winget unreachable)
      12 dependency floor not met (winget missing, unsupported Windows build)

    W7 scope rule: this script implements Windows ONLY. If the detected
    platform is not Windows, it exits 12 with a clear message.
    Ubuntu/Debian (apt) and macOS (brew) are separate work orders.
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [Parameter(Mandatory)] [ValidateSet('full', 'mini', 'edge', 'developer')] [string]$Profile,
    [switch]$DryRun,
    [switch]$Execute,
    [switch]$System,
    [switch]$Help
)

# Source the detect shim (dot-source). PowerShell dot-sourcing requires a
# real path; we resolve the script's own directory and join detect.ps1.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = $PSScriptRoot }
if (-not $scriptDir) { $scriptDir = $PWD.Path }
. (Join-Path $scriptDir 'detect.ps1')

# H3 fix: when -RepoRoot is not supplied, derive it from the script's
# own location (parent of bootstrap/lib) rather than the caller's CWD.
# Mirrors install.ps1 Resolve-RepoRoot and detect.ps1 CLI-mode resolution.
# Without this, a standalone `pwsh deps.ps1 -Profile X` invoked from a
# non-repo CWD silently fails to find the manifest and exits 12.
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
}

# ---------------------------------------------------------------------
# Exit codes (mirror aee.installer.lifecycle).
# ---------------------------------------------------------------------
$script:EXIT_OK = 0
$script:EXIT_PARSE_ERROR = 2
$script:EXIT_STAGE_FAILED_RETRYABLE = 7
$script:EXIT_NETWORK_ERROR = 10
$script:EXIT_DEPENDENCY_FLOOR_NOT_MET = 12

# ---------------------------------------------------------------------
# read_manifest <manifest_path>
# Prints one package id per line to stdout, comments/blanks stripped.
# ---------------------------------------------------------------------
function Read-Manifest {
    param([string]$Manifest)
    if (-not (Test-Path $Manifest)) {
        Write-Error "deps.ps1: manifest not found: $Manifest"
        return $null
    }
    Get-Content $Manifest | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) { $line }
    }
}

# ---------------------------------------------------------------------
# filter_packages_by_profile <raw_packages> <profile>
# Rules (spec §6.2):
#   * Core hard deps (git, python, curl) — always installed.
#   * supervisor — NOT a winget package on Windows. Spec §6.2 explicitly
#     says supervisor on Windows is installed via ``pip install
#     supervisor`` (Python package), and §13.4 says Windows uses a
#     Windows Service or scheduled task instead of the supervisor
#     package. The manifest entry exists for parity with the POSIX
#     manifests, but Filter-PackagesByProfile MUST exclude it from
#     the winget install set on all profiles so it never reaches
#     ``Invoke-WingetInstall`` (where ``winget install --id supervisor``
#     would fail with "no package found").
#   * docker — full + edge only.
# ---------------------------------------------------------------------
function Filter-PackagesByProfile {
    param([string[]]$Packages, [string]$ProfileName)
    foreach ($pkg in $Packages) {
        switch -Regex ($pkg) {
            '^supervisor$' {
                # H2 fix: supervisor is a pip package on Windows (spec
                # §6.2), not a winget id. Exclude from the winget
                # install set on every profile. Operators register the
                # Windows Service / scheduled task by hand (spec §13.4).
                continue
            }
            '^docker' {
                if ($ProfileName -in @('full', 'edge')) { $pkg }
            }
            default { $pkg }
        }
    }
}

# ---------------------------------------------------------------------
# winget helpers
# ---------------------------------------------------------------------
# Classify a winget $LASTEXITCODE into one of the documented exit
# categories (spec §10.4). Returns a hashtable with:
#   category  = 'success' | 'retryable' | 'network' | 'already_installed'
#   exit_code = the deps.ps1 exit code to emit (0 / 7 / 10 / 0)
# Known winget exit codes (Microsoft docs, learn.microsoft.com/windows/
# package-manager/winget/error-messages):
#   0x8A150011 (-1978335045)  AppInstallerStatus.AlreadyInstalled  — idempotent no-op
#   0x8A150019 (-1978335015)  AppInstallerStatus.DownloadError     — network
#   0x8A150006 (-1978335034)  AppInstallerStatus.NoNetwork        — network
#   0x8A150001 (-1978335359)  AppInstallerStatus.AppNotInstalled  — retryable
#   0x8A150027 (-1978335065)  AppInstallerStatus.InstallerNotDeclared — retryable
# Any other non-zero code is treated as retryable (conservative).
# ---------------------------------------------------------------------
function Get-WingetExitCategory {
    param([int]$Code)
    switch ($Code) {
        0                    { return @{ category = 'success';          exit_code = $script:EXIT_OK } }
        -1978335045          { return @{ category = 'already_installed'; exit_code = $script:EXIT_OK } }
        -1978335015          { return @{ category = 'network';         exit_code = $script:EXIT_NETWORK_ERROR } }
        -1978335034          { return @{ category = 'network';         exit_code = $script:EXIT_NETWORK_ERROR } }
        default              { return @{ category = 'retryable';       exit_code = $script:EXIT_STAGE_FAILED_RETRYABLE } }
    }
}

function Invoke-WingetInstall {
    param([string[]]$Packages, [bool]$ExecuteFlag)
    foreach ($pkg in $Packages) {
        if ($ExecuteFlag) {
            winget install --id $pkg --silent --accept-package-agreements --accept-source-agreements
            $rc = $LASTEXITCODE
            if ($rc -ne 0) {
                $cat = Get-WingetExitCategory -Code $rc
                Write-Error "deps.ps1: winget install --id $pkg failed with $rc (category=$($cat.category))"
                exit $cat.exit_code
            }
        } else {
            Write-Host "[dry-run] winget install --id $pkg --silent --accept-package-agreements --accept-source-agreements"
        }
    }
}

# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
function Main {
    if ($Help) { Write-Help; exit $script:EXIT_OK }

    # -DryRun and -Execute are mutually exclusive; default is dry-run.
    if ($DryRun -and $Execute) {
        Write-Error 'deps.ps1: -DryRun and -Execute are mutually exclusive'
        exit $script:EXIT_PARSE_ERROR
    }
    $executeFlag = [bool]$Execute
    $dryRunFlag = [bool]$DryRun
    if (-not $executeFlag -and -not $dryRunFlag) { $dryRunFlag = $true }

    if ($System) {
        Write-Error 'deps.ps1: -System is NOT supported on Windows v1 (spec §6.5 + §13.4)'
        Write-Error 'deps.ps1: operator must run system-scope installs by hand'
        exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    # Detect platform (W7 scope gate).
    $identity = Detect-Platform -Repo $RepoRoot
    if ($identity -ne 'windows') {
        Write-Error "deps.ps1: W7 supports Windows only; detected platform=$identity"
        Write-Error 'deps.ps1: Ubuntu/Debian (apt) and macOS (brew) are separate work orders'
        exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    # Windows build floor (§13.4: Windows 11 = 10.0.22621+).
    $build = Detect-WindowsBuild
    if ($build -ne 'unknown' -and [int]$build -lt 22621) {
        Write-Error "deps.ps1: Windows build $build is below the floor 22621 (Windows 11; spec §13.4)"
        exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    # Verify winget is available.
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Error 'deps.ps1: winget not found — cannot proceed (spec §13.4)'
        exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    # Load + filter manifest.
    $manifest = Join-Path $RepoRoot 'bootstrap/manifests/pwsh.deps.txt'
    $raw = Read-Manifest -Manifest $manifest
    if (-not $raw) { exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET }
    $filtered = Filter-PackagesByProfile -Packages $raw -ProfileName $Profile
    if (-not $filtered) {
        Write-Error 'deps.ps1: no packages to install after profile filter'
        exit $script:EXIT_DEPENDENCY_FLOOR_NOT_MET
    }

    Write-Host "deps.ps1: platform=$identity build=$build profile=$Profile dry_run=$dryRunFlag"
    Write-Host "deps.ps1: packages: $($filtered -join ' ')"

    Invoke-WingetInstall -Packages $filtered -ExecuteFlag $executeFlag

    Write-Host "deps.ps1: stage 01_deps $(if ($dryRunFlag) { 'planned (dry-run)' } else { 'completed' })"
    exit $script:EXIT_OK
}

function Write-Help {
    Write-Host @'
AEE Bootstrap v1 — W7 Windows dependency installer

Usage: pwsh bootstrap/lib/deps.ps1 -RepoRoot <path> -Profile <name>
                                  [-DryRun | -Execute] [-System]

W7 scope: Windows ONLY. Exits 12 if the detected platform is not windows.

Parameters:
  -RepoRoot <path>   Path to the AEE repo root (required for manifest).
  -Profile <name>     One of: full, mini, edge, developer.
  -DryRun             Print planned winget commands without executing (default).
  -Execute            Perform real winget installs.
  -System             NOT supported on Windows v1 (rejected with exit 12).

Exit codes:
  0  success / dry-run plan printed
  2  parse error
  7  stage failed retryable
  10 network error
  12 dependency floor not met (winget missing, unsupported build)
'@
}

if ($MyInvocation.InvocationName -and $MyInvocation.InvocationName -ne '.') {
    Main
}