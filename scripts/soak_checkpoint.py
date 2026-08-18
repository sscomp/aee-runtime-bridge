#!/usr/bin/env python3
"""7-Day Soak Certification Daily Checkpoint Script.

Non-mutating, evidence-first verification. Reads-only; no source edits, commits, pushes, or restarts.
Produces a daily checkpoint report and anomaly log entry.

Usage: python3 soak_checkpoint.py --day N
"""

import subprocess
import os
import sys
import json
import hashlib
import sqlite3
import datetime
import urllib.request

REPO = "/home/ubuntu/hermes-runtime-bridge"
DB = os.path.join(REPO, "data", "dispatcher.db")
REPORTS_DIR = os.path.join(REPO, "reports")
BASELINE_HEAD = "e1fc46b4af3b25870c85b267fc027094ec483348"

PROTECTED_FILES = {
    "app.py": "bebe83c48a163bfdf18015cc5df22b585986da9c90b62379400a1e31960834e0",
    "dispatcher/__init__.py": "93f9928b784625553b158fd50a8f029eed58a09ecd19e8b7e7ac1b0e197861e2",
    "dispatcher/db.py": "46561e6b435da1df477529e43376018b3bcf22244bb97b389cd6eaa869566a00",
    "dispatcher/executor_runs.py": "f5278c8692e25a5706a98888c795464210b5743b56a9a2d22c99c41db92ceaa0",
    "dispatcher/executor_watcher.py": "581a9021a2919ccb741a3087f13f7f72cf429402fea6df954f4ca7f94e5fbad0",
    "dispatcher/manager.py": "d58794617cbaed329bfb9e6a7cc7d94c20d2dc1c1c677ce05846cb17cb9ae877",
    "dispatcher/models.py": "2413d9f80f31d5d28d5b886beab05cca9c6c45eb5fd9da98c95912fdc4c0ed6f",
    "dispatcher/notifier.py": "cd9ffaac60ab72a0088c618035effdfdab1bd59e2d294aa0835f1bb7d758032d",
    "dispatcher/progress.py": "2abd938c8274eb0191910ecbaedb7c1c6580676ae0f64db5080993346185fe8a",
    "dispatcher/reaper.py": "cdec7aa29eab551ec6a1cf500139ba446c8681f8f669fb146d0a92815bfe3416",
    "dispatcher/safety.py": "10b684ef65f231ec31195c5daca7674e9099c47383578d3751d33c6b126310e2",
}

BASELINE_COUNTS = {
    "tasks_total": 193,
    "tasks_completed": 184,
    "tasks_failed": 4,
    "tasks_timeout": 3,
    "tasks_cancelled": 1,
    "tasks_running": 1,
    "executor_runs_total": 209,
    "executor_runs_completed": 190,
    "executor_runs_cancelled": 13,
    "executor_runs_failed": 3,
    "executor_runs_timeout": 2,
    "executor_runs_running": 1,
    "task_outputs_total": 192,
    "task_outputs_delivery_json": 113,
    "notification_completed_events": 188,
    "notification_failed_events": 4,
    "delivery_unverified_events": 8,
}


