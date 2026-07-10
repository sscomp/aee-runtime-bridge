#!/usr/bin/env bash
# test_create_run.sh — start a Hermes run via the bridge and print run_id.
#
# Usage:
#     export BRIDGE_API_KEY=...
#     ./test_create_run.sh [BASE_URL] [INPUT]
#     # default BASE_URL=http://127.0.0.1:8787
#     # default INPUT="請執行 pwd 並回傳結果。"
#
# Exits 0 on success and writes the run_id to /tmp/bridge_last_run_id so the
# companion tests (get/stop) can chain.

set -euo pipefail

BASE="${1:-http://127.0.0.1:8787}"
INPUT="${2:-請執行 pwd 並回傳目前目錄。}"

if [ -z "${BRIDGE_API_KEY:-}" ]; then
    echo "ERROR: BRIDGE_API_KEY is not set" >&2
    exit 64
fi

PAYLOAD=$(python3 -c "
import json, os
print(json.dumps({
    'input': os.environ.get('TEST_INPUT') or '''$INPUT''',
    'session_id': 'dingde-orchestrator-test',
    'mode': 'normal',
    'timeout_seconds': 120,
}))
")

echo "==> POST ${BASE}/runs"
RESP=$(curl -sS -w "\n%{http_code}" -X POST "${BASE}/runs" \
    -H "Authorization: Bearer ${BRIDGE_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

if [ "$CODE" != "200" ]; then
    echo "FAIL: expected 200, got $CODE"
    echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
    exit 1
fi

echo "$BODY" | python3 -m json.tool

RUN_ID=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin)['run_id'])")
echo "$RUN_ID" > /tmp/bridge_last_run_id
echo "==> saved run_id to /tmp/bridge_last_run_id: $RUN_ID"
echo "PASS"
