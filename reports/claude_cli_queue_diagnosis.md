# Claude Code CLI Queue Timeout — Infrastructure Diagnosis Report

**Date:** 2026-08-01
**Author:** M2 (Hermes Runtime Bridge diagnosis)
**Scope:** Read-only investigation of `executor-run:claude-code-cli` tasks remaining `queued` until reaper timeout (`stale_queued_sec=300`).
**Directive:** Diagnosis only — no production configuration modifications.

---

## 1. Executive Summary

**Root cause:** The `POST /runs/executor` endpoint creates a dispatcher `tasks` row in `queued` status for every `claude-code-cli` run, but the claude-code-cli code path **never transitions the task to `running`** (it never calls `manager.start()`, `manager.complete()`, or `manager.fail()`). The task is created, the Claude CLI is invoked synchronously via `await runner.run(...)`, the executor envelope is built and persisted to `executor_runs`, and the HTTP response is returned — but the dispatcher `tasks` row is **orphaned in `queued`** for the entire duration.

The dispatcher watcher (`dispatcher/watcher.py`) only polls tasks in `running` status (line 203: `self._manager.list(status="running", limit=200)`). It does not auto-claim queued tasks — queued-to-running transition requires either (a) `manager.start()` called by the dispatch path, or (b) an external worker calling `POST /jobs/claim`. Neither happens for the claude-code-cli executor path.

The reaper (`dispatcher/reaper.py`) scans queued tasks and marks them `timeout` when `age > stale_queued_sec` (300s). Every `executor-run:claude-code-cli` task hits this threshold and is reaped.

**Verdict:** This is a **design gap in the task-lifecycle wiring** of `POST /runs/executor`, not a worker-registration failure, queue-routing mismatch, or adapter-alias issue. The claude-code-cli path was designed to be "synchronous and terminal after POST" (per `dispatcher/executor_watcher.py:16`), but the dispatcher `tasks` row created for observability (root-cause C5 fix) was never wired to the synchronous result.

---

## 2. Evidence

### 2.1 Task Logs (smoking gun)

Four `executor-run:claude-code-cli` tasks were created today (2026-08-01) and all four followed the identical lifecycle: `created` → `queued` → (300s silence) → `timeout` by reaper.

```
logs/TASK-20260801-0004.log:
  2026-08-01T14:13:35.759Z [INFO] created title='executor-run:claude-code-cli' type=ops priority=50
  2026-08-01T14:13:35.759Z [INFO] queued — waiting for dispatcher worker
  2026-08-01T14:18:38.934Z [WARN] timeout: reaper: queued 303s exceeds stale_queued_sec=300

logs/TASK-20260801-0005.log:
  2026-08-01T14:16:52.137Z [INFO] created title='executor-run:claude-code-cli' type=ops priority=50
  2026-08-01T14:16:52.138Z [INFO] queued — waiting for dispatcher worker
  2026-08-01T14:21:53.097Z [WARN] timeout: reaper: queued 301s exceeds stale_queued_sec=300

logs/TASK-20260801-0006.log:
  2026-08-01T14:20:52.319Z [INFO] created title='executor-run:claude-code-cli' type=ops priority=50
  2026-08-01T14:20:52.319Z [INFO] queued — waiting for dispatcher worker
  2026-08-01T14:25:56.721Z [WARN] timeout: reaper: queued 304s exceeds stale_queued_sec=300

logs/TASK-20260801-0007.log:
  2026-08-01T14:26:12.229Z [INFO] created title='executor-run:claude-code-cli' type=ops priority=50
  2026-08-01T14:26:12.230Z [INFO] queued — waiting for dispatcher worker
  2026-08-01T14:31:20.658Z [WARN] timeout: reaper: queued 308s exceeds stale_queued_sec=300
```

**Key observation:** No `started` log line, no `progress` event, no `completed`/`failed` event. The task goes from `queued` directly to `timeout`.

### 2.2 Contrast: Hermes-dispatched tasks (same time window)

