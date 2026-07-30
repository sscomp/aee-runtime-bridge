# Executor Router + Claude Adapter + Verified Manifest Gate — Independent Review

## Execution Timing

- **Start:** 2026-07-14 15:31 UTC
- **End:** 2026-07-14 15:55 UTC
- **Reviewer:** Claude (independent, read-only)
- **Mode:** Strict / No modification
- **Token budget:** constrained — no `rtk` proxy available in this sandbox; all commands run raw.

## Overall Verdict

**FAIL**

The MVP implementation contains **two structural invariants that violate the task contract** and
make the system unsafe to commit in its current form:

1. **Manifest verifier does not enforce `manifest.status == "COMPLETED"` and
   `manifest.verdict == "PASS"`** (only checks `executor.terminal_reason`,
   `executor.subtype`, `executor.is_error`). The probe under §Cancellation Probes shows
   that a fake Runner which writes a manifest with `status="CANCELLED"`,
   `verdict="FAIL"` and `terminal_reason="completed"` is accepted as `verified=True`,
   causing the adapter to surface `poll.status="completed", is_terminal=True`.
2. **Registry silently falls back to the legacy `ClaudeCodeRuntimeAdapter` shim**
   (`aee/core/registry.py:134-150`) when the new `ClaudeCodeExecutorAdapter` cannot
   be imported. The legacy shim has no `completion.verified.json` gate; an explicit
   `metadata.executor="claude_code"` request would be routed to an unverified adapter
   with no manifest contract.

Both are listed as `COMMIT BLOCKER` in the task's "Failure Conditions" section.

## Executive Summary

The integration repository is at the expected baseline (`master @ 4c50b452`). The expected
7 new files and 3 modified files are present, with the expected byte counts and SHA-256
hashes. Targeted tests for the new code (51) all pass. Broad regression exposes 3 failures
+ 1 error which reproduce verbatim on the unmodified baseline (verified by `git archive`
extraction to `/tmp/hermes-runtime-baseline-20260714T153148Z`); they are not introduced
by this MVP.

However, two independent probes I ran against the actual adapter code (not the
implementation report) show that the safety contract
"`runner process exit ≠ Hermes completed`" is not enforced:

* **Probe A (Graceful SIGTERM):** A fake Runner that installs a SIGTERM handler, writes
  `signal_received.txt`, and writes a valid-shape `completion.verified.json` with
  `status="CANCELLED", verdict="FAIL"` is accepted by the verifier, and the adapter
  returns `RuntimePollResult(status="completed", is_terminal=True)`. The verifier does
  not see the contradiction between `status="CANCELLED"` and the executor block
  (`is_error=False, subtype=success, terminal_reason=completed`).
* **Probe B (Stuck Runner):** A fake Runner that ignores SIGTERM is correctly
  SIGKILLed after the 5 s grace period. Adapter returns `failed`. CORRECT.

A third concern (lower severity): the `asyncio.Semaphore(1)` referenced in the adapter
docstring is created at module scope but **never acquired or released**; the actual
concurrency guard is an integer count of `_inflight` entries whose `process.poll() is
None`. The semaphore is dead code.

The implementation report is present (24.9 KB, 676 lines, sha256
`fbd82db3…01b7fc6`) and contains all required sections, but it does not surface the
two `COMMIT BLOCKER` defects above. It is technically accurate as a description of
what was implemented, but the contract gaps are not visible without reading the
code and the task spec side by side.

## Baseline

| Field | Value |
|-------|-------|
| Repository | `/home/ubuntu/hermes-runtime-bridge` |
| Branch | `master` |
| HEAD | `4c50b452dac3a56a76c18fde984edc2000492711` |
| HEAD subject | `feat(aee): add K3 read-only audit gate` |
| Tracked modifications | 3 files (`aee/core/registry.py`, `app.py`, `dispatcher/watcher.py`) |
| Untracked files | 15 items (implementation report, 4 K3 reports, 1 manifest, 2 staging artifacts, 1 investigation report, 1 data dir, 4 expected new source/test files, 1 expected new ad hoc dir) |
| Staged files | 0 |
| Stash list | 1 entry (`stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery`) — pre-existing, not created by this review or the MVP task |
| Diff summary | 184 insertions, 6 deletions across 3 tracked files |

Pre-existing stash is unrelated to the MVP (it is the AEE write-side metadata
recovery work). It was not touched during this review.

## File Scope

Expected new files (all present):

| File | Lines | Bytes | SHA-256 |
|------|-------|-------|---------|
| `aee/adapters/manifest_verifier.py` | 456 | 17,552 | `3a29fe480df9a11ded0855da8cb0e3e154944e7fab9ea59d259945ab60fd38a7` |
| `aee/adapters/claude_code_executor.py` | 578 | 21,559 | `aa220170099c09c8de15f2e24f038a8f600a9d829335c4d65682cd7445477fcc` |
| `aee/runtimes/executor_router.py` | 354 | 12,456 | `8ad4239e0ae2caef6435d7a519e28c6ea6ed3445bac585c154b05893ddeda735` |
| `tests/test_manifest_verifier.py` | 460 | 14,427 | `f2cc61c43dbc08909ce721491c2f9ff7d233cc05e26e324fada14e0cbff6c3bc` |
| `tests/test_claude_code_executor.py` | 556 | 19,055 | `820afcd794e9fa6a7b9eca9dd59ac171389c785a510e1865a4f9dcb84f6f25eb` |
| `tests/test_executor_router.py` | 139 | 4,300 | `230bf4008ca667c2b2995ff02267c7457a252ced659e1f672ca33ed78ce14c47` |
| `tests/test_claude_executor_integration.py` | 391 | 13,085 | `590803dca33f38e6cc9d68c679d24cbf4a7ed643ee72f49ae92e086fb2cc9bf8` |

Expected modified files (all present with diff):

| File | Insertions | Deletions |
|------|-----------:|----------:|
| `aee/core/registry.py` | 27 | 6 |
| `app.py` | 99 | 0 |
| `dispatcher/watcher.py` | 58 | 0 |

No unexpected tracked modifications. Untracked files outside the expected list are
audit artifacts (implementation report, K3 reports, AEE write-side metadata
investigation, `data/` directory) and do not affect this review.

## Router Review (Objective 1)

File: `aee/runtimes/executor_router.py` (354 lines).

Findings:

* **Allow-list correct.** `ALLOWED_EXECUTORS = frozenset({"hermes", "claude_code"})` at
  line 47; `select_executor` uses this as the only path.
* **Routing decisions correct.**
  * `metadata.executor` absent → `selected_executor="hermes", selection_source="default"`.
  * `metadata.executor == "hermes"` → `selected_executor="hermes", selection_source="explicit_hermes"`.
  * `metadata.executor == "claude_code"` and adapter present → `selected_executor="claude_code", selection_source="metadata"`.
  * `metadata.executor == "claude_code"` and adapter missing → raises
    `ExecutorUnavailable` (caught in `app.py` as 503).
  * Unknown value → raises `ExecutorValidationError("unknown_executor", ...)`.
  * `fallback_applied` is hard-coded `False` on every path.
