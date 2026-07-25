#!/usr/bin/env bash
# AEE Bootstrap v1 — W2 POSIX detect shim (spec §2.3, §4 stage 00_detect).
#
# Thin POSIX shell trampoline that delegates ALL platform detection to
# the canonical Python resolver `aee.platform.current.resolve_platform_identity`
# (spec §2.3: "the shell layer MUST NOT re-implement platform detection").
#
# W2 scope: Ubuntu/Debian ONLY. macOS and Windows detection are out of
# scope for W2 (spec §13.3 / §13.4). This shim is invoked by the W6
# POSIX trampoline (not yet shipped) and is safe to source independently.
#
# Contract:
#   * Prints exactly one line to stdout: the resolved PlatformIdentity
#     value (`linux`, `darwin`, or `unknown`) when Python is available.
#   * Falls back to a native heuristic ONLY when Python is missing
#     (stage 00 runs before deps are installed; Python may be absent).
#     The heuristic is intentionally conservative: it reports `linux`
#     only when `/etc/os-release` confirms a Linux host; otherwise it
#     reports `unknown` and the caller must refuse work.
#   * Exits 0 on success, non-zero on failure (missing repo root when
#     Python delegation is attempted).
#
# Usage:
#   bash bootstrap/lib/detect.sh [--repo-root <path>]
#   source bootstrap/lib/detect.sh; detect_platform <repo_root>
#
# Safety (W2 contract):
#   * No subprocess side effects (no apt, no git clone, no writes).
#   * Read-only: reads /etc/os-release and invokes `python3 -c`.
#   * No macOS / Windows branches (out of scope).

set -euo pipefail

# ---------------------------------------------------------------------------
# resolve_via_python <repo_root>
# ---------------------------------------------------------------------------
# Delegates to the canonical Python resolver. Prints the identity value
# to stdout. Returns non-zero if Python or the aee.platform module is
# unavailable (caller falls back to native heuristic).
# ---------------------------------------------------------------------------
resolve_via_python() {
    local repo_root="$1"
    if ! command -v python3 >/dev/null 2>&1; then
        return 1
    fi
    PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -c \
        'from aee.platform.current import resolve_platform_identity
print(resolve_platform_identity().value)' \
        2>/dev/null
}

# ---------------------------------------------------------------------------
# resolve_via_heuristic
# ---------------------------------------------------------------------------
# Conservative native fallback used when Python is not yet installed
# (stage 00 runs before stage 01_deps). Reports `linux` only when
# /etc/os-release confirms a Linux host; otherwise `unknown`.
# Prints the identity value to stdout; always exits 0.
# ---------------------------------------------------------------------------
resolve_via_heuristic() {
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release 2>/dev/null || true
        if [ -n "${ID:-}" ] || [ -n "${NAME:-}" ]; then
            printf 'linux\n'
            return 0
        fi
    fi
    # uname fallback (POSIX). Linux kernel → linux; Darwin → darwin
    # (reported for completeness; macOS is out of scope for W2 but the
    # value is honest and matches the Python resolver's mapping).
    local kernel
    kernel="$(uname -s 2>/dev/null || printf '')"
    case "$kernel" in
        Linux)  printf 'linux\n' ;;
        Darwin) printf 'darwin\n' ;;
        *)      printf 'unknown\n' ;;
    esac
}

# ---------------------------------------------------------------------------
# detect_platform <repo_root>
# ---------------------------------------------------------------------------
# Public entry point. Tries Python delegation first (canonical), falls
# back to the native heuristic. Prints one identity value to stdout.
# Exits 0 on success.
# ---------------------------------------------------------------------------
detect_platform() {
    local repo_root="${1:-.}"
    local identity
    if identity="$(resolve_via_python "$repo_root" 2>/dev/null)"; then
        printf '%s\n' "$identity"
        return 0
    fi
    resolve_via_heuristic
}

# ---------------------------------------------------------------------------
# detect_linux_distro
# ---------------------------------------------------------------------------
# Reads /etc/os-release and prints the distro ID (`ubuntu`, `debian`,
# or `unknown`). Used by deps.sh to pick the apt flow (deadsnakes PPA on
# Ubuntu 22.04, native python3.11 on Debian 12). Exits 0 always; prints
# `unknown` if /etc/os-release is absent or ID is not set.
# ---------------------------------------------------------------------------
detect_linux_distro() {
    if [ ! -f /etc/os-release ]; then
        printf 'unknown\n'
        return 0
    fi
    # shellcheck disable=SC1091
    . /etc/os-release 2>/dev/null || true
    case "${ID:-}" in
        ubuntu|debian) printf '%s\n' "${ID}" ;;
        *)             printf 'unknown\n' ;;
    esac
}

# ---------------------------------------------------------------------------
# detect_linux_version_id
# ---------------------------------------------------------------------------
# Prints the VERSION_ID from /etc/os-release (e.g. `22.04`, `12`), or
# `unknown` if absent. Used by deps.sh to decide the deadsnakes branch.
# ---------------------------------------------------------------------------
detect_linux_version_id() {
    if [ ! -f /etc/os-release ]; then
        printf 'unknown\n'
        return 0
    fi
    # shellcheck disable=SC1091
    . /etc/os-release 2>/dev/null || true
    if [ -n "${VERSION_ID:-}" ]; then
        printf '%s\n' "${VERSION_ID}"
    else
        printf 'unknown\n'
    fi
}

# ---------------------------------------------------------------------------
# CLI mode: when invoked as a script, print the identity for the repo
# root passed via --repo-root (default: parent of this file's dir).
# ---------------------------------------------------------------------------
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    repo_root="."
    while [ $# -gt 0 ]; do
        case "$1" in
            --repo-root)
                repo_root="$2"
                shift 2
                ;;
            --repo-root=*)
                repo_root="${1#--repo-root=}"
                shift
                ;;
            --help|-h)
                cat <<'EOF'
AEE Bootstrap v1 — W2 POSIX detect shim

Usage: bootstrap/lib/detect.sh [--repo-root <path>]

Prints the resolved PlatformIdentity value (linux | darwin | unknown)
to stdout. Delegates to aee.platform.current when Python is available;
falls back to a conservative /etc/os-release + uname heuristic.

W2 scope: Ubuntu/Debian only. macOS and Windows are out of scope.
EOF
                exit 0
                ;;
            *)
                printf 'detect.sh: unknown argument: %s\n' "$1" >&2
                exit 2
                ;;
        esac
    done
    # Default repo root: parent of bootstrap/
    if [ "$repo_root" = "." ]; then
        script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
        repo_root="$(cd -P "$script_dir/../.." >/dev/null 2>&1 && pwd)"
    fi
    detect_platform "$repo_root"
fi