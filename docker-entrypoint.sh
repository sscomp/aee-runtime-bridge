#!/usr/bin/env bash
# AEE Epic 9.5 — Docker Entrypoint (Master Plan §21.5).
#
# Single Docker image, profile selected at ``docker run`` time:
#   docker run aee:X.Y.Z --profile {full,mini,edge,developer} [CMD...]
#
# The entrypoint:
#   1. Parses ``--profile`` from args (default: ``full``, matching
#      ``aee.profiles.descriptor.DEFAULT_PROFILE``).
#   2. Validates the profile via the canonical Python source
#      (``aee.profiles.descriptor.parse_profile``) — no parallel
#      hard-coded profile matrix (per §21.3 / §21.2 design rule).
#   3. Sets profile-specific environment variables:
#        AEE_PROFILE=<profile>          (all profiles)
#        AEE_DB_READ_ONLY=1             (edge only, per §21.5 line 7630)
#        AEE_DB_PATH=/tmp/aee-dev.db    (developer tempdir DB, per §21.5)
#   4. If a command follows ``--profile``, ``exec`` it with the env
#      vars set. Otherwise, print the resolved profile + env vars and
#      exit 0 (smoke-test / inspection mode).
#
# Exit codes:
#   0  — success (profile resolved, env set, command exec'd or info printed)
#   3  — unknown profile (defence in depth; canonical parser rejected)
#   64 — missing python interpreter
#   65 — missing aee.profiles.descriptor module
#   70 — internal error
#
# Compatibility surface (must NOT be modified by this slice):
#   * aee/profiles/descriptor.py  — canonical profile matrix (§21.1)
#   * aee/cli.py                  — unified CLI UX (§21.2)
#   * aee/installer/backend.py    — installer backend (§21.3)
#   * install.sh                  — shell wrapper (§21.3)
#   * dispatcher/db.py            — runtime profile selection (§21.4)
#   * dispatcher/safety.py        — safety-gate enforcement (AEE-8.3)
#
# This script is ADDITIVE: it re-uses the canonical parse_profile and
# does not duplicate the profile matrix.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate repo root (directory containing this script inside the image).
# ---------------------------------------------------------------------------
script_path="${BASH_SOURCE[0]:-$0}"
while [ -h "$script_path" ]; do
    dir="$(cd -P "$(dirname "$script_path")" >/dev/null 2>&1 && pwd)"
    script_path="$(readlink "$script_path")"
    case "$script_path" in
        /*) : ;;
        *)  script_path="$dir/$script_path" ;;
    esac
done
repo_root="$(cd -P "$(dirname "$script_path")" >/dev/null 2>&1 && pwd)"
export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"

# ---------------------------------------------------------------------------
# Locate python3.
# ---------------------------------------------------------------------------
python_bin="${PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
    echo "error: docker-entrypoint: python3 not found" >&2
    exit 64
fi

# ---------------------------------------------------------------------------
# Parse --profile from args. Default is ``full`` (matches
# aee.profiles.descriptor.DEFAULT_PROFILE). The --profile flag may
# appear anywhere before the first non-flag argument (the command to
# exec). Unknown flags are passed through to the exec'd command.
# ---------------------------------------------------------------------------
profile=""
remaining=()
while [ $# -gt 0 ]; do
    case "$1" in
        --profile)
            shift
            if [ $# -eq 0 ]; then
                echo "error: docker-entrypoint: --profile requires a value" >&2
                exit 2
            fi
            profile="$1"
            shift
            ;;
        --profile=*)
            profile="${1#--profile=}"
            shift
            ;;
        --)
            shift
            remaining+=("$@")
            break
            ;;
        *)
            # First non-flag arg starts the command to exec. Stop
            # parsing --profile here; everything else is the command.
            remaining+=("$@")
            break
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate the profile via the canonical Python source. This raises
# UnknownProfileError for unknown values, which we surface as exit 3.
# ---------------------------------------------------------------------------
validate_profile() {
    "$python_bin" - <<'PYEOF' "$1"
import sys
try:
    from aee.profiles.descriptor import parse_profile
except Exception as exc:
    sys.stderr.write("error: docker-entrypoint: aee.profiles.descriptor import failed: %s\n" % exc)
    sys.exit(65)
raw = sys.argv[1]
try:
    resolved = parse_profile(raw if raw else None)
except Exception as exc:
    sys.stderr.write("error: docker-entrypoint: unknown profile: %s\n" % exc)
    sys.exit(3)
sys.stdout.write(resolved)
PYEOF
}

if [ -z "$profile" ]; then
    # No --profile supplied: use canonical default (full).
    resolved="$("$python_bin" -c "from aee.profiles.descriptor import DEFAULT_PROFILE; print(DEFAULT_PROFILE)" 2>/dev/null || echo full)"
else
    resolved="$(validate_profile "$profile")" || exit $?
fi

# ---------------------------------------------------------------------------
# Set profile-specific env vars (per Master Plan §21.5 line 7630).
# ---------------------------------------------------------------------------
export AEE_PROFILE="$resolved"

case "$resolved" in
    edge)
        # §21.5: --profile edge → AEE_DB_READ_ONLY=1 env var.
        # §21.4's dispatcher/db.py:_apply_pragmas reads this and emits
        # PRAGMA query_only=1 on every connection.
        export AEE_DB_READ_ONLY=1
        ;;
    developer)
        # §21.5: --profile developer → tempdir DB.
        export AEE_DB_PATH="${AEE_DB_PATH:-/tmp/aee-dev.db}"
        ;;
esac

# ---------------------------------------------------------------------------
# If a command was supplied after --profile, exec it with env vars set.
# Otherwise, print the resolved profile + env vars (smoke-test mode).
# ---------------------------------------------------------------------------
if [ ${#remaining[@]} -gt 0 ]; then
    exec "${remaining[@]}"
fi

# Smoke-test / inspection mode: print resolved state, exit 0.
echo "aee docker-entrypoint (§21.5)"
echo "  profile (resolved)  : $resolved"
echo "  AEE_PROFILE         : $AEE_PROFILE"
if [ -n "${AEE_DB_READ_ONLY:-}" ]; then
    echo "  AEE_DB_READ_ONLY    : $AEE_DB_READ_ONLY"
fi
if [ -n "${AEE_DB_PATH:-}" ]; then
    echo "  AEE_DB_PATH         : $AEE_DB_PATH"
fi
exit 0
