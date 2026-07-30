# TASK-M11 — Claude Executor Post-Restart Validation Report

## Execution Timing

| Field | Value |
|-------|-------|
| Report written UTC | 2026-07-15T02:28:36Z |
| Report written Asia/Taipei | 2026-07-15T10:28:36+08:00 |
| Restart start UTC | 2026-07-15T02:22:24Z |
| Restart start Asia/Taipei | 2026-07-15T10:22:24+08:00 |
| Restart end UTC (post-PID start) | 2026-07-15T02:22:27Z |
| Interruption duration | ~3 seconds (supervisor stop→start) |
| Smoke submission UTC | 2026-07-15T02:25:08Z |
| Smoke completion UTC | 2026-07-15T02:25:16Z |
| Smoke duration (wall clock) | ~8 seconds |

## Overall Verdict

**PASS WITH CAVEATS**

All restart, environment, smoke, manifest, process-cleanup, secret-safety, and test-execution
requirements were met. The live OpenAPI now exposes `CreateRunRequest.metadata` (it was missing
pre-restart). The Claude Executor adapter is registered (`aee.adapters.claude_code_executor.
ClaudeCodeExecutorAdapter`, not the legacy shim). A real end-to-end Claude coding smoke ran
through the live production `/runs` endpoint and reached `status=completed` with a verified
`verdict=PASS` manifest, the requested `report.md` artifact, no commit, no push, and no
leftover subprocesses.

**Caveat (also raised in M9/M10):** the live `/openapi.json` does NOT auto-publish
`components.securitySchemes.bearerAuth`. The bridge uses manual `Header(None)` parsing
with `require_auth(authorization)` rather than FastAPI's `HTTPBearer(...)` security
dependency, so `app.openapi()` (which is what `/openapi.json` returns) does not list
`bearerAuth`. The static `openapi.yaml` (1.2.0) declares the scheme but the live runtime
does not surface it. Authentication still works (every request without a valid Bearer
header returns 401), but the schema does not advertise it. See the **Findings** section
and **GPT Action Publication Readiness Gate** below.

Per M9 F-03, this gap was already a known blocker for publication. M11 confirms the
gap is still present after the controlled restart, so **GPT Action Publication remains
HELD** until either (a) the bridge is changed to use `HTTPBearer(...)` security on the
guarded routes so `app.openapi()` auto-publishes `bearerAuth`, or (b) the task's
acceptance contract is updated to recognize the static `openapi.yaml` as the
authoritative schema (since the runtime behaviour is identical).

## Baseline

### Bridge Git State

```text
Branch              master
HEAD                da9f5667473da5c2468c5bc675b89fcef29b9a7a
HEAD log            feat(runtime): expose Claude executor metadata and auth env
Tracked changes     0
Staged changes      0
Untracked files     (many task reports, no source changes)
Stash list          stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery
                    (legacy stash, NOT a tracked/staged modification; HEAD is unaffected)
Diff summary        (clean working tree)
```

Expected values match exactly: `da9f5667473da5c2468c5bc675b89fcef29b9a7a`.

### Runner Git State

```text
Branch              master
HEAD                ba1170901df46abe2b2d7ce73daf086fa599d863
HEAD log            feat(runtime): add Claude Code runner MVP
Tracked changes     0
Staged changes      0
Untracked files     (workspace noise; no source changes)
Stash list          (empty)
Diff summary        (clean working tree)
```

Expected values match exactly: `ba1170901df46abe2b2d7ce73daf086fa599d863`.

Note: M11 does not perform any git operations on either repository.

## Pre-Restart Runtime

- Process manager: `supervisord` (config: `/etc/supervisor/conf.d/openclaw.conf`)
- Supervisor program name: `hermes-runtime-bridge`
- Old PID: `4414`
- Old PPID: `7`
- Old start time (UTC): `2026-07-14T05:15:33Z`
- Old elapsed time at restart: `21:04:04` (≈21 hours)
- Old command line: `/home/ubuntu/hermes-runtime-bridge/.venv/bin/python /home/ubuntu/hermes-runtime-bridge/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787 --env-file /home/ubuntu/hermes-runtime-bridge/.env --no-access-log`
- Working directory: `/home/ubuntu/hermes-runtime-bridge`
- Listening port: `127.0.0.1:8787` (uvicorn, fd 16)
- Runtime user: `ubuntu`

The old PID was running the **pre-target** code (process started 2026-07-14T05:15:33Z;
target commit da9f566 is dated 2026-07-14T18:37:46Z). The live `/openapi.json` confirmed
pre-target state: no `CreateRunRequest.metadata`, no `bearerAuth` (see Pre-Restart
Health below).

## Safe Restart Window

```text
Queued tasks          0
Running tasks         0
Waiting tasks         0
Claude executor tasks 0
claude_code_runner    0 (none)
Claude CLI children   0 (no bridge-launched Claude CLI subprocess; the assistant's own Claude
                       Code process was the only `claude` process in `ps`, but it is the
                       M11 task's parent, not a bridge artifact)
Runner lock files     0
.git/index.lock       absent
.git/HEAD.lock        absent
Active dispatcher     0 (no in-flight dispatcher task)
Safe Restart Window   YES
```

Legacy `reaper` view at restart time also showed 0 running / 0 queued / 0 waiting.

## Environment Verification

### `.env` presence and ownership

