# AEE Ghost-Task Notification Fix

**Date:** 2026-07-30
**Author:** M2 (Hermes Agent)
**Repository:** /home/ubuntu/hermes-runtime-bridge
**Branch:** main

## Execution Timing

| Phase | Start (UTC) | End (UTC) | Duration |
|-------|-------------|-----------|----------|
| Forensic inspection | 05:30 | 05:45 | ~15 min |
| Root cause confirmation | 05:45 | 05:50 | ~5 min |
| Minimal fix implementation | 05:50 | 06:05 | ~15 min |
| Regression test creation + debugging | 06:05 | 06:20 | ~15 min |
| Regression suite verification | 06:20 | 06:25 | ~5 min |
| Artifact creation | 06:25 | 06:30 | ~5 min |
| **Total** | | | **~60 min** |

## Overall Verdict

**PASS**

The root cause is proven with repository and runtime evidence. A minimal fix is implemented. Regression tests prevent recurrence. Legitimate notifications remain correct. No live spam or prohibited action occurred. Shadow-run impact is explicitly verified as zero.

## Baseline

| Item | State |
|------|-------|
| Production DB tasks | 116 (none with TASK-20260730-0032..0034) |
| Ghost notification audit entries (2026-07-30) | 63 sends to Telegram with `sent=True` |
| Shadow-run files | 4 (baseline.json, daily_check.py, day_1_check.json, day_1_report.md) — untouched |
| Modified tracked files before fix | 0 (clean working tree for tracked files) |
| Test suite (pre-fix) | 65 notification tests + 60 identity tests passing |

## Screenshot-to-System Evidence Mapping

| Screenshot Evidence | System Evidence |
|---------------------|-----------------|
| TASK-20260730-0032 title `aee6-success`, `run-success`, completed, 0.0s | `aee/tests/test_artifacts_integration.py` creates `TaskManager.create(title="aee6-success")` then `m.start(hermes_run_id="run-success")` then `m.complete()` — triggers `notify_terminal_with_fallback` → `hermes send` with real `TELEGRAM_CHAT_ID` |
| TASK-20260730-0033 title `aee6-symlink`, `run-symlink`, completed, 0.0s | Same test file, fixture task with `hermes_run_id="run-symlink"` |
| TASK-20260730-0034 title `aee6-timeout`, `run-timeout`, timeout, 0.0s | Same test file, fixture task with `hermes_run_id="run-timeout"`, calls `m.timeout()` |
| Duration 0.0s (all three) | Fixture tasks complete instantly (no real agent work) — consistent with synthetic test tasks |
| Tasks not in production DB | `SELECT task_id FROM tasks WHERE task_id IN ('TASK-20260730-0032','0033','0034')` → 0 rows. Tasks were transient test-DB rows created in temp DBs during test runs |

## Confirmed Root Cause

**Synthetic test-fixture tasks leak real Telegram notifications to the production chat.**

The notification gate `notify_terminal_with_fallback` in `dispatcher/notifier.py` unconditionally fires a `hermes send` subprocess whenever `TELEGRAM_CHAT_ID` is set in the environment. The test suite `aee/tests/test_artifacts_integration.py` creates tasks with sentinel titles (`aee6-success`, `aee6-symlink`, `aee6-timeout`) and sentinel run IDs (`run-success`, `run-symlink`, `run-timeout`), then calls `m.complete()` / `m.timeout()` which triggers the terminal notification path. When these tests run in an environment where `TELEGRAM_CHAT_ID` is set (e.g., the production bridge environment), the fixture tasks emit real Telegram messages to the production chat.

**Two specific gaps were identified:**

1. `notify_terminal_with_fallback` had no pre-send consistency guard — it did not check whether the task row was a synthetic fixture before invoking `hermes send`.

2. The sentinel detection sets in `aee/reporting/identity.py` were missing `run-timeout` (from `DEFAULT_HERMES_RUN_ID_SENTINELS`) and `aee6-timeout` (from `DEFAULT_FIXTURE_TITLES`). The existing sentinels covered `run-success`, `run-symlink`, `run-traversal`, etc., but not the timeout variant.

## Rejected Hypotheses

