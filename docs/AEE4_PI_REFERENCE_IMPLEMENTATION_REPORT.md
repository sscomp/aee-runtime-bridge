# AEE-4 Part B — Pi Reference Implementation Report

**Version:** 1.0
**Date:** 2026-07-10
**Status:** Shipped (AEE-4 Part B freeze)
**Source spec:** `Abacus/AEE4_WORKER_RUNTIME_CONTRACT_AND_PI_REFERENCE_IMPLEMENTATION.md`
**Related:** `docs/AEE_RUNTIME_INTEGRATION_GUIDE.md`,
`docs/runtime/Worker_Runtime_Contract.md`,
`docs/AEE3_CAPABILITY_MATCHING_REPORT.md`.

---

## 1. Change summary

AEE-4 Part B implements the **Pi Agent Worker**, the first
production-quality runtime that conforms to the AEE-4 Worker
Runtime Contract. Pi Worker is a **separate process** that
talks to the bridge over HTTP at `/v1/workers/...` and
`/v1/jobs/...`; it has no import-time dependency on the
bridge or on Hermes. The runtime itself is split into two
pieces:

- A **Python daemon** (`pi-agent/pi_worker.py`, ~580 lines)
  that handles the AEE-4 lifecycle: register, heartbeat
  (with the 5-value status model), claim, execute, log,
  complete / fail. Plus a config loader, a stdlib HTTP
  client, and the per-job workdir + spec-file plumbing.
- A **Node.js LLM runtime**
  (`pi-agent/runtime/pi-agent-runtime.js` + 5 lib files,
  ~250 lines) that reads a JSON spec, calls an
  OpenAI-compatible LLM with a small tool-calling loop
  (shell / file_read / file_write), enforces a workdir
  + allowlist, and prints a single JSON result on stdout.

This split is deliberate. The Python side is the bridge
client (the "talking to the dispatcher" half); the Node
side is the LLM client (the "talking to the model" half).
They communicate via a single JSON spec file written to
the per-job workdir. A failure in either side maps to a
distinct exit code (0..9), so the daemon can fail the
Job on the bridge with a precise reason.

The full lifecycle was exercised end-to-end in the
closed-loop smoke test (`pi-agent/tests/test_smoke.py`)
that spawns a real uvicorn bridge, a real daemon
subprocess, and a real node runtime in `--dry-run` mode
(no LLM call). The test asserts the Job's status moves
from `queued` to `completed` within 15 seconds, the
output_text contains the canned dry-run marker, and the
worker is registered in the bridge's DB with all 8
metadata fields.

## 2. New / modified files

### 2.1 New files (24)

| Path | Purpose |
|---|---|
| `docs/runtime/Worker_Runtime_Contract.md` | The canonical AEE-4 Worker Runtime Contract (Part A; 780 lines). |
| `pi-agent/README.md` | Operational guide for the Pi Worker. |
| `pi-agent/requirements.txt` | Daemon deps: PyYAML >= 6.0 (only addition over the bridge). |
| `pi-agent/config.example.yaml` | Config template. |
| `pi-agent/pi_agent.provider.env.example` | Provider env template. |
| `pi-agent/pi_worker.py` | The Python daemon. |
| `pi-agent/runtime/package.json` | Node deps: openai, commander, dotenv, zod. |
| `pi-agent/runtime/package-lock.json` | (npm-generated, 17 KB) |
| `pi-agent/runtime/pi-agent-runtime.js` | The Node CLI entrypoint. |
| `pi-agent/runtime/lib/spec.js` | Job-spec schema (zod). |
| `pi-agent/runtime/lib/tools.js` | 3 tool handlers (shell, file_read, file_write) + TOOL_DEFS. |
| `pi-agent/runtime/lib/provider.js` | OpenAI client wrapper. |
| `pi-agent/runtime/lib/loop.js` | The function-calling loop. |
| `pi-agent/runtime/lib/result.js` | Result envelope + dry-run canned response. |
| `pi-agent/runtime/tests/test_dry_run.js` | Node tests (3 tests, `node --test`). |
| `pi-agent/tests/test_pi_worker.py` | Daemon unit tests (13 tests). |
| `pi-agent/tests/test_smoke.py` | Closed-loop smoke tests (2 tests). |
| `pi-agent/systemd/pi-agent.service` | Reference systemd unit. |
| `pi-agent/supervisor/pi-agent.conf` | Production supervisord unit. |
| `docs/AEE_RUNTIME_INTEGRATION_GUIDE.md` | The runtime-agnostic integration handbook (NEW; see §3.4 of the master plan). |
| `docs/AEE4_PI_REFERENCE_IMPLEMENTATION_REPORT.md` | This document. |

