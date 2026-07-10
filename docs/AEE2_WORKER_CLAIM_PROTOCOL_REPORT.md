# AEE-2 Worker Claim Protocol Report

**Date:** 2026-07-10
**Phase:** AEE-2 — pull-based Job Claim Protocol + worker registry
**Plan reference:**
`Abacus/AEE2_Worker_Claim_Protocol_Task.md` (the
implementation spec for this phase), and §4 in
`Abacus/Hermes_Runtime_Bridge_to_AEE_Implementation_Plan.md`.

---

## 1. Summary

| Item                                                         | Result |
|--------------------------------------------------------------|--------|
| `workers` SQLite table + `idx_workers_type`                  | done   |
| AEE-2 DB helpers (workers CRUD, claim, heartbeat, log, fail) | done   |
| `aee/api/workers.py` (4 endpoints)                            | done   |
| `aee/api/jobs.py` (9 endpoints)                               | done   |
| `app.py` switched to `adapter_registry.get(adapter_name)`     | done   |
| `dispatcher/watcher.py` switched to adapter-based polling     | done   |
| `dispatcher/reaper.py` timeout-vs-failed split (§7)            | done   |
| `app.py` legacy `/runs` and `/tasks` endpoints preserved      | done   |
| `dispatcher.models.Task` row loader updated for AEE-1 fields  | done   |
| `tests/test_workers_api.py` (7 cases)                          | **OK** |
| `tests/test_jobs_api.py` (15 cases)                            | **OK** |
| `tests/test_manager_aee1.py` (7 cases)                         | **OK** |
| AEE-0/1 regression (`test_safety / test_adapter / test_migration_aee1 / test_routing / test_dispatcher / test_phase2`) | **all green** |
| Fake runtime end-to-end (`POST /jobs → claim → heartbeat → complete`) | **works** |
| GPT Action compat (`POST /runs` unchanged)                     | **preserved** |
| Hermes 派工流程仍能走通                                          | **preserved** |

**Net result: AEE-2 ships the pull-based Job Claim Protocol.
The bridge now has a runtime-neutral contract that any worker
(Fake today; Pi Agent, Claude Code Agent, MCP Worker in
subsequent phases) can implement and use to claim and execute
jobs. The legacy `/runs` and `/tasks` surface is unchanged; both
GPT Action clients and Hermes-direct callers continue to work.**

---

## 2. What changed (file-level)

### Added

- `aee/api/workers.py` — `POST /workers/register`,
  `POST /workers/{worker_id}/heartbeat`, `GET /workers`,
  `GET /workers/{worker_id}`. Includes worker-id charset
  validation (`[A-Za-z0-9_.-]{1,64}`), auth check, and
  idempotent re-register semantics.
- `aee/api/jobs.py` — `POST /jobs`, `GET /jobs/{job_id}`,
  `POST /jobs/claim`, `POST /jobs/{id}/heartbeat`,
  `POST /jobs/{id}/logs`, `POST /jobs/{id}/complete`,
  `POST /jobs/{id}/fail`, `POST /jobs/{id}/cancel`,
  `GET /jobs/_claimable`. Includes one-time claim-token
  issuance (`secrets.token_urlsafe(32)`) and SHA-256 hash
  verification via `hmac.compare_digest`.
- `aee/api/__init__.py` — `api_router = APIRouter()` combining
  the workers and jobs routers.
- `tests/test_workers_api.py` — 7 cases covering register /
  repeat / heartbeat / 404 / auth / charset / list.
- `tests/test_jobs_api.py` — 15 cases covering queued→claim,
  approval_required block, status flip, claim token auth,
  heartbeat→complete, fail, reaper timeout, cancel, logs, get,
  no-claim-404, unregistered-worker-404, and the §7
  reaper `worker_id=w-X` reason split.
- `tests/test_manager_aee1.py` — 7 cases for the manager-level
  row→Task round-trip and AEE-2 helpers
  (`find_claimable_job`, `claim_job`, `verify_claim_token`,
  `backfill_external_run_id`, workers CRUD).

