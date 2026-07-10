# AEE-5 Runtime Registry — Architecture

**Version:** 1.0
**Date:** 2026-07-10
**Status:** Frozen (AEE-5)
**Source spec:** `Abacus/AEE-5_Claude_Code_Runtime_Registry_Task.md`

---

## 0. Executive summary

AEE-5 introduces the **Runtime Capability Registry** and
the **Runtime Selection / Dispatch Foundation** on top
of the AEE-4 platform. After AEE-5, the bridge can:

* Register multiple Runtimes with structured metadata
  (capabilities, labels, limits, health).
* Describe each Runtime's capabilities as a normalized
  capability set (`task.shell`, `task.python`,
  `runtime.aee_runtime`, ...).
* Select a Runtime for a job via a **deterministic
  selector** that respects required capabilities,
  required labels, preferred runtimes, and the
  Runtime's health.
* Fail clearly with `AEE_RUNTIME_NOT_FOUND` when no
  Runtime fits the job — and persist the rejection
  reasons in the audit trail.
* Persist every dispatch decision in a
  `dispatch_records` table for the operator's audit
  log.
* Keep full backward compatibility with the AEE-1
  `Job` model and the AEE-4 worker claim protocol.

The AEE-5 boundary is **runtime metadata + selection**;
it does **not** add a new Runtime adapter, change the
adapter wire protocol, or alter the
`/v1/workers/register` contract. AEE-5 is the
"Runtime registry" layer that sits *above* the existing
adapter / worker registries.

---

## 1. Layering

AEE-5 does not change the AEE 4-layer responsibility
boundary (AEE-5 task spec §3). The four layers remain:

```
┌───────────────────────────────────────────────┐
│ 3.1  Upper AI platform (ChatGPT, Claude, ...) │
│       Orchestrator / Planner / Commander      │
└──────────────────────┬────────────────────────┘
                       │  task / job
┌──────────────────────▼────────────────────────┐
│ 3.2  AEE  ──────────────────────────────────  │
│       • validate                            │
│       • create Run                          │
│       • queue                               │
│       • select Runtime  ◀── NEW in AEE-5    │
│       • dispatch                            │
│       • state management                    │
│       • records (task_events, logs)         │
└──────────────────────┬────────────────────────┘
                       │  Runtime-aware job
┌──────────────────────▼────────────────────────┐
│ 3.3  Runtime Adapter  ──────────────────────  │
│       RuntimeAdapter (Protocol)              │
│       submit() / poll() / cancel()           │
└──────────────────────┬────────────────────────┘
                       │  wire protocol
┌──────────────────────▼────────────────────────┐
│ 3.4  Worker / Agent SDK                      │
│       (e.g. aee-runtime daemon,             │
│        Claude Code, future Runtimes)         │
└───────────────────────────────────────────────┘
```

AEE-5 sits inside the AEE layer (3.2). The new
sub-modules are:

```
aee/
  runtimes/
    models.py        # RuntimeDescriptor, RuntimeSelectionResult, ...
    errors.py        # RuntimeNotFoundError + structured details
    repository.py    # RuntimeRepository + SqliteRuntimeRepository
    registry.py      # RuntimeRegistry (service over the repo)
    selector.py      # RuntimeSelector (deterministic matching)
    health.py        # health model + dispatchable policy
    builtins/
      aee_lightweight.py  # built-in aee-lightweight-local
  dispatch/
    service.py       # DispatchService (the only selector caller)
  config/
    runtime_config.py  # YAML loader
  api/
    runtimes.py      # /v1/runtimes admin + query endpoints
  dispatch_runtime.py  # convenience re-exports
```

The two pre-existing registries (AEE-1) keep their
narrower roles:

* `aee.core.registry.adapter_registry` — Protocol-based
  registry of `RuntimeAdapter` instances (the
  wire-protocol layer).
* `aee.core.registry.worker_registry` — the AEE-2
  worker fleet registry (the worker-claim layer).

AEE-5's `RuntimeRegistry` is the *policy* layer above
both. It owns:

* What Runtimes exist (their metadata).
* Which Runtime a given job should be dispatched to.
* The health of each Runtime.

