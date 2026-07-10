# AEE-5 Migration Guide

**Version:** 1.0
**Date:** 2026-07-10

This guide documents how to migrate an existing AEE-4
deployment to AEE-5. The migration is **fully
backward compatible** — no operator action is required
to keep the AEE-4 wiring working. The steps below
cover the new capabilities AEE-5 unlocks.

---

## 1. What's new in AEE-5

AEE-5 adds:

1. The **Runtime Registry** — a structured metadata
   store for Runtimes (capabilities, labels, limits,
   health, enabled).
2. The **Runtime Selector** — a deterministic
   component that picks the right Runtime for a job
   based on the job's `runtime_requirements`.
3. The **Dispatch Service** — the single seam between
   job creation and Runtime selection. The service
   persists a `dispatch_records` row for every
   selection.
4. The **`/v1/runtimes` admin + query API** — for
   registering, listing, and updating Runtimes at
   runtime.
5. The **`runtimes:` YAML config** — for declaring
   Runtimes declaratively.
6. The **Runtime Health model** — with the 5-status
   vocabulary (`healthy` / `degraded` / `unhealthy` /
   `offline` / `unknown`).

AEE-5 does **not**:

* Add new Runtime adapters. The built-in
  `aee-lightweight` Runtime is the only one.
* Replace the `adapter_registry` or `worker_registry`.
  They continue to own the wire-protocol and
  worker-claim layers.
* Change the AEE-1 `Job` model in any breaking way.
  The new `runtime_requirements` field is optional.
* Add new auth. The same `BRIDGE_API_KEY` is used.
* Touch the existing `tasks` or `workers` tables.
  AEE-5's schema is purely additive.

---

## 2. Operator migration steps

### 2.1 If you have a fresh deployment

Do nothing. The bridge starts with the built-in
`aee-lightweight-local` Runtime auto-registered. Jobs
without `runtime_requirements` continue to dispatch to
the built-in Runtime, exactly as in AEE-4.

### 2.2 If you want to add a second Runtime

You have two options:

**Option A: YAML config (recommended for permanent changes).**

1. Create a `runtimes.yaml` (see
   `docs/aee/AEE5_CONFIGURATION.md`).
2. Set `AEE_RUNTIME_CONFIG=/path/to/runtimes.yaml`.
3. Restart the bridge.
4. The new Runtime is auto-registered; the built-in
   remains.

**Option B: API (recommended for ad-hoc changes).**

```bash
curl -X POST -H "Authorization: Bearer $BRIDGE_API_KEY" \
     -H "Content-Type: application/json" \
     -d @new-runtime.json \
     http://127.0.0.1:8787/v1/runtimes
```

The change is immediate; no restart needed. The
Runtime is persisted in the `runtimes` table.

### 2.3 If you want to use runtime-aware job routing

Existing callers (e.g. `POST /jobs`) continue to work
without changes. To use runtime-aware routing, the
caller adds a `runtime_requirements` field to the
body:

```json
{
  "title": "deploy",
  "input": "run deploy.sh",
  "runtime_requirements": {
    "required_capabilities": ["task.shell"],
    "required_labels": { "environment": "production" }
  }
}
```

If no Runtime matches, the call fails with HTTP 422
and a structured `AEE_RUNTIME_NOT_FOUND` error body
listing the rejected Runtimes. The job is NOT
dispatched; the Run record (if any) is stamped
`failed` with the structured `failure_code`.

### 2.4 If you want to inspect dispatch history

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
     "http://127.0.0.1:8787/v1/runtimes/aee-lightweight-local/dispatches?limit=20"
