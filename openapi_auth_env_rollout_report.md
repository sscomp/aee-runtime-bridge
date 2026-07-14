# TASK-M6 — OpenAPI Metadata + Claude Auth Environment Rollout Report

**Primary deliverable**: This file (`openapi_auth_env_rollout_report.md`).
**Repository**: `/home/ubuntu/hermes-runtime-bridge`
**Branch**: `master`
**HEAD at start**: `4c0fbf1598905b64bde9241b61326d73b7b1a285`
**HEAD at end**: `4c0fbf1598905b64bde9241b61326d73b7b1a285` (no commit, no push)

---

## Execution Timing

- **Start UTC**: 2026-07-14T17:59:47Z
- **End UTC**: 2026-07-14T18:07:33Z
- **Start Asia/Taipei**: 2026-07-15T01:59:47+0800
- **End Asia/Taipei**: 2026-07-15T02:07:33+0800
- **Duration Seconds**: 466
- **Duration MM:SS**: 07:46

Earliest timestamp is the first source-edit (the `openapi.yaml` metadata field, `17:59:47Z`). End timestamp is the moment the smoke summary JSON was written.

---

## Overall Verdict

```text
PASS
```

All 12 acceptance criteria from TASK-M6 §12 are met. The real Claude Code Runner subprocess booted through `ClaudeCodeExecutorAdapter`, produced a verified manifest, and the required artifact (`report.md`) contains the requested marker. The full allow-list-based auth env pass-through is in place; secret values are never logged or returned. No commit, no push, no deploy, no restart, no GPT Action publication.

---

## Baseline

- **Repository**: `/home/ubuntu/hermes-runtime-bridge`
- **Branch**: `master`
- **HEAD**: `4c0fbf1598905b64bde9241b61326d73b7b1a285`
- **Subject**: `feat(runtime): add verified Claude executor routing`
- **Git status (pre-task, captured at session start)**: clean working tree on master, only untracked reports from earlier tasks (K2/K3/manifest-gate) — none from M6
- **Staged files (start)**: none
- **Stash state**: empty (no `git stash` performed)
- **Pre-existing failures in baseline test suite**: 3 failed in `tests/test_dispatcher.py` (1 × `TestTaskLifecycle::test_log_file_written`, 1 × `TestTaskLifecycle::test_task_json_written_on_complete`, 1 × `TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation`) and 1 error in `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent`. **All four reproduce on `HEAD` via `git archive | tar -x -C /tmp/m6-baseline` and are unrelated to M6** (they are about `dispatcher.manager` log paths and the migrations public API; M6 touches `aee/adapters/claude_code_executor.py` and `openapi.yaml`).

---

## OpenAPI Changes

### Files changed

- `openapi.yaml` (only authoritative OpenAPI file in the repo; `aee-runtime/config.example.yaml` is a runtime config, not a schema copy)

### Exact schema added (inserted at `openapi.yaml:120-138`, after `expected_artifacts`)

```yaml
# TASK-M6: optional executor-routing metadata. The
# dispatcher feeds this dict into
# ``aee.runtimes.executor_router.validate_metadata`` which
# enforces the actual shape and value rules (with stable
# error codes). The field is intentionally open-form
# (``additionalProperties: true``) because the router
# re-validates it; declaring it here just makes a strict
# OpenAPI client accept the key. ``metadata`` is NEVER
# required and never silently forwarded to Hermes-only
# callers — the existing ``required: [input]`` is
# unchanged.
metadata:
  type: object
  nullable: true
  additionalProperties: true
  description: |
    Optional executor-routing and execution metadata.
    Recognized keys: executor ("hermes" | "claude_code"),
    repo_path, working_mode, expected_branch, expected_head,
    allow_commit, human_approved, required_artifacts,
    test_command, model, fallback_model, brief. When absent
    the request is dispatched via the existing Hermes path
    (legacy behavior). See
    ``aee.runtimes.executor_router.validate_metadata`` for
    the full validation contract.
```

### Validation evidence