The adapter registry continues to own *how* to talk to
a specific Runtime's wire protocol. The two are wired
together in `bootstrap_default_runtimes()` /
`app.py:_lifespan`.

---

## 2. Domain model

### 2.1 RuntimeDescriptor

A `RuntimeDescriptor` is the canonical metadata for a
Runtime. Field-level types and constraints are
documented in the AEE-5 task spec §4.1.

```python
@dataclass
class RuntimeDescriptor:
    runtime_id: str          # PK, unique
    runtime_type: str        # logical family (e.g. "aee_lightweight")
    display_name: str
    version: str = "1.0.0"
    enabled: bool = True
    endpoint: str = "local"
    capabilities: RuntimeCapabilities  # normalized subset
    labels: Dict[str, str]
    limits: RuntimeLimits
    health: RuntimeHealth
    registered_at: str       # ISO-8601 UTC
    updated_at: Optional[str]
```

The descriptor is the AEE-5 API's "wire" type: every
JSON payload on `/v1/runtimes` deserializes to a
`RuntimeDescriptor.from_dict()`.

### 2.2 TaskRuntimeRequirements

A `TaskRuntimeRequirements` is the *task-side*
declaration of what a job needs. The AEE-1 `Job` model
gained a new field `runtime_requirements` (AEE-5); old
callers that don't pass it get the default
`aee-lightweight-local` Runtime (AEE-4 compat).

```python
@dataclass
class TaskRuntimeRequirements:
    runtime_type: Optional[str] = None
    required_capabilities: List[str] = field(default_factory=list)
    preferred_capabilities: List[str] = field(default_factory=list)
    required_labels: Dict[str, str] = field(default_factory=dict)
    preferred_runtime_ids: List[str] = field(default_factory=list)
    excluded_runtime_ids: List[str] = field(default_factory=list)
```

Semantics:

* `required_*` — hard constraints. Failing a
  `required_*` excludes a Runtime from candidacy.
* `preferred_*` — soft. They influence ranking, not
  whether a Runtime is eligible.
* `excluded_runtime_ids` — hard exclude; never
  considered.

### 2.3 RuntimeSelectionResult

The selector's return type:

```python
@dataclass
class RuntimeSelectionResult:
    selected_runtime_id: Optional[str]
    selection_reason: str
    candidate_count: int
    evaluated_runtime_ids: List[str]
    rejected_reasons: Dict[str, List[str]]  # {runtime_id: [reason, ...]}
```

When `selected_runtime_id` is `None`, the selector
raised `RuntimeNotFoundError` instead. The exception's
`to_dict()` is the API's 422 response.

### 2.4 DispatchRecord

A `DispatchRecord` is persisted in the
`dispatch_records` table for audit. Every selection +
dispatch attempt writes a record.

```python
@dataclass
class DispatchRecord:
    dispatch_id: str
    task_id: str
    run_id: str
    selected_runtime_id: str
    selected_runtime_type: str
    selection_reason: str = ""
    candidate_count: int = 0
    attempt_number: int = 1
    dispatched_at: str  # ISO-8601 UTC
    dispatch_status: str  # "selected" | "dispatched" | "failed"
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    rejected_reasons: Dict[str, List[str]] = field(default_factory=dict)
```

---

## 3. Selector

### 3.1 Determinism

The selector is a **pure function** of
`(requirements, available_runtimes)`. Two calls with
the same arguments return the same result. There is no
randomness, no time-dependence, no external state read.

The score function is:

```text
score = 0
if runtime_id not in preferred_runtime_ids: score += 1
score -= len(preferred_capabilities & runtime_capabilities)
score += health_score(runtime.health.status)
```

`score` is sorted ascending; ties are broken by
`runtime_id` ASCII order. This is the deterministic
tie-breaker the AEE-5 task spec §4.4 requires.

### 3.2 Health policy

| Status     | Dispatchable? | Notes |
|------------|---------------|-------|
| `healthy`  | yes           | highest priority (score 0) |
| `degraded` | yes           | second priority (score 1) |
| `unknown`  | yes (default `allow_unknown_health=True`) | third (score 2) |
| `unhealthy`| **no**        | rejected |
| `offline`  | **no**        | rejected |