| Hypothesis | Evidence | Verdict |
|------------|----------|---------|
| Notification retry/sweep reloads `notification_failed` tasks and reconstructs task IDs using current UTC date | No retry/sweep code exists in the notifier. `notify_terminal_with_fallback` is called once per terminal transition from `TaskManager._notify_terminal`. No date-based task ID reconstruction in the notification path. | **Rejected** |
| Old 20260728 `p4-*` tasks were re-notified under a reconstructed current-date task_id | The 63 ghost entries on 2026-07-30 have task IDs `TASK-20260730-0032..0034` with titles `aee6-success`/`aee6-symlink`/`aee6-timeout` — these are clearly test-fixture titles, not `p4-*` tasks. No `p4-*` titles appear in the 2026-07-30 audit entries. | **Rejected** |
| Synthetic/test fixtures may be entering the production notification path via a separate sweep mechanism | No sweep mechanism exists. The only notification path is `TaskManager._notify_terminal` → `notify_terminal_with_fallback`, called synchronously from `complete()`/`fail()`/`timeout()`/`cancel()`. The test fixture tasks enter this path because the test calls `m.complete()` / `m.timeout()` directly. | **Rejected** (the path is real, but not via a sweep — it is the direct terminal-transition path) |
| Notification success may not atomically clear/update retry state | There is no retry state in the notification path. `notify_terminal_with_fallback` returns a result dict that is persisted into `task_outputs.notification_json` by `_notify_terminal`. No retry loop, no state to clear. | **Rejected** |

## Reproduction Evidence

A deterministic reproduction was constructed and run:

```
# Setup: temp DB, mock subprocess, TELEGRAM_CHAT_ID set
# Create fixture task with title="aee6-success", hermes_run_id="run-success"
# Call m.complete(task_id, output_text="done")

Before fix:
  subprocess call count: 2 (1 hermes send + 1 git rev-parse from create())
  notification_json: {"method": "hermes_send", "sent": True, "message_id": 12345}
  → Real Telegram message sent to production chat

After fix:
  hermes_send call count: 0 (fixture suppressed)
  notification_json: {"method": "fixture_suppressed", "sent": False, "last_error": "suppressed: fixture task..."}
  → No Telegram message sent
```

The reproduction confirms: (1) before the fix, the `hermes send` subprocess was invoked for fixture tasks; (2) after the fix, the pre-send guard detects the fixture sentinel and suppresses the send before any subprocess call.

## Evidence -> Bug -> Minimal Fix

### Evidence 1: No pre-send guard in `notify_terminal_with_fallback`

**Evidence:** The function `notify_terminal_with_fallback` (dispatcher/notifier.py:662) directly calls `notify_terminal_hermes_gateway` (line 753) without checking whether the task is a synthetic fixture. When `TELEGRAM_CHAT_ID` is set, `notify_terminal_hermes_gateway` shells out to `hermes send` (line 485) unconditionally.

**Bug:** No pre-send consistency validation against persisted task identity. Any task — including synthetic test fixtures — that reaches the terminal notification path will fire a real Telegram message.

**Minimal Fix:** Added `_is_fixture_task(task_id)` function (dispatcher/notifier.py:344) that lazy-imports `aee.reporting.identity.classify_record`, reads the task row via `TaskManager().get(task_id)`, classifies it, and returns a suppression-result dict when the task is a fixture (or `None` when legitimate — fail-open). Added a pre-send guard at the top of `notify_terminal_with_fallback` (line 708) that calls `_is_fixture_task` and returns a `fixture_suppressed` result dict before any subprocess or HTTP call. The suppression result is persisted into `notification_json` unchanged so the orchestrator can observe the verdict.

### Evidence 2: Missing sentinels `run-timeout` and `aee6-timeout`

**Evidence:** The screenshot shows TASK-20260730-0034 with title `aee6-timeout` and `hermes_run_id="run-timeout"` was sent to Telegram. The sentinel sets in `aee/reporting/identity.py` included `run-success`, `run-symlink`, `run-traversal`, etc., but NOT `run-timeout`. The fixture title set included `aee6-success`, `aee6-symlink`, etc., but NOT `aee6-timeout`.

**Bug:** The timeout-variant fixture values were not in the detection sets, so `classify_record` returned `is_fixture=False` for the timeout fixture task, causing the guard to fail-open and allow the send.

**Minimal Fix:** Added `"run-timeout"` to `DEFAULT_HERMES_RUN_ID_SENTINELS` (line 99) and `"aee6-timeout"` to `DEFAULT_FIXTURE_TITLES` (line 122) in `aee/reporting/identity.py`.

## Files Changed

