# 7-Day AEE/Hermes Runtime Soak & Stability Certification — START

| Field | Value |
|-------|-------|
| Document Type | Certification Window Start (Baseline Capture) |
| Certification ID | SOAK-2026-08-09 |
| Repository | `/home/ubuntu/hermes-runtime-bridge` |
| Branch | `main` |
| Baseline Commit | `e1fc46b4af3b25870c85b267fc027094ec483348` |
| Commit Short | `e1fc46b` |
| Commit Subject | `fix(bridge): periodic stale-run reconciliation in executor watcher` |
| Bridge Version | 1.2.0 |
| Bridge Phase | 2 — Reaper + Notifier + Safety upgrade |

## Timestamps

| Marker | UTC | Asia/Taipei (CST) |
|--------|-----|-------------------|
| Window Start | 2026-08-09 14:47:34 UTC | 2026-08-09 22:47:34 CST |
| Day 1 Checkpoint | 2026-08-10 14:47:34 UTC | 2026-08-10 22:47:34 CST |
| Day 2 Checkpoint | 2026-08-11 14:47:34 UTC | 2026-08-11 22:47:34 CST |
| Day 3 Checkpoint | 2026-08-12 14:47:34 UTC | 2026-08-12 22:47:34 CST |
| Day 4 Checkpoint | 2026-08-13 14:47:34 UTC | 2026-08-13 22:47:34 CST |
| Day 5 Checkpoint | 2026-08-14 14:47:34 UTC | 2026-08-14 22:47:34 CST |
| Day 6 Checkpoint | 2026-08-15 14:47:34 UTC | 2026-08-15 22:47:34 CST |
| Day 7 Checkpoint (Final) | 2026-08-16 14:47:34 UTC | 2026-08-16 22:47:34 CST |
| Window End | 2026-08-16 14:47:34 UTC | 2026-08-16 22:47:34 CST |

## 1. Repository State at Baseline

### 1.1 Branch & HEAD

```
Branch: main
HEAD: e1fc46b4af3b25870c85b267fc027094ec483348
HEAD (short): e1fc46b
```

### 1.2 Dirty Tracked Changes

**NONE** — Working tree has zero dirty tracked files. All 24 untracked items are report artifacts under `reports/` and one manifest JSON, none affecting protected/runtime source.

### 1.3 Recent Commits

```
e1fc46b fix(bridge): periodic stale-run reconciliation in executor watcher
72f4b39 docs(reports): add refreshed authoritative soak/OpenAPI Class B reports
ca7fc37 feat(deps): add uv pip compile lockfile system with constraints, hashes, and verification scripts
```

### 1.4 Suitability Assessment

The repository is in a **clean, suitable state** for certification:

- Zero dirty tracked changes on protected/runtime files
- HEAD is a stable committed point on `main`
- No uncommitted source modifications
- No stash entries referencing protected files
- All untracked items are report artifacts (non-source)

**Verdict: SAFE TO START CERTIFICATION**

## 2. Protected-File Baseline (Immutable Set)

The following files are declared as the frozen immutable baseline for this certification window. Any SHA-256 change to these files during the 7-day window invalidates certification and requires restarting.

| # | File Path | SHA-256 |
|---|-----------|---------|
| 1 | `app.py` | `bebe83c48a163bfdf18015cc5df22b585986da9c90b62379400a1e31960834e0` |
| 2 | `dispatcher/__init__.py` | `93f9928b784625553b158fd50a8f029eed58a09ecd19e8b7e7ac1b0e197861e2` |
| 3 | `dispatcher/db.py` | `46561e6b435da1df477529e43376018b3bcf22244bb97b389cd6eaa869566a00` |
| 4 | `dispatcher/executor_runs.py` | `f5278c8692e25a5706a98888c795464210b5743b56a9a2d22c99c41db92ceaa0` |
| 5 | `dispatcher/executor_watcher.py` | `581a9021a2919ccb741a3087f13f7f72cf429402fea6df954f4ca7f94e5fbad0` |
| 6 | `dispatcher/manager.py` | `d58794617cbaed329bfb9e6a7cc7d94c20d2dc1c1c677ce05846cb17cb9ae877` |
| 7 | `dispatcher/models.py` | `2413d9f80f31d5d28d5b886beab05cca9c6c45eb5fd9da98c95912fdc4c0ed6f` |
| 8 | `dispatcher/notifier.py` | `cd9ffaac60ab72a0088c618035effdfdab1bd59e2d294aa0835f1bb7d758032d` |
| 9 | `dispatcher/progress.py` | `2abd938c8274eb0191910ecbaedb7c1c6580676ae0f64db5080993346185fe8a` |
| 10 | `dispatcher/reaper.py` | `cdec7aa29eab551ec6a1cf500139ba446c8681f8f669fb146d0a92815bfe3416` |
| 11 | `dispatcher/safety.py` | `10b684ef65f231ec31195c5daca7674e9099c47383578d3751d33c6b126310e2` |

