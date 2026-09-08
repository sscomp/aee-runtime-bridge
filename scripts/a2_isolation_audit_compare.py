#!/usr/bin/env python3
"""Compare notification_audit.jsonl after a pytest run against baseline.

Usage: compare_audit_after_tests.py <baseline_json> [<baseline_json> ...]

Reports rows added since the (latest) baseline and how many of the new
rows are sent:true, with per-task_id / per-method breakdown so the
work-order's own lifecycle notifications can be separated from test
leaks. Exit code 0 always (report-only tool).
"""
import json
import os
import sys
from collections import Counter

AUDIT = os.path.expanduser("~/aee-runtime-bridge/logs/notification_audit.jsonl")


def _parse_ts(value):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

if len(sys.argv) < 2:
    print("usage: compare_audit_after_tests.py <baseline_json> [...]")
    sys.exit(2)

baselines = []
for p in sys.argv[1:]:
    with open(p, "r", encoding="utf-8") as f:
        baselines.append(json.load(f))
ref = min(baselines, key=lambda b: b["ts_epoch"])
since_epoch = ref["ts_epoch"]

rows = []
with open(AUDIT, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

new = []
for r in rows:
    ts = _parse_ts(r.get("ts_utc"))
    if ts is not None and ts > since_epoch:
        new.append(r)

sent_true = [r for r in new if r.get("sent") is True]
report = {
    "baseline_ts_utc": ref["ts_utc"],
    "baseline_rows": ref["rows"],
    "baseline_sent_true_rows": ref["sent_true_rows"],
    "audit_rows_now": len(rows),
    "new_rows": len(new),
    "new_sent_true": len(sent_true),
    "new_sent_true_by_task": dict(Counter(r.get("task_id") for r in sent_true)),
    "new_sent_true_by_method": dict(Counter(r.get("method") for r in sent_true)),
    "new_rows_by_task": dict(Counter(r.get("task_id") for r in new)),
}
print(json.dumps(report, indent=2))

out = "/tmp/a2_isolation_compare_last.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)
print(f"written: {out}")
sys.exit(0)