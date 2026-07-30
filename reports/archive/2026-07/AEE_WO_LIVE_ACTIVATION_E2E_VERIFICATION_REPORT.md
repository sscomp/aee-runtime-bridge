# WO-LIVE-ACTIVATION-E2E-VERIFICATION Report

**Work Order:** WO-LIVE-ACTIVATION-E2E-VERIFICATION
**Executed by:** Hermes M2 (Abacus.ai runtime)
**Date:** 2026-07-24 (UTC) / 2026-07-24 (Asia/Taipei)
**Safety constraints:** No commit, push, deploy, merge, rebase, stash, reset, clean, or unrelated changes. No automatic restart.

---

## Phase 1 — Runtime Identity

| Field | Value |
|---|---|
| Service | hermes-runtime-bridge (supervisord program) |
| PID | 1430846 |
| PPID | 7 (supervisord) |
| Executable | /usr/bin/python3.11 |
| Module path | /home/ubuntu/hermes-runtime-bridge/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787 --env-file .env --no-access-log |
| Working directory | /home/ubuntu/hermes-runtime-bridge |
| Process start | 2026-07-23 13:57:30 UTC |
| Elapsed at audit | ~4h 17m (15,413s) |
| Load flags | `--no-access-log`, NO `--reload` |
| Listening | 127.0.0.1:8787 (verified via ss) |
| Supervisor status | RUNNING (pid 1430846, uptime 4:16:00) |
| Health endpoint | `{"status":"ok","service":"hermes-runtime-bridge","version":"1.2.0",...}` |
| Data DB | /home/ubuntu/hermes-runtime-bridge/data/dispatcher.db (tables: tasks, task_events, task_outputs, workers, runtimes, dispatch_records, artifacts, artifact_policy_events, executor_runs) |

### Git state

| Field | Value |
|---|---|
| Repo HEAD (committed) | `654cf24` "test: add v3 blocking-gate + control-plane stop-routing tests" (2026-07-23 15:31:41 UTC) |
| HEAD at process start (13:57:30 UTC) | `c3adf5b` "fix: expose output-only Hermes task results" (2026-07-23 13:44:41 UTC) — the process started 13 minutes after c3adf5b was committed |
| Working tree HEAD (now) | `654cf24` (process started before this commit existed; the process has NOT loaded 654cf24) |
| Working tree status | DIRTY — 4 protected files modified, uncommitted: app.py, dispatcher/db.py, dispatcher/manager.py, dispatcher/models.py (+576/-5 across 4 files) |
| Stash list | (empty) |

### Running source vs working tree source

| Check | Result |
|---|---|
| `_telegram_result_is_confirmed` function in working tree `app.py` | **PRESENT** at line 2115 (uncommitted, mtime 2026-07-23 18:05:22 UTC) |
| `_telegram_result_is_confirmed` function in `c3adf5b:app.py` (running HEAD) | **ABSENT** — `git show c3adf5b:app.py \| grep _telegram_result_is_confirmed` returns zero matches |
| `_telegram_result_is_confirmed` function in `654cf24:app.py` (current HEAD) | **ABSENT** — same, function is only in the dirty working tree |
| `app.py` source mtime vs process start | source mtime 18:05:22 > proc start 13:57:30 → source was modified ~4h08m AFTER the process started |
| `app.cpython-311.pyc` mtime | 18:06:28 UTC — also after process start; uvicorn has no `--reload` so the running process did NOT re-import |
| Live API schema (`/openapi.json`) | `max_rescues` field **ABSENT** from `CreateRunRequest` (the dirty working tree adds it; the running process predates the addition) |
| Live API schema | `expected_artifacts` field **PRESENT** (added in commit 99d8d1c, before process start — confirms the running code is the c3adf5b-era code) |

### Conclusion (Phase 1)

The **running process does NOT contain the Telegram sync fix**. The fix (`_telegram_result_is_confirmed` + the merge-guard rewrite in `_merge_task_evidence_into_envelope`) exists only in the **uncommitted working-tree modifications** to `app.py`. The running process is executing the code as of commit `c3adf5b` (2026-07-23 13:44:41 UTC), which predates the fix. The process was started 2026-07-23 13:57:30 UTC and has not been restarted since; uvicorn was launched without `--reload`, so subsequent working-tree edits are not hot-loaded.

---

## Phase 2 — Activation Assessment

**The running service does NOT contain the Telegram sync fix.**

Evidence:

1. The fix function `_telegram_result_is_confirmed` is absent from `c3adf5b:app.py` — the commit the running process started under.
2. The working-tree `app.py` containing the fix was last modified 2026-07-23 18:05:22 UTC, ~4h08m AFTER the process started.
3. The supervisord unit has no `--reload` flag, so uvicorn does not watch for source changes.
4. The live `/openapi.json` schema does not advertise the `max_rescues` field that the dirty working tree adds to `CreateRunRequest` — the running process is serving the pre-fix code.
5. `git stash list` is empty, so the fix has never been stashed/recovered; it lives only in the dirty working tree.