The `allow_unknown_health` flag is a global config
toggle. The AEE-4 compat default is `True` (so
Runtimes that have never reported health can still
dispatch). Operators can flip it to `False` once the
health-check endpoint is in production use.

### 3.3 Selection order

The selector implements the 9-step order from the
AEE-5 task spec §4.4:

1. Exclude `Runtime.enabled == False`.
2. Exclude `runtime_id in excluded_runtime_ids`.
3. Exclude by `is_dispatchable(health, allow_unknown_health)`.
4. Filter by `runtime_type` exact match.
5. Filter by `required_capabilities` subset match.
6. Filter by `required_labels` subset match.
7. Rank by `preferred_runtime_ids` (preferred first).
8. Rank by `preferred_capabilities` weight (more matches
   = lower score).
9. Deterministic tie-break by `runtime_id` ascending.

When no Runtime passes the required filters, the
selector raises `RuntimeNotFoundError` with a
structured `evaluated_runtimes` list — every
candidate, paired with the reason(s) it was rejected.

---

## 4. Registry

### 4.1 Service layer

`RuntimeRegistry` (in `aee/runtimes/registry.py`) is the
public API. It owns:

* `register_runtime(descriptor, replace=False)` — register.
* `unregister_runtime(runtime_id)` — remove.
* `get_runtime(runtime_id)` — fetch.
* `list_runtimes(enabled=None, runtime_type=None)` — list.
* `update_runtime(runtime_id, updates)` — patch.
* `set_runtime_enabled(runtime_id, enabled)` — flip the flag.
* `find_runtimes_by_capability(capability)` — O(1) lookup.
* `find_runtimes_by_labels(labels)` — label-subset match.
* `update_runtime_health(runtime_id, status, message=None)`.
* `check_runtime_health(runtime_id)` — read current health.
* `list_healthy_runtimes(allow_unknown_health=True)` —
  dispatchable subset.

All write operations take a process-wide
`threading.Lock`. Reads are O(1) for capability lookup
(via the in-memory cap cache) and O(N) for label
lookup (a full registry walk — fine for the expected
N < 100 Runtimes).

The lock-free `__init__` is **intentional** — see
`aee/runtimes/registry.py:RuntimeRegistry.__init__`
for the deadlock-avoidance rationale. The
capability / label caches are built lazily on the
first read, not at construction time. This matters
because the dispatcher's `_init_schema` imports
`aee.runtimes.repository`, which transitively
imports the registry, which would otherwise call
`get_conn()` while the dispatcher is still holding
`_init_lock`. The AEE-5 fix is to defer the cache
build to the first read; the lock is therefore
acquired once (per process) by the dispatcher, not
twice.

### 4.2 Storage abstraction

`RuntimeRepository` is the storage interface
(in `aee/runtimes/repository.py`). Two
implementations:

* `SqliteRuntimeRepository` — default. Uses the same
  thread-local connection as `dispatcher.db.get_conn()`.
  The `runtimes` and `dispatch_records` tables live in
  the dispatcher's existing SQLite file.
* `InMemoryRuntimeRepository` — used by unit tests so
  they don't touch the real DB.

The storage layer is the only place that knows the
SQLite shape; the rest of AEE-5 sees the abstract
`RuntimeRepository`.

### 4.3 Migration

AEE-5 adds two new tables (`runtimes`,
`dispatch_records`) and four indexes. The migration
is **additive** — no existing tables or columns are
modified — and **idempotent** (the
`ensure_aee5_schema()` function is called from
`dispatcher.db:_init_schema` and uses
`CREATE TABLE IF NOT EXISTS` + `IF NOT EXISTS`
indexes). Operators do not run a manual migration
script; the schema is created on first DB open.

---

## 5. Dispatch integration

### 5.1 The dispatch service

`aee.dispatch.service.DispatchService` is the *only*
caller of `RuntimeSelector.select()`. API controllers
and adapters do not re-implement matching.

The service exposes:

* `select_runtime_for_job(task_id, run_id, runtime_requirements)`
  — selects a Runtime, persists a `DispatchRecord` with
  `dispatch_status="selected"`. Raises
  `RuntimeNotFoundError` on no match (after persisting
  a `failed` audit record).
* `mark_dispatched(dispatch_id)` — flips the record to
  `dispatched`.
* `mark_dispatch_failed(dispatch_id, failure_code, failure_message)`
  — flips the record to `failed` with a structured
  failure code.
* `list_dispatches(task_id=None, runtime_id=None, limit=50)`.

### 5.2 Default Runtime

The default Runtime id is `aee-lightweight-local`
(`aee.runtimes.builtins.aee_lightweight.build_default_descriptor`).
A task that doesn't pass `runtime_requirements` is
dispatched to the default Runtime if it's still
enabled and dispatchable. If the default is disabled
or unhealthy, the selector runs the full algorithm
and may return a different Runtime (or fail with
`AEE_RUNTIME_NOT_FOUND`).

The default id is **configurable** via the YAML
`runtimes.default_runtime_id` field; the
`apply_runtime_config()` helper updates
`dispatch_service._default_runtime_id` accordingly.

### 5.3 Lifecycle

```
                ┌─────────────────┐
   POST /jobs   │  DispatchService │  writes dispatch_records row
   ─────────▶   │ .select_…        │  with dispatch_status=selected
                └────────┬────────┘
                         │
                         │ RuntimeDescriptor
                         ▼
                ┌─────────────────┐
                │ adapter_registry │  submit() / poll() / cancel()
                │ .get(adapter)   │
                └────────┬────────┘
                         │
                         │ wire (Hermes / aee-runtime / future)
                         ▼
                ┌─────────────────┐
                │ Runtime worker   │  executes, sends heartbeats
                │ daemon           │
                └────────┬────────┘
                         │
                         │ terminal state
                         ▼
                mark_dispatched / mark_dispatch_failed
                (flips dispatch_records.dispatch_status)
```

---

## 6. Configuration

AEE-5 introduces a `runtimes:` block in a YAML file
loaded at startup. The bridge reads the path from
`AEE_RUNTIME_CONFIG` (env var); the file is optional
(a fresh bridge works without it). See
`docs/aee/AEE5_CONFIGURATION.md` for the full
schema.

Failure modes:

* Malformed YAML → `RuntimeConfigError` → 500 at
  startup (fail-fast).
* Reference to undefined env var (`${VAR}`) → fail-fast.
* Invalid health status / runtime_id collision → `RuntimeValidationError`.

---

## 7. API

