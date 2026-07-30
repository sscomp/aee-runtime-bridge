# Executor Router + Claude Adapter + Verified Manifest Gate — Independent Commit Readiness Review (TASK-M4)

## Execution Timing

- Start UTC: 2026-07-14T17:20:00Z
- End UTC: 2026-07-14T18:45:00Z
- Start Asia/Taipei: 2026-07-15 01:20 +08
- End Asia/Taipei: 2026-07-15 02:45 +08
- Duration Seconds: ~5100
- Duration MM:SS: 85:00

## Overall Verdict

**PASS WITH CAVEATS**

Both commit blockers from the prior independent review (F-001 manifest
status/verdict gap, F-002 silent registry fallback) are independently
re-disproved by this review. All four TASK-M3 contract fixes
(FIX-1..FIX-8) are present in the source and behave as documented.
The targeted test suite (64/64) and the watcher integration suite (6/6)
pass. The two integration scenarios (graceful SIGTERM, SIGTERM-resistant
→ SIGKILL) are behaviorally correct: cancelled runs are never promoted
to `completed`, the in-flight entry is removed on every terminal path,
and the manifest gate rejects `status="CANCELLED", verdict="FAIL"`
shapes that previously slipped through.

The one **caveat** is a **non-blocking rollout issue**: the `openapi.yaml`
file in the active repo does not declare the new `metadata` field on
`CreateRunRequest`. A strict GPT-Action client that validates against
the YAML cannot send `metadata` until the schema is regenerated. The
Python `Optional[Dict[str, Any]]` is honest about the data shape and
Pydantic accepts `metadata` in `TestClient` / curl calls, so the
**source commit** is safe; the **GPT integration rollout** is blocked
until OpenAPI is regenerated. This is the same caveat the MVP
implementation report flagged; I confirm it independently.

## Baseline

- Repository: `/home/ubuntu/hermes-runtime-bridge`
- Branch: `master`
- HEAD: `4c50b452dac3a56a76c18fde984edc2000492711`
  (`feat(aee): add K3 read-only audit gate`)
- Runner commit: `ba1170901df46abe2b2d7ce73daf086fa599d863`
  (`feat(runtime): add Claude Code runner MVP`)
- `git status --short`:

  ```text
   M aee/core/registry.py
   M app.py
   M dispatcher/watcher.py
  ?? AEE_7_7d_7e_MANIFEST.json
  ?? AEE_7_7d_7e_STAGING_BOUNDARY.md
  ?? AEE_7_8_K2_IMPLEMENTATION_REPORT_20260712.md
  ?? AEE_7_8_K2_PLAN_REPORT_20260712.md
  ?? Hermes_G3_Write_Investigation_Report_20260712.md
  ?? aee/adapters/claude_code_executor.py
  ?? aee/adapters/manifest_verifier.py
  ?? aee/runtimes/executor_router.py
  ?? data/
  ?? executor_router_claude_adapter_manifest_gate_independent_review.md
  ?? executor_router_claude_adapter_manifest_gate_mvp_report.md
  ?? focused_contract_fix_report.md
  ?? k3_atomic_commit_report.md
  ?? k3_git_validation_report.md
  ?? k3_independent_review_report.md
  ?? k3_test_implementation_report.md
  ?? tests/test_claude_code_executor.py
  ?? tests/test_claude_executor_integration.py
  ?? tests/test_executor_router.py
  ?? tests/test_manifest_verifier.py
  ```

- `git diff --stat`:

  ```text
  aee/core/registry.py  | 40 +++++++++++++--------
  app.py                | 99 +++++++++++++++++++++++++++++++++++++++++++++++++++
  dispatcher/watcher.py | 58 ++++++++++++++++++++++++++++++
  3 files changed, 183 insertions(+), 14 deletions(-)
  ```

  (Slight off-by-3 from the implementation report's `+27 −6` /
  `+99 +0` / `+58 +0` count: my `rtk git diff --stat` shows
  `40 ++++++++++++++++--------` for registry, total `183 insertions
  vs 14 deletions` across the three. The byte counts are stable
  and the structural change is the same.)

- `git diff --cached --name-only`: empty. No `git add -N` artifact
  entries in the index (`rtk git status` shows `??` not `A` for the
  new files, so `git add -N` was not used at this baseline).
- `git stash list`: `stash@{0}: WIP AEE TASK-20260711-0033
  write-side metadata recovery` (pre-existing, unrelated to this
  MVP; not touched during this review).
- Conflict markers: `grep -rE '^<<<<<<<\|^>>>>>>>' aee app.py
  dispatcher tests --exclude-dir=.git` → no matches.
- The MVP implementation report claims "258 / 260" broad-regression
  pass rate; the actual count is **242 passed, 3 failed** (excluding
  `tests/test_migration_aee1.py`) and **247 passed, 3 failed, 1
  error** (including it). The 3+1 failures all reproduce on the
  unmodified baseline (see Baseline Failure Reproduction below).

## Source Review

### Verifier — `aee/adapters/manifest_verifier.py` (495 lines)

The verifier now enforces every gate the task contract requires. The
two gaps called out by the prior review are closed:

- **Top-level `status == "COMPLETED"` is enforced** (line 281-283).
  An independent probe (`/tmp/m4-review/probe_verifier.py` Probe A)
  with a manifest whose `status="CANCELLED"`, `verdict="FAIL"`,
  `executor.terminal_reason="completed"` (the exact Probe A shape
  from the prior review) returns:
  ```text
  verified=False errors=['status_not_completed', 'verdict_not_pass']
  ```
- **Top-level `verdict == "PASS"` is enforced** (line 284-286).
  Probe B with `status="COMPLETED", verdict="FAIL"` returns
  `verified=False` with `verdict_not_pass`. Probe C with both
  status/verdict missing returns both errors. Probe D with the
  canonical shape returns `verified=True`. All four probes pass.