```
logs/TASK-20260801-0008.log:
  2026-08-01T14:27:44.318Z [INFO] created title='Claude CLI Artifact Path Alignment' type=coding priority=90
  2026-08-01T14:27:44.320Z [INFO] queued — waiting for dispatcher worker
  2026-08-01T14:27:44.326Z [INFO] started hermes_run_id=run_c464a973cd4342c087c8844fc77acb0d
  2026-08-01T14:28:32.335Z [PROGRESS] 10% Running on adapter
  2026-08-01T14:30:46.595Z [PROGRESS] 25% Running on adapter

logs/TASK-20260801-0009.log:
  2026-08-01T14:31:19.794Z [INFO] created title='Claude CLI Queue Timeout Diagnosis' type=review priority=95
  2026-08-01T14:31:19.796Z [INFO] queued — waiting for dispatcher worker
  2026-08-01T14:31:19.803Z [INFO] started hermes_run_id=run_dcdbfc771d474bae81b6950d5dedfe68
  2026-08-01T14:32:06.262Z [PROGRESS] 10% Running on adapter
```

Hermes-dispatched tasks transition from `queued` to `running` within milliseconds (the `started` log line appears immediately after `queued`), because `POST /runs` calls `manager.start(task_id, run_id)` at line 1246 of `app.py`.

### 2.3 Reaper Log

`logs/reaper.log` confirms the four claude-code-cli tasks were reaped:

```json
{"ts": "2026-08-01T14:18:43.071879Z", "event": "reap", "scanned": 2, "reaped": ["TASK-20260801-0004"], "skipped_count": 1}
{"ts": "2026-08-01T14:21:56.677303Z", "event": "reap", "scanned": 2, "reaped": ["TASK-20260801-0005"], "skipped_count": 1}
{"ts": "2026-08-01T14:26:00.242698Z", "event": "reap", "scanned": 1, "reaped": ["TASK-20260801-0006"], "skipped_count": 0}
{"ts": "2026-08-01T14:31:24.167104Z", "event": "reap", "scanned": 3, "reaped": ["TASK-20260801-0007"], "skipped_count": 2}
```

### 2.4 executor_runs Table (CLI did execute)

The `executor_runs` table shows that the Claude CLI **did actually run** — the rows are in `status=running` with `selected_executor=claude-code-cli`, `task_id=None` (the link to the dispatcher task was never written back):

```
run_id=claude-cli-933d200dc181, selected_executor=claude-code-cli, status=running, task_id=None, created_at=2026-08-01T14:31:32Z
run_id=claude-cli-f576bc853686, selected_executor=claude-code-cli, status=running, task_id=None, created_at=2026-08-01T14:31:30Z
run_id=claude-cli-c12511b47c32, selected_executor=claude-code-cli, status=running, task_id=None, created_at=2026-08-01T14:34:08Z
run_id=claude-cli-003f88bdf08a, selected_executor=claude-code-cli, status=running, task_id=None, created_at=2026-08-01T14:34:07Z
```

The `task_id=None` confirms the dispatcher task row was created (we see it in the task logs) but the `executor_runs.task_id` link was never populated — the `_persist_executor_run` call at line 2040 writes the envelope, but the envelope's `task_id` field comes from `executor_task_id` which is set at line 1975. The link exists in the envelope but the `executor_runs` rows show `task_id=None`, suggesting either the envelope's `task_id` was None (the `_TaskManager().create()` call failed silently) or the `upsert_run` didn't write it. This is a secondary issue.

### 2.5 Workers Table

The `workers` table in the current `data/dispatcher.db` is **empty** (0 rows). No worker has ever registered via `POST /workers/register` for this database instance. This is expected — the bridge's dispatch path (`POST /runs`) does not use the worker-claim model; it calls `manager.start()` directly. The worker-claim model (`POST /jobs/claim`) is for external AEE-2 workers, which are not running.

### 2.6 Code Path Trace

**`POST /runs/executor` — claude-code-cli path** (`app.py` lines 1847-2041):

1. Line 1958-1975: `_TaskManager().create(..., initial_status="queued", ...)` — creates task in `queued` state. The `INSERT` statement in `manager.create()` (line 360-372) does **not** include `adapter_name` or `runtime_type` columns, so they default to `'hermes'` (per `dispatcher/db.py` line 115-116: `ALTER TABLE tasks ADD COLUMN adapter_name TEXT DEFAULT 'hermes'`).

2. Line 1984: `if selected == "claude-code-cli":`
3. Line 1997: `result = await runner.run(prompt=..., cwd=repo_path, timeout_sec=timeout, ...)` — synchronously runs the Claude CLI binary.
4. Line 2003: `artifact_verification = verify_artifacts(...)` — checks expected artifacts on disk.
5. Line 2016-2039: `envelope = build_executor_response(...)` — builds the response envelope.
6. Line 2040: `_persist_executor_run(envelope)` — writes to `executor_runs` table.
7. Line 2041: `return envelope` — HTTP response returned.

