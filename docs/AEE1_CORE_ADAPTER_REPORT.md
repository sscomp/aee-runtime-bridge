# AEE-1 Core & HermesAdapter Report

**Date:** 2026-07-10
**Phase:** AEE-1 — extract Core + `HermesAdapter` so `app.py` is no
longer the bridge
**Plan reference:** §4 in
`Abacus/Hermes_Runtime_Bridge_to_AEE_Implementation_Plan.md`

---

## 1. Summary

| Item                                                       | Result |
|------------------------------------------------------------|--------|
| `aee/` package skeleton created                            | done |
| `RuntimeAdapter` Protocol + result types defined            | done |
| `HermesAdapter` (extracted from `app.py` HTTP calls)         | done |
| `FakeAdapter` (in-memory reference implementation)           | done |
| `AdapterRegistry` / `WorkerRegistry` scaffolding             | done |
| `Job` / `JobCreate` / `JobStatus` / state machine            | done |
| SQLite additive migration (8 new columns + index)            | done, idempotent |
| `dispatcher.models.Task` extended with AEE-1 fields         | done |
| `tests/test_adapter.py` (contract tests, 11 cases)           | **ALL PASS** |
| `tests/test_migration_aee1.py` (6 cases)                     | **ALL PASS** |
| `compileall` of `app.py dispatcher research config cli tests aee` | OK |
| `tests/test_safety.py`                                       | **ALL PASS** (regression) |
| `tests/test_routing.py`                                      | **OK** (24/24, regression) |
| `tests/test_dispatcher.py`                                   | **OK** (21/21, regression) |
| `tests/test_phase2.py`                                       | **OK** (19/19, regression) |
| `app.py` still calls Hermes inline (adapter wired but unused) | yes, AEE-2 will switch over |

**Net result: AEE-1 ships the seam. The bridge still works the
same way; new adapters (Pi Agent, Claude Code, MCP) can be
implemented and registered without touching `app.py` or the
dispatcher.**

---

## 2. Package layout (added)

```
aee/
├── __init__.py                — public version + re-exports
├── adapters/
│   ├── __init__.py            — Protocol + adapter re-exports
│   ├── base.py                — Protocol + result dataclasses + errors
│   ├── hermes_adapter.py      — HermesAdapter (POST/GET/STOP /v1/runs)
│   └── fake_adapter.py        — FakeAdapter (in-memory, for tests)
├── api/
│   └── __init__.py            — placeholder (AEE-2 routers go here)
├── core/
│   ├── __init__.py            — public core API
│   ├── job_models.py          — Job / JobCreate dataclasses
│   ├── state_machine.py       — JobStatus + can_transition()
│   └── registry.py            — AdapterRegistry / WorkerRegistry
├── security/
│   └── __init__.py            — re-exports dispatcher.safety.evaluate
└── storage/
    └── __init__.py            — placeholder (AEE-1 keeps SQLite in dispatcher/)
```

Total new Python: **1 117 lines** of AEE package + **547 lines** of
new tests, all unit-tested and dependency-light (httpx is the only
third-party import, already required by the bridge).

---

## 3. What changed (file-level)

### Added

- `aee/__init__.py`, `aee/{adapters,api,core,security,storage}/__init__.py` —
  package skeleton. `aee/__init__` exports `__version__ = "0.1.0"`.
- `aee/adapters/base.py` — `RuntimeAdapter` Protocol (structural,
  `@runtime_checkable`), the four result dataclasses
  (`RuntimeSubmitResult`, `RuntimePollResult`,
  `RuntimeCancelResult`) and three exception types
  (`RuntimeError`, `AdapterNotFoundError`,
  `UnknownExternalRunError`).
- `aee/adapters/hermes_adapter.py` — `HermesAdapter` extracts the
  four `httpx.AsyncClient` call sites from `app.py` (submit at
  `app.py:615-633`, poll at `app.py:708-721` and
  `app.py:762-774`, cancel at `app.py:811-823`). Wire-level
  behaviour is unchanged: same endpoints, same headers, same
  `instructions` text, same `metadata` translation. The adapter
  reads `HERMES_BASE_URL` / `HERMES_API_KEY` from env at construction.
  The `_build_submit_payload` helper keeps the legacy
  `mode="normal"` → no-`mode`-in-metadata behaviour so the upstream
  audit log is preserved.
- `aee/adapters/fake_adapter.py` — `FakeAdapter`. Records every
  `submit` / `poll` / `cancel`; exposes `mark_running` /
  `mark_completed` / `mark_failed` / `mark_cancelled` test helpers
  and a `hook` slot to simulate transport failures. Asserts at
  import time that it satisfies `RuntimeAdapter`.
- `aee/core/job_models.py` — `Job` + `JobCreate`. `Job` is the
  runtime-neutral contract every adapter sees; `JobCreate` is the
  request body for AEE-2's `POST /jobs`.
- `aee/core/state_machine.py` — `JobStatus` + adjacency-list
  `can_transition` / `assert_transition`. Terminal states
  (`completed` / `failed` / `cancelled` / `timeout`) are a sink.