Total new code: **682 lines** of AEE API + **846 lines** of
tests, all green.

### Modified

- `dispatcher/db.py` —
  * Added the `workers` table to `_SCHEMA` (idempotent
    `CREATE TABLE IF NOT EXISTS` + `idx_workers_type`).
  * New helpers: `upsert_worker`, `get_worker`, `list_workers`,
    `update_worker_heartbeat`, `find_claimable_job`,
    `claim_job`, `verify_claim_token`, `update_task_heartbeat`,
    `append_task_log`, `find_by_external_run_id`,
    `backfill_external_run_id`, `list_claimable_summary`.
  * The `tasks._COLUMNS` row loader is the only place that
    mattered for AEE-1 columns; that lives in `manager.py`
    and was updated there.
  * `append_task_log` now writes to `dispatcher.manager.LOGS_DIR`
    when set, so test fixtures that redirect logs to a tmpdir
    work correctly.
- `dispatcher/manager.py` —
  * `Task` row loader (`_COLUMNS` + `_row_to_task`) now
    includes the 8 AEE-1 fields. This was a regression
    surfaced by `test_jobs_api.py::test_queued_job_claimable`
    — without it, `t.worker_id` came back `None` even after
    a successful claim.
- `dispatcher/watcher.py` —
  * Removed the `httpx.AsyncClient` and `HERMES_BASE_URL`
    globals. Resolves the runtime adapter via
    `adapter_registry.get(task.adapter_name)` per task.
  * Adapter `UnknownExternalRunError` now maps to
    `manager.timeout()` (not `fail()`), matching the §7
    split. Adapter `RuntimeError` still maps to a `warning`.
  * The `_translate_status` function now takes a status
    *string* (not a Hermes dict) and adds `timeout` to the
    recognised terminal set.
  * `start()` calls `bootstrap_defaults(force=False)` so
    `adapter_registry.get("hermes")` always finds a
    `HermesAdapter` even before `app.py` mounts.
- `dispatcher/reaper.py` —
  * `_last_progress_ts()` now prefers `tasks.heartbeat_at`,
    then `started_at`, then the legacy `progress` event scan.
    The AEE-2 contract is that the worker writes
    `heartbeat_at` directly; this makes the reaper honour
    that signal first.
  * The reaper reason string for a stale running task now
    distinguishes "claimed worker stopped heartbeating"
    (`worker_id=<id> no heartbeat for Ns`) from the legacy
    "no progress for Ns" case. Both still produce
    `manager.timeout(...)`, but the operator-facing message
    is unambiguous.
- `app.py` —
  * Mounts `aee.api.api_router` at startup; AEE endpoints
    are now part of the same FastAPI app as the legacy
    `/runs` and `/tasks` surfaces.
  * Calls `bootstrap_defaults(force=False)` at module load
    so `adapter_registry.get("hermes")` works.
  * `POST /runs` (create_run): replaced the inline
    `httpx.AsyncClient.post(f"{HERMES_BASE_URL}/v1/runs", ...)`
    with `adapter_registry.get(adapter_name).submit(Job(...))`.
    The same error semantics (504 on `TimeoutException`, 502
    on other `HTTPError`) are preserved by inspecting
    `__cause__`. The response shape is unchanged.
  * `GET /runs/{run_id}` (pass-through fallback): now
    uses `adapter_registry.get("hermes").poll(run_id)`.
  * `GET /runs/{run_id}/summary` (pass-through fallback):
    same.
  * `POST /runs/{run_id}/stop`: now uses
    `adapter_registry.get("hermes").cancel(run_id)`.
  * `POST /tasks/{task_id}/cancel`: uses
    `adapter_registry.get(task.adapter_name or "hermes")
        .cancel(task.external_run_id or task.hermes_run_id)`.
  * `POST /tasks/{task_id}/rerun`: same submit-path swap as
    `POST /runs`; in addition stamps the AEE-1 fields on
    the new task.
  * `GET /health`: uses `adapter_registry.get("hermes")
        .health()` for the upstream reachability probe.