**Activation IS required for the fix to take effect.** Per the work-order safety constraint ("If a restart is required, DO NOT perform it. Stop with evidence and await operator approval"), I am **stopping before any restart**.

### Why the fix is not active

The Hermes async submit path (`app.py:2027` in the running code) persists a placeholder `{"success": False, "skipped": "hermes is async; ..."}` into `executor_runs.telegram_result_json`. In the running (unfixed) code, `_merge_task_evidence_into_envelope` guards the telegram_result merge with `if not merged.get("telegram_result")` — a truthy-dict check. The placeholder dict is truthy, so the task-side `notification_json` (which carries `sent: True` + `message_id` on a successful Hermes Telegram Gateway send) never overrides it. Result: stdout shows a successful send with `message_id`, but the structured envelope returns `telegram_result.success == False`.

The fix (working tree only) introduces `_telegram_result_is_confirmed(value)` which returns True only when `success` OR `sent` is True AND `message_id` is non-None, and rewrites the merge guard to treat non-confirmed placeholders as empty. The running process does not have this function.

---

## Phase 3 — End-to-End Verification

**NOT executed.** The fix is not live, so an E2E verification would exercise the buggy code path and produce evidence of the bug, not evidence of the fix. Per the work-order structure, Phase 3 requires the live service to be running the intended code first. Since Phase 2 determined the live service is NOT running the fix, Phase 3 is gated on operator-approved activation.

### What Phase 3 would verify (post-activation)

A minimal work order through the normal Hermes path, comparing:
- **Hermes stdout** (the `hermes send --json` output from the dispatcher's notifier, carrying `success: true`, `message_id`, `chat_id`, `mirrored`).
- **`task_outputs.notification_json`** (persisted by `manager.complete()`, carrying `sent`, `message_id`, `recipient`, `ts_utc`, `ts_taipei`, `attempts`).
- **`executor_runs.telegram_result_json`** (the placeholder persisted at submit time by the async path).
- **Final API envelope** (`GET /runs/{run_id}` and `/runs/{run_id}/summary`) — the `telegram_result` field after `_merge_task_evidence_into_envelope` runs.

Pass criterion: `telegram_result.sent == True`, `telegram_result.success == True`, `telegram_result.message_id` matches the stdout `message_id`, `telegram_result.recipient` matches the configured chat_id, and `telegram_result.ts_utc` matches the `notification_json.ts_utc` within clock skew.

### Pre-existing DB evidence (the bug, observed in dispatcher.db)

Querying `data/dispatcher.db` shows 10 recent `executor_runs` rows with `telegram_result_json = '{}'` (empty dict, not the placeholder — these are test runs). The 10 most recent `task_outputs.notification_json` rows all carry `{"sent": false, "method": "failed", "recipient": null, "message_id": null, ...}` with `last_error: "gateway: TELEGRAM_CHAT_ID not set and no chat_id passed"`. This is a separate configuration gap (no `TELEGRAM_CHAT_ID` in the bridge `.env` for the notifier path), not the sync bug itself — but it means even after the fix is activated, a successful Telegram send requires the dispatcher notifier to be configured with a valid chat_id, OR the Hermes async path's `hermes send` (which reads the gateway's own Telegram credentials) must be the source of the `notification_json`. The fix specifically targets the case where `notification_json` carries `sent: True` + `message_id` (Hermes gateway send) and the executor-side placeholder suppresses it in the merge.

---

## Phase 4 — Regression

**NOT executed.** The fix is not live, so regression verification is gated on activation. The work-order lists five regression surfaces:

1. **Completion Gate** — `expected_artifacts` enforcement at `manager.complete()`. The running code has `expected_artifacts` in the schema (added pre-process-start), so the gate is live; but the `max_rescues` extension is working-tree-only.
2. **Incomplete Delivery** — the `incomplete_delivery` state + auto-rescue loop. Working-tree-only (the `max_rescues` field is absent from the live schema).
3. **Auto Rescue** — same as above.
4. **Artifact Verification** — the `artifact_verification` merge in `_merge_task_evidence_into_envelope`. The running code has the pre-fix merge logic (requires `artifact_paths` non-empty; the fix relaxes to `output_text OR artifacts`).
5. **Lifecycle Merge** — the `_merge_task_evidence_into_envelope` function itself. The running code has the pre-fix version with the truthy-dict telegram_result guard.

Post-activation regression checks would re-run the in-tree test suite (`tests/`) to confirm no regression on these five surfaces.

---

## Phase 5 — Evidence Summary

### Runtime identity (consolidated)