**Total protected files: 11**

Rationale: These files constitute the complete runtime/lifecycle surface of the Hermes Runtime Bridge dispatcher — API server, task manager, database layer, executor lifecycle (runs + watcher), notification, progress tracking, reaper, safety gate, and data models. Any change to these files during the soak window represents a mutation of the certified runtime surface.

## 3. Baseline Health Snapshot

### 3.1 Bridge Health (HTTP GET /health)

```json
{
  "status": "ok",
  "service": "hermes-runtime-bridge",
  "version": "1.2.0",
  "phase": "2 — Reaper + Notifier + Safety upgrade",
  "hermes": "reachable",
  "hermes_base_url": "http://127.0.0.1:8642",
  "dispatcher": {
    "tasks_total": 193,
    "by_status": {
      "running": 1,
      "completed": 184,
      "timeout": 3,
      "failed": 4,
      "cancelled": 1
    }
  },
  "reaper": {
    "running": 1,
    "queued": 0,
    "waiting": 0,
    "would_reap": 0
  },
  "safety": {
    "mode": "blocklist_plus_allowlist",
    "allowlist_size": 42,
    "blocklist_size": 18,
    "approval_size": 8,
    "log_rejected": true
  },
  "notifier": {
    "enabled": false,
    "notify_on": ["failed", "timeout", "completed", "cancelled"],
    "rate_limit_per_hour": 20,
    "bot_token_present": false,
    "chat_id_present": true
  }
}
```

### 3.2 Supervisord Service Status

| Service | State | PID | Uptime |
|---------|-------|-----|--------|
| api-server | RUNNING | 478 | 5 days, 8:45:39 |
| cloudflared-tunnel | RUNNING | 190855 | 3 days, 22:06:31 |
| gateway-memory-watchdog | RUNNING | 479 | 5 days, 8:45:39 |
| hermes-runtime-bridge | RUNNING | 703319 | 3:00:48 |
| openclaw-gateway | RUNNING | 477 | 5 days, 8:45:39 |
| proxy-server | RUNNING | 481 | 5 days, 8:45:39 |
| syncthing | RUNNING | 1936 | 5 days, 8:39:58 |
| terminal-server | RUNNING | 485 | 5 days, 8:45:39 |
| ws-chat-server | RUNNING | 487 | 5 days, 8:45:39 |

All 9 services RUNNING. No STOPPED/EXITED/FATAL services.

### 3.3 OpenAPI Health

- `/openapi-chatgpt.json`: HTTP 200 (ChatGPT-compatible schema endpoint active)

## 4. Task & Run Lifecycle Baseline

### 4.1 Task Counts by Status

| Status | Count |
|--------|-------|
| completed | 184 |
| failed | 4 |
| timeout | 3 |
| cancelled | 1 |
| running | 1 |
| **Total** | **193** |

### 4.2 Executor Run Counts by Status

| Status | Count |
|--------|-------|
| completed | 190 |
| cancelled | 13 |
| failed | 3 |
| timeout | 2 |
| running | 1 |
| **Total** | **209** |

### 4.3 Task Events Summary (Top 15)