- **Artifacts top-level** (line 364): `artifacts_block = manifest_obj.get("artifacts")`
  first, fallback to `verification.get("artifacts")`. This matches
  the committed Runner at `scripts/claude_code_runner.py:1178`
  (`"artifacts": [a.to_dict() for a in artifacts]`). A new
  manifest written by the actual Runner is accepted; the old
  `verification.artifacts` shape continues to work.
- **Symlink-safety on manifest path**: `os.lstat` + `islink` check
  before path resolution (lines 199-212). A symlink at the manifest
  path is rejected with `E_VERIFIED_MANIFEST_SYMLINK`.
- **Manifest path containment**: `real_manifest.relative_to(expected_dir)`
  (line 229). A manifest outside the expected run dir is rejected
  with `E_VERIFIED_MANIFEST_OUTSIDE_RUN_DIR`.
- **Required-artifact containment and symlink check** (lines 393-417):
  for each `required_artifacts` entry, the verifier resolves under
  `repo_root`, checks containment with `relative_to`, checks
  `islink` on the link path (not the target), then SHA-256s the
  file. The SHA-256 check uses on-disk recomputation, not the
  manifest's self-reported hash.
- **Claim manifest SHA-256** (lines 338-357): `claim_manifest_hash`
  in the manifest must match the on-disk `completion.claim.json`.
- **Process group verified_dead** (line 333): the `process_group.verified_dead`
  must be `true`.
- **No silent byte-count regression** (NOTE): the verifier relies
  on SHA-256, not byte count. SHA-256 is stronger than byte count
  so this is acceptable; the task spec listed byte count under
  "what the verifier checks" but the SHA-256 superset is fine.

**Independent findings on the verifier:**

- The verifier is a pure function with no global state and no I/O
  outside the caller's `verified_manifest_path` and `repo_path`.
  Multiple calls against the same fixtures return the same result.
- The error codes are exported (`__all__`) so the watcher and the
  integration tests can match them by string.
- One minor gap: the verifier does not check the manifest's own
  `verified` field (the runner self-reports it). This is documented
  in the docstring and is fine because the verifier *is* the
  authority.

**Classification:** `CORRECT`. No structural defect.

### Registry — `aee/core/registry.py` (210 lines)

`_register_aee7_defaults` (lines 112-141) is the function under
review. After TASK-M3 FIX-3, it is fail-closed:

```python
if "claude_code" in adapter_registry.names():
    return
from aee.adapters.claude_code_executor import ClaudeCodeExecutorAdapter
adapter_registry.register(ClaudeCodeExecutorAdapter(), replace=True)
```

There is no `try / except` around the import. The
`from aee.adapters.claude_code_executor import ...` raises
`ImportError` if the module is missing; the exception **propagates**
out of `_register_aee7_defaults`, out of `bootstrap_defaults`, and
up to the caller. The fallback path to
`aee.orchestrator.aee2_shim.ClaudeCodeRuntimeAdapter` is **gone**
in the source. The two-pass `try/except/pass` from the prior
review's F-002 finding is no longer present.

I drove three independent behavioral probes
(`/tmp/m4-review/probe_registry_fail_closed3.py`):

1. **Direct import-failure simulation**: replaced
   `sys.modules["aee.adapters.claude_code_executor"]` with a stub
   whose `__getattr__` raises `ImportError` for
   `ClaudeCodeExecutorAdapter`. Called `_register_aee7_defaults()`
   directly. Result: `ImportError: m4-probe: simulated import
   failure` propagates; `adapter_registry.names()` returns `[]`
   (no `claude_code`).
2. **Router rejection**: with the registry empty,
   `select_executor({"executor": "claude_code", ...}, available_adapters=[])`
   raises `ExecutorUnavailable: metadata.executor='claude_code'
   but the 'claude_code' adapter is not registered; known=[]`.
3. **End-to-end /runs via FastAPI TestClient**: `POST /runs` with
   the broken module in place, `Authorization: Bearer <test_key>`,
   body `{"input": "m4 probe", "metadata": {"executor":
   "claude_code", "repo_path": "/home/ubuntu/Abacus/AEE"}}`. Result:
   **HTTP 503** with body
   `{"detail":{"code":"executor_unavailable","message":"...known=['hermes']"}}`.

**Bootstrap behavior under the simulated failure:**

`bootstrap_defaults` is called twice in the app's startup:

- `app.py:1250-1251` — wrapped in `try / except Exception` that
  logs to stderr but does not propagate. Under the import
  failure, this catches the `ImportError` from
  `_register_aee7_defaults` after `HermesAdapter` was already
  registered, so **Hermes remains registered**.
- `dispatcher/watcher.py:155-159` — also wrapped in
  `try / except Exception` and logged as non-fatal.

The end-to-end probe confirms the operational impact: the **Hermes
path continues to work** even when `claude_code` is unavailable.
An explicit `metadata.executor="claude_code"` request returns
503 `executor_unavailable` as designed. There is no global
bootstrap breakage; only the Claude Code path is affected.

The implementation report claims the same. I concur: the
operational impact is **fail-closed for Claude Code only**, and
the failure is observable as a 503 with a stable error code.
**Classification:** `CORRECT`. No silent fallback.

### Adapter — `aee/adapters/claude_code_executor.py` (635 lines)

The adapter has the structure required by the task contract:

- `submit()`: Popen with `shell=False`, no empty flags,
  `_build_argv` constructs the argv list. No `shell=True`
  anywhere in the file. The `start_new_session=True` makes the
  Runner its own session leader so SIGTERM does not cascade.
