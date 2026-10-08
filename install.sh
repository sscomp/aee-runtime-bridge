#!/usr/bin/env bash
# AEE Epic 9.3 — Installer Shell Wrapper (Master Plan §21.3).
#
# Single profile-aware installer wrapper that delegates ALL profile
# validation, planning, and read-only pre-flight to the canonical
# Python CLI (aee.cli → aee.installer.backend). Per Master Plan
# §21.3, this script accepts:
#
#   install.sh --profile {full,mini,edge,developer}
#
# The default profile is ``full`` (matching
# aee.profiles.descriptor.DEFAULT_PROFILE). This script does NOT
# maintain a parallel hard-coded profile matrix — the four profile
# names and the default come from the canonical Python source via
# ``python3 -m aee.cli install --help``.
#
# Safety contract:
#   * Default mode is dry-run: read-only planning and pre-flight,
#     with no bootstrap side effects.
#   * Explicit --execute delegates to the existing BootstrapRunner:
#     stages 02-07 may create a venv, install dependencies and write
#     bootstrap markers. Credentials are operator-provisioned.
#   * Profile validation and exit codes come from the Python CLI.
#
# Exit code propagation (composed with the Python backend):
#   0  — dry-run or bootstrap success
#   2  — argument parsing failure (argparse / shell usage)
#   3  — unknown profile (defence in depth)
#   4  — pre-flight or bootstrap stage failed
#   5  — profile switch rejected (existing install with different profile)
#  64  — missing python interpreter
#  65  — missing CLI or installer module
#  70  — internal error (unexpected CLI output)
#
# Run: ./install.sh --help
#      ./install.sh --profile mini --dry-run
#      ./install.sh --profile edge --execute   (real bootstrap; opt in)

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the repo root (directory containing this script). The Python
# CLI is invoked with PYTHONPATH=<repo_root> so the ``aee`` package is
# importable regardless of the caller's cwd.
# ---------------------------------------------------------------------------
script_path="${BASH_SOURCE[0]:-$0}"
# Resolve symlinks so a wrapper invoked through a symlink finds the
# real repo root.
while [ -h "$script_path" ]; do
    dir="$(cd -P "$(dirname "$script_path")" >/dev/null 2>&1 && pwd)"
    script_path="$(readlink "$script_path")"
    case "$script_path" in
        /*) : ;;                  # absolute
        *)  script_path="$dir/$script_path" ;;  # relative
    esac
done
repo_root="$(cd -P "$(dirname "$script_path")" >/dev/null 2>&1 && pwd)"
export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
profile=""
dry_run=1          # default is dry-run (safe)
execute=0          # --execute must be passed explicitly
json_output=0      # --json forwarded to Python CLI
show_help=0

# ---------------------------------------------------------------------------
# Usage / help
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE'
AEE Installer (§21.3 shell wrapper)

Usage:
  install.sh [--profile {full,mini,edge,developer}] [--dry-run] [--json] [--execute] [--help]

Options:
  --profile {full,mini,edge,developer}
                      Product profile to install for. Default: full
                      (matches aee.profiles.descriptor.DEFAULT_PROFILE).
                      Profile validation is delegated to the canonical
                      Python CLI; this wrapper does NOT maintain a parallel
                      hard-coded matrix.

  --dry-run           Print the resolved profile + install plan and exit
                      without performing any side effects. This is the
                      DEFAULT mode. Read-only pre-flight is performed.

  --json              Emit the install plan as a JSON object on stdout
                      (forwarded to the Python CLI's --json flag).

  --execute           Run the existing BootstrapRunner (stages 02-07).
                      This is an explicit opt-in to bootstrap side effects:
                      venv creation, locked dependency installation, health
                      and smoke checks, and AGENT_READY marker writes.
                      Credentials must already be provisioned by the operator.

  -h, --help          Show this help message and exit.

Exit codes:
  0   dry-run or bootstrap success
  2   argument parsing failure
  3   unknown profile (defence in depth)
  4   pre-flight or bootstrap stage failed
  5   profile switch rejected (existing install with different profile)
  64  missing python interpreter
  65  missing CLI or installer module
  70  internal error (unexpected CLI output)

NOTE: Default --dry-run performs no bootstrap side effects. Explicit --execute
runs bootstrap stages; use it only for an operator-approved installation.
See docs/aee/bootstrap/onboarding.md for the existing bootstrap contract.
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing (long options only; POSIX-friendly while loop)
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --profile)
            if [ $# -lt 2 ]; then
                echo "install.sh: --profile requires an argument" >&2
                usage >&2
                exit 2
            fi
            profile="$2"
            shift 2
            ;;
        --profile=*)
            profile="${1#--profile=}"
            shift
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        --json)
            json_output=1
            shift
            ;;
        --execute)
            execute=1
            shift
            ;;
        -h|--help)
            show_help=1
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "install.sh: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$show_help" -eq 1 ]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Locate the Python interpreter. Prefer python3, fall back to python.
# Exit code 64 if no interpreter is found.
# ---------------------------------------------------------------------------
python_bin=""
if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
elif command -v python >/dev/null 2>&1; then
    python_bin="python"
else
    echo "install.sh: no python interpreter found (python3 or python)" >&2
    exit 64
fi

# ---------------------------------------------------------------------------
# Verify the CLI and installer backend are importable. Exit 65 if missing.
# This catches the case where the wrapper is invoked from outside the
# repo or the aee package is not on PYTHONPATH.
# ---------------------------------------------------------------------------
if ! "$python_bin" -c 'import aee.cli; import aee.installer.cli_install' >/dev/null 2>&1; then
    echo "install.sh: cannot import aee.cli or aee.installer.cli_install" >&2
    echo "install.sh: ensure you are invoking this wrapper from the repo root" >&2
    exit 65
fi

# Explicit --execute delegates to the existing BootstrapRunner (stages 02-07).
# Interpreter and module checks apply before either dispatch mode.
if [ "$execute" -eq 1 ]; then
    # Forward --execute to the Python CLI so the BootstrapRunner drives
    # the stage chain. The CLI exits 0 on success (AGENT_READY written)
    # or non-zero on stage failure.
    cli_args=(install)
    if [ -n "$profile" ]; then
        cli_args+=(--profile "$profile")
    fi
    if [ "$json_output" -eq 1 ]; then
        cli_args+=(--json)
    fi
    cli_args+=(--execute)
    set +e
    "$python_bin" -m aee.cli "${cli_args[@]}"
    cli_exit=$?
    set -e
    exit "$cli_exit"
fi

# ---------------------------------------------------------------------------
# Build the Python CLI argv. The canonical entrypoint is:
#   python3 -m aee.cli install --profile <profile> [--dry-run] [--json]
#
# Profile validation is delegated entirely to the Python CLI's argparse
# (choices=KNOWN_PROFILES). An invalid profile exits 2 from argparse;
# the wrapper propagates that exit code.
# ---------------------------------------------------------------------------
cli_args=(install)
if [ -n "$profile" ]; then
    cli_args+=(--profile "$profile")
fi
if [ "$json_output" -eq 1 ]; then
    cli_args+=(--json)
fi
# --dry-run is the Python CLI's default behavior; pass it explicitly
# for clarity and forward-compat (the flag is accepted by the CLI).
cli_args+=(--dry-run)

# ---------------------------------------------------------------------------
# Invoke the canonical Python CLI and propagate its exit code.
# set -e is already on; we capture the exit code explicitly so the
# wrapper can map it if needed in the future.
# ---------------------------------------------------------------------------
set +e
"$python_bin" -m aee.cli "${cli_args[@]}"
cli_exit=$?
set -e

exit "$cli_exit"
