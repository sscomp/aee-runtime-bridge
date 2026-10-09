#!/usr/bin/env bash
# Compile the A3 closure without touching the live environment or legacy locks.
set -euo pipefail
cd "$(dirname "$0")/.."
uv pip compile requirements-mcp.in -c requirements-mcp.constraints.txt \
  --python-version 3.13 --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes --custom-compile-command 'bash scripts/compile-mcp-deps.sh' \
  -o requirements-mcp.lock
