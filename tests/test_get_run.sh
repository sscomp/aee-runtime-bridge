#!/usr/bin/env bash
# test_get_run.sh — poll a run's current status via the bridge.
#
# Usage:
#     export BRIDGE_API_KEY=...
#     ./test_get_run.sh [BASE_URL] [RUN_ID]
#     # default RUN_ID = /tmp/bridge_last_run_id (set by test_create_run.sh)
#     # default BASE_URL = http://127.0.0.1:8787

set -euo pipefail

BASE="${1:-http://127.0.0.1:8787}"
RUN_ID="${2:-$(cat /tmp/bridge_last_run_id 2>/dev/null || true)}"

if [ -z "${BRIDGE_API_KEY:-}" ]; then
    echo "ERROR: BRIDGE_API_KEY is not set" >&2
    exit 64
fi
if [ -z "$RUN_ID" ]; then
    echo "ERROR: no run_id provided and /tmp/bridge_last_run_id is empty" >&2
    exit 64
fi

echo "==> GET ${BASE}/runs/${RUN_ID}"
RESP=$(curl -sS -w "\n%{http_code}" "${BASE}/runs/${RUN_ID}" \
    -H "Authorization: Bearer ${BRIDGE_API_KEY}")
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

echo "$BODY" | python3 -m json.tool

if [ "$CODE" != "200" ]; then
    echo "FAIL: expected 200, got $CODE"
    exit 1
fi

echo
echo "==> GET ${BASE}/runs/${RUN_ID}/summary"
RESP=$(curl -sS -w "\n%{http_code}" "${BASE}/runs/${RUN_ID}/summary" \
    -H "Authorization: Bearer ${BRIDGE_API_KEY}")
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

echo "$BODY" | python3 -m json.tool

if [ "$CODE" != "200" ]; then
    echo "FAIL: summary expected 200, got $CODE"
    exit 1
fi

echo "PASS"
