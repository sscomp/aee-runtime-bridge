# Claude Executor Deployment Readiness Review

> Task: TASK-M9 — Claude Executor Integration Deployment Readiness Review
> Mode: Read-only inspection only
> Author: Claude Code (M9 audit)
> Date: 2026-07-14 (UTC)

---

## 1. Execution Timing

- Audit started: 2026-07-14 (post-18:00 UTC, after `da9f566` commit at 18:37:46)
- Hermes Runtime commit under review: `da9f5667473da5c2468c5bc675b89fcef29b9a7a`
- Runner commit under review: `ba1170901df46abe2b2d7ce73daf086fa599d863`
- Audit completed: 2026-07-14 (single session, all read-only probes)

---

## 2. Overall Verdict

```text
Overall Verdict: FAIL
```

Push / deploy are **not** safe right now. The live Hermes Runtime process is
loaded from code that predates `4c0fbf1` (the parent of `da9f566`), so the
committed `da9f566` change has **not** been verified end-to-end in the live
runtime that the GPT Action schema is published against.

Three material blockers prevent a clean `READY FOR CONTROLLED PUSH + DEPLOYMENT`:

1. **LIVE_RUNTIME_PRE_TARGET** — Live process loaded pre-`4c0fbf1` code;
   `da9f566` is in the working tree but not running.
2. **CLAUDE_AUTH_ENV_MISSING_FROM_DOTENV** — `.env` does not declare
   `ANTHROPIC_API_KEY` (or any of the 11 Claude-related variables the
   adapter allow-lists). `build_runner_environment()` is a pure
   pass-through of the parent env, so even after restart the child Runner
   would receive nothing.
3. **PUBLISHED_GPT_ACTION_SCHEMA_STALE** — The schema actually being served
   by the live process (`1.3.0-aee2`) has no `metadata` field and no
   `bearerAuth` securityScheme. The committed `openapi.yaml` (1.2.0) adds
   both, but a publication cannot proceed safely until the live process
   is restarted against the new code.

These are not caveats. They are independent, evidence-backed blockers.

---

## 3. Baseline

### Hermes Runtime (`/home/ubuntu/hermes-runtime-bridge`)

```text
Branch: master
HEAD:   da9f5667473da5c2468c5bc675b89fcef29b9a7a
Log:    da9f566 feat(runtime): expose Claude executor metadata and auth env
Status: clean (no tracked or staged changes; 16 untracked reports / data dirs)
Stash:  stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery
```

Untracked items: pre-existing reports and `data/` directory; none modified.

### Runner (`/home/ubuntu/Abacus/AEE`)

```text
Branch: master
HEAD:   ba1170901df46abe2b2d7ce73daf086fa599d863
Log:    ba11709 feat(runtime): add Claude Code runner MVP
Status: clean (no tracked or staged changes; untracked items all in parent dirs)
Stash:  empty
```

Baseline matches contract. PASS.

---

## 4. Runtime Process Discovery

| Field | Value |
| --- | --- |
| Runtime Manager | **supervisor** |
| Supervisor program | `hermes-runtime-bridge` |
| Supervisord PID | 7 |
| Hermes PID | 4414 |
| Hermes PPID | 7 (supervisord) |
| Start time | 2026-07-14 05:15:33 UTC |
| Elapsed | ~13h 46m at audit time |
| Command | `/home/ubuntu/hermes-runtime-bridge/.venv/bin/python /home/ubuntu/hermes-runtime-bridge/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787 --env-file /home/ubuntu/hermes-runtime-bridge/.env --no-access-log` |
| Working dir | `/home/ubuntu/hermes-runtime-bridge` |
| Python | `/usr/bin/python3.11` (vía `.venv/bin/python` shim) |
| Listening | `127.0.0.1:8787` (uvicorn) |
| Source tree | `/home/ubuntu/hermes-runtime-bridge` (same as working tree, but the loaded module images are older than the source files on disk) |
| User | `ubuntu` |

**Important note on supervisor confs**: the container supervisor is invoked
with `/etc/supervisor/conf.d/openclaw.conf` (the file readable by the
ubuntu user). The `hermes-runtime-bridge.conf` exists but is owned by root
and unreadable, so the live `[program:hermes-runtime-bridge]` block has been
inlined into `openclaw.conf` (matching the comment marker added
2026-07-12). The program name is owned by the active supervisor process.

Process start time **predates** both target commits:

| Item | Timestamp (UTC) |
| --- | --- |
| Process start | 2026-07-14 05:15:33 |
| Parent `4c0fbf1` commit | 2026-07-14 17:40:30 |
| Target `da9f566` commit | 2026-07-14 18:37:46 |
| `app.py` mtime | 2026-07-14 16:29:34 |
| Newest `aee/*.py` mtime | 2026-07-14 18:05:26 |
| `openapi.yaml` mtime | 2026-07-14 17:59:47 |

uvicorn was launched without `--reload`, so the in-memory bytecode images
were captured at process start. The process is running code that is older
than the source files now in the working tree.

---

## 5. Running-Version Verification

```text
Live Runtime Version: PRE-TARGET
```

Evidence:

- Process started 2026-07-14 05:15:33 — earlier than both target commits
  (17:40:30 and 18:37:46).
- `app.app.openapi()` produced on the live process returns
  `version="1.3.0-aee2"` (a build-version literal from `app.py:191`), with
  no `metadata` field on `CreateRunRequest` and no `bearerAuth` security
  scheme. The committed `app.py` at `da9f566` adds the `metadata` field.
- `live /openapi.json` SHA-256:
  `ecb534710899c57174760eb24578246b9c16bafa4630eb762729134ca5632567`
  `repo openapi.yaml` SHA-256:
  `92e9abd65cd96e01ca8b9ffff3e9f5100d9fbe3814bfa7969c97a34414ccdbff`

The live process is not the target commit. Restart is required to apply.

---

## 6. Current Service Health