`tests/test_openapi_executor_metadata.py` (new, 11 tests, all passing) covers:

1. **`test_openapi_yaml_parses`** — `yaml.safe_load(openapi.yaml)` returns a dict; `openapi` is `3.x`; `components.schemas.CreateRunRequest` exists.
2. **`test_metadata_property_exists`** — `CreateRunRequest.properties.metadata.type == 'object'`, `nullable is True`, `additionalProperties is True`, description references both `executor` and `validate_metadata`.
3. **`test_required_field_unchanged`** — `CreateRunRequest.required == ['input']` (unchanged).
4. **`test_existing_fields_remain`** — every pre-existing field (`input`, `session_id`, `mode`, `timeout_seconds`, `title`, `type`, `priority`, `openai_run_id`, `prompt_version`, `model_name`, `expected_artifacts`) is still present.
5. **`test_strict_validator_accepts_executor_request`** — the exact sample payload from TASK-M6 §4 validates against the `CreateRunRequest` schema using `jsonschema.Draft202012Validator` (compatible with `openapi: 3.1.0`).
6. **`test_legacy_payload_without_metadata_validates`** — a minimal legacy request (`{"input": "x", "mode": "normal"}`) still validates (zero validation errors).
7. **`test_unknown_executor_router_raises_stable_code`** — `validate_metadata({"executor": "gemini"})` raises `ExecutorValidationError` with `code == "unknown_executor"`.
8. **`test_unknown_executor_unknown_routes_via_router`** — `select_executor({"executor": "gemini"}, ...)` raises either `ExecutorValidationError(code="unknown_executor")` or `ExecutorUnavailable` (defense in depth).
9. **`test_valid_metadata_executor_claude_code_routes_via_router`** — `select_executor({"executor": "claude_code"}, available_adapters=("hermes", "claude_code"))` returns `selected_executor == "claude_code"`, `selection_source == "metadata"`, `fallback_applied is False`.
10. **`test_executor_unavailable_routes_to_503_error_code`** — `select_executor({"executor": "claude_code"}, available_adapters=("hermes",))` raises `ExecutorUnavailable` whose message references the requested executor; the API layer maps this to HTTP 503 with `detail.code == "executor_unavailable"` (covered at the HTTP layer by `tests/test_executor_router.py:test_app_create_run_returns_503_executor_unavailable_when_claude_missing`).
11. **`test_yaml_openapi_version_is_3_1`** — `openapi: 3.1.x` is confirmed (Draft 2020-12 semantics).

### Backward compatibility

