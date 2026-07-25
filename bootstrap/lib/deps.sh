#!/usr/bin/env bash
# AEE Bootstrap v1 — W2 Ubuntu/Debian dependency installer (spec §6, §13.1, §13.2).
#
# Stage 01_deps for Ubuntu/Debian ONLY. Installs the hard + profile-gated
# apt dependencies listed in `bootstrap/manifests/apt.deps.txt` via
# `apt-get install --no-install-recommends` (spec §6.3 reproducibility).
# macOS and Windows are out of scope for W2.
#
# Contract (spec §5.1 idempotency, §6.4 privilege, §6.5 scope):
#   * Idempotent: re-running is a no-op when packages are already
#     installed (apt's own short-circuit). Does NOT remove packages.
#   * Privilege: uses `sudo` only for apt commands. The runtime runs
#     as the install owner (non-root) per §6.4.
#   * Scope: per-user by default. System-scope (`--system`) is accepted
#     but only changes the apt invocation path (sudo is always required
#     for apt); runtime user is unchanged.
#   * Dry-run by default: `--dry-run` prints the planned apt commands
#     without executing them. `--execute` is required for real installs.
#     Even with `--execute`, this W2 slice WILL perform real apt installs
#     (unlike the W1 skeleton which held execute). The W6 trampoline
#     gates this behind operator authorization.
#
# Usage:
#   bash bootstrap/lib/deps.sh --repo-root <path> --profile <profile> [--execute|--dry-run]
#
# Exit codes (spec §10.4; constants defined in aee.installer.lifecycle):
#   0  — success (or dry-run plan printed)
#   2  — argument parsing failure
#   7  — stage failed retryable (apt lock held, transient network)
#   10 — network error (apt mirror unreachable)
#   12 — dependency floor not met (apt missing, unsupported distro)
#
# W2 scope rule: this script implements Ubuntu/Debian ONLY. If the
# detected distro is not `ubuntu` or `debian`, it exits 12 with a clear
# message. macOS (brew) and Windows (winget) are separate work orders.

set -euo pipefail

# ---------------------------------------------------------------------------
# Source the detect shim for distro/version helpers.
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
# Manifest parsing: read apt.deps.txt, skip comments and blanks.
# ---------------------------------------------------------------------------
# read_manifest <manifest_path>
# Prints one package name per line to stdout, comments/blanks stripped.
read_manifest() {
    local manifest="$1"
    if [ ! -f "$manifest" ]; then
        printf 'deps.sh: manifest not found: %s\n' "$manifest" >&2
        return 1
    fi
    awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        { gsub(/[[:space:]]+$/, ""); print }
    ' "$manifest"
}

# ---------------------------------------------------------------------------
# Profile gating: filter the raw package list by the requested profile.
# ---------------------------------------------------------------------------
# filter_packages_by_profile <raw_packages> <profile>
# Reads raw package list from stdin, prints filtered list to stdout.
# Rules (spec §6.2):
#   * Core hard deps (git, python3, python3-pip, python3-venv, curl,
#     ca-certificates, gnupg, python3.11, python3.11-venv) — always
#     installed for all profiles.
#   * supervisor — mini + full only.
#   * docker.io — full + edge only.
filter_packages_by_profile() {
    local profile="$1"
    while IFS= read -r pkg; do
        case "$pkg" in
            supervisor)
                case "$profile" in
                    mini|full) printf '%s\n' "$pkg" ;;
                    *) ;;
                esac
                ;;
            docker.io)
                case "$profile" in
                    full|edge) printf '%s\n' "$pkg" ;;
                    *) ;;
                esac
                ;;
            *)
                # Core hard dep — always installed.
                printf '%s\n' "$pkg"
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# apt helpers
# ---------------------------------------------------------------------------
# apt_update_run: runs apt-get update (with sudo). Idempotent.
apt_update_run() {
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] sudo apt-get update\n'
        return 0
    fi
    sudo apt-get update -qq
}

# apt_install_run <packages...>: installs packages via apt-get with
# --no-install-recommends (spec §6.3). Idempotent.
apt_install_run() {
    local pkgs=("$@")
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] sudo apt-get install --no-install-recommends -y %s\n' "${pkgs[*]}"
        return 0
    fi
    sudo apt-get install --no-install-recommends -y -qq "${pkgs[@]}"
}

# maybe_add_deadsnakes <distro> <version_id>: adds the deadsnakes PPA on
# Ubuntu 22.04 (which ships python3.10) so python3.11 is installable.
# No-op on Debian (uses native python3.11) and on Ubuntu 24.04 (native 3.12).
maybe_add_deadsnakes() {
    local distro="$1"
    local version_id="$2"
    if [ "$distro" != "ubuntu" ]; then
        return 0
    fi
    if [ "$version_id" != "22.04" ]; then
        return 0
    fi
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] sudo add-apt-repository -y ppa:deadsnakes/ppa\n'
        return 0
    fi
    # add-apt-repository is in software-properties-common.
    if ! command -v add-apt-repository >/dev/null 2>&1; then
        sudo apt-get install --no-install-recommends -y -qq software-properties-common
    fi
    sudo add-apt-repository -y ppa:deadsnakes/ppa
}

# install_uv: installs uv via pip (spec §6.1). Idempotent — skips if
# `uv` is already on PATH.
install_uv() {
    if command -v uv >/dev/null 2>&1; then
        printf 'uv already installed: %s\n' "$(uv --version 2>/dev/null || echo unknown)"
        return 0
    fi
    if [ "${DRY_RUN:-1}" = "1" ]; then
        printf '[dry-run] python3 -m pip install --user uv\n'
        return 0
    fi
    python3 -m pip install --user uv
}