def run_git(args):
    cmd = ["git", "-C", REPO] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def http_get(url, timeout=5):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.88.1"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode())
    except Exception as e:
        return None, str(e)


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _parse_day(argv):
    """Parse --day N or --day=N from argv. Returns int >= 1, defaults to 1."""
    for i, arg in enumerate(argv):
        if arg == "--day" and i + 1 < len(argv):
            try:
                return max(1, int(argv[i + 1]))
            except ValueError:
                return 1
        if arg.startswith("--day="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                return 1
    return 1


def main():
    day = _parse_day(sys.argv[1:])
    is_final = (day == 7)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_tpe = now_utc + datetime.timedelta(hours=8)

    results = {"checks": [], "anomalies": [], "failures": []}

    # Check 1: HEAD unchanged
    head, _, _ = run_git(["rev-parse", "HEAD"])
    head_ok = (head == BASELINE_HEAD)
    results["checks"].append({"dim": 1, "name": "HEAD unchanged", "pass": head_ok, "actual": head, "expected": BASELINE_HEAD})
    if not head_ok:
        results["failures"].append("F-2: HEAD changed")

    # Check 2: Protected hashes
    hash_failures = []
    for fname, expected_hash in PROTECTED_FILES.items():
        fp = os.path.join(REPO, fname)
        if os.path.isfile(fp):
            actual = sha256_file(fp)
            if actual != expected_hash:
                hash_failures.append(f"{fname}: expected {expected_hash[:16]}... got {actual[:16]}...")
        else:
            hash_failures.append(f"{fname}: FILE MISSING")
    hash_ok = len(hash_failures) == 0
    results["checks"].append({"dim": 2, "name": "Protected hashes", "pass": hash_ok, "failures": hash_failures})
    if not hash_ok:
        results["failures"].append("F-1: Protected file hash mismatch")

    # Check 3: Bridge health
    status, health = http_get("http://localhost:8787/health")
    bridge_ok = (status == 200 and isinstance(health, dict) and health.get("status") == "ok")
    results["checks"].append({"dim": 3, "name": "Bridge health", "pass": bridge_ok, "status": status, "bridge_status": health.get("status") if isinstance(health, dict) else str(health)[:200]})

    # Check 3b: Supervisord
    sup = subprocess.run(["supervisorctl", "--serverurl=unix:///tmp/supervisor.sock", "status"], capture_output=True, text=True)
    sup_lines = [l for l in sup.stdout.strip().split("\n") if l.strip()]
    all_running = all("RUNNING" in l for l in sup_lines)
    results["checks"].append({"dim": 3, "name": "Supervisord services", "pass": all_running, "services": len(sup_lines), "all_running": all_running})
    if not all_running:
        stopped = [l for l in sup_lines if "RUNNING" not in l]
        results["anomalies"].append(f"Services not RUNNING: {stopped}")

    # Check 4: Task/run counts
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    task_counts = {}
    for row in conn.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"):
        task_counts[row["status"]] = row["cnt"]
    exec_counts = {}
    for row in conn.execute("SELECT status, COUNT(*) as cnt FROM executor_runs GROUP BY status"):
        exec_counts[row["status"]] = row["cnt"]
    results["checks"].append({"dim": 4, "name": "Task/run counts", "pass": True, "task_counts": task_counts, "exec_counts": exec_counts})

    # Check 5: Failed/timeout/cancelled deltas
    current_failed = task_counts.get("failed", 0)
    current_timeout = task_counts.get("timeout", 0)
    current_cancelled = task_counts.get("cancelled", 0)
    baseline_failed = BASELINE_COUNTS["tasks_failed"]
    baseline_timeout = BASELINE_COUNTS["tasks_timeout"]
    baseline_cancelled = BASELINE_COUNTS["tasks_cancelled"]
    delta_failed = current_failed - baseline_failed
    delta_timeout = current_timeout - baseline_timeout
    delta_cancelled = current_cancelled - baseline_cancelled
    spike = (delta_failed > 3 or delta_timeout > 3)
    results["checks"].append({"dim": 5, "name": "Failed/timeout deltas", "pass": not spike, "delta_failed": delta_failed, "delta_timeout": delta_timeout, "delta_cancelled": delta_cancelled})
    if spike:
        results["anomalies"].append(f"F-6: Unexplained spike: failed delta={delta_failed}, timeout delta={delta_timeout}")

    # Check 6: Stale/orphan runs
    stale = conn.execute("""
        SELECT er.run_id, er.task_id, er.status
        FROM executor_runs er
        WHERE er.status = 'running'
        AND er.task_id NOT IN (SELECT task_id FROM tasks WHERE status = 'running')
    """).fetchall()
    stale_count = len(stale)
    results["checks"].append({"dim": 6, "name": "Stale/orphan runs", "pass": stale_count == 0, "stale_count": stale_count})
    if stale_count > 0:
        results["anomalies"].append(f"F-7: {stale_count} stale/orphan executor runs")

    # Check 7: Heartbeat/reaper health
    reaper_info = health.get("reaper", {}) if isinstance(health, dict) else {}
    reaper_ok = reaper_info.get("would_reap", 1) == 0
    results["checks"].append({"dim": 7, "name": "Reaper health", "pass": reaper_ok, "reaper": reaper_info})

    # Check 8: Artifact registration
    art = conn.execute("SELECT COUNT(*) as total, SUM(CASE WHEN delivery_json IS NOT NULL AND delivery_json != '' THEN 1 ELSE 0 END) as has_delivery FROM task_outputs").fetchone()
    delivery_count = art["has_delivery"] or 0
    delivery_ok = delivery_count >= BASELINE_COUNTS["task_outputs_delivery_json"]
    results["checks"].append({"dim": 8, "name": "Artifact registration", "pass": delivery_ok, "delivery_json_count": delivery_count, "baseline": BASELINE_COUNTS["task_outputs_delivery_json"]})

    # Check 9: Notifier duplicate-send anomalies
    dupes = conn.execute("""
        SELECT task_id, COUNT(*) as cnt
        FROM task_events
        WHERE kind = 'notification_completed'
        GROUP BY task_id
        HAVING cnt > 1
    """).fetchall()
    dupe_count = len(dupes)
    results["checks"].append({"dim": 9, "name": "Notifier duplicates", "pass": dupe_count == 0, "duplicate_tasks": dupe_count})
    if dupe_count > 0:
        results["failures"].append("F-8: New notifier duplicate-send anomaly")

    # Check 10: Executor health
    claude_ver = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    claude_ok = claude_ver.returncode == 0
    hermes_reachable = bridge_ok and health.get("hermes") == "reachable" if isinstance(health, dict) else False
    exec_ok = claude_ok and hermes_reachable
    results["checks"].append({"dim": 10, "name": "Executor health (Hermes+Claude CLI)", "pass": exec_ok, "claude_version": claude_ver.stdout.strip(), "hermes_reachable": hermes_reachable})

    conn.close()

    # Overall verdict
    all_pass = all(c["pass"] for c in results["checks"])
    has_failure = len(results["failures"]) > 0
    verdict = "PASS" if all_pass and not has_failure else ("FAIL" if has_failure else "CONDITIONAL")

    # Write daily checkpoint report
    report_path = os.path.join(REPORTS_DIR, f"7_day_soak_day{day}_checkpoint.md")
    with open(report_path, "w") as f:
        f.write(f"# 7-Day Soak Certification — Day {day} Checkpoint\n\n")
        f.write(f"| Field | Value |\n|-------|-------|\n")
        f.write(f"| Day | {day}/7 |\n")
        f.write(f"| Timestamp (UTC) | {now_utc.isoformat()} |\n")
        f.write(f"| Timestamp (CST) | {now_tpe.isoformat()} |\n")
        f.write(f"| HEAD | {head} |\n")
        f.write(f"| Verdict | {verdict} |\n\n")
        f.write(f"## Check Results\n\n")
        for c in results["checks"]:
            status = "PASS" if c["pass"] else "FAIL"
            f.write(f"- [{status}] Dim {c['dim']}: {c['name']}")
            if "failures" in c and c["failures"]:
                f.write(f" — {c['failures']}")
            f.write("\n")
        if results["anomalies"]:
            f.write(f"\n## Anomalies\n\n")
            for a in results["anomalies"]:
                f.write(f"- {a}\n")
        if results["failures"]:
            f.write(f"\n## Failures (Invalidating)\n\n")
            for fl in results["failures"]:
                f.write(f"- {fl}\n")
        f.write(f"\n## Full JSON Evidence\n\n```json\n{json.dumps(results, indent=2, default=str)}\n```\n")

    # Append to anomaly log if anomalies found
    if results["anomalies"] or results["failures"]:
        log_path = os.path.join(REPORTS_DIR, "7_day_soak_anomaly_log.md")
        with open(log_path, "a") as f:
            f.write(f"\n## Day {day} — {now_utc.isoformat()}\n\n")
            for a in results["anomalies"]:
                f.write(f"- ANOMALY: {a}\n")
            for fl in results["failures"]:
                f.write(f"- FAILURE: {fl}\n")

    # Final report on Day 7
    final_path = None
    if is_final:
        final_path = os.path.join(REPORTS_DIR, "7_day_soak_certification_final.md")
        with open(final_path, "w") as f:
            f.write(f"# 7-Day Soak Certification — FINAL REPORT\n\n")
            f.write(f"| Field | Value |\n|-------|-------|\n")
            f.write(f"| Certification ID | SOAK-2026-08-09 |\n")
            f.write(f"| Window | 2026-08-09 14:47:34 UTC — 2026-08-16 14:47:34 UTC |\n")
            f.write(f"| Baseline Commit | e1fc46b4af3b25870c85b267fc027094ec483348 |\n")
            f.write(f"| Final Verdict | {verdict} |\n")
            f.write(f"| Final HEAD | {head} |\n\n")
            f.write(f"## Day 7 Final Checkpoint\n\n")
            for c in results["checks"]:
                status = "PASS" if c["pass"] else "FAIL"
                f.write(f"- [{status}] Dim {c['dim']}: {c['name']}\n")
            f.write(f"\n## Cumulative Analysis\n\n")
            f.write(f"Review all daily checkpoint reports (Day 1-7) and anomaly log for full analysis.\n")
            f.write(f"\n## Certification Statement\n\n")
            if verdict == "PASS":
                f.write(f"The Hermes Runtime Bridge at commit e1fc46b is certified as stable for 7-day continuous operation.\n")
            elif verdict == "CONDITIONAL":
                f.write(f"Certification conditional — non-invalidating warnings present. Review anomalies before production reliance.\n")
            else:
                f.write(f"Certification FAILED — invalidating condition triggered. Diagnose, fix, re-commit, and restart new window.\n")

    print(f"Day {day} checkpoint complete: {verdict}")
    print(f"Report: {report_path}")
    if is_final:
        print(f"Final report: {final_path}")

    # Exit code for cron monitoring
    sys.exit(0 if verdict != "FAIL" else 1)


if __name__ == "__main__":
    main()