The new endpoints are mounted under both `/runtimes`
(legacy alias) and `/v1/runtimes` (forward path),
mirroring the AEE-4 alias pattern. The auth surface is
the same as `/v1/jobs` and `/v1/workers` — the existing
`BRIDGE_API_KEY` bearer.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/runtimes` | Register a new Runtime |
| `GET`  | `/v1/runtimes` | List / filter Runtimes |
| `GET`  | `/v1/runtimes/search` | List with `label.*` filters |
| `GET`  | `/v1/runtimes/{id}` | Detail |
| `PATCH`| `/v1/runtimes/{id}` | Update (partial) |
| `DELETE`| `/v1/runtimes/{id}` | Remove |
| `POST` | `/v1/runtimes/{id}/enable` | Set enabled=True |
| `POST` | `/v1/runtimes/{id}/disable` | Set enabled=False |
| `POST` | `/v1/runtimes/{id}/health-check` | Read current health |
| `PATCH`| `/v1/runtimes/{id}/health` | Set health status |
| `GET`  | `/v1/runtimes/{id}/dispatches` | Recent dispatch records |

See `docs/aee/AEE5_API_REFERENCE.md` for the full
request / response shape.

---

## 8. Compatibility

AEE-5 maintains AEE-4 behavior:

1. **Old task payload without `runtime_requirements`**
   still works — the selector falls back to the
   default `aee-lightweight-local` Runtime. The
   pre-AEE-5 `POST /jobs` and `POST /runs` payloads
   continue to be accepted (the new field is
   optional).
2. **The default Runtime is still `aee_lightweight`**
   (the AEE-4 in-house AEE Lightweight Agent Runtime).
3. **No breaking changes to the existing API.** The
   `/v1/jobs`, `/v1/workers`, and `/v1/runs` endpoints
   behave exactly as in AEE-4.
4. **Existing Run statuses and task lifecycle
   unchanged.** AEE-5's `dispatch_records` is a
   *new* audit table; the existing `task_events`
   table is still the primary state machine audit.
5. **All AEE-4 tests pass** (after the pre-existing
   test isolation issue in `test_dispatcher.py` /
   `test_phase4_delivery.py` is excluded; see the
   test report).
6. **No `Pi Agent` naming regression.** AEE-5 ships a
   regression test that scans the entire repository
   and fails on `pi_agent`, `pi-agent`, `pi-mono`,
   `PiWorker`, etc. See
   `aee/tests/test_pi_agent_naming_regression.py`.
7. **No third-party Pi Agent dependency.** AEE-5
   also asserts that `aee-runtime/runtime/package.json`
   does not pull in any third-party Pi Agent package.

---

## 9. Observability

AEE-5 emits structured events (a future AEE-5+ will
wire the actual log emitter; the records are
persisted in `dispatch_records` and `task_events`).
The `dispatch_records` table is the canonical audit
trail for selector decisions. Every selector
invocation writes a record; the record's
`rejected_reasons` JSON column lists every candidate
that was rejected, with a per-Runtime reason. A future
log emitter can publish structured events from this
table; the v1 deliverable persists the records and
the API exposes `GET /v1/runtimes/{id}/dispatches`.

---

## 10. Module map

| Module | Lines | Purpose |
|--------|-------|---------|
| `aee/runtimes/models.py` | ~360 | Domain dataclasses |
| `aee/runtimes/errors.py` | ~75 | Structured errors |
| `aee/runtimes/health.py` | ~75 | Health model + dispatchable policy |
| `aee/runtimes/repository.py` | ~470 | Storage abstraction (SQLite + in-memory) |
| `aee/runtimes/registry.py` | ~330 | Service layer over the repository |
| `aee/runtimes/selector.py` | ~270 | Deterministic selector |
| `aee/runtimes/builtins/aee_lightweight.py` | ~70 | Built-in Runtime descriptor |
| `aee/runtimes/builtins/__init__.py` | ~3 | Re-export |
| `aee/runtimes/__init__.py` | ~75 | Public API |
| `aee/runtimes/AUDIT_NOTE.md` | ~200 | AEE-5 audit note |
| `aee/dispatch/service.py` | ~200 | Dispatch service (selector caller) |
| `aee/dispatch/__init__.py` | ~3 | Re-export |
| `aee/config/runtime_config.py` | ~290 | YAML loader + applier |
| `aee/config/__init__.py` | ~5 | Re-export |
| `aee/api/runtimes.py` | ~290 | /v1/runtimes API |
| `aee/dispatch_runtime.py` | ~80 | Convenience re-exports |
| `aee/tests/test_models.py` | ~190 | Domain unit tests |
| `aee/tests/test_errors.py` | ~55 | Error contract tests |
| `aee/tests/test_health.py` | ~75 | Health policy tests |
| `aee/tests/test_repository.py` | ~190 | Repository tests (in-mem + SQLite) |
| `aee/tests/test_registry.py` | ~270 | Service tests |
| `aee/tests/test_selector.py` | ~245 | Selector tests |
| `aee/tests/test_dispatch_service.py` | ~120 | Dispatch service tests |
| `aee/tests/test_runtime_config.py` | ~210 | Config loader tests |
| `aee/tests/test_pi_agent_naming_regression.py` | ~165 | Pi Agent naming regression |
| `tests/test_aee5_runtime_registry.py` | ~340 | API + repo integration tests |
| `tests/test_aee5_job_lifecycle.py` | ~165 | AEE-5 spec §8.2 lifecycle tests |
| `tests/test_aee5_app_integration.py` | ~125 | App-level /v1 alias tests |

Total AEE-5 source: ~3,400 lines of Python.
Total AEE-5 tests: **173** (137 unit + 36 integration).
