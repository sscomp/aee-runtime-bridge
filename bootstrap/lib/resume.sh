#!/usr/bin/env bash
# AEE Bootstrap v1 — W6 POSIX resume helper (spec §5.5, §4 stage table).
#
# Resume-from-last-stage helper for the bootstrap v1 POSIX shell layer.
# Reads the on-disk marker directory (``bootstrap/stages/``), finds the
# first stage with no marker or a ``failed`` marker, and prints the
# stage name so the caller (the future W6 POSIX trampoline) can resume
# from there rather than restarting from scratch.
#
# Spec §5.5:
#   * ``aee install --resume`` reads the marker set, finds the first
#     stage with no marker or ``state=failed``, and runs from there.
#   * The marker directory is per-install-path, so multiple installs on
#     the same host (e.g. ``~/aee-dev`` and ``/opt/aee``) do not collide.
#
# Contract:
#   * Read-only: reads marker files under the marker directory; performs
#     NO writes, NO subprocess side effects, NO apt/brew/git calls.
#   * Prints exactly one line to stdout: the stage name to resume from
#     (e.g. ``01_deps``), or ``completed`` if every stage has a
#     ``completed`` or ``skipped`` marker.
#   * Exits 0 on success, non-zero on failure (missing marker dir,
#     corrupt marker file, missing Python when Python delegation is
#     attempted).
#
# Usage:
#   bash bootstrap/lib/resume.sh --repo-root <path> [--marker-dir <path>]
#   source bootstrap/lib/resume.sh; resume_stage <marker_dir>
#
# Safety (W6 contract):
#   * No subprocess side effects (no apt, no git clone, no writes).
#   * Read-only: reads marker files only.
#   * No macOS / Windows branches (POSIX-only; Windows is W7).

set -euo pipefail

# ---------------------------------------------------------------------------
# Stage order (must match aee.installer.lifecycle.StageName — spec §4).
# ---------------------------------------------------------------------------
# The canonical stage list is owned by the Python layer
# (aee.installer.lifecycle.StageName). The shell layer keeps a literal
# copy here so resume.sh can run without a Python interpreter (stage 00
# runs before deps are installed; Python may be absent). If the Python
# layer changes the stage list, this literal MUST be updated to match
# (verified by tests/test_bootstrap_lib_resume.sh).
# ---------------------------------------------------------------------------
STAGE_ORDER=(
    "00_detect"
    "01_deps"
    "02_clone"
    "03_pin"
    "04_runtime_setup"
    "05_health_check"
    "06_smoke_test"
    "07_agent_ready"
)

# ---------------------------------------------------------------------------
# read_marker_state <marker_file>
# ---------------------------------------------------------------------------
# Reads a marker file and prints the ``state=`` value, or ``missing`` if
# the file does not exist or the state field is absent. Marker files are
# key=value text (one per line); the ``state`` field is the canonical
# StageState value (pending/in_progress/completed/failed/skipped).
# ---------------------------------------------------------------------------
read_marker_state() {
    local marker_file="$1"
    if [ ! -f "$marker_file" ]; then
        printf 'missing\n'
        return 0
    fi
    local state
    state="$(grep -E '^state=' "$marker_file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [ -z "$state" ]; then
        printf 'missing\n'
        return 0
    fi
    printf '%s\n' "$state"
}

# ---------------------------------------------------------------------------
# resume_stage <marker_dir>
# ---------------------------------------------------------------------------
# Public entry point. Iterates STAGE_ORDER, returns the first stage with
# no marker or a ``failed``/``in_progress`` marker (per §5.5: "the first
# stage with no marker or state=failed"). IN_PROGRESS is treated as
# "needs re-run" since the process that started it may have died.
# Prints the stage name to stdout, or ``completed`` if every stage is
# completed/skipped. Exits 0 on success.
# ---------------------------------------------------------------------------
resume_stage() {
    local marker_dir="$1"
    local stage marker_file state
    for stage in "${STAGE_ORDER[@]}"; do
        marker_file="${marker_dir}/${stage}"
        state="$(read_marker_state "$marker_file")"
        case "$state" in
            missing|failed|in_progress)
                printf '%s\n' "$stage"
                return 0
                ;;
            completed|skipped|pending)
                # completed/skipped → advance. pending (explicit) is
                # treated as "not yet started" → resume here.
                if [ "$state" = "pending" ]; then
                    printf '%s\n' "$stage"
                    return 0
                fi
                ;;
            *)
                # Unknown state → treat as needs-rerun (conservative).
                printf '%s\n' "$stage"
                return 0
                ;;
        esac
    done
    printf 'completed\n'
}

# ---------------------------------------------------------------------------
# CLI mode: when invoked as a script, parse --repo-root / --marker-dir
# and print the resume stage.
# ---------------------------------------------------------------------------
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    repo_root="."
    marker_dir=""
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
            --marker-dir)
                marker_dir="$2"
                shift 2
                ;;
            --marker-dir=*)
                marker_dir="${1#--marker-dir=}"
                shift
                ;;
            --help|-h)
                cat <<'EOF'
AEE Bootstrap v1 — W6 POSIX resume helper

Usage: bootstrap/lib/resume.sh --repo-root <path> [--marker-dir <path>]

Prints the stage name to resume from (the first stage with no marker or
a failed/in_progress marker), or "completed" if every stage is
completed/skipped.

Options:
  --repo-root <path>   Repo root (default: parent of bootstrap/).
  --marker-dir <path>  Marker directory (default: <repo-root>/bootstrap/stages/).

Read-only: reads marker files only; performs no writes or subprocess
side effects.

W6 scope: POSIX-only. Windows is W7 (out of scope).
EOF
                exit 0
                ;;
            *)
                printf 'resume.sh: unknown argument: %s\n' "$1" >&2
                exit 2
                ;;
        esac
    done
    # Default marker dir: <repo-root>/bootstrap/stages/
    if [ -z "$marker_dir" ]; then
        script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
        if [ "$repo_root" = "." ]; then
            repo_root="$(cd -P "$script_dir/../.." >/dev/null 2>&1 && pwd)"
        fi
        marker_dir="${repo_root}/bootstrap/stages"
    fi
    if [ ! -d "$marker_dir" ]; then
        # No marker dir → fresh install → resume from first stage.
        printf '%s\n' "${STAGE_ORDER[0]}"
        exit 0
    fi
    resume_stage "$marker_dir"
fi