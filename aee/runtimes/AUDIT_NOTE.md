# AEE-5 Repository Audit Note

**Date:** 2026-07-10
**Auditor:** Claude Code
**Scope:** AEE-4 → AEE-5 transition

---

## 1. Existing inventory (AEE-1 → AEE-4)

### 1.1 Task / Run / Worker data model

- **Persisted in SQLite** (`dispatcher/db.py`):
  - `tasks` table — every AEE Job is a row here. The dispatcher
    originally used `hermes_run_id` only; AEE-1 added
    `runtime_type`, `adapter_name`, `external_run_id`, `worker_id`,
    `heartbeat_at`, `claim_token_hash`, `approval_required`,
    `approval_state`. AEE-3 added
    `required_capabilities_json` (normalized subset matcher).
  - `task_events` — audit log of every state change.
  - `task_outputs` — terminal result + delivery envelope (Phase 4).
  - `workers` table — registered workers. AEE-2 added it; AEE-4
    added 10 metadata columns + `status` (5 canonical values
    plus `unknown`).
- **Domain models**:
  - `dispatcher.models.Task` — SQLite-shaped dataclass; the
    canonical job record. It carries `required_capabilities`.
  - `aee.core.job_models.Job` / `JobCreate` — runtime-neutral
    Job model. Used by adapters. Already exposes
    `runtime_type`, `adapter_name`, `spec`,
    `required_capabilities`.
  - `aee.core.registry.WorkerRecord` — in-memory worker
    description; AEE-2 only used the DB-backed version.

### 1.2 Queue / dispatch flow

1. Caller hits `POST /runs` (legacy) **or** `POST /jobs`
   (AEE-2). Both paths converge on
   `dispatcher.manager.TaskManager.create()`, which writes a
   `tasks` row.
2. Adapter is resolved by `adapter_registry.get(adapter_name)`
   in `app.py:618` (for `/runs`) or by direct call in
   `aee/api/jobs.py`. Adapters speak the `RuntimeAdapter`
   protocol (`aee.adapters.base`).
3. Worker `POST /jobs/claim` returns one row from
   `db.find_claimable_job(worker_type, capabilities)`. The
   dispatcher writes `claim_token_hash` and the worker_id.
4. The worker's runtime daemon
   (`aee-runtime/aee_runtime.py`) is the **only** existing
   conformant AEE-4 runtime. It registers with
   `worker_type="aee_lightweight"`, and is the built-in
   Runtime AEE-5 must auto-register.

### 1.3 API surface (existing)

- `POST /runs`, `GET /runs/{id}` — GPT-Action compat
  (`app.py`).
- `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/claim`,
  `POST /jobs/{id}/{heartbeat,logs,complete,fail,cancel}` —
  AEE-2 (FastAPI in `aee/api/jobs.py`).
- `POST /workers/register`, `POST /workers/{id}/heartbeat`,
  `GET /workers`, `GET /workers/{id}` —
  AEE-2 / AEE-4 (FastAPI in `aee/api/workers.py`).
- All `/jobs/...` and `/workers/...` are re-mounted under
  `/v1/...` aliases in `aee/api/__init__.py`.

### 1.4 Config loading

- Bridge reads `BRIDGE_API_KEY`, `DISPATCHER_API_KEY`,
  `HERMES_BASE_URL`, etc. directly from env.
- `aee-runtime/aee_runtime.py` uses PyYAML to load
  `config.example.yaml`. **The bridge has no general YAML
  config layer.**
- `dispatcher.config.*` is the closest existing structure
  (JSON files under `dispatcher/config/*.json`) but is
  consumed only by the research module, not by the runtime
  path.

### 1.5 Database / repository abstraction

- All DB I/O goes through `dispatcher/db.py` with
  thread-local connection + `transaction()` context
  manager. AEE-5 will follow the same pattern.
- `_AEE1_MIGRATIONS`, `_AEE3_MIGRATIONS`, `_AEE4_MIGRATIONS`
  are the precedent for AEE-5's own migration: list of
  `(column, ALTER, default)` triples, applied
  idempotently via `pragma_table_info`.

### 1.6 Test entry points (AEE-4 baseline)

- 59 unittest TestCases
- 3 Node tests (in `aee-runtime/runtime/tests/`)
- 46 script-style assertions (AEE-0/AEE-1 shell runners)
- Total: 108 assertions

Test entry points:
- `tests/test_dispatcher.py` — 21 tests
- `tests/test_jobs_api.py` — AEE-2 jobs API
- `tests/test_workers_api.py` — AEE-2/AEE-4 workers API
- `tests/test_migration_aee1.py` — schema migration
- `tests/test_manager_aee1.py` — Job model + claim
- `tests/test_adapter.py` — RuntimeAdapter protocol
- `tests/test_routing.py` — client routing
- `tests/test_safety.py` — safety
- `tests/test_phase2.py`, `test_phase4_delivery.py` —
  intent-mismatch detection
