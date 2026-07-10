#!/usr/bin/env python3
"""Final end-to-end smoke test of the bridge — pure Python so no shell metachars."""
import json
import os
import sys
import time
from urllib import error, request

BASE = "http://127.0.0.1:8787"
BRIDGE_API_KEY = next(
    l for l in open("/tmp/bridge_env.sh").read().splitlines()
    if l.startswith("BRIDGE_API_KEY=")
).split("=", 1)[1].strip()

PASSED = []
FAILED = []


def step(label):
    print(f"\n--- {label} ---")


def ok(label):
    PASSED.append(label)
    print(f"  PASS: {label}")


def fail(label, msg):
    FAILED.append((label, msg))
    print(f"  FAIL: {label}: {msg}")


def call(method, path, body=None, expect=200, auth=True):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if auth:
        headers["Authorization"] = f"Bearer {BRIDGE_API_KEY}"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300]}


# 1. /health (no auth)
step("1. /health no-auth")
code, body = call("GET", "/health", auth=False)
if code == 200 and body.get("hermes") == "reachable":
    ok("/health public + hermes reachable")
else:
    fail("/health", f"code={code} body={body}")

# 2. auth missing → 401
step("2. POST /runs without auth -> 401")
code, _ = call("POST", "/runs", body={"input": "x"}, auth=False)
if code == 401:
    ok("auth required on POST /runs")
else:
    fail("auth required", f"got {code}, expected 401")

# 3. dangerous input → 400
step("3. dangerous input -> 400")
code, body = call("POST", "/runs", body={"input": "rm -rf /etc"})
if code == 400 and body.get("detail", {}).get("code") == "dangerous_input":
    ok(f"danger blocklist catches 'rm -rf /etc' -> {body['detail']['matched_pattern']}")
else:
    fail("danger blocklist", f"code={code} body={body}")

# 4. actual create + poll
step("4. real Hermes run: 'pwd'")
code, body = call("POST", "/runs", body={
    "input": "請執行 pwd 並回傳結果。",
    "session_id": "dingde-orchestrator-smoke",
    "timeout_seconds": 90,
})
if code == 200 and body.get("run_id"):
    run_id = body["run_id"]
    ok(f"createRun -> run_id={run_id}")
else:
    fail("createRun", f"code={code} body={body}")
    sys.exit(1)

# 5. poll until done
step("5. poll until completed")
final = None
for i in range(18):  # 90s
    code, body = call("GET", f"/runs/{run_id}")
    if body.get("status") in ("completed", "failed", "cancelled"):
        final = body
        break
    time.sleep(5)
if final and final.get("status") == "completed":
    output = final.get("output", "")
    ok(f"run completed in ~{i*5}s, output={output!r}")
else:
    fail("poll", f"final state: {final}")

# 6. /summary
step("6. /summary friendly view")
code, body = call("GET", f"/runs/{run_id}/summary")
if code == 200 and body.get("status") == "completed":
    ok(f"summary status={body['status']} preview_len={len(str(body.get('output_preview') or ''))}")
else:
    fail("summary", f"code={code} body={body}")

# 7. stop on already-completed run — should be benign
step("7. stop on already-completed run (benign)")
code, body = call("POST", f"/runs/{run_id}/stop")
if code in (200, 404):
    ok(f"stop on completed: code={code} (acceptable)")
else:
    fail("stop", f"code={code} body={body}")

# 8. unknown run_id
step("8. unknown run_id -> 404")
code, _ = call("GET", "/runs/run_does_not_exist")
if code == 404:
    ok("unknown run_id -> 404")
else:
    fail("unknown run_id", f"got {code}")

print()
print("=" * 60)
print(f"PASSED: {len(PASSED)}")
for p in PASSED:
    print(f"  + {p}")
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    for label, msg in FAILED:
        print(f"  - {label}: {msg}")
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