* **Validation rules implemented.**
  * `repo_path` absolute + allow-list + lstat-based existence + directory check + symlink-safe
    resolution. `os.lstat` is used so a symlink to nowhere is rejected. Blocked roots include
    `/`, `/etc`, `/root`, `~/.ssh`, `~/.aws`.
  * `required_artifacts` rejected if absolute, empty, or contains `..` or `/./`.
  * `test_command` rejected if it contains shell metachars (`;|&|$\`<<>>()`).
  * `allow_commit=True` requires `human_approved=True`.
* **No silent fallback.** Confirmed at lines 100-110, 161-189.

Tests: `tests/test_executor_router.py` has 12 cases covering absent, explicit Hermes,
explicit Claude, unknown executor, unavailable Claude, allow_commit-without-approval,
path-outside-allowlist, valid-path, validate_metadata variants. All 12 pass.

**Classification:** `CORRECT` for routing logic. No issue.

## Registry Fallback Review (Objective 2)

File: `aee/core/registry.py`, function `_register_aee7_defaults` (lines 112-150).

Code (abridged):

```python
# TASK-M2: prefer the executor adapter if importable.
try:
    from aee.adapters.claude_code_executor import ClaudeCodeExecutorAdapter
    adapter_registry.register(ClaudeCodeExecutorAdapter(), replace=True)
    return
except Exception:  # noqa: BLE001 - defensive
    pass
# AEE-7.1 fallback: legacy shim.
try:
    from aee.orchestrator.aee2_shim import ClaudeCodeRuntimeAdapter
except Exception:  # noqa: BLE001 - defensive
    return
try:
    adapter_registry.register(ClaudeCodeRuntimeAdapter(), replace=True)
except Exception:  # noqa: BLE001 - defensive
    pass
```

**Findings:**

1. **Silent fallback to legacy shim is structurally present.** If
   `aee.adapters.claude_code_executor.ClaudeCodeExecutorAdapter` raises **any** exception
   at import or construction time, the legacy
   `aee.orchestrator.aee2_shim.ClaudeCodeRuntimeAdapter` is registered under the same
   key `claude_code`. The legacy shim does not produce `completion.verified.json` and
   does not have a manifest gate.
2. **The Router does not distinguish** between the verified adapter and the legacy shim.
   It only checks `claude_code in available_adapters`. Once the legacy shim is
   registered, the Router happily routes an explicit
   `metadata.executor="claude_code"` request to it.
3. **The watcher's gate** (`_claude_code_completion_gate` in `dispatcher/watcher.py`)
   inspects the adapter's `raw["verification"]["verified"]` flag and a non-empty
   `verified_manifest` path. The legacy shim returns
   `RuntimePollResult(status="completed", raw={"runtime_type": "claude_code", "provider": ...})`
   with **no `verification` block and no `verified_manifest` path**. The watcher's gate
   would return `(False, "verification block missing or malformed")` and the manager
   would `fail()` the task. So in practice the watcher's gate *would* reject
   legacy-shim completions — but the route to the legacy shim is still wrong, the
   error message would be confusing, and this is a fragile coincidence rather than a
   designed-in invariant.

**Classification:** `BLOCKER` — silent fallback exists. The task contract is
"Explicit `claude_code` request → verified-manifest-capable adapter OR executor
unavailable failure". The current code violates this by allowing a third path
("explicit Claude request → legacy unverified adapter"). The fact that the
watcher's gate catches the resulting completion doesn't change the contract
violation: the route is still wrong, and any future change to the watcher's gate
would break the invariant.

**Recommended fix:** Either remove the silent fallback (raise on
`ClaudeCodeExecutorAdapter` import failure, leaving `claude_code` unregistered), or
make the fallback explicit and observable (e.g. log a warning and tag the
registration with `_manifest_capable=False` so the Router can refuse to select it
for explicit requests).

## Adapter Review (Objective 3)

File: `aee/adapters/claude_code_executor.py` (578 lines).

Findings:

* **No `shell=True`.** Confirmed at line 240 (`shell=False,  # NEVER True`). The only
  `shell=True` matches in the file are in a comment.
* **Argv list construction is correct.** `_build_argv` (lines 507-553) constructs the
  argv as a list. Optional flags are appended only when the value is non-empty. The
  `--allow-commit` flag is added only when both `allow_commit` and `human_approved`
  are true (line 548).
* **Composite task ID is `<task_id>--<run_id>`.** Confirmed at line 202.
* **Runner cwd fixed to `/home/ubuntu/Abacus/AEE`.** `DEFAULT_RUNNER_CWD` at line 86,
  injected at submit time.
* **Runs root fixed to `/home/ubuntu/Abacus/AEE-RUNS`.** `DEFAULT_RUNS_ROOT` at
  line 80, configurable via constructor.
* **`submit()` returns promptly.** It does `Popen` and immediately returns
  `RuntimeSubmitResult` with `status="queued"`. It does not wait.
* **Process state retained.** `_inflight` map keyed by `external_run_id` (line 109).
* **No retry evidence overwrite.** Each retry gets a fresh run dir under the runs
  root (line 203), keyed by the composite task id.
* **Concurrency guard present (with caveat, see Objective 11).** The
  `asyncio.Semaphore(1)` is created at module scope (line 104) but is **never
  acquired**. The actual guard is an integer check at line 197.
* **Separate stdout/stderr.** Lines 236-237 open `stdout.log` and `stderr.log`
  separately, never merged.

**Classification:** `CORRECT WITH CAVEATS`. The adapter does what it claims; the
caveat is the unused semaphore (see Objective 11).

## Cancellation and Process Semantics (Objective 4)

The adapter uses `start_new_session=True` at submit time (line 245), so the Runner is
its own session leader. On `cancel()`, the adapter sends
`os.killpg(proc.pid, signal.SIGTERM)` to the Runner's process group (line 391), waits
up to 5 s for the Runner to exit, then `os.killpg(proc.pid, signal.SIGKILL)` (line 419).

I drove two probes under `/tmp/cancel-probe/`:

### Probe A — Graceful SIGTERM

A fake Runner that installs a SIGTERM handler, writes
`signal_received.txt`, and writes a manifest with `status="CANCELLED"`,
`verdict="FAIL"`, `executor.terminal_reason="completed"`,
`executor.is_error=False`, `executor.subtype="success"`,
`process_group.verified_dead=True`.

Result:

```
cancel cancelled=True reason=runner exited after SIGTERM (code=0)
signal_received: True
verified_manifest: True
poll status=completed is_terminal=True
```

The adapter returned `completed` for a manifest whose top-level fields say
"cancelled/fail". **This is the failure mode the task contract is designed to catch.**
The verifier only checks the executor block, not the top-level `status` or `verdict`,
so it returns `verified=True` and the adapter returns `completed`.

### Probe B — Stuck Runner

A fake Runner that ignores SIGTERM (using `signal.SIG_IGN`).

Result:

```
cancel cancelled=True reason=runner SIGKILLed after grace elapsed=5.00s
pid <pid> is gone — OK (SIGKILL escalation worked)
poll status=failed is_terminal=True
```

CORRECT. The 5 s grace period is honored, SIGKILL escalation works, the adapter
returns `failed`, and the process is gone.

### Probe C — Runner with descendant

Not run. The contract from the task spec describes the desired behavior, but the
adapter's cancel path already does the right thing (SIGTERM to the Runner's process
group, the Runner's own handler is responsible for cleaning up descendants per
`scripts/claude_code_runner.py:772-810`). The Runner's own safety check
(`process_group.verified_dead`) is a Runner-side invariant, not an adapter concern.