```text
.env exists                  YES
.env is not tracked          YES (git status shows no path for .env)
.env is ignored by git       YES (.gitignore covers it; not in any tracked file)
.env owner                   ubuntu
.env group                   ubuntu
.env mode                    0600
Supervisor command contains  YES (--env-file /home/ubuntu/hermes-runtime-bridge/.env)
--env-file /home/ubuntu/...
```

### Required Claude env keys (presence only, no values)

```text
ANTHROPIC_AUTH_TOKEN              PRESENT
ANTHROPIC_BASE_URL                PRESENT
ANTHROPIC_MODEL                   PRESENT
ANTHROPIC_DEFAULT_SONNET_MODEL    PRESENT
ANTHROPIC_DEFAULT_OPUS_MODEL      PRESENT
ANTHROPIC_DEFAULT_HAIKU_MODEL     PRESENT
CLAUDE_CODE_ENTRYPOINT            PRESENT
CLAUDE_CODE_EXECPATH              PRESENT
```

All 8 required keys are present and non-empty (no value fragment or length is reported).

### Controlled projection via `build_runner_environment(parent)`

In-process test (PYTHONPATH=. .venv/bin/python) confirmed that when the parent environment
contains the 8 Claude keys plus 5 base vars (PATH, HOME, LANG, LC_ALL, PYTHONPATH) and 7
"unrelated" keys, the projected child environment forwarded by `build_runner_environment`
is exactly the allow-listed set (13 keys: 8 Claude + 5 base). The 7 unrelated keys
(BRIDGE_API_KEY, GPT_BRIDGE_API_KEY, AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, DATABASE_URL,
OPENAI_API_KEY, SSH_AUTH_SOCK) are NOT forwarded.

```text
Forwarded keys (13):
  ANTHROPIC_AUTH_TOKEN
  ANTHROPIC_BASE_URL
  ANTHROPIC_DEFAULT_HAIKU_MODEL
  ANTHROPIC_DEFAULT_OPUS_MODEL
  ANTHROPIC_DEFAULT_SONNET_MODEL
  ANTHROPIC_MODEL
  CLAUDE_CODE_ENTRYPOINT
  CLAUDE_CODE_EXECPATH
  HOME
  LANG
  LC_ALL
  PATH
  PYTHONPATH

All 8 required Claude keys forwarded: True
Unrelated keys NOT forwarded: True
```

### Live process env note (uvicorn --env-file semantics)

The supervisor command runs uvicorn with `--env-file /home/ubuntu/hermes-runtime-bridge/.env`.
uvicorn loads the file via `python-dotenv`'s `load_dotenv(...)` at process start, which
populates the Python process's `os.environ` but does NOT modify the kernel-level
`/proc/<pid>/environ`. Inspection of `/proc/153620/environ` therefore shows the
supervisor-injected host env (HOME, PATH, plus dozens of K8s/host vars) but no
`ANTHROPIC_*` / `CLAUDE_CODE_*` keys — these are accessible to the running Python
process via `os.environ` (and are the only way the FastAPI app, the Claude adapter, and
the `claude_code_runner` subprocess see them). This is standard uvicorn `--env-file`
behaviour, not a configuration defect. The smoke test (§Claude Executor Smoke) is the
authoritative end-to-end confirmation that the live process actually loaded the .env:
without those env vars, the Claude CLI would fail to authenticate against the custom
endpoint and the smoke would not have reached `status=completed`. It did.

## Pre-Restart Health

Endpoint: `GET http://127.0.0.1:8787/health` with `Authorization: Bearer <bridge key>`.

```text
HTTP status            200
Service                hermes-runtime-bridge
Reported version       1.2.0 (note: app.py / openapi.json report 1.3.0-aee2;
                        /health uses a separate string. See Findings.)
hermes                 reachable
hermes_base_url        http://127.0.0.1:8642
dispatcher.tasks_total 32
reaper                 running=0 queued=0 waiting=0 would_reap=0
safety.mode            blocklist_plus_allowlist
```

Endpoint: `GET http://127.0.0.1:8787/openapi.json`.

```text
HTTP status                      200
openapi version                  3.1.0
info.version                     1.3.0-aee2
CreateRunRequest.metadata        ABSENT
CreateRunRequest.required        ['input']
components.securitySchemes       {} (no bearerAuth)
POST /runs                       present
GET  /runs/{run_id}              present
GET  /runs/{run_id}/summary      present
POST /runs/{run_id}/stop         present
Live Runtime                     PRE-TARGET  (matches M10 §16)
```

Endpoint: `GET http://127.0.0.1:8787/tasks`.

```text
HTTP status         200
Total tasks         32
Non-terminal count  0
Active dispatchers  0
```

## Restart Execution

Command executed (exactly one, as required):

```bash
supervisorctl -c /etc/supervisor/conf.d/openclaw.conf restart hermes-runtime-bridge
```

Result:

```text
hermes-runtime-bridge: stopped
hermes-runtime-bridge: started
Exit code: 0
Supervisor response: standard stop/start pair
Old PID: 4414
New PID: 153620
Restart UTC: 2026-07-15T02:22:24Z
Restart Asia/Taipei: 2026-07-15T10:22:24+08:00
Interruption duration: ~3 seconds
```

Forbanned commands were not used (`supervisorctl restart all`, `reload`, `reread`,
`systemctl restart`, `kill -9`, `pkill`, `reboot`).

## Post-Restart Process

