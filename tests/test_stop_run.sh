#!/usr/bin/env bash
# test_stop_run.sh — request cancellation of a run via the bridge.
#
# Usage:
#     export BRIDGE_API_KEY=...
#     ./test_stop_run.sh [BASE_URL] [RUN_ID]
#     # default RUN_ID = /tmp/bridge_last_run_id

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

echo "==> POST ${BASE}/runs/${RUN_ID}/stop"
RESP=$(curl -sS -w "\n%{http_code}" -X POST "${BASE}/runs/${RUN_ID}/stop" \
    -H "Authorization: Bearer ${BRIDGE_API_KEY}")
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

echo "$BODY" | python3 -m json.tool

# 200 (cancelled) or 404 (already gone) are both acceptable end states.
if [ "$CODE" != "200" ] && [ "$CODE" != "404" ]; then
    echo "FAIL: expected 200 or 404, got $CODE"
    exit 1
fi

echo "PASS"