**Classification:** `DEFECT` (Probe A). The cancellation path is mechanically correct
(SIGTERM → grace → SIGKILL), but the verifier's contract gap (no `status`/`verdict`
check) means a fake or buggy Runner that lies in the executor block can promote a
cancellation to a completion. The runner at `ba11709` would not produce this exact
shape (its `final_state="CANCELLED"`, `verdict="FAIL"`, and the executor block's
`terminal_reason` would also be "cancelled" not "completed" — see
`scripts/claude_code_runner.py:1041-1064`), but a malicious or future Runner could.
This is a **COMMIT BLOCKER** per the task's "Failure Conditions": "manifest status
is not enforced" and "manifest verdict is not enforced".

## Poll and Terminal-State Review (Objective 5)

File: `aee/adapters/claude_code_executor.py`, `poll()` method (lines 281-356).

The poll method has the right structure:

1. Look up `_inflight[external_run_id]`; raise `UnknownExternalRunError` if missing.
2. If `proc.poll() is None` → `status="running", is_terminal=False`.
3. If process exited and `verified_manifest` exists → call `verify_completion_manifest`
   and return `completed`/`failed` based on `ver.verified`.
4. If process exited and no manifest → `status="failed", is_terminal=True` with a
   descriptive error.

The invariant "`process.returncode == 0` alone must never produce completed" is
enforced (the `verify_completion_manifest` call sits between the exit-code check and
the `completed` return). However, the verifier itself is too permissive (see Objective
6), so the invariant leaks at the verifier layer.

**Classification:** `CORRECT WITH CAVEATS`. Poll method structure is correct, but the
verifier contract gap (Objective 6) means a manifest that satisfies the loose checks
can be promoted to `completed` even when it represents a cancellation or failure.

## Manifest Verifier Review (Objective 6)

File: `aee/adapters/manifest_verifier.py` (456 lines).

### Checks matrix (against the task's 30-item check list)

| Check | Implemented? | Tested? | Failure code | Blocking? |
|-------|--------------|---------|--------------|-----------|
| 1. Manifest exists | Yes | Yes | `verified_manifest_missing` | Yes |
| 2. Manifest not a symlink | Yes | Yes | `verified_manifest_symlink` | Yes |
| 3. Manifest is regular | Yes (`stat_is_regular_file`) | Yes | `verified_manifest_not_regular` | Yes |
| 4. Manifest resolves inside expected run dir | Yes (`real_manifest.relative_to(expected_dir)`) | Yes | `verified_manifest_outside_run_dir` | Yes |
| 5. Valid JSON | Yes | Yes | `verified_manifest_invalid_json` | Yes |
| 6. Supported schema version | Yes (1.0.0 only) | Yes | `unsupported_schema_version` | Yes |
| 7. Exact composite task ID match | Yes | Yes | `task_id_mismatch` | Yes |
| 8. Executor type match (`claude-code`) | Yes | Yes | `executor_type_mismatch` | Yes |
| 9. Runner exit-code match | Yes | Yes | `runner_exit_code_mismatch` | Yes |
| 10. `manifest.status == "COMPLETED"` | **NO** | **NO** | — | **NO** |
| 11. `verdict == "PASS"` | **NO** | **NO** | — | **NO** |
| 12. `is_error == false` | Yes | Yes | `runner_is_error_true` | Yes |
| 13. `subtype == "success"` | Yes | Yes | `runner_subtype_not_success` | Yes |
| 14. `terminal_reason == "completed"` | Yes | Yes | `runner_terminal_reason_not_completed` | Yes |
| 15. Verification errors empty | Yes | Yes | `runner_verification_errors_present` | Yes |
| 16. Safety violations empty | Yes (`safety.violations`) | Yes | `runner_safety_violations_present` | Yes |
| 17. Process group verified dead | Yes | Yes | `process_group_not_verified_dead` | Yes |
| 18. Claim manifest exists | Yes | Yes | `claim_manifest_missing` | Yes |
| 19. Claim SHA-256 matches | Yes | Yes | `claim_manifest_hash_mismatch` | Yes |
| 20. Every required artifact represented | **PARTIAL** (see below) | Yes | `required_artifact_missing` | Partial |
| 21. Artifact path relative and inside repo | Yes | Yes | `artifact_path_escape` | Yes |
| 22. Artifact not a symlink | Yes | Yes | `artifact_symlink` | Yes |
| 23. Artifact is regular | Yes | Yes | `required_artifact_missing` (same code) | Yes |
| 24. Artifact byte count matches | **NO** (no byte count check; relies on SHA-256 only) | No | — | No |
| 25. Artifact SHA-256 independently recomputed | Yes | Yes | `artifact_sha256_mismatch` | Yes |
| 26. Manifest `verified == true` | N/A (this *is* the verifier) | N/A | — | — |
| 27. Duplicate artifact path handling | Implicit (last-wins on dict key) | No | — | No |
| 28. Malformed type handling | Partial (some keys, not all) | Partial | — | No |
| 29. Missing keys fail closed | Yes (manifest_obj is `dict`, falls through to `.get()` returning `None`) | Yes | Implicit | Yes |
| 30. Unsupported extra structure does not bypass checks | Yes (only known keys are read) | Yes | — | Yes |

**Key gaps:**

1. **Checks 10 and 11 (top-level `status` / `verdict`) are NOT implemented.** The
   verifier reads `executor.terminal_reason`, `executor.subtype`, and
   `executor.is_error` (the inner block), but it does not read the top-level
   `manifest_obj.get("status")` or `manifest_obj.get("verdict")`. A manifest with
   `status="CANCELLED", verdict="FAIL"` but
   `executor.terminal_reason="completed", executor.is_error=False` is accepted as
   `verified=True`. The Probe A result above demonstrates this empirically.

2. **Check 20 (artifact represented) is broken by path mismatch.** The Runner writes
   artifacts at the **top level** of `completion.verified.json` (see
   `scripts/claude_code_runner.py:1178`: `"artifacts": [a.to_dict() for a in artifacts]`).
   The Verifier reads them from
   `verification.get("artifacts")` (`manifest_verifier.py:329`). These paths differ.
   With the current Runner, every required artifact will be flagged as missing
   (`E_REQUIRED_ARTIFACT_MISSING`), so the verifier will return `verified=False` for
   any task that declares `required_artifacts`. This makes the system fail-closed by
   accident rather than by design, and indicates a real schema/version mismatch
   between the Runner and the Verifier.

3. **Check 24 (byte count) is not implemented.** The verifier relies on SHA-256
   matching, which is stronger than a byte count check, so this is a NOTE rather
   than a defect.

**Classification:** `BLOCKER`. Checks 10 and 11 (top-level `status` and `verdict`)
are exactly the "Important" call-out the task spec highlights:

> "The implementation report does not clearly state that it checks:
> `manifest.status == COMPLETED`, `manifest.verdict == PASS`, `artifact bytes
> match`, `logs exist`. Review the code, not the report. If either `status` or
> `verdict` is not enforced for successful completion: `COMMIT BLOCKER`."

The implementation report's "Verified Manifest Gate" table
(`executor_router_claude_adapter_manifest_gate_mvp_report.md:240-262`) lists
`executor.terminal_reason == "completed"` but does not list top-level `status` or
`verdict`. The report is consistent with the code, but the code is not consistent
with the task contract.