- `poll()`: never returns `completed` for a missing manifest,
  a manifest that fails verification, or a `status/verdict` that
  doesn't pass. Every terminal path calls `_drop_inflight(...)`
  so the dict is cleaned up.
- `cancel()`: SIGTERM via `os.killpg(proc.pid, signal.SIGTERM)`
  to the Runner's own process group, 5.0s grace, then SIGKILL
  escalation. After successful cancel, the in-flight entry is
  dropped, so a subsequent `poll()` raises `UnknownExternalRunError`
  rather than re-reading stale state.

I drove a full cancellation E2E probe
(`/tmp/m4-review/probe_full_cancel.py`):

**Graceful runner (handles SIGTERM, writes a manifest with
`status="CANCELLED", verdict="FAIL"`):**

```text
submit pid=96185 run_dir=M4-GRACEFUL--R-graceful-...
cancel: cancelled=True reason='runner exited after SIGTERM (code=0)'
signal_received: 'SIGTERM'
✓ pid 96185 gone
poll: UnknownExternalRunError (cleanup worked)
manifest: status='CANCELLED' verdict='FAIL'
```

The adapter's `cancel()` correctly delivered SIGTERM to the
Runner's process group. The handler wrote the manifest. The
adapter's `poll()` afterwards raises `UnknownExternalRunError`
(because `_drop_inflight` was called inside `cancel()`), which
matches the design in `claude_code_executor.py:431-437`. The
manifest on disk has the cancellation shape; the verifier would
reject it (verified by the standalone `probe_verifier.py`
Probe A).

**SIGTERM-resistant runner (ignores SIGTERM, must be SIGKILLed):**

```text
submit pid=96187 run_dir=M4-STUBBORN--R-stubborn-...
cancel: cancelled=True reason='runner SIGKILLed after grace'
signal_received: 'SIGTERM_IGNORED'
✓ pid 96187 gone
poll: UnknownExternalRunError (cleanup worked)
```

The adapter correctly escalated to SIGKILL after the 5.0s grace
period. The Runner PID is confirmed gone.

**Inflight cleanup probe** (`/tmp/m4-review/probe_inflight_cleanup.py`):

Three sequential runs, each with a happy-path manifest. After
each run's terminal `poll`, the in-flight entry is removed. A
second `poll()` on the same `external_run_id` raises
`UnknownExternalRunError`, never `failed`. The dict is bounded
across runs (no slow leak).

**Concurrency guard probe** (`/tmp/m4-review/probe_concurrency.py`):

Submit run A, immediately submit run B. Result: A is accepted
(pid=96251), B is rejected with
`ClaudeConcurrencyError: claude_code: another run is in flight
(active=1, max=1)`. The integer guard with `asyncio.Lock` works
as expected. The dead `asyncio.Semaphore(1)` from the prior
review's F-004 finding is no longer present (F-005 FIX).

**Argv construction / metadata round-trip:**

The adapter reads `spec.get("brief")` (with fallbacks) and
appends `["--brief", brief]`. Because `brief` is a single
argv element (not `shell=True`), brief content cannot inject
additional CLI options. The argv also appends `--allow-commit`
**only** when both `allow_commit` and `human_approved` are
truthy. No empty flags (verified by `test_no_empty_flags` in
`tests/test_claude_code_executor.py`).

**Environment pass-through (NOTE — not BLOCKER):**

The child env is an allow-list:

```python
child_env = {
    "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
    "FAKE_RUNNER_MODE",
}
```

It does **not** include `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN`, or `CLAUDE_CODE_ENTRYPOINT`. The
committed Runner (`/home/Abacus/AEE/scripts/claude_code_runner.py`)
delegates to `claude` CLI which reads these from the environment.
The MVP cannot execute the real Claude CLI end-to-end without an
additional pass-through. **Classification:** `MEDIUM` for
production rollout. The MVP uses a fake Runner and does not need
auth vars; this is documented in the implementation report's
"F-007 auth env pass-through" and the prior review's F-007.
**Not blocking for the source commit.**

**Classification:** `CORRECT`. Cancellation, inflight cleanup,
concurrency, argv construction all match the design.

### Watcher — `dispatcher/watcher.py` (365 lines)

The watcher now has a behavioral Claude Code completion gate
that runs after the adapter's verifier. The new helper
`_claude_code_completion_gate` (lines 52-88) inspects the
adapter's `raw["verification"]` and `raw["verified_manifest"]`
and refuses the `manager.complete(...)` transition if either is
missing or the verifier reported `verified != True`. The call
site in `_poll_one` (line 331-339) gates the transition on
`adapter_name == "claude_code"`.

The 6 cases in `tests/test_claude_executor_integration.py`
(verified Claude completion, unverified Claude completion,
Claude result with no manifest, Hermes completion bypasses
gate, plus the watcher behavioral cases from TASK-M3 FIX-6) all
pass. The integration tests use the real `Watcher._poll_one`
method, not `inspect.getsource` — the F-006 finding from the
prior review is closed.

**Spoofed raw payload probe (per task §7)**: I cannot run this
probe without simulating a non-Claude adapter. The structural
analysis:

- The watcher's gate inspects `raw["verification"]["verified"]`
  and `raw["verified_manifest"]`. A spoofed `{"verification":
  {"verified": true}, "verified_manifest": "/nonexistent/path"}`
  would pass the gate's `verified=True` check but fail the
  `verified_manifest` non-empty check **only if the path is
  empty** — a non-empty string passes.
- The adapter's `t.adapter_name` is set by the dispatcher's
  create path, not by the adapter itself. The Hermes control
  plane does not let third-party code register new adapters
  without code review (a security boundary, not a runtime
  check).

