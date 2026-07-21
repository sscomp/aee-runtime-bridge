#!/usr/bin/env bash
# AEE Final-Mile — Claude Code CLI executor smoke via POST /runs/executor.
#
# Usage:
#   export AEE_BRIDGE_TOKEN="<bearer token from the bridge host>"
#   export AEE_RUNTIME_BRIDGE_BASE_URL="https://<your-bridge-public-host>"
#   bash gpt/examples/curl_executor_claude_code_cli.sh
#
# Expected: a 200 with selected_executor == "claude-code-cli", status == "completed",
# a real exit_code, and artifact_verification[].exists == true for the declared file.
set -euo pipefail

: "${AEE_BRIDGE_TOKEN:?AEE_BRIDGE_TOKEN must be set (bearer token from the bridge host)}"
: "${AEE_RUNTIME_BRIDGE_BASE_URL:?AEE_RUNTIME_BRIDGE_BASE_URL must be set (e.g. https://bridge.example.com)}"

curl -sS -X POST \
  -H "Authorization: Bearer ${AEE_BRIDGE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "executor": "claude-code-cli",
    "prompt": "Use the write tool to create /tmp/aee_executor_smoke.md with content: hello from claude-code-cli",
    "expected_artifacts": ["/tmp/aee_executor_smoke.md"],
    "timeout_sec": 120
  }' \
  "${AEE_RUNTIME_BRIDGE_BASE_URL}/runs/executor" | jq