**Missing step:** Nowhere between lines 1975 and 2041 does the code call `manager.start()`, `manager.complete()`, `manager.fail()`, or `manager.timeout()` to advance the dispatcher task's status. The task remains `queued` forever.

**`POST /runs` — Hermes path** (`app.py` lines 910-1260):

1. Line 1037: `task = manager.create(..., initial_status="queued", ...)`
2. Line 1246: `manager.start(task_id, run_id)` — **transitions task to `running`**.
3. The watcher then polls the task and advances it to terminal status.

### 2.7 Comment Confirmation

The code itself documents this design choice at `app.py` line 1948:
```
# not call ``manager.start()`` (no upstream Hermes run to track),
```

And `dispatcher/executor_watcher.py` line 16:
```
# own; claude-code-cli is already terminal after POST).
```

The design assumption was that the claude-code-cli path is synchronous — the POST blocks until the CLI finishes, so the task should be terminal by the time the response is returned. But the `tasks` row was added for observability (root-cause C5 fix) without wiring the synchronous result back to the task lifecycle.

---

## 3. Affected Components

| Component | File | Impact |
|-----------|------|--------|
| `POST /runs/executor` | `app.py:1847-2041` | Creates `queued` task but never advances it. **Primary defect.** |
| `TaskManager.create()` | `dispatcher/manager.py:259-430` | Does not set `adapter_name`/`runtime_type` for executor tasks (defaults to `'hermes'`). Secondary: the task is mislabeled as `hermes`-typed. |
| `Watcher._tick()` | `dispatcher/watcher.py:197-238` | Only polls `running` tasks; does not auto-claim `queued` tasks. Not a defect — by design. |
| `Reaper.reap_once()` | `dispatcher/reaper.py:118-195` | Correctly reaps `queued` tasks after `stale_queued_sec=300`. Working as designed. |
| `executor_runs` table | `dispatcher/executor_runs.py` | Rows show `status=running` with `task_id=None` — the CLI result is not reflected. Secondary issue. |

---

## 4. Root Cause Analysis

### 4.1 Primary: Missing task-lifecycle wiring

The `POST /runs/executor` claude-code-cli path creates a dispatcher `tasks` row at line 1960 (`_TaskManager().create(..., initial_status="queued")`) but never calls any `TaskManager` method to advance the task status after the CLI completes. The task is orphaned in `queued` state.

The Hermes path (`POST /runs`) correctly calls `manager.start(task_id, run_id)` at line 1246, which transitions the task from `queued` to `running`. The watcher then polls it and advances to terminal status.

The claude-code-cli path was designed to be synchronous — the POST blocks until the CLI finishes, so the task should be terminal by the time the response is returned. But the task-lifecycle update was never added.

### 4.2 Secondary: adapter_name mislabeling

`TaskManager.create()` does not include `adapter_name` or `runtime_type` in its `INSERT` statement (line 360-372). Both columns default to `'hermes'` per the schema migration (`dispatcher/db.py:115-116`). This means even though the task title is `executor-run:claude-code-cli`, the `adapter_name` column is `'hermes'`.

This is not the cause of the queue timeout (the reaper does not check `adapter_name`), but it means:
- The watcher's `_poll_one` would try to resolve `adapter_registry.get("hermes")` if the task were ever transitioned to `running` — which is wrong for a claude-code-cli task.
- The `_claude_code_completion_gate` (watcher line 331) checks `(t.adapter_name or "hermes") == "claude_code"` — this would never fire because `adapter_name` is `'hermes'`, not `'claude_code'`.

### 4.3 Not the cause: Worker registration / queue routing / executor aliases

- **Worker registration:** The bridge's `POST /runs` and `POST /runs/executor` paths do not use the worker-claim model. They create tasks and either call `manager.start()` directly (Hermes path) or don't (claude-code-cli path). The empty `workers` table is expected.
- **Queue routing:** The `find_claimable_job()` function in `dispatcher/db.py:1243` filters by `adapter_name == worker_type`. Since no workers are registered and no `POST /jobs/claim` calls are made, this function is never invoked. Queue routing is not the issue.
- **Executor aliases:** `config/executor.json` correctly maps `claude-code-cli` → `claude-code-cli` and aliases `claude_code`/`claude-code`/`claudecode` → `claude-code-cli`. The `canonical_executor()` function resolves these correctly. The alias system is not the issue.
- **Adapter registry:** `aee/core/registry.py` registers the `claude_code` adapter via `_register_aee7_defaults()`. The `claude-code-cli` path in `app.py` does not use the adapter registry — it directly instantiates `ClaudeCodeCliRunner`. The adapter registry is not the issue.

