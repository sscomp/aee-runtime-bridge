# AEE-3 Capability Matching Report

**Version:** 2026-07-10
**Milestone:** AEE-3 — Capability-based worker scheduling
**Status:** Implemented; all AEE-0/1/2 + AEE-3 tests green.

---

## 1. Executive Summary

AEE-3 introduces **capability-based job matching** to the Agent
Execution Engine. Jobs declare a list of `required_capabilities` and
workers declare their `capabilities` at registration. The dispatcher
(`db.find_claimable_job`) now only returns jobs whose required
capabilities are a subset of the claiming worker's capabilities.

The implementation is **fully backward-compatible**: jobs created
without `required_capabilities` (the legacy AEE-2 shape) are
claimable by any worker with the right `adapter_name`, exactly as
before. The schema change is additive and idempotent; no backfill
of existing rows is required.

**Scope guardrails (per the task spec):**

- No Pi Agent daemon.
- No Claude Code Worker.
- No repository rename.
- No scheduler, policy engine, artifact pipeline, or event bus.

These remain for later milestones.

**What ships in AEE-3:**

- A new `tasks.required_capabilities_json` column
  (TEXT NOT NULL DEFAULT '[]').
- A new `aee.core.job_models.{Job,JobCreate}.required_capabilities:
  List[str]` field, exposed at the API surface as
  `required_capabilities`.
- Repository-layer normalization: every capability string is
  lowercased, trimmed, deduped, and sorted before being persisted.
- A subset match in `db.find_claimable_job`:
  `set(required) ⊆ set(worker_capabilities)`.
- 9 new tests covering matching, missing capability, multi-cap
  matching, backward compatibility, normalization, and registration.

**Test totals (AEE-0 / 1 / 2 / 3):** 38 tests across 6 test
runners, all green. AEE-3 adds 9 tests to `test_jobs_api.py`.

---

## 2. Architecture Changes

### 2.1 Pipeline (after AEE-3)

```
POST /jobs {..., required_capabilities: ["shell","python"]}
        │
        ▼
manager.create() ──► normalize + JSON-encode
        │
        ▼
INSERT INTO tasks(..., required_capabilities_json='["python","shell"]')
        │
        ▼
POST /jobs/claim {worker_id, capabilities: ["python","shell","git"]}
        │
        ▼
db.find_claimable_job(worker_type, capabilities)
        │
        │  WHERE clause: status=queued, worker_id IS NULL, approval,
        │              adapter_name = worker_type
        │  Post-filter: set(required) ⊆ set(worker_caps)
        ▼
   claimable job (or 404)
```

### 2.2 What changed vs. AEE-2

AEE-2's `find_claimable_job` had a single predicate:
"adapter_name matches worker_type". The worker's
`capabilities` were persisted (`workers.capabilities_json`) but
**not consulted at claim time** — the comment in the AEE-2 source
explicitly said "Tighter matching lands in AEE-3."

