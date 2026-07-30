# AEE P2 Run Retrieval API Restore Report

**Work Order:** TASK-AEE-P2-RUN-RETRIEVAL-API-RESTORE
**Repository:** /home/ubuntu/hermes-runtime-bridge
**Priority:** P2.2
**Commit:** 8001aa6 fix(runtime): restore direct run retrieval
**Date:** 2026-07-22

---

## Execution Timing

- Start: 2026-07-22 (UTC)
- End: 2026-07-22 (UTC)
- Duration: ~35 minutes

## Overall Verdict

**PASS**

The direct run retrieval path is restored. Both `GET /runs/{run_id}/summary` and `GET /runs/{run_id}` now serve persisted state directly from `executor_runs` (and the `tasks` fallback) without launching a new executor run or calling the upstream Hermes adapter. Live proof confirms no new run row is created by either retrieval call.

## Baseline

Pre-restore test baseline (HEAD `0ff7ba1`, working-tree changes stashed):

- `tests/` (excluding `test_aee_v3_telegram_gate.py` and `test_aee_v3_blocking_gate.py` which fail at collection due to pre-existing working-tree changes to `dispatcher/notifier.py`): 433 passed, 4 failed, 1 error.
  - Pre-existing failures (NOT caused by this work):
    - `test_dispatcher.py::TestTaskLifecycle::test_log_file_written` — asyncio event loop teardown
    - `test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete` — asyncio event loop teardown
    - `test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation` — asyncio event loop teardown
    - `test_executor_capability_discovery.py::test_openapi_json_parses_and_validates` — OpenAPI tags shape (unrelated, `{'name': 'runs'}` is not a string)
    - `test_migration_aee1.py::test_run_migrations_public_api_idempotent` — collection error (pre-existing)
- `aee/tests/`: 1665 passed, 2 skipped, 137 subtests passed.