- `aee-runtime/tests/test_aee_runtime.py` — 11 tests
- `aee-runtime/tests/test_smoke.py` — closed-loop smoke
- `aee-runtime/tests/test_live_provider.py` — 1
  operator-only live provider test (skipped by default)

> **Pre-existing test discovery issue noted:** When
> `unittest discover` is run with a clean DB,
> `tests/test_dispatcher.py` deletes the DB at import time,
> which causes `tests/test_phase4_delivery.py` (which runs
> later in alphabetical order) to fail because
> `task_events` no longer exists when its `addCleanup`
> tries to delete rows. This is a pre-existing test
> isolation issue **not introduced by AEE-5**; AEE-5
> preserves the per-module behaviour (each test file
> passes individually) and reports the issue in the
> AEE-5 Test Report.

---

## 2. AEE-5 design decisions (informed by the audit)

1. **No schema migration of `tasks`.** AEE-5 introduces
   **new** `runtimes` and `dispatch_records` tables;
   existing `tasks` columns stay intact. `Job` gets a
   new optional `runtime_requirements` field; old
   callers that don't pass it get the default
   `aee-lightweight-local` Runtime selected.
2. **Runtime descriptors are the source of truth.** All
   Runtime metadata (capabilities, labels, limits,
   health, enabled) lives in the `runtimes` table — not
   on `workers`. The pre-existing `workers` table stays
   the AEE-2 worker fleet registry; AEE-5 is
   deliberately one layer above.
3. **Selector is a pure function** (`select(task, runtimes)
   -> RuntimeSelectionResult` or `AEE_RUNTIME_NOT_FOUND`).
   The dispatch service in `aee/dispatch/service.py` is
   the only caller; controllers and adapters do not
   re-implement matching.
4. **Built-in `aee-lightweight-local`** is auto-registered
   at app startup (mirrors the existing
   `bootstrap_defaults()` pattern for adapter_registry).
5. **No new auth surface.** AEE-5 re-uses the existing
   `BRIDGE_API_KEY` bearer (AEE-2 already
   re-uses it for the admin endpoints). The new
   `/v1/runtimes` admin endpoints sit behind the same
   `_require_auth()` as `/v1/jobs` and `/v1/workers`.
6. **Storage abstraction.** The repository in
   `aee/runtimes/repository.py` is the single
   SQLite-aware module. The service in
   `aee/runtimes/registry.py` is the public API for the
   rest of AEE. Tests can substitute a fake
   `RuntimeRepository` to keep unit tests
   DB-free if desired.
7. **Backward compat.** AEE-4's `adapter_registry` (an
   in-memory Protocol-based registry) is **not**
   replaced. It is the lower layer (Adapter ↔ Runtime
   instance); AEE-5's Runtime Registry is the higher
   layer (Runtime metadata + selection). The two
   registries will be wired together in
   `bootstrap_defaults()`.
8. **No Pi Agent re-introduction.** AEE-5 does not
   re-introduce `pi-agent`, `pi_agent`, `runtime.pi`,
   or `PiWorker`. A regression test scans the
   repository and fails the build on any of those
   tokens (with an explicit allowlist for the
   historical migration notes).

---

## 3. Module layout (delivered)

```text
aee/
  runtimes/
    models.py        # RuntimeDescriptor, capabilities, limits, health, ...
    errors.py        # RuntimeNotFoundError + structured error code
    repository.py    # SQLite repository (CRUD, queries)
    registry.py      # Service over the repository (public API)
    selector.py      # Deterministic selector
    health.py        # Health model + helpers
    builtins/
      aee_lightweight.py  # Built-in Runtime descriptor factory
  dispatch/
    service.py       # Dispatch integration: select_runtime + write DispatchRecord
  config/
    runtime_config.py  # YAML loader for `runtimes:` block
  api/
    runtimes.py      # /v1/runtimes/* admin & query endpoints

aee/tests/         # unit tests (DB-free where possible)
tests/             # integration tests (DB-backed)
```

## 4. What AEE-5 does NOT do

- Does not build new Runtime adapters (only the descriptor
  is registered). Adapter implementation is AEE-6+
  territory (e.g. Claude Code, Shell, HTTP, Container,
  Remote Worker).
- Does not replace the `adapter_registry`. Adapters
  remain the wire-protocol layer; AEE-5's Runtime
  Registry is the policy / metadata layer above it.
- Does not touch Artifact, Scheduler, or Workflow DAG.
- Does not add new auth.