| Event Kind | Count |
|------------|-------|
| log | 720 |
| progress | 315 |
| queued | 193 |
| created | 193 |
| started | 192 |
| notification_completed | 188 |
| completed | 184 |
| delivery_unverified | 8 |
| status | 6 |
| notification_failed | 4 |
| failed | 4 |
| timeout | 3 |
| cancelled | 1 |

## 5. Active Executor Inventory

| Field | Value |
|-------|-------|
| Active running executor_runs | 1 |
| run_id | `run_eb5a166cb0bd4c8b8738a575366a361f` |
| task_id | `TASK-20260809-0028` |
| requested_executor | None (default routing) |
| selected_executor | hermes |
| runtime_type | hermes |
| adapter | hermes |
| phase | running |
| progress | 0.0 |
| last_heartbeat_at | 2026-08-09T14:46:47Z |

## 6. Stale/Orphan Run Closure State

| Check | Result |
|-------|--------|
| Stale/orphan executor_runs (running, task not running) | NONE |
| Tasks with multiple notification_completed | NONE |
| Tasks with multiple notification_failed | NONE |
| `reconcile_stale_runs()` last invoked | Bridge startup (3:00:48 ago via `init_executor_runs`) |
| Periodic reconciliation hook | ACTIVE (every ~30 min via `executor_watcher._tick` at tick-count 360) |

### 6.1 Claude Code CLI Stale-Run Closure

| Check | Result |
|-------|--------|
| Claude Code CLI path | `/home/ubuntu/.local/bin/claude` |
| Claude Code CLI version | 2.1.221 |
| Executor runs with NULL task_id (orphan CLI runs) | 12 (all have `telegram_result_json: {}`, expected for pre-task CLI-only runs) |
| Executor runs with duplicate telegram_result per task_id | 8 task_ids with 2 telegram_result entries each (pre-existing, all from early Aug 2026 — investigation shows these are re-dispatch runs, not duplicate-send anomalies) |
| Notification duplicate-send anomalies | NONE detected (zero tasks with >1 `notification_completed` event) |

## 7. Notifier & Artifact Registration Health

### 7.1 Notifier

| Field | Value |
|-------|-------|
| notifier.enabled | false (bridge-level notifier disabled; `hermes send` used per-task) |
| notify_on | failed, timeout, completed, cancelled |
| rate_limit_per_hour | 20 |
| bot_token_present | false |
| chat_id_present | true |
| notification_completed events | 188 |
| notification_failed events | 4 |
| Tasks with duplicate notification_completed | 0 (NONE) |
| Tasks with duplicate notification_failed | 0 (NONE) |

### 7.2 Artifact Registration

| Field | Value |
|-------|-------|
| Total task_outputs rows | 192 |
| task_outputs with delivery_json | 113 |
| task_outputs with notification_json | 192 |
| delivery_unverified events | 8 (pre-existing, from tasks where agent declared intent but did not write file) |

### 7.3 Dispatcher DB

| Field | Value |
|-------|-------|
| Path | `/home/ubuntu/hermes-runtime-bridge/data/dispatcher.db` |
| Size | 2,301,952 bytes (2.2 MB) |
| Last modified | 2026-08-09T14:44:40Z |

## 8. Executor Health Assessment

### 8.1 Hermes Runtime Executor

| Check | Result |
|-------|--------|
| Hermes gateway reachable | YES (health: `"hermes": "reachable"`) |
| Hermes base URL | `http://127.0.0.1:8642` |
| Hermes CLI path | `/home/ubuntu/.local/bin/hermes` |
| Active hermes executor runs | 1 (TASK-20260809-0028) |
| Last heartbeat | 2026-08-09T14:46:47Z (recent) |

### 8.2 Claude Code CLI Executor

| Check | Result |
|-------|--------|
| CLI installed | YES (`/home/ubuntu/.local/bin/claude`) |
| CLI version | 2.1.221 (Claude Code) |
| Executor runs using claude-cli | Present in executor_runs table |
| Stale closure mechanism | `reconcile_stale_runs()` + periodic hook in `executor_watcher._tick` |
| Known stale runs | 0 active stale (all orphan CLI runs from early Aug already cancelled) |

### 8.3 Codex CLI Executor