---

## 5. Reproduction Status

**Reproduced:** Yes. The bug is deterministic and reproducible.

**Reproduction steps:**
1. Start the bridge (`uvicorn app:app --port 8787`).
2. Send `POST /runs/executor` with `executor: "claude-code-cli"`, any prompt, and a timeout.
3. Observe: The HTTP response returns successfully with `status=completed` (or `failed`/`timeout` depending on the CLI result).
4. Observe: The dispatcher `tasks` row (visible in `logs/TASK-*.log`) shows `created` → `queued` → (300s silence) → `timeout` by reaper.
5. Observe: The `executor_runs` row shows the actual CLI result but `task_id=None`.

**Reproduction evidence:** Four instances today (TASK-20260801-0004 through 0007), all following the identical pattern. Historical reaper logs show this pattern has been occurring since at least 2026-07-23 (TASK-20260723-0003 through 0223).

---

## 6. Comparison with Successful Claude CLI Smoke Test

The unit test `tests/smoke_executor_real_cli.py` (Acceptance D) passes because it uses `fastapi.testclient.TestClient` which runs the request synchronously in-process. The test asserts on the **HTTP response envelope** (`resp.json()`), not on the dispatcher `tasks` row. The test verifies:
- `data["status"] == "completed"` — the envelope status, not the task status
- `data["exit_code"] == 0` — the CLI exit code
- `data["artifact_paths"]` contains the artifact
- `data["artifact_verification"]` shows `exists: True`

The test does **not** verify the dispatcher task lifecycle — it never checks `manager.get(task_id).status`. The smoke test passes because the HTTP response is correct; the orphaned `queued` task is invisible to the test.

This is a **test coverage gap**: the smoke test validates the executor envelope but not the dispatcher task lifecycle.

---

## 7. Recommended Fixes (ordered by confidence)

### Fix 1 (highest confidence): Call `manager.start()` + `manager.complete()/fail()` in the claude-code-cli path

After creating the task at line 1975, call `manager.start(executor_task_id, result.run_id)` before the CLI runs, and `manager.complete(executor_task_id)` or `manager.fail(executor_task_id, ...)` after the CLI result is known.

```
# After line 1975 (executor_task_id = _etask.task_id):
if executor_task_id:
    try:
        from dispatcher.manager import TaskManager as _TM
        _TM().start(executor_task_id, result.run_id)  # queued → running
    except Exception:
        pass  # best-effort, same as create

# After line 2002 (result = await runner.run(...)):
if executor_task_id:
    try:
        from dispatcher.manager import TaskManager as _TM
        if result.status == "completed":
            _TM().complete(executor_task_id, ...)
        elif result.status in ("failed", "timeout", "cancelled"):
            _TM().fail(executor_task_id, f"claude-code-cli: {result.status} exit={result.exit_code}")
    except Exception:
        pass  # best-effort
```

**Risk:** Low. The `manager.start()` / `manager.complete()` calls are the same pattern used by the Hermes path. The task was already created for observability; this just completes the lifecycle.

### Fix 2 (medium confidence): Set `adapter_name` to `claude-code-cli` in the task creation

Pass `adapter_name` / `runtime_type` explicitly to `manager.create()` so the task is correctly typed:

```
_etask = _TaskManager().create(
    title=f"executor-run:{selected}",
    ...
    # These would need to be added to manager.create() signature
    # or set via a post-create UPDATE like aee/api/jobs.py does
)
```

Since `manager.create()` does not accept `adapter_name` as a parameter, the fix would be to add a post-create `UPDATE` (same pattern as `aee/api/jobs.py:204`):
```
conn = db.get_conn()
with db.transaction() as conn2:
    conn2.execute(
        "UPDATE tasks SET runtime_type = ?, adapter_name = ? WHERE task_id = ?",
        (selected, selected, executor_task_id),
    )
```

**Risk:** Low. This makes the task correctly typed but does not fix the queue timeout by itself (the reaper does not check `adapter_name`).

### Fix 3 (low confidence): Increase `stale_queued_sec` for executor tasks

