# AEE-5 Runtime Registry — API Reference

**Version:** 1.0
**Date:** 2026-07-10
**Base path:** `/v1/runtimes` (canonical) — `/runtimes` (legacy alias)

All endpoints require the same `Authorization: Bearer <key>` header
as the AEE-4 endpoints. The key is `BRIDGE_API_KEY` (or
`DISPATCHER_API_KEY` override).

The AEE-5 endpoints are mounted by `aee/api/__init__.py` under both
prefixes; the responses are identical.

---

## 1. Common shapes

### Runtime

```json
{
  "runtime_id": "aee-lightweight-local",
  "runtime_type": "aee_lightweight",
  "display_name": "AEE Lightweight Local Runtime",
  "version": "1.0.0",
  "enabled": true,
  "endpoint": "local",
  "capabilities": [
    "runtime.aee_runtime",
    "task.filesystem",
    "task.git",
    "task.python",
    "task.shell"
  ],
  "labels": {
    "environment": "local",
    "trust_level": "internal"
  },
  "limits": {
    "max_concurrency": 2,
    "timeout_seconds": 1800
  },
  "health": {
    "status": "unknown",
    "last_checked_at": null,
    "message": null
  },
  "registered_at": "2026-07-10T10:00:00Z",
  "updated_at": null
}
```

### Error responses

| Status | Code | When |
|--------|------|------|
| 400 | `RuntimeValidationError` | Body / payload validation failed |
| 401 | (auth) | Missing / wrong bearer token |
| 404 | `RuntimeNotFoundError` | runtime_id not found |
| 409 | `RuntimeRegistryError` | Duplicate runtime_id |
| 422 | `AEE_RUNTIME_NOT_FOUND` | (Future) Job-level: no Runtime matches requirements |
| 500 | (config) | BRIDGE_API_KEY not set |

The error body is always `{"detail": "..."}` for HTTPException
errors, or `{"detail": {"code": "...", "message": "...", "details": {...}}}`
for `RuntimeNotFoundError` (404 from the GET-by-id endpoint).

---

## 2. Endpoints

### 2.1 `POST /v1/runtimes` — register a Runtime

```http
POST /v1/runtimes
Authorization: Bearer <key>
Content-Type: application/json

{
  "runtime_id": "r-shell-01",
  "runtime_type": "shell",
  "display_name": "Shell runtime",
  "version": "1.0.0",
  "enabled": true,
  "endpoint": "http://shell.example.invalid:9000",
  "capabilities": ["task.shell", "task.git"],
  "labels": {
    "environment": "sandbox",
    "trust_level": "external"
  },
  "limits": {
    "max_concurrency": 4,
    "timeout_seconds": 600
  },
  "health": {
    "status": "unknown"
  }
}
```

Required fields: `runtime_id`, `runtime_type`. All other
fields are optional with sensible defaults.

Response: `200 OK` with the full Runtime descriptor.

Errors:
* `400` — `runtime_id` / `runtime_type` missing or empty;
  `capabilities` is not a list; `limits.max_concurrency < 1`;
  `health.status` not in `{unknown, healthy, degraded, unhealthy, offline}`.
* `409` — `runtime_id` already registered (without `replace=True`).

### 2.2 `GET /v1/runtimes` — list Runtimes

```http
GET /v1/runtimes?enabled=true&runtime_type=shell&capability=task.shell
Authorization: Bearer <key>
```

Query parameters:

| Param | Type | Description |
|-------|------|-------------|
| `enabled` | bool (`true`/`false`) | Filter by enabled flag |
| `runtime_type` | str | Exact-match filter |
| `capability` | str | Filter Runtimes that advertise this capability |

For `label.<key>=<value>` filters (e.g. `?label.environment=local`),
use `GET /v1/runtimes/search`.

Response:

```json
{
  "version": "v1",
  "count": 2,
  "runtimes": [ { ... }, { ... } ]
}
```

### 2.3 `GET /v1/runtimes/search` — list with `label.*` filters

```http
GET /v1/runtimes/search?label.environment=local
Authorization: Bearer <key>
```

Combines the standard filters (`enabled`, `runtime_type`,
`capability`) with arbitrary `label.<key>=<value>` query
parameters. Multiple `label.*` filters are AND'd (every
key=value must match).

Response: same shape as `GET /v1/runtimes`.

### 2.4 `GET /v1/runtimes/{runtime_id}` — Runtime detail

```http
GET /v1/runtimes/aee-lightweight-local
Authorization: Bearer <key>
```

Response: `200 OK` with the full Runtime descriptor.

Errors: `404` if the Runtime is not registered.

### 2.5 `PATCH /v1/runtimes/{runtime_id}` — partial update

```http
PATCH /v1/runtimes/r1
Authorization: Bearer <key>
Content-Type: application/json

{
  "display_name": "Renamed",
  "labels": { "environment": "sandbox" },
  "capabilities": ["task.shell", "task.git"]
}
```

Updatable fields: `runtime_type`, `display_name`, `version`,
`endpoint`, `capabilities`, `labels`, `limits`, `health`,
`enabled`. The `runtime_id` and `registered_at` are
immutable.