**NOT ASSESSED** — per operator directive, Codex CLI is not assessed as deployed for this certification.

## 9. Heartbeat & Reaper Health

### 9.1 Reaper

| Check | Result |
|-------|--------|
| Reaper running | 1 |
| Reaper queued | 0 |
| Reaper waiting | 0 |
| would_reap | 0 |
| Reaper-eligible tasks (failed/timeout with finished_at) | 7 (4 failed + 3 timeout) |

### 9.2 Heartbeat

| Check | Result |
|-------|--------|
| Running tasks with heartbeat_at | 0 of 1 (the 1 running task has NULL heartbeat — this is the currently executing task dispatched moments ago; heartbeat is set on first progress event) |

## 10. 7-Day Certification Plan

### 10.1 Certification Window

- **Start**: 2026-08-09 14:47:34 UTC (2026-08-09 22:47:34 CST)
- **End**: 2026-08-16 14:47:34 UTC (2026-08-16 22:47:34 CST)
- **Duration**: 7 days (168 hours)

### 10.2 Daily Checkpoint Schedule

Each daily checkpoint runs as a non-mutating, evidence-first verification via the repository's existing safe scheduler mechanism (Hermes cronjob). Checkpoints run at approximately the same time each day (±30 min window).

| Day | Checkpoint Time (UTC) | Checkpoint Time (CST) | Cron Schedule |
|-----|----------------------|----------------------|---------------|
| Day 1 | 2026-08-10 ~14:47 | 2026-08-10 ~22:47 | `47 14 10 8 *` (one-shot) |
| Day 2 | 2026-08-11 ~14:47 | 2026-08-11 ~22:47 | `47 14 11 8 *` (one-shot) |
| Day 3 | 2026-08-12 ~14:47 | 2026-08-12 ~22:47 | `47 14 12 8 *` (one-shot) |
| Day 4 | 2026-08-13 ~14:47 | 2026-08-13 ~22:47 | `47 14 13 8 *` (one-shot) |
| Day 5 | 2026-08-14 ~14:47 | 2026-08-14 ~22:47 | `47 14 14 8 *` (one-shot) |
| Day 6 | 2026-08-15 ~14:47 | 2026-08-15 ~22:47 | `47 14 15 8 *` (one-shot) |
| Day 7 | 2026-08-16 ~14:47 | 2026-08-16 ~22:47 | `47 14 16 8 *` (one-shot) — **FINAL REPORT** |

### 10.3 Daily Checkpoint Verification Matrix

Each daily checkpoint verifies the following 10 dimensions:

| # | Dimension | Method | Pass Criteria |
|---|-----------|--------|---------------|
| 1 | HEAD unchanged | `git -C /home/ubuntu/hermes-runtime-bridge rev-parse HEAD` | Equals `e1fc46b4af3b25870c85b267fc027094ec483348` |
| 2 | Protected hashes unchanged | `sha256sum` all 11 protected files | Each hash matches Section 2 baseline table |
| 3 | Bridge/service health | `curl http://localhost:8787/health` + `supervisorctl status` | status=ok, all services RUNNING |
| 4 | Task/run lifecycle counts | SQLite query on dispatcher.db | Record counts; flag any unexpected delta |
| 5 | Failed/timeout/cancelled deltas | SQLite count by status | Delta from previous day recorded; no unexplained spike |
| 6 | Stale/orphan runs | SQLite: executor_runs running with no matching task running | Count = 0 or all explained |
| 7 | Heartbeat/reaper health | Bridge /health reaper block + task heartbeat_at | Reaper active, no runaway queue |
| 8 | Artifact registration | SQLite: task_outputs with delivery_json | Count stable or growing (no regression) |
| 9 | Notifier duplicate-send anomalies | SQLite: tasks with >1 notification_completed event | Count = 0 (no new duplicates) |
| 10 | Executor health (Hermes + Claude Code CLI) | Bridge /health + `claude --version` + active run check | Both executors responsive; Codex CLI not assessed |

### 10.4 Final Report Trigger (Day 7)

The Day 7 checkpoint produces the final certification report:

