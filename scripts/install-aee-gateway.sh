#!/usr/bin/env bash
# Discoverable entry point for the canonical AEE MCP gateway installer.
# Thin wrapper — all logic lives in scripts/install_aee_gateway.py (testable,
# receipt-producing, idempotent; profiles os-native|restricted).
#
#   scripts/install-aee-gateway.sh --profile os-native --release-dir <release> \
#       --env-path <0600-env> --check|--dry-run|--apply
#
# Never acquires privileges, never copies secrets, never uses SSH. The
# unrelated facade installer at ./install.sh (Epic 9.3 profiles
# full/mini/edge/developer) is unaffected by this tool.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${script_dir}/install_aee_gateway.py" "$@"