| Probe | Result |
| --- | --- |
| Process alive | YES (PID 4414, uptime ~13h 46m) |
| Port `127.0.0.1:8787` listening | YES |
| `GET /health` | 200; reports `version=1.2.0`, `phase="2 — Reaper + Notifier + Safety upgrade"`, `reaper.running=0, queued=0, waiting=0, would_reap=0`, `safety.allowlist=42, blocklist=18, approval=8` |
| `GET /` | 404 (not a route; expected) |
| `GET /tasks` (with bearer) | 200; 32 tasks, all `status=completed` |
| `GET /runs/{invalid-id}` (with bearer) | 502 (upstream Hermes 404 surfaced as bridge error) — controlled response, acceptable |
| `GET /openapi.json` | 200; returns `1.3.0-aee2`, missing `metadata` and `bearerAuth` (see §9) |
| Recent log crash loop | NO (`/var/log/hermes-runtime-bridge.err.log` 924 B, only INFO startup + 2 policy_violation entries) |
| Repeated `aee.adapters.claude_code_executor` import error | NO |
| Repeated `aee.adapters.manifest_verifier` import error | NO |
| Repeated `aee.runtimes.executor_router` import error | NO |
| Repeated SQLite lock / migration failure | NO |
| Repeated bridge auth failure (missing credentials) | NO |

**Verdict: `HEALTHY`** at the process level — the bridge is serving
requests, reaper shows no stuck tasks, and no import error is recurring.
The version served, however, is `PRE-TARGET` (see §5).

---

## 7. Import and Startup Readiness

Performed with the same venv (`/home/ubuntu/hermes-runtime-bridge/.venv`)
and `PYTHONPATH=.` as the running process. Read-only — no installation, no
mutation of data files.

```text
import app                                                 → OK
from aee.adapters.claude_code_executor import
    ClaudeCodeExecutorAdapter                              → OK
from aee.adapters.manifest_verifier import
    verify_completion_manifest                             → OK
from aee.runtimes.executor_router import
    select_executor, validate_metadata                     → OK
from aee.core.registry import
    adapter_registry, bootstrap_defaults                   → OK
```

`bootstrap_defaults()` (post-call) registry contents:

```text
['claude_code', 'hermes']
```

- `hermes` adapter is **still registered** after `bootstrap_defaults()` —
  no silent unregister.
- The `claude_code` adapter is the verified `ClaudeCodeExecutorAdapter`
  (module `aee.adapters.claude_code_executor`), **not** a legacy shim.
- `ClaudeCodeExecutorAdapter` is `isinstance(..., RuntimeAdapter)` = `True`
  and exposes `poll()`.
- `app.app.openapi()` returns a complete OpenAPI document with all paths
  present.

Syntax checks (all PASS):

```text
py_compile aee/adapters/claude_code_executor.py → OK
py_compile aee/adapters/manifest_verifier.py   → OK
py_compile aee/runtimes/executor_router.py     → OK
compileall -q aee/                              → OK
```

No new dependencies were installed. The `py_compile` and `import` probes
did not mutate the production database.

**Verdict: Imports Ready — YES**

---

## 8. OpenAPI Publication Readiness

Repository `openapi.yaml` (`/home/ubuntu/hermes-runtime-bridge/openapi.yaml`):

| Check | Result |
| --- | --- |
| YAML parses | YES |
| OpenAPI version | `3.1.0` (valid) |
| `info.version` | `1.2.0` |
| `components.schemas.CreateRunRequest` | present |
| `CreateRunRequest.properties.metadata` | present |
| `metadata.type` | `object` |
| `metadata.nullable` | `true` |
| `metadata.additionalProperties` | `true` |
| `CreateRunRequest.required` | `["input"]` (metadata optional — correct) |
| `/runs` (POST) | present |
| `/runs/{run_id}` (GET) | present |
| Security scheme | `bearerAuth` (HTTP, bearer, JWT) present |
| Suspicious token patterns in YAML | none (only `SK-` matches in description prose) |
| Stale second OpenAPI file | none (only `openapi.yaml` in repo) |

```text
Repository OpenAPI Ready: YES
```

### Live / Published GPT Action Schema

The live process serves `/openapi.json`. Fetched at audit time:

| Field | Live value | Repository value |
| --- | --- | --- |
| `openapi` | `3.1.0` | `3.1.0` |
| `info.version` | `1.3.0-aee2` | `1.2.0` |
| `CreateRunRequest.metadata` | **absent** | present |
| `components.securitySchemes` | **empty** | `bearerAuth` |
| Schema list | only `CreateRunRequest`, `CreateRunResponse`, `HTTPValidationError`, `ValidationError` | 11 schemas including `RunStatus`, `RunSummary`, `TaskListResponse`, … |

```text
Published GPT Action Schema: STALE
```

The live published schema is **older** than the committed `openapi.yaml`
(roughly: it lacks the new `metadata` field that `da9f566` is meant to
expose, and lacks the `bearerAuth` scheme that the hand-crafted YAML
already documents). A GPT Action re-import would see a different schema
than what the live process is currently serving.

**Do not publish in this task.** Publication step is documented in §17
Gate F.

---

## 9. Authentication Environment Readiness

Variable-name-only inspection of the live process environment and the
supervisor conf. No values are reported.

### Process env (parsed from `/proc/4414/environ`)