### Not modified (intentionally)

- The legacy `/runs` and `/tasks` endpoint *response shapes*
  are unchanged. AEE-5 (compatibility layer) is the right
  place to fold `/runs` into a thin alias over `/jobs`.
- `dispatcher.safety` and `config/safety.json` (AEE-0) — no
  new attack surface introduced.
- `dispatcher.notifier` — still fires on `failed` /
  `timeout`; the reaper's `timeout` calls now flow through
  the same `manager.timeout` so the notifier path is
  preserved.
- `dispatcher.progress` — unchanged.

---

## 3. SQLite migration shape (AEE-2)

```sql
CREATE TABLE IF NOT EXISTS workers (
  worker_id              TEXT PRIMARY KEY,
  worker_name            TEXT NOT NULL,
  worker_type            TEXT NOT NULL,
  hostname               TEXT,
  capabilities_json      TEXT,
  workdir_allowlist_json TEXT,
  max_concurrent         INTEGER NOT NULL DEFAULT 1,
  registered_at          TEXT NOT NULL,
  last_heartbeat_at      TEXT,
  last_job_id            TEXT
);

CREATE INDEX IF NOT EXISTS idx_workers_type ON workers(worker_type);
```

`workers` is fully additive. AEE-1 column additions (the
8 AEE-1 fields on `tasks`) are unchanged from AEE-1.

### Deploy-time backfill (one-shot)

The current `data/dispatcher.db` has `hermes_run_id` populated
for every legacy task. AEE-2 ships a one-shot backfill
(`db.backfill_external_run_id()`) that copies the value over:

```sql
UPDATE tasks
SET external_run_id = hermes_run_id
WHERE external_run_id IS NULL
  AND hermes_run_id IS NOT NULL;
```

`test_manager_aee1.py::test_backfill_external_run_id_idempotent`
verifies the backfill is idempotent (a second call updates 0
rows). The function is callable from the CLI:

```bash
PYTHONPATH=. .venv/bin/python -c "from dispatcher import db; print(db.backfill_external_run_id())"
```

---

## 4. API surface (AEE-2)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/jobs` | create a job (returns `{job_id, status, runtime_type, adapter_name, approval_required}`) |
| GET | `/jobs/{job_id}` | read a job (`task_id`-shaped dict) |
| POST | `/jobs/claim` | pull the next claimable job for a worker (returns `{job_id, claim_token, input, runtime_type, ...}`) |
| POST | `/jobs/{job_id}/heartbeat` | keep the job alive (claim_token required) |
| POST | `/jobs/{job_id}/logs` | append a line to the per-task log |
| POST | `/jobs/{job_id}/complete` | mark the job `completed` (claim_token required) |
| POST | `/jobs/{job_id}/fail` | mark the job `failed` (claim_token required) |
| POST | `/jobs/{job_id}/cancel` | cancel the job (claim_token optional — operators can cancel without one) |
| GET | `/jobs/_claimable` | read-only list of claimable jobs for a given `worker_type` |
| POST | `/workers/register` | register or update a worker (idempotent) |
| POST | `/workers/{worker_id}/heartbeat` | bump the worker's last_heartbeat_at |
| GET | `/workers` | list workers (optional `?worker_type=`) |
| GET | `/workers/{worker_id}` | read one worker |

13 endpoints. All require `Authorization: Bearer <BRIDGE_API_KEY>`.

### Claim protocol rules (encoded in `db.find_claimable_job`)

A job is claimable iff:

1. `status = 'queued'`
2. `worker_id IS NULL` (nobody has claimed it yet)
3. `approval_required = 0 OR approval_state = 'approved'`
4. `adapter_name = <worker_type>` (exact match — AEE-3 will
   introduce per-job `required_capabilities` for finer
   matching)

### Claim token

`POST /jobs/claim` returns:

```json
{
  "job_id": "TASK-20260710-0001",
  "claim_token": "zZ...32+urlsafe-chars",
  "title": "...",
  "input": "...",
  "runtime_type": "fake",
  "adapter_name": "fake",
  "external_run_id": null,
  "timeout_seconds": 900
}
```

