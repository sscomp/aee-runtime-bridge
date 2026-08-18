# 7-Day Soak Certification — Day 7 Checkpoint

| Field | Value |
|-------|-------|
| Day | 7/7 |
| Timestamp (UTC) | 2026-08-16T14:48:42.037176+00:00 |
| Timestamp (CST) | 2026-08-16T22:48:42.037176+00:00 |
| HEAD | e1fc46b4af3b25870c85b267fc027094ec483348 |
| Verdict | PASS |

## Check Results

- [PASS] Dim 1: HEAD unchanged
- [PASS] Dim 2: Protected hashes
- [PASS] Dim 3: Bridge health
- [PASS] Dim 3: Supervisord services
- [PASS] Dim 4: Task/run counts
- [PASS] Dim 5: Failed/timeout deltas
- [PASS] Dim 6: Stale/orphan runs
- [PASS] Dim 7: Reaper health
- [PASS] Dim 8: Artifact registration
- [PASS] Dim 9: Notifier duplicates
- [PASS] Dim 10: Executor health (Hermes+Claude CLI)

## Full JSON Evidence

```json
{
  "checks": [
    {
      "dim": 1,
      "name": "HEAD unchanged",
      "pass": true,
      "actual": "e1fc46b4af3b25870c85b267fc027094ec483348",
      "expected": "e1fc46b4af3b25870c85b267fc027094ec483348"
    },
    {
      "dim": 2,
      "name": "Protected hashes",
      "pass": true,
      "failures": []
    },
    {
      "dim": 3,
      "name": "Bridge health",
      "pass": true,
      "status": 200,
      "bridge_status": "ok"
    },
    {
      "dim": 3,
      "name": "Supervisord services",
      "pass": true,
      "services": 10,
      "all_running": true
    },
    {
      "dim": 4,
      "name": "Task/run counts",
      "pass": true,
      "task_counts": {
        "cancelled": 1,
        "completed": 217,
        "failed": 4,
        "timeout": 3
      },
      "exec_counts": {
        "cancelled": 13,
        "completed": 223,
        "failed": 3,
        "timeout": 2
      }
    },
    {
      "dim": 5,
      "name": "Failed/timeout deltas",
      "pass": true,
      "delta_failed": 0,
      "delta_timeout": 0,
      "delta_cancelled": 0
    },
    {
      "dim": 6,
      "name": "Stale/orphan runs",
      "pass": true,
      "stale_count": 0
    },
    {
      "dim": 7,
      "name": "Reaper health",
      "pass": true,
      "reaper": {
        "running": 0,
        "queued": 0,
        "waiting": 0,
        "would_reap": 0
      }
    },
    {
      "dim": 8,
      "name": "Artifact registration",
      "pass": true,
      "delivery_json_count": 142,
      "baseline": 113
    },
    {
      "dim": 9,
      "name": "Notifier duplicates",
      "pass": true,
      "duplicate_tasks": 0
    },
    {
      "dim": 10,
      "name": "Executor health (Hermes+Claude CLI)",
      "pass": true,
      "claude_version": "2.1.227 (Claude Code)",
      "hermes_reachable": true
    }
  ],
  "anomalies": [],
  "failures": []
}
```
