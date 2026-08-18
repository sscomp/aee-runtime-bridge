# 7-Day Soak Certification Schedule & Artifact Verification

**Task ID:** TASK-20260809-0028 (verification)
**Certification ID:** SOAK-2026-08-09
**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Verification Date:** 2026-08-09T14:53:09Z (2026-08-09T22:53:09 CST)
**Verifier:** M2 (Hermes Agent, glm-5.2, ollama-cloud)
**Mode:** READ-ONLY verification + minimal artifact registration recovery (no source/runtime logic changes)

---

## 1. Certification Baseline

| Field | Value |
|-------|-------|
| Certification ID | SOAK-2026-08-09 |
| Repository | `/home/ubuntu/hermes-runtime-bridge` |
| Branch | `main` |
| Baseline Commit | `e1fc46b4af3b25870c85b267fc027094ec483348` |
| Commit Short | `e1fc46b` |
| Commit Subject | `fix(bridge): periodic stale-run reconciliation in executor watcher` |
| Protected Files | 11 (see Section 7) |
| Start Artifact | `reports/7_day_soak_certification_start.md` (21330 bytes, sha256 `5463f9e688debedee8738fbeacb520d45dc7081bad83cf9ffe12de375772cc88`) |
| Checkpoint Script | `scripts/soak_checkpoint.py` (273 lines) |

### Baseline Origin

The 7-day soak/stability certification was created and started by TASK-20260809-0028 (dispatched 2026-08-09T14:46:47Z, completed 2026-08-09T14:49:53Z, duration 185.95s). The task produced the start artifact and created 7 one-shot Hermes cronjobs for daily checkpoints.

---

## 2. Start/End Times

| Marker | UTC | Asia/Taipei (CST, UTC+8) |
|--------|-----|--------------------------|
| Window Start | 2026-08-09 14:47:34 | 2026-08-09 22:47:34 |
| Day 1 Checkpoint | 2026-08-10 14:47:34 | 2026-08-10 22:47:34 |
| Day 2 Checkpoint | 2026-08-11 14:47:34 | 2026-08-11 22:47:34 |
| Day 3 Checkpoint | 2026-08-12 14:47:34 | 2026-08-12 22:47:34 |
| Day 4 Checkpoint | 2026-08-13 14:47:34 | 2026-08-13 22:47:34 |
| Day 5 Checkpoint | 2026-08-14 14:47:34 | 2026-08-14 22:47:34 |
| Day 6 Checkpoint | 2026-08-15 14:47:34 | 2026-08-15 22:47:34 |
| Day 7 Checkpoint (Final) | 2026-08-16 14:47:34 | 2026-08-16 22:47:34 |
| Window End | 2026-08-16 14:47:34 | 2026-08-16 22:47:34 |
| Duration | 7 days (168 hours) | |

### Timestamp Verification