- `aee/core/registry.py` — `AdapterRegistry` (thread-safe,
  replace-on-collision) and `WorkerRegistry` (in-memory; AEE-2 will
  back this with a DB table). `bootstrap_defaults()` registers the
  production `HermesAdapter`.
- `aee/security/__init__.py` — re-exports
  `dispatcher.safety.evaluate` so AEE code has a single
  safety entrypoint.
- `tests/test_adapter.py` — 11 contract tests covering Protocol
  shape, `submit`/`poll`/`cancel` happy paths, state transitions,
  unknown-id handling, hook-based error injection, registry
  lookup, `bootstrap_defaults`, and HermesAdapter payload
  translation.
- `tests/test_migration_aee1.py` — 6 tests covering the new
  columns, defaults, idempotency at three levels (private helper,
  in-process state, public `run_migrations()`), index creation,
  and the `Task` dataclass.

### Modified

- `dispatcher/db.py` —
  * Added `_AEE1_MIGRATIONS` list of 8 additive `ALTER TABLE
    tasks ADD COLUMN ...` statements (all with safe defaults).
  * Added `_apply_aee1_migrations()` (idempotent; only logs when
    it actually adds a column, and to `stderr` so it doesn't
    pollute JSON output).
  * Wired into `_init_schema()` so the very first `get_conn()`
    after AEE-1 rollout runs the migration transparently.
  * Added `idx_tasks_external_run_id` (idempotent
    `CREATE INDEX IF NOT EXISTS`).
  * Added public `run_migrations()` entry point for CLI use.
- `dispatcher/models.py` — `Task` dataclass gained 8 new fields
  with safe defaults, so existing code that constructs `Task(...)`
  without AEE-1 args still works. `to_dict()` includes them.

### Not modified (intentionally)

- `app.py` — the legacy `/runs` and `/tasks` endpoints still call
  Hermes inline. AEE-1 ships the seam; AEE-2 will switch
  `app.py:584-723` and `app.py:990` over to
  `adapter_registry.get(adapter_name)`.
- `dispatcher/manager.py` — still uses `hermes_run_id` and
  `find_by_hermes_run_id()`. AEE-2 will add `find_by_external_run_id`
  and start populating the new columns.
- `dispatcher/watcher.py` — still polls Hermes directly via
  `HERMES_BASE_URL`. AEE-2 will switch to
  `adapter_registry.get(task.adapter_name).poll(...)`.

---

## 4. The RuntimeAdapter seam

```python
@runtime_checkable
class RuntimeAdapter(Protocol):
    name: str          # "hermes" | "fake" | "pi_agent" | ...
    runtime_type: str  # same vocabulary; used as a routing key

    async def submit(self, job: "Job") -> RuntimeSubmitResult: ...
    async def poll(self, external_run_id: str) -> RuntimePollResult: ...
    async def cancel(self, external_run_id: str) -> RuntimeCancelResult: ...
```

Returning dataclasses (not raw dicts) lets the dispatcher and the
watcher work against the same shape regardless of backend. The
Protocol is structural: any object that implements the three
methods (with the right signatures) is a valid adapter without
inheritance.

`AdapterRegistry` is a module-level singleton; tests register a
`FakeAdapter` under the name `"fake"`, production uses
`bootstrap_defaults()` to install the real `HermesAdapter`. The
plan's "watcher 改 adapter-based polling" is now a one-liner:

```python
adapter = adapter_registry.get(task.adapter_name)
poll = await adapter.poll(task.external_run_id)
```

---

## 5. SQLite migration shape

The 8 new columns and their defaults:

| column               | type | default           | purpose                                  |
|----------------------|------|-------------------|------------------------------------------|
| `runtime_type`       | TEXT | `'hermes'`        | routing key for adapter lookup           |
| `adapter_name`       | TEXT | `'hermes'`        | which adapter is responsible             |
| `external_run_id`    | TEXT | `NULL`            | canonical external id (was `hermes_run_id` for Hermes) |
| `worker_id`          | TEXT | `NULL`            | set by `/jobs/claim` in AEE-2            |
| `heartbeat_at`       | TEXT | `NULL`            | last worker heartbeat (ISO timestamp)    |
| `claim_token_hash`   | TEXT | `NULL`            | hash of one-time claim token             |
| `approval_required`  | INT  | `0`               | sticky bit from safety.evaluate()        |
| `approval_state`     | TEXT | `'not_required'`  | one of not_required / pending / approved / denied |

Plus a new index: `idx_tasks_external_run_id`.

The migration is **purely additive** — no column is dropped, no
type is changed. Re-running `db.run_migrations()` is a no-op and
returns `[]`. The dispatcher's `Task` dataclass matches the new
schema; `to_dict()` surfaces all 8 fields.

AEE-2 will:
* backfill `external_run_id` from `hermes_run_id` for legacy tasks
  (one-shot `UPDATE`),
* leave `hermes_run_id` populated so `/runs/{hermes_run_id}` keeps
  working,
* switch `find_by_external_run_id` lookups to the new indexed column.

---

## 6. Test results