# ---------------------------------------------------------------------------
# main: parse args, detect distro, install deps.
# ---------------------------------------------------------------------------
main() {
    local repo_root="."
    local profile=""
    local dry_run=1

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
            --help|-h)
                cat <<'EOF'
AEE Bootstrap v1 — W2 Ubuntu/Debian dependency installer

Usage: bootstrap/lib/deps.sh --repo-root <path> --profile <profile>
      [--dry-run | --execute]

W2 scope: Ubuntu/Debian ONLY. Exits 12 if the detected distro is not
ubuntu or debian.

Options:
  --repo-root <path>   Path to the AEE repo root (required for manifest
                       resolution).
  --profile <name>     One of: full, mini, edge, developer.
  --dry-run            Print planned apt commands without executing
                       (default).
  --execute            Perform real apt installs (requires sudo).

Exit codes:
  0  success / dry-run plan printed
  2  parse error
  7  stage failed retryable (apt lock)
  10 network error
  12 dependency floor not met (unsupported distro, apt missing)
EOF
                exit 0
                ;;
            *)
                printf 'deps.sh: unknown argument: %s\n' "$1" >&2
                exit "$EXIT_PARSE_ERROR"
                ;;
        esac
    done

    # --- Validate args ---
    if [ -z "$profile" ]; then
        printf 'deps.sh: --profile is required\n' >&2
        exit "$EXIT_PARSE_ERROR"
    fi
    case "$profile" in
        full|mini|edge|developer) ;;
        *)
            printf 'deps.sh: unknown profile: %s\n' "$profile" >&2
            exit "$EXIT_PARSE_ERROR"
            ;;
    esac

    # --- Detect distro (W2 scope gate) ---
    local distro version_id
    distro="$(detect_linux_distro)"
    version_id="$(detect_linux_version_id)"
    if [ "$distro" != "ubuntu" ] && [ "$distro" != "debian" ]; then
        printf 'deps.sh: W2 supports Ubuntu/Debian only; detected distro=%s\n' \
            "$distro" >&2
        printf 'deps.sh: macOS (brew) and Windows (winget) are separate work orders.\n' >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi

    # --- Verify apt is available ---
    if ! command -v apt-get >/dev/null 2>&1; then
        printf 'deps.sh: apt-get not found on %s — cannot proceed\n' "$distro" >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi

    # --- Load + filter manifest ---
    local manifest="$repo_root/bootstrap/manifests/apt.deps.txt"
    local raw_pkgs filtered_pkgs
    if ! raw_pkgs="$(read_manifest "$manifest")"; then
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi
    filtered_pkgs="$(printf '%s\n' "$raw_pkgs" | filter_packages_by_profile "$profile")"

    if [ -z "$filtered_pkgs" ]; then
        printf 'deps.sh: no packages to install after profile filter\n' >&2
        exit "$EXIT_DEPENDENCY_FLOOR_NOT_MET"
    fi

    # --- Propagate DRY_RUN state to helpers (P1 fix, TASK-20260725-0020) ---
    # Helpers (apt_update_run, apt_install_run, maybe_add_deadsnakes,
    # install_uv) read ${DRY_RUN:-1} from the environment. The CLI flags
    # must deterministically control execution regardless of any inherited
    # DRY_RUN value:
    #   * --execute  → DRY_RUN=0 (real apt installs)
    #   * --dry-run  → DRY_RUN=1 (plan only; overrides inherited DRY_RUN=0)
    #   * default    → DRY_RUN=1
    # Exporting here (after arg parsing, before any helper call) makes the
    # CLI flag the authoritative source and prevents an inherited DRY_RUN=0
    # from authorizing real execution when the operator passed --dry-run.
    export DRY_RUN="$dry_run"

    # --- Announce plan ---
    printf 'deps.sh: distro=%s version_id=%s profile=%s dry_run=%d\n' \
        "$distro" "$version_id" "$profile" "$dry_run"
    printf 'deps.sh: packages: %s\n' "$(echo "$filtered_pkgs" | tr '\n' ' ')"

    # --- apt update ---
    if ! apt_update_run; then
        printf 'deps.sh: apt-get update failed (network?)\n' >&2
        exit "$EXIT_NETWORK_ERROR"
    fi

    # --- deadsnakes PPA for Ubuntu 22.04 ---
    if ! maybe_add_deadsnakes "$distro" "$version_id"; then
        printf 'deps.sh: deadsnakes PPA add failed (retryable)\n' >&2
        exit "$EXIT_STAGE_FAILED_RETRYABLE"
    fi

    # --- Install packages ---
    # shellcheck disable=SC2086
    if ! apt_install_run $filtered_pkgs; then
        printf 'deps.sh: apt-get install failed (retryable)\n' >&2
        exit "$EXIT_STAGE_FAILED_RETRYABLE"
    fi

    # --- Install uv (pip, not apt) ---
    if ! install_uv; then
        printf 'deps.sh: uv install failed (non-fatal; pip fallback available)\n' >&2
        # Non-fatal — uv is preferred but pip is the fallback (spec §6.1).
    fi

    printf 'deps.sh: stage 01_deps %s\n' \
        "$([ "$dry_run" = "1" ] && echo 'planned (dry-run)' || echo 'completed')"
    exit "$EXIT_OK"
}

# Run main only when executed, not when sourced.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    main "$@"
fi