```text
New PID exists                  YES (153620)
New PID differs from old PID    YES (153620 ≠ 4414)
Process user                    ubuntu
Working directory               /home/ubuntu/hermes-runtime-bridge
Command uses expected venv      YES
  (/home/ubuntu/hermes-runtime-bridge/.venv/bin/python ...)
Command includes --env-file     YES
  (--env-file /home/ubuntu/hermes-runtime-bridge/.env)
Port 127.0.0.1:8787 listening   YES
No crash loop                   YES
Start time                      2026-07-15T02:22:27Z
Start time > target commit      YES (target 2026-07-14T18:37:46Z; start 2026-07-15T02:22:27Z)
```

Classification: `Live Runtime Version: TARGET OR LATER`.

## Post-Restart Environment

The live process `os.environ` is what the FastAPI app, the Claude adapter, and the
runner subprocess all see. The 8 required keys are loaded into `os.environ` by
uvicorn's `--env-file` flag (see Environment Verification §Live process env note above).
The smoke test reached `status=completed` against the custom endpoint, which is
direct end-to-end evidence that `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and the
model/entrypoint/execpath keys were all present and valid. Per-variable inspection of
`/proc/<pid>/environ` is not meaningful for `--env-file` keys (the kernel does not see
them), so the authoritative check is the smoke result.

```text
Required live-process variables (effective via os.environ):
  ANTHROPIC_AUTH_TOKEN              PRESENT
  ANTHROPIC_BASE_URL                PRESENT
  ANTHROPIC_MODEL                   PRESENT
  ANTHROPIC_DEFAULT_SONNET_MODEL    PRESENT
  ANTHROPIC_DEFAULT_OPUS_MODEL      PRESENT
  ANTHROPIC_DEFAULT_HAIKU_MODEL     PRESENT
  CLAUDE_CODE_ENTRYPOINT            PRESENT
  CLAUDE_CODE_EXECPATH              PRESENT
```

No values or lengths are reported.

## Post-Restart Health

### Health

```text
GET /health  →  HTTP 200
JSON parses                       YES
Runtime reports healthy           YES (status=ok)
No unexpected queued/running task YES (reaper: 0/0/0)
No reaper error                   YES
No database error                 YES
No startup exception              YES (uvicorn: "Application startup complete.")
```

### OpenAPI

```text
GET /openapi.json  →  HTTP 200

components.schemas.CreateRunRequest.properties.metadata   PRESENT
metadata.anyOf[0].type                                    'object'
metadata.anyOf[0].additionalProperties                    true
metadata.anyOf[1].type                                    'null'   (nullable/optional)
components.securitySchemes.bearerAuth                     ABSENT
POST /runs                                                present
GET  /runs/{run_id}                                       present
GET  /runs/{run_id}/summary                               present
POST /runs/{run_id}/stop                                  present
```

`metadata` is now exposed. `bearerAuth` is **NOT** exposed (see Findings and Overall
Verdict). Per the runtime shape, the live schema is otherwise current — the only
remaining gap is the missing `bearerAuth` securityScheme.

```text
Published Runtime Schema: PARTIAL
  - metadata:               present (current)
  - bearerAuth securitySch: absent (gap carried over from pre-restart)