### 2.2 Modified files (Part A of AEE-4, separately committed)

- `dispatcher/db.py` — 11 new columns on `workers` (8
  metadata + status / status_message / last_status_change_at);
  `idx_workers_status` index; `_AEE4_MIGRATIONS` tuple
  list; `WORKER_STATUSES` and `is_valid_status` helpers;
  `upsert_worker` and `update_worker_heartbeat` extended
  with status and metadata.
- `aee/api/workers.py` — accept the 8 new metadata
  fields + initial status; validate the 5-value status
  enum on heartbeat (HTTP 400 on invalid); all responses
  gain `version: "v1"`.
- `aee/api/jobs.py` — all responses gain `version: "v1"`.
- `aee/api/__init__.py` — `v1_router = APIRouter(prefix="/v1")`
  re-includes jobs + workers; mounted into `api_router`.
  Both `/jobs/...` and `/v1/jobs/...` work; same handlers.
- `tests/test_workers_api.py` — 5 new tests in
  `TestWorkersAPIAEE4` (metadata round-trip, NULLs,
  status update, invalid status, /v1/ aliases).

### 2.3 Master plan (in `Abacus/`)

- `AEE_MASTER_PLAN.md` — ADR-006, ADR-007, ADR-008
  published; §7 progress table updated (AEE-4 Part A
  ships 3 more docs + 3 more ADRs); §10.5 deliverables
  table updated (Part A list, Part B list); §11 release
  history updated (AEE-4 Part A row).

## 3. DB migration results

The AEE-4 migration adds 11 columns to the `workers`
table, all idempotent (idempotent pragma check + ALTER
TABLE):

```
[db] AEE-4 migration: added workers.runtime_name
[db] AEE-4 migration: added workers.runtime_version
[db] AEE-4 migration: added workers.operating_system
[db] AEE-4 migration: added workers.architecture
[db] AEE-4 migration: added workers.python_version
[db] AEE-4 migration: added workers.node_version
[db] AEE-4 migration: added workers.git_version
[db] AEE-4 migration: added workers.start_time
[db] AEE-4 migration: added workers.status
[db] AEE-4 migration: added workers.status_message
[db] AEE-4 migration: added workers.last_status_change_at
```

The migration is wired into both `_init_schema(conn)` and
`run_migrations()`. Re-running on an already-migrated DB
is a no-op. The `idx_workers_status` index is also
created idempotently — it's a forward-looking index for
the AEE-5+ scheduler's "give me all idle workers"
query.

The schema default for `status` is `'unknown'`, so
pre-AEE-4 worker rows have a sensible value (rather
than NULL). Pre-AEE-4 workers that don't supply
metadata fields get NULLs for the 8 metadata columns.

## 4. API endpoint list

The 5 **required** endpoints from the spec, all
mounted under `/v1/...` (canonical) and `/...` (legacy
alias) — the same handler serves both:

| Method | Path | Auth | Required by Pi Worker? |
|---|---|---|---|
| POST | `/v1/workers/register` | bearer | YES |
| POST | `/v1/workers/{id}/heartbeat` | bearer | YES |
| POST | `/v1/jobs/claim` | bearer | YES |
| POST | `/v1/jobs/{id}/logs` | bearer + claim_token | YES |
| POST | `/v1/jobs/{id}/complete` | bearer + claim_token | YES |
| POST | `/v1/jobs/{id}/heartbeat` | bearer + claim_token | supplementary (Pi Worker sends these too) |
| POST | `/v1/jobs/{id}/fail` | bearer + claim_token | YES (error path) |
| POST | `/v1/jobs/{id}/cancel` | bearer (+ token) | not used by Pi Worker |
| POST | `/v1/jobs` | bearer | not used by Pi Worker (operator-side) |
| GET  | `/v1/jobs/{id}` | bearer | not used by Pi Worker |
| GET  | `/v1/workers` | bearer | not used by Pi Worker |
| GET  | `/v1/workers/{id}` | bearer | not used by Pi Worker |

All responses include `"version": "v1"`. The same
handlers serve `/jobs/...` and `/workers/...` (no
prefix) as legacy aliases for backward compatibility
with AEE-2 / AEE-3 clients and the `/runs` GPT-Action
compat layer.

## 5. Pi Worker architecture

### 5.1 The Python daemon

