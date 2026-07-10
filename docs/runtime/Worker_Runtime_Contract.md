# AEE Worker Runtime Contract

**Version:** 1.0
**Date:** 2026-07-10
**Status:** Accepted (AEE-4 Part A freeze)
**Supersedes:** the AEE-2 / AEE-3 ad-hoc /workers and /jobs surface
  (this document formalises the contract those endpoints have always
  implied, and adds the /v1/ versioning + Worker Status model).
**Backward compatible:** YES — see §2.3.

**Source-of-truth ADR entries:** ADR-006, ADR-007, ADR-008 in
`Abacus/AEE_MASTER_PLAN.md` §9.

---

## 1. Purpose

AEE (the Agent Execution Engine) ships a runtime-neutral job
dispatcher. The dispatcher speaks to **Worker runtimes** over
HTTP: every Worker is a separate process that registers,
claims, heartbeats, executes, logs, and completes jobs.

This document is the **canonical contract** that every Worker
runtime — past, present, and future — must follow. The Pi
Worker (AEE-4 Part B) is the **first** runtime built to this
contract; Claude Code, GPT-direct, and MCP workers (AEE-5+)
follow the same shape.

The contract pins down:

1. The 5 (and 6 supplementary) HTTP endpoints a Worker calls.
2. The API versioning policy (`/v1/...` is canonical, legacy
   `/...` aliases exist for backward compat).
3. The Worker Metadata fields every Worker reports at register
   time.
4. The Worker Status model (5 values + transition rules).
5. The lease / retry / timeout semantics the dispatcher enforces
   on the Worker's behalf.
6. The Capability Declaration namespace every Worker uses when
   advertising what it can do.

A Worker that satisfies the conformance checklist (§9) is
**conformant**; the bridge will register it, claim jobs to it,
and accept its lifecycle calls without complaint.

---

## 2. Worker APIs

A conformant Worker uses the following HTTP endpoints. All
endpoints are POST or GET; the Worker never opens a TCP socket
the bridge isn't already listening on. All endpoints require
`Authorization: Bearer <BRIDGE_API_KEY>` (or `DISPATCHER_API_KEY`
override for tests). Lifecycle endpoints (`claim`, `heartbeat`,
`logs`, `complete`, `fail`, `cancel`) additionally require a
`claim_token` minted at claim time.

### 2.1 Required endpoints (5)

The 5 endpoints below are the **minimum** a conformant Worker
must implement a client for. A Worker that only calls these 5
can claim and complete jobs; the bridge handles the rest.

#### 2.1.1 `POST /v1/workers/register`

Register a Worker. Idempotent: re-registering the same
`worker_id` updates metadata but preserves `registered_at`.

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `worker_id` | string | optional | Auto-derived from `worker_name` if absent. Charset `[A-Za-z0-9_.-]{1,64}`. |
| `worker_name` | string | required | Human-readable name. |
| `worker_type` | string | required | e.g. `pi_agent`, `claude_code`, `hermes`, `mcp`. **This is the key the dispatcher uses to match a Worker to a Job's `adapter_name` field.** |
| `hostname` | string | optional | The host the Worker runs on. |
| `capabilities` | string[] | optional | Namespaced capability strings (§6). Default `[]`. |
| `workdir_allowlist` | string[] | optional | Path prefixes the Worker is allowed to write to. Default `[]`. |
| `max_concurrent` | integer | optional | Soft cap on simultaneous running jobs. Default 1. |
| `runtime_name` | string | optional | §3. |
| `runtime_version` | string | optional | §3. |
| `operating_system` | string | optional | §3. |
| `architecture` | string | optional | §3. |
| `python_version` | string | optional | §3. |
| `node_version` | string | optional | §3. |
| `git_version` | string | optional | §3. |
| `start_time` | string (ISO-8601 UTC) | optional | §3. |
| `status` | string | optional | One of the 5 §4 statuses. Default `unknown` on insert. |
| `status_message` | string | optional | Human-readable context. |

**Response 200:**