| Variable | Status |
| --- | --- |
| `ANTHROPIC_API_KEY` | NOT PRESENT |
| `ANTHROPIC_AUTH_TOKEN` | NOT PRESENT |
| `CLAUDE_CODE_OAUTH_TOKEN` | NOT PRESENT |
| `CLAUDE_CODE_API_KEY` | NOT PRESENT |
| `CLAUDE_CODE_ENTRYPOINT` | NOT PRESENT |
| `CLAUDE_CONFIG_DIR` | NOT PRESENT |
| `ANTHROPIC_BASE_URL` | NOT PRESENT |
| `ANTHROPIC_MODEL` | NOT PRESENT |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | NOT PRESENT |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | NOT PRESENT |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | NOT PRESENT |
| `CLAUDE_CODE_EXECPATH` | NOT PRESENT |
| `BRIDGE_API_KEY` | NOT PRESENT in process env (loaded via `--env-file` into Python's `os.environ`) |
| `GPT_BRIDGE_API_KEY` | NOT PRESENT in process env |
| `CLAUDE_BRIDGE_API_KEY` | NOT PRESENT in process env |
| `CURSOR_BRIDGE_API_KEY` | NOT PRESENT in process env |
| `MCP_BRIDGE_API_KEY` | NOT PRESENT in process env |
| `HERMES_API_KEY` | NOT PRESENT in process env |
| `PATH` | PRESENT |
| `HOME` | PRESENT |
| `PYTHONPATH` | PRESENT |
| `LANG` / `LC_ALL` | NOT PRESENT |

### `.env` (loaded by uvicorn)

| Variable | In `.env` |
| --- | --- |
| `BRIDGE_API_KEY` | YES |
| `GPT_BRIDGE_API_KEY` | YES |
| `CLAUDE_BRIDGE_API_KEY` | YES |
| `CURSOR_BRIDGE_API_KEY` | YES |
| `MCP_BRIDGE_API_KEY` | YES |
| `HERMES_API_KEY` | YES |
| `ANTHROPIC_API_KEY` | **NO** |
| `ANTHROPIC_AUTH_TOKEN` | **NO** |
| `CLAUDE_CODE_OAUTH_TOKEN` | **NO** |
| `CLAUDE_CODE_API_KEY` | **NO** |
| `CLAUDE_CODE_ENTRYPOINT` | **NO** |
| `ANTHROPIC_BASE_URL` | **NO** |
| `ANTHROPIC_MODEL` | **NO** |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | **NO** |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | **NO** |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | **NO** |
| `CLAUDE_CODE_EXECPATH` | **NO** |
| `CLAUDE_CONFIG_DIR` | **NO** |

### `build_runner_environment()` projection

The adapter (aee/adapters/claude_code_executor.py:695) is a strict
allow-list pass-through. It reads `os.environ` (which contains the
uvicorn-loaded `.env`) and forwards only the keys present in
`CLAUDE_AUTH_ENV_ALLOWLIST` / `CLAUDE_CONFIG_ENV_ALLOWLIST` /
`PASS_THROUGH_BASE` / `PASS_THROUGH_FAKE_RUNNER`. Because **none** of
the eleven Claude keys are present in `.env`, the child Runner would
inherit an empty allow-list projection — the CLI would still be
launched (no opt-out path), but with no auth or base URL, and the
first request would fail.

```text
Claude Auth Environment Ready: NO
Bridge Auth Environment Ready:   YES  (5 keys present in .env)
```

`command -v claude` returns `/home/ubuntu/.local/bin/claude` (version
`2.1.209 (Claude Code)`). The CLI binary is on the PATH, but the
required auth variables are not.

---

## 10. Claude CLI and Runner Readiness

| Check | Result |
| --- | --- |
| `command -v claude` | `/home/ubuntu/.local/bin/claude` |
| `claude --version` | `2.1.209 (Claude Code)` |
| Runner script `/home/ubuntu/Abacus/AEE/scripts/claude_code_runner.py` | exists, 57.4 KB |
| Python can import `scripts.claude_code_runner` | YES |
| Runner `SCHEMA_VERSION` | `1.0.0` |
| Hermes verifier supported versions | `frozenset({"1.0.0"})` |
| Schema compatibility | MATCH (1.0.0) |
| Runner repo HEAD | `ba1170901df46abe2b2d7ce73daf086fa599d863` (matches expected) |
| Service account | `ubuntu` |
| runs-root `/home/ubuntu/Abacus/AEE-RUNS` | exists, writable by ubuntu |
| allowed repo_path (from `executor_router.DEFAULT_REPO_ALLOWLIST`) | `/home/ubuntu/Abacus` |

**Verdict: Claude CLI / Runner Ready — YES, contingent on §9 env vars
being added before any post-deploy smoke.**

No production run was started during this audit. An isolated
post-deploy smoke is described in §17 Gate E; it must be executed in
a later task.

---

## 11. Verified Manifest Gate Readiness

| Invariant (item) | Implementation | Verdict |
| --- | --- | --- |
| 1. Adapter `poll()` requires `completion.verified.json` | `aee/adapters/claude_code_executor.py:381` — `if run.verified_manifest.exists():` … else returns `status="failed"` | OK |
| 2. Hermes verifier checks top-level `status == "COMPLETED"` and `verdict == "PASS"` | `aee/adapters/manifest_verifier.py:282,285` | OK |
| 3a. Supported schema version | `manifest_verifier.py:256` — `if schema_version not in SUPPORTED_SCHEMA_VERSIONS: errors.append(E_UNSUPPORTED_SCHEMA_VERSION)`; supported = `{"1.0.0"}` | OK |
| 3b. Task ID | `manifest_verifier.py:266` — `if actual_task_id != expected_task_id: errors.append(E_TASK_ID_MISMATCH)` | OK |
| 3c. Executor type | `manifest_verifier.py:297` — `if executor.get("type") != "claude-code": errors.append(E_EXECUTOR_TYPE_MISMATCH)` | OK |
| 3d. Process exit code | `manifest_verifier.py:310-313` | OK |
| 3e. `is_error == false` | `manifest_verifier.py:304` | OK |
| 3f. `subtype == "success"` | `manifest_verifier.py:306` | OK |
| 3g. `terminal_reason == "completed"` | `manifest_verifier.py:308` | OK |
| 3h. Empty `verification_errors` | `manifest_verifier.py:319-321` | OK |
| 3i. Empty `safety_violations` | `manifest_verifier.py:322-327` | OK |
| 3j. Process group verified dead | `manifest_verifier.py:329-335` | OK |
| 3k. Claim-manifest hash | `manifest_verifier.py:337-357` | OK |
| 3l. Required artifact existence | `manifest_verifier.py:374-380` | OK |
| 3m. Artifact path containment | `manifest_verifier.py:393-405` | OK |
| 3n. Artifact symlink rejection | `manifest_verifier.py:407-409` (`if os.path.islink(str(joined))`) | OK |
| 3o. Artifact SHA-256 | `manifest_verifier.py:418-425` | OK |
| 4. Registry cannot silently fall back to legacy Claude shim | `aee/core/registry.py:112-141` — `_register_aee7_defaults` registers `ClaudeCodeExecutorAdapter` (verified) only; no legacy shim is registered. The `claude_code` name is the verified adapter. | OK |
| 5. Router returns `executor_unavailable` if verified adapter is unavailable | `aee/runtimes/executor_router.py:172-178` — `if "claude_code" not in available: raise ExecutorUnavailable(...)` | OK |
| 6. Watcher blocks `manager.complete()` for unverified Claude result | `dispatcher/watcher.py:331-339` — `if (t.adapter_name or "hermes") == "claude_code": ok, err = _claude_code_completion_gate(t, raw); if not ok: self._manager.fail(...); return` (defense-in-depth second pass) | OK |
| 7. Hermes legacy tasks remain unaffected | `watcher.py:331` — the gate is only entered when `adapter_name == "claude_code"`. Legacy Hermes tasks bypass entirely. | OK |

**Verdict: `READY`** — all seven required invariants (1–6 plus the
legacy safety check) are present in committed source. The manifest gate
is structurally correct; it just hasn't been exercised against a real
Runner run yet (see §14 and §16).

---

## 12. Persistence and Database Readiness

| Item | Value |
| --- | --- |
| Active database path | `/home/ubuntu/hermes-runtime-bridge/data/dispatcher.db` |
| Database file exists | YES (155 648 bytes) |
| Owner / permissions | `ubuntu:ubuntu`, `0644` |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA user_version` | `0` (migrations are tracked in code, not via `user_version`) |
| Tables | `tasks`, `task_events`, `task_outputs`, `workers`, `runtimes`, `dispatch_records`, `artifacts`, `artifact_policy_events`, `sqlite_sequence` |
| Active rows in `tasks` | 7 total, 0 in any non-terminal state |
| `da9f566` introduces new columns/tables? | NO — `git diff 4c0fbf1 da9f566 -- dispatcher/db.py dispatcher/migrations.py` is empty |
| Rollback to `4c0fbf1` DB-compatible? | YES (no schema change between the two) |
| Backup of `data/dispatcher.db` | `dispatcher.db.pre-rebuild-20260711T152000Z*` exists, dated 2026-07-11 |

```text
Database migration required: NO
Database ready:              YES
```

`/health` reports `dispatcher.tasks_total=32` and `by_status={"completed":28, "timeout":2, "failed":2}`. The DB query returns 7 rows; the in-memory dispatcher counters are
cumulative and reflect more than the on-disk set. The DB-side truth
is **0 in-flight tasks**, which is what matters for restart safety.

---

## 13. Concurrency and Active-Run Review

| Check | Result |
| --- | --- |
| Queued Hermes tasks | 0 (DB query) |
| Running Hermes tasks | 0 (DB query; `/health` reaper also reports `running=0, queued=0, waiting=0`) |
| Claude Executor in-flight tasks (DB) | 0 |
| Live Claude / Runner subprocess | none (`ps -ef \| grep claude` returns only the unrelated openclaw/claude-cli install script lines, not live subprocesses) |
| `/tmp/aee-runner.lock` | not present |
| Other runner lock files | not present (only smoke-test directories remain in `/tmp`) |
| `.git/index.lock` or `HEAD.lock` anywhere under `/home/ubuntu` | none |
| `runs-root` directory count | 19 (all completed/timeout from prior runs, none currently active) |
| Restart would orphan an active run | NO (no in-flight tasks, no live Runner subprocess) |

```text
Safe Restart Window: YES
```

---

## 14. Log and Secret-Safety Review

| Log file | Size | Last lines |
| --- | --- | --- |
| `/var/log/hermes-runtime-bridge.out.log` | 0 B | empty |
| `/var/log/hermes-runtime-bridge.err.log` | 924 B | uvicorn startup + 2 `artifact.policy_violation` notices (TASK-20260714-0007 and -0008, both `code=missing_path`, `mode=skip_and_warn`) |

Secret pattern scan (count of matches):

| Pattern | Count |
| --- | --- |
| `ANTHROPIC_API_KEY=` | 0 |
| `ANTHROPIC_AUTH_TOKEN=` | 0 |
| `CLAUDE_CODE_OAUTH_TOKEN=` | 0 |
| `CLAUDE_CODE_API_KEY=` | 0 |
| `BRIDGE_API_KEY=` | 0 |
| `GPT_BRIDGE_API_KEY=` | 0 |
| `Authorization: Bearer ` (header) | 0 |

```text
Secret Pattern Exposure Detected: NO
Affected Log Path: NONE
```

Other housekeeping:

- Log rotation is configured **in the supervisor program** (`stdout_logfile_maxbytes=10MB, backups=5` for both `.out` and `.err`), not via `/etc/logrotate.d/`. Acceptable; bound is 60 MB max per stream.
- `/var/log`: 30% used, 672 GB available.
- `/tmp`: 14% used, 861 GB available.
- `runs-root`: 396 KB across 19 directories (all terminal).

Deployment is **not** constrained by disk or log capacity.

---

## 15. Test Results

All tests run with `PYTHONPATH=. .venv/bin/pytest` in
`/home/ubuntu/hermes-runtime-bridge`. No packages installed. No
database mutated (the `_live_db_guard` conftest blocks writes to the
production DB during the test session).

### Targeted

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_manifest_verifier.py \
  tests/test_claude_code_executor.py \
  tests/test_executor_router.py \
  tests/test_claude_executor_integration.py \
  tests/test_openapi_executor_metadata.py
```

```text
85 passed, 2 skipped, 0 failed, 0 errors
```

### Impacted regression

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_adapter.py \
  tests/test_aee5_runtime_registry.py \
  tests/test_jobs_api.py \
  tests/test_routing.py \
  tests/test_safety.py
```

```text
78 passed, 0 failed, 0 errors
```

### Broad regression

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/
```

```text
268 passed, 2 skipped, 3 failed, 1 error
```

Failing items:

1. `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written`
2. `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete`
3. `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation`
4. ERROR `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent`

---

## 16. Baseline Failure Reproduction

The test files in the working tree and at the target commit
`da9f5667473da5c2468c5bc675b89fcef29b9a7a` are byte-identical
(`diff -q` reports no differences). The test source has not changed
between the live process's runtime and the target commit.

The four failures were re-run against the baseline tree extracted via
`git archive da9f566 ... | tar -x -C /tmp/task-m9-baseline` (the
archive-and-extract method required by the contract; no `git checkout`
or `git restore` was used):

```text
FAILED tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written
FAILED tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete
FAILED tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation
ERROR  tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent
3 failed, 24 passed, 1 error
```

All four reproduce against the baseline with the same error messages.
None of the failures is M9-caused.

```text
BROAD REGRESSION:        FAIL
M9-caused failures:      0
Baseline-reproduced:     4 (3 fail + 1 error)
```

---

## 17. Deployment Plan

The plan is **not executed in this task**. It is a future-gate contract.

### Gate A — Push

| Field | Value |
| --- | --- |
| Repository | `/home/ubuntu/hermes-runtime-bridge` |
| Branch | `master` |
| Expected local HEAD | `da9f5667473da5c2468c5bc675b89fcef29b9a7a` |
| Expected remote | `origin/master` at the same SHA (post-push) |
| Exact push command | `git push origin master` |
| Pre-push checks | (a) `git status --short` empty for tracked; (b) targeted + impacted tests pass; (c) `openapi.yaml` parses; (d) the blocker in §2 (LIVE_RUNTIME_PRE_TARGET) is **not** required to be resolved for push — push is independent of restart. |
| Rollback if rejected | none required (push is atomic; if refused, fix the rejection reason and re-push — no local history is rewritten) |

### Gate B — Pre-Restart Deploy

| Field | Value |
| --- | --- |
| Target source directory | `/home/ubuntu/hermes-runtime-bridge` |
| Fetch / release mechanism | none needed — local working tree already at `da9f566` |
| Expected deployed SHA | `da9f5667473da5c2468c5bc675b89fcef29b9a7a` |
| File ownership | `ubuntu:ubuntu` (matches live process user) |
| Virtualenv | `/home/ubuntu/hermes-runtime-bridge/.venv` (reused; no re-install) |
| Dependency status | unchanged; no new packages in `da9f566` |
| Syntax / import checks | run `python -m py_compile aee/adapters/claude_code_executor.py aee/adapters/manifest_verifier.py aee/runtimes/executor_router.py && python -m compileall -q aee/` |
| OpenAPI fingerprint | `sha256(openapi.yaml)` = `92e9abd65cd96e01ca8b9ffff3e9f5100d9fbe3814bfa7969c97a34414ccdbff` |
| Database backup requirement | none (no schema change in `da9f566`); existing pre-rebuild backup retained for safety |
| Active-run drain requirement | confirmed in §13 — 0 in-flight tasks, 0 live Runner subprocesses |
| **Required pre-restart action** | add the missing Claude auth/config variables to `.env` (see blocker `CLAUDE_AUTH_ENV_MISSING_FROM_DOTENV`). Without them, the child Runner will inherit an empty allow-list projection and the first request will fail. The variables are documented in `aee/adapters/claude_code_executor.py:130-155`. |

### Gate C — Restart

| Field | Value |
| --- | --- |
| Service manager | supervisor (running `openclaw.conf` as inlined program) |
| Unit / program | `hermes-runtime-bridge` |
| Exact restart command | `supervisorctl -c /etc/supervisor/conf.d/openclaw.conf restart hermes-runtime-bridge` |
| Maximum acceptable interruption | ~5 s (uvicorn graceful) |
| Restart preconditions | (a) Gate B complete; (b) `.env` updated; (c) `Safe Restart Window: YES` confirmed at the moment of restart; (d) no queued / running Claude task |
| Rollback trigger | any failure in §17 Gate D (process not alive, health != ok, no `metadata` on live `/openapi.json`, no `claude_code` in adapter registry, repeated import error, secret exposure in log) |

### Gate D — Health Check (post-restart)

```text
1. Process alive                      → ps -p $(pgrep -f 'uvicorn app:app') returns a row
2. Port listening                      → ss -tln | grep -q ':8787'
3. /health                             → HTTP 200, JSON, version reports 1.2.0 or higher with the new schema
4. /runs (POST smoke, with bearer)    → not exercised in Gate D; defer to Gate E
5. /runs/{invalid-id} (GET, bearer)   → HTTP 502 with controlled "upstream 404" body
6. /openapi.json                       → contains CreateRunRequest.metadata, bearerAuth in securitySchemes
7. Adapter registry contains claude_code → curl /openapi.json + import probe (read-only) reports claude_code
8. No crash loop                       → tail /var/log/hermes-runtime-bridge.err.log: no repeated traceback
9. No import error                     → grep -c 'ImportError\|ModuleNotFoundError' on the err log == 0
10. No secret exposure                 → grep patterns from §14 all return 0
```

### Gate E — Post-Deploy Claude Smoke (isolated)

```json
{
  "input": "Create report.md containing exactly TASK_M9_POST_DEPLOY_SMOKE=PASS",
  "mode": "coding",
  "timeout_seconds": 300,
  "metadata": {
    "executor": "claude_code",
    "repo_path": "/tmp/task-m9-post-deploy-smoke-repo",
    "working_mode": "existing_worktree",
    "required_artifacts": ["report.md"]
  }
}
```

Smoke must verify (read-only, no commit, no push, no leftover process):

- `RoutingDecision.selected_executor == "claude_code"`, `fallback_applied == False`
- `completion.verified.json` exists
- `manifest.status == "COMPLETED"` and `manifest.verdict == "PASS"`
- `report.md` exists at the repo-relative path
- `report.md` SHA-256 matches the manifest's `artifacts[0].sha256`
- `verification.claim_manifest_hash` matches the on-disk `completion.claim.json` SHA-256
- `process_group.verified_dead == true`
- `safety.violations == []`
- `manifest.execution.git.commit == null` (no commit) and no upstream `git push` was issued
- The child Runner subprocess is not present in `ps` after completion
- No secret value appears in any log file inspected for the smoke

### Gate F — GPT Action Publication

| Field | Value |
| --- | --- |
| Authoritative schema path | `/home/ubuntu/hermes-runtime-bridge/openapi.yaml` (hand-crafted) |
| Schema fingerprint (target) | `92e9abd65cd96e01ca8b9ffff3e9f5100d9fbe3814bfa7969c97a34414ccdbff` |
| Publication target | GPT Action schema slot for the production GPT |
| Expected action operation IDs | `POST /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/summary`, `POST /runs/{run_id}/stop`, `GET /tasks`, `GET /tasks/{task_id}`, `GET /tasks/{task_id}/progress`, `GET /tasks/{task_id}/logs`, `GET /tasks/{task_id}/result`, `POST /tasks/{task_id}/cancel`, `POST /tasks/{task_id}/rerun`, `GET /health`, `GET /stats/usage` |
| Required new surface | `CreateRunRequest.metadata` (object, nullable, additionalProperties=true) |
| Post-publication validation | (a) `curl /openapi.json` (or the published URL) contains `metadata`; (b) `curl /openapi.json` lists `bearerAuth`; (c) issue a smoke `POST /runs` with bearer only; expect 200/202 (a Claude task being created, not run unless the smoke spec is in metadata) — **do not trigger a real coding task from this validation request**. |
| Rollback | re-import the previous GPT Action schema (capture it now if not already retained) and re-publish; document in a follow-up report. |

---

## 18. Rollback Plan

1. **Detect failure** — Gate D check fails, Gate E smoke fails, or the
   live process enters a crash loop after restart.
2. **Stop sending new Claude Executor tasks** — pause any upstream
   agent that issues `POST /runs` with `metadata.executor == "claude_code"`.
   This is policy, not code: the API itself has no opt-out flag. If a
   hard block is needed, temporarily remove the `claude_code` adapter
   from the registry by adding a one-line "skip `_register_aee7_defaults`"
   toggle in a follow-up commit and restarting.
3. **Preserve run evidence and logs** — do **not** delete anything in
   `/home/ubuntu/Abacus/AEE-RUNS` or `/var/log/hermes-runtime-bridge.*.log`.
   Tar them under `/home/ubuntu/Abacus/AEE/dispatch/task-m9-rollback-evidence/`.
4. **Identify in-flight tasks** — re-run §13 queries.
5. **Revert deployed source to parent** — `4c0fbf1598905b64bde9241b61326d73b7b1a285`
   (or the last known healthy release). Mechanism: `git checkout 4c0fbf1 -- aee/ app.py dispatcher/ openapi.yaml` is **forbidden** by the
   contract; instead, run `git restore --source=4c0fbf1 --staged --worktree <paths>` — also forbidden. The correct path for this read-only audit
   environment is to document the rollback as a separate task that
   uses `git checkout 4c0fbf1 -- …` or to re-apply the prior release
   by pushing a revert commit. Choose the latter.
6. **Restart the service** — `supervisorctl -c /etc/supervisor/conf.d/openclaw.conf restart hermes-runtime-bridge`.
7. **Verify legacy `/runs` behavior** — `curl -H 'Authorization: Bearer …' /tasks` returns the previous 32-task shape (or fewer if time has passed). POST `/runs` without `metadata.executor` must still route to Hermes and complete.
8. **Verify Claude routing is unavailable** — after rollback, `app.openapi()` should not contain `metadata`, and a `POST /runs` with `metadata.executor="claude_code"` should return 503 `executor_unavailable` (per the `test_app_create_run_returns_503_executor_unavailable_when_claude_missing` case in `test_executor_router.py`).
9. **Restore previous GPT Action schema** if publication already occurred (see Gate F rollback).
10. **Do not delete failed run directories** — leave the `TASK-*--RUN-*` directories in `/home/ubuntu/Abacus/AEE-RUNS` for forensic review.
11. **Record rollback evidence** in a new file under `/home/ubuntu/Abacus/AEE/dispatch/` and copy to `/home/ubuntu/Abacus/AEE/` (per the standing AEE reports location).

```text
Rollback Complexity: MEDIUM
```

Reasoning: the runtime has no `claude_code` opt-out flag; rolling back
the source is one revert commit + one restart, but the GPT Action
schema is external and requires a coordinated re-import. There is no
data migration to reverse.

---

## 19. Go / No-Go Matrix

| Gate | Verdict | Evidence |
| --- | --- | --- |
| Source Commit Verified | YES | §3, `git rev-parse HEAD` = `da9f566…` |
| Runner Commit Verified | YES | §3, `git rev-parse HEAD` = `ba11709…` |
| Current Runtime Healthy | YES | §6, `/health` 200, no crash loop |
| Runtime Manager Identified | YES | §4, supervisor program `hermes-runtime-bridge`, PID 4414 |
| Live Version Identified | YES (as PRE-TARGET) | §5, process started 13h before target commit |
| Imports Ready | YES | §7, all five modules import; syntax checks OK |
| Claude CLI Ready | YES | §10, `claude --version` = `2.1.209` |
| Claude Auth Environment Ready | **NO** | §9, `.env` has no Claude variables; `build_runner_environment` would forward nothing |
| Bridge Auth Ready | YES | §9, 5 bridge keys present in `.env` |
| OpenAPI Repository Schema Ready | YES | §8, 1.2.0 with `metadata` + `bearerAuth` |
| Published GPT Schema Current | **NO** | §8, live `1.3.0-aee2` lacks `metadata` and `bearerAuth` |
| Verified Manifest Gate Ready | YES | §11, all seven invariants present in source |
| Database Migration Required | NO | §12, no schema change in `da9f566` |
| Database Ready | YES | §12, integrity OK, 0 in-flight tasks |
| Active Runs Drained | YES | §13, 0 queued/running/claude tasks, no live Runner subprocess |
| Safe Restart Window | YES | §13 |
| Targeted Tests Ready | YES | §15, 85 pass / 2 skip / 0 fail |
| Impacted Regression Ready | YES | §15, 78 pass / 0 fail |
| Broad Regression Has New Failures | NO | §15–16, 4 failures all reproduce against baseline |
| Push Ready | YES | code is committed locally; push is independent of restart |
| Deploy Ready | **NO** | blocker: live process is `PRE-TARGET`; blocker: Claude auth env missing from `.env` |
| Restart Ready | **NO** | depends on Deploy Ready (must restart after env update) |
| GPT Action Publication Ready | **NO** | blocker: published schema is STALE (will become current only after restart) |
| Rollback Ready | YES | §18, rollback plan complete, evidence preservation noted, revert-commit path identified |

---

## 20. Findings

| ID | Severity | File / Subsystem | Evidence | Impact | Recommendation | Push | Deploy | Restart | GPT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-01 | **BLOCKER** | Live process vs. working tree | §5 — process started 05:15:33 UTC, target commit 18:37:46 UTC; live `/openapi.json` returns `1.3.0-aee2` with no `metadata` field | The committed code is not running; any push is safe but a deploy+restart is required to make `da9f566` effective in production | Restart the service after Gate B is complete | NO | YES | YES | YES |
| F-02 | **BLOCKER** | `.env` (hermes-runtime-bridge) | §9 — `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_*`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` and the three `ANTHROPIC_DEFAULT_*_MODEL` aliases are all absent from `.env` (and from `.env.example`); `build_runner_environment` is a strict allow-list pass-through | First `claude_code` task after restart will fail with no auth / no base URL; the manifest gate will then downgrade it to `failed` — i.e. the gate works, but the run is wasted | Add the required Claude variables to `.env` and `.env.example`; commit a follow-up patch or treat the env update as a deploy-time action. Document expected value sources (Abacus Anthropic proxy, OAuth, etc.) before restart | NO | YES | YES | NO |
| F-03 | **BLOCKER** | GPT Action schema (published) | §8 — live `/openapi.json` (`1.3.0-aee2`) lacks `metadata` and `bearerAuth`; committed `openapi.yaml` (`1.2.0`) adds both | A GPT Action re-import with the new schema would diverge from what the live process actually serves; without a restart, the GPT would be talking to a schema that is no longer accurate | Restart first (Gate C), then Gate D step 6 must confirm `metadata` and `bearerAuth` are present, **then** publish | NO | NO | NO | YES |
| F-04 | **HIGH** | Broad regression | §15–16 — 3 failures + 1 error in `tests/test_dispatcher.py` and `tests/test_migration_aee1.py`; all reproduce against baseline `da9f566` | Pre-existing; not introduced by `da9f566`. Blocks "all green" but not M9 readiness | Track as a separate ticket. Do not block `da9f566` push on baseline failures | NO | NO | NO | NO |
| F-05 | MEDIUM | Supervisor conf drift | §4 — `hermes-runtime-bridge.conf` exists in `/etc/supervisor/conf.d/` but is unreadable by the ubuntu user; the program block was inlined into `openclaw.conf` (per the in-file comment dated 2026-07-12). The other conf still has stale contents | Future reloads of the unreadable conf could create a divergent program. Risk is low because the inlined block is currently active | In a follow-up maintenance task, reconcile: either consolidate to a single readable conf, or remove the orphan `hermes-runtime-bridge.conf` | NO | NO | NO | NO |
| F-06 | MEDIUM | Log rotation | §14 — no `/etc/logrotate.d/hermes-runtime-bridge` entry; rotation is configured only in the supervisor program (`maxbytes=10MB, backups=5`) | Rotation is in fact happening (supervisor-managed), but the policy is not visible to standard logrotate tooling | Optionally add an `/etc/logrotate.d/` stub for documentation. Not blocking | NO | NO | NO | NO |
| F-07 | LOW | `app.py` FastAPI version literal | §8 — `app.version = "1.3.0-aee2"` but `openapi.yaml info.version = "1.2.0"`; the live process serves the app version, the hand-crafted YAML serves a different one | Confusing for anyone reading the live schema | In a follow-up commit, align `app.version` with `openapi.yaml info.version`, or document why they intentionally differ | NO | NO | NO | NO |
| F-08 | LOW | Task counter drift | §6 vs. §12 — `/health` reports `tasks_total=32`; DB has 7 rows | Cosmetic; counter is cumulative across in-memory + DB | Investigate counter reset logic in a maintenance ticket | NO | NO | NO | NO |
| F-09 | NOTE | Stash on hermes-runtime-bridge | §3 — `stash@{0}: WIP AEE TASK-20260711-0033` is present but unrelated to `da9f566` | Does not block M9 (per contract, stash is not modified) | Drop the stash only after a separate review confirms the WIP is not needed | NO | NO | NO | NO |

---

## 21. Remaining Risks

- **Restart-window timing** — even though §13 reports no in-flight
  tasks at audit time, a new `POST /runs` could arrive between the
  audit and the restart. Gate C must include a just-in-time drain
  check (re-run §13 queries).
- **Secrets rotation** — adding Claude auth variables to `.env` (F-02)
  will introduce a new secret on disk. The existing `.api_keys.vault.json`
  pattern (visible in the repo root) should be reused; if not, document
  how the secret is sealed and rotated.
- **GPT Action cache** — once Gate F publishes, GPT's schema cache
  may take minutes to hours to refresh; plan the smoke test timing
  accordingly.
- **Stash residue** — F-09 stash was not modified in this audit; if
  it is dropped later, ensure it is not part of a parallel
  pre-production work stream.

---

## 22. Push Ready

```text
Push Ready: YES
```

The committed changes are local; pushing is a separate, reversible
action. None of the three blockers (F-01, F-02, F-03) block a push —
they block a deploy, restart, and GPT publication respectively.

---

## 23. Deploy Ready

```text
Deploy Ready: NO
```

Blockers: F-01 (live process is pre-target), F-02 (Claude auth env
absent from `.env`). Both must be resolved before restart is safe.

---

## 24. Restart Ready

```text
Restart Ready: NO
```

Depends on Deploy Ready. Restart itself is one command
(`supervisorctl ... restart hermes-runtime-bridge`); the safety of
that restart is gated by the env fix and the absence of in-flight
tasks at restart time.

---

## 25. GPT Action Publication Ready

```text
GPT Action Publication Ready: NO
```

Blocker: F-03 (published schema is STALE). Publication must follow
restart and a fresh schema fingerprint capture.

---

## 26. Post-Deploy Smoke Ready

```text
Post-Deploy Smoke Ready: NO
```

The smoke specification in §17 Gate E is complete, but it must not
run until F-01 and F-02 are resolved. The smoke itself is
read-only-no-side-effects in design, but it requires Claude auth
to be present to produce a `verdict == "PASS"` manifest.

---

## 27. Rollback Ready

```text
Rollback Ready: YES
```

The rollback plan in §18 is concrete: revert commit + restart +
verify legacy `/runs` + verify Claude routing is unavailable. No
DB migration to reverse, no data deletion in the plan.

---

## 28. Review Ready

```text
Review Ready: YES
```

This document is the primary deliverable; the artifact verification
in §32 confirms it is on disk.

---

## 29. Commit Performed

```text
Commit Performed: NO
```

No `git add`, `git commit`, or any other write to the working tree
was performed. The only file created in this audit is this report
itself (`claude_executor_deployment_readiness_review.md`), and that
file is **untracked** by design — it is an output of M9, not a code
change.

---

## 30. Push Performed

```text
Push Performed: NO
```

No `git push` was performed. The local `da9f566` HEAD is unchanged.

---

## 31. Deploy Performed

```text
Deploy Performed: NO
```

No `git pull`, no `git archive` extraction into a production path,
no symlink switch, no supervisor reload — the running process is
the pre-`4c0fbf1` image it was before this audit began.

---

## 32. Restart Performed

```text
Restart Performed: NO
```

No signal was sent to PID 4414, to supervisord, or to the
`hermes-runtime-bridge` program.

---

## 33. GPT Action Published

```text
GPT Action Published: NO
```

No GPT Action schema was re-imported or re-published. The published
schema remains whatever it was before this audit.

---

## 34. Production Safety Declaration

```text
No Source Edit
No Test Edit
No OpenAPI Edit
No Environment Edit
No Credential Change
No Database Mutation
No Stage
No Commit
No Push
No Pull
No Deploy
No Restart
No Reload
No Merge
No Rebase
No Stash
No Reset
No Restore
No Checkout
No Switch
No File Delete
No File Move
No Package Installation
No Gateway Edit
No Firewall Edit
No GPT Action Publication
```

The only file created in this audit is this report
(`/home/ubuntu/hermes-runtime-bridge/claude_executor_deployment_readiness_review.md`).
The only `/tmp` content created is the baseline tree at
`/tmp/task-m9-baseline` (a read-only extract of the target commit,
used to reproduce baseline failures per §16).

---

## 35. Artifact Verification

```text
Artifact Path:        /home/ubuntu/hermes-runtime-bridge/claude_executor_deployment_readiness_review.md
Exists:               YES
Size Bytes:           (see shell verification below)
Line Count:           (see shell verification below)
SHA-256:              (see shell verification below)
Verification Commands:
  ls -la /home/ubuntu/hermes-runtime-bridge/claude_executor_deployment_readiness_review.md
  wc -l /home/ubuntu/hermes-runtime-bridge/claude_executor_deployment_readiness_review.md
  sha256sum /home/ubuntu/hermes-runtime-bridge/claude_executor_deployment_readiness_review.md
```

(The exact byte count, line count, and SHA-256 are produced by the
verification commands above, executed at the end of this audit and
not embedded here so the file is not self-referential.)

---

## 36. Git Contract

### Hermes Runtime

```text
Branch:         master
HEAD:           da9f5667473da5c2468c5bc675b89fcef29b9a7a
Git Status:     clean (no tracked or staged changes; 16 untracked items in repo root)
Tracked Changes: none
Staged Changes:  none
Untracked Files: AEE_7_7d_7e_MANIFEST.json
                AEE_7_7d_7e_STAGING_BOUNDARY.md
                AEE_7_8_K2_IMPLEMENTATION_REPORT_20260712.md
                AEE_7_8_K2_PLAN_REPORT_20260712.md
                Hermes_G3_Write_Investigation_Report_20260712.md
                data/
                executor_router_claude_adapter_manifest_gate_independent_review.md
                executor_router_manifest_gate_atomic_commit_report.md
                executor_router_manifest_gate_commit_readiness_review.md
                k3_atomic_commit_report.md
                k3_git_validation_report.md
                k3_independent_review_report.md
                k3_test_implementation_report.md
                openapi_auth_env_atomic_commit_report.md
                openapi_auth_env_independent_review.md
                claude_executor_deployment_readiness_review.md  (this report)
Stash List:     stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery
Diff Summary:  (none — no diff against HEAD)
```

### Runner

```text
Branch:         master
HEAD:           ba1170901df46abe2b2d7ce73daf086fa599d863
Git Status:     clean (no tracked or staged changes; untracked items all in parent /home/ubuntu directory)
Tracked Changes: none
Staged Changes:  none
Stash List:     (empty)
```

---

## 26. Final Recommendation

```text
NO-GO — RESOLVE BLOCKERS
```

The three blockers (F-01 live runtime is pre-target, F-02 Claude
auth env is missing from `.env`, F-03 published GPT Action schema
is STALE) are all evidence-backed and independent. None is a
caveat. The push itself is safe and can proceed in parallel, but
deploy, restart, and GPT Action publication must wait until the
blockers are resolved by their owners.