The daemon (`pi-agent/pi_worker.py`) is a single-threaded
loop with one background thread (the heartbeat thread
that's only alive while a Job is running). The shape:

```
                   ┌─────────────────────┐
                   │  load_config()      │
                   │  load_env_file()    │
                   └──────────┬──────────┘
                              │
                   ┌──────────▼──────────┐
                   │  _register()        │──── POST /v1/workers/register
                   └──────────┬──────────┘       (with 8 metadata fields)
                              │
       ┌──────────────────────▼──────────────────────┐
       │  while not shutdown:                         │
       │    _claim_and_execute_once()                 │
       │      ├─ heartbeat idle                      │
       │      ├─ POST /v1/jobs/claim                  │
       │      │  (404 → sleep, retry)                │
       │      ├─ status=busy                         │
       │      ├─ _start_heartbeat_thread()            │
       │      ├─ _execute_job()                      │
       │      │  ├─ build per-job workdir            │
       │      │  ├─ write spec.json                  │
       │      │  ├─ spawn node pi-agent-runtime      │
       │      │  ├─ stream stderr to /v1/jobs/.../logs│
       │      │  ├─ parse final stdout JSON          │
       │      │  └─ /v1/jobs/{id}/complete|fail      │
       │      ├─ _stop_heartbeat_thread()             │
       │      └─ status=idle                          │
       └──────────────────────────────────────────────┘
                              │
                   ┌──────────▼──────────┐
                   │ _send_offline_then  │──── POST /v1/workers/{id}/heartbeat
                   │     _exit()         │     {status: "offline"}
                   └─────────────────────┘
```

The `_execute_job` method is the only place that talks
to the node runtime. It writes the spec, spawns the
runtime, waits for it (with a per-Job timeout), parses
the single JSON line on stdout, and either calls
`/v1/jobs/{id}/complete` (with output_text / usage / raw)
or `/v1/jobs/{id}/fail` (with a precise error from the
runtime's exit code map).

### 5.2 The Node.js runtime

The runtime (`pi-agent/runtime/`) is a minimal LLM
function-calling loop. The shape:

```
   ┌──────────────────────────────────────────┐
   │ pi-agent-runtime.js (CLI entrypoint)     │
   │  --job-file <path>                       │
   │  --provider-{base-url,api-key,model}     │
   │  --allowlist-cmds "ls,cat,git"           │
   │  --workdir <path>                        │
   │  --max-steps 20                          │
   │  --dry-run                               │
   └──────────────┬───────────────────────────┘
                  │
   ┌──────────────▼───────────────────────────┐
   │ lib/loop.js                              │
   │   messages = [system, user(input)]       │
   │   for step in 1..max_steps:              │
   │     response = provider.chat(...)        │
   │     if finish_reason == "stop":          │
   │       return assistant.content           │
   │     if finish_reason == "tool_calls":    │
   │       for tc in tool_calls:              │
   │         result = dispatchTool(tc)        │
   │         messages.append(tool_result)     │
   │   return { status: "ok"|"error", ... }   │
   └──────────────┬───────────────────────────┘
                  │
   ┌──────────────▼───────────────────────────┐
   │ lib/tools.js                             │
   │   shell(cmd)        exec with allowlist  │
   │   file_read(path)   workdir containment  │
   │   file_write(...)   workdir containment  │
   │   dispatchTool()    routes by name       │
   └──────────────────────────────────────────┘
```

The runtime exits 0 on success, 2-9 on various failure
modes (the daemon maps these to bridge /fail calls).
The `--dry-run` mode short-circuits the provider call
and prints a canned response; it's how the closed-loop
smoke test exercises the daemon without a real LLM.

### 5.3 The job spec contract

The contract between the Python daemon and the Node
runtime is a single JSON file at
`{workdir_root}/jobs/{job_id}/spec.json`:

```json
{
  "job_id": "TASK-20260710-0010",
  "input": "echo hello from pi",
  "tools": ["shell", "file_read", "file_write"],
  "max_steps": 20,
  "per_step_timeout_ms": 30000,
  "max_output_bytes": 204800,
  "workdir": "/home/ubuntu/hermes-runtime-bridge/runtime_data/pi-agent/jobs/TASK-20260710-0010",
  "allowlist_cmds": ["ls", "cat", "echo", ...],
  "approval_required": false
}
```

Validated by zod (`lib/spec.js`); invalid spec exits
the runtime with code 2.

## 6. Capability namespace + the worker's capabilities

The Pi Worker declares these capabilities at register:

```
[
  "runtime.pi",
  "tool.shell",
  "tool.python",
  "tool.git",
  "tool.filesystem"
]
```

The `runtime.pi` is a future-matcher affordance per
`docs/AEE4_CAPABILITY_NAMING_SPEC.md` §3.1 — the AEE-3
matcher doesn't use it (it just checks `worker_type` vs
`adapter_name`), but AEE-5+ may use it for affinity
routing.

A Job with `required_capabilities=["tool.shell"]` is
claimable by the Pi Worker (subset match). A Job with
`required_capabilities=["tool.brain_surgery"]` is NOT
claimable; the matcher filters it out at the SQL level.

The daemon's "unsupported capability" guard in
`test_pi_worker.py::TestCapabilityCheck` is a
defense-in-depth check: if the bridge ever returns a
Job whose `required_capabilities` is not a subset of
the worker's `capabilities` (race condition or future
matcher bug), the daemon fails the Job with
`error="unsupported capability"`.