```json
{
  "version": "v1",
  "worker_id": "pi-agent-m2-001",
  "registered": true,
  "registered_at": "2026-07-10T00:00:00Z",
  "worker_type": "pi_agent"
}
```

**Errors:**
- `400` — invalid body (missing required field, bad `worker_id` charset, invalid `status`).
- `401` — missing or invalid bearer.

#### 2.1.2 `POST /v1/workers/{worker_id}/heartbeat`

Liveness ping. Also where the Worker reports its current
**status** (idle / busy / etc.) per §4.

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `job_id` | string | optional | The Job the Worker is currently executing (helps operators debug). |
| `status` | string | optional | One of the 5 §4 statuses. |
| `status_message` | string | optional | Human-readable context. |

**Response 200:**

```json
{
  "version": "v1",
  "worker_id": "pi-agent-m2-001",
  "last_heartbeat_at": "2026-07-10T00:00:05Z",
  "last_job_id": "TASK-20260710-0010",
  "status": "busy",
  "status_message": "executing TASK-20260710-0010",
  "last_status_change_at": "2026-07-10T00:00:01Z"
}
```

**Errors:**
- `400` — invalid `status` value.
- `401` — missing or invalid bearer.
- `404` — Worker not registered.

**Heartbeat interval guidance:** the dispatcher reaps a
running Job whose Worker stops heartbeating for
`reaper.stale_running_sec` (default 60 s; configurable in
`config/reaper.json`). A conformant Worker MUST heartbeat at
least every `stale_running_sec / 3` while it has a running Job.
For the 60 s default, that's a heartbeat every 20 s or less;
the Pi Worker uses 15 s.

#### 2.1.3 `POST /v1/jobs/claim`

Pull a Job for the Worker. Returns either a Job + claim token
(200) or `404` (no claimable Job right now — sleep and retry).

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `worker_id` | string | required | The registered Worker. |
| `worker_type` | string | optional | Defaults to the Worker's registered `worker_type`. |
| `capabilities` | string[] | optional | The Worker's currently-advertised capabilities. Defaults to the registration's `capabilities`. |
| `max_jobs` | integer | optional | Soft hint, default 1. |

**Response 200:**

```json
{
  "version": "v1",
  "job_id": "TASK-20260710-0010",
  "task_id": "TASK-20260710-0010",
  "claim_token": "<plain-text-token-returned-once>",
  "title": "echo test",
  "type": "ops",
  "mode": "normal",
  "input": "echo hello from pi",
  "session_id": null,
  "runtime_type": "pi_agent",
  "adapter_name": "pi_agent",
  "external_run_id": null,
  "timeout_seconds": 900,
  "expected_artifacts": [],
  "required_capabilities": ["tool.shell"]
}
```

**Errors:**
- `400` — invalid body.
- `401` — missing or invalid bearer.
- `404` — Worker not registered, OR no claimable Job.
- `409` — Worker is already at `max_concurrent`.

**Token handling:** the `claim_token` is returned **once**,
in plain text, in this response. The Worker MUST store it
(e.g. in memory) and present it (or its sha256 hash) on every
subsequent lifecycle call. The dispatcher never returns the
plain token again; the Worker must persist it locally.

#### 2.1.4 `POST /v1/jobs/{job_id}/logs`

Append a line to the Job's log. The line is appended to
`<bridge_root>/logs/{job_id}.log` and emitted as a `task_events`
row of kind `"log"`.

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `claim_token` | string | required if not using `claim_token_hash` | Plain text token. |
| `claim_token_hash` | string | required if not using `claim_token` | sha256 hex digest of the token. |
| `line` | string | required | The line to append. Truncated to 500 chars. |

**Response 200:** `{"version": "v1", "job_id": "...", "appended": true}`

**Errors:** `400` (missing line), `401` (no token), `403` (bad token).

#### 2.1.5 `POST /v1/jobs/{job_id}/complete`