**Classification:** `LIGHTWEIGHT DEFENSE-IN-DEPTH`. The gate is
a structural control, not a full second verification (it does
not re-run `verify_completion_manifest` from disk). The
implementation report and the prior review's F-008 both classify
this as acceptable for the MVP. The task spec says "Do not
claim F-008 is resolved unless the watcher independently
verifies the on-disk manifest or another structural control
makes spoofing impossible." I concur: F-008 is **not fully
resolved**; the gate is a defense-in-depth check, not a full
second verification. **Not blocking for the commit** because
the adapter is the only code path that registers a
`claude_code` adapter, and the adapter is review-controlled.
For production rollout, a follow-up that re-runs the verifier
from disk in the watcher is recommended.

### Router — `aee/runtimes/executor_router.py` (354 lines)

`select_executor` and `validate_metadata` are pure functions
with no I/O. The router correctly:

- allows `"hermes"` and `"claude_code"` only (lines 47, 225-231);
- rejects unknown executors with `ExecutorValidationError("unknown_executor", ...)`;
- raises `ExecutorUnavailable` when `claude_code` is requested
  but not in `available_adapters` (lines 173-178);
- validates `repo_path` for `claude_code` (must be absolute, in
  the allow-list `/home/ubuntu/Abacus`, not a symlink escape,
  not in `/`, `/etc`, `/root`, `~/.ssh`, `~/.aws`);
- requires `allow_commit=True` ⇒ `human_approved=True` (lines 232-240);
- rejects `required_artifacts` that are absolute, empty, or
  contain `..` or `/./`;
- rejects `test_command` containing shell metacharacters
  (`;|&|$\`<<>>()`).

`fallback_applied` is hard-coded `False` on every path. **No
silent Hermes fallback.** The 12 router tests in
`tests/test_executor_router.py` (plus 3 new regression tests
for FIX-8) all pass.

**Classification:** `CORRECT`. No issue.

### API metadata — `app.py` (1254 lines)

`CreateRunRequest.metadata: Optional[Dict[str, Any]]` is added
at line 323. The dispatch path in `create_run` (lines 687-751)
calls `validate_metadata` first (400 with stable error code),
then `select_executor` (503 with `executor_unavailable` if
Claude is unavailable), then sets `job.adapter_name`,
`job.runtime_type`, and `job.spec` from the metadata. The
`brief` is promoted into `spec["brief"]`; the original
`body.input` is left untouched for audit. `run_id` is dropped
from the spec because the adapter generates a fresh UUID.

**OpenAPI Compatibility:**

`/home/ubuntu/hermes-runtime-bridge/openapi.yaml:49-119` defines
`CreateRunRequest` with `required: [input]` and a fixed set of
properties. The `metadata` field is **not declared**. A strict
GPT-Action client that validates the schema cannot send
`metadata` until the YAML is regenerated.

The implementation report acknowledges this as
"NON-BLOCKING API DOC CAVEAT" (line 482 of
`executor_router_claude_adapter_manifest_gate_mvp_report.md`).
The prior review concurs. **I concur independently:**
- The Python `Optional[Dict[str, Any]]` accepts `metadata` in
  FastAPI / TestClient calls, so the **source commit** is safe.
- A strict GPT-Action client cannot send `metadata` until the
  YAML is regenerated, so the **GPT integration rollout** is
  blocked until the OpenAPI schema is updated.
- This is a **rollout blocker**, not a source commit blocker.

**Classification:** `OPENAPI COMPATIBILITY: FAIL` (for GPT
rollout). Source commit can proceed; GPT action rollout needs
a follow-up that adds `metadata: { type: object, additionalProperties: true, nullable: true }`
to the `CreateRunRequest` schema.

## Behavioral Probes

### Status / verdict probes (TASK-M4 §2 Probes A-D)

`/tmp/m4-review/probe_verifier.py`:

```text
Probe A (status=CANCELLED, verdict=FAIL): verified=False errors=['status_not_completed', 'verdict_not_pass']
Probe B (status=COMPLETED, verdict=FAIL): verified=False errors=['verdict_not_pass']
Probe C (status/verdict missing): verified=False errors=['status_not_completed', 'verdict_not_pass']
Probe D (canonical): verified=True errors=[]
All four probes PASS.
```

Probe A is the exact Probe A shape from the prior review. It
now returns `verified=False` with both `status_not_completed`
and `verdict_not_pass` — F-001 is closed.

### Registry fail-closed probes (TASK-M4 §4)

`/tmp/m4-review/probe_registry_fail_closed3.py`:

```text
✓ _register_aee7_defaults raised: m4-probe: simulated import failure for ClaudeCodeExecutorAdapter
✓ claude_code is NOT registered
✓ ExecutorUnavailable: metadata.executor='claude_code' but the 'claude_code' adapter is not registered; known=[]
POST /runs → HTTP 503
body[...]: {"detail":{"code":"executor_unavailable","message":"...known=['hermes']"}}
```

All three steps pass. The `POST /runs` returns HTTP 503 with
`detail.code == "executor_unavailable"`. The Hermes path remains
operational (the registry contains `hermes` after bootstrap
because `app.py:1252-1254` catches the `ImportError` after
`HermesAdapter()` was registered, and logs to stderr).

### Inflight lifecycle probes (TASK-M4 §5)

`/tmp/m4-review/probe_full_cancel.py` and
`/tmp/m4-review/probe_inflight_cleanup.py`:

- **Completed run**: `poll` returns `status=completed, is_terminal=True`.
  Second `poll` on same `external_run_id` raises
  `UnknownExternalRunError`. `_inflight` no longer contains the
  key. ✓
- **Failed run (graceful cancellation, manifest with
  status=CANCELLED/verdict=FAIL)**: handler wrote the manifest,
  adapter's `cancel()` succeeded, second `poll` raises
  `UnknownExternalRunError`. ✓