```

Returns the most recent dispatch records (newest
first), including the rejected reasons and the
selection rationale.

---

## 3. Schema migration

AEE-5 adds two new tables to the dispatcher's SQLite
file (`data/dispatcher.db`):

* `runtimes` — one row per registered Runtime.
* `dispatch_records` — one row per selection /
  dispatch attempt.

The migration is **additive** and **idempotent**.
Existing tables (`tasks`, `task_events`,
`task_outputs`, `workers`) are not modified. The
schema is created on first DB open by
`dispatcher.db:_init_schema`, which calls
`aee.runtimes.repository.ensure_aee5_schema`. No
operator action is required.

If you want to verify the migration:

```bash
sqlite3 data/dispatcher.db \
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('runtimes', 'dispatch_records') ORDER BY name;"
# expected:
# dispatch_records
# runtimes
```

---

## 4. Compatibility matrix

| AEE-4 surface | AEE-5 status |
|---------------|-------------|
| `POST /runs` (legacy) | unchanged |
| `POST /jobs` (AEE-2) | unchanged; new `runtime_requirements` field is optional |
| `POST /jobs/claim` (AEE-2) | unchanged |
| `POST /workers/register` (AEE-2) | unchanged |
| `POST /workers/{id}/heartbeat` (AEE-4) | unchanged |
| `GET /v1/runtimes/*` (AEE-5) | **new** |
| `runtimes:` YAML config | **new** |
| `RuntimeSelector` (AEE-5) | **new** (pure, deterministic) |
| `DispatchService` (AEE-5) | **new** (the only selector caller) |
| `runtimes` table (AEE-5) | **new** |
| `dispatch_records` table (AEE-5) | **new** |

The `RuntimeRegistry` is a sibling of the existing
`adapter_registry` and `worker_registry`, not a
replacement.

---

## 5. Rollback

AEE-5 is purely additive; rolling back is the same
as rolling back any AEE release:

1. Stop the bridge.
2. Revert to the previous release.
3. The new tables (`runtimes`, `dispatch_records`)
   remain in the SQLite file but are not used by
   the AEE-4 code. They are safe to leave in
   place; an operator can `DROP TABLE` them once
   they confirm no audit data needs to be kept.
4. Restart the bridge.

The AEE-4 wiring is restored exactly as it was
before AEE-5.

---

## 6. Migration from a hypothetical AEE-6 / future

AEE-5 was designed to make AEE-6 (Artifact
Management) and AEE-7 (Scheduler) easier:

* AEE-6 will add an `artifacts` table + an
  `Artifact` dataclass. The Runtime Descriptor's
  `limits.max_concurrency` and `timeout_seconds`
  already provide the contract surface AEE-6 will
  need.
* AEE-7 will add a `schedules` table. The Runtime
  Registry's deterministic tie-break is the
  contract surface AEE-7 will need.

No AEE-5 work is being re-done in AEE-6 or AEE-7.
The AEE-5 architecture document
(`docs/aee/AEE5_RUNTIME_REGISTRY_ARCHITECTURE.md`)
§1 documents the layering for both follow-on phases.

---

## 7. Common questions

**Q: Do I need to update my existing workers?**

No. Existing workers that registered with
`worker_type="aee_lightweight"` continue to work
unchanged. The AEE-4 worker claim protocol is not
modified.

**Q: Do I need to change my client code?**

No. Existing `POST /jobs` and `POST /runs` payloads
without `runtime_requirements` continue to work. The
new field is purely opt-in.

**Q: How do I tell whether my deployment is using
AEE-5's selector or the AEE-4 default?**

Check the `dispatch_records` table:

```bash
sqlite3 data/dispatcher.db \
    "SELECT COUNT(*) FROM dispatch_records;"
```

If non-zero, AEE-5 is wired in. The AEE-4 path does
not write `dispatch_records`.

**Q: Can I keep the AEE-4 default Runtime and add
AEE-5 selectors on top?**

Yes — that's the default behaviour. The built-in
`aee-lightweight-local` Runtime is the AEE-4
default; AEE-5's selector runs the full 9-step
algorithm if `runtime_requirements` is non-empty, and
falls back to the default Runtime otherwise.

**Q: How do I disable AEE-5 if I want to?**

Set `AEE_RUNTIME_CONFIG=` (empty) and restart. The
built-in is still auto-registered (so the AEE-4 path
keeps working), but no AEE-5 admin endpoints are
exposed. The AEE-4 `POST /jobs` and `POST /runs`
paths continue to work; the only difference is the
`runtime_requirements` field, which would be ignored
if you set the dispatcher to skip AEE-5 (out of
scope for this migration — AEE-5 is enabled by
default and there's no off-switch in v1).

If you need a kill switch, the simplest path is to
remove the AEE-5 modules from the package directory
before restart; the AEE-4 code doesn't depend on
them. (We don't recommend this; the AEE-5 fallback
to the default Runtime is non-disruptive.)

**Q: Where's the audit trail for the AEE-5
selector's decisions?**

In the `dispatch_records` table. Every selector
invocation writes a row, even when no Runtime
matches (the row is then stamped with
`dispatch_status="failed"`,
`failure_code="AEE_RUNTIME_NOT_FOUND"`, and a
`rejected_reasons` JSON column listing every
candidate that was rejected, with the per-Runtime
reason). The `GET /v1/runtimes/{id}/dispatches`
endpoint exposes the audit trail per-Runtime.
