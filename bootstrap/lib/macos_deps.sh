#!/usr/bin/env bash
# AEE Bootstrap v1 — W3 macOS Homebrew dependency installer (spec §6, §6.3, §13.3).
#
# Stage 01_deps for macOS ONLY. Installs the hard + profile-gated
# Homebrew formulae listed in `bootstrap/manifests/brew.deps.txt` via
# `brew install --quiet` (spec §6.3 reproducibility). Ubuntu/Debian
# (apt) and Windows (winget) are out of scope for W3.
#
# Contract (spec §5.1 idempotency, §6.4 privilege, §6.5 scope, §13.3):
#   * Idempotent: re-running is a no-op when formulae are already
#     installed (brew's own "already installed" short-circuit, spec
#     §5.1). Does NOT uninstall formulae.
#   * Privilege: NO sudo is used on macOS (spec §6.4 + §13.3 — install
#     into the user's homebrew prefix and ~/Library/LaunchAgents).
#     This matches the existing constraint that the runtime runs as
#     the install owner (non-root).
#   * Scope: per-user by default. System-scope is NOT supported on
#     macOS v1 (spec §6.5 + §13.3 — operator runs launchd by hand).
#     `--system` is accepted but rejected with a clear message.
#   * Dry-run by default: `--dry-run` prints the planned brew commands
#     without executing them. `--execute` is required for real installs.
#     Even with `--execute`, this W3 slice WILL perform real brew
#     installs (unlike the W1 skeleton which held execute). The W6
#     trampoline gates this behind operator authorization.
#   * Homebrew install: if `brew` is not on PATH and the operator did
#     NOT pass `--no-brew`, the script announces that Homebrew needs
#     to be installed first (spec §13.3 limitation). The actual
#     Homebrew first-install is a sudo step documented in §13.3; this
#     W3 slice records `needs_homebrew_install=true` in the plan and,
#     in --execute mode, exits with EXIT_DEPENDENCY_FLOOR_NOT_MET (12)
#     so the operator can install Homebrew by hand. This is the
#     honest-scope contract: the bootstrap does not silently sudo.
#   * Brew prefix: detected via `brew --prefix` (NOT hardcoded). Apple
#     Silicon uses `/opt/homebrew`; Intel uses `/usr/local`
#     (spec §13.3). The prefix is recorded in the plan.
#
# Usage:
#   bash bootstrap/lib/macos_deps.sh --repo-root <path> --profile <profile>
#       [--dry-run|--execute] [--no-brew] [--system]
#
# Exit codes (spec §10.4; constants defined in aee.installer.lifecycle):
#   0  — success (or dry-run plan printed)
#   2  — argument parsing failure
#   7  — stage failed retryable (brew lock, transient network)
#   10 — network error (brew tap unreachable)
#   12 — dependency floor not met (brew missing + --no-brew not set;
#        --system requested on macOS; unsupported platform)
#
# W3 scope rule: this script implements macOS ONLY. If the detected
# platform is not Darwin (macOS), it exits 12 with a clear message.
# Ubuntu/Debian (apt) and Windows (winget) are separate work orders.

set -euo pipefail

# ---------------------------------------------------------------------------
# Source the detect shim for platform helpers (reuse W2 detect.sh which
# already supports darwin via the uname fallback).
# ---------------------------------------------------------------------------
script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=detect.sh
. "$script_dir/detect.sh"

# ---------------------------------------------------------------------------
# Exit codes (mirror aee.installer.lifecycle — kept as integers so this
# script works without Python present, per §4 stage 01 ownership).
# ---------------------------------------------------------------------------
EXIT_OK=0
EXIT_PARSE_ERROR=2
EXIT_STAGE_FAILED_RETRYABLE=7
EXIT_NETWORK_ERROR=10
EXIT_DEPENDENCY_FLOOR_NOT_MET=12

# ---------------------------------------------------------------------------
# Manifest parsing: read brew.deps.txt, skip comments and blanks.
# (Mirrors the W2 read_manifest helper; kept local for self-containment.)
# ---------------------------------------------------------------------------
# read_manifest <manifest_path>
# Prints one formula name per line to stdout, comments/blanks stripped.
read_manifest() {
    local manifest="$1"
    if [ ! -f "$manifest" ]; then
        printf 'macos_deps.sh: manifest not found: %s\n' "$manifest" >&2
        return 1
    fi
    awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        { gsub(/[[:space:]]+$/, ""); print }
    ' "$manifest"
}

# ---------------------------------------------------------------------------
# Profile gating: filter the raw formula list by the requested profile.
# (Mirrors the W2 filter_packages_by_profile; supervisor → mini+full,
#  docker → full+edge. On macOS v1 only developer is supported, so
#  neither supervisor nor docker is ever installed by W3.)
# ---------------------------------------------------------------------------
# filter_formulae_by_profile <raw_formulae> <profile>
# Reads raw formula list from stdin, prints filtered list to stdout.
filter_formulae_by_profile() {
    local profile="$1"
    while IFS= read -r f; do
        case "$f" in
            supervisor)
                case "$profile" in
                    mini|full) printf '%s\n' "$f" ;;
                    *) ;;
                esac
                ;;
            docker)
                case "$profile" in
                    full|edge) printf '%s\n' "$f" ;;
                    *) ;;
                esac
                ;;
            *)
                # Core hard dep — always installed.
                printf '%s\n' "$f"
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# macOS detection helpers
# ---------------------------------------------------------------------------
# detect_macos_kernel: prints `Darwin` on macOS, or the actual uname -s
# output otherwise. Always exits 0.
detect_macos_kernel() {
    uname -s 2>/dev/null || printf 'unknown'
}