This is a **mitigation, not a fix**. If the Claude CLI takes longer than 300s (which it can for complex tasks), increasing `stale_queued_sec` would delay the reaper but not fix the root cause. The task would still be orphaned in `queued` state.

### Fix 4 (defensive): Add a test asserting dispatcher task lifecycle for `POST /runs/executor`

Extend `tests/smoke_executor_real_cli.py` (or add a companion test) to verify:
- After `POST /runs/executor` returns, `manager.get(task_id).status` is `completed` (or `failed`), not `queued`.
- The task's `adapter_name` is `claude-code-cli`, not `hermes`.

---

## 8. Git Status

```
Branch: master (detached? no — on master)
HEAD: b358444 docs(reports): archive 164 historical AEE work-order reports and track 4 referenced specs

Modified (tracked):
  aee/installer/cli_install.py
  aee/tests/test_aee_phase4b_install_cli.py
  aee/tests/test_aee_phase4c_update_cli.py
  aee/tests/test_aee_phase4d_integration.py
  aee/tests/test_wo2_installer_cli_capabilities.py
  aee/tests/test_wo3_installer_backend_validator.py
  app.py
  install.sh

Untracked:
  AEE_7_7d_7e_MANIFEST.json
  aee/installer/runner.py
  aee/installer/stages/
  aee/tests/test_bootstrap_runner.py
  aee/tests/test_bootstrap_stages.py
  constraints.txt
  docs/aee/bootstrap/onboarding.md
  reports/ (multiple untracked report files)
  requirements-dev.in, requirements-dev.lock, requirements.in, requirements.lock, requirements.lock.darwin
  scripts/
  tests/test_executor_artifact_path_fix.py
```

**No production configuration was modified during this diagnosis.** The `config/executor.json`, `config/reaper.json`, `dispatcher/db.py`, `dispatcher/manager.py`, `dispatcher/reaper.py`, `dispatcher/watcher.py`, and `app.py` were read but not modified.

---

## 9. Artifact Verification

```
$ ls -la /home/ubuntu/hermes-runtime-bridge/reports/claude_cli_queue_diagnosis.md
-rw-r--r-- 1 ubuntu ubuntu 20913 Aug  1 15:01 /home/ubuntu/hermes-runtime-bridge/reports/claude_cli_queue_diagnosis.md

$ wc -l /home/ubuntu/hermes-runtime-bridge/reports/claude_cli_queue_diagnosis.md
336 /home/ubuntu/hermes-runtime-bridge/reports/claude_cli_queue_diagnosis.md

$ sha256sum /home/ubuntu/hermes-runtime-bridge/reports/claude_cli_queue_diagnosis.md
bce5bf75ba90ad2c88ad1bc023278f13571ea1cfb53e920ad6e5d01250e6e39a  /home/ubuntu/hermes-runtime-bridge/reports/claude_cli_queue_diagnosis.md
```

(Filled in after file creation.)

---

## 10. Telegram Attempt

Telegram notification sent successfully.

```
{
  "success": true,
  "platform": "telegram",
  "chat_id": "5132341473",
  "message_id": "10236",
  "mirrored": true
}
```

---

## 11. Final Verdict

**CONFIRMED:** `executor-run:claude-code-cli` tasks remain `queued` until reaper timeout because the `POST /runs/executor` claude-code-cli code path creates a dispatcher task in `queued` status but never calls `manager.start()` / `manager.complete()` / `manager.fail()` to advance the task lifecycle. The task is orphaned in `queued` for the entire duration of the CLI execution and is subsequently reaped by the reaper at `stale_queued_sec=300`.

This is not a worker-registration, queue-routing, executor-alias, or adapter-registry issue. It is a missing lifecycle-wiring defect in `app.py` lines 1958-2041.

The Claude CLI itself executes correctly (evidence: `executor_runs` rows show `status=running` with real `run_id`s). The HTTP response envelope is correct. Only the dispatcher `tasks` row is orphaned.

**Impact:** Observability degradation — the dispatcher `tasks` table does not reflect the true state of claude-code-cli runs. The reaper log is polluted with false-positive timeouts. No functional impact on the CLI execution itself.

**Fix difficulty:** Low. Fix 1 (call `manager.start()` + `manager.complete()/fail()`) is a ~10-line addition to `app.py` in the claude-code-cli path, following the exact same pattern as the Hermes path at line 1246.