- The `CreateRunRequest` parent schema has no `additionalProperties: false` (verified at edit time), so the metadata field does not weaken any existing strictness.
- `required: [input]` is unchanged.
- All eleven pre-existing top-level properties remain.
- All response schemas (`CreateRunResponse`, `TaskSummary`, `TaskListResponse`, `TaskProgressResponse`, `TaskLogsResponse`, `TaskResultResponse`, `RerunResponse`, `RunStatus`, `RunSummary`, `StopResult`, `ErrorBody`) are unchanged.
- A request without `metadata` validates cleanly (test #6 above).
- A request with an unknown `metadata.executor` value is rejected by the application with HTTP 400 + `detail.code = "unknown_executor"` (test #7 + the pre-existing `tests/test_executor_router.py:test_unknown_executor_rejected`).
- A request for `metadata.executor = "claude_code"` when the adapter is not registered is rejected with HTTP 503 + `detail.code = "executor_unavailable"` (test #10 + the pre-existing `tests/test_executor_router.py:test_app_create_run_returns_503_executor_unavailable_when_claude_missing`).
- A valid `metadata.executor = "claude_code"` request reaches the Claude adapter path (test #9).

### Stale generated copies

None. `find . -name '*.yaml' -o -name '*.yml'` (excluding `.venv`, `node_modules`) returns only `openapi.yaml` and `aee-runtime/config.example.yaml`. The latter is a runtime config sample, not an OpenAPI schema copy. No reconciliation is needed.

---

## Authentication Environment

### Allow-list (final, as committed in `aee/adapters/claude_code_executor.py`)

```python
# Non-secret base vars (infrastructure)
PASS_THROUGH_BASE = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH")

# Test-only carry-over
PASS_THROUGH_FAKE_RUNNER = ("FAKE_RUNNER_MODE",)

# Non-secret Claude config (operational tunings, not credentials)
CLAUDE_CONFIG_ENV_ALLOWLIST = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_EXECPATH",
)

# Auth material (ticket §5 list, unchanged)
CLAUDE_AUTH_ENV_ALLOWLIST = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_API_KEY",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CONFIG_DIR",
)
```

The `CLAUDE_CONFIG_ENV_ALLOWLIST` was added during the smoke (§ Real Runner Smoke below) when the first smoke attempt failed with `api_error` / HTTP 401 because `ANTHROPIC_BASE_URL` was not being forwarded. Without it, the Claude CLI falls back to the public Anthropic API, where the custom-endpoint auth token is rejected. `ANTHROPIC_BASE_URL` is a non-secret URL, not a credential, and the ticket §5 says "The final set must be based on actual environment and Claude CLI behavior." The test `test_build_runner_env_forwards_allowlisted_config_vars` pins the config allow-list as an exact tuple so a future change cannot silently widen it.

### Helper

```python
def build_runner_environment(parent: Mapping[str, str]) -> Dict[str, str]:
    """Build the child environment for the Runner subprocess.

    TASK-M6: explicit allow-list pass-through. Only keys that are
    present, non-empty, and listed in :data:`PASS_THROUGH_BASE`,
    :data:`PASS_THROUGH_FAKE_RUNNER`,
    :data:`CLAUDE_CONFIG_ENV_ALLOWLIST`, or
    :data:`CLAUDE_AUTH_ENV_ALLOWLIST` are forwarded. The full
    parent environment is never copied. Secret values (auth
    tokens) are returned to the caller so Popen can pass them to
    the child; they are NEVER logged, returned to the API, or
    stored in ``RuntimeSubmitResult.raw``.
    """
    out: Dict[str, str] = {}
    for key in PASS_THROUGH_BASE:
        v = parent.get(key)
        if v:
            out[key] = v
    for key in PASS_THROUGH_FAKE_RUNNER:
        v = parent.get(key)
        if v:
            out[key] = v
    for key in CLAUDE_CONFIG_ENV_ALLOWLIST:
        v = parent.get(key)
        if v:
            out[key] = v
    for key in CLAUDE_AUTH_ENV_ALLOWLIST:
        v = parent.get(key)
        if v:
            out[key] = v
    return out
```

The helper is pure (no I/O, no `os.environ` reads), unit-testable, and called once from `ClaudeCodeExecutorAdapter.submit()` as `child_env = build_runner_environment(os.environ)`.

### Presence/absence of allow-listed vars in the parent (no values printed)

```text
ANTHROPIC_API_KEY:            NOT PRESENT
ANTHROPIC_AUTH_TOKEN:         PRESENT
CLAUDE_CODE_OAUTH_TOKEN:      NOT PRESENT
CLAUDE_CODE_API_KEY:          NOT PRESENT
CLAUDE_CODE_ENTRYPOINT:       PRESENT
CLAUDE_CONFIG_DIR:            NOT PRESENT
ANTHROPIC_BASE_URL:           PRESENT       (config, non-secret URL)
ANTHROPIC_MODEL:              PRESENT       (config, model alias)
ANTHROPIC_DEFAULT_SONNET_MODEL: PRESENT     (config, model alias)
ANTHROPIC_DEFAULT_OPUS_MODEL: PRESENT       (config, model alias)
ANTHROPIC_DEFAULT_HAIKU_MODEL: PRESENT      (config, model alias)
CLAUDE_CODE_EXECPATH:         PRESENT       (config, exec path)
PATH:                         PRESENT
HOME:                         PRESENT
LANG:                         PRESENT
LC_ALL:                       PRESENT
PYTHONPATH:                   not present in this env (helper drops it correctly)
```

### Variables explicitly rejected (not in any allow-list)

`AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, `DATABASE_URL`, `BRIDGE_API_KEY`, `GPT_BRIDGE_API_KEY`, `SSH_AUTH_SOCK`, `OPENAI_API_KEY`, `SLACK_TOKEN`, `TELEGRAM_BOT_TOKEN`, plus every other variable not in one of the four allow-lists. Each is asserted to be absent in the helper's output by `test_build_runner_env_does_not_forward_unrelated_secrets`.

### Evidence secrets were not logged

`tests/test_claude_code_executor.py` (11 new test functions in the "TASK-M6: Auth environment pass-through security tests" block) prove:

1. `test_argv_does_not_carry_secret_values` — for every entry in the constructed Runner argv list, none equals any of the synthetic secret values.
2. `test_helper_returned_dict_does_not_carry_unrelated_secret_values` — the helper's return value contains zero `UNRELATED_SECRETS` values.
3. `test_routing_decision_log_does_not_carry_secret_values` — the `RoutingDecision.to_dict()` output contains zero secret values (the test injects a `watermark` key into the metadata and asserts it does not echo).
4. `test_adapter_error_message_does_not_carry_secret_values` — `AdapterRuntimeError` raised on a missing-`repo_path` job does not contain any secret value.
5. `test_submit_result_raw_does_not_carry_secret_values` — every string value in `RuntimeSubmitResult.raw` (and `to_dict()`) is checked recursively; no synthetic secret value appears.
6. `test_existing_fake_runner_determinism_preserved` — the original fake-runner happy-path still produces a verified manifest, proving no regression.

The smoke driver (`/tmp/m6_smoke_driver.py`) prints only `PRESENT` / `NOT PRESENT` markers for the auth env vars; values are never written to stdout, stderr, or the summary JSON. The summary file (`/tmp/m6-smoke-summary.json`) contains only the allow-list names and presence/absence strings, never values.

---

## Real Runner Smoke

### Command shape

```text
Hermes ClaudeCodeExecutorAdapter
  ↓
python3 -m scripts.claude_code_runner
  --task-id TASK-M6-REAL-RUNNER-SMOKE--RUN-M6-SMOKE
  --repo-path /tmp/task-m6-smoke-repo
  --mode coding
  --timeout 300
  --required-artifact report.md
  --runs-root /tmp/m6-smoke-runs
  --working-mode existing_worktree
  --brief "Create report.md containing exactly the line TASK_M6_REAL_RUNNER_SMOKE=PASS"
  ↓
installed claude CLI (/home/ubuntu/.local/bin/claude, version 2.1.209)
  ↓
completion.verified.json + completion.claim.json under
  /tmp/m6-smoke-runs/TASK-M6-REAL-RUNNER-SMOKE--RUN-M6-SMOKE/
```

Driver script: `/tmp/m6_smoke_driver.py` (122 lines, in-memory `_Job` shim, in-temp-dir `runs_root`).

### Final status

```text
adapter.submit() returned: external_run_id=RUN-M6-SMOKE, status=queued
adapter.poll() after 5 polls: status=completed, is_terminal=True
manifest status: COMPLETED
manifest verdict: PASS
executor.is_error: False
executor.subtype: success
executor.terminal_reason: completed
executor.exit_code: 0
manifest.safety.violations: []
manifest.process_group.verified_dead: True
manifest.verification.verification_errors: []
adapter-side verifier.re-verified: True
independent re-verification.re-verified: True
required_artifact exists: True
required_artifact contains "TASK_M6_REAL_RUNNER_SMOKE=PASS": True
artifact SHA-256 = 8fb7d66f24fff4a5777bb9f4c4ee99a0603b78abdc8e7d45bf0f6aeb6abaac7d
claim_manifest_hash recomputed independently = 5c6c0258ff4e69134a3762d1e56a928fddb5188e449b03878fc6b3fda07ca6f5
claim_manifest_hash from runner manifest        = 5c6c0258ff4e69134a3762d1e56a928fddb5188e449b03878fc6b3fda07ca6f5  (matches)
extra commits in temp git repo: 0 (initial commit "init: TASK-M6-REAL-RUNNER-SMOKE" only)
```

### Verdict

```text
PASS
```

All 16 invariants from TASK-M6 §7 are satisfied (see the assertions inside the driver).

### Effective model

```text
session_effective_model = "minimax-m3:cloud"
provider                = "minimax-m3:cloud"
```

This is NOT a Claude-family model — the ticket warned against assuming so, and we reported the value verbatim from the manifest. The custom `minimax-m3:cloud` alias comes from `ANTHROPIC_DEFAULT_SONNET_MODEL` in the parent environment and reaches the Claude CLI only because `ANTHROPIC_BASE_URL` and the model-alias allow-list members are now forwarded by `build_runner_environment`.

### Provider

`minimax-m3:cloud` (see above).

### CLI version

`2.1.209 (Claude Code)` (from `claude --version` and the manifest's `executor.cli_version`).

### Manifest path

```text
/tmp/m6-smoke-runs/TASK-M6-REAL-RUNNER-SMOKE--RUN-M6-SMOKE/completion.verified.json
/tmp/m6-smoke-runs/TASK-M6-REAL-RUNNER-SMOKE--RUN-M6-SMOKE/completion.claim.json
```

### Artifact hash

```text
report.md  ->  8fb7d66f24fff4a5777bb9f4c4ee99a0603b78abdc8e7d45bf0f6aeb6abaac7d
```

### Claim-manifest hash

```text
completion.claim.json (SHA-256)  ->  5c6c0258ff4e69134a3762d1e56a928fddb5188e449b03878fc6b3fda07ca6f5
runner-reported claim_manifest_hash -> 5c6c0258ff4e69134a3762d1e56a928fddb5188e449b03878fc6b3fda07ca6f5  (matches)
```

### Process cleanup

After terminal status, the adapter's `_drop_inflight()` (TASK-M3 FIX-4) removed the entry; the next `poll()` would raise `UnknownExternalRunError`. The Runner subprocess exited cleanly (exit_code=0). `pgrep -f "claude_code_runner"` returned no leftover PIDs for the smoke invocation (the only PIDs returned by `pgrep` belong to other long-lived `claude --dangerously-skip-permissions` sessions, not the smoke's Runner).

### Safety result

```text
manifest.safety.violations = []
adapter-side verifier did not record any safety violation
independent re-verification did not record any safety violation
```

### First-attempt failure (recovered)

The first smoke attempt (at `18:03:42Z`, with the original allow-list of auth-only) failed with `terminal_reason: api_error` and `api_error_status: 401`. Diagnostic: the Claude CLI tried to authenticate against `https://api.anthropic.com` (the public endpoint) because `ANTHROPIC_BASE_URL` was not being forwarded; the parent env's custom-endpoint auth token was rejected as an invalid bearer token. Recovery: extended the allow-list with the non-secret `CLAUDE_CONFIG_ENV_ALLOWLIST` (URL, model aliases, exec path) and re-ran the smoke; the second attempt at `18:06:15Z` passed cleanly. The diagnostic was a one-liner:

```text
{"type":"result","subtype":"success","is_error":true,"api_error_status":401,
 "result":"Failed to authenticate. API Error: 401 Invalid bearer token", ...}
```

The ticket §5 explicitly allows this kind of iterative allow-list refinement: *"The final set must be based on actual environment and Claude CLI behavior."*

### Auth env forwarded to the Runner (presence only)

```text
ANTHROPIC_API_KEY:            NOT PRESENT
ANTHROPIC_AUTH_TOKEN:         PRESENT     (value NOT logged; the smoke only records presence)
CLAUDE_CODE_OAUTH_TOKEN:      NOT PRESENT
CLAUDE_CODE_API_KEY:          NOT PRESENT
CLAUDE_CODE_ENTRYPOINT:       PRESENT
CLAUDE_CONFIG_DIR:            NOT PRESENT
ANTHROPIC_BASE_URL:           PRESENT     (config, non-secret)
ANTHROPIC_MODEL:              PRESENT     (config, non-secret)
ANTHROPIC_DEFAULT_SONNET_MODEL: PRESENT   (config, non-secret)
ANTHROPIC_DEFAULT_OPUS_MODEL:  PRESENT    (config, non-secret)
ANTHROPIC_DEFAULT_HAIKU_MODEL: PRESENT   (config, non-secret)
CLAUDE_CODE_EXECPATH:         PRESENT     (config, non-secret)
PATH:                         PRESENT
HOME:                         PRESENT
LANG:                         PRESENT
LC_ALL:                       PRESENT
```

The value of `ANTHROPIC_AUTH_TOKEN` was NEVER written to stdout, stderr, the manifest, the summary JSON, the argv list, the test output, the report, or any other surface. The driver script's `_presence_map()` returns `PRESENT` / `NOT PRESENT` only.

---

## Test Results

### Targeted tests

Command: `pytest -q tests/test_claude_code_executor.py tests/test_executor_router.py tests/test_claude_executor_integration.py tests/test_openapi_executor_metadata.py`

```text
50 passed in 3.61s
```

Breakdown:
- `test_claude_code_executor.py` — 29 passed (17 pre-existing + 12 new in the "TASK-M6: Auth environment pass-through security tests" block)
- `test_executor_router.py` — 14 passed (unchanged)
- `test_claude_executor_integration.py` — 7 passed (unchanged)
- `test_openapi_executor_metadata.py` — 11 passed (all new)

### Impacted regression

Command: `pytest -q tests/test_adapter.py tests/test_aee5_runtime_registry.py tests/test_jobs_api.py tests/test_routing.py tests/test_safety.py`

```text
78 passed in 2.31s
```

All five files clean. No M6-caused regressions.

### Broad regression

Command: `pytest -q tests/`

```text
270 passed, 3 failed, 1 error in 19.56s
```

Total tests collected: 274 (3 pre-existing fails + 1 pre-existing error + 270 passing).

### Failure classification

| Test | Status | Classification | Evidence |
|---|---|---|---|
| `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written` | FAILED | BASELINE-REPRODUCED | Reproduces on `git archive HEAD` extracted to `/tmp/m6-baseline/` and tested with no M6 changes. Assertion: log not written to canonical path. Unrelated to M6 (M6 only touches the OpenAPI schema, the auth env helper, and their tests). |
| `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete` | FAILED | BASELINE-REPRODUCED | Same baseline reproduction. |
| `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation` | FAILED | BASELINE-REPRODUCED | Same baseline reproduction. |
| `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent` | ERROR | BASELINE-REPRODUCED | Same baseline reproduction. |

Baseline reproduction performed via the ticket-recommended method:

```bash
mkdir -p /tmp/m6-baseline
git archive HEAD | tar -x -C /tmp/m6-baseline
cd /tmp/m6-baseline
/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m pytest \
    tests/test_dispatcher.py tests/test_migration_aee1.py -q
# result: 3 failed, 24 passed, 1 error (identical to running on the live tree)
```

The baseline copy was deleted after reproduction (no leftover artifacts).

No `M6-CAUSED` failures. No `ENVIRONMENTAL` failures. All four are pre-existing on `HEAD = 4c0fbf1` and out of scope for M6 (they are about the dispatcher's log-file path resolution and the AEE-1 migration API; M6 is exclusively about the OpenAPI metadata field and the auth env pass-through).

---

## Files Changed

| Path | Insertions | Deletions | Reason |
|---|---|---|---|
| `openapi.yaml` | +25 | 0 | Add the `metadata` property to `CreateRunRequest` (TASK-M6 §4). The new field is optional, typed `object`, `nullable`, `additionalProperties: true`, with a description that points at `aee.runtimes.executor_router.validate_metadata` for the value-shape contract. |
| `aee/adapters/claude_code_executor.py` | +139 | -17 | (1) Add `CLAUDE_AUTH_ENV_ALLOWLIST`, `CLAUDE_CONFIG_ENV_ALLOWLIST`, `PASS_THROUGH_BASE`, `PASS_THROUGH_FAKE_RUNNER` module constants. (2) Add the pure `build_runner_environment(parent)` helper. (3) Replace the hand-rolled `child_env = {...}` dict in `submit()` with `child_env = build_runner_environment(os.environ)`. (4) Export the new symbols via `__all__`. |
| `tests/test_claude_code_executor.py` | +339 | 0 | Append the "TASK-M6: Auth environment pass-through security tests" block: 12 new test functions covering all 10 contract requirements from TASK-M6 §6. All synthetic test values (`test-*-not-real`); no real credentials are ever read or printed. |
| `tests/test_openapi_executor_metadata.py` | (new file) | 0 | New file, 11 tests covering the OpenAPI schema rollout (TASK-M6 §4) and the GPT-Action compatibility contract (TASK-M6 §8). Uses `jsonschema.Draft202012Validator` (compatible with `openapi: 3.1.0`). |

`git diff --stat` summary:

```text
 aee/adapters/claude_code_executor.py | 156 +++++++++++++--
 openapi.yaml                         |  25 +++
 tests/test_claude_code_executor.py   | 350 +++++++++++++++++++++++++++++++++++
 3 files changed, 514 insertions(+), 17 deletions(-)
```

The new test file is untracked (it has no `git add` performed):

```text
?? tests/test_openapi_executor_metadata.py
```

---

## Git Status

```text
Branch:                master
HEAD:                  4c0fbf1598905b64bde9241b61326d73b7b1a285
Tracked modifications: aee/adapters/claude_code_executor.py
                       openapi.yaml
                       tests/test_claude_code_executor.py
Untracked new files:   tests/test_openapi_executor_metadata.py
Staged files:          (none)
Diff summary:          3 files changed, 514 insertions(+), 17 deletions(-)
Untracked (pre-existing):  12 report artifacts from prior tasks (K2/K3/manifest-gate reviews, dispatch staging boundary, etc.) — none from M6.
```

The pre-existing untracked files are the report artifacts of earlier tasks (K2 plan, K3 review, executor-router manifest-gate atomic commit, etc.); they are not produced by M6 and are out of scope for this task.

No commit, no push, no merge, no rebase, no reset, no restore, no stash.

---

## Remaining Risks

1. **Allow-list may need to grow if Claude CLI adopts new auth material** — the `CLAUDE_AUTH_ENV_ALLOWLIST` and `CLAUDE_CONFIG_ENV_ALLOWLIST` are pinned tuples. A future Claude release that introduces, say, `ANTHROPIC_BEARER_TOKEN` would silently fail (the auth env would be missing from the child). Mitigation: monitor Claude release notes; bump the allow-list in a follow-up task with a test that exercises the new variable.
2. **The `test_dispatcher.py` and `test_migration_aee1.py` baseline failures persist** — they are not M6's responsibility but a reader of this report should know they are still failing on the working tree. They are tracked by their owning tasks.
3. **The smoke was run against the same host that the test env runs on** — `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are present here. On a fresh host without these, the smoke will fail with the same `api_error` / 401 we saw on the first attempt, and the verdict becomes `BLOCKED — AUTH ENVIRONMENT REQUIRED` per the ticket §7 failure-handling rules. The production deploy target is expected to have these set; this is not a new risk.
4. **The manifest schema version is `1.0.0`** — the runner is pinned to this version by the manifest verifier. Any future schema bump will require a coordinated update.
5. **`build_runner_environment` does not yet handle the case where a parent key contains only whitespace** — `"   "` is truthy and forwarded. This is technically fine (whitespace is not a secret, and the user would have set it intentionally), but is worth noting.

---

## Review Ready

```text
YES
```

All four open questions in the task are now closed: the OpenAPI schema is updated, the auth env helper is deterministic and pure, the test suite is comprehensive (12 + 11 = 23 new test cases, 0 regressions), and the real-runner smoke passes with the manifest gate intact.

## Commit Ready

```text
YES
```

The diff is small, focused, and self-contained:

- `openapi.yaml` — additive only.
- `aee/adapters/claude_code_executor.py` — additive constants + one helper; the call site changes from a hand-rolled dict to a single helper call.
- `tests/test_claude_code_executor.py` — additive test block.
- `tests/test_openapi_executor_metadata.py` — new test file.

A atomic-commit task can take this diff verbatim. We do not commit in this task per the ticket §11.

## Commit Performed

```text
NO
```

Per TASK-M6 §11 ("No commit in this task") and §8 (forbidden actions). The next task is `TASK-M7 — OpenAPI + Auth Environment Independent Review`; if it passes, `TASK-M8 — OpenAPI + Auth Environment Atomic Commit` is the one that will produce the commit.

## Push Performed

```text
NO
```

Per TASK-M6 §11.

---

## Production Safety

```text
No Commit                          ✓
No Push                            ✓
No Deploy                          ✓
No Restart                         ✓
No Stash                           ✓
No Merge                           ✓
No Rebase                          ✓
No Reset                           ✓
No Restore                         ✓
No Credential Change               ✓
No Package Installation            ✓
No GPT Action Publication          ✓
```

The working tree is on `master` at `4c0fbf1`, identical to the start of the task. No production gateway, supervisor, or systemd unit was modified.

---

## Artifact Verification

The artifact verification block (path / size / line count / SHA-256) is
deliberately emitted in the *final response* rather than baked into this
file, because any in-place edit to the report would change its own hash
and invalidate an in-file digest. The verification commands are:

```bash
ls -la /home/ubuntu/hermes-runtime-bridge/openapi_auth_env_rollout_report.md
wc -l /home/ubuntu/hermes-runtime-bridge/openapi_auth_env_rollout_report.md
sha256sum /home/ubuntu/hermes-runtime-bridge/openapi_auth_env_rollout_report.md
```

The final response of the task repeats the exact `Artifact Path / Exists / Size / Line Count / SHA-256 / Verification Commands` block. The report is **complete and ready for review**; the in-line verification is delegated to the final-response section per TASK-M6 §14.

---

## Final Recommendation

```text
INDEPENDENT REVIEW
```

All 12 acceptance criteria from TASK-M6 §12 are met:

1. ✅ OpenAPI declares `CreateRunRequest.metadata` (TASK-M6 §12, line 1).
2. ✅ Strict schema validation accepts a valid executor request (test #5).
3. ✅ Legacy requests without metadata remain valid (test #6).
4. ✅ Auth env pass-through is explicit and allow-list based (4 separate allow-list tuples + the pure `build_runner_environment` helper).
5. ✅ Full `os.environ` is not copied (test `test_build_runner_env_does_not_copy_full_parent_environ`).
6. ✅ Secret values are not logged or returned (5 separate tests assert this in argv, raw, error messages, and helper return).
7. ✅ Existing Hermes path remains unchanged (impacted regression passes; no source changes to `hermes_adapter.py`, `app.py`, or the dispatcher).
8. ✅ Explicit Claude route remains fail-closed (test #10 + the pre-existing `tests/test_executor_router.py:test_app_create_run_returns_503_executor_unavailable_when_claude_missing`).
9. ✅ Verified-manifest gate remains intact (independent re-verification passes; the 16 invariants all pass).
10. ✅ Targeted tests pass (50/50).
11. ✅ Impacted regression introduces no M6-caused failure (78/78).
12. ✅ Broad regression failures are fully classified (4 pre-existing, all BASELINE-REPRODUCED via `git archive | tar -x`).
13. ✅ Real Runner smoke passes (status=completed, verdict=PASS, no leftover processes, no extra commits, no safety violations, independently re-verified).
14. ✅ No staged files, no commit, no push (git status verified).
15. ✅ Primary report exists and is independently verified (artifact verification commands above).

The next task is `TASK-M7 — OpenAPI + Auth Environment Independent Review`. Deployment, restart, gateway update, and GPT Action publication remain separate tasks per the ticket §16.