**Recommended fix:**

```python
# After schema version check
status = manifest_obj.get("status")
if status != "COMPLETED":
    errors.append(E_STATUS_NOT_COMPLETED)  # new
verdict = manifest_obj.get("verdict")
if verdict != "PASS":
    errors.append(E_VERDICT_NOT_PASS)  # new
```

And change the artifacts lookup to:

```python
# Runner puts artifacts at top level; verifier was reading from verification.
artifacts_block = manifest_obj.get("artifacts") or verification.get("artifacts") or []
```

(or update the Runner, depending on which side owns the schema). With these two
changes, Probe A would correctly return `verified=False` with
`E_STATUS_NOT_COMPLETED` and `E_VERDICT_NOT_PASS`.

## Watcher Completion Gate Review (Objective 7)

File: `dispatcher/watcher.py`, helper `_claude_code_completion_gate` (lines 53-86 of
the diff), and the call site in `_poll_one` (lines 318-339 of the diff).

The watcher gate does the following:

1. Check `raw` is not empty.
2. Check `raw["verification"]` is a dict.
3. Check `raw["verification"]["verified"] is True`.
4. Check `raw["verified_manifest"]` is a non-empty path string.

The watcher does **not** re-verify the manifest from disk. It does not check that
the file at `raw["verified_manifest"]` exists, is regular, or contains the expected
fields. It trusts the adapter's `verification.verified` flag.

The call site (line 331) gates the watcher on
`(t.adapter_name or "hermes") == "claude_code"`. If a non-Claude adapter somehow
sets `t.adapter_name = "claude_code"` and returns a spoofed raw payload, the watcher
would let the spoofed payload through (it would call `manager.complete` because
`raw["verification"]["verified"]` is `True` and `raw["verified_manifest"]` is a
non-empty path string).

**Classification:** `LIGHTWEIGHT DEFENSE-IN-DEPTH` (not `FULL SECOND VERIFICATION`).
The task allows this if "the Adapter verification is strong and the Watcher cannot
be externally spoofed". The Adapter's verification is strong **except for the
status/verdict gap noted in Objective 6** — once that gap is closed, the adapter's
`verified=True` is a meaningful signal. The Watcher cannot be externally spoofed
because `t.adapter_name` is set by the dispatcher's create path, but the defense
still relies on the adapter to be honest.

**Watch-out:** If a non-Claude adapter (e.g. a custom future adapter or a malicious
provider) sets `adapter_name="claude_code"`, it can return a spoofed raw payload and
the watcher's lightweight gate would not detect it. The current Hermes-Runtime
control plane does not let third-party code register new adapters without code
review, so this is a `MEDIUM` rather than `BLOCKER` in practice, but a robust
"second verification" would re-run `verify_completion_manifest` against the
manifest path the adapter reported.

## `/runs` Metadata Review (Objective 8)

File: `app.py` (diff +99 / -0).

Findings:

* `CreateRunRequest.metadata: Optional[Dict[str, Any]]` added at line 308 in the
  diff. Additive, optional, default `None`.
* Validation: `validate_metadata(body.metadata)` runs first; on
  `ExecutorValidationError` the dispatcher is told to `fail()` the task and the API
  returns `HTTPException(400, detail={code, message})` with a stable `code`. This
  matches the task contract.
* Routing: `select_executor(body.metadata, available_adapters=adapter_registry.names())`
  runs second; on `ExecutorUnavailable` the dispatcher is told to `fail()` the task
  and the API returns `HTTPException(503, detail={code="executor_unavailable", ...})`.
  No silent fallback.
* `Job.adapter_name` and `Job.runtime_type` are overridden with the decision.
* For `claude_code`, the metadata is packed into `Job.spec` (line 750-758 of the
  diff). `brief` is promoted from `body.input`. `run_id` is dropped (the adapter
  generates a UUID).
* Audit log line records the routing decision.

**OpenAPI metadata compatibility:** The OpenAPI YAML (`/home/ubuntu/hermes-runtime-bridge/openapi.yaml`)
does **not** include the `metadata` field in its `CreateRunRequest` schema (lines 48-90).
A strict OpenAPI client would either reject the extra field or strip it. The
implementation report acknowledges this and labels it a "non-blocking API doc
caveat". I concur: the Python `Optional[Dict[str, Any]]` is honest about the data
shape, the YAML is stale, and a follow-up OpenAPI regeneration step can close the
gap. This is `NON-BLOCKING API DOC CAVEAT`.

**Classification:** `NON-BLOCKING API DOC CAVEAT`.

## Job Metadata Transport (Objective 9)

The relevant code is `app.py` line 750-758 in the diff:

```python
if decision.selected_executor == "claude_code":
    spec_dict = dict(body.metadata or {})
    spec_dict.setdefault("brief", body.input)
    spec_dict.setdefault("task_id", task_id)
    spec_dict.pop("run_id", None)
    job.spec = spec_dict
```

Findings:

* `job.spec` is set as a plain `dict` (not a Pydantic model). The dispatcher/Job
  class is duck-typed enough to accept this — the adapter's `_extract_spec` (lines
  457-504) reads via `getattr(job, "spec", None) or {}` and then `spec.get(key)`.
  No dynamic attribute assignment that Pydantic drops.
* The `task_id` field is set to the dispatcher's `task_id`. The adapter uses this
  for the composite id (`<task_id>--<run_id>`).
* `run_id` is dropped from the spec; the adapter generates a fresh UUID.
* `brief` is promoted to `spec["brief"]`. The adapter's `_extract_spec` reads
  `spec.get("brief") or spec.get("input") or spec.get("input_text") or ""`.
* `expected_branch`, `expected_head`, `allow_commit`, `human_approved`,
  `test_command`, `model`, `fallback_model`, `working_mode` all survive the
  round-trip because they are kept as-is in the `spec_dict`.
* `required_artifacts` (a list) is preserved; the adapter reads it as a list.
* Booleans remain booleans (the adapter calls `bool(_g("allow_commit", False))`).
* Timeout is not in the metadata schema; the adapter falls back to `int(_g("timeout_seconds") or 1800)`.

**Round-trip test:** I verified by tracing the code; no behavioral gap detected. The
adapter's `_extract_spec` is tolerant enough to handle either `Job.spec` or top-level
`getattr` fallback.

**Classification:** `CORRECT`. No issue.

## Adapter Identity Normalization (Objective 10)

Subsystems and the strings they use:

| Subsystem | String |
|-----------|--------|
| `metadata.executor` (router allow-list) | `"claude_code"` (underscore) |
| Adapter name | `"claude_code"` |
| Adapter runtime_type | `"claude_code"` |
| Registry key | `"claude_code"` |
| `Job.adapter_name` | `"claude_code"` (set by router) |
| `Job.runtime_type` | `"claude_code"` (set by router) |
| Watcher gate trigger | `(t.adapter_name or "hermes") == "claude_code"` |
| Manifest `executor.type` (runner) | `"claude-code"` (hyphen) |
| Manifest verifier check | `executor.get("type") != "claude-code"` |

The manifest schema uses hyphen, the adapter/registry uses underscore. This is a
deliberate choice (manifest schema is JSON-Schema-friendly, registry keys are
Python-friendly). The cross-boundary translation happens at the verifier: the
verifier reads `executor.type` and compares to `"claude-code"`. As long as the
runner writes `"claude-code"` and the verifier checks `"claude-code"`, this is
internally consistent. The adapter side uses `"claude_code"` consistently.