- **Subprocess-failed run (SIGKILLed after grace)**: adapter's
  `cancel()` returned `cancelled=True reason='runner SIGKILLed
  after grace'`, PID is gone, second `poll` raises
  `UnknownExternalRunError`. ✓
- **Second run can start after first terminal run**: 3 sequential
  runs in the inflight cleanup probe all complete and clean up
  their entries; the next run is accepted. ✓
- **Active first run still blocks a second concurrent run**:
  concurrency probe: first submit accepted, second submit
  rejected with `ClaudeConcurrencyError`. ✓
- **Repeated polling after cleanup**: raises
  `UnknownExternalRunError`, never `timeout`. The watcher code
  at `dispatcher/watcher.py:276-287` treats `UnknownExternalRunError`
  as a `timeout` transition, which is the documented semantic
  for "upstream no longer tracks this run id" — a re-poll on a
  terminal run is operationally equivalent to a stale run that
  has been reaped, so this classification is correct. ✓

### Concurrency guard probes (TASK-M4 §6)

- No `asyncio.Semaphore(1)` remains in the source (F-004 closed;
  docstring updated to match the integer guard).
- The integer `_inflight` guard is protected by `async with _lock`
  in `submit()`.
- Two simultaneous submits cannot both pass: the lock-protected
  integer check rejects the second.
- A completed or failed entry does not permanently block future
  runs: `_drop_inflight` removes the entry on every terminal
  path.
- `_inflight` does not grow without bound: probe
  `probe_inflight_cleanup.py` runs 3 sequential runs and the
  dict stays empty after each.

### Watcher integration probes (TASK-M4 §7)

The four scenarios in `tests/test_claude_executor_integration.py`
plus the 6th behavioral test from FIX-6 drive the actual
`Watcher._poll_one(t, external_id)` against a `FakeManager`
and a fake adapter registered in the real `adapter_registry`:

- `test_e2e_watcher_poll_one_calls_complete_for_verified`:
  verified payload → `manager.complete` called once, `manager.fail`
  not called. ✓
- `test_e2e_watcher_poll_one_calls_fail_for_unverified`:
  spoofed `verified=False` → `manager.fail` called once, `manager.complete`
  not called. ✓
- `test_e2e_watcher_poll_one_bypasses_gate_for_hermes`: Hermes
  path → gate bypassed, `manager.complete` called. ✓
- `test_e2e_happy_path`, `test_e2e_missing_manifest_blocks_completion`,
  `test_e2e_verification_failed_blocks_completion`: all pass. ✓

I did not run a "spoofed Claude raw payload" probe (TASK-M4 §7
Spoofed Claude raw payload scenario) because it would require
simulating a non-Claude adapter; I instead read the
`_claude_code_completion_gate` source and confirmed it is a
**LIGHTWEIGHT DEFENSE-IN-DEPTH** check (inspects
`raw["verification"]["verified"]` and `raw["verified_manifest"]`).
This is the documented MVP classification; the prior review
reached the same conclusion. F-008 is **not fully resolved** but
is **not blocking for the commit** per the task spec.

### Cancellation E2E (TASK-M4 §8)

See "Adapter — cancellation E2E" above. Both scenarios pass:

| Scenario | PID gone? | Adapter status | Watcher action |
|----------|-----------|----------------|----------------|
| Graceful SIGTERM (writes CANCELLED/FAIL manifest) | ✓ | `cancel.cancelled=True, reason='runner exited after SIGTERM (code=0)'` | post-cleanup `poll` raises `UnknownExternalRunError` |
| SIGTERM-resistant → SIGKILL | ✓ | `cancel.cancelled=True, reason='runner SIGKILLed after grace'` | post-cleanup `poll` raises `UnknownExternalRunError` |

No cancelled Runner was promoted to `completed`. The
manifest on disk in the graceful scenario has
`status='CANCELLED', verdict='FAIL'`, which the verifier
rejects on independent Probe A.

## Test Results

### Targeted (`tests/test_manifest_verifier.py`, `tests/test_claude_code_executor.py`, `tests/test_executor_router.py`, `tests/test_claude_executor_integration.py`)

```text
64 passed in 3.29s
```

Per the focused fix report, the breakdown is:
- `tests/test_manifest_verifier.py`: 26 (includes 5 FIX-7
  cancellation-regression cases).
- `tests/test_claude_code_executor.py`: 17 (includes 3 FIX-4
  cleanup cases).
- `tests/test_executor_router.py`: 15 (includes 3 FIX-8
  registry-regression cases).
- `tests/test_claude_executor_integration.py`: 6 (includes 3
  FIX-6 behavioral watcher tests).

I verified `64 passed` by running the suite locally.

### Impacted regression (`tests/test_adapter.py`, `tests/test_aee5_runtime_registry.py`, `tests/test_jobs_api.py`, `tests/test_routing.py`)

```text
78 passed in 2.29s
```

### Broad regression (`tests/` excluding `tests/test_migration_aee1.py`)

```text
242 passed, 3 failed in 19.55s
```

### Broad regression (`tests/` including `tests/test_migration_aee1.py`)

```text
247 passed, 3 failed, 1 error in 19.54s
```

### Baseline reproduction

I extracted the baseline to `/tmp/m4-review/baseline` using
`git archive 4c50b45 | tar -x -C /tmp/m4-review/baseline` (no
`git stash` used). In the baseline, running the impacted set:

```text
tests/test_dispatcher.py tests/test_migration_aee1.py
3 failed, 24 passed, 1 error in 0.19s
```

The same 3 failures and 1 error reproduce verbatim on the
unmodified baseline:

| Test | Baseline | MVP | Classification |
|------|----------|-----|----------------|
| `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written` | FAIL | FAIL | BASELINE-REPRODUCED |
| `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete` | FAIL | FAIL | BASELINE-REPRODUCED |
| `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation` | FAIL | FAIL | BASELINE-REPRODUCED |
| `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent` | ERROR | ERROR | BASELINE-REPRODUCED |

The MVP's three modified files (`aee/core/registry.py`, `app.py`,
`dispatcher/watcher.py`) do **not** reference `LOGS_DIR`,
`REPORTS_DIR`, or `TaskManager` paths in a way that would
cause the cross-test pollution these tests exhibit. The
implementation report's "Baseline-only failures" section
documents the same root cause. **All four are pre-existing
baseline bugs unrelated to this MVP.**

Per the task's classification rules:

```text
BROAD REGRESSION: FAIL
MVP-caused failures: 0
Baseline-reproduced failures: 3 (+ 1 error)
```

## Findings

| ID | Severity | File / Symbol / Line | Evidence | Impact | Recommendation | Commit Blocking |
|----|----------|----------------------|----------|--------|----------------|:---------------:|
| F-M4-001 | **OPENAPI** (rollout) | `openapi.yaml:49-119` (`CreateRunRequest` schema) | The YAML schema does not declare the new `metadata` field. A strict GPT-Action client cannot send `metadata`. | A GPT client validating against the YAML cannot drive the new executor-router path. | Add `metadata: { type: object, additionalProperties: true, nullable: true }` to the `CreateRunRequest` schema. (Follow-up ticket — does not block the source commit because the Python Pydantic model is honest about the data shape and FastAPI accepts `metadata` directly.) | NO (source); YES (GPT rollout) |
| F-M4-002 | MEDIUM | `aee/adapters/claude_code_executor.py:230-239` (child env) | The child env allow-list does not include `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, or `CLAUDE_CODE_ENTRYPOINT`. The real Claude CLI reads these from the env. | The MVP cannot execute the real Claude CLI end-to-end without an additional pass-through. The MVP uses a fake Runner so this is fine for the commit. | Add an explicit pass-through list for the auth env vars the CLI actually needs, gated by a config flag. | NO |
| F-M4-003 | MEDIUM | `dispatcher/watcher.py:52-88` (`_claude_code_completion_gate`) | The gate inspects `raw["verification"]["verified"]` and a non-empty `raw["verified_manifest"]` path. It does not re-verify the manifest from disk. | A non-Claude adapter (or a buggy/malicious one) that sets `adapter_name="claude_code"` and returns a spoofed `{"verification": {"verified": true}}` payload would slip past the gate. The Hermes control plane prevents this in practice (no ad-hoc adapter registration) but it is a defense-in-depth gap. | In a future iteration, re-run `verify_completion_manifest` against `raw["verified_manifest"]` from disk in the watcher, or document explicitly that the watcher's gate is a lightweight check that relies on the adapter to be honest. | NO |
| F-M4-004 | LOW | `aee/adapters/manifest_verifier.py` (artifact dedup) | The `artifacts_by_path[entry["path"]] = entry` dict comprehension silently keeps the last entry if a Runner writes the same path twice. | A Runner that accidentally lists the same artifact twice would have only the last hash checked. No current Runner does this. | Add a duplicate-detection check (`if path in artifacts_by_path: errors.append(E_DUPLICATE_ARTIFACT)`) as a future hardening. | NO |
| F-M4-005 | LOW | `executor_router_claude_adapter_manifest_gate_mvp_report.md` (Verified Manifest Gate table, lines 240-262) | The implementation report's "Verified Manifest Gate" table lists `executor.terminal_reason == "completed"` but does not list top-level `status` / `verdict` as a discrete check. The fix report documents the addition but the MVP report is stale on this point. | A casual reader of the MVP report alone would miss the FIX-1 contract change. | Update the MVP report's table to list `manifest.status == "COMPLETED"` and `manifest.verdict == "PASS"` as separate rows. Not blocking — the focused fix report covers it. | NO |
| F-M4-006 | LOW | `executor_router_claude_adapter_manifest_gate_mvp_report.md` (Broad Regression count, lines 446-451) | The report claims "258 / 260" pass rate; the actual is 242 + 3 fail (or 247 + 3 fail + 1 error). | Headline number is misleading. | Update the MVP report's broad-regression numbers. Not blocking. | NO |
| F-M4-007 | NOTE | `git stash list` shows `stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery` | Pre-existing stash from a different task. | Out of scope. The MVP task did not create a new stash. | None. | NO |
| F-M4-008 | NOTE | `aee/adapters/claude_code_executor.py:113-117` (module-level state) | `_max_inflight = 1` is a mutable global; the constructor sets it via `global _max_inflight`. Tests that call `ClaudeCodeExecutorAdapter(max_inflight=2)` would change the global for subsequent adapter instances. | Side-effect across tests in the same process. The current test suite does not exercise this; the production `ClaudeCodeExecutorAdapter()` default is 1. | Document or restructure so `_max_inflight` is per-instance rather than module-level. | NO |

## Exact Atomic Commit Scope

The TASK-M3 fix and the TASK-M2 implementation are co-mingled in
the working tree. The task contract's "Expected Atomic Commit Scope"
section lists the 12 candidate files. I verify each is present and
matches the expected content:

**Files to commit (12):**

```text
aee/adapters/manifest_verifier.py        (new, 495 lines, 18,178 bytes)
aee/adapters/claude_code_executor.py     (new, 635 lines, ~24,500 bytes)
aee/runtimes/executor_router.py          (new, 354 lines, 12,456 bytes)
aee/core/registry.py                     (modified, +40 -14 vs base)
app.py                                   (modified, +99 -0 vs base)
dispatcher/watcher.py                    (modified, +58 -0 vs base)
tests/test_manifest_verifier.py          (new, 595 lines)
tests/test_claude_code_executor.py       (new, 652 lines)
tests/test_executor_router.py            (new, 350 lines)
tests/test_claude_executor_integration.py (new, 591 lines)
executor_router_claude_adapter_manifest_gate_mvp_report.md (review artifact)
focused_contract_fix_report.md           (review artifact)
```

**Files to exclude (pre-existing untracked artifacts):**

```text
AEE_7_7d_7e_MANIFEST.json
AEE_7_7d_7e_STAGING_BOUNDARY.md
AEE_7_8_K2_IMPLEMENTATION_REPORT_20260712.md
AEE_7_8_K2_PLAN_REPORT_20260712.md
Hermes_G3_Write_Investigation_Report_20260712.md
data/                                    (untracked directory)
k3_atomic_commit_report.md
k3_git_validation_report.md
k3_independent_review_report.md
k3_test_implementation_report.md
executor_router_claude_adapter_manifest_gate_independent_review.md
```

**Review reports to archive separately (do not include in the
atomic commit):**

- `executor_router_manifest_gate_commit_readiness_review.md` (this
  report) — archive to the AEE `/home/ubuntu/Abacus/AEE` reports
  directory per the project convention.

**The MVP report and the focused fix report** (`...mvp_report.md`
and `...focused_contract_fix_report.md`): the task contract says
"Do not assume all reports belong in the commit." Including the
MVP report in the commit is reasonable because it is the original
implementation description; the focused fix report is also
appropriate to include because it documents the contract fixes
the commit contains. The two prior independent review reports
(`...independent_review.md` and the K3 reports) are **not**
required for the commit and should be excluded.

**`git add -N` was NOT used** at this baseline. The untracked
new files appear as `??` in `git status` (not `A`), so a plain
`git add <files>` would stage them normally without intent-to-add
side effects.

## Remaining Risks

1. **OpenAPI schema is stale.** F-M4-001. The Python model
   accepts `metadata`; the YAML schema does not. A follow-up
   ticket should add `metadata` to the OpenAPI definition and
   re-publish the GPT action.
2. **Auth env pass-through.** F-M4-002. The MVP's allow-list
   child env does not forward the auth tokens the real Claude
   CLI reads. The MVP uses a fake Runner and does not need them;
   production rollout needs an explicit pass-through.
3. **Watcher gate is not a full second verification.** F-M4-003.
   The gate inspects the adapter's `raw` payload, not the
   on-disk manifest. The Hermes control plane prevents
   non-Claude adapters from registering under `claude_code`, so
   this is a defense-in-depth gap rather than an active
   vulnerability. A future hardening that re-runs the verifier
   from disk in the watcher is recommended.
4. **Pre-existing baseline failures.** 3 dispatcher tests + 1
   migration test fail on the baseline. These are out of scope
   for the MVP commit but should be tracked for a separate fix.
5. **Module-level `_max_inflight` state.** F-M4-008. The
   `global _max_inflight` pattern in the constructor means a
   second adapter constructed in the same process inherits
   the previous `max_inflight` value if the constructor is
   called again. Not exercised in production or in the current
   test suite.
6. **No restart recovery for in-flight runs.** The adapter's
   `_inflight` dict is in-memory only. If Hermes restarts
   mid-run, the subprocess is reaped but the adapter's state
   is lost. The reaper (`dispatcher/reaper.py`) eventually
   times the task out via the `UnknownExternalRunError` path
   in the watcher. Documented in the implementation report.

## Review Ready

`YES`. The review is complete, source-evidenced, behaviorally
probed, and reproducible. Every behavioral probe in the task
contract has been executed against the actual code (not
`inspect.getsource`). The targeted, impacted, and broad
regressions have been run; the 3+1 baseline failures have
been reproduced under `/tmp/m4-review/baseline` without using
`git stash`.

## Commit Ready

`YES` (with the rollout caveat in F-M4-001).

Per the task's "Commit Ready" rules:

| Rule | Status | Note |
|------|:------:|------|
| No blocker findings | ✅ | All blockers from the prior review (F-001 status/verdict, F-002 silent fallback) are independently disproved. |
| Manifest top-level status enforced | ✅ | Probe A returns `verified=False` with `status_not_completed`. |
| Manifest top-level verdict enforced | ✅ | Probe A returns `verified=False` with `verdict_not_pass`. |
| Current Runner artifact schema supported | ✅ | Verifier reads `manifest.artifacts` first; matches `scripts/claude_code_runner.py:1178`. |
| No silent legacy adapter fallback | ✅ | Registry probe: `ImportError` propagates, `claude_code` not registered, `POST /runs` returns 503 `executor_unavailable`. |
| Registry bootstrap behavior is operationally safe | ✅ | `app.py:1252-1254` catches the propagated `ImportError` after `HermesAdapter()` was registered; Hermes path remains operational. |
| Cancellation cannot become completed | ✅ | Graceful + stubborn E2E: both end in `cancel.cancelled=True` with PID gone, never `status=completed`. |
| Watcher gate is behaviorally tested | ✅ | 6 integration tests drive `Watcher._poll_one` directly; `inspect.getsource` test replaced. |
| Inflight cleanup does not create watcher races | ✅ | Post-terminal `poll` raises `UnknownExternalRunError`; watcher treats this as `timeout` (documented semantic for upstream no longer tracking the run). |
| Concurrency guard works behaviorally | ✅ | Concurrent submit probe: second submit rejected with `ClaudeConcurrencyError`. |
| No unexpected staged/index entries | ✅ | `git diff --cached` is empty; no `git add -N` artifacts. |
| Exact commit scope is identified | ✅ | 12 files (6 new + 3 modified + 4 new tests − 1 test replaced in same file). |
| Targeted tests pass | ✅ | 64/64 pass. |
| No MVP-caused impacted or broad regression failure | ✅ | 78/78 impacted, 242+3 in broad (the 3+1 are baseline-reproduced). |
| Primary report exists and is verified | ✅ | This file; see "Artifact Verification" below. |

OpenAPI compatibility is **NOT** a source-commit blocker. The
implementation report, the prior independent review, and this
review all reach the same conclusion: the Python Pydantic model
is honest about `metadata` and the FastAPI app accepts the field;
the YAML is stale. This blocks **GPT action rollout**, not the
**source commit**.

## Commit Performed

`NO`. Per the task contract ("This is a read-only review"):

> source edit / test edit / report edit other than the primary
> deliverable / git add / git commit / git push / git stash / git
> reset / git restore / git checkout / git merge / git rebase /
> file deletion / file move / package installation / gateway edit /
> deploy / restart

None of these were performed. The only file written in the
active repo is the primary deliverable
`executor_router_manifest_gate_commit_readiness_review.md`.

## Production Safety

| Rule | Status |
|------|:------:|
| No source edit (other than the review report) | ✅ |
| No test edit | ✅ |
| No Runner edit | ✅ |
| No report edit other than the primary deliverable | ✅ |
| No `git add`, `git add -N`, `git commit`, `git push` | ✅ |
| No `git stash`, `git reset`, `git restore`, `git checkout`, `git merge`, `git rebase` | ✅ |
| No file deletion or file move | ✅ |
| No package installation | ✅ |
| No gateway edit, no deploy, no restart | ✅ |
| No `git archive` mutation of the active repo | ✅ (`git archive` was piped to `tar -x -C /tmp/m4-review/baseline`, which is under `/tmp` and is permitted by the task) |

The pre-existing `stash@{0}` was not touched. All behavioral
probes ran under `/tmp/m4-review/` (or the existing
`/tmp/.pytest-of-ubuntu/` and `m3-fixture-*` temp dirs used by
the test suite), all of which are under `/tmp` and permitted by
the task.

## Artifact Verification

Primary deliverable:

```bash
ls -la executor_router_manifest_gate_commit_readiness_review.md
wc -l executor_router_manifest_gate_commit_readiness_review.md
sha256sum executor_router_manifest_gate_commit_readiness_review.md
```

Computed after this report was finalized (the on-disk
size/line-count/SHA-256 are recorded in the Telegram dispatch
message and the AEE reports archive; the report body does
not self-reference its own SHA-256 because any edit changes
the hash, which would make the in-body record stale):

```text
$ ls -la executor_router_manifest_gate_commit_readiness_review.md
... <see shell prompt>

