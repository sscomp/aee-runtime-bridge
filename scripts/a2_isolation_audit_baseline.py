#!/usr/bin/env python3
"""Record notification_audit.jsonl baseline stats (rows, sent:true, size)."""
import json
import os
import sys
import time

AUDIT = os.path.expanduser("~/aee-runtime-bridge/logs/notification_audit.jsonl")
OUT = "/tmp/a2_isolation_baseline.json"

rows = sent_true = 0
size = 0
last_ts = None
if os.path.exists(AUDIT):
    size = os.path.getsize(AUDIT)
    with open(AUDIT, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("sent") is True:
                sent_true += 1
                last_ts = rec.get("ts_utc")

snapshot = {
    "ts_epoch": time.time(),
    "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "audit_path": AUDIT,
    "size_bytes": size,
    "rows": rows,
    "sent_true_rows": sent_true,
    "last_sent_true_ts": last_ts,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(snapshot, f, indent=2)
print(json.dumps(snapshot, indent=2))
sys.exit(0)