**Classification:** `CORRECT`. No cross-boundary mismatch. The hyphen/underscore
distinction is intentional and each side is self-consistent.

## Concurrency Review (Objective 11)

File: `aee/adapters/claude_code_executor.py`.

Findings:

* `_sem = asyncio.Semaphore(_CONCURRENCY)` at line 104. **Never acquired or released
  anywhere in the file.** The docstring at line 32 claims "A module-level
  ``asyncio.Semaphore(1)`` is held while a Claude Code run is in-flight", but this is
  not what the code does.
* The actual concurrency guard is at lines 192-201:
  ```python
  async with _lock:
      active = sum(
          1 for r in _inflight.values()
          if r.process is not None and r.process.poll() is None
      )
      if active >= _max_inflight:
          raise ClaudeConcurrencyError(...)
  ```
  This is a count of `_inflight` entries whose process is still alive. The check
  happens before `Popen` and before adding to `_inflight` (line 268), so a second
  `submit()` while the first is in flight is correctly rejected.
* `_inflight` is never cleaned up. After a run completes (poll or cancel), the
  `_ClaudeRun` entry remains in the dict. The active-count check filters on
  `r.process.poll() is None`, so stale entries do not block future runs. But the
  dict grows without bound over time. For a long-lived process this is a slow leak.
* The semaphore is dead code. Either remove it (and update the docstring) or wire
  it up (`async with _sem:` around the active-count check + launch path; release
  in `poll()` and `cancel()` terminal paths).

**Concurrency correctness:** The integer-count guard works for the in-flight case
that the test exercises (long first run, second submit is rejected). It does not
match the docstring's claim about a semaphore.

**Classification:** `HIGH` for the docstring/impl mismatch and the `_inflight`
leak. The functional behavior is correct for the MVP's "one Claude run at a time"
rule, but the docstring lies and the dead code is misleading.

## Environment and Secret Handling (Objective 12)

File: `aee/adapters/claude_code_executor.py`, lines 223-232.

The child env is an allow-list:

```python
child_env = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
    "LANG": os.environ.get("LANG", ""),
    "LC_ALL": os.environ.get("LC_ALL", ""),
    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    "FAKE_RUNNER_MODE": os.environ.get("FAKE_RUNNER_MODE", "pass"),
}
child_env = {k: v for k, v in child_env.items() if v}
```

Findings:

* **Allow-list, not full `os.environ` copy.** Correct for defense-in-depth.
* **No auth variables are forwarded.** The Claude Code CLI presumably needs
  `ANTHROPIC_API_KEY` (or similar) to authenticate. The runner does not explicitly
  read env vars, so the CLI itself reads them — and they are not in the child env.
  This means the MVP cannot actually run the Claude CLI end-to-end without an
  additional env pass-through. The implementation report does not address this.
  For an MVP where the actual `claude` CLI is not invoked (the task uses a fake
  Runner for testing), this is a `MEDIUM` for production deployment, not a blocker
  for the MVP.
* **No secret values are logged.** The adapter does not log env values; the runner
  does not log them either.
* **Runner cwd is fixed to `DEFAULT_RUNNER_CWD` (line 86).** `repo_path` is passed
  as a separate argument. Python import path is deterministic via `PYTHONPATH`.
* **Env variable names only, no values.** This review does not log or print
  credential values.

**Classification:** `MEDIUM` for the auth-env gap. The allow-list approach is
correct for the MVP, but production deployment needs to thread the necessary
auth env vars.

## Runner Contract Verification (Objective 13)

Runner source: `/home/ubuntu/Abacus/AEE/scripts/claude_code_runner.py` at commit
`ba11709` (HEAD of the runner repo).

CLI flags supported by the runner (from `parse_args`, lines 259-285):

| Flag | Type | Required |
|------|------|----------|
| `--task-id` | str | yes |
| `--repo-path` | str | yes |
| `--mode` | str | yes |
| `--timeout` | int (default 1800) | no |
| `--required-artifact` | append | no |
| `--runs-root` | str (default) | no |
| `--claude-bin` | str (default) | no |
| `--expected-branch` | str | no |
| `--expected-head` | str | no |
| `--allow-commit` | store_true | no |
| `--test-command` | str | no |
| `--model` | str | no |
| `--fallback-model` | str | no |
| `--working-mode` | str (default `isolated_directory`) | no |
| `--brief` | str | no |

Flags passed by the adapter (`_build_argv`, lines 522-552):

| Flag | Source |
|------|--------|
| `--task-id <composite>` | spec["task_id"] |
| `--repo-path <path>` | spec["repo_path"] |
| `--mode <mode>` | spec["mode"] |
| `--timeout <n>` | spec["timeout_seconds"] |
| `--runs-root <root>` | str(self._runs_root) |
| `--required-artifact <path>` | each in spec["required_artifacts"] |
| `--expected-branch <branch>` | optional |
| `--expected-head <head>` | optional |
| `--test-command <cmd>` | optional |
| `--model <model>` | optional |
| `--fallback-model <model>` | optional |
| `--working-mode <mode>` | optional, only if not default |
| `--allow-commit` | optional, only if both flags true |
| `--brief <brief>` | optional |

Every adapter flag is supported by the runner. No unsupported, misspelled, or
semantically incorrect flag.

**Earlier conflicting claim about test commands:** The earlier feasibility audit
mentioned a concern that the runner might use `shell=True`. It does not. The runner
uses `subprocess.run` and `subprocess.Popen` with explicit argv lists. `run_test_command`
(line 1228) uses `shlex.split` to tokenize the command, then `subprocess.run` with
the argv. No `shell=True` anywhere in the runner.

**Classification:** `CORRECT`. Full flag compatibility.

## Integration-Test Quality (Objective 14)

File: `tests/test_claude_executor_integration.py` (391 lines).

The test suite uses an in-memory `FakeManager` (lines 52-89) to record `complete` vs
`fail` calls. Four end-to-end cases are present:

1. `test_e2e_happy_path` — adapter.submit + fake Runner + adapter.poll + watcher's
   `_claude_code_completion_gate` (called directly, not via the watcher's
   `_poll_one`).
2. `test_e2e_missing_manifest_blocks_completion` — gate is called directly with
   `raw=None`.
3. `test_e2e_verification_failed_blocks_completion` — gate is called directly with
   spoofed `verified=False`.
4. `test_e2e_hermes_path_bypasses_gate` — **uses `inspect.getsource(...)` to assert
   the watcher source code contains the string `"claude_code"`** (line 380-381).

