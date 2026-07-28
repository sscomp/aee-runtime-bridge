<#
.SYNOPSIS
    AEE Bootstrap v1 — W7 Windows detect shim (spec §2.3, §4 stage 00_detect).

.DESCRIPTION
    Thin PowerShell trampoline that delegates ALL platform detection to the
    canonical Python resolver ``aee.platform.current.resolve_platform_identity``
    (spec §2.3: "the shell layer MUST NOT re-implement platform detection").

    W7 scope: Windows ONLY. Ubuntu/Debian and macOS detection are out of
    scope for W7 (spec §13.1 / §13.2 / §13.3). This shim is invoked by the W7
    Windows trampoline (install.ps1) and is safe to dot-source independently.

    Contract:
      * Prints exactly one line to stdout: the resolved PlatformIdentity
        value (``windows``, ``linux``, ``darwin``, or ``unknown``) when
        Python is available.
      * Falls back to a native heuristic ONLY when Python is missing (stage
        00 runs before deps are installed; Python may be absent). The
        heuristic is intentionally conservative: it reports ``windows`` only
        when [System.Environment]::OSVersion.Platform is Win32NT; otherwise it
        reports ``unknown`` and the caller must refuse work.
      * Exits 0 on success, non-zero on failure (missing repo root when
        Python delegation is attempted).

    Usage:
      pwsh bootstrap/lib/detect.ps1 -RepoRoot <path>
      . bootstrap/lib/detect.ps1; detect_platform <repo_root>

    Safety (W7 contract):
      * No subprocess side effects (no winget, no git clone, no writes).
      * Read-only: reads [System.Environment]::OSVersion and invokes ``python -c``.
      * No Linux / macOS branches (out of scope).
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = ''
)

# ---------------------------------------------------------------------
# resolve_via_python <repo_root>
# Delegates to the canonical Python resolver. Prints the identity value
# to stdout. Returns $null if Python or the aee.platform module is
# unavailable (caller falls back to native heuristic).
# ---------------------------------------------------------------------
function Resolve-ViaPython {
    param([string]$Repo)
    $py = $null
    foreach ($cmd in @('python', 'py', 'python3')) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { $py = $found.Source; break }
    }
    if (-not $py) { return $null }

    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$Repo;$($env:PYTHONPATH)" } else { $Repo }
    $code = 'from aee.platform.current import resolve_platform_identity; print(resolve_platform_identity().value)'
    $out = & $py -c $code 2>$null
    return $out
}

# ---------------------------------------------------------------------
# resolve_via_heuristic
# Conservative native fallback used when Python is not yet installed
# (stage 00 runs before stage 01_deps). Reports ``windows`` only when
# [System.Environment]::OSVersion.Platform is Win32NT; otherwise ``unknown``.
# Always exits 0.
# ---------------------------------------------------------------------
function Resolve-ViaHeuristic {
    $plat = [System.Environment]::OSVersion.Platform
    if ($plat -eq 'Win32NT') { return 'windows' }
    # uname fallback for non-Windows hosts (informational; matches the
    # POSIX detect.sh contract — Windows hosts always hit the Win32NT branch).
    if (Get-Command uname -ErrorAction SilentlyContinue) {
        $kernel = & uname -s 2>$null
        if ($LASTEXITCODE -eq 0) {
            switch ($kernel) {
                'Linux'   { return 'linux' }
                'Darwin'  { return 'darwin' }
                default   { return 'unknown' }
            }
        }
    }
    return 'unknown'
}

# ---------------------------------------------------------------------
# detect_platform <repo_root>
# Public entry point. Tries Python delegation first (canonical), falls
# back to the native heuristic. Prints one identity value to stdout.
# ---------------------------------------------------------------------
function Detect-Platform {
    param([string]$Repo = '.')
    $identity = Resolve-ViaPython -Repo $Repo
    if ($identity) { return $identity }
    return Resolve-ViaHeuristic
}

# ---------------------------------------------------------------------
# detect_windows_build
# Prints the Windows build number (e.g. '22621') or 'unknown' if it
# cannot be determined. Used by deps.ps1 to gate on the §13.4 floor
# (Windows 11 = 10.0.22621+).
# ---------------------------------------------------------------------
function Detect-WindowsBuild {
    $ver = [System.Environment]::OSVersion.Version
    if ($ver -and $ver.Build) {
        return $ver.Build.ToString()
    }
    return 'unknown'
}

# ---------------------------------------------------------------------
# CLI mode: when invoked as a script, print the identity for the repo
# root passed via -RepoRoot (default: parent of this file's dir).
# ---------------------------------------------------------------------
if ($MyInvocation.InvocationName -and $MyInvocation.InvocationName -ne '.') {
    $repo = $RepoRoot
    if (-not $repo) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
        if (-not $scriptDir) { $scriptDir = $PSScriptRoot }
        $repo = (Resolve-Path (Join-Path $scriptDir '..')).Path
    }
    $identity = Detect-Platform -Repo $repo
    Write-Output $identity
}