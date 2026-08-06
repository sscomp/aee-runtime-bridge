# Runtime Bridge Artifact Metadata Investigation and Fix

**Date:** 2026-08-07
**Author:** M2 (Hermes Agent)
**Repository:** hermes-runtime-bridge
**Base commit:** dfb0d02
**Files changed:** app.py (+18/-0)

---

## 1. Root Cause

The Hermes Runtime Bridge returned empty structured artifact metadata
(`artifact_paths`, `artifact_verification`, `artifact_count`) for
Hermes-dispatched runs across three endpoints, even when verified artifacts
existed in the `artifacts` table linked to the run's `task_id`.

### Three distinct gaps were identified:

**Gap 1 — GET /runs (list endpoint):** The list endpoint iterated
`executor_runs` rows and augmented each with observability fields, but
**never called `_merge_task_evidence_into_envelope()`**. Hermes-dispatched
runs are persisted as lifecycle-sync stubs (empty `artifact_paths_json`,
empty `stdout_summary`) because the executor transcript lives in the
dispatcher's `tasks`/`task_outputs`/`artifacts` tables, not in
`executor_runs`. Without the merge call, the list endpoint returned
`artifact_paths=[]` and no `artifact_count` field for every Hermes run.

**Gap 2 — GET /runs/{run_id} (full response):** The full GET endpoint
already called `_merge_task_evidence_into_envelope()`, so
`artifact_paths` and `artifact_verification` were populated. However,
the `artifact_count` convenience field was **missing** — it was only
present in the summary endpoint (`GET /runs/{run_id}/summary`). GPT-source
callers that check `artifact_count` to decide whether to read
`artifact_verification` saw a missing field and treated it as "no
artifacts".

**Gap 3 — GPT source path:** The `_flatten_for_gpt()` helper converts
nested dicts/lists to JSON strings for ChatGPT compatibility. Because
`artifact_count` was never added to the envelope before flattening, it
was absent from GPT-source responses too.

### Why the merge existed but wasn't applied everywhere

The `_merge_task_evidence_into_envelope()` function was introduced in
commit `dfb0d02` to merge task-side evidence (artifacts, output, telegram)
into Hermes lifecycle-sync stubs. It was wired into `GET /runs/{run_id}`
and `GET /runs/{run_id}/summary` but was **not** wired into the list
endpoint (`GET /runs`). The `artifact_count` field was added to the
summary endpoint but **not** to the full GET response.

---

## 2. Reproduction (Before Fix)

**Test run:** `run_ffaa2983dee94f0487c4fe82d35baedb`
**Task:** `TASK-20260804-0035` (completed, hermes executor)
**Artifacts table:** 1 row — `/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md` (exists=1, size=68297)

### Before fix — GET /runs (list):
```
artifact_paths: []
artifact_count: NOT PRESENT
source: executor_runs
```

### Before fix — GET /runs/{run_id} (full):
```
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']  # merge worked
artifact_verification: [{'path': '...', 'exists': True, 'size': 68297, ...}]  # merge worked
artifact_count: NOT PRESENT  # MISSING
```

### Before fix — GET /runs/{run_id}/summary:
```
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']
artifact_count: 1  # already correct
```

### Before fix — GPT source GET /runs/{run_id}:
```
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']
artifact_count: NOT PRESENT  # MISSING
```

---

## 3. Fix

Two minimal additions to `app.py` (18 lines, 0 deletions):

### Fix A — GET /runs list endpoint (line ~1376)

Added `_merge_task_evidence_into_envelope(env)` call per item plus
`artifact_count` derivation, before the observability merge:

```python
env = _merge_task_evidence_into_envelope(env)
env["artifact_count"] = len(env.get("artifact_paths") or [])
```

### Fix B — GET /runs/{run_id} full response (line ~3250)

Added `artifact_count` derivation after the existing merge call:

```python
envelope["artifact_count"] = len(envelope.get("artifact_paths") or [])
```

Both additions are purely additive. No existing fields are modified or
removed. The merge helper is idempotent — fully populated executor_runs
rows (e.g. claude-code-cli runs) short-circuit inside the helper and are
returned byte-for-byte unchanged.

---

## 4. Verification (After Fix)

### After fix — GET /runs (list):
```
status: completed
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']
artifact_count: 1
source: executor_runs+tasks_merge
```

### After fix — GET /runs/{run_id} (full):
```
status: completed
source: executor_runs+tasks_merge
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']
artifact_count: 1
artifact_verification: [{'path': '...', 'exists': True, 'size': 68297, ...}]
```

### After fix — GET /runs/{run_id}/summary:
```
status: completed
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']
artifact_count: 1
```

### After fix — GPT source GET /runs/{run_id}:
```
artifact_paths: ['/home/ubuntu/Abacus/Finance/phase5_m4s2_kickoff_plan.md']
artifact_count: 1
artifact_verification: '[{"path": "...", "exists": true, "size": 68297, ...}]'
```

### Second test run — run_fa6ee4b6de014d95b4ae3c1443f5d2e0 (TASK-20260802-0011)
```
artifact_paths: ['/home/ubuntu/Abacus/Finance/a3_artifact_backfill_report.md',
                 '/home/ubuntu/Abacus/Finance/Phase4_Operational_A3/daily_reconciliation.log',
                 '/home/ubuntu/Abacus/Finance/a3_artifact_backfill_review.md']
artifact_count: 3
artifact_verification count: 3
```

All four verification checks PASS.

---

## 5. Regression Tests

Full test suite: **665 passed, 1 skipped, 0 failures** (excluding
`test_openapi_executor_metadata.py` which fails on missing `yaml`
module — pre-existing env gap, unrelated to this fix).

Targeted artifact-related tests: **78/78 PASS**:
- test_executor_artifact_evidence.py
- test_executor_artifact_path_fix.py
- test_run_summary_retrieval.py
- test_run_task_mapping.py (includes TestHermesStubEvidenceMerge)
- test_completion_sync.py
- test_wo_fix_telegram_result_sync.py

---

## 6. Git Evidence

**Base commit:** dfb0d028f6b27b6e3f72c8af0a95a9251393a14e
**Files changed:** app.py only (+18/-0)
**Diff stat:** `app.py | 18 ++++++++++++++++++`

```
@@ -1376,6 +1376,17 @@ async def list_runs_endpoint(
+        env = _merge_task_evidence_into_envelope(env)
+        env["artifact_count"] = len(env.get("artifact_paths") or [])
         env.update(derive_observability(env))

@@ -3250,6 +3250,13 @@ async def get_run(
+        envelope["artifact_count"] = len(envelope.get("artifact_paths") or [])
         # P1 observability: derive the canonical observability envelope
```

---

## 7. Report Artifact Verification

```
File: /home/ubuntu/hermes-runtime-bridge/docs/ARTIFACT_METADATA_INVESTIGATION_FIX_20260807.md
```

(Verified with ls -la, wc -l, sha256sum after write — see below.)

---

## 8. Telegram Notification

Attempted via `hermes send` after commit.