Response: `200 OK` with the updated Runtime descriptor.

### 2.6 `DELETE /v1/runtimes/{runtime_id}` — remove

```http
DELETE /v1/runtimes/r1
Authorization: Bearer <key>
```

Response: `200 OK` with `{"version": "v1", "runtime_id": "r1", "deleted": true}`.

Errors: `404` if the Runtime is not registered.

### 2.7 `POST /v1/runtimes/{runtime_id}/enable` — set enabled=true

```http
POST /v1/runtimes/r1/enable
Authorization: Bearer <key>
```

Response: `200 OK` with the updated Runtime descriptor.

### 2.8 `POST /v1/runtimes/{runtime_id}/disable` — set enabled=false

```http
POST /v1/runtimes/r1/disable
Authorization: Bearer <key>
```

Response: `200 OK` with the updated Runtime descriptor.

### 2.9 `POST /v1/runtimes/{runtime_id}/health-check` — read health

```http
POST /v1/runtimes/r1/health-check
Authorization: Bearer <key>
```

Reads the Runtime's current health status. The v1
implementation returns the stored health; a future
AEE-6+ may probe the Runtime's wire endpoint. The
response shape is the same so callers don't need to
change.

Response:

```json
{
  "version": "v1",
  "runtime_id": "r1",
  "health": {
    "status": "healthy",
    "last_checked_at": "2026-07-10T10:00:00Z",
    "message": "probe ok"
  }
}
```

### 2.10 `PATCH /v1/runtimes/{runtime_id}/health` — set health

```http
PATCH /v1/runtimes/r1/health
Authorization: Bearer <key>
Content-Type: application/json

{
  "status": "healthy",
  "message": "manual override"
}
```

Required field: `status` (one of the 5 canonical values
plus `unknown`). Optional: `message`.

Response: `200 OK` with the updated Runtime descriptor.

### 2.11 `GET /v1/runtimes/{runtime_id}/dispatches` — recent dispatches

```http
GET /v1/runtimes/r1/dispatches?limit=20
Authorization: Bearer <key>
```

Lists the most recent dispatch records for the Runtime
(newest first). The `limit` query param defaults to 50
and is capped at 500.

Response:

```json
{
  "version": "v1",
  "runtime_id": "r1",
  "count": 2,
  "dispatches": [
    {
      "dispatch_id": "dispatch-abc123",
      "task_id": "TASK-20260710-0001",
      "run_id": "TASK-20260710-0001",
      "selected_runtime_id": "r1",
      "selected_runtime_type": "aee_lightweight",
      "selection_reason": "required capabilities satisfied: ['task.shell']; health=healthy",
      "candidate_count": 1,
      "attempt_number": 1,
      "dispatched_at": "2026-07-10T10:00:00Z",
      "dispatch_status": "selected",
      "failure_code": null,
      "failure_message": null,
      "rejected_reasons": {}
    }
  ]
}
```

---

## 3. Auth

All AEE-5 endpoints require the same bearer auth as
`/v1/jobs` and `/v1/workers`. AEE-5 does not introduce a
new auth surface; the same `BRIDGE_API_KEY` is used. See
the AEE-2 / AEE-4 documentation for details.

The 401 response is `{"detail": "missing bearer token"}` or
`{"detail": "invalid bearer token"}`. The 500 response
`{"detail": "BRIDGE_API_KEY is not configured"}` is returned
when the key is not set (this should be a startup-time
error, not a per-request one).

---

## 4. Worked example: register a new Runtime and use it

```bash
# 1. Register a new Shell Runtime.
curl -X POST -H "Authorization: Bearer $BRIDGE_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "runtime_id": "r-shell-prod-01",
       "runtime_type": "shell",
       "display_name": "Production shell runtime",
       "capabilities": ["task.shell", "task.git"],
       "labels": { "environment": "production" },
       "limits": { "max_concurrency": 8, "timeout_seconds": 900 }
     }' \
     http://127.0.0.1:8787/v1/runtimes

# 2. List all enabled shell Runtimes.
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
     "http://127.0.0.1:8787/v1/runtimes?runtime_type=shell&enabled=true"

# 3. Create a job that requires the shell Runtime.
curl -X POST -H "Authorization: Bearer $BRIDGE_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "title": "deploy",
       "input": "run deploy.sh",
       "runtime_requirements": {
         "required_capabilities": ["task.shell"],
         "required_labels": { "environment": "production" }
       }
     }' \
     http://127.0.0.1:8787/v1/jobs

# 4. Inspect the dispatch record.
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
     http://127.0.0.1:8787/v1/runtimes/r-shell-prod-01/dispatches
```

---

## 5. Versioning

The AEE-5 API is at `/v1/runtimes` (canonical). The legacy
`/runtimes` alias is kept for AEE-4 callers; the responses
are byte-identical. A future `/v2/runtimes` may diverge;
existing clients should not depend on internal field
ordering.

AEE-5 does not add `/v2/...` paths; the spec doesn't
require it.