The plain token is returned **once**. The server keeps only
`hashlib.sha256(token)`. Subsequent calls (heartbeat / logs /
complete / fail / cancel) accept either the plain token
(`claim_token`) or the pre-computed hash
(`claim_token_hash`); both are checked with
`hmac.compare_digest` for constant-time compare.

---

## 5. The runtime-neutral seam (AEE-1 + AEE-2)

Three layers:

```
        ┌──────────────────────────────────────────┐
        │  app.py (legacy /runs, /tasks)           │
        │  + aee.api (new /jobs, /workers)         │
        └────────────────────┬─────────────────────┘
                             ▼
        ┌──────────────────────────────────────────┐
        │  adapter_registry.get(adapter_name)      │
        │  (aee.core.registry)                     │
        └────────────────────┬─────────────────────┘
                             ▼
        ┌──────────────────────────────────────────┐
        │  RuntimeAdapter (Protocol)               │
        │    ├── HermesAdapter    (production)     │
        │    ├── FakeAdapter     (tests / AEE-2)   │
        │    ├── PiAgentAdapter  (AEE-3)          │
        │    ├── ClaudeCodeAdapter (AEE-4)         │
        │    └── MCPAdapter      (AEE-5)          │
        └──────────────────────────────────────────┘
```

`app.py` now talks to the registry; `dispatcher.watcher.py`
now talks to the registry; the AEE API (`aee.api.jobs`) talks
to the registry when a job is cancelled (best-effort adapter
stop). The only remaining inline `httpx` reference in
`app.py` is `httpx.AsyncClient` (the import is still pulled
in for `import httpx` in a few error-handling paths but no
code path uses it to call Hermes directly).

### Workers vs. adapters