- Artifact: `reports/7_day_soak_certification_final.md`
- Content: 7-day cumulative analysis, pass/fail per dimension per day, trend analysis, anomaly log, certification verdict (PASS/FAIL/CONDITIONAL)
- Trigger: Day 7 cronjob (one-shot at 2026-08-16 ~14:47 UTC)

## 11. Failure Conditions

The following conditions during the 7-day window constitute **certification failure**:

| # | Condition | Impact |
|---|-----------|--------|
| F-1 | Any protected file SHA-256 changes from baseline | **AUTOMATIC INVALIDATION** — certification must restart from new baseline |
| F-2 | HEAD commit changes (new commit, rebase, merge, reset) | **AUTOMATIC INVALIDATION** — frozen baseline violated |
| F-3 | Any source edit to protected files (even if uncommitted) | **AUTOMATIC INVALIDATION** |
| F-4 | Bridge service STOPPED/EXITED/FATAL for > 5 min | Failure — investigate and document |
| F-5 | Hermes gateway unreachable for > 10 min | Failure — investigate and document |
| F-6 | Unexplained spike in failed/timeout tasks (> 3/day baseline) | Warning → Failure if unexplained and persistent |
| F-7 | New stale/orphan executor runs that are not auto-reconciled within 30 min | Warning → Failure if pattern repeats |
| F-8 | New notifier duplicate-send anomaly detected | Failure — investigate notifier logic |
| F-9 | Artifact registration regression (delivery_json count drops without task cleanup) | Warning → Failure if unexplained |
| F-10 | Claude Code CLI becomes non-functional (version check fails, dispatch fails) | Failure for Claude Code CLI executor dimension |
| F-11 | supervisord `[include]` section lost (recurring issue) | Warning → Failure if services not recovered |

## 12. Allowed Activities During Certification Window

The following activities are **allowed** and do NOT invalidate certification:

| Activity | Rationale |
|----------|-----------|
| Ordinary task dispatch via `POST /runs` or `POST /runs/executor` | Does not mutate frozen repo source files |
| Bridge creating/editing `data/dispatcher.db` rows | Normal runtime operation; DB is not a protected file |
| Bridge writing to `reports/` directory | Report artifacts are untracked, non-source |
| Hermes cronjob checkpoints running | Non-mutating read-only verification |
| `supervisorctl restart hermes-runtime-bridge` | Service restart does not change source files (binary identical); allowed for recovery but must be documented |
| Claude Code CLI task dispatch | Does not mutate frozen repo |
| New untracked report files | Untracked artifacts do not affect protected file hashes |

## 13. Prohibited Activities During Certification Window

The following activities **INVALIDATE** certification:

| Activity | Impact |
|----------|--------|
| Any `git commit`, `git push`, `git merge`, `git rebase`, `git reset` on `main` | F-2: HEAD change |
| Any source edit to protected files (app.py, dispatcher/*.py) | F-1/F-3: Protected hash change |
| Any `pip install`, `apt install`, package upgrade in the bridge venv | Mutates runtime environment |
| Any deploy or code change to the bridge service beyond restart | Mutates frozen baseline |
| `git stash` involving protected files | Creates uncommitted mutation |
| Deletion of protected files | Hash change to missing |
| Modification of `requirements.txt` / `pyproject.toml` / lockfiles | Runtime surface change |

## 14. Operator Guidance

### 14.1 During the Window

1. **Do NOT commit, push, merge, rebase, or deploy** to the `hermes-runtime-bridge` repository during the 7-day window.
2. **Do NOT edit** any file listed in Section 2 (Protected-File Baseline).
3. **DO** continue normal task dispatch — the bridge should handle ordinary workloads.
4. **DO** monitor for any supervisord service failures (especially the recurring `[include]` loss issue).
5. If a service fails, restart with `supervisorctl restart <service>` — this is allowed and does not invalidate certification.
6. If a protected file is accidentally modified, run `git checkout -- <file>` immediately to restore, and document the incident. If the modification was committed, certification is invalidated and must restart.

### 14.2 At Each Daily Checkpoint

1. The scheduled cronjob will run automatically and produce a daily checkpoint report.
2. Review the report for any F-1 through F-11 failure conditions.
3. If any failure condition is triggered, assess whether it invalidates the entire window or is a recoverable incident.
4. Anomaly log: each daily report appends to `reports/7_day_soak_anomaly_log.md`.

### 14.3 At Window End (Day 7)

1. The final checkpoint cronjob produces `reports/7_day_soak_certification_final.md`.
2. Review all 7 daily reports + anomaly log.
3. Certification verdict:
   - **PASS**: All 10 dimensions pass on all 7 days, zero invalidating failures.
   - **CONDITIONAL**: Non-invalidating warnings present but all 10 dimensions ultimately pass.
   - **FAIL**: Any invalidating failure condition triggered (F-1, F-2, F-3) or critical service failure (F-4, F-5) unexplained.

### 14.4 Post-Certification

1. If PASS: the bridge at commit `e1fc46b` is certified as stable for 7-day continuous operation.
2. If FAIL: diagnose root cause, fix, re-commit, and restart a new 7-day window from the new baseline.
3. If CONDITIONAL: document warnings, assess whether fixes are needed before production reliance.

## 15. Baseline Evidence Summary

| Evidence Category | Captured | Location |
|-------------------|----------|----------|
| Git HEAD + branch | YES | Section 1 |
| Dirty tracked file check | YES (NONE found) | Section 1.2 |
| Protected file SHA-256 hashes (11 files) | YES | Section 2 |
| Bridge /health snapshot | YES | Section 3.1 |
| Supervisord service status (9 services) | YES | Section 3.2 |
| OpenAPI endpoint health | YES | Section 3.3 |
| Task counts by status | YES | Section 4.1 |
| Executor run counts by status | YES | Section 4.2 |
| Task events summary | YES | Section 4.3 |
| Active executor inventory | YES | Section 5 |
| Stale/orphan run state | YES | Section 6 |
| Notifier health + duplicate-send check | YES | Section 7.1 |
| Artifact registration counts | YES | Section 7.2 |
| Dispatcher DB file info | YES | Section 7.3 |
| Hermes executor health | YES | Section 8.1 |
| Claude Code CLI health | YES | Section 8.2 |
| Codex CLI health | NOT ASSESSED (per directive) | Section 8.3 |
| Reaper health | YES | Section 9.1 |
| Heartbeat health | YES | Section 9.2 |
| Daily checkpoint schedule (7 days) | YES | Section 10.2 |
| Checkpoint verification matrix (10 dims) | YES | Section 10.3 |
| Failure conditions (F-1..F-11) | YES | Section 11 |
| Allowed/prohibited activities | YES | Sections 12-13 |
| Operator guidance | YES | Section 14 |

## 16. Certification Statement

**This document establishes the frozen baseline for the 7-Day AEE/Hermes Runtime Soak & Stability Certification (SOAK-2026-08-09).**

The repository at commit `e1fc46b4af3b25870c85b267fc027094ec483348` on branch `main` has been verified as clean (zero dirty tracked changes on protected files) and suitable for certification.

**Any repo/source change touching the frozen baseline during the 7-day window (2026-08-09 14:47:34 UTC through 2026-08-16 14:47:34 UTC) invalidates certification and requires restarting the window from a new baseline.**

**Ordinary task dispatch that does not mutate the frozen repo is allowed and expected during the window.**

Daily checkpoints are scheduled via Hermes cronjob (non-mutating, evidence-first). The final report is triggered on Day 7.

---

**Certification Status**: STARTED

**Baseline Recorded**: YES

**Daily Checkpoints**: SCHEDULED (7 one-shot cronjobs, Day 1 through Day 7)

**Final Report Trigger**: SCHEDULED (Day 7 checkpoint)

**Verdict**: STARTED/PASS — baseline evidence recorded, checkpoint schedule armed.

---

_Generated by Hermes M2 on 2026-08-09 14:47:34 UTC (2026-08-09 22:47:34 CST)_
_Repository: /home/ubuntu/hermes-runtime-bridge @ e1fc46b_
_Bridge version: 1.2.0_