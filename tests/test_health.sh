#!/usr/bin/env bash
# test_health.sh — verify /health responds and that the bridge is configured.
#
# Usage:
#     export BRIDGE_API_KEY=...        # optional; /health is unauthenticated
#     ./test_health.sh [BASE_URL]
#     # default BASE_URL=http://127.0.0.1:8787

set -euo pipefail

BASE="${1:-http://127.0.0.1:8787}"

echo "==> GET ${BASE}/health"
RESP=$(curl -sS -w "\n%{http_code}" "${BASE}/health")
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

if [ "$CODE" != "200" ]; then
    echo "FAIL: expected 200, got $CODE"
    echo "$BODY"
    exit 1
fi

echo "$BODY" | python3 -m json.tool

HERMES=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin)['hermes'])")
if [ "$HERMES" != "reachable" ]; then
    echo "WARN: hermes reports '$HERMES' (not 'reachable'). The bridge is up but upstream is not."
    exit 2
fi

echo "PASS"