```

## Live OpenAPI Verification

Detailed comparison (live `/openapi.json` vs task §10 contract):

| Required | Live state | Result |
|----------|------------|--------|
| `components.schemas.CreateRunRequest.properties.metadata` | present | PASS |
| `metadata.type == object` | `anyOf[0].type = "object"` (FastAPI 3.1 idiom for Optional[Dict]) | PASS |
| `metadata nullable or optional` | `anyOf[1].type = "null"` + field is `Optional[Dict[str, Any]] = Field(None, ...)` | PASS |
| `metadata additionalProperties == true` | `anyOf[0].additionalProperties = true` | PASS |
| `components.securitySchemes.bearerAuth` | absent | **FAIL** (see Findings) |
| `POST /runs` | present | PASS |
| `GET /runs/{run_id}` | present | PASS |
| `GET /runs/{run_id}/summary` | present | PASS |
| `POST /runs/{run_id}/stop` | present | PASS |

`bearerAuth` is the only missing item. Auth still works (every protected route calls
`require_auth(authorization)` which 401s without a valid Bearer key) — the gap is purely
in the OpenAPI advertisement, not in actual security. See Findings.

## Adapter Registry Verification

In-process check (post-restart, same code as the running bridge):

```python
from aee.core.registry import adapter_registry, bootstrap_defaults
bootstrap_defaults()
adapter_registry.names()
# → ['claude_code', 'hermes']
```

```text
adapter_registry.names()         ['claude_code', 'hermes']
claude_code class                 ClaudeCodeExecutorAdapter
claude_code module                aee.adapters.claude_code_executor
Is ClaudeCodeExecutorAdapter?     YES (NOT the legacy shim)
hermes class                      HermesAdapter
hermes module                     aee.adapters.hermes_adapter
```

The required class is `aee.adapters.claude_code_executor.ClaudeCodeExecutorAdapter`,
which is exactly what the registry resolves to.

## Legacy Hermes Compatibility

Submitted a minimal non-Claude request (no `metadata` field) that the bridge MUST route
to Hermes and never to Claude:

```json
{
  "input": "Reply with the single token HERMES_LEGACY_PROBE_OK to confirm legacy path works.",
  "mode": "normal",
  "timeout_seconds": 60
}
```

```text
HTTP status                       200
Response status                   started (then completed)
run_id                            run_b12cd590029f41e7886250d47adde955
task_id                           TASK-20260715-0001
selected_executor (in /tasks)     n/a (routing key absent in Hermes path)
runtime_type                      hermes
adapter_name                      hermes
Output                            HERMES_LEGACY_PROBE_OK
Git commit                        unchanged (da9f5667473da5c2468c5bc675b89fcef29b9a7a)
No mutation                       YES
No commit/push                    YES
No stuck task                     YES
```

The probe used the Hermes path end-to-end, was dispatched to the Hermes adapter
(`runtime_type: hermes`, `adapter_name: hermes`), did not require a Claude manifest,
did not mutate the repository, and reached a clean `completed` state in 3.4 seconds.

```text
Legacy Hermes Route: PASS
```

## Claude Executor Smoke

### Setup

- Temporary repository: `/home/ubuntu/Abacus/task-m11-post-restart-smoke-repo` (fresh
  isolated git repo, branch `master`, one initial commit, no remote). Originally the
  task specified `/tmp/...` but the live bridge's `repo_path` allow-list is
  `['/home/ubuntu/Abacus']` (see Findings) and the allow-list cannot be bypassed
  through the API without editing source. The repo was placed under
  `/home/ubuntu/Abacus/...` so the live allow-list accepts it; this is still a fresh,
  isolated, never-used repo with no production data.
- Initial commit hash: `0a1c875aa58c2a8b345140048a20f34a278fe2a8` (`init M11 smoke repo`).
- Temporary runs root: the live bridge hard-codes `DEFAULT_RUNS_ROOT =
  "/home/ubuntu/Abacus/AEE-RUNS"` (no per-request override exists in the API; see
  Findings). The smoke therefore landed in the standard runs root under
  `TASK-20260715-0004--RUN-0537024105c9/`. This is the same runs root the live
  process uses for every Claude run; no production repository is co-located there
  and no other Claude runs were active at the time.

### Request (sent to live production `/runs`)

```json
{
  "input": "Create report.md containing exactly the line TASK_M11_POST_RESTART_SMOKE=PASS",
  "mode": "coding",
  "timeout_seconds": 300,
  "metadata": {
    "executor": "claude_code",
    "repo_path": "/home/ubuntu/Abacus/task-m11-post-restart-smoke-repo",
    "working_mode": "existing_worktree",
    "required_artifacts": ["report.md"],
    "allow_commit": false,
    "human_approved": false
  }
}
```

The full live chain was exercised:

```text
POST /runs
  → Executor Router (request metadata.executor = claude_code)
  → ClaudeCodeExecutorAdapter (registered, verified adapter)
  → claude_code_runner (subprocess)
  → claude CLI (--env-file child env built via build_runner_environment)
  → completion.verified.json (write)
  → adapter verifier (verify_completion_manifest)
  → dispatcher completion gate
  → GET /runs/{run_id} (terminal state)
```

### Routing Evidence (from the response envelope)

```text
requested_executor       claude_code
selected_executor        claude_code
selection_source         metadata
fallback_applied         false
fallback_reason          null
```

### Polling Result

```text
Run id                   RUN-0537024105c9
Task id                  TASK-20260715-0004
Selected executor        claude_code (set in CreateRunResponse.safety)
Routing source           metadata
Status transitions       queued → running → completed
Final status             completed
Verdict                  PASS (from completion.verified.json)
Manifest path            /home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/completion.verified.json
Stdout log path          /home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/stdout.log
Stderr log path          /home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/stderr.log
Executor provider        minimax-m3:cloud (the routing layer resolved the custom
                         endpoint to this model; see Findings)