# detect_brew_prefix: prints `brew --prefix` output (e.g. /opt/homebrew
# on Apple Silicon, /usr/local on Intel). Prints `unknown` if brew is
# not on PATH. Always exits 0.
detect_brew_prefix() {
    if ! command -v brew >/dev/null 2>&1; then
        printf 'unknown'
        return 0
    fi
    brew --prefix 2>/dev/null || printf 'unknown'
}

# brew_available: returns 0 if brew is on PATH, 1 otherwise.
brew_available() {
    command -v brew >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# brew helpers
# ---------------------------------------------------------------------------
# brew_install_run <formulae...>: installs formulae via `brew install
# --quiet` (spec §6.3). Idempotent — brew short-circuits already
# installed formulae. NO sudo (spec §6.4 + §13.3).
brew_install_run() {
    local formulae=("$@")
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] brew install --quiet %s\n' "${formulae[*]}"
        return 0
    fi
    brew install --quiet "${formulae[@]}"
}

# install_homebrew: announces that Homebrew needs a first-install. Per
# spec §13.3 the first /opt/homebrew setup requires sudo and is an
# operator step; this W3 slice does NOT auto-sudo. In --execute mode
# it exits 12 so the operator can install Homebrew by hand. In
# --dry-run mode it prints the plan.
install_homebrew() {
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] Homebrew first-install (operator step; see §13.3):\n'
        printf '[dry-run]   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"\n'
        return 0
    fi
    printf 'macos_deps.sh: Homebrew not on PATH and --no-brew not set.\n' >&2
    printf 'macos_deps.sh: Homebrew first-install requires sudo (operator step; spec §13.3).\n' >&2
    printf 'macos_deps.sh: Install Homebrew by hand, then re-run with --execute.\n' >&2
    return 1
}

# install_uv: installs uv via pip (spec §6.1). Idempotent — skips if
# `uv` is already on PATH. Uses the brew-installed python@3.11.
install_uv() {
    if command -v uv >/dev/null 2>&1; then
        printf 'uv already installed: %s\n' "$(uv --version 2>/dev/null || echo unknown)"
        return 0
    fi
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] python3 -m pip install --user uv\n'
        return 0
    fi
    # Use the brew python@3.11 if available; fall back to system python3.
    local py_bin="python3"
    if [ -x "/opt/homebrew/bin/python3.11" ]; then
        py_bin="/opt/homebrew/bin/python3.11"
    elif [ -x "/usr/local/bin/python3.11" ]; then
        py_bin="/usr/local/bin/python3.11"
    fi
    "$py_bin" -m pip install --user uv
}

# ---------------------------------------------------------------------------
# main: parse args, detect platform, install deps.
# ---------------------------------------------------------------------------
main() {
    local repo_root="."
    local profile=""
    local dry_run=1
    local no_brew=0
    local system_scope=0

    while [ $# -gt 0 ]; do
        case "$1" in
            --repo-root)
                repo_root="$2"; shift 2 ;;
            --repo-root=*)
                repo_root="${1#--repo-root=}"; shift ;;
            --profile)
                profile="$2"; shift 2 ;;
            --profile=*)
                profile="${1#--profile=}"; shift ;;
            --dry-run)
                dry_run=1; shift ;;
            --execute)
                dry_run=0; shift ;;
            --no-brew)
                no_brew=1; shift ;;
            --system)
                system_scope=1; shift ;;
            --help|-h)
                cat <<'EOF'
AEE Bootstrap v1 — W3 macOS Homebrew dependency installer

Usage: bootstrap/lib/macos_deps.sh --repo-root <path> --profile <profile>
      [--dry-run | --execute] [--no-brew] [--system]

W3 scope: macOS ONLY. Exits 12 if the detected platform is not Darwin.

Options:
  --repo-root <path>   Path to the AEE repo root (required for manifest
                       resolution).
  --profile <name>     One of: full, mini, edge, developer.
                       macOS v1 supports only `developer` (spec §13.3).
  --dry-run            Print planned brew commands without executing
                       (default).
  --execute            Perform real brew installs (NO sudo; spec §6.4).
  --no-brew            Skip Homebrew install/dependency. The operator
                       must have python@3.11 available another way
                       (e.g. python.org installer; spec §13.3).
  --system             Request system-scope install. NOT supported on
                       macOS v1 (spec §6.5); exits 12.

Exit codes:
  0  success / dry-run plan printed
  2  parse error
  7  stage failed retryable (brew lock)
  10 network error
  12 dependency floor not met (unsupported platform, brew missing +
     --no-brew not set, --system on macOS)