Terminal success. The Job transitions `running -> completed`.

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `claim_token` or `claim_token_hash` | string | required | Per §2.1.4. |
| `output_text` | string | optional | The Job's final output. Truncated to 200 KB. |
| `usage` | object | optional | Token / cost metadata (e.g. `{input_tokens, output_tokens, total_tokens}`). |
| `raw` | object | optional | Adapter-specific raw response, stored verbatim for audit. |

**Response 200:** `{"version": "v1", "job_id": "...", "status": "completed"}`

**Errors:** `404` (job not found), `403` (bad token), `409` (job not in `running`).

### 2.2 Supplementary endpoints (6)

The endpoints below are **not required** for a minimum-viable
Worker, but every conformant Worker SHOULD implement them.
They are needed for full lifecycle management and for the
dispatcher's Reaper to know when a Job is abandoned.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/jobs` | (Operator-side) Create a Job. Not called by a Worker in normal operation. |
| `GET`  | `/v1/jobs/{job_id}` | Read a Job's full state. |
| `POST` | `/v1/jobs/{job_id}/heartbeat` | Keep a running Job alive (a Worker is also expected to do this between `claim` and `complete`). |
| `POST` | `/v1/jobs/{job_id}/fail` | Terminal failure (`running -> failed`). The `error` field is required. |
| `POST` | `/v1/jobs/{job_id}/cancel` | Cancel a running Job (Worker-side). The dispatcher also tries to cancel the upstream adapter. |
| `GET`  | `/v1/workers` | List all registered Workers (filterable by `worker_type`). |
| `GET`  | `/v1/workers/{worker_id}` | Read a Worker's full record. |

The Pi Worker implements all of these.

### 2.3 Error model

All endpoints use the same error model. HTTP status codes
follow REST conventions; the body is always a JSON object with
a `detail` field describing the error in human-readable terms.

| Status | Meaning |
|---|---|
| 200 | Success. |
| 400 | Bad request — invalid body, unknown enum value, missing required field. |
| 401 | Missing or invalid bearer. |
| 403 | Bad claim token. |
| 404 | Resource not found (Worker / Job). |
| 409 | State conflict (e.g. Job not in `running`, Worker over `max_concurrent`). |
| 500 | Server error (e.g. `BRIDGE_API_KEY` not configured). |

---

## 3. API versioning

### 3.1 The /v1/... canonical path

Per ADR-007, the canonical path prefix for the Worker Runtime
Contract is `/v1/`. New Worker implementations should call
`/v1/...`. The legacy `/jobs/...` and `/workers/...` paths
(without the `/v1` prefix) are kept as aliases for backward
compatibility with AEE-2 / AEE-3 clients and the
`/runs` GPT-Action compat layer.

Both paths share the same handlers; there is no functional
difference. The `version: "v1"` field in every response
identifies the contract version.

### 3.2 Future versions