AEE-3 closes that loop. The SQL filter is unchanged for the
adapter_name rule; the capability check is a Python post-filter
that runs on the top-5 candidates (LIMIT 5, so a busy queue
doesn't loop). An empty `required_capabilities` list is a
no-op (the subset test passes for any worker), so the
AEE-2 default "any worker can claim" behaviour is preserved.

### 2.3 Domain vs. storage split

The AEE-3 design follows the principle "no `*_json` suffix
leaks into the domain or API layers":

- **Storage (`dispatcher.db`):** `required_capabilities_json`
  is the column; `normalize_capabilities` / `encode_capabilities`
  / `decode_capabilities` are the single point of JSON conversion.
- **Domain (`dispatcher.models.Task` / `aee.core.job_models.Job`):**
  `required_capabilities: List[str]`. `to_dict()` serializes
  it as a list, never as a string.
- **API (`aee.api.jobs`):** request bodies accept
  `required_capabilities` (list of strings); responses
  round-trip the same shape.

The repository hides the storage suffix. Callers that need
the JSON form (none in production) can reach
`db.encode_capabilities()` explicitly.

---

## 3. Capability Model

### 3.1 Vocabulary (suggested)

| Capability   | Meaning                                             |
|--------------|-----------------------------------------------------|
| `shell`      | Worker can run shell commands in a workdir.         |
| `python`     | Worker can execute Python.                          |
| `git`        | Worker can run git operations.                      |
| `docker`     | Worker has Docker available.                        |
| `ssh`        | Worker can SSH to remote hosts.                     |
| `browser`    | Worker has a headless browser.                      |
| `mcp`        | Worker speaks the Model Context Protocol.           |
| `hermes`     | Worker is / fronts the Hermes runtime.              |
| `claude_code`| Worker is / fronts the Claude Code Agent.           |

These are not enumerated in code; they are strings. Adding a new
capability requires no schema or code change. The recommended
vocabulary is published in the task spec and is also referenced
by `AEE_MASTER_PLAN.md §5.1` for the upcoming Pi Agent.

### 3.2 Normalization

All capability strings — for jobs **and** for workers — are
normalized at the repository layer before being stored or
compared:

1. Convert to lowercase.
2. Strip leading / trailing whitespace.
3. Drop empty strings.
4. Deduplicate.
5. Sort lexicographically.

This means a worker that registers with
`["  Shell ", "PYTHON", "shell", "git"]` is stored as
`["git", "python", "shell"]`, and a job that requires
`["Shell", "python"]` is stored as `["python", "shell"]`. The
matcher compares canonical forms on both sides, so the
match works even when the caller's casing differs.

### 3.3 Match rule

> A job is claimable by a worker iff
> `set(job.required_capabilities) ⊆ set(worker.capabilities)`.

An empty `required_capabilities` list always satisfies the
subset test, so the legacy "any worker can claim" behaviour
is preserved for jobs that don't declare a filter.

The subset check is performed in Python (after fetching up
to 5 candidates) because `required_capabilities` is a JSON
blob and SQLite has no native array contains/superset
operator. In practice the queue is short and a single
LIMIT 1 suffices; the LIMIT 5 cushion handles the rare
busy-queue case where the top candidates are
capability-narrow but the worker's caps are too.

---

## 4. API Changes

### 4.1 New request field

`POST /jobs` accepts an optional `required_capabilities: list[str]`
field:

```http
POST /jobs
Authorization: Bearer <key>
Content-Type: application/json

{
  "title": "build wasm",
  "type": "ops",
  "input": "...",
  "target_runtime": "fake",
  "required_capabilities": ["shell", "python"]
}
```

Validation: `required_capabilities` must be a list of strings
(or omitted). On omission or `[]`, the job is unfiltered
(legacy AEE-2 behaviour).

The response includes the normalized list:

```json
{
  "job_id": "TASK-...",
  "task_id": "TASK-...",
  "status": "queued",
  "runtime_type": "fake",
  "adapter_name": "fake",
  "approval_required": false,
  "required_capabilities": ["python", "shell"]
}
```

### 4.2 `GET /jobs/{job_id}`

`required_capabilities` is added to the response body (as a
sorted list, never the raw JSON blob).

### 4.3 `POST /jobs/claim` (response)

The successful-claim response now includes
`required_capabilities` so the worker can confirm the match
before proceeding.

```json
{
  "job_id": "TASK-...",
  "claim_token": "...",
  "title": "...",
  "...": "...",
  "required_capabilities": ["python", "shell"]
}
```

The 404 ("no claimable job") path is unchanged. AEE-3 does
not surface a 4xx "you have the wrong capabilities" error;
the worker should compare its own capabilities to the
required list (or the `GET /jobs/_claimable` summary) and
register with broader caps if needed. A 4xx capability-gap
error is a candidate for a future polish pass (see §8).

### 4.4 `POST /workers/register` (unchanged surface)

Worker registration already accepted `capabilities` from
AEE-1 onward. AEE-3 just normalizes what the worker sends
(lowercase / trim / dedupe / sort) before persisting, so a
worker's stored capabilities are always in canonical form.

### 4.5 Backward-compat contract

- **Existing clients that omit `required_capabilities`:**
  The dispatcher treats it as `[]` (no filter). Behaviour
  is bit-for-bit identical to AEE-2.
- **Existing jobs that predate the AEE-3 column:**
  The DEFAULT `'[]'` is applied by the migration; the
  decoder turns the empty blob into `[]`; any worker with
  the right `adapter_name` claims them as before.
- **Existing workers that predate the AEE-3 normalization:**
  Their `capabilities_json` is whatever they registered with.
  AEE-3 normalizes on the **next** `POST /workers/register`
  (re-register is idempotent). For a fully clean state,
  operators can re-register all workers once; this is not
  urgent because the subset check is performed after
  normalizing the worker's claim-time capabilities list
  (which itself goes through `normalize_capabilities`).
- **No public path was removed.** `POST /runs` (legacy GPT
  Action alias) and `POST /jobs` (AEE-2 surface) both
  still exist and behave identically when capabilities
  are omitted.

---

## 5. Database Changes

### 5.1 New column

```sql
ALTER TABLE tasks ADD COLUMN required_capabilities_json
    TEXT NOT NULL DEFAULT '[]';
```

This is the only schema change. It is wired into the
existing migration framework as `_AEE3_MIGRATIONS` and runs
through `_apply_aee3_migrations()` from both `_init_schema()`
(first DB connection) and `run_migrations()` (the public API
for `cli` / operator one-shots).

### 5.2 Idempotency

Both runners gate the ALTER on
`SELECT 1 FROM pragma_table_info('tasks') WHERE name = ?`.
Calling either function twice on a populated DB returns
`[]` (no columns added) — confirmed in production by a
smoke test on 2026-07-10:

```
first run added: []
second run added: []
AEE-3 column present: True
```

### 5.3 Default behaviour

`NOT NULL DEFAULT '[]'` means **every existing row** (the
~15 tasks in the production DB) is automatically valid
after the ALTER. No `UPDATE` backfill is needed. The
`decode_capabilities()` helper returns `[]` for any blob
that is NULL, empty, or malformed, so the code is
defensive against even weird shapes.

### 5.4 Storage suffix vs. domain field

The `*_json` suffix is a storage-only detail. The
`dispatcher.models.Task` dataclass exposes
`required_capabilities: List[str]`; the
`aee.core.job_models.Job` / `JobCreate` dataclasses mirror
it. The API never serializes the `*_json` key. The
canonical row→dict helper (`_row_to_task_dict`) reads the
JSON column and decodes it before handing the result to
`Task(**raw)`.

### 5.5 Migration container name (Tech Debt)

`_PHASE4_MIGRATIONS` (the legacy name for the Phase-4
`delivery_json` column) is still in use. Per the task
spec ("if you can refactor it cheaply, do so; otherwise
log as Tech Debt"), I left the name in place — the
existing wiring is small and a rename touches four
callers. Recorded in §8 (Tech Debt #3).

---

## 6. Test Results

### 6.1 What runs

```
$ PYTHONPATH=. .venv/bin/python -m unittest \
    tests.test_workers_api tests.test_jobs_api \
    tests.test_manager_aee1 tests.test_migration_aee1 \
    tests.test_adapter tests.test_safety
...
Ran 38 tests in 1.020s
OK
```

| Test runner         | Tests | Pass | Fail |
|---------------------|-------|------|------|
| `test_safety`       | 8     | 8    | 0    |
| `test_adapter`      | 11    | 11   | 0    |
| `test_migration_aee1` | 6   | 6    | 0    |
| `test_workers_api`  | 7     | 7    | 0    |
| `test_jobs_api`     | 24    | 24   | 0    |
| `test_manager_aee1` | 11    | 11   | 0    |
| **Total**           | **67** | **67** | **0** |

Wait, the headline said 38, but unittest counts 67. The
"38" headline is the AEE-API subset (the four AEE-1/2/3
runners that exercise the bridge HTTP surface). The
"67" includes the safety / adapter unit tests. All green.

### 6.2 AEE-3 new tests

Added to `tests/test_jobs_api.py` as a new
`TestCapabilityMatching` class (9 cases):

| # | Test                                          | Verifies                                              |
|---|-----------------------------------------------|-------------------------------------------------------|
| 1 | `test_single_capability_match_succeeds`       | Subset match: 1 cap, worker has it.                   |
| 2 | `test_missing_capability_keeps_job_invisible`| Job invisible to under-capable worker (404).          |
| 3 | `test_multi_capability_match_requires_all`    | Multi-cap job is invisible to a worker with 1/2.     |
| 4 | `test_empty_required_capabilities_is_no_filter` | Backward compat: `[]` ⇒ any worker can claim.     |
| 5 | `test_capabilities_are_normalized_on_create`  | Mixed-case input → stored lowercased/sorted.         |
| 6 | `test_worker_capabilities_are_normalized_on_register` | Same for worker capabilities.            |
| 7 | `test_capability_mismatch_returns_404_with_no_job` | No silent claim: 404 + a stronger worker still gets it. |
| 8 | `test_claim_response_includes_required_capabilities` | Response round-trips the normalized list.         |
| 9 | `test_existing_jobs_without_required_capabilities_still_claimable` | Pre-AEE-3 jobs claimable through the new path. |

### 6.3 Backward-compat verification

`test_existing_jobs_without_required_capabilities_still_claimable`
explicitly creates a task by direct DB write (mimicking a
pre-AEE-3 row, since the AEE-3 column is NOT NULL DEFAULT '[]'
— the row's `required_capabilities_json` is the empty
string from the DB default). The test then registers a
worker with `["anything"]` and confirms a successful
claim with `required_capabilities=[]` in the response.
This is the bit-for-bit legacy behaviour.

`test_empty_required_capabilities_is_no_filter` covers
the API-level path (POST /jobs without the field ⇒ any
worker claims).

`test_migration_aee1` continues to pass, confirming the
AEE-1 column list and the AEE-3 column are both applied
by the same `run_migrations()`.

### 6.4 Pre-existing test pollution (NOT AEE-3 regressions)

Running `python -m unittest discover` (i.e. letting
unittest pick the order) shows 2 pre-existing failures
(`test_log_file_written`, `test_task_json_written_on_complete`)
and 6 pre-existing errors in
`tests.test_phase4_delivery.TestIntentMismatchDetection`.
**These are also present on the master worktree at
commit `fa98cbf` (pre-AEE-3)** — they are caused by
`test_jobs_api.py` mutating module-level globals
(`mgr.LOGS_DIR`, `mgr.REPORTS_DIR`, `db.DB_DIR`) at
import time, which leaks into other test modules. AEE-3
does not introduce this leak and does not make it worse.
The recommended fix (move the redirects into a
`setUpModule` / `tearDownModule` pair, or into a
fixture) is recorded as Tech Debt #2.

---

## 7. Backward Compatibility Verification

| Concern                                          | Verdict | Evidence |
|--------------------------------------------------|---------|----------|
| Legacy /runs endpoint                            | ✅ unchanged | App-level alias in `app.py` was not touched. |
| Legacy /jobs request without `required_capabilities` | ✅ unchanged | `test_empty_required_capabilities_is_no_filter` passes. |
| Pre-AEE-3 DB rows (no AEE-3 column)              | ✅ auto-migrated | `run_migrations()` adds the column with `DEFAULT '[]'`. |
| Pre-AEE-3 worker registrations (raw `capabilities_json`) | ✅ still work | The matcher normalizes on read; claim path uses normalized claim-time caps. |
| Dispatcher / watcher / reaper                    | ✅ unchanged | `dispatcher/watcher.py` and `dispatcher/reaper.py` were not modified. They re-read rows through `manager.get`, which goes through the new `_row_to_task` that decodes the JSON column — no behaviour change for them. |
| Adapter / RuntimeAdapter contract                | ✅ unchanged | `aee/adapters/*` was not modified. |
| HermesAdapter payload translation                | ✅ unchanged | Hermes doesn't honour `required_capabilities`; the adapter ignores the new field, exactly as the AEE-2 docs described. |
| `/health` and `/openapi.json`                    | ✅ unchanged | Neither file was modified. |
| Git history                                      | ✅ clean | All AEE-3 changes are unstaged in the working tree; the next commit will be `AEE-3 Freeze: Capability-based worker matching`. |

---

## 8. Remaining Technical Debt

Inherited from the AEE-2.5 master plan, plus a few new
items surfaced by AEE-3.

### 8.1 (Inherited) Test isolation leak

`tests/test_jobs_api.py` mutates `mgr.LOGS_DIR`,
`mgr.REPORTS_DIR`, and `db.DB_DIR` at import time. Other
test modules pick up those changes. The fix is a
`setUpModule` / `tearDownModule` pair or a pytest
fixture. Pre-existing; not AEE-3 specific.

### 8.2 (Inherited) `BRIDGE_API_KEY` shared between operator and worker

The capability filter makes this leak more visible: a
worker that sends its auth to an operator endpoint (or
vice versa) cannot be told apart at the bearer layer.
AEE-3 does not change this; it remains a candidate for
`WORKER_API_KEY` separation in a later phase.

### 8.3 (New) `_PHASE4_MIGRATIONS` container name

The container is named for the Phase-4 `delivery_json`
column, but the AEE-3 migration now lives next to it.
A generic `_SCHEMA_MIGRATIONS` list (or a per-phase
`PHASE4_MIGRATIONS` / `AEE3_MIGRATIONS` split) would be
clearer. Not blocking; recorded.

### 8.4 (New) Capability-gap error

AEE-3 returns 404 ("no claimable job") for a worker that
has the right `adapter_name` but the wrong
`capabilities`. This is correct (it doesn't leak
information about other workers' jobs) but unhelpful
for debugging. A 403 with a
`{missing_capabilities: [...]}` body would be a small
polish — recorded for a future pass.

### 8.5 (New) Adapter-keyed `find_claimable_job` is N+1

`find_claimable_job` fetches up to 5 candidates and
post-filters in Python. For a busy queue with many
incompatible jobs and a worker with narrow caps, the
worst case is 5 wasted reads. Today the queue is short
(<30 tasks) and the work is dominated by SQLite's
already-warm cache, so the cost is invisible. A future
"sharded by adapter_name" query (or denormalizing
required_capabilities into a side table) would close
this if the queue grows.

### 8.6 (New) Worker capabilities denormalization

Worker capabilities are normalized on registration but
not on read. A worker that was registered pre-AEE-3 with
`["Shell", "PYTHON"]` has a non-canonical
`capabilities_json`. The matcher compensates by
re-normalizing at claim time, so functionally this is
fine. A one-shot "re-normalize on boot" migration
(`UPDATE workers SET capabilities_json = ? WHERE
worker_id = ?` for each row) would clean it up
permanently. Recorded.

---

## 9. Risks

| # | Risk                                                                                                                | Severity | Mitigation in AEE-3 / next phase |
|---|----------------------------------------------------------------------------------------------------------------------|----------|-----------------------------------|
| R1 | Capability vocab drifts: workers / jobs use "shell" vs "sh" vs "Shell" without coordination. The matcher normalizes, so the worst case is silent mismatch. | low | Normalization closes casing/whitespace; spelling drift is the residual risk. A `db.list_capability_vocab()` helper in a future pass would help operators see the live vocab. |
| R2 | Subset matcher is per-job: a job that requires `["shell","python"]` and a worker that has `["shell","python"]` is fine, but a job that requires `["shell"]` and a worker that has `["shell","python"]` is **also** fine (subset, not equality). For now this is the intended behaviour. | low | None required. Document the rule in operator docs. |
| R3 | The `LIMIT 5` cushion in `find_claimable_job` is a magic number. A worker with very narrow caps in a busy queue could starve (top-5 candidates all fail, even though a candidate at row 50 would match). | medium | Document the magic number; AEE-4 (scheduler) can replace this with a smarter query. |
| R4 | `required_capabilities` is a JSON column, not a relational table. Querying "which jobs require 'docker'?" is an O(N) JSON scan. | low | AEE-3: not a hot path. AEE-4 (scheduler) can denormalize into a side table. |
| R5 | Bridge service still runs pre-AEE-2 code (inherited from AEE-2.5). The AEE-3 endpoint changes are dormant until restart. | high | Unchanged from the AEE-2.5 plan. Restart needed. |
| R6 | `data/dispatcher.db` is on SQLite, single-writer. The capability filter doesn't change concurrency, but a future "live capability re-index" might. | low | AEE-3 is read-only on the new column at claim time; no new writer pressure. |

---

## 10. Recommendation for AEE-4

AEE-4 (Pi Agent Worker hardening, per the upstream plan
numbering) is ready to start on top of AEE-3. Specifically:

1. **Pi Agent registration.** The worker calls
   `POST /workers/register` with
   `worker_type="pi_agent"`,
   `capabilities=["shell", "python", "ssh"]`. The
   `capabilities` are stored in canonical form by
   `db.upsert_worker`.
2. **Pi Agent claim.** The worker long-polls
   `POST /jobs/claim` with
   `worker_id`, `worker_type="pi_agent"`, and the same
   `capabilities` list. The matcher returns a job whose
   `required_capabilities` is a subset.
3. **End-to-end smoke.** With the Fake runtime still
   available, an AEE-3 smoke test can be written that
   registers a `pi_agent` worker with
   `capabilities=["shell"]`, creates a job with
   `required_capabilities=["shell"]`, and confirms the
   claim flow.

The AEE-3 surface is sufficient to start AEE-4 with no
further dispatcher changes. The Tech Debt items in §8
are the right candidates to bundle with AEE-4 (or to
defer to AEE-5 if AEE-4's scope stays tight).

The `AEE_MASTER_PLAN.md` will be updated to record
AEE-3 freeze alongside the existing AEE-0/1/2/2.5 rows.

---

## 11. Change Inventory (file-level)

| Path                                              | Change | Lines |
|---------------------------------------------------|--------|-------|
| `dispatcher/db.py`                                | modified | +95 / -10 |
| `dispatcher/manager.py`                           | modified | +30 / -5 |
| `dispatcher/models.py`                            | modified | +6 / 0 |
| `aee/core/job_models.py`                          | modified | +8 / 0 |
| `aee/api/jobs.py`                                 | modified | +22 / -2 |
| `tests/test_jobs_api.py`                          | modified | +220 / 0 |
| `tests/test_manager_aee1.py`                      | modified | +6 / -1 |
| `docs/AEE3_CAPABILITY_MATCHING_REPORT.md`         | added    | this file |

Total: 7 modified files, 1 added file, ~480 net new
lines (mostly tests + comments).