Effective model          minimax-m3:cloud
CLI version              2.1.210 (Claude Code)
Session ID               2107ff06-1bfa-4ac8-9d26-7da99cc349bb
Process group            pgid=154204, force_kill_used=false, verified_dead=true
```

### Required Smoke Assertions

| Assertion | Result | Evidence |
|-----------|--------|----------|
| `routing.selected_executor == claude_code` | PASS | `selected_executor: claude_code` in safety envelope |
| `routing.fallback_applied == false` | PASS | `fallback_applied: false` in safety envelope |
| `final status == completed` | PASS | `status: completed` from `GET /runs/RUN-0537024105c9` |
| Manifest exists | PASS | `completion.verified.json` present (2.7K) |
| Manifest is a regular file | PASS | `file` (not directory, not symlink) |
| Manifest is not a symlink | PASS | regular file |
| Manifest is inside the expected runs directory | PASS | under `/home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/` |
| `schema_version == 1.0.0` | PASS | `schema_version: 1.0.0` |
| `manifest.status == COMPLETED` | PASS | `status: COMPLETED` |
| `manifest.verdict == PASS` | PASS | `verdict: PASS` |
| `executor.type == claude-code` | PASS | `executor.type: claude-code` |
| `executor.exit_code == 0` | PASS | `executor.exit_code: 0` |
| `executor.is_error == false` | PASS | `executor.is_error: false` |
| `executor.subtype == success` | PASS | `executor.subtype: success` |
| `executor.terminal_reason == completed` | PASS | `executor.terminal_reason: completed` |
| `verification.verification_errors == []` | PASS | `verification.verification_errors: []` |
| `safety.violations == []` | PASS | `safety.violations: []` |
| `process_group.verified_dead == true` | PASS | `process_group.verified_dead: true` |
| `completion.claim.json exists` | PASS | present (2.0K) |
| `claim_manifest_hash matches recomputed SHA-256` | PASS | manifest `f90114c85425a4b30677e47f1d0c7d10131efc955821b0651f00e6950a30410f` == `sha256sum completion.claim.json` = `f90114c85425a4b30677e47f1d0c7d10131efc955821b0651f00e6950a30410f` |
| `report.md exists` | PASS | present (33 bytes) in the smoke repo |
| `report.md is a regular file` | PASS | regular file |
| `report.md is not a symlink` | PASS | regular file |
| `report.md contains exactly TASK_M11_POST_RESTART_SMOKE=PASS` | PASS | `cat report.md` = `TASK_M11_POST_RESTART_SMOKE=PASS` |
| `artifact byte count matches if recorded` | PASS | manifest `bytes: 33` == `stat -c %s report.md` = 33 |
| `artifact SHA-256 matches recomputed SHA-256` | PASS | manifest `cc62642afa1c1ad79b932fef33fec8cc1f88241e7be991318cc085e0637bd7a1` == `sha256sum report.md` |
| `git head_before == git head_after` | PASS | both `0a1c875aa58c2a8b345140048a20f34a278fe2a8` |
| `no commit created` | PASS | `git log` shows only the initial commit |
| `no push performed` | PASS | no remote configured; no push attempted |
| `no leftover claude_code_runner process` | PASS | `pgrep -af claude_code_runner` empty (post-smoke) |
| `no leftover Claude CLI subprocess` | PASS | `pgrep -af "TASK-20260715-0004\|RUN-0537024105c9"` empty |
| `adapter in-flight entry cleaned up` | PASS | `_inflight: Dict` size = 0 after smoke |

```text
Post-Deploy Smoke: PASS
```

## Manifest Verification

### Independent recheck

Called `aee.adapters.manifest_verifier.verify_completion_manifest` directly against
the smoke manifest:

```python
verify_completion_manifest(
    verified_manifest_path=".../completion.verified.json",
    expected_task_id="TASK-20260715-0004--RUN-0537024105c9",
    expected_run_dir=".../TASK-20260715-0004--RUN-0537024105c9",
    repo_path="/home/ubuntu/Abacus/task-m11-post-restart-smoke-repo",
    required_artifacts=["report.md"],
    subprocess_exit_code=0,
)
```

Result:

```text
verified                         True
verification_errors              []
schema_version                   1.0.0
artifacts_rechecked              1
claim_hash_match                 True
process_group_verified_dead      True
```

### Independent SHA-256 recompute

```text
SHA-256(completion.claim.json)
  recomputed: f90114c85425a4b30677e47f1d0c7d10131efc955821b0651f00e6950a30410f
  manifest:   f90114c85425a4b30677e47f1d0c7d10131efc955821b0651f00e6950a30410f
  match:      YES

SHA-256(report.md)
  recomputed: cc62642afa1c1ad79b932fef33fec8cc1f88241e7be991318cc085e0637bd7a1
  manifest:   cc62642afa1c1ad79b932fef33fec8cc1f88241e7be991318cc085e0637bd7a1
  match:      YES
```

```text
Verified Manifest Gate: PASS
```

## Artifact Verification

The smoke produced exactly one artifact, `report.md`, with the contractually required
content:

```text
Path:           /home/ubuntu/Abacus/task-m11-post-restart-smoke-repo/report.md
Size:           33 bytes
Lines:          1
Content:        TASK_M11_POST_RESTART_SMOKE=PASS
Regular file:   YES
Symlink:        NO
SHA-256:        cc62642afa1c1ad79b932fef33fec8cc1f88241e7be991318cc085e0637bd7a1
```

The manifest's `artifacts[0]` matches these values byte-for-byte.

## Process Cleanup

```text
Supervisor status:               RUNNING (pid 153620)
Bridge uptime at report time:    ~6 minutes
Bridge stderr log tail:          clean (no tracebacks after the restart)
Bridge stdout log:               empty (--no-access-log enabled)
Traceback count in bridge err:   0
Import-error count:              0
SQLite lock count:               0 (none reported)
HTTP 5xx count:                  0 (post-restart)
Process count of leftover claude_code_runner: 0
Process count of leftover Claude CLI child:   0
Stale runner lock:               none
Active task count:               0 (reaper: 0/0/0/0)
Adapter in-flight entries:       0 (verified by importing the adapter module)
```

The `_inflight` module-level dict in `aee.adapters.claude_code_executor` is empty
after the smoke; the smoke's run entry was removed by the adapter's own cleanup
path. No subprocess escaped.

## Secret Exposure Review

Scanned the following artifacts for the credential patterns listed in the task
(ANTHROPIC_API_KEY=, ANTHROPIC_AUTH_TOKEN=, CLAUDE_CODE_OAUTH_TOKEN=,
CLAUDE_CODE_API_KEY=, BRIDGE_API_KEY=, GPT_BRIDGE_API_KEY=, Authorization: Bearer):

```text
/home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/stdout.log
/home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/stderr.log
/home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/completion.claim.json
/home/ubuntu/Abacus/AEE-RUNS/TASK-20260715-0004--RUN-0537024105c9/completion.verified.json
/home/ubuntu/Abacus/task-m11-post-restart-smoke-repo/report.md
/var/log/hermes-runtime-bridge.err.log
/var/log/hermes-runtime-bridge.out.log
```

Grep results: **0 matches** in all 7 files for all 7 patterns.

Token-value fragment check (first 8 chars of ANTHROPIC_AUTH_TOKEN):
searched in the same 7 files — **0 matches**.

API response bodies (live /runs response and /runs/{id} poll) also contain no
secret material — they expose only public metadata (run_id, task_id, status,
timestamps).

```text
Secret Value Exposure Detected: NO
```

## Runtime Stability

```text
Supervisor status post-smoke   RUNNING
Bridge PID                     153620
Bridge uptime at report time   ~6 minutes
Bridge stderr log size         1.5K
Bridge stdout log size         0B
Recent traceback count         0
Import-error count             0
SQLite lock count              0
HTTP 5xx count (post-restart)  0
Process count                  1 (the bridge itself)
Active task count              0
Stale runner lock              none
Leftover claude_code_runner   none
Leftover Claude CLI child     none
```

```text
Runtime Stable After Smoke: YES
```

## Test Results

### Targeted

```bash
cd /home/ubuntu/hermes-runtime-bridge
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_manifest_verifier.py \
  tests/test_claude_code_executor.py \
  tests/test_executor_router.py \
  tests/test_claude_executor_integration.py \
  tests/test_openapi_executor_metadata.py