- Minor changes (new optional fields, new enum values that
  don't break the old ones, new endpoints) are backward
  compatible and stay on `/v1/`. A conformant v1 Worker
  continues to work.
- Breaking changes (a removed field, a renamed endpoint, a
  new required field) get a `/v2/...` prefix. The bridge
  serves BOTH `/v1/...` and `/v2/...` for a deprecation
  window of at least 1 minor version. The conformance
  checklist (§9) for v2 will be a separate document.
- The bridge never removes a v1 path while a v1 Worker is
  registered. Operators can check the Worker's contract
  version via `GET /v1/workers/{id}` (the `runtime_version`
  field).

### 3.3 Pre-v1 callers

AEE-2 / AEE-3 Workers that call `/jobs/...` or
`/workers/...` (no `/v1/` prefix) continue to work. They
simply don't see the `version` field as often. The bridge
does not enforce that a Worker uses `/v1/`.

---

## 4. Worker Metadata

Every Worker MUST populate the 8 metadata fields below at
register time. All are optional, but a conformant Worker
populates all 8.

| Field | Type | Example | Set when |
|---|---|---|---|
| `runtime_name` | string | `pi`, `hermes`, `claude_code` | Once, at process start. The runtime family. |
| `runtime_version` | string | `0.1.0` | Once, at process start. Semver recommended. |
| `operating_system` | string | `linux`, `darwin`, `windows` | Once, at process start. |
| `architecture` | string | `x86_64`, `arm64` | Once, at process start. |
| `python_version` | string | `3.11.2` | Once, at process start. The Python interpreter that runs the Worker process. |
| `node_version` | string | `v22.22.2` | Once, at process start. The Node interpreter that runs the Worker's runtime (if any). Empty string if no Node. |
| `git_version` | string | `2.34.1` | Once, at process start. The git CLI version. Empty string if no git. |
| `start_time` | string (ISO-8601 UTC) | `2026-07-10T00:00:00Z` | Once, at process start. The UTC time the Worker process started. |

### 4.1 What "once" means

A Worker that is restarted (process dies, supervisor brings
it back) MUST re-register with a fresh `start_time` and may
update `runtime_version` if it was upgraded. The
`registered_at` on the dispatcher's side is the time of the
**first** registration; the Worker's own `start_time` is the
time of the **current** process.

### 4.2 Where the values come from

- `runtime_name` and `runtime_version` — set by the runtime
  itself (e.g. Pi Worker's `--runtime-version` flag, or
  hard-coded in the runtime's source).
- `operating_system` — `platform.system().lower()`.
- `architecture` — `platform.machine()`.
- `python_version` — `platform.python_version()`.
- `node_version` — `node --version` (if Node is on `PATH`).
- `git_version` — `git --version` (if git is on `PATH`).
- `start_time` — `datetime.now(timezone.utc).isoformat()`.

The Pi Worker populates all 8 in `pi_worker.py`'s `register()`
function, sourced from `platform` + `subprocess`.

---

## 5. Worker Status

A conformant Worker reports its current **status** on every
heartbeat. The dispatcher records the status; it does not act
on it in AEE-4 (no scheduler, no auto-drain). AEE-5+ will
consume the status for scheduling decisions.

### 5.1 The 5-value vocabulary

| Status | Meaning | When a Worker should send it |
|---|---|---|
| `idle` | Ready to claim a Job. | Between Jobs, while alive. |
| `busy` | Currently executing a Job. | While the Job is in `running` (between `claim` and `complete` / `fail`). |
| `offline` | Going away gracefully; the dispatcher should not route new Jobs to it. | The Worker is shutting down and won't accept more work. |
| `draining` | Finishing the current Job, but will not claim new ones. | Like `busy`, but the Worker has decided to stop after the current Job. |
| `error` | The Worker hit an unrecoverable error; needs operator intervention. | The Worker process is alive but the runtime (e.g. LLM provider) is broken. |

The schema also has a sixth value, `unknown`, which is the
default for pre-AEE-4 Workers and is not part of the
conformance vocabulary. New Workers MUST send one of the 5
canonical values.

### 5.2 Status transition rules

A Worker may transition between statuses freely; the
dispatcher does not enforce a state machine. The convention
is:

```
   ┌──────────┐  start   ┌──────┐
   │ (none)   │ ───────► │ idle │
   └──────────┘          └──────┘
                            │   ▲
                            ▼   │ job arrives
                          ┌──────┐
                          │ busy │ ──── complete/fail
                          └──────┘        │
                            │             ▼
                            │   ┌──────────┐
                            └──►│ draining │  (Job done; going offline)
                                └──────────┘
                                      │ shutdown
                                      ▼
                                ┌──────────┐
                                │ offline  │
                                └──────────┘

   any ──── error ────► error
```

Practical guidance:

- A Worker that just started and has no Jobs sends `idle`.
- A Worker that just claimed a Job sends `busy` (in the
  heartbeat that carries the `job_id`).
- A Worker that decides to stop accepting Jobs sends
  `draining` (and continues to heartbeat the in-flight Job).
- A Worker that has finished a Job and is between Jobs sends
  `idle` again.
- A Worker that is shutting down sends `offline` in its
  final heartbeat, then exits.
- A Worker that hits an unrecoverable error sends `error`
  and then exits; the dispatcher will mark the in-flight
  Job as `failed` on the next reap.

### 5.3 Status validation

The bridge rejects an unknown `status` value with HTTP 400.
The DB layer is forgiving and silently coerces unknowns to
`unknown`, but the API is the contract. A Worker that sends
`status="BUSY"` (uppercase) gets a 400; it should send
`status="busy"`.

### 5.4 `last_status_change_at`

Every time the Worker's `status` field changes (compared to
the previous value), the bridge stamps `last_status_change_at`
with the current UTC time. A heartbeat with the same `status`
as the previous one does NOT bump the timestamp.

A conformant Worker does not need to read
`last_status_change_at`; it's an operator-side audit field.

---

## 6. Lease / Retry / Timeout

### 6.1 Claim token

A claim token is a 32-byte URL-safe random string, returned
plain once in the `POST /v1/jobs/claim` response, and stored
on the dispatcher's side as a sha256 hex digest.

- **Validity:** the token is valid for the lifetime of the
  Job. There is no expiry. If the Job transitions to a
  terminal state (`completed` / `failed` / `cancelled` /
  `timeout`), the token is invalidated.
- **Single use:** the token is presented on every lifecycle
  call (`heartbeat`, `logs`, `complete`, `fail`, `cancel`).
  It is not consumed by any single call; the dispatcher
  compares the presented token (or its sha256 hash) to the
  stored hash on every call.
- **Hash vs. plain:** the Worker MAY present the plain token
  (`claim_token` field) or the sha256 hex digest
  (`claim_token_hash` field). The dispatcher accepts either.
  A conformant Worker that wants to avoid sending the plain
  token over the wire can hash it locally and send the hash
  on every call after the first.

### 6.2 Heartbeat staleness

A running Job whose `tasks.heartbeat_at` is older than
`reaper.stale_running_sec` (default 60 s; configurable in
`config/reaper.json`) is reaped to `timeout` by the dispatcher's
Reaper thread.

- A conformant Worker MUST heartbeat its running Job at
  least every `stale_running_sec / 3`.
- A conformant Worker MUST also heartbeat the **Worker**
  (i.e. `POST /v1/workers/{id}/heartbeat`) at the same
  cadence, with `status="busy"` and the `job_id` set.
- A Worker that misses 3+ heartbeats in a row loses its
  lease: the dispatcher reaps the Job to `timeout`, and
  the Worker's next `complete` / `fail` call gets HTTP 409.

### 6.3 Abandoned jobs

A Job is "abandoned" when:
- The Worker that claimed it stops heartbeating (the
  Reaper times the Job out), OR
- The Worker process dies without sending `fail` (the
  Reaper's next tick finds the `heartbeat_at` stale).

The Reaper transitions the abandoned Job to `timeout` (not
`failed`; the distinction matters for the dispatcher's
audit log). The dispatcher's `TaskManager.retry()` method
can clone the abandoned Job into a new `queued` Job for
re-execution by any conformant Worker; this is operator
action, not automatic.

### 6.4 Worker disconnect

AEE-4 does not have a `DELETE /v1/workers/{id}` endpoint.
A Worker that wants to leave the registry either:
- Sends a final `status="offline"` heartbeat and exits, OR
- Just exits. The dispatcher does NOT remove the row.
  Operators can `DELETE FROM workers WHERE worker_id = ?`
  via the CLI (or the future AEE-5 UI).

A conformant Worker that is restarted (e.g. by supervisord)
re-registers. Re-registration is idempotent; the dispatcher
preserves `registered_at` and the historical `status`
transitions are visible in `last_status_change_at`.

### 6.5 Lease and Retry at the contract level

- A Job is "leased" from the moment of `claim` until it
  transitions to a terminal state.
- A Job CANNOT be claimed twice. The dispatcher uses an
  atomic UPDATE with `WHERE status='queued' AND worker_id IS NULL`
  to guarantee this; a loser of the race gets HTTP 409.
- A Job that reaches `failed` / `cancelled` / `timeout` can
  be retried by an operator (`POST /v1/jobs` with
  `retry_of=<id>`; this is the AEE-2 retry path that uses
  the existing `TaskManager.retry()` method). The new Job
  has a fresh `task_id` and is claimable by any conformant
  Worker whose `worker_type` matches.

---

## 7. Capability Declaration

A conformant Worker declares its capabilities at register
time and on every claim. The bridge matches Jobs to Workers
by the rule `set(job.required_capabilities) ⊆ set(worker.capabilities)`
(see ADR-003 and `docs/AEE4_CAPABILITY_NAMING_SPEC.md`).

### 7.1 Namespace

A capability string is:

```
<category>.<name>[.<sub>]
```

where `<category>` ∈ `{runtime, tool, network, protocol, container}`.
The full format rules, regex, reserved namespaces, and
proposal workflow are in
`docs/AEE4_CAPABILITY_NAMING_SPEC.md`.

### 7.2 The canonical categories

| Category | Meaning | Examples |
|---|---|---|
| `runtime.*` | Which Worker runtime is this. | `runtime.pi`, `runtime.hermes`, `runtime.claude` |
| `tool.*` | Which execution tools the Worker can invoke. | `tool.shell`, `tool.python`, `tool.git`, `tool.filesystem` |
| `network.*` | Network access the Worker has. | `network.https`, `network.ssh` |
| `protocol.*` | Wire protocols the Worker speaks. | `protocol.mcp`, `protocol.a2a` |
| `container.*` | Container / sandbox primitives. | `container.docker`, `container.podman` |

### 7.3 Normalization

The bridge normalizes every capability string to
`lowercase / trim / dedupe / sort` before persisting. A
Worker that registers `["Shell", "SHELL", " shell "]` ends
up with `["shell"]` in the database. A conformant Worker
sends pre-normalized strings to be a good citizen, but the
dispatcher is forgiving.

### 7.4 Required capability: `runtime.<type>`

A conformant Worker MUST declare at least one `runtime.*`
capability whose `<type>` matches its `worker_type`. The
Pi Worker declares `runtime.pi` and uses `worker_type="pi_agent"`.
The dispatcher does not enforce this match today (the
matcher only checks the worker_type vs the job's
`adapter_name`), but the convention is enforced by the
naming spec so a future matcher can use it.

### 7.5 Capability subset rule

A Job with `required_capabilities=["tool.shell", "tool.git"]`
is claimable by a Worker that declares
`["runtime.pi", "tool.shell", "tool.python", "tool.git"]`
(subset match) but NOT by a Worker that declares
`["runtime.pi", "tool.shell"]` only (missing `tool.git`).

A Job with `required_capabilities=[]` is claimable by any
Worker whose `worker_type` matches the Job's `adapter_name`.
This is the AEE-2 / AEE-3 default.

---

## 8. Worked example

A complete `register → claim → heartbeat → complete` cycle,
with curl. Assume `BRIDGE_API_KEY=br_xxx` and the bridge is
on `127.0.0.1:8787`.

```bash
# 1. Register a Pi Worker
curl -sS -X POST http://127.0.0.1:8787/v1/workers/register \
  -H "Authorization: Bearer br_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_name": "pi-agent-m2-001",
    "worker_type": "pi_agent",
    "capabilities": ["runtime.pi", "tool.shell", "tool.python", "tool.git"],
    "workdir_allowlist": ["/home/ubuntu/hermes-runtime-bridge/runtime_data/pi-agent"],
    "max_concurrent": 1,
    "runtime_name": "pi",
    "runtime_version": "0.1.0",
    "operating_system": "linux",
    "architecture": "x86_64",
    "python_version": "3.11.2",
    "node_version": "v22.22.2",
    "git_version": "2.34.1",
    "start_time": "2026-07-10T00:00:00Z"
  }'
# {"version":"v1","worker_id":"pi-agent-m2-001","registered":true,
#  "registered_at":"2026-07-10T00:00:00Z","worker_type":"pi_agent"}

# 2. Heartbeat (between Jobs)
curl -sS -X POST http://127.0.0.1:8787/v1/workers/pi-agent-m2-001/heartbeat \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d '{"status": "idle"}'
# {"version":"v1","worker_id":"...","last_heartbeat_at":"...","status":"idle", ...}

# 3. Operator creates a Job (the Worker doesn't do this)
curl -sS -X POST http://127.0.0.1:8787/v1/jobs \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d '{
    "title": "echo test",
    "input": "echo hello from pi",
    "target_runtime": "pi_agent",
    "required_capabilities": ["tool.shell"]
  }'
# {"version":"v1","job_id":"TASK-20260710-0010", "status":"queued", ...}

# 4. Worker claims the Job
CLAIM=$(curl -sS -X POST http://127.0.0.1:8787/v1/jobs/claim \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d '{
    "worker_id": "pi-agent-m2-001",
    "worker_type": "pi_agent",
    "capabilities": ["runtime.pi", "tool.shell", "tool.python", "tool.git"]
  }')
echo "$CLAIM" | jq .
# {"version":"v1","job_id":"TASK-20260710-0010","claim_token":"<plain>", ...}
JOB_ID=$(echo "$CLAIM" | jq -r .job_id)
TOKEN=$(echo "$CLAIM" | jq -r .claim_token)
TOKEN_HASH=$(echo -n "$TOKEN" | sha256sum | cut -d' ' -f1)

# 5. Heartbeat the running Job (also report Worker status=busy)
curl -sS -X POST http://127.0.0.1:8787/v1/workers/pi-agent-m2-001/heartbeat \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d "{\"status\": \"busy\", \"status_message\": \"executing $JOB_ID\", \"job_id\": \"$JOB_ID\"}"

curl -sS -X POST http://127.0.0.1:8787/v1/jobs/$JOB_ID/heartbeat \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d "{\"claim_token_hash\": \"$TOKEN_HASH\"}"

# 6. Stream a log line
curl -sS -X POST http://127.0.0.1:8787/v1/jobs/$JOB_ID/logs \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d "{\"claim_token_hash\": \"$TOKEN_HASH\", \"line\": \"shell: echo hello from pi\"}"

# 7. Complete
curl -sS -X POST http://127.0.0.1:8787/v1/jobs/$JOB_ID/complete \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d "{
    \"claim_token_hash\": \"$TOKEN_HASH\",
    \"output_text\": \"hello from pi\\n\",
    \"usage\": {\"input_tokens\": 50, \"output_tokens\": 12, \"total_tokens\": 62}
  }"
# {"version":"v1","job_id":"TASK-20260710-0010","status":"completed"}

# 8. Heartbeat (back to idle)
curl -sS -X POST http://127.0.0.1:8787/v1/workers/pi-agent-m2-001/heartbeat \
  -H "Authorization: Bearer br_xxx" -H "Content-Type: application/json" \
  -d '{"status": "idle"}'
```

---

## 9. Conformance checklist

A new Worker runtime is **conformant** if it satisfies every
item below. The Pi Worker is the reference implementation.

### 9.1 Register

- [ ] Calls `POST /v1/workers/register` on startup, populating
      all 8 metadata fields plus a real `worker_type` and a
      non-empty `capabilities` list.
- [ ] Uses the same `worker_id` on re-registration
      (idempotent; the dispatcher preserves `registered_at`).
- [ ] On 5xx from the bridge, retries with exponential backoff
      (capped at 30 s) for at least 5 minutes before exiting.

### 9.2 Heartbeat (Worker)

- [ ] Sends `POST /v1/workers/{id}/heartbeat` at least every
      `stale_running_sec / 3` (default 20 s) while alive.
- [ ] Reports `status="idle"` between Jobs.
- [ ] Reports `status="busy"` while executing a Job, with the
      `job_id` set in the heartbeat body.
- [ ] Reports `status="draining"` if it wants to finish the
      current Job but reject new ones.
- [ ] Reports `status="offline"` in the final heartbeat before
      exiting.
- [ ] Reports `status="error"` on an unrecoverable error
      (the in-flight Job will be reaped to `failed` / `timeout`).

### 9.3 Claim

- [ ] Calls `POST /v1/jobs/claim` in a loop, sleeping
      `claim_interval_sec` (default 5 s) on 404.
- [ ] Stores the returned `claim_token` (or its sha256 hash)
      locally; never sends the plain token over the wire
      except on the first lifecycle call after claim.
- [ ] On 409 ("job was claimed by another worker"), backs off
      and retries after `claim_interval_sec`.

### 9.4 Execute

- [ ] Builds a per-Job workdir inside the registered
      `workdir_allowlist`; refuses to execute outside it.
- [ ] Streams progress via `POST /v1/jobs/{id}/logs` at least
      once per step (or every 30 s, whichever comes first).
- [ ] Honors the Job's `timeout_seconds` (default 900) by
      aborting execution and calling `/v1/jobs/{id}/fail` with
      `error="job exceeded timeout_seconds"`.
- [ ] Sends `POST /v1/jobs/{id}/heartbeat` at the same cadence
      as the Worker heartbeat (§9.2) while the Job is running.

### 9.5 Complete / Fail

- [ ] On success: calls `POST /v1/jobs/{id}/complete` with
      `output_text` (or `output`), `usage`, and `raw`.
- [ ] On failure: calls `POST /v1/jobs/{id}/fail` with
      `error` set to a short human-readable message.
- [ ] On unexpected exception: catches, calls `/fail`, then
      continues to the next claim.
- [ ] Does NOT catch the `IllegalTransition` HTTP 409
      response silently — that's the dispatcher telling
      the Worker the Job was already reaped, and the Worker
      should log and move on.

### 9.6 Lifecycle hygiene

- [ ] Refuses to send PII, API keys, or other secrets in
      `output_text` (the dispatcher does not redact, by
      design).
- [ ] Sends a final `status="offline"` heartbeat before
      process exit (clean shutdown).
- [ ] Does NOT call `/v1/workers/{id}` DELETE — that endpoint
      does not exist in v1. Operators clean up via the CLI.

---

## 10. Open questions (AEE-5+)

- **Multi-Worker same `worker_id`.** AEE-4 forbids two
  Workers with the same `worker_id`. AEE-5 may relax this
  to allow horizontal scaling.
- **Claim token rotation.** AEE-4 has one token per Job.
  AEE-5 may rotate the token on long-running Jobs.
- **Worker-side metrics.** AEE-4 doesn't standardize a
  `metrics` field on heartbeats. AEE-5 may add CPU / memory
  / queue depth.
- **Drain protocol.** `status="draining"` is advisory in
  AEE-4. AEE-5 may add a `POST /v1/jobs/_reassign` endpoint
  to move in-flight Jobs from a draining Worker to another.

These are intentionally out of scope for v1. The contract
above is the floor; future versions will extend it.

---

## 11. References

- ADR-006 — Worker Runtime Contract is canonical. (`AEE_MASTER_PLAN.md` §9)
- ADR-007 — Path-prefix versioning, `/v1/...` canonical. (`AEE_MASTER_PLAN.md` §9)
- ADR-008 — Worker Status is a 5-value first-class state. (`AEE_MASTER_PLAN.md` §9)
- ADR-003 — Capability Naming Convention. (`AEE_MASTER_PLAN.md` §9 + `AEE4_CAPABILITY_NAMING_SPEC.md`)
- ADR-004 — Capability Extension Point. (`AEE_MASTER_PLAN.md` §9 + `AEE4_CAPABILITY_EXTENSION_POINT.md`)
- ADR-005 — Future Matcher Design. (`AEE_MASTER_PLAN.md` §9 + `AEE4_FUTURE_MATCHER_DESIGN.md`)
- `docs/AEE4_PI_REFERENCE_IMPLEMENTATION_REPORT.md` — the Pi Worker as the first conformant runtime.
- `docs/AEE_RUNTIME_INTEGRATION_GUIDE.md` — the runtime-agnostic integration handbook.
- `Abacus/AEE_MASTER_PLAN.md` — the master plan; §10.5 enumerates AEE-4 deliverables.