```
PID:               1430846
PPID:              7 (supervisord)
Executable:        /usr/bin/python3.11
Module:            /home/ubuntu/hermes-runtime-bridge/.venv/bin/uvicorn app:app
                   --host 127.0.0.1 --port 8787 --env-file .env --no-access-log
CWD:               /home/ubuntu/hermes-runtime-bridge
Started:           2026-07-23 13:57:30 UTC
Elapsed:           ~4h 17m at audit time
Reload enabled:    NO (--reload absent from supervisord unit)
Listening:         127.0.0.1:8787
Health:            ok (version 1.2.0, hermes reachable)
```

### Git HEAD comparison

```
Running code HEAD (at process start):  c3adf5b  (2026-07-23 13:44:41 UTC)
Current committed HEAD:                654cf24  (2026-07-23 15:31:41 UTC)
Working tree HEAD (rev-parse):         654cf24
Working tree status:                   DIRTY (4 files modified, +576/-5, uncommitted)
Stash list:                            empty
```

### Fix presence by source layer

```
Function: _telegram_result_is_confirmed
  In c3adf5b:app.py (running):    ABSENT
  In 654cf24:app.py (HEAD):       ABSENT
  In working-tree app.py:         PRESENT (line 2115, mtime 2026-07-23 18:05:22 UTC)
  In running process memory:      ABSENT (uvicorn started 13:57, no --reload)
```

### Live API schema evidence

```
GET /openapi.json → CreateRunRequest.properties:
  expected_artifacts:  PRESENT  (pre-fix, commit 99d8d1c)
  max_rescues:         ABSENT   (working-tree-only, confirms running code is pre-dirty-tree)
```

### Side-by-side: stdout vs structured telegram_result

**Not produced.** The fix is not live, so a side-by-side would show the bug (stdout `success: true, message_id: N` vs structured `telegram_result.success: False`). This is the expected pre-fix behavior, not the post-fix behavior the work-order asks to verify.

For reference, the pre-fix merge logic in the running `c3adf5b:app.py`:

```python
# _merge_task_evidence_into_envelope (c3adf5b, running)
if not merged.get("telegram_result") and evidence.get("telegram_result"):
    merged["telegram_result"] = dict(evidence["telegram_result"])
```

The post-fix logic in the dirty working tree:

```python
# _merge_task_evidence_into_envelope (working tree, NOT running)
# ... plus the early-merge block at the _executor_evidence_is_empty gate:
task_evidence = _collect_task_evidence(task_id)
if task_evidence is not None and task_evidence.get("telegram_result"):
    if not _telegram_result_is_confirmed(envelope.get("telegram_result")):
        merged = dict(envelope)
        merged["telegram_result"] = dict(task_evidence["telegram_result"])
        return merged
# ... and the relaxed guard later:
if not _telegram_result_is_confirmed(merged.get("telegram_result")) and evidence.get("telegram_result"):
    merged["telegram_result"] = dict(evidence["telegram_result"])
```

---

## Verdict

**FAIL — the live service is NOT verified to be running the intended code.**

The Telegram sync fix exists only in the uncommitted working tree (`app.py` mtime 2026-07-23 18:05:22 UTC). The running process (PID 1430846, started 2026-07-23 13:57:30 UTC, no `--reload`) is executing code as of commit `c3adf5b`, which predates the fix. The live `/openapi.json` confirms the running code is the pre-dirty-tree version (`max_rescues` absent).

**Activation is required.** Per the work-order safety constraint, I am NOT restarting the service. Awaiting operator approval to restart `hermes-runtime-bridge` via `supervisorctl --serverurl=unix:///tmp/supervisor.sock restart hermes-runtime-bridge`.

### Required operator action

1. Review the uncommitted working-tree changes (`git diff HEAD -- app.py dispatcher/db.py dispatcher/manager.py dispatcher/models.py`).
2. Approve or reject the restart.
3. If approved, the restart command is:
   ```
   supervisorctl --serverurl=unix:///tmp/supervisor.sock restart hermes-runtime-bridge
   ```
4. Post-restart, re-run this work-order's Phase 3 + Phase 4 to verify the fix is live and the structured `telegram_result` matches the actual Telegram delivery outcome.

### Hard red lines honored

- No commit, push, deploy, merge, rebase, stash, reset, clean, or unrelated changes.
- No automatic restart.
- No modification to `data/dispatcher.db` (read-only queries only).
- No modification to supervisord config.
- No Telegram send attempted (the fix is not live; a send would exercise the buggy path).

---

## Deliverables

- **Report file:** `/home/ubuntu/hermes-runtime-bridge/AEE_WO_LIVE_ACTIVATION_E2E_VERIFICATION_REPORT.md` (this file)
- **SHA256 sidecar:** `/home/ubuntu/hermes-runtime-bridge/AEE_WO_LIVE_ACTIVATION_E2E_VERIFICATION_REPORT.sha256`

### Report stats