```text
$ .venv/bin/python -m compileall -q app.py dispatcher research config cli tests aee
(rc=0)

$ PYTHONPATH=. .venv/bin/python tests/test_adapter.py
=== AEE-1 adapter contract ===
  OK   protocol shape
  OK   submit returns RuntimeSubmitResult with non-empty id
  OK   poll observes queued -> running -> completed
  OK   cancel transitions running -> cancelled
  OK   cancel of unknown id is treated as already gone
  OK   poll of unknown id raises UnknownExternalRunError
  OK   submit() hook can raise RuntimeError to simulate failure
  OK   AdapterRegistry get / not-found / replace
  OK   bootstrap_defaults installs HermesAdapter
  OK   HermesAdapter payload translation preserves legacy fields
  OK   JobStatus.TERMINAL is a sink; legal transitions documented
ALL PASS

$ PYTHONPATH=. .venv/bin/python tests/test_migration_aee1.py
=== AEE-1 SQLite migration tests ===
  OK   migration adds all 8 AEE-1 columns and keeps hermes_run_id
  OK   AEE-1 columns have the expected defaults
  OK   AEE-1 migration is idempotent
  OK   idx_tasks_external_run_id is present
  OK   Task dataclass + to_dict expose AEE-1 fields with defaults
  OK   run_migrations() is idempotent at the public API
ALL PASS

$ PYTHONPATH=. .venv/bin/python -m unittest \
    tests.test_dispatcher tests.test_phase2 tests.test_routing
Ran 64 tests in 6.182s
OK

$ PYTHONPATH=. .venv/bin/python tests/test_safety.py
ALL PASS
```

(Existing tests are regression-clean. AEE-1 did not modify any
production behaviour.)

---

## 7. Open risk register

| # | Risk                                                                       | Mitigation in AEE-2+ |
|---|-----------------------------------------------------------------------------|----------------------|
| 1 | `app.py` still uses `httpx.AsyncClient` directly against Hermes, so the AEE seam is unused in production. | AEE-2: replace `app.py:615-823` and `app.py:990` with `adapter_registry.get(adapter_name)`. |
| 2 | `dispatcher.watcher` still polls Hermes via `HERMES_BASE_URL`.              | AEE-2: switch to `adapter_registry.get(task.adapter_name).poll(task.external_run_id)`. |
| 3 | Legacy tasks have `external_run_id = NULL` until a backfill runs.           | AEE-2: one-shot `UPDATE tasks SET external_run_id = hermes_run_id WHERE external_run_id IS NULL` at deploy time. |
| 4 | The new `Task` dataclass fields are typed `str` / `bool` / `Optional`, but SQLite stores them as TEXT / INT. The dispatcher's row→Task loader uses positional or named access, so the new fields get NULL defaults; not a correctness problem. | AEE-2: tighten the row→Task loader; add `tests/test_manager_aee1.py` to lock in the new fields round-trip. |
| 5 | `FakeAdapter` is the only non-Hermes adapter right now. Real Pi / Claude Code adapters don't exist yet. | AEE-3 + AEE-4 in the plan. |
| 6 | `WorkerRegistry` is in-memory; restart loses worker state.                  | AEE-2: persist via new `workers` table (additive migration). |

---

## 8. Recommended next steps (AEE-2 preview)

Per the plan, **AEE-2** adds the Worker Registry and pull-based
Job Claim Protocol. Concrete asks for that phase:

1. New `workers` SQLite table (`worker_id`, `worker_name`,
   `worker_type`, `capabilities`, `workdir_allowlist`,
   `max_concurrent`, `last_heartbeat_at`, `last_job_id`,
   `registered_at`). Additive migration, same idempotent pattern.
2. `POST /workers/register` and `POST /workers/{id}/heartbeat`
   (AEE API routers under `aee/api/workers.py`).
3. `POST /jobs` (create) + `POST /jobs/claim` (pull) +
   `POST /jobs/{id}/heartbeat` + `POST /jobs/{id}/complete` +
   `POST /jobs/{id}/fail` + `POST /jobs/{id}/cancel` under
   `aee/api/jobs.py`.
4. Switch `app.py` and `dispatcher/watcher.py` to drive the new
   endpoints via `adapter_registry.get(adapter_name)`. Keep
   `POST /runs` as a thin alias to `POST /jobs` for GPT Action
   compat.
5. Reaper update: when `external_run_id` is set but
   `heartbeat_at` is older than `stale_running_sec`, mark
   `timeout` instead of `failed`.
6. Add `tests/test_jobs_api.py` and `tests/test_workers_api.py`
   (using `FakeAdapter` from this phase).
7. Acceptance: the existing GPT Action flow still works;
   `curl POST /jobs {target_runtime: fake}` + a
   `FakeAdapter`-driven worker can claim, heartbeat, and complete
   a job end-to-end.

---

## 9. Constraints honoured

- No secret text disclosed.
- `app.py` HTTP behaviour unchanged.
- `data/dispatcher.db` was migrated in place (additive only); the
  legacy `hermes_run_id` column is preserved.
- No service was restarted. The new modules are wired in but
  `app.py` does not call them yet — the seam ships dark until
  AEE-2 flips the switch.