- Start timestamp derived from TASK-0028 completion event (`2026-08-09T14:49:53.087Z` completed; start artifact records `14:47:34 UTC` as the window start, which is the task's progress-to-10% timestamp at `14:47:34.582Z`).
- UTC-to-CST conversion verified: UTC+8 is correct for Asia/Taipei.
- End timestamp = start + 7 days = 2026-08-16 14:47:34 UTC. Confirmed.
- All 7 daily checkpoint times are exactly 24 hours apart, sequential, no gaps, no overlaps.

---

## 3. Scheduler Entries Day 1-Day 7

All 7 checkpoint jobs are registered in the Hermes cronjob scheduler (`~/.hermes/cron/jobs.json`) as one-shot (`kind: "once"`) jobs. Each job is `enabled: true`, `state: "scheduled"`, with `deliver: "local"` and `enabled_toolsets: ["terminal", "file"]`.

| Day | Job ID | Job Name | Schedule (run_at) | Enabled | State | Toolsets |
|-----|--------|----------|-------------------|---------|-------|----------|
| 1 | `5bfb3f0cb850` | soak-day1-checkpoint | 2026-08-10T14:47:34+00:00 | true | scheduled | terminal, file |
| 2 | `5b59d0a53aa3` | soak-day2-checkpoint | 2026-08-11T14:47:34+00:00 | true | scheduled | terminal, file |
| 3 | `f9fe029b54f2` | soak-day3-checkpoint | 2026-08-12T14:47:34+00:00 | true | scheduled | terminal, file |
| 4 | `d8d775fd84fb` | soak-day4-checkpoint | 2026-08-13T14:47:34+00:00 | true | scheduled | terminal, file |
| 5 | `d1dd8243b3aa` | soak-day5-checkpoint | 2026-08-14T14:47:34+00:00 | true | scheduled | terminal, file |
| 6 | `afba3803cc25` | soak-day6-checkpoint | 2026-08-15T14:47:34+00:00 | true | scheduled | terminal, file |
| 7 | `0bfe17decc43` | soak-day7-final-checkpoint | 2026-08-16T14:47:34+00:00 | true | scheduled | terminal, file |

### Scheduler Entry Verification

- **Total soak jobs:** 7 (expected 7)
- **Duplicate names:** NONE
- **Duplicate IDs:** NONE
- **Distinct run_at dates:** 7 (expected 7)
- **All enabled + state=scheduled:** YES
- **Provider snapshot:** ollama-cloud / glm-5.2 (consistent across all 7)
- **Prompt content:** Each job prompt instructs running `python3 /home/ubuntu/hermes-runtime-bridge/scripts/soak_checkpoint.py --day=N` with explicit "Do NOT edit any source files, commit, push, or restart any services" directive.

### Job Creation Timestamps

All 7 jobs were created within a 15-second window on 2026-08-09T22:48:55–22:49:10 CST, consistent with TASK-0028's execution window.

---

## 4. Final Trigger

The Day 7 checkpoint job (`0bfe17decc43`, `soak-day7-final-checkpoint`) serves as the final report trigger.

| Field | Value |
|-------|-------|
| Job ID | `0bfe17decc43` |
| Name | soak-day7-final-checkpoint |
| Schedule | 2026-08-16T14:47:34+00:00 (one-shot) |
| Enabled | true |
| State | scheduled |
| Final Report Artifact | `reports/7_day_soak_certification_final.md` |
| Prompt mentions "final" | YES |
| Prompt mentions `7_day_soak_certification_final.md` | YES |
| Checkpoint script `is_final` logic | `day == 7` triggers final report generation (lines 237-261) |

The checkpoint script (`soak_checkpoint.py`) has a `is_final = (day == 7)` flag at line 81 that triggers writing `reports/7_day_soak_certification_final.md` with the final certification verdict, cumulative analysis, and certification statement.

---

## 5. Timezone Validation

| Check | Result |
|-------|--------|
| UTC-to-CST offset | UTC+8 (correct for Asia/Taipei) |
| Start UTC (14:47:34) → CST (22:47:34) | CORRECT |
| End UTC (2026-08-16 14:47:34) → CST (2026-08-16 22:47:34) | CORRECT |
| Day 1-7 cron `run_at` values match expected timestamps | ALL 7 MATCH |
| Cron `run_at` timezone suffix | `+00:00` (correct UTC) |
| No timezone mistakes (e.g., midnight triggers, wrong offset) | CONFIRMED |
| Impossible trigger times (past dates, contradictory schedules) | NONE FOUND |
| Daily intervals are exactly 24h apart | CONFIRMED |

### Prior Soak Comparison

The prior P0-1 shadow run (TASK-20260809-0003 audit) used `b01d45d3895a` with schedule `0 1 * * *` (daily at 01:00 TPE = 17:00 UTC prior day). That run had 6/7 missing daily checks. The new SOAK-2026-08-09 uses one-shot jobs with exact timestamps, which avoids the recurring-cron-silence failure mode.

---

## 6. Baseline/Protected Hash Binding

Three-way hash verification: Start Artifact vs Checkpoint Script (`PROTECTED_FILES` dict) vs Current On-Disk SHA-256.

| # | File | Artifact == Script | Artifact == Disk | Script == Disk |
|---|------|-------------------|------------------|----------------|
| 1 | `app.py` | MATCH | MATCH | MATCH |
| 2 | `dispatcher/__init__.py` | MATCH | MATCH | MATCH |
| 3 | `dispatcher/db.py` | MATCH | MATCH | MATCH |
| 4 | `dispatcher/executor_runs.py` | MATCH | MATCH | MATCH |
| 5 | `dispatcher/executor_watcher.py` | MATCH | MATCH | MATCH |
| 6 | `dispatcher/manager.py` | MATCH | MATCH | MATCH |
| 7 | `dispatcher/models.py` | MATCH | MATCH | MATCH |
| 8 | `dispatcher/notifier.py` | MATCH | MATCH | MATCH |
| 9 | `dispatcher/progress.py` | MATCH | MATCH | MATCH |
| 10 | `dispatcher/reaper.py` | MATCH | MATCH | MATCH |
| 11 | `dispatcher/safety.py` | MATCH | MATCH | MATCH |

**All 11 protected files: three-way hash binding CONFIRMED.**

### Checkpoint Script Baseline HEAD

The checkpoint script hardcodes `BASELINE_HEAD = "e1fc46b4af3b25870c85b267fc027094ec483348"` at line 22. This matches the start artifact's recorded baseline commit. CONFIRMED.

### Checkpoint Script Baseline Counts

The script also hardcodes `BASELINE_COUNTS` (lines 38-56) with task/run lifecycle counts matching the start artifact's Section 4 (tasks_total=193, completed=184, failed=4, timeout=3, cancelled=1, running=1, executor_runs_total=209, etc.). These provide delta-tracking for daily checkpoints.

---

## 7. Artifact Registration Check

### Initial State (Pre-Recovery)

| Check | Result |
|-------|--------|
| `task_outputs.delivery_json` for TASK-0028 | NULL (not populated) |
| `artifacts` table rows for TASK-0028 | 0 (not registered) |
| `output_text` mentions start artifact path | NO |
| Start artifact file on disk | EXISTS (21330 bytes, sha256 `5463f9e6...`) |
| `notification_json` | Populated (message_id=11776, sent=true) |

**Finding:** TASK-0028 completed successfully and sent a Telegram notification (message_id 11776), but the bridge's `_collect_task_evidence` did not register the start artifact in the `artifacts` table or populate `delivery_json`. The `expected_artifacts` field in the task row was empty (`[]`), meaning the GPT orchestrator did not declare expected artifacts when dispatching — so the bridge had no artifact paths to verify.

### Recovery Performed

The start artifact was registered without re-running any analysis:

| Action | Details |
|--------|---------|
| Artifacts table INSERT | artifact_id=`8bd08e7002ea76a5`, task_id=TASK-20260809-0028, path=`/home/ubuntu/hermes-runtime-bridge/reports/7_day_soak_certification_start.md`, sha256=`5463f9e688debedee8738fbeacb520d45dc7081bad83cf9ffe12de375772cc88`, size=21330, file_exists=1, kind=report, content_type=text/markdown, classification_source=recovery_verification, collected_at=2026-08-09T14:53:09Z, version=1 |
| task_outputs UPDATE | delivery_json populated with artifact_count=1, artifact_paths, artifact_verification (exists/sha256/size/mtime), recovery_performed=true, recovery_reason, recovery_timestamp |
| Source/runtime code changed | NO — only DB data rows inserted/updated |
| Re-analysis re-run | NO — existing artifact file was registered, not regenerated |

### Post-Recovery State

| Check | Result |
|-------|--------|
| `artifacts` table rows for TASK-0028 | 1 (registered) |
| `task_outputs.delivery_json` | POPULATED (artifact_count=1, artifact_paths populated, artifact_verification populated) |
| `artifact_verification` | exists=true, sha256=5463f9e6..., size=21330 |
| Start artifact file on disk | UNCHANGED (same sha256, same size) |

---

## 8. Any Minimal Recovery Performed

| Recovery Item | Method | Impact |
|---------------|--------|--------|
| Start artifact registration in `artifacts` table | Direct SQLite INSERT (1 row) | DB data only; no source/runtime code change |
| `delivery_json` population in `task_outputs` | Direct SQLite UPDATE (1 row) | DB data only; provides artifact_verification metadata for the task |

**No source files were modified.** No runtime logic was changed. No commit, push, deploy, restart, merge, rebase, stash, delete, cleanup, or package changes were performed. The recovery was strictly a metadata registration in the dispatcher database, registering an existing on-disk file that was produced by TASK-0028 but never formally cataloged.

### Why This Was Needed

The bridge's `manager.complete()` scans `output_text` for absolute paths and registers them via `_collect_task_evidence`. TASK-0028's `output_text` was a verification summary ("Ad-hoc verification: 12/12 ALL PASS...") that did not contain the absolute path `/home/ubuntu/hermes-runtime-bridge/reports/7_day_soak_certification_start.md`. The `expected_artifacts` field was empty (`[]`), so the bridge had no paths to verify. The artifact file was written to disk by the agent but never registered in the DB.

---

## 9. Remaining Risks

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R-1 | Container reset could wipe Hermes cron scheduler state (one-shot jobs lost) | MEDIUM | One-shot jobs are stored in `~/.hermes/cron/jobs.json` which persists; but if container is reset, the scheduler process restarts and reads jobs.json — one-shots with future `run_at` should survive. Verify after any reset. |
| R-2 | `soak_checkpoint.py` is an untracked file (`scripts/soak_checkpoint.py`) — could be lost on clean checkout | LOW | File is on disk and referenced by 7 cron jobs. Not a protected file. If lost, checkpoints fail loudly (cron job returns error). |
| R-3 | Start artifact is untracked (`reports/7_day_soak_certification_start.md`) | LOW | Same as R-2. Untracked report artifacts are expected per soak definition (Section 12 of start artifact). |
| R-4 | Supervisord `[include]` section loss (recurring issue, 3rd occurrence 2026-08-05) | LOW-MEDIUM | Not a protected file. If services stop, checkpoint Dim 3 catches it. Restart is allowed per soak rules. |
| R-5 | Morning-brief cron mutates `macro_history.db` daily — not a protected file but could confuse checkpoint analysis | NEGLIGIBLE | `macro_history.db` is not in the protected file set. No impact. |
| R-6 | Day 1 checkpoint is ~23.9 hours from verification time (2026-08-10 14:47 UTC). If container resets before then, one-shot job may or may not fire depending on scheduler recovery. | LOW | Verify scheduler state after any container reset. |
| R-7 | The checkpoint script does not verify `scripts/soak_checkpoint.py` itself is unchanged — only protected files are hash-checked | NEGLIGIBLE | The script is not a protected file. If modified, checkpoints would use different logic but protected-file checks would still catch mutations. |
| R-8 | `delivery_json` recovery was performed via direct SQLite write, bypassing the bridge's `_collect_task_evidence` code path | LOW | This is a metadata-only recovery. The artifact file was already on disk. No runtime logic was bypassed. |

---

## 10. Formal Certification Status

### ARMED

**Certification Status: ARMED**

The 7-Day AEE/Hermes Runtime Soak & Stability Certification (SOAK-2026-08-09) is formally ARMED.

### Evidence Summary

| Verification Dimension | Status | Evidence |
|------------------------|--------|----------|
| Baseline commit frozen | PASS | HEAD = `e1fc46b4af3b25870c85b267fc027094ec483348`, matches start artifact and checkpoint script |
| Protected file hashes (11 files) | PASS | Three-way match: artifact == script == disk for all 11 files |
| Zero tracked dirty files | PASS | `git status --short` shows 0 tracked modifications, 26 untracked (all reports/scripts) |
| Start artifact exists on disk | PASS | 21330 bytes, sha256 `5463f9e6...`, mtime 2026-08-09T14:48:16Z |
| Start artifact registered in DB | PASS (recovered) | Artifacts table row + delivery_json populated by this verification task |
| Day 1-7 cron jobs scheduled | PASS | 7 one-shot jobs, all enabled, state=scheduled, distinct run_at dates |
| No duplicate/missing schedule entries | PASS | 7 unique IDs, 7 unique names, 7 distinct dates |
| Timezone validation | PASS | UTC+8 conversion correct, all run_at match expected timestamps |
| Final report trigger (Day 7) | PASS | Job `0bfe17decc43` scheduled at 2026-08-16T14:47:34+00:00, prompt references final report |
| Checkpoint script functional | PASS | 273 lines, imports valid, produces daily checkpoint + final report on Day 7 |
| Ordinary task dispatch allowed | PASS | Soak definition Section 12 explicitly allows POST /runs without invalidation |
| Repo/source mutation invalidates window | PASS | Soak definition Section 13 + failure conditions F-1/F-2/F-3 enforce this |

### Conditions for ARMED Status

1. All 7 one-shot cron jobs are `enabled=true` and `state=scheduled` in the Hermes scheduler.
2. All `run_at` timestamps are in the future (current time: 2026-08-09T14:51Z; first checkpoint: 2026-08-10T14:47Z).
3. No checkpoint has yet fired — the window is in its initial phase (Day 0 → Day 1 transition).
4. HEAD and all 11 protected file hashes match the frozen baseline.
5. The start artifact is formally registered in Runtime metadata (artifacts table + delivery_json).

---

## 11. Next Checkpoint Due

| Field | Value |
|-------|-------|
| Next Checkpoint | Day 1 |
| Scheduled Time (UTC) | 2026-08-10T14:47:34+00:00 |
| Scheduled Time (CST) | 2026-08-10T22:47:34 CST |
| Job ID | `5bfb3f0cb850` |
| Job Name | soak-day1-checkpoint |
| Time Until (from verification time 14:53 UTC Aug 9) | ~23 hours 54 minutes |
| Expected Output | `reports/7_day_soak_day1_checkpoint.md` |
| Verification Dimensions | 10 (HEAD, protected hashes, bridge health, supervisord, task counts, failed/timeout deltas, stale/orphan runs, reaper health, artifact registration, notifier duplicates, executor health) |

---

## Appendix A: Git State at Verification

| Field | Value |
|-------|-------|
| HEAD | `e1fc46b4af3b25870c85b267fc027094ec483348` |
| Branch | `main` |
| Tracked dirty files | 0 |
| Untracked entries | 26 (all reports/ and scripts/, no source files) |
| HEAD matches baseline | YES |

## Appendix B: All Cron Jobs in Scheduler

| # | Job ID | Name | Schedule | Enabled | State |
|---|--------|------|----------|---------|-------|
| 1 | 381d62ce7f5e | morning-brief-dreaming | 0 22 * * * | true | scheduled |
| 2 | 4d8197ba6dab | morning-brief-delivery | 0 8 * * * | true | scheduled |
| 3 | 50d257f12a18 | zo-computer-keepalive | */20 * * * * | true | scheduled |
| 4 | ed214c19c4ac | 總體經濟晨報 | 30 8 * * 1-6 | true | scheduled |
| 5 | 60d92c57b826 | 產業趨勢週報 | 0 8 * * 1 | true | scheduled |
| 6 | 5eaa5fa9a50d | 公司研究月報 | 0 8 12 * * | true | scheduled |
| 7 | af64556bc8e9 | 季度成分股更新提醒 | 0 9 1 1,4,7,10 * | true | scheduled |
| 8 | 966aaf806098 | phase4-shadow-run | 0 9 * * * | true | scheduled |
| 9 | b01d45d3895a | p0-1-shadow-run-daily-check | 0 1 * * * | true | scheduled |
| 10 | 5bfb3f0cb850 | soak-day1-checkpoint | 2026-08-10T14:47:34+00:00 | true | scheduled |
| 11 | 5b59d0a53aa3 | soak-day2-checkpoint | 2026-08-11T14:47:34+00:00 | true | scheduled |
| 12 | f9fe029b54f2 | soak-day3-checkpoint | 2026-08-12T14:47:34+00:00 | true | scheduled |
| 13 | d8d775fd84fb | soak-day4-checkpoint | 2026-08-13T14:47:34+00:00 | true | scheduled |
| 14 | d1dd8243b3aa | soak-day5-checkpoint | 2026-08-14T14:47:34+00:00 | true | scheduled |
| 15 | afba3803cc25 | soak-day6-checkpoint | 2026-08-15T14:47:34+00:00 | true | scheduled |
| 16 | 0bfe17decc43 | soak-day7-final-checkpoint | 2026-08-16T14:47:34+00:00 | true | scheduled |

Total: 16 jobs (9 pre-existing + 7 soak certification). No duplicates. No conflicts.

## Appendix C: TASK-0028 Dispatcher DB Record

| Field | Value |
|-------|-------|
| task_id | TASK-20260809-0028 |
| status | completed |
| created_at | 2026-08-09T14:46:47.127Z |
| completed_at | 2026-08-09T14:49:53.086Z |
| duration_sec | 185.953 |
| model_name | MiniMaxAI/MiniMax-M3 |
| git_commit | e1fc46b4af3b25870c85b267fc027094ec483348 |
| git_branch | main |
| runtime_type | hermes |
| adapter_name | hermes |
| hermes_run_id | run_eb5a166cb0bd4c8b8738a575366a361f |
| expected_artifacts | [] (empty — root cause of missing registration) |
| warning_count | 0 |
| notification message_id | 11776 (sent=true, recipient=5132341473) |
| task_events | 11 events (created, queued, 3x log, started, log, 2x progress, completed, notification_completed) |

## Appendix D: Telegram Notification

| Field | Value |
|-------|-------|
| Notification sent | YES (pre-existing from TASK-0028) |
| message_id | 11776 |
| recipient | 5132341473 (鼎鼎) |
| method | hermes_send |
| timestamp | 2026-08-09T14:49:53Z (22:49:53 CST) |

This verification task did not send a separate Telegram notification (read-only verification mode). The original TASK-0028 notification (message_id 11776) serves as the certification start notification.

---

_Generated by Hermes M2 on 2026-08-09T14:53:09Z (2026-08-09T22:53:09 CST)_
_Repository: /home/ubuntu/hermes-runtime-bridge @ e1fc46b_
_Verification mode: READ-ONLY + minimal artifact registration recovery_
_No commit, push, deploy, restart, merge, rebase, stash, delete, cleanup, or package changes_