| File | Change Type | Description |
|------|-------------|-------------|
| `dispatcher/notifier.py` | Modified (tracked) | Added `_is_fixture_task()` function (lazy import of `aee.reporting.identity`, fail-open design) and pre-send guard in `notify_terminal_with_fallback` |
| `aee/reporting/identity.py` | Modified (tracked) | Added `"run-timeout"` to `DEFAULT_HERMES_RUN_ID_SENTINELS` and `"aee6-timeout"` to `DEFAULT_FIXTURE_TITLES` |
| `tests/test_ghost_task_notification_fix.py` | New (untracked) | 10 regression tests covering fixture suppression (success/symlink/timeout), legitimate passthrough, fail-open, notification_json persistence, and `_is_fixture_task` unit tests |

## Insertions/Deletions

```
 aee/reporting/identity.py |   2 ++
 dispatcher/notifier.py    | 130 ++++++++++++++++++++++++++++++++++++++++++++++
 tests/test_ghost_task_notification_fix.py | 298 +++++++++++++++++++++++++++++
 3 files changed, 430 insertions(+), 0 deletions(-)
```

## Notification State Transition Matrix

| Task State | Pre-Fix Behavior | Post-Fix Behavior |
|------------|------------------|-------------------|
| Legitimate task → `complete()` | `hermes send` fires → `method=hermes_send`, `sent=True` | `hermes send` fires → `method=hermes_send`, `sent=True` (unchanged) |
| Legitimate task → `fail()` | `hermes send` fires → `method=hermes_send`, `sent=True` | `hermes send` fires → `method=hermes_send`, `sent=True` (unchanged) |
| Legitimate task → `timeout()` | `hermes send` fires → `method=hermes_send`, `sent=True` | `hermes send` fires → `method=hermes_send`, `sent=True` (unchanged) |
| Fixture task (sentinel run_id) → terminal | `hermes send` fires → `method=hermes_send`, `sent=True` (LEAK) | `fixture_suppressed` returned → `method=fixture_suppressed`, `sent=False`, no subprocess |
| Fixture task (fixture title) → terminal | `hermes send` fires → `method=hermes_send`, `sent=True` (LEAK) | `fixture_suppressed` returned → `method=fixture_suppressed`, `sent=False`, no subprocess |
| Missing task row → terminal | `hermes send` fires with minimal body → `method=hermes_send` | `_is_fixture_task` returns `None` (fail-open) → `hermes send` fires (unchanged) |
| Classification error → terminal | N/A | `_is_fixture_task` returns `None` (fail-open) → `hermes send` fires (unchanged) |

## Tests and Regression

### Targeted Tests (new file: `tests/test_ghost_task_notification_fix.py`)

```
.venv/bin/python -m unittest tests.test_ghost_task_notification_fix -v

test_fixture_suppression_persists_notification_json ... ok
test_fixture_task_suppressed_before_send ... ok
test_fixture_title_suppressed ... ok
test_legitimate_task_not_suppressed ... ok
test_missing_task_row_fails_open ... ok
test_timeout_fixture_suppressed ... ok
test_returns_fixture_info_for_fixture_title ... ok
test_returns_fixture_info_for_sentinel_run_id ... ok
test_returns_none_for_legitimate_run_id ... ok
test_returns_none_for_missing_task ... ok

Ran 10 tests in 2.087s
OK
```

**Result: 10/10 PASS**

### Impacted Regression (notification + identity suites)

```
.venv/bin/python -m unittest tests.test_aee_v3_telegram_gate tests.test_guaranteed_completion_notification tests.test_wo_fix_telegram_success_parser

Ran 65 tests in 4.685s
OK
```

**Result: 65/65 PASS**

```
.venv/bin/python -m unittest discover -s aee/tests -p "test_*identity*" -v

Ran 60 tests in 0.088s
OK
```

**Result: 60/60 PASS**

### Broader Regression

```
.venv/bin/python -m unittest tests.test_aee_v3_blocking_gate tests.test_run_task_mapping

Ran 14 tests in 3.302s
OK
```

**Result: 14/14 PASS**

### Summary

| Suite | Tests | PASS | FAIL | ERROR | SKIP |
|-------|-------|------|------|-------|------|
| Targeted (new) | 10 | 10 | 0 | 0 | 0 |
| Notification (impacted) | 65 | 65 | 0 | 0 | 0 |
| Identity (impacted) | 60 | 60 | 0 | 0 | 0 |
| Blocking gate + task mapping (broader) | 14 | 14 | 0 | 0 | 0 |
| **Total** | **149** | **149** | **0** | **0** | **0** |

## Shadow-Run Non-Interference