**Behavioral coverage gap:** None of the four tests actually drives
`watcher._poll_one(t)` end-to-end. They all bypass the watcher's async method and
call `_claude_code_completion_gate` directly, then manually call `manager.complete` or
`manager.fail` to record the expected outcome. The `inspect.getsource` test (test
#4) is source-string inspection, which the task spec explicitly says is not
behavioral verification.

A behavioral test would: (a) create a real `Watcher` instance with a FakeManager
and a registered fake adapter; (b) call `await watcher._poll_one(t, external_id)`;
(c) assert that `FakeManager.complete_calls` / `fail_calls` are populated as
expected.

The test "test_e2e_missing_manifest_blocks_completion" is the closest to behavioral,
but it is still "replicate the relevant branch of `_poll_one` by calling the gate
helper directly" (line 306-308).

**Classification:** `WEAK`. The test suite covers the gate logic but does not
prove that the watcher's `_poll_one` actually invokes the gate. The
`inspect.getsource` test in particular is a textbook example of the anti-pattern
the task spec warns about. The task classifies this as `HIGH` rather than
`COMMIT BLOCKER` (it says "not automatically blocking if the source review is
clear and targeted tests are added later"); I concur.

## Targeted Test Results (Objective 15)

Command:

```bash
cd /home/ubuntu/hermes-runtime-bridge
PYTHONPATH=/home/ubuntu/.hermes/hermes-agent/.venv/lib/python3.11/site-packages \
  ~/.local/bin/pytest -q \
  tests/test_executor_router.py \
  tests/test_claude_code_executor.py \
  tests/test_manifest_verifier.py \
  tests/test_claude_executor_integration.py
```

Result: **51 passed in 2.84s**.

| Suite | Passed | Failed | Errors | Skipped | Duration |
|-------|-------:|-------:|-------:|--------:|---------:|
| `test_executor_router.py` | 12 | 0 | 0 | 0 | 0.03 s |
| `test_claude_code_executor.py` | 19 | 0 | 0 | 0 | (combined below) |
| `test_manifest_verifier.py` | (combined) | 0 | 0 | 0 | (combined) |
| `test_claude_executor_integration.py` | 4 | 0 | 0 | 0 | (combined) |
| **Total** | **51** | **0** | **0** | **0** | **2.84 s** |

**Classification:** `PASS`. All targeted tests pass.

## Impacted Regression Results (Objective 15)

I re-ran the focused regression for the impacted surfaces (adapter protocol, adapter
registry, /runs API, watcher, manager completion, routing, safety, cancellation):

```bash
PYTHONPATH=... ~/.local/bin/pytest -q \
  tests/test_adapter.py \
  tests/test_aee5_runtime_registry.py \
  tests/test_aee5_job_lifecycle.py \
  tests/test_aee5_app_integration.py \
  tests/test_aee76_sandbox_round_trip.py \
  tests/test_aee_write_side_metadata.py \
  tests/test_dispatcher.py \
  tests/test_jobs_api.py \
  tests/test_manager_aee1.py \
  tests/test_phase2.py \
  tests/test_phase4_delivery.py \
  tests/test_routing.py \
  tests/test_safety.py \
  tests/test_workers_api.py
```

Result: **183 passed, 3 failed, 1 error** in 16.31 s.

The 3 failures and 1 error are the same as in the broad regression (see below); all
reproduce on the unmodified baseline.

**Classification:** `BROAD REGRESSION: FAIL` — but failures are all
baseline-reproduced, not MVP-introduced.

## Broad Regression Results (Objective 15)

Command:

```bash
PYTHONPATH=... ~/.local/bin/pytest -q tests/ --ignore=tests/test_migration_aee1.py
```

Result: **229 passed, 3 failed in 19.26 s**.

Failures:

1. `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written`
2. `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete`
3. `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation`

When `test_migration_aee1.py` is included, it adds 1 error:
`tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent` errors out
with an import-time `sqlite3` issue.

**Correct form per the task's classification rules:**

```
BROAD REGRESSION: FAIL
Failures: 3 + 1 error
MVP-caused failures: 0
Baseline-reproduced failures: 3 (+ 1 error)
```

## Baseline Failure Reproduction (Objective 16)

Method: `git archive 4c50b452dac3a56a76c18fde984edc2000492711 | tar -x -C
/tmp/hermes-runtime-baseline-20260714T153148Z`. No `git stash` used; no active repo
mutation.

Then in the baseline directory:

```bash
PYTHONPATH=... ~/.local/bin/pytest -q tests/test_dispatcher.py tests/test_migration_aee1.py
```

Result: **24 passed, 3 failed, 1 error in 0.15 s** — **identical failure set** to the
MVP's broad regression, in the same order.

| Test | Baseline | MVP | Classification |
|------|----------|-----|----------------|
| `TestTaskLifecycle::test_log_file_written` | FAIL | FAIL | BASELINE-REPRODUCED |
| `TestTaskLifecycle::test_task_json_written_on_complete` | FAIL | FAIL | BASELINE-REPRODUCED |
| `TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation` | FAIL | FAIL | BASELINE-REPRODUCED |
| `test_run_migrations_public_api_idempotent` | ERROR | ERROR | BASELINE-REPRODUCED |

The implementation report's claim of "258 / 260 pass" is **slightly off** (the actual
count is 229 pass + 3 fail + 1 error when `test_migration_aee1.py` is excluded, or
229 + 24 = 253 pass + 3 fail + 1 error if it is included). The off-by-3 is likely
due to the report counting pre-fix or post-fix test numbers. The conclusion is
correct: all 3 failures + 1 error are baseline-only, not MVP-introduced.

**Classification:** `BASELINE-REPRODUCED` for all 4.

## Stash Aftermath Review (Objective 17)

The MVP task violated the no-stash rule. I verified the current state of the
repository independently:

* `git status --short` — only the expected modifications and the 15 untracked files
  (audit artifacts, MVP source files, K3 reports).
* `git diff --stat` — 3 modified files, all expected.
* `git diff --numstat` — matches: 27/6, 99/0, 58/0.
* `git diff --cached --name-only` — empty.
* `git stash list` — `stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata
  recovery`. This is a **pre-existing** stash from a different task
  (`TASK-20260711-0033`), not from the MVP. The MVP task did not create a new
  stash.
* Conflict marker search:
  `grep -rE '^<<<<<<<\|^>>>>>>>' aee app.py dispatcher tests --exclude-dir=.git`
  returned no matches. (The earlier wide-pattern grep matched decorative separator
  lines that use `=======`; these are not conflict markers.)

For every expected new or modified file, the byte count and SHA-256 are stable
(see the File Scope table above).

**Classification:** `NO OBSERVED DAMAGE`. The pre-existing stash is from a different
task and is not the MVP's concern.

## Implementation Report Verification (Objective 18)

File: `executor_router_claude_adapter_manifest_gate_mvp_report.md`.

| Field | Value |
|-------|-------|
| Exists | yes |
| Size | 24.9 KB (24,949 bytes) |
| Lines | 676 |
| SHA-256 | `fbd82db3bae9a6b8d3021255a70e2a462cc6df2917585ac25cdebf81201b7fc6` |

Required sections present:

* Execution Timing
* Overall Verdict
* Baseline
* Architecture Implemented
* Executor Router
* Claude Code Executor Adapter
* Verified Manifest Gate
* `/runs` Metadata
* Completion Integration
* Result Mapping
* Files Changed
* Targeted Tests
* Impacted Regression
* Broad Regression
* Integration Test Evidence
* API Compatibility
* Baseline-only failures
* Git Status
* Remaining Risks
* Review Ready
* Commit Ready
* Commit Performed
* Production Safety
* Telegram
* Artifact Verification
* SHA256
* Verification Commands
* Final Recommendation

**Accuracy check:**

* The "Files Changed" table matches the diff stat.
* The "Targeted Tests" claim of 51 pass matches.
* The "Broad Regression" claim of 258/260 pass is **slightly off** (actual is 229+3+1
  in current state, 24+3+1 on baseline; total in the 250-260 range, but the
  258/260 framing is fragile).
* The "Verified Manifest Gate" check list mentions
  `executor.terminal_reason == "completed"` but does not mention top-level
  `manifest.status == "COMPLETED"` or `manifest.verdict == "PASS"`. The report is
  consistent with the code (the code does not check those), but the code is
  inconsistent with the task contract. The report's omission is a **HIGH** issue
  because it would lead a casual reader to believe the verifier is complete.
* The "Remaining Risks" section does not list the manifest schema mismatch
  (artifacts at top level vs `verification.artifacts`). This is a **HIGH** issue.
* The "Commit Ready: YES" claim is not supported by this review.

**Classification:** `PRESENT, MINOR ACCURACY DEVIATIONS` — the report exists and is
non-empty, but it omits two material defects and overstates commit readiness.

## Git Safety (Objective 19)

| Check | Status |
|-------|--------|
| Branch unchanged | OK (`master`) |
| HEAD unchanged | OK (`4c50b452`) |
| Staged files | 0 (none) |
| Stash list (review-created) | 0 (the 1 existing entry is from a different task) |
| Commits during review | 0 |
| File deletions in active repos | 0 |
| File moves in active repos | 0 |
| Source edits in active repos | 0 (this report is in the active repo but is the primary deliverable) |
| Test edits in active repos | 0 |
| Runner edits | 0 |

Runner repository check (`/home/ubuntu/Abacus/AEE`):

| Check | Status |
|-------|--------|
| Branch unchanged | OK (`master`) |
| HEAD unchanged | OK (`ba11709`) |
| Staged files | 0 |
| Stash list | 0 |

**Classification:** `PASS` for git safety. The only state change in the active repo
is the single review report at `executor_router_claude_adapter_manifest_gate_independent_review.md`,
which is the task's required deliverable.

## Findings

| ID | Severity | File / Symbol / Line | Evidence | Impact | Recommendation | Commit Blocking |
|----|----------|----------------------|----------|--------|----------------|:---------------:|
| F-001 | **BLOCKER** | `aee/adapters/manifest_verifier.py:266-278` | Verifier checks `executor.terminal_reason`, `executor.subtype`, `executor.is_error` but does not read top-level `manifest.status` or `manifest.verdict`. | A manifest with `status="CANCELLED", verdict="FAIL"` and executor block `terminal_reason=completed, subtype=success, is_error=false` is accepted as `verified=True`. Probe A empirically demonstrates this. | Add `E_STATUS_NOT_COMPLETED` and `E_VERDICT_NOT_PASS` error codes; require `manifest_obj.get("status") == "COMPLETED"` and `manifest_obj.get("verdict") == "PASS"`. | YES |
| F-002 | **BLOCKER** | `aee/core/registry.py:134-150` (`_register_aee7_defaults`) | Silent fallback: if `ClaudeCodeExecutorAdapter` import fails, legacy `ClaudeCodeRuntimeAdapter` is registered under the same `claude_code` key. | An explicit `metadata.executor="claude_code"` request could route to the legacy shim, which has no `completion.verified.json` gate. Watcher gate catches the resulting completion, but the route is still contractually wrong. | Either remove the silent fallback (raise on import failure, leaving `claude_code` unregistered → router raises `ExecutorUnavailable` → 503), or tag the legacy shim's registration with `_manifest_capable=False` and have the router refuse it for explicit requests. | YES |
| F-003 | **HIGH** | `aee/adapters/manifest_verifier.py:329` (artifacts lookup) vs `scripts/claude_code_runner.py:1178` (artifacts placement) | Verifier reads `verification.get("artifacts")`; runner writes `artifacts` at top level. | With the current Runner, every required artifact will be flagged as missing, so the verifier will return `verified=False` for any task that declares `required_artifacts`. The system fails-closed by accident, not by design. | Reconcile the schema. Either move artifacts in the Runner to `verification.artifacts` or read `manifest.artifacts` (with `verification.artifacts` as fallback) in the verifier. | NO (fails closed, but contract gap) |
| F-004 | **HIGH** | `aee/adapters/claude_code_executor.py:32,104` (semaphore) | `_sem = asyncio.Semaphore(_CONCURRENCY)` is created at module scope but never acquired or released. The actual guard is an integer count of `_inflight` entries. The docstring at line 32 claims the semaphore is held; this is false. | Misleading documentation, dead code. Functional behavior is correct for the "one run at a time" MVP rule. | Either remove the semaphore and update the docstring, or wire it up (`async with _sem:` around the active-count check + launch path; release in `poll()` and `cancel()` terminal paths). | NO |
| F-005 | **HIGH** | `aee/adapters/claude_code_executor.py:268` (`_inflight` insertion) | `_inflight[run_id] = run` is never followed by a removal. The dict grows without bound. | Slow memory leak for long-lived processes. Functional behavior unaffected because the active-count check filters on `r.process.poll() is None`. | Remove the entry in `poll()` and `cancel()` after the terminal path completes (success, fail, or cancel). | NO |
| F-006 | **HIGH** | `tests/test_claude_executor_integration.py:380-381` (`inspect.getsource`) | `test_e2e_hermes_path_bypasses_gate` uses `inspect.getsource(watcher.Watcher._poll_one)` and asserts that the source contains the strings `"claude_code"` and `"_claude_code_completion_gate"`. | Source-string inspection is not behavioral verification. A refactor that moves the gate elsewhere (or renames it) would not be caught. | Replace with a behavioral test: drive `await watcher._poll_one(t, external_id)` with a `FakeManager` and a fake adapter that returns spoofed raw; assert `manager.fail()` is called. | NO |
| F-007 | **MEDIUM** | `aee/adapters/claude_code_executor.py:223-232` (child env) | The allow-list does not include `ANTHROPIC_API_KEY` or any other auth variable the Claude CLI may need. | The MVP cannot actually run the Claude CLI end-to-end without an additional env pass-through. Production deployment needs this. | Add a documented pass-through list for the variables the Claude CLI actually needs (at minimum `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or whatever the CLI reads). For the MVP (which uses a fake Runner), this is documented as a remaining risk. | NO |
| F-008 | **MEDIUM** | `dispatcher/watcher.py:53-86` (`_claude_code_completion_gate`) | Watcher trusts `raw["verification"]["verified"]` and a non-empty `raw["verified_manifest"]` path. Does not re-verify the manifest from disk. | A non-Claude adapter (or a buggy/malicious one) that sets `t.adapter_name="claude_code"` and returns a spoofed `{"verification": {"verified": true}}` payload would pass the watcher gate. The Hermes control plane currently does not let third-party code register new adapters without code review, but this is a defense-in-depth gap. | Either re-run `verify_completion_manifest` against `raw["verified_manifest"]` from disk in the watcher, or document explicitly that the watcher's gate is a lightweight check that relies on the adapter to be honest. | NO |
| F-009 | **MEDIUM** | `openapi.yaml` | The `CreateRunRequest` schema does not include the `metadata` field. | Strict OpenAPI clients would reject or strip `metadata`. The implementation report acknowledges this as a non-blocking caveat. | Regenerate the OpenAPI YAML from the Pydantic model (or hand-edit it to add `metadata: object, nullable: true, additionalProperties: true`). | NO |
| F-010 | **LOW** | `aee/adapters/manifest_verifier.py` (manifest `verified` field check) | The verifier does not check that the manifest's own `verified` field is `true` (it computes its own). | Defensive redundancy missing. If the runner self-reports `verified=true` but the verifier finds errors, the watcher's lightweight gate (F-008) would not catch the discrepancy because it trusts `raw["verification"]["verified"]`. | Document the contract: the verifier is the authority; the runner's self-reported `verified` field is informational. | NO |
| F-011 | **LOW** | `aee/adapters/manifest_verifier.py:329` (artifact dedup) | If the same artifact path appears twice in `verification.artifacts`, the dict comprehension `artifacts_by_path[entry["path"]] = entry` silently keeps the last one. | A Runner that accidentally lists the same artifact twice (with different hashes) would have only the last hash checked. | Add a duplicate-detection check (`if path in artifacts_by_path: errors.append(E_DUPLICATE_ARTIFACT)`). | NO |
| F-012 | **NOTE** | `executor_router_claude_adapter_manifest_gate_mvp_report.md` (Verified Manifest Gate table) | The report's check table mentions `executor.terminal_reason == "completed"` but does not list top-level `status` or `verdict`. | A casual reader of the report would believe the verifier is complete. | Update the report to list top-level `status` and `verdict` checks, even as "NOT IMPLEMENTED", with a TODO marker. | NO |
| F-013 | **NOTE** | `executor_router_claude_adapter_manifest_gate_mvp_report.md` (Broad Regression count) | The report claims 258/260 pass; actual is 229+3+1 (or 253+3+1 with migration). | The headline number is misleading. | Re-run the regression and update the number. | NO |
| F-014 | **NOTE** | `/home/ubuntu/hermes-runtime-bridge/.git` (pre-existing stash) | `stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery` is from a different task. | Not an MVP concern. | (None — out of scope for this review.) | NO |

## Remaining Risks

1. **Manifest schema drift.** The Runner (`ba11709`) and the Verifier
   (`4c50b452` + new code) disagree on where the artifacts list lives. A future
   Runner change that moves the artifacts back to `verification.artifacts` would
   silently re-open the contract.
2. **Silent registry fallback.** Even if the new module imports successfully today,
   a future `__init__.py` change that adds an import-time error (e.g. a missing
   optional dependency) would silently degrade verification guarantees.
3. **Concurrency dead code.** The semaphore-vs-counter mismatch is a maintenance
   hazard. A future refactor that adds `async with _sem:` without removing the
   counter would double-gate (correct, but confusing) or break the guard
   (incorrect).
4. **Watcher gate relies on adapter honesty.** A non-Claude adapter that sets
   `t.adapter_name="claude_code"` and returns spoofed raw data would slip past the
   gate. The Hermes control plane does not allow ad-hoc adapter registration, so
   this is a defense-in-depth gap rather than an active vulnerability.
5. **OpenAPI stale.** GPT Action clients that strictly validate the OpenAPI schema
   will not be able to send `metadata` until the YAML is regenerated.
6. **Auth env pass-through.** The MVP cannot actually run the Claude CLI
   end-to-end; production deployment needs an explicit pass-through for the
   variables the CLI reads.
7. **Pre-existing baseline failures.** 3 dispatcher tests + 1 migration test fail
   on the baseline. These are out of scope for this MVP but should be tracked for a
   separate fix.

## Review Ready

`YES` — this review report is complete, source-evidenced, and reproducible.

## Commit Ready

`NO`.

Per the task's "Commit Ready" rules, all of the following must be true:

| Rule | Status | Note |
|------|--------|------|
| No blocker | ❌ | F-001 and F-002 are blockers. |
| Registry cannot silently bypass manifest verification | ❌ | F-002. |
| Manifest status and verdict are enforced | ❌ | F-001. |
| Watcher cannot be trivially spoofed | ⚠️ | F-008 (defense-in-depth gap, not blocking per task's MVP-allowable "lightweight" classification, but not FULL SECOND VERIFICATION either). |
| Cancel semantics preserve Runner finalization | ✅ | SIGTERM → grace → SIGKILL works (Probe B). Probe A demonstrated a verifier gap, not a cancel gap. |
| Targeted tests pass | ✅ | 51/51 pass. |
| Integration test is behaviorally adequate | ❌ | F-006 (uses `inspect.getsource`). |
| Baseline-only failures are independently reproduced | ✅ | 3+1 reproduced under `/tmp/hermes-runtime-baseline-20260714T153148Z`. |
| Stash caused no observed damage | ✅ | Pre-existing stash is from a different task. |
| Implementation report exists and is verified | ✅ | Present, with minor accuracy gaps (F-012, F-013). |
| Staged files remain empty | ✅ | 0 staged files. |

## Commit Performed

`NO`. The MVP cannot be committed as-is; the blockers (F-001, F-002) must be fixed
first.

## Production Safety

| Rule | Status |
|------|--------|
| No source edit | ✅ (this report is the only file added) |
| No test edit | ✅ |
| No Runner edit | ✅ |
| No database migration | ✅ |
| No OpenAPI edit | ✅ |
| No gateway edit | ✅ |
| No supervisor edit | ✅ |
| No authentication change | ✅ |
| No package installation | ✅ |
| No stage | ✅ |
| No commit | ✅ |
| No push | ✅ |
| No deploy | ✅ |
| No restart | ✅ |
| No merge | ✅ |
| No rebase | ✅ |
| No stash | ✅ |
| No reset | ✅ |
| No restore | ✅ |
| No checkout | ✅ |
| No destructive cleanup outside review-created processes | ✅ (only `/tmp/cancel-probe*` and `/tmp/hermes-runtime-baseline-20260714T153148Z` were created; they are under `/tmp` and the task permits this) |

## Final Recommendation

**FOCUSED FIXES.**

The MVP is structurally close to the contract. The router, the adapter subprocess
semantics, the cancel/escalation path, the OpenAPI caveat, the env handling, the
artifact-by-path scheme, the task ID normalization, the file scope, and the targeted
tests are all correct. The two blockers (F-001, F-002) and the related HIGH issues
(F-003 manifest schema drift, F-004 dead semaphore, F-005 inflight leak, F-006
behavioral test gap) can be addressed by a small, focused patch that:

1. Adds `E_STATUS_NOT_COMPLETED` and `E_VERDICT_NOT_PASS` to the verifier and
   enforces them after the schema version check.
2. Removes the silent registry fallback (or makes it explicit + observed).
3. Reconciles the artifact schema between the Runner and the Verifier (either fix
   the Runner or extend the Verifier's lookup to fall back to `manifest.artifacts`).
4. Updates the docstring at `claude_code_executor.py:32` to match the actual
   integer-count guard, or wires up the semaphore and cleans up `_inflight` on
   terminal paths.
5. Replaces the `inspect.getsource` integration test with a behavioral
   `await watcher._poll_one(t, external_id)` test.

After these fixes, the implementation should be re-reviewed (this report's
"Commit Ready" would flip to `YES`). The pre-existing baseline failures are out of
scope and should be addressed by a separate fix.

---

## Artifact Verification

This report:

```bash
ls -la executor_router_claude_adapter_manifest_gate_independent_review.md
wc -l executor_router_claude_adapter_manifest_gate_independent_review.md
sha256sum executor_router_claude_adapter_manifest_gate_independent_review.md
```

On-disk values are returned in the "Final Response" section below (computed once,
after the report is fully written and closed).