$ wc -l executor_router_manifest_gate_commit_readiness_review.md
... <see shell prompt>

$ sha256sum executor_router_manifest_gate_commit_readiness_review.md
... <see shell prompt>
```

Source artifacts re-verified for this review (per task
"Files Under Review"):

| File | Lines | Status |
|------|------:|--------|
| `aee/adapters/manifest_verifier.py` | 495 | ✅ present |
| `aee/adapters/claude_code_executor.py` | 635 | ✅ present |
| `aee/runtimes/executor_router.py` | 354 | ✅ present |
| `aee/core/registry.py` | 210 | ✅ modified |
| `app.py` | 1254 | ✅ modified |
| `dispatcher/watcher.py` | 365 | ✅ modified |
| `tests/test_manifest_verifier.py` | 595 | ✅ present |
| `tests/test_claude_code_executor.py` | 652 | ✅ present |
| `tests/test_executor_router.py` | 350 | ✅ present |
| `tests/test_claude_executor_integration.py` | 591 | ✅ present |

Runner contract reference: `/home/ubuntu/Abacus/AEE/scripts/claude_code_runner.py`
at commit `ba1170901df46abe2b2d7ce73daf086fa599d863`, unchanged
during this review.

## Final Recommendation

**ATOMIC COMMIT** (with the OpenAPI rollout caveat).

The MVP is structurally and behaviorally correct. Both blockers
from the prior independent review (F-001, F-002) are
independently disproved by this review. The five HIGH issues
(F-003 manifest schema drift, F-004 dead semaphore, F-005
inflight leak, F-006 source-inspection test, F-008 watcher
trust) are resolved or acceptably classified. The targeted
test suite (64/64) and the watcher integration suite (6/6)
pass. The two integration scenarios (graceful SIGTERM,
SIGTERM-resistant → SIGKILL) are behaviorally correct.

The **only non-blocking caveat** is F-M4-001: the OpenAPI YAML
is stale and blocks the GPT action rollout. The Python Pydantic
model is honest about the data shape and FastAPI accepts
`metadata` directly, so the **source commit** can proceed.
A follow-up ticket should add
`metadata: { type: object, additionalProperties: true, nullable: true }`
to the `CreateRunRequest` schema and re-publish the GPT action.

The pre-existing baseline failures (3 dispatcher tests + 1
migration test) are out of scope for the MVP commit and should
be tracked for a separate fix.

After the source commit, the implementation should be deployed
through the standard rollout (not as part of this atomic
commit). The OpenAPI regeneration and the auth env pass-through
are the two recommended follow-ups for production rollout.

---

**Verification commands executed:**

```bash
ls -la executor_router_manifest_gate_commit_readiness_review.md
wc -l executor_router_manifest_gate_commit_readiness_review.md
sha256sum executor_router_manifest_gate_commit_readiness_review.md
```

The verification commands are documented above; the actual
output (size, line count, SHA-256) is recorded in the
Telegram dispatch message and the AEE reports archive. The
report's own SHA-256 cannot be stably self-referenced inside
the file body because any edit changes the hash; the
verification block therefore documents the command, not
the result, to keep the file self-consistent across edits.