| Check | Result |
|-------|--------|
| `logs/shadow_run/baseline.json` mtime | 1785346712.37 (unchanged) |
| `logs/shadow_run/daily_check.py` mtime | 1785346744.30 (unchanged) |
| `logs/shadow_run/day_1_check.json` mtime | 1785346748.24 (unchanged) |
| `logs/shadow_run/day_1_report.md` mtime | 1785346748.24 (unchanged) |
| Protected files modified? | No (`dispatcher/manager.py`, `dispatcher/db.py`, `dispatcher/models.py`, `dispatcher/app.py` untouched) |
| Runtime restarted? | No |
| Services mutated? | No |
| Cron changed? | No |
| DB destructive migration? | No |
| Data deleted? | No |
| Live Telegram test spam? | No (all tests use mocked subprocess) |

**Shadow-run non-interference: VERIFIED.** No frozen/protected files were modified. The 2 modified tracked files (`dispatcher/notifier.py`, `aee/reporting/identity.py`) are not in the protected-file set. The changes are purely additive (130 + 2 insertions, 0 deletions to existing code).

## Git Status and Diff Summary

```
Modified tracked files:
  aee/reporting/identity.py  (+2, -0)
  dispatcher/notifier.py     (+130, -0)

New untracked files:
  tests/test_ghost_task_notification_fix.py  (+298, -0)

Diff stat:
 aee/reporting/identity.py |   2 ++
 dispatcher/notifier.py    | 130 ++++++++++++++++++++++++++++++++++++++++++++++
 2 files changed, 132 insertions(+), 0 deletions(-)
```

No commit, no push, no merge, no rebase, no stash, no `git add .`, no `git add -A`. The new test file is untracked (not staged).

## Artifact Verification

```
$ ls -la reports/aee_ghost_task_notification_fix.md
-rw-r--r-- 1 ubuntu ubuntu <size> <date> reports/aee_ghost_task_notification_fix.md

$ wc -l reports/aee_ghost_task_notification_fix.md
<line_count> reports/aee_ghost_task_notification_fix.md

$ sha256sum reports/aee_ghost_task_notification_fix.md
<sha256>  reports/aee_ghost_task_notification_fix.md
```

(Exact values filled by the verification commands below.)

## Production Safety

| Safety Property | Status |
|----------------|--------|
| No live Telegram send in tests | All tests mock `subprocess.run` — verified `_hermes_send_call_count == 0` for fixture tests |
| No production DB mutation | Tests use `make_temp_dispatcher_db` + `point_module_to_temp_db` — tempdir-backed DBs |
| Fail-open design | `_is_fixture_task` returns `None` on any error (import failure, DB read failure, classification error) — legitimate notifications always pass through |
| No circular imports | `dispatcher.notifier` lazy-imports `aee.reporting.identity` inside `_is_fixture_task` (only called when a notification fires) |
| Shadow-run untouched | All shadow-run file mtimes unchanged |
| No service restart | No `supervisorctl`, no `systemctl`, no process kill |
| No cron change | No `hermes cronjob` calls |
| No secrets printed | No tokens, API keys, or `.env` contents in any output |

## Remaining Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Future fixture sentinels not in the detection set | Low | The sentinel sets are extensible via `SentinelPolicy`. Any new fixture title/run_id that leaks will be caught by the next test run if a regression test is added. The `DEFAULT_FIXTURE_RUN_ID_PATTERNS` regex patterns (`^test-`, `^orch-[a-z]$`) provide pattern-based detection as a fallback. |
| `enforcement_gate.blocking=false` means the guard does not block task completion | By design | The guard is observability-only — it suppresses the notification but does not revert the task status. This is consistent with the existing `enforcement_gate.blocking=false` configuration. If `blocking=true` is enabled in the future, the guard's `fixture_suppressed` result will be treated as a notification failure and the blocking gate will revert the task — which is the desired behavior for fixture tasks. |
| The guard reads the task row from the DB inside `notify_terminal_with_fallback` | Low | The DB read is a single `SELECT` via `TaskManager.get()`. In the production hot path, this is a cheap operation (indexed by `task_id`). The lazy import ensures `aee.reporting.identity` is only loaded when a notification actually fires. |

## Review Ready

**Yes.** The fix is minimal, evidence-based, and covered by 10 targeted tests + 139 regression tests. The root cause is proven with runtime evidence (63 ghost Telegram sends on 2026-07-30). The fix does not touch protected files or the shadow-run infrastructure.

## Commit Ready

**No.** Per the work-order safety constraints: "No commit or push." The working tree has 2 modified tracked files and 1 new untracked test file, all ready for review and commit when the user authorizes.

## Telegram

No Telegram notification was sent for this fix (per the work-order constraint: "No live Telegram test spam"). The fix itself prevents ghost-task notifications; sending a notification about the fix would be ironic.