EOF
                exit 0
                ;;
            *)
                printf 'macos_deps.sh: unknown argument: %s\n' "$1" >&2
                exit "$EXIT_PARSE_ERROR"
                ;;
        esac
    done

    # --- Validate args ---
    if [ -z "$profile" ]; then
        printf 'macos_deps.sh: --profile is required\n' >&2
        exit "$EXIT_PARSE_ERROR"
    fi
    case "$profile" in
        full|mini|edge|developer) ;;
        *)
            printf 'macos_deps.sh: unknown profile: %s\n' "$profile" >&2
            exit "$EXIT_PARSE_ERROR"
            ;;
    esac

    # --- macOS v1 supports only `developer` (defence in depth) ---
    if [ "$profile" != "developer" ]; then
        printf 'macos_deps.sh: macOS v1 supports only the developer profile (spec §13.3); got: %s\n' \
            "$profile" >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi

    # --- System-scope is not supported on macOS v1 (spec §6.5) ---
    if [ "$system_scope" = "1" ]; then
        printf 'macos_deps.sh: --system is not supported on macOS v1 (spec §6.5); operator runs launchd by hand\n' >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi

    # --- Detect platform (W3 scope gate) ---
    local kernel brew_prefix
    kernel="$(detect_macos_kernel)"
    if [ "$kernel" != "Darwin" ]; then
        printf 'macos_deps.sh: W3 supports macOS only; detected kernel=%s\n' \
            "$kernel" >&2
        printf 'macos_deps.sh: Ubuntu/Debian (apt) and Windows (winget) are separate work orders.\n' >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi
    brew_prefix="$(detect_brew_prefix)"

    # --- Verify brew is available (or --no-brew) ---
    local brew_present=0
    if brew_available; then
        brew_present=1
    fi
    if [ "$brew_present" = "0" ] && [ "$no_brew" = "0" ]; then
        # Brew missing and operator did not opt out → announce install.
        # In --dry-run, print the plan; in --execute, exit 12 (honest scope).
        if ! install_homebrew; then
            exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
        fi
        # In dry-run we proceed to print the formula plan (the brew
        # install itself is a separate operator step per §13.3).
    fi

    # --- Load + filter manifest ---
    local manifest="$repo_root/bootstrap/manifests/brew.deps.txt"
    local raw_formulae filtered_formulae
    if ! raw_formulae="$(read_manifest "$manifest")"; then
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi
    filtered_formulae="$(printf '%s\n' "$raw_formulae" | filter_formulae_by_profile "$profile")"

    if [ -z "$filtered_formulae" ]; then
        printf 'macos_deps.sh: no formulae to install after profile filter\n' >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi

    # --- Propagate DRY_RUN state to helpers (P1 fix parity with W2) ---
    # Helpers (brew_install_run, install_homebrew, install_uv) read
    # ${DRY_RUN:-1} from the environment. The CLI flags must
    # deterministically control execution regardless of any inherited
    # DRY_RUN value:
    #   * --execute  → DRY_RUN=0 (real brew installs)
    #   * --dry-run  → DRY_RUN=1 (plan only; overrides inherited DRY_RUN=0)
    #   * default    → DRY_RUN=1
    # Exporting here (after arg parsing, before any helper call) makes
    # the CLI flag the authoritative source and prevents an inherited
    # DRY_RUN=0 from authorizing real execution when the operator
    # passed --dry-run.
    export DRY_RUN="$dry_run"

    # --- Announce plan ---
    printf 'macos_deps.sh: kernel=%s brew_prefix=%s profile=%s dry_run=%d no_brew=%d\n' \
        "$kernel" "$brew_prefix" "$profile" "$dry_run" "$no_brew"
    printf 'macos_deps.sh: formulae: %s\n' "$(echo "$filtered_formulae" | tr '\n' ' ')"

    # --- Skip brew install when --no-brew ---
    if [ "$no_brew" = "1" ]; then
        printf 'macos_deps.sh: --no-brew set; skipping brew install (operator provides python@3.11)\n'
    elif [ "$brew_present" = "0" ]; then
        # Brew missing + dry-run: install_homebrew already printed the
        # plan; skip the brew install step (no brew to install with).
        printf 'macos_deps.sh: brew not on PATH; skipping brew install step (see Homebrew install plan above)\n'
    else
        # --- Install formulae ---
        # shellcheck disable=SC2086
        if ! brew_install_run $filtered_formulae; then
            printf 'macos_deps.sh: brew install failed (retryable)\n' >&2
            exit "$EXIT_STAGE_FAILED_RETRYABLE"
        fi
    fi

    # --- Install uv (pip, not brew) ---
    if ! install_uv; then
        printf 'macos_deps.sh: uv install failed (non-fatal; pip fallback available)\n' >&2
        # Non-fatal — uv is preferred but pip is the fallback (spec §6.1).
    fi

    printf 'macos_deps.sh: stage 01_deps %s\n' \
        "$([ "$dry_run" = "1" ] && echo 'planned (dry-run)' || echo 'completed')"
    exit "$EXIT_OK"
}

# Run main only when executed, not when sourced.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    main "$@"
fi