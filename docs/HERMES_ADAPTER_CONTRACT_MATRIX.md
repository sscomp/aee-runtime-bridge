# Hermes Adapter Contract Matrix

> **Migrated per Master Plan §21.9 (Documentation Migration).** This
> matrix was **moved** (not copied) from the AEE-MINI repository
> (`/home/ubuntu/Abacus/aee-runtime-api-mini/docs/HERMES_ADAPTER_CONTRACT_MATRIX.md`)
> into the unified repo at `/home/ubuntu/hermes-runtime-bridge/docs/`. The
> AEE-MINI archive copy stays on disk untouched as the frozen archive
> reference; **this file is the canonical copy** going forward. The target
> file reference below was updated from the AEE-MINI
> `aee_runtime_api/adapters/hermes.py` to the unified
> `aee/adapters/hermes_adapter.py`. The contract body (endpoint tables,
> evidence labels, status vocabulary, configurable knobs, layer
> separation) is preserved verbatim from the AEE-MINI original.

**Date:** 2026-07-12 (migrated 2026-07-18 per §21.9)
**Target file:** `aee/adapters/hermes_adapter.py` (unified repo)
**Companion stub:** `tests/test_hermes_adapter.py` (in the AEE-MINI
archive; the unified repo's adapter tests live in `aee/tests/`)
**Source-of-truth repo:** `/home/ubuntu/hermes-runtime-bridge/`

This matrix is the source of truth for what the Hermes adapter is known
to do today, what is configurable, and what is still assumed. It is
intentionally explicit: a Custom GPT Action that targets a Hermes
deployment should be able to read this document and know what shape the
adapter sends and accepts.

The evidence labels mean:

| Label                                | Meaning                                                                            |
|--------------------------------------|------------------------------------------------------------------------------------|
| `VERIFIED_FROM_CODE`                 | The behaviour is implemented in `aee_runtime_api/adapters/hermes.py`.             |
| `VERIFIED_FROM_TEST_STUB`            | The behaviour is covered by the local stub server in `tests/test_hermes_adapter.py` (or the strengthened stub added in this hardening pass). |
| `VERIFIED_FROM_REAL_NON_PRODUCTION_ENDPOINT` | The behaviour was exercised against a non-production Hermes instance. |
| `ASSUMED`                            | A working assumption; the adapter can be configured but has not been tested with this shape. |
| `UNKNOWN`                            | Not investigated and not yet safe to rely on.                                      |

The current local environment does not have access to a production Hermes
endpoint, so no item carries the `VERIFIED_FROM_REAL_NON_PRODUCTION_ENDPOINT`
label. Anything labelled `ASSUMED` must be confirmed by N2 before the
production installer is run.

## 1. Endpoints

### 1.1 `GET /health`

| Aspect              | Value                                                                                  | Evidence                       | Risk | Configurable | Action          |
|---------------------|----------------------------------------------------------------------------------------|--------------------------------|------|--------------|-----------------|
| Method              | `GET`                                                                                  | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Path                | `{worker.base_url}{health_path}` (default `/health`)                                   | `VERIFIED_FROM_CODE`           | low  | yes (`health_path`) | keep as-is |
| Headers             | `Authorization: Bearer <value of worker.api_key_env>` (omitted when env unset)         | `VERIFIED_FROM_CODE`           | low  | indirect (env) | keep as-is |
| Request body        | none                                                                                   | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Status 200          | Adapter reports `status="ok"`                                                          | `VERIFIED_FROM_TEST_STUB`      | low  | no           | keep as-is      |
| Status 5xx          | Adapter reports `status="error"`, includes detail string                               | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Status 4xx          | Adapter reports `status="degraded"`                                                    | `VERIFIED_FROM_CODE`           | med  | no           | keep as-is; document in handoff that 401/403 on /health is interpreted as "degraded", not "not_configured" |
| Transport failure   | Adapter reports `status="error"`, surfaces the exception string                        | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Timeout             | `request_timeout_seconds` (default 30)                                                 | `VERIFIED_FROM_CODE`           | low  | yes          | keep as-is      |

### 1.2 `POST /runs` (submit)

| Aspect              | Value                                                                                  | Evidence                       | Risk | Configurable | Action          |
|---------------------|----------------------------------------------------------------------------------------|--------------------------------|------|--------------|-----------------|
| Method              | `POST`                                                                                 | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Path                | `{worker.base_url}{submit_path}` (default `/runs`)                                     | `VERIFIED_FROM_CODE`           | low  | yes (`submit_path`) | keep as-is |
| Headers             | `Content-Type: application/json` + bearer                                              | `VERIFIED_FROM_CODE`           | low  | indirect     | keep as-is      |
| Request body        | `{input, session_id, mode, timeout_seconds[, working_directory]}`                      | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is; remove optional fields with no Hermes analogue if N2 reports trouble |
| 2xx with `run_id`   | Adapter stores `external_run_id` and treats the run as submitted                       | `VERIFIED_FROM_TEST_STUB`      | low  | no           | keep as-is      |
| 5xx                 | Adapter retries up to `submit_max_retries` (default 2); otherwise raises `AdapterError` | `VERIFIED_FROM_CODE`           | med  | yes          | keep as-is; document the retry policy in the handoff |
| 4xx (non-2xx/5xx)   | Adapter raises `AdapterError(code=hermes_submit_failed)`; no retry                    | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Connection drop     | Treated as transport error: retry then `AdapterError(code=hermes_submit_exhausted)`     | `VERIFIED_FROM_CODE`           | high | yes (retry count) | strengthen: ambiguous submit is now recorded as a run failure in the dispatcher, never silently re-submitted (see §4) |
| 2xx without `run_id` | Adapter raises `AdapterError(code=hermes_no_run_id)`                                   | `VERIFIED_FROM_CODE`           | med  | no           | keep as-is      |
| Malformed JSON      | Adapter raises `AdapterError`; treated as submit failure                               | `VERIFIED_FROM_CODE`           | med  | no           | keep as-is      |

### 1.3 `GET /runs/{run_id}` (inspect / status)

| Aspect              | Value                                                                                  | Evidence                       | Risk | Configurable | Action          |
|---------------------|----------------------------------------------------------------------------------------|--------------------------------|------|--------------|-----------------|
| Method              | `GET`                                                                                  | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Path                | `{worker.base_url}{status_path}` with `{run_id}` substituted (default `/runs/{run_id}`) | `VERIFIED_FROM_CODE`           | low  | yes (`status_path`) | keep as-is |
| 2xx                 | Parsed into `WorkerSnapshot`; status text is lowercased and matched against the runtime vocabulary | `VERIFIED_FROM_CODE`  | med  | no           | keep as-is; "completed/succeeded/complete" map to COMPLETED, "failed/error" to FAILED, "cancelled/canceled" to CANCELLED, "timed_out/timeout" to TIMED_OUT, anything else stays "running" |
| 404                 | Adapter returns `WorkerSnapshot(status="not_found", error="hermes lost the run")`       | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is; treated as a terminal failure at the dispatcher layer |
| 5xx                 | Retry up to `inspect_max_retries` (default 3); otherwise raise `AdapterError`          | `VERIFIED_FROM_CODE`           | low  | yes          | keep as-is      |
| 4xx (other)         | Raise `AdapterError(code=hermes_inspect_failed)`                                       | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Transport failure   | Retry, then raise `AdapterError`                                                       | `VERIFIED_FROM_CODE`           | low  | yes          | keep as-is      |
| Delayed response    | Bounded by `request_timeout_seconds` (default 30); no extra long-poll knob              | `VERIFIED_FROM_CODE`           | med  | yes          | keep as-is; N2 handoff must record the actual value used in production |
| Auth failure (401)  | Raise `AdapterError(code=hermes_inspect_failed)` (no special handling)                 | `VERIFIED_FROM_CODE`           | med  | no           | document in handoff: a 401 from the worker means the worker credential is wrong, not that the run is missing |

### 1.4 `POST /runs/{run_id}/stop`

| Aspect              | Value                                                                                  | Evidence                       | Risk | Configurable | Action          |
|---------------------|----------------------------------------------------------------------------------------|--------------------------------|------|--------------|-----------------|
| Method              | `POST`                                                                                 | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| Path                | `{worker.base_url}{stop_path}` with `{run_id}` substituted (default `/runs/{run_id}/stop`) | `VERIFIED_FROM_CODE`       | low  | yes (`stop_path`) | keep as-is |
| 2xx                 | Adapter returns a `WorkerSnapshot` with status text (default `cancel_requested`)       | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is      |
| 404                 | Adapter returns `WorkerSnapshot(status="not_found", error="hermes lost the run")`       | `VERIFIED_FROM_CODE`           | low  | no           | keep as-is; dispatcher treats this as "the worker already forgot the run", which is fine |
| 5xx                 | Retry up to `stop_max_retries` (default 3); otherwise raise `AdapterError`             | `VERIFIED_FROM_CODE`           | low  | yes          | keep as-is      |
| Transport failure   | Retry, then raise `AdapterError`                                                       | `VERIFIED_FROM_CODE`           | low  | yes          | keep as-is      |

## 2. Status vocabulary

The adapter lowercases the worker `status` string and maps it onto the
runtime vocabulary using a small allow-list. Anything outside the
allow-list is treated as a non-terminal snapshot: the dispatcher keeps
the run in `running`/`cancel_requested` and persists the worker status
as `last_event` so a human can diagnose.

| Worker status text           | Runtime status            | Evidence                  | Configurable |
|------------------------------|---------------------------|---------------------------|--------------|
| `completed` / `succeeded` / `complete` | `completed`     | `VERIFIED_FROM_CODE`      | no           |
| `failed` / `error`                     | `failed`        | `VERIFIED_FROM_CODE`      | no           |
| `cancelled` / `canceled`               | `cancelled`     | `VERIFIED_FROM_CODE`      | no           |
| `timed_out` / `timeout`                | `timed_out`     | `VERIFIED_FROM_CODE`      | no           |
| `running` / `queued` / anything else   | non-terminal    | `VERIFIED_FROM_CODE`      | no           |

The unknown-status policy is documented in §5 below.

## 3. Stop semantics

* `POST /runs/{run_id}/stop` is **best-effort**: a non-2xx response is
  retried up to `stop_max_retries` times and then logged. The runtime
  continues to call `inspect()` and only transitions to `cancelled`
  once the worker reports a terminal status.
* A 404 on stop is **not** an error at the runtime layer — it means the
  worker already forgot the run. The runtime records
  `WorkerSnapshot(status="not_found", error="hermes lost the run")` and
  the dispatcher will reconcile on the next inspect.

## 4. Unknown-state policy (E2)

When the adapter returns a `WorkerSnapshot` whose status is not in the
allow-list:

* The runtime **never** infers success.
* The runtime **never** infers failure unless the adapter explicitly
  reported it.
* The dispatcher:
  1. Persists `last_event = "worker reported non-terminal state: <raw>"`.
  2. Persists `heartbeat_at = now`.
  3. Continues to call `inspect()` on the next poll.
  4. If the raw status remains unknown across `worker_recovery_grace_seconds`
     (default 60 s) of cumulative running time, the run is failed with
     `error_code="unknown_state"`, `error_message="worker reported non-terminal state for too long: <raw>"`,
     and an event of type `unknown_state_failure` is written.
* This keeps an unknown worker state visible without silently dropping it
  on the floor and without blocking the queue forever.

The 60 s default lives in the existing `execution.worker_recovery_grace_seconds`
config knob; deployments that need a different policy can override it.

## 5. Ambiguous submit policy (E3)

A submit is ambiguous when:

* the request was sent and a `run_id` may have been created on the
  worker, but the response was lost (connection drop, malformed body,
  HTTP 5xx followed by a refused retry).
* the worker has no stable client-side idempotency key we can use to
  re-look-up the run.

The adapter does **not** retry an ambiguous submit. After the first
attempt fails with a transport error or an unparseable response, the
dispatcher records the run as `failed` with
`error_code="submit_ambiguous"` and the operator must reconcile by hand
(either inspect the worker, or use the same `idempotency_key` from the
caller to re-submit, which will be rejected with 409 by the local
store and can be reissued with a fresh key after a human checks the
worker state).

This is the safe outcome required by the spec: a duplicate submission
on the worker would be a much larger problem than a single failed local
run.

## 6. Auth policy

* The bearer is read from the environment variable named in
  `worker.api_key_env` (default `HERMES_API_KEY`) and sent as
  `Authorization: Bearer <value>` on every call.
* An unset env produces a request **without** the Authorization header
  (the adapter does not raise). This is intentional for local
  development but is a deployment misconfiguration in production.
* The adapter never logs the raw token. The `_log_safe` helper routes
  the `Authorization` value through `secret_redact` before it reaches
  the JSON logger.

## 7. Layer separation (A2)

| Layer                       | Where it lives                          | What it owns                                              |
|-----------------------------|------------------------------------------|-----------------------------------------------------------|
| Public API schema           | `openapi/aee-runtime-api-mini-v1.yaml` and `aee_runtime_api/api/` | Path templates, request/response shape, error codes, OpenAPI operation IDs |
| Local run model             | `aee_runtime_api/core/models.py` and `aee_runtime_api/storage/` | Local run record, SQLite schema, state machine             |
| Local state machine         | `aee_runtime_api/core/state_machine.py`  | Legal transitions, terminal immutability                   |
| Hermes transport model      | `aee_runtime_api/adapters/hermes.py`     | HTTP method, path template substitution, JSON body shape, headers, retry counts, status mapping |
| Hermes status translation   | `_parse_hermes_run` in `adapters/hermes.py` plus `_map_snapshot` in `dispatch/worker_loop.py` | Lowercasing, allow-list, terminal mapping, unknown-state policy |

The five layers do not share code. The public API does not mention
`hermes`; it uses generic fields like `worker_type`, `worker_name`,
`adapter_name`, `external_run_id`. The Hermes-specific field on
`/health` (`hermes: "ok" | "not_configured"`) is the single exception
and exists only for legacy GPT-Action compatibility.

## 8. Configurable knobs (recap)

| Knob                        | Default                | Source                                   |
|-----------------------------|------------------------|------------------------------------------|
| `worker.base_url`           | `http://127.0.0.1:8642` | YAML / `AEE_CONFIG_PATH`                |
| `worker.api_key_env`        | `HERMES_API_KEY`        | YAML / `AEE_CONFIG_PATH`                |
| `worker.submit_path`        | `/runs`                | YAML / `AEE_CONFIG_PATH`                |
| `worker.status_path`        | `/runs/{run_id}`       | YAML / `AEE_CONFIG_PATH`                |
| `worker.stop_path`          | `/runs/{run_id}/stop`   | YAML / `AEE_CONFIG_PATH`                |
| `worker.health_path`        | `/health`              | YAML / `AEE_CONFIG_PATH`                |
| `worker.request_timeout_seconds` | `30`              | YAML / `AEE_CONFIG_PATH`                |
| `worker.submit_max_retries` | `2`                    | YAML / `AEE_CONFIG_PATH`                |
| `worker.inspect_max_retries`| `3`                    | YAML / `AEE_CONFIG_PATH`                |
| `worker.stop_max_retries`   | `3`                    | YAML / `AEE_CONFIG_PATH`                |
| `worker.poll_interval_seconds` | `2`                  | YAML / `AEE_CONFIG_PATH`                |
| `execution.worker_recovery_grace_seconds` | `60`      | YAML / `AEE_CONFIG_PATH`                |

## 9. What is still assumed

The following items are *assumed* in the current code; they need to be
confirmed before the N2 installer is run against a real Hermes
endpoint. None of them are showstoppers in isolation, but they are the
items an operator should check first:

| Assumption | Risk |
|------------|------|
| The submit endpoint returns a `run_id` field (we also accept `id` as a fallback). | high |
| The inspect endpoint returns a JSON body with at least `status`. | high |
| The worker status vocabulary uses the documented strings (`completed`, `failed`, `cancelled`, `timed_out`, `running`). | med |
| A 404 from inspect means "the worker lost the run". | med |
| The worker accepts a `POST /runs` JSON body with `input`, `session_id`, `mode`, `timeout_seconds`, and optional `working_directory`. | med |
| The stop endpoint is idempotent and a 404 there is not an error. | low |

If any of these is wrong, the adapter is the only place to change.
The dispatcher, the API layer, and the storage layer stay untouched.
