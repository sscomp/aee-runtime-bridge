"""Phase 2 end-to-end acceptance: 5 tests."""
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta

ROOT = "/home/ubuntu/hermes-runtime-bridge"
os.chdir(ROOT)

# Auth: pull key from os.environ set by the calling shell, with
# fallback to a hardcoded mask (caller MUST set the env var).
KEY = os.environ.get("PHASE2_TEST_KEY", "")
if not KEY:
    print("ERROR: set PHASE2_TEST_KEY=<bridge API key> in env before running", file=__import__("sys").stderr)
    raise SystemExit(1)
AUTH = "Authorization: Bearer " + KEY


def curl(method, path, body=None):
    cmd = ["curl", "-sS", "-w", "\n__HTTP__:%{http_code}", "-X", method,
           "-H", AUTH, "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    cmd += ["http://127.0.0.1:8787" + path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = r.stdout
    http_code = 0
    body_text = out
    if "__HTTP__:" in out:
        body_text, http_str = out.rsplit("__HTTP__:", 1)
        try:
            http_code = int(http_str.strip())
        except ValueError:
            pass
    try:
        return http_code, json.loads(body_text) if body_text else {}
    except Exception:
        return http_code, body_text


print("=== Phase 2 End-to-End Acceptance ===\n")

print("## Test 1: P3 blocklist - 'rm -rf /' should be rejected (400)")
rc, resp = curl("POST", "/runs", {"input": "please rm -rf /", "type": "normal"})
print("  HTTP", rc, "body:", json.dumps(resp)[:300])
assert rc == 400
assert resp.get("detail", {}).get("code") == "dangerous_input"
print("  PASS\n")

print("## Test 2: P3 approval gate - 'sudo apt install' should pass with requires_review=True")
rc, resp = curl("POST", "/runs", {"input": "sudo apt install nginx", "type": "normal"})
print("  HTTP", rc, "status:", resp.get("status"), "requires_review:", resp.get("requires_review"))
print("  safety:", resp.get("safety"))
assert rc == 200
assert resp.get("requires_review") is True
assert resp.get("safety", {}).get("action") == "require_approval"
print("  PASS\n")

print("## Test 3: P1 reaper - aged queued task should be reaped")
rc, create = curl("POST", "/runs", {"input": "test reaper", "type": "normal"})
tid = create["task_id"]
print("  created:", tid)
# Stop the watcher from racing us: clear hermes_run_id + rewind.
db = sqlite3.connect(os.path.join(ROOT, "data/dispatcher.db"))
old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
db.execute("UPDATE tasks SET created_at=?, hermes_run_id=NULL, status='queued', started_at=NULL WHERE task_id=?", (old, tid))
db.commit()
db.close()
print("  rewound created_at by 10 min and cleared hermes_run_id, waiting 12s for reaper tick...")
time.sleep(12)
rc, fetched = curl("GET", "/tasks/" + tid)
print("  task status:", fetched.get("status"))
assert fetched.get("status") == "timeout", f"expected timeout, got {fetched.get('status')}"
print("  PASS\n")

print("## Test 4: P4 usage aggregation")
rc, usage = curl("GET", "/stats/usage?period=today")
print("  totals:", usage["totals"])
print("  by_type:", [d.get("type") for d in usage.get("by_type", [])])
print("  by_model:", [d.get("model") for d in usage.get("by_model", [])])
assert usage["totals"]["task_count"] >= 1
print("  PASS\n")

print("## Test 5: /health shows Phase 2 summary")
rc, h = curl("GET", "/health")
print("  version:", h.get("version"), "phase:", h.get("phase"))
print("  reaper:", h.get("reaper"))
print("  safety:", h.get("safety"))
print("  notifier:", h.get("notifier"))
assert h.get("reaper") is not None
assert h.get("safety") is not None
assert h.get("notifier") is not None
print("  PASS\n")

print("=== ALL PHASE 2 ACCEPTANCE TESTS PASSED ===")