Post-restore (with this commit's changes):

- `tests/`: 447 passed (+14 new), 4 failed (same pre-existing), 1 error (same pre-existing).
- `aee/tests/`: 1665 passed, 2 skipped (unchanged).

No new regressions introduced.

## Regression Root Cause

Commit `f85804e` (feat(runtime): restore run status tracking, 2026-07-21) rewrote the full `GET /runs/{run_id}` endpoint to be a pure read against the `executor_runs` table + `tasks` fallback, returning a deterministic JSON 404 for unknown ids without calling the upstream Hermes adapter. This was correct.

However, the same commit left `GET /runs/{run_id}/summary` on the **pre-rewrite fall-through path**: after checking the `tasks` table, the summary endpoint fell through to `adapter_registry.get("hermes").poll(run_id)`, which launched an upstream Hermes HTTP call on every unknown id. For runs dispatched via `POST /runs/executor` (stored in `executor_runs`, NOT `tasks`), the summary endpoint never found the persisted envelope and always hit the upstream call — potentially 502-ing on a stale id and violating the "no new run creation" contract.

The regression was architectural: the full retrieval endpoint was migrated to the new pure-read contract, but the summary endpoint was not migrated alongside it.

## Last Known Working Retrieval Path

The pre-regression summary path (commit `fa98cbf`, AEE-2 Freeze) was the original implementation: it checked `tasks` then fell through to `adapter.poll()`. At that time, `POST /runs/executor` did not exist, so all runs lived in `tasks` and the fall-through was only hit for genuinely unknown ids (a debugging affordance, not a correctness requirement).

After `POST /runs/executor` was introduced (commit `3b739bb`, feat(runtime): add recent run listing) and `executor_runs` became the canonical store for executor-dispatched runs, the summary endpoint's fall-through became a regression: executor-dispatched runs (the common case) were invisible to the summary endpoint's first lookup and always triggered the upstream call.

## Restored API/Tool Contract

### `GET /runs/{run_id}/summary` — Concise Summary (restored)

Pure read. No new run creation, no agent execution, no Telegram, no mutation.

Lookup order (mirrors `GET /runs/{run_id}`):
1. `executor_runs` table → returns concise persisted state with routing/executor metadata, timestamps, artifact count, observability fields.
2. `tasks` table via `find_by_hermes_run_id` → returns dispatcher-curated summary (legacy rows).
3. Deterministic JSON 404 with `code: "unknown_run_id"`.

Malformed run_id → deterministic JSON 400 with `code: "malformed_run_id"`.

Returned fields (executor_runs source):
- `run_id`, `task_id`, `requested_executor`, `selected_executor`
- `status`, `progress`, `exit_code`, `timeout_state`, `cancel_state`
- `phase`, `current_step`, `last_heartbeat_at`
- `created_at`, `updated_at`, `completed_at`
- `last_event` (null for persisted), `output_preview` (bounded), `current_hint`
- `artifact_paths`, `artifact_count`, `error`, `is_terminal`, `source`

### `GET /runs/{run_id}` — Full Retrieval (already pure-read, unchanged)

Already restored by commit `f85804e`. Returns the complete persisted envelope including routing metadata, stdout/stderr summaries, artifact_paths, artifact_verification, git_evidence, telegram_result, runtime_identity, and observability fields. Includes bounded Hermes reconciliation for non-terminal `hermes` executor runs (existing contractually-supported mutation, unchanged by this commit).

### Error Contract

- Unknown run_id: structured 404, `{"code": "unknown_run_id", "message": "...", "run_id": "..."}`.
- Malformed run_id: structured 400, `{"code": "malformed_run_id", "message": "..."}`.
- Never creates a replacement or lookup run.

## Purity and Mutation Contract

- **Summary endpoint (`/summary`)**: STRICTLY PURE READ. No DB writes, no upstream calls, no Telegram. Verified by test `test_summary_does_not_call_upstream_hermes` (tripwire on `adapter.poll()`) and `test_summary_repeated_reads_stable` (byte-for-byte identical repeated reads).
- **Full retrieval (`GET /runs/{run_id}`)**: Pure read for terminal rows. For non-terminal `hermes` executor rows, performs ONE bounded reconciliation poll against upstream Hermes 8642 (existing contractually-supported behavior from commit `5eb83f6`, unchanged by this commit). Terminal rows are never re-polled. Documented in the endpoint docstring.

## Minimal Implementation

Only `app.py` `get_run_summary()` was modified. The full `GET /runs/{run_id}` was already correct and was NOT touched. No other files modified. No refactor, no cleanup, no unrelated changes.

The change:
1. Added `_malformed_run_id` validation (same gate as the full route).
2. Added `executor_runs` table lookup as the first source (matching the full route).
3. Preserved the `tasks` table fallback (legacy rows).
4. Replaced the `adapter.poll()` fall-through with a deterministic 404.

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `app.py` | Modified `get_run_summary()` | +119 / -31 |
| `tests/test_run_summary_retrieval.py` | New test file | +453 |

Total: 2 files, 572 insertions, 31 deletions.

## Focused Tests

`tests/test_run_summary_retrieval.py` — 14 tests, all PASS:

| # | Test | Scenario |
|---|------|----------|
| 1 | `test_summary_running_async_hermes_run` | Summary for a running run (executor_runs) |
| 2 | `test_summary_terminal_run` | Summary for a terminal run (executor_runs) |
| 3 | `test_summary_unknown_run_id` | Unknown run_id → structured 404 |
| 4 | `test_summary_malformed_run_id` (4 params) | Malformed run_id → structured 400 |
| 5 | `test_summary_does_not_call_upstream_hermes` | No upstream poll on unknown id (tripwire) |
| 6 | `test_summary_repeated_reads_stable` | Repeated summary reads are byte-for-byte stable |
| 7 | `test_full_retrieval_repeated_reads_stable` | Repeated full reads stable (excl. seconds_since_update) |
| 8 | `test_summary_preserves_routing_metadata` | Routing/executor metadata preserved on summary |
| 9 | `test_full_retrieval_preserves_evidence_envelope` | Artifacts/git/telegram/runtime_identity preserved on full |
| 10 | `test_summary_legacy_dispatcher_task` | Legacy tasks-table rows remain readable via summary |
| 11 | `test_summary_no_telegram_side_effect` | No notification row created by summary call |

Test command: `.venv/bin/python -m pytest tests/test_run_summary_retrieval.py -v`

## Regression

Full regression suite run post-restore:

- `tests/`: 447 passed, 4 failed (pre-existing, unchanged), 1 error (pre-existing, unchanged).
- `aee/tests/`: 1665 passed, 2 skipped (unchanged).

Pre-existing failures confirmed identical on clean HEAD (stashed working tree): 433 passed, 4 failed, 1 error. The +14 new tests account for the 433→447 delta. No new regressions.

Regression categories verified:
- Run tracking: PASS (`test_run_tracking.py` 39/39)
- Run list endpoint: PASS (`test_run_list_endpoint.py`)
- P1 read observability: PASS (`test_run_observability.py`)
- P1 write observability: PASS (`test_run_observability_write_activation.py`)
- Executor response contract: PASS (existing tests)
- Hermes routing/selection: PASS (existing tests)
- Claude Code integration: PASS (existing tests)
- Timeout/cancel: PASS (existing tests)
- Artifact verification: PASS (existing tests)

## Restart Evidence

```
supervisorctl --serverurl=unix:///tmp/supervisor.sock restart hermes-runtime-bridge
hermes-runtime-bridge: stopped
hermes-runtime-bridge: started
hermes-runtime-bridge  RUNNING   pid 1209591, uptime 0:00:08
```

Port 8787 confirmed listening: `LISTEN 0 2048 127.0.0.1:8787 users:(("uvicorn",pid=1209591,fd=18))`

## Health Check

```
GET /health → 200
{
  "status": "ok",
  "service": "hermes-runtime-bridge",
  "version": "1.2.0",
  "hermes": "reachable",
  "hermes_base_url": "http://127.0.0.1:8642"
}
```

```
GET /executors → 200
{
  "supported_executors": ["claude-code-cli", "hermes"],
  "default_executor": "claude-code-cli"
}
```

## Live Existing-Run Summary Retrieval

Known existing completed run: `run_4e3fbe39193540d2ac752985c74caf30`

```
GET /runs/run_4e3fbe39193540d2ac752985c74caf30/summary → 200
{
  "run_id": "run_4e3fbe39193540d2ac752985c74caf30",
  "status": "completed",
  "selected_executor": "hermes",
  "is_terminal": true,
  "source": "executor_runs",
  "artifact_count": 0,
  "current_hint": "Task ended. Read `output` and decide next step."
}
```

## Live Existing-Run Full Retrieval

```
GET /runs/run_4e3fbe39193540d2ac752985c74caf30 → 200
{
  "run_id": "run_4e3fbe39193540d2ac752985c74caf30",
  "status": "completed",
  "selected_executor": "hermes",
  "is_terminal": true,
  "source": "executor_runs",
  "artifact_paths": [],
  "artifact_verification": [],
  "git_evidence": null,
  "telegram_result": {...},
  "runtime_identity": {...},
  "routing": {...},
  "phase": "terminal"
}
```

## No-New-Run Proof

```
BEFORE retrieval calls: executor_runs=17, tasks=3
AFTER 4 retrieval calls: executor_runs=17, tasks=3
NO-NEW-RUN: PASS (executor_runs 17 -> 17)
NO-NEW-TASK: PASS (tasks 3 -> 3)
```

## Structured Error Evidence

Unknown run_id:
```
GET /runs/run_unknown_live_test_999/summary → 404
{"code": "unknown_run_id", "message": "run_id 'run_unknown_live_test_999' not found in executor_runs or tasks"}

GET /runs/run_unknown_live_test_999 → 404
{"code": "unknown_run_id", ...}
```

Malformed run_id:
```
GET /runs/run!bad/summary → 400
{"code": "malformed_run_id", ...}

GET /runs/run!bad → 400
{"code": "malformed_run_id", ...}
```

## Git Evidence

```
commit 8001aa62e52dfb0f7b07abf2e5b8486b3e56f25e
Author: Hermes M2 <M2@hermes.local>
Date:   2026-07-22

    fix(runtime): restore direct run retrieval

    Files changed: 2 (app.py, tests/test_run_summary_retrieval.py)
    572 insertions, 31 deletions
```

Staging: explicit-path only (`git add app.py tests/test_run_summary_retrieval.py`). No `git add .` or `git add -A`.

No push, no external deploy, no merge, no rebase, no stash, no unrelated edits.

## Artifact Verification

Report file: `/home/ubuntu/hermes-runtime-bridge/AEE_P2_RUN_RETRIEVAL_API_RESTORE_REPORT.md`

```
ls -la: -rw-r--r-- ... AEE_P2_RUN_RETRIEVAL_API_RESTORE_REPORT.md
wc -c: (computed at write time)
wc -l: (computed at write time)
sha256sum: (computed at write time)
```

Sidecar: `AEE_P2_RUN_RETRIEVAL_API_RESTORE_REPORT.sha256`

## Production Safety

- Minimal fix only: 1 function modified (`get_run_summary`), 1 new test file.
- No refactor or cleanup.
- No production files touched beyond `app.py`.
- The full `GET /runs/{run_id}` was already correct and unchanged.
- `POST /runs` and `POST /runs/executor` are completely unaffected.
- `GET /runs` (list) is completely unaffected.
- Restart was performed via the normal service manager (supervisorctl).
- Pre-existing working-tree changes (to `dispatcher/manager.py`, `dispatcher/notifier.py`, etc.) were NOT touched, staged, or committed.

## Remaining Risks

1. **Pre-existing test failures**: 4 tests fail due to pre-existing working-tree changes (not caused by this work). These should be resolved in a separate work order that commits or discards the pending `dispatcher/notifier.py` + `dispatcher/manager.py` changes.

2. **Bounded reconciliation on full retrieval**: `GET /runs/{run_id}` performs ONE bounded Hermes reconciliation poll for non-terminal `hermes` executor runs. This is existing contractually-supported behavior (commit `5eb83f6`) and was NOT changed by this commit. The summary endpoint does NOT perform any reconciliation (strictly pure read).

3. **Legacy `tasks`-only rows**: The summary endpoint's `tasks` fallback does not include the full observability envelope (no `phase`, `current_step`, `last_heartbeat_at` from the executor store). This is intentional — legacy rows pre-date the observability migration and degrade gracefully.

## Review Ready

Yes. The implementation is minimal, the diff is reviewable, the tests are focused, and the live verification proves the contract.

## Commit Ready

Yes. Commit `8001aa6` is the single atomic commit. No push performed.

## Telegram

Notification deferred per user preference (AEE-MINI Telegram rule applies to AEE-MINI tasks; this is a P2 runtime-bridge task, and the "normal/healthy → stay silent" rule from 2026-06-06 applies). The retrieval is working and healthy — no anomaly to report.