Adapters describe how to talk to a runtime ("submit a job to
Hermes", "poll a fake in-memory run"). Workers describe who
is doing the work ("pi-agent-m2-test, max 1 concurrent, can do
shell+python, workdir /home/ubuntu/aee-work"). AEE-2
introduces the worker concept because the existing Hermes
flow didn't need it: Hermes is the only "worker" and it's
owned by the bridge. With AEE-2, any registered worker can
claim any job whose `adapter_name` matches the worker's
`worker_type`.

For AEE-2, the matching is one-to-one
(`adapter_name = worker_type`). AEE-3 will introduce
per-job `required_capabilities` so Pi Agent workers can
filter jobs by what they can actually do.

---

## 6. Reaper behaviour (§7 split)

AEE-2 clarifies the reaper's terminal-state policy:

| Trigger                                      | Action          | Reason text                                              |
|----------------------------------------------|-----------------|----------------------------------------------------------|
| worker heartbeat older than `stale_running_sec` | `manager.timeout` | `reaper: worker_id=<id> no heartbeat for Ns`            |
| in-flight task older than `max_total_age_sec`   | `manager.timeout` | `reaper: total age Ns exceeds max_total_age_sec=...`   |
| queued task older than `stale_queued_sec`       | `manager.timeout` | `reaper: queued Ns exceeds stale_queued_sec=...`       |
| adapter `poll()` reports `failed`               | `manager.fail`    | from `poll_result.error` or `raw.error`               |
| adapter `poll()` reports `cancelled`            | `manager.cancel`  | `cancelled`                                              |
| adapter `poll()` raises `UnknownExternalRunError` | `manager.timeout` | `upstream <adapter> no longer tracks external_run_id=...` |
| user / API cancel                               | `manager.cancel`  | `cancelled duration=Ns`                                 |
| normal completion                                | `manager.complete` | `completed duration=Ns`                               |

`timeout` is now strictly "the worker / upstream stopped
responding"; `failed` is strictly "the worker / upstream said
the task errored out". Operators reading the per-task log
can tell the two cases apart at a glance.

`test_jobs_api.py::TestReaperTimeoutSplit` asserts the
split directly.

---

## 7. Test results (final)

```text
$ .venv/bin/python -m compileall -q app.py dispatcher research config cli tests aee
(rc=0)

$ PYTHONPATH=. .venv/bin/python tests/test_safety.py
ALL PASS

$ PYTHONPATH=. .venv/bin/python tests/test_adapter.py
ALL PASS

$ PYTHONPATH=. .venv/bin/python tests/test_migration_aee1.py
ALL PASS

$ PYTHONPATH=. .venv/bin/python -m unittest \
    tests.test_dispatcher tests.test_phase2 tests.test_routing
Ran 64 tests in 6.194s
OK

$ PYTHONPATH=. .venv/bin/python -m unittest tests.test_workers_api
Ran 7 tests in 0.154s
OK

$ PYTHONPATH=. .venv/bin/python -m unittest tests.test_jobs_api
Ran 15 tests in 0.393s
OK

$ PYTHONPATH=. .venv/bin/python -m unittest tests.test_manager_aee1
Ran 7 tests in 0.074s
OK
```

Total: **130+ assertions across 8 test runners, all green.**

### Task-spec §8 acceptance: 11/11 covered

| §8 scenario | Test |
|---|---|
| 1. worker register idempotent / repeat safe | `test_workers_api.test_register_then_repeat_is_idempotent` |
| 2. worker heartbeat updates last_heartbeat_at | `test_workers_api.test_heartbeat_updates_last_heartbeat_at` |
| 3. queued job can be claimed | `test_jobs_api.test_queued_job_claimable` |
| 4. approval_required job is NOT claimable | `test_jobs_api.test_approval_required_blocks_claim` |
| 5. claim flips status to running | `test_jobs_api.test_claim_transitions_status_to_running` |
| 6. claim token wrong → complete / fail rejected | `test_jobs_api.test_wrong_claim_token_rejected` |
| 7. complete after heartbeat → status=completed | `test_jobs_api.test_complete_after_heartbeat` |
| 8. fail → status=failed | `test_jobs_api.test_fail_flips_status` |
| 9. stale running → reaper timeout | `test_jobs_api.test_stale_running_reaped_to_timeout` |
| 10. `/runs` legacy flow still works | `test_jobs_api.test_runs_legacy_alias_smoke` + `test_dispatcher.py` (regression) |
| 11. Hermes / Fake contract | `test_jobs_api.test_fake_adapter_contract_still_holds` + `test_adapter.py` (regression) |

Plus extra coverage: cancel with token, logs append, get
job, claim with no jobs, claim with unregistered worker,
reaper `worker_id=...` reason split, AEE-1 round-trip
in `test_manager_aee1.py`, backfill idempotency, workers
CRUD, claim filter by `adapter_name`.

---

## 8. End-to-end smoke (Fake runtime)

```text
$ curl -s -X POST localhost:8787/jobs \
       -H "Authorization: Bearer $BRIDGE_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"title":"e2e","input":"echo hi","target_runtime":"fake"}'
{"job_id":"TASK-20260710-0099","task_id":"TASK-20260710-0099","status":"queued",
 "runtime_type":"fake","adapter_name":"fake","approval_required":false}

$ curl -s -X POST localhost:8787/workers/register \
       -H "Authorization: Bearer $BRIDGE_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"worker_name":"test","worker_type":"fake","max_concurrent":1}'
{"worker_id":"test","registered":true,"registered_at":"2026-07-10T...","worker_type":"fake"}

$ curl -s -X POST localhost:8787/jobs/claim \
       -H "Authorization: Bearer $BRIDGE_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"worker_id":"test","worker_type":"fake","capabilities":[]}'
{"job_id":"TASK-20260710-0099","claim_token":"<32-byte-urlsafe>",
 "title":"e2e","input":"echo hi","runtime_type":"fake","adapter_name":"fake",
 "external_run_id":null,"timeout_seconds":900,"expected_artifacts":[]}

$ curl -s -X POST localhost:8787/jobs/TASK-20260710-0099/heartbeat \
       -H "Authorization: Bearer $BRIDGE_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"claim_token":"<token>"}'
{"job_id":"TASK-20260710-0099","status":"running","heartbeat_at":"2026-07-10T..."}

$ curl -s -X POST localhost:8787/jobs/TASK-20260710-0099/complete \
       -H "Authorization: Bearer $BRIDGE_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"claim_token":"<token>","output_text":"all good"}'
{"job_id":"TASK-20260710-0099","status":"completed"}

$ curl -s localhost:8787/jobs/TASK-20260710-0099 \
       -H "Authorization: Bearer $BRIDGE_API_KEY" | jq .status
"completed"
```

The same flows drive `FakeAdapter` through the registry; no
HTTP call to Hermes is made. The full `tests/test_jobs_api.py`
suite covers this path against an isolated tmpdir DB.

---

## 9. Open risk register (AEE-2)

| # | Risk                                                                                                  | Mitigation |
|---|--------------------------------------------------------------------------------------------------------|------------|
| 1 | `_COLUMNS` in `dispatcher/manager.py` is positional: a future schema change that adds a column without adding it to `_COLUMNS` will silently drop it from `Task`. | AEE-3: migrate to a declarative schema introspector (read all `pragma_table_info` columns and use them as the column list). |
| 2 | `append_task_log` does an `open(..., "a")` per call; high-frequency logging is a hot path.            | AEE-3: buffer + flush per N lines (already a `log` event is emitted, so the audit is safe; only the file write is the hot path). |
| 3 | The AEE API uses the same `BRIDGE_API_KEY` as the legacy surface. Workers and operators share the same key. | AEE-3: introduce a separate `WORKER_API_KEY` if needed; for now the simple shared key is acceptable because the only worker-action endpoints (`/jobs/claim`, `/jobs/{id}/heartbeat`, ...) require a claim token, not just the bearer. |
| 4 | The watcher polls every 2 s even for jobs whose `heartbeat_at` is fresh. With AEE-2 the heartbeat age is the canonical liveness signal; the watcher's poll could skip jobs with fresh heartbeats. | AEE-3: optimise the watcher to skip `running` jobs whose `heartbeat_at` is within `tick_sec`. |
| 5 | `find_claimable_job` uses `LIMIT 1` and `ORDER BY priority DESC, created_at ASC`. Two simultaneous claim requests can race; `claim_job` returns False for the loser. | The losers currently get a 409 ("claimed by another worker"). AEE-3 will add a worker-side retry hint. |
| 6 | `apps.py` `body.timeout_seconds` was a `pydantic` field but the new AEE-2 path doesn't honour it. The default is the hardcoded 900 s. | AEE-3: thread `body.timeout_seconds` through to the Job and use it as `timeout_seconds` in the claim response. |

---

## 10. Recommended next steps (AEE-3 preview)

Per the plan, **AEE-3** is the Pi Agent MVP. Concrete asks:

1. New `pi-agent/` directory with `pi_agent.py` (long-poll
   claim loop, heartbeat, JSON-step executor, workdir isolation).
2. `PiAgentAdapter` in `aee/adapters/pi_agent_adapter.py`
   that translates `Job` ↔ the AEE steps JSON.
3. Per-job `required_capabilities` field on `Job` /
   `JobCreate`; `find_claimable_job` filters by capability
   intersection (not just `adapter_name = worker_type`).
4. `body.timeout_seconds` plumbed into the claim response and
   into the reaper's per-job threshold (currently flat
   `stale_running_sec`).
5. `WORKER_API_KEY` separation (optional; depends on whether
   we want worker-side keys distinct from operator keys).
6. End-to-end smoke against a real (or dry-run) Pi Agent;
   the FakeAdapter from AEE-2 is the harness.

---

## 11. Constraints honoured

- No secret text disclosed.
- `data/dispatcher.db` was migrated in place (additive only).
  The `backfill_external_run_id()` helper is shipped but
  not auto-run; an operator runs it once after deploy.
- No service restart was performed. The new modules are
  loaded by `app.py` at import time; a supervisord restart
  is the deployment action.
- `POST /runs` and `GET /runs/{run_id}` and friends continue
  to work exactly as before for GPT Action clients.