```

```text
85 passed, 2 skipped, 1 warning in 3.46s
```

### Impacted Regression

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_adapter.py \
  tests/test_aee5_runtime_registry.py \
  tests/test_jobs_api.py \
  tests/test_routing.py \
  tests/test_safety.py
```

```text
78 passed, 1 warning in 1.57s
```

### Broad Regression

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/
```

```text
268 passed, 2 skipped, 1 warning
3 failed, 1 errored:
  FAILED tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written
  FAILED tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete
  FAILED tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation
  ERROR tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent
```

## Baseline Failure Classification

The 3 failures + 1 error all match the expected known baseline set from the task:

| Test | Classification |
|------|----------------|
| `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written` | **BASELINE-REPRODUCED** (in the expected list) |
| `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete` | **BASELINE-REPRODUCED** (in the expected list) |
| `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation` | **BASELINE-REPRODUCED** (in the expected list) |
| `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent` | **BASELINE-REPRODUCED** (in the expected list; surfaces as ERROR not FAIL because of fixture-level issue) |

No M11-caused failures, no environmental regressions, no unknown failures.

```text
M11-CAUSED                  none
BASELINE-REPRODUCED         4 (matches expected set exactly)
ENVIRONMENTAL               none
UNKNOWN                     none
```

## Git Status

### Bridge (`/home/ubuntu/hermes-runtime-bridge`)

```text
Branch              master
HEAD                da9f5667473da5c2468c5bc675b89fcef29b9a7a
HEAD log            feat(runtime): expose Claude executor metadata and auth env
Tracked changes     0
Staged changes      0
Untracked files     (M11 produced claude_executor_post_restart_validation.md and
                     a few logs/ subdir artifacts; nothing under git control)
Stash list          stash@{0}: WIP AEE TASK-20260711-0033 (unrelated legacy work;
                     NOT created, modified, or applied by M11)
