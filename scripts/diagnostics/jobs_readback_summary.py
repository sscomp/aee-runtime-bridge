#!/usr/bin/env python3
"""Read-only JobStore inventory + seal verification; writes nothing.

P3 Gate B1 replacement for the root-run readback tool that instantiated
JobStore (whose construction mkdir/chmods the store root to 0700 and creates
registry/active lock files) - the confirmed cause of the 0750 -> 0700
/run/aee-broker/jobs drift. This version never instantiates JobStore: every
record is read through read_job_record() (fd-based O_NOFOLLOW, full
production record contract, SHA-256 integrity proof), so a 0750 broker jobs
directory stays 0750 and no lock files appear.

Prints ONLY: file names, status, error_code, byte size and the integrity
verdict. Never prints task/summary/log/error payload content.

Usage: jobs_readback_summary.py [JOBSTORE_DIR ...]
       (default: /var/lib/aee/jobs /run/aee-broker/jobs)
       e.g. jobs_readback_summary.py /root/aee-backup/var-lib-aee-20261012T033428Z/jobs
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")  # never write __pycache__ into any release tree
from aee.mcp_runtime.store import JobError, read_job_record

ACTIVE = {"queued", "running"}                                   # baseline store.py:25
TERMINAL = {"completed", "failed", "timed_out", "cancelled"}     # baseline store.py:26


def _safe_size(root, name):
    """Record byte size for the report line; -1 when the size is unreadable."""
    try:
        return os.path.getsize(Path(root) / name)
    except OSError:
        return -1


denied = []
totals = {}
roots = sys.argv[1:] if len(sys.argv) > 1 else ["/var/lib/aee/jobs", "/run/aee-broker/jobs"]
for root_str in roots:
    root = Path(root_str)
    print("==", root_str, flush=True)
    try:
        if root.is_symlink():
            print("   (store root is a symlink; refused)")
            continue
        if not root.is_dir():
            print("   (directory missing)")
            continue
        names = sorted(p.name for p in root.iterdir())
    except PermissionError:
        print("   (permission denied)", flush=True)
        denied.append(root_str)
        continue
    non_records = [n for n in names if not n.endswith(".json")]
    records = [n for n in names if n.endswith(".json")]
    if not root_str.endswith("jobs") or len(non_records):
        print(f"   [non-record entries] {non_records}")
    counts = {"active": 0, "terminal": 0, "corrupt": 0, "other": 0}
    for name in records:
        job_id = name[:-len(".json")]
        try:
            d = read_job_record(root, job_id)   # production read contract, zero side effects
        except JobError as e:
            counts["corrupt"] += 1
            print(f"   {name}: READER_REJECT code={e.code} bytes={_safe_size(root, name)}")
            continue
        except Exception as e:          # unexpected reader crash
            counts["corrupt"] += 1
            print(f"   {name}: READER_ERROR type={type(e).__name__}")
            continue
        status = d.get("status")
        size = _safe_size(root, name)
        if status in TERMINAL:
            counts["terminal"] += 1
        elif status in ACTIVE:
            counts["active"] += 1
            print(f"   ACTIVE RECORD: {name} status={status} bytes={size}")
        else:
            counts["other"] += 1
        # status/error_code/seal verdict only - never record content
        print(f"   {name}: status={status} error_code={d.get('error_code')} "
              f"seal=ok bytes={size}")
    print(f"   summary {root_str}: {counts}")
    totals[root_str] = counts
print("== totals:", totals)
if denied:
    sys.exit(1)