## 7. Test results

### 7.1 Test inventory at AEE-4 Part B freeze

| Test runner | Tests | Status |
|---|---|---|
| `tests/test_workers_api.py` | 12 (7 AEE-2 + 5 AEE-4) | all green |
| `tests/test_jobs_api.py` | 24 (15 AEE-2 + 9 AEE-3) | all green |
| `tests/test_manager_aee1.py` | 6 | all green |
| `tests/test_adapter.py` | 9 | all green |
| `tests/test_safety.py` | 8 | all green |
| `tests/test_migration_aee1.py` | 2 | all green |
| `pi-agent/tests/test_pi_worker.py` | 13 (NEW) | all green |
| `pi-agent/tests/test_smoke.py` | 2 (NEW) | all green |
| `pi-agent/runtime/tests/test_dry_run.js` | 3 (NEW) | all green |

**Total at AEE-4 Part B freeze: 79 tests across 9
test runners, all green.**

(The 2 dispatcher test failures in
`tests/test_dispatcher.py` are pre-existing on master
and unrelated to AEE-4 — see `Abacus/AEE_MASTER_PLAN.md`
§4 risk #18 / §13 R1. They are test-isolation issues
from `test_workers_api.py:48` mutating
`mgr.LOGS_DIR`. Carried forward as tech debt.)

### 7.2 Closed-loop smoke test transcript

The closed-loop smoke test (`pi-agent/tests/test_smoke.py::TestEndToEndDryRun`)
spins up a real bridge on a random port, spawns the
daemon as a subprocess, creates a Job in the bridge's
DB via direct SQL, and asserts the daemon claims +
completes the Job end-to-end. The transcript:

```
$ PYTHONPATH=. .venv/bin/python -m unittest \
    pi-agent.tests.test_smoke.TestEndToEndDryRun -v
test_daemon_claims_completes_a_job_end_to_end ...

2026-07-10T14:25:00+0800 [pi_worker] INFO starting pi-worker pid=339737
2026-07-10T14:25:00+0800 [pi_worker] INFO registered worker_id=pi-smoke-01 type=pi_agent
2026-07-10T14:25:00+0800 [pi_worker] INFO spawning runtime: node /home/ubuntu/hermes-runtime-bridge/pi-agent/runtime/pi-agent-runtime.js --job-file /tmp/.../spec.json --dry-run
2026-07-10T14:25:00+0800 [pi_worker] INFO job TASK-SMOKE-001 completed: completed
ok

Ran 1 test in 2.667s
OK
```

The Job's `output_text` after completion contains the
canned `--dry-run` marker (`[dry-run] would execute:
echo hello from pi`). The `workers` table in the
bridge's DB has a row with `worker_id=pi-smoke-01`,
`runtime_name=pi`, and `status`=`busy` (or `idle` if the
daemon has had time to send the post-completion
heartbeat).

### 7.3 Coverage of the spec's error-handling matrix

The spec lists 6 error modes that Pi Worker must handle.
The test suite covers each:

| Error mode | Test |
|---|---|
| `timeout` | `test_subprocess_timeout_kills_process_and_fails_job` |
| `provider failure` | (covered by the runtime's `die(EXIT.PROVIDER, ...)` path; an integration test with a real LLM is out of scope for the smoke test) |
| `missing API key` | `test_missing_api_key_exits_3` |
| `unsupported capability` | `test_unsupported_capability_in_claim_fails_job` |
| `heartbeat expiry` | (covered by the AEE-3 reaper tests in `tests/test_jobs_api.py::TestReaperTimeoutSplit`; the daemon's `heartbeat_interval_sec=15` is well under the `stale_running_sec=60` threshold) |
| `non-zero exit` | `test_non_zero_exit_maps_to_fail_call` |

## 8. Known risks (carry-forward + new)

| # | Risk | Severity | Status |
|---|---|---|---|
| R1 | Bridge service still runs pre-AEE-2 code. | high | Unchanged. Restart needed. |
| R2 | `external_run_id` is NULL on legacy tasks. | medium | Mitigated (AEE-2.5). |
| R3 | `_COLUMNS` positional list will silently drop future columns. | medium | Unchanged. |
| R4 | `BRIDGE_API_KEY` shared by operator and worker. | low | Unchanged. |
| R8 | Backfill not in `run_migrations()`. | medium | Unchanged. |
| R9 | No `.git/` directory. | high | Mitigated (AEE-2.5). |
| R10 | Stale `hermes-runtime-bridge.tar.gz` in `Abacus/`. | medium | Unchanged. |
| R12 | `GET /jobs/_claimable` is shadowed by `GET /jobs/{id}`. | low | Unchanged. |
| R13 | `GET /health` reports stale `tasks_total`. | low | Unchanged. |
| R15 | `find_claimable_job` post-filters up to 5 candidates. | medium | Unchanged. |
| R16 | Workers registered pre-AEE-3 may have non-canonical caps. | low | Unchanged. |
| **R17 (new)** | Pre-existing `tests/test_dispatcher.py::test_log_file_written` and `test_task_json_written_on_complete` fail when run after `tests/test_workers_api.py` (the latter mutates `mgr.LOGS_DIR`). | low | Pre-existing on master; not introduced by AEE-4. The test passes when run alone. Documented for the AEE-5 fix. |
| **R18 (new)** | The Pi Worker daemon uses a single `urllib.request`-based HTTP client. Under heavy load, a connection pool would be more efficient. | low | Out of scope for AEE-4. The daemon makes ~3 HTTP calls per minute; connection pooling is premature. |
| **R19 (new)** | The Node runtime has no metric collection (no token / cost / latency histograms). | low | Out of scope for AEE-4. AEE-5+ may add. |
| **R20 (new)** | The Node runtime spawns a new `node` process per Job. Cold-start cost is ~100 ms; for low-throughput batch workloads this is fine. | low | Out of scope for AEE-4. A future AEE-5 may switch to a long-lived `node` worker (a `RuntimeAdapter`-style seam). |
| **R21 (new)** | The closed-loop smoke test spins up a real uvicorn server in a thread; it works in CI but may flake in environments where port 0 isn't honored. | low | AEE-4 smoke is best-effort; the 13 unit tests in `test_pi_worker.py` give most of the coverage without the integration dependency. |

## 9. AEE-5+ next steps

Pi Worker is the first of N future runtimes. The next
ones (per the master plan) are:

- **AEE-5: Claude Code Worker.** Same shape as Pi
  Worker. Register with `worker_type=claude_code`,
  `runtime_name=claude`, capabilities
  `["runtime.claude", "tool.shell", "tool.python",
  "tool.git"]`. The runtime calls Anthropic's API
  directly via `@anthropic-ai/sdk`. The conformance
  checklist (§9 of the contract) maps 1:1 to the Pi
  Worker's code; Claude Code Worker's daemon will be
  a fork-and-rename of `pi_worker.py`.
- **AEE-6: artifact pipeline.** Out of scope for
  AEE-4. The contract's `expected_artifacts` field is
  passed to Pi Worker but not yet honored; AEE-6
  lands the `POST /v1/jobs/{id}/artifacts` upload
  endpoint.
- **AEE-7: scheduler / future matcher.** Per
  ADR-005. The AEE-3 subset rule is the floor; the
  AEE-7 weighted scorer (capability / load / priority
  / affinity / latency) is the ceiling. The data
  model is in place (ADR-004 `requirements_json`,
  `capability_versions_json`); the matcher is not.
- **AEE-8: event bus.** Out of scope for AEE-4.
  Pi Worker's `task_events` writes go through the
  existing AEE-1 `manager._emit_event` path. A
  pub/sub layer (Redis Streams? Kafka?) is AEE-8
  scope.
- **AEE-9: MCP Worker.** Out of scope for AEE-4.
  The contract's `protocol.mcp` capability is
  reserved; AEE-9 implements it.
- **AEE-10: cluster / multi-host.** Out of scope.

GPT Architecture Review is required before AEE-5
starts. The review should focus on:

1. The Worker Runtime Contract (`docs/runtime/Worker_Runtime_Contract.md` §1-§9).
2. The Pi Worker's conformance (§9 checklist mapping in `pi-agent/README.md`).
3. The 5-value status model (§5 of the contract; ADR-008).
4. The /v1/... path-prefix versioning decision (ADR-007).
5. The runtime-agnostic integration handbook (`docs/AEE_RUNTIME_INTEGRATION_GUIDE.md`) — is the template good for Claude Code / GPT / MCP?
6. The "closed loop validation" (this report §7.2) — is the end-to-end test sufficient, or should AEE-5 add a CI hook that runs it on every commit?