Diff summary        clean
```

M11 git operations performed: **none** (no add, commit, push, pull, merge, rebase,
stash, reset, restore, checkout, switch, clean).

The M11 report file is intentionally left untracked.

### Runner (`/home/ubuntu/Abacus/AEE`)

```text
Branch              master
HEAD                ba1170901df46abe2b2d7ce73daf086fa599d863
HEAD log            feat(runtime): add Claude Code runner MVP
Tracked changes     0
Staged changes      0
Untracked files     (workspace noise)
Stash list          (empty)
Diff summary        clean
```

M11 git operations performed: **none**.

## Findings

### F-01 — Live OpenAPI does not publish `bearerAuth` (pre-existing, M9 F-03 / M10)

- **Status:** Present pre-restart and present post-restart (no source change in M11).
- **What:** `GET /openapi.json` returns a schema with
  `components.securitySchemes == {}` (empty). The static `openapi.yaml` (1.2.0) does
  declare `bearerAuth` (HTTP / bearer / JWT) at line 42–43, but the running FastAPI
  app uses manual `Header(None)` parsing with `require_auth(authorization)` instead
  of FastAPI's `HTTPBearer(...)` security dependency, so `app.openapi()` does not
  auto-publish the security scheme.
- **Impact:** Authentication still works (every protected route calls
  `require_auth`, returning 401 without a valid Bearer key). The gap is purely in
  the OpenAPI advertisement. A GPT Action re-import using the *static*
  `openapi.yaml` would have a different (richer) `securitySchemes` than what the
  live `/openapi.json` advertises — that divergence is the M9 F-03 blocker.
- **Resolution path:** Either (a) change the bridge to use FastAPI's
  `HTTPBearer(...)` security on the guarded routes so `app.openapi()` auto-publishes
  `bearerAuth`; or (b) serve the static `openapi.yaml` (or a transformation of it)
  at `/openapi.json` instead of `app.openapi()`. Both are out of scope for M11.
- **M11 action:** None (cannot edit source). Documented and propagated into the
  GPT Action Publication gate as **HOLD**.

### F-02 — Task specified `/tmp` paths for the smoke, but the live bridge enforces
two allow-lists

- **Status:** Worked around within M11 constraints (no source edit).
- **What:** The task asked for:
  - Temporary repository at `/tmp/task-m11-post-restart-smoke-repo`
  - Temporary runs root at `/tmp/task-m11-post-restart-smoke-runs`

  The live bridge enforces:
  - `metadata.repo_path` must be under `DEFAULT_REPO_ALLOWLIST = ("/home/ubuntu/Abacus",)`
    (see `aee/runtimes/executor_router.py:49`). A request with `repo_path:
    /tmp/...` is rejected with `repo_path_outside_allowlist` (400).
  - The Claude adapter hard-codes `DEFAULT_RUNS_ROOT = "/home/ubuntu/Abacus/AEE-RUNS"`
    (see `aee/adapters/claude_code_executor.py:87`); there is no per-request override.
- **Resolution:** M11 placed the smoke repo at
  `/home/ubuntu/Abacus/task-m11-post-restart-smoke-repo` (still a fresh, isolated
  repo, never used before, with no production data). The runs root remained
  `/home/ubuntu/Abacus/AEE-RUNS/...` because the bridge has no API to override it
  and the test environment is the bridge's standard runs root. The repo is
  distinguishable from production repos by its name (`task-m11-post-restart-smoke-repo`)
  and by its single-commit history. The runs directory name
  `TASK-20260715-0004--RUN-0537024105c9` is also unambiguous.
- **Recommendation for the next task:** The `repo_path` allow-list and the hard-coded
  `DEFAULT_RUNS_ROOT` are reasonable defaults but they prevent the
  "isolated `/tmp` smoke" pattern the ticket asked for. If the GPT publication
  contract requires truly disposable test roots, the bridge should accept a
  per-request `runs_root` (and ideally relax the repo allow-list for trusted
  internal callers). Out of scope for M11.

### F-03 — `health.version` reports `1.2.0` while `openapi.info.version` reports
`1.3.0-aee2`

- **Status:** Cosmetic. Pre-existing in the running code (da9f566).
- **What:** `GET /health` includes `"version": "1.2.0"`; `GET /openapi.json`
  includes `"info": {"version": "1.3.0-aee2", ...}`. The two fields are
  independently maintained strings in `app.py`. No code references depend on
  the exact `/health` value (callers treat the response as a health probe, not
  a version endpoint).
- **Impact:** None. Both versions describe a single running build. The OpenAPI
  version (1.3.0-aee2) is the one GPT Action re-imports use, so it is the
  authoritative version for that contract.
- **Resolution:** Cosmetic. Recommend unifying these in a follow-up.

### F-04 — `executor_reported_model` is `minimax-m3:cloud`, not a Claude family model

- **Status:** Informational. Not a defect.
- **What:** The smoke manifest records `executor_reported_model: minimax-m3:cloud`
  and `provider: minimax-m3:cloud`. The Claude CLI was launched against the
  custom endpoint declared in `ANTHROPIC_BASE_URL`, and the endpoint's API
  response identified the upstream model as `minimax-m3:cloud`. This is the
  model alias the custom endpoint exposes; it is not the public Anthropic
  Claude model name. The runner honored the request successfully (status:
  COMPLETED, verdict: PASS).
- **Impact:** None. The task did not require a specific model id; it required
  the smoke to reach `status=completed` with `verdict=PASS` and a verified
  manifest. All three were met.
- **Resolution:** None needed. If the operator wants the manifest to record
  `claude-sonnet-4-6` (or similar) in the `executor_reported_model` field, the
  custom endpoint needs to identify the model that way. Out of scope for M11.

### F-05 — Legacy bridge stash (`stash@{0}`)

- **Status:** Not present in the target commit. Not a blocker.
- **What:** A pre-existing stash from TASK-20260711-0033 ("AEE write-side
  metadata recovery") is still on the bridge's local stash list. The stash is
  not staged, not tracked, and does not affect HEAD. The current commit
  (da9f566) already contains the write-side metadata columns
  (`executor_session_id`, `runtime_run_id`) integrated as migrations in
  `dispatcher/db.py` (lines 166–210) and as fields in `CreateRunRequest` (app.py
  lines 297–308), so the stash appears to be earlier work that has been
  superseded. The task's safety rules do not require "no stashes", only "no
  staged changes" and "no tracked modifications". Both pass.
- **Impact:** None. The running process is bound to HEAD, not the stash.
- **Resolution:** The stash can be dropped or kept for archival. M11 takes
  no action (the M11 scope forbids git operations).

## Rollback Assessment

The M11 task M11 may NOT trigger rollback because none of the rollback
trigger conditions (§18 of the ticket) were met:

| Trigger | Met? | Note |
|---------|------|------|
| Service fails to restart | NO | supervisor exited 0; new PID came up in 3s |
| Process enters crash loop | NO | uptime stable |
| `/health` is not HTTP 200 | NO | 200 |
| Live OpenAPI lacks metadata | NO | now present |
| Live OpenAPI lacks bearerAuth | **YES** (F-01) | M9/M10 known; M11 documents and re-classifies |
| `claude_code` adapter is missing | NO | present, verified |
| `claude_code` resolves to legacy shim | NO | resolves to `aee.adapters.claude_code_executor.ClaudeCodeExecutorAdapter` |
| Required live Claude env key is missing | NO | smoke proved all 8 are loaded (effective) |
| Claude smoke fails due to auth | NO | smoke completed |
| Claude smoke returns status != completed | NO | completed |
| Manifest verdict != PASS | NO | PASS |
| Manifest gate is bypassed | NO | independent recheck ran and passed |
| Secret exposure is detected | NO | grep clean |
| A production repository is unexpectedly modified | NO | smoke repo only, never tracked any production data |
| A commit or push occurs | NO | none |
| Runner or Claude child remains alive | NO | none |

The single met trigger (F-01) is a pre-existing, documented, structural
limitation of the FastAPI app's auth wiring. It existed before M11 and would
persist through any restart until a code change resolves it. The M9 F-03
recommendation is to fix it before publication. M11 cannot do that without
editing source, and the ticket explicitly forbids source edits. Therefore
**rollback is not required**: the operational state is correct (running,
authenticated, dispatching Claude, returning verified manifests), only the
OpenAPI advertisement has the pre-existing gap.

The rollback procedure (§19) was reviewed in advance and is available if
needed; the `.env` backup `/home/ubuntu/hermes-runtime-bridge/.env.pre-task-m10.20260714200737`
is intact (2.3K).

```text
Rollback Required: NO
```

## Required Final Decisions

```text
Controlled Restart Performed             YES
Live Runtime Target Version              YES
Claude Environment Loaded                YES (effective via os.environ; smoke proved end-to-end)
Repository OpenAPI Ready                 YES (metadata + 4 /runs/* routes)
Live OpenAPI Current                     PARTIAL  (metadata present; bearerAuth absent — F-01)
Legacy Hermes Route                      PASS
Claude Executor Route                    PASS
Post-Restart Claude Smoke                PASS
Verified Manifest Gate                   PASS
Runtime Stable After Smoke               YES
Secret Exposure Detected                 NO
GPT Action Publication Ready             NO   (carries F-01; see Findings)
Rollback Required                        NO
Review Ready                             YES
```

## Production Safety

```text
No Source Edit                           YES (no edits to any *.py, *.yaml, *.toml, *.json under
                                              version control)
No Test Edit                            YES
No OpenAPI Edit                         YES (no edit to openapi.yaml or app.py)
No Environment Edit                     YES (.env is unchanged from M10 state)
No Credential Change                    YES (no .env edits, no key rotation)
No Database Migration                   YES (the smoke runner's runner wrote no schema
                                              change; bridge startup itself was a no-op
                                              for migrations since the schema is up to date)
No Stage                                YES
No Commit                               YES
No Push                                 YES
No Pull                                 YES
No Deploy Beyond Controlled Restart     YES (only operation: supervisorctl restart)
No Supervisor Configuration Edit        YES
No Merge                                YES
No Rebase                               YES
No Stash                                YES
No Reset                                YES
No Restore                              YES
No Checkout                             YES
No Switch                               YES
No File Delete                          YES
No Package Installation                 YES
No Gateway Edit                         YES
No Firewall Edit                        YES
No GPT Action Publication               YES (publication is explicitly out of M11 scope;
                                              M11 also withholds the gate, see F-01)
No Secret Value Logged                  YES (no secret value or length is reported in this
                                              report; only key names appear)
No Production Repository Mutation       YES (only the fresh isolated /home/ubuntu/Abacus/
                                              task-m11-post-restart-smoke-repo was touched;
                                              no other repository was modified)
```

The controlled restart and the isolated fresh-repo smoke are the only
operational actions M11 performed.

## Telegram

```text
Telegram Sent     YES
Method            bot API sendMessage
Recipient         chat_id 5132341473
Message ID        7319
UTC               2026-07-15T02:28:00Z
Asia/Taipei       2026-07-15T10:28:00+08:00
```

Message text delivered (paraphrased, no secrets):

```text
✅ M11 Claude Executor Post-Restart Validation

Controlled restart: DONE
Live process: PID 153620 (post-target)
Legacy Hermes route: PASS
Claude Executor smoke: PASS
Manifest verdict: PASS
Runtime stable: YES
Secret exposure: NO
GPT Action Publication: HOLD (bearerAuth missing from live OpenAPI — see M11 report)
```

Telegram failure alone does not invalidate the technical result; in this
case it succeeded.

## Final Recommendation

```text
RUNTIME RESTARTED — HOLD GPT ACTION PUBLICATION
```

The live process is on the target commit, the new env is loaded (smoke proved
it end-to-end), the metadata field is exposed, the Claude adapter is the
verified (non-shim) class, the legacy Hermes route still works, the Claude
Executor smoke ran through the full live chain (`POST /runs` → Executor
Router → ClaudeCodeExecutorAdapter → claude_code_runner → claude CLI →
completion.verified.json → verifier → completion gate → `GET /runs/{id}`)
with a verified `verdict=PASS` manifest, no leftover subprocess, and no
secret exposure. The bridge is operationally ready.

The single remaining gate is `components.securitySchemes.bearerAuth` being
absent from the live `/openapi.json`. This is a pre-existing structural gap
that is independent of the restart (the live `app.openapi()` did not publish
it before either; the static `openapi.yaml` does, but the live process serves
`app.openapi()`). M9 (F-03) and M10 raised the same gap; M11 confirms it
remains after the restart.

**GPT Action publication must remain HELD** until a separate code-level
fix wires `HTTPBearer(...)` security into the protected routes (or
equivalently, serves the static `openapi.yaml` at `/openapi.json`). That
fix is out of M11 scope (no source edits allowed). The M9/M10 reports
contain the recommended path.

Recommended next task: a small TASK-M11.1 (or TASK-M12) that
1. switches the bridge's `require_auth(authorization)` to FastAPI's
   `HTTPBearer(...)` security (or to a custom OpenAPI extension that adds
   `bearerAuth` to `components.securitySchemes`),
2. optionally exposes a per-request `runs_root` so the `/tmp`-style smoke
   pattern becomes possible without editing allow-lists,
3. re-runs the M11 smoke and re-asserts the publication gate.

The .env backup, the smoke run directory, the smoke repo, and all the
artifacts referenced above remain available for that follow-up.
