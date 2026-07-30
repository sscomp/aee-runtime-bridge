# Claude Auth Environment Provisioning & Pre-Restart Validation Report

**Task**: `TASK-M10 — Claude Auth Environment Provisioning & Pre-Restart Validation`
**Parent Task**: `TASK-M9 — Claude Executor Integration Deployment Readiness Review`
**Blocker Addressed**: `F-02 — CLAUDE_AUTH_ENV_MISSING_FROM_DOTENV`
**Type**: Controlled configuration update and pre-restart validation
**Repository (bridge)**: `/home/ubuntu/hermes-runtime-bridge`
**Repository (runner)**: `/home/ubuntu/Abacus/AEE`
**Bridge HEAD at start**: `da9f5667473da5c2468c5bc675b89fcef29b9a7a`
**Bridge HEAD at end**:   `da9f5667473da5c2468c5bc675b89fcef29b9a7a` (no commit, no push)
**Runner HEAD at start**: `ba1170901df46abe2b2d7ce73daf086fa599d863`
**Runner HEAD at end**:   `ba1170901df46abe2b2d7ce73daf086fa599d863` (no commit, no push)
**Primary deliverable**: This file
**Mode**: Read-only inspection except for the targeted `.env` update

> **Safety** — no secret value is printed in this report. Variables are reported as `PRESENT` / `NOT_PRESENT` / `UPDATED`. Token values are referenced by name and SHA-256 of the file, never by value.

---

## 1. Execution Timing

| Field | Value (UTC) |
| --- | --- |
| Start UTC | 2026-07-14T20:06:30Z |
| End UTC   | 2026-07-14T20:13:00Z |
| Start Asia/Taipei | 2026-07-15T04:06:30+0800 |
| End Asia/Taipei   | 2026-07-15T04:13:00+0800 |
| Duration Seconds  | ~390 |
| Duration MM:SS    | 06:30 |

Earliest timestamp is the baseline verification commands. End timestamp is the moment the report write completed.

---

## 2. Overall Verdict

```text
Overall Verdict: PASS — AUTH ENV READY, ISOLATED SMOKE PASSED
```

The deployment blocker `F-02 — CLAUDE_AUTH_ENV_MISSING_FROM_DOTENV` is resolved. The minimum required Claude authentication and configuration variables are provisioned in `.env`. The variables load via uvicorn's `--env-file` mechanism, are forwarded by `build_runner_environment()` to the Runner subprocess, and the isolated real Runner smoke completed with `verdict=PASS` and the required artifact at the expected SHA-256. No secret value was exposed in any artifact, log, report, or process argument. The live service was not restarted.

---

## 3. Baseline

### Hermes Runtime Bridge (`/home/ubuntu/hermes-runtime-bridge`)

```text
Branch:           master
HEAD:             da9f5667473da5c2468c5bc675b89fcef29b9a7a
HEAD subject:     feat(runtime): expose Claude executor metadata and auth env
Status (short):   clean tracked/staged; 16 untracked reports/data files (pre-existing, unchanged)
Staged files:     none
Stash:            stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery (unmodified, pre-existing)
```

### Runner (`/home/ubuntu/Abacus/AEE`)

```text
Branch:           master
HEAD:             ba1170901df46abe2b2d7ce73daf086fa599d863
HEAD subject:     feat(runtime): add Claude Code runner MVP
Status:           clean tracked/staged; only untracked items in parent dirs
Stash:            empty
```

Baseline matches the task contract. PASS.

---

## 4. M9 Blocker Addressed

`F-02 — CLAUDE_AUTH_ENV_MISSING_FROM_DOTENV` (claude_executor_deployment_readiness_review.md §20):

> `.env` (hermes-runtime-bridge) — `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_*`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` and the three `ANTHROPIC_DEFAULT_*_MODEL` aliases are all absent from `.env` (and from `.env.example`); `build_runner_environment` is a strict allow-list pass-through. First `claude_code` task after restart will fail with no auth / no base URL; the manifest gate will then downgrade it to `failed`.

**Resolution**: Eight required Claude env vars added to `.env` under a clearly marked `# TASK-M10 — Claude Executor runtime environment` section, preserving all pre-existing bridge auth and MiniMax-M3 routing variables.

---

## 5. Credential Source Review

Inspection was performed by reading the parent shell env that already had the working values set (the host Claude CLI is in active use by other workloads; this is the same source the M6 smoke used successfully).

| Variable | Parent shell env (presence only) | .env (pre-M10) | Source |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | NOT PRESENT | NOT PRESENT | N/A |
| `ANTHROPIC_AUTH_TOKEN` | PRESENT | NOT PRESENT | Parent env (custom-endpoint token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | NOT PRESENT | NOT PRESENT | N/A |
| `CLAUDE_CODE_API_KEY` | NOT PRESENT | NOT PRESENT | N/A |
| `CLAUDE_CODE_ENTRYPOINT` | PRESENT | NOT PRESENT | Parent env (`cli`) |
| `CLAUDE_CONFIG_DIR` | NOT PRESENT | NOT PRESENT | N/A |
| `ANTHROPIC_BASE_URL` | PRESENT | NOT PRESENT | Parent env (`https://ollama.com` — non-secret config) |
| `ANTHROPIC_MODEL` | PRESENT | NOT PRESENT | Parent env (model alias) |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | PRESENT | NOT PRESENT | Parent env (model alias) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | PRESENT | NOT PRESENT | Parent env (model alias) |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | PRESENT | NOT PRESENT | Parent env (model alias) |
| `CLAUDE_CODE_EXECPATH` | PRESENT | NOT PRESENT | Parent env (CLI path) |

The parent shell env values were captured by a Python script (`/tmp/m10_capture.py`) into `/tmp/m10_env_values.json` with mode `0600` (ubuntu:ubuntu). The capture script printed only `PRESENT` / `NOT_PRESENT` markers. No values were echoed to stdout, the report, or git history.

Authentication mode selected: **Existing `ANTHROPIC_AUTH_TOKEN` + custom `ANTHROPIC_BASE_URL` (custom-endpoint mode)**, per the M6 §5 / M7 §5 selection precedence and confirmed by the M6 successful smoke evidence.

---

## 6. Authentication Mode Selected

```text
Mode: ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL (custom-endpoint)
Precedence rank: 1 (per TASK-M10 §6)
```

The parent env confirms the host Claude CLI is currently authenticating against a custom endpoint. The same mode succeeded in TASK-M6 (smoke verdict PASS, executor provider `minimax-m3:cloud`). No credential was rotated; values are copied from the active parent env that the installed Claude CLI is already using.

---

## 7. Required Environment Matrix

| Variable | In `.env` (post) | Required | Classification | Source |
| --- | --- | --- | --- | --- |
| `ANTHROPIC_AUTH_TOKEN` | YES | YES | REQUIRED | Parent env (already-in-use custom-endpoint token) |
| `ANTHROPIC_BASE_URL` | YES | YES | REQUIRED | Parent env (non-secret URL — required per M6 evidence: without it, the CLI falls back to the public Anthropic API and the custom token returns HTTP 401) |
| `ANTHROPIC_MODEL` | YES | YES | REQUIRED | Parent env (model alias override) |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | YES | YES | REQUIRED | Parent env (model alias override) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | YES | YES | REQUIRED | Parent env (model alias override) |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | YES | YES | REQUIRED | Parent env (model alias override) |
| `CLAUDE_CODE_ENTRYPOINT` | YES | YES | REQUIRED | Parent env (`cli`) |
| `CLAUDE_CODE_EXECPATH` | YES | YES | REQUIRED | Parent env (CLI binary path) |
| `ANTHROPIC_API_KEY` | NO | NO | NOT USED | Parent env not present; would be alternative mode |
| `CLAUDE_CODE_OAUTH_TOKEN` | NO | NO | NOT USED | Parent env not present; would be alternative mode |
| `CLAUDE_CODE_API_KEY` | NO | NO | NOT USED | Parent env not present; would be alternative mode |
| `CLAUDE_CONFIG_DIR` | NO | NO | NOT USED | Parent env not present; not required by current CLI setup |
| `PATH` | NO (inherited from shell) | NO | N/A | uvicorn inherits from supervisor process env |
| `HOME` | NO (set by supervisor `environment=`) | NO | N/A | Already set by supervisor |
| `LANG`, `LC_ALL` | NO | NO | N/A | Not required by current setup |
| `PYTHONPATH` | NO (inherited) | NO | N/A | Inherited by uvicorn |

`PATH`, `HOME`, `LANG`, `LC_ALL`, and `PYTHONPATH` are not duplicated in `.env` per TASK-M10 §7 (they remain service/runtime vars; supervisor already passes them through).

---

## 8. `.env` Backup and Mutation Evidence

### Pre-mutation inspection

```text
File:    /home/ubuntu/hermes-runtime-bridge/.env
Size:    2360 bytes
Owner:   ubuntu (uid 1000)
Group:   ubuntu (gid 1000)
Mode:    0600
Git:     .gitignore:13 matches .env → git check-ignore confirms ignored
Tracked: NO (git ls-files --error-unmatch .env → "did not match any file(s) known to git")
```

### Backup created

```text
Path:    /home/ubuntu/hermes-runtime-bridge/.env.pre-task-m10.20260714200737
Size:    2360 bytes (identical to pre-mutation .env)
Owner:   ubuntu:ubuntu
Mode:    0600
Git:     NOT tracked (not in .gitignore's negative list either; backup is a sibling dotfile and not added to git)
```

### Mutation summary (key names only)

Keys **added** to `.env` under `# TASK-M10 — Claude Executor runtime environment`:

```text
ANTHROPIC_AUTH_TOKEN
ANTHROPIC_BASE_URL
ANTHROPIC_MODEL
ANTHROPIC_DEFAULT_SONNET_MODEL
ANTHROPIC_DEFAULT_OPUS_MODEL
ANTHROPIC_DEFAULT_HAIKU_MODEL
CLAUDE_CODE_ENTRYPOINT
CLAUDE_CODE_EXECPATH
```

Keys **preserved** (unchanged from pre-mutation): 16 keys (BRIDGE_HOST, BRIDGE_PORT, BRIDGE_API_KEY, GPT_BRIDGE_API_KEY, CLAUDE_BRIDGE_API_KEY, CURSOR_BRIDGE_API_KEY, MCP_BRIDGE_API_KEY, HERMES_BASE_URL, HERMES_API_KEY, DEFAULT_SESSION_ID, ALLOWED_SESSION_PREFIXES, MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL, MINIMAX_ALLOW_CALLER_OVERRIDE, BRIDGE_DEFAULT_MODEL).

Insertions: 9 lines (1 blank line + 1 header comment + 8 KEY=VALUE lines — 8 new keys plus the section header). No deletions. No duplicates. No empty keys. No whitespace-only keys (all 8 provisioned values are non-empty and non-whitespace — verified via `dotenv_values(...).get(k, '')` with length > 0 in §11 below).

### Mutation mechanism

The mutation was performed by `/tmp/m10_update_env.py` (mode 0600, ubuntu:ubuntu), which:

1. Read source values from `/tmp/m10_env_values.json` (also 0600).
2. Parsed `.env` and preserved all existing records.
3. Removed any pre-existing TASK-M10 section (idempotent re-run safety).
4. Appended the new section with 8 KEY=VALUE pairs.
5. Wrote to `.env.tmp-m10`, set 0600 ubuntu:ubuntu, then `os.replace()` onto `.env`.

No values were ever passed through a shell command, command-line argument, or printed to stdout.

---

## 9. `.env` Ownership and Permission Verification

Post-mutation:

```text
Path:    /home/ubuntu/hermes-runtime-bridge/.env
Size:    2801 bytes
Owner:   ubuntu (uid 1000)
Group:   ubuntu (gid 1000)
Mode:    0600
Git:     ignored (git check-ignore -v .env → .gitignore:13:.env .env)
Tracked: NO
```

`uvicorn` is launched as user `ubuntu` (per `user=ubuntu` in `[program:hermes-runtime-bridge]` block), so the 0600 mode is readable by the runtime. No ownership mismatch was found.

The backup `/home/ubuntu/hermes-runtime-bridge/.env.pre-task-m10.20260714200737` is also `0600 ubuntu:ubuntu`.

---

## 10. Service Environment Load Verification

Without restarting, configuration was inspected to confirm the next restart will load the updated `.env`.

| Check | Result | Evidence |
| --- | --- | --- |
| Active supervisor program command includes `--env-file /home/ubuntu/hermes-runtime-bridge/.env` | YES | `/etc/supervisor/conf.d/hermes-runtime-bridge.conf` (also inlined into `/etc/supervisor/conf.d/openclaw.conf` per M9 §4) |
| Runtime user is `ubuntu` | YES | `user=ubuntu` in conf |
| Runtime working directory is `/home/ubuntu/hermes-runtime-bridge` | YES | `directory=/home/ubuntu/hermes-runtime-bridge` |
| `.env` readable by `ubuntu` (mode 0600) | YES | stat confirmed 0600 ubuntu:ubuntu |
| No supervisor-level `environment=` override that empties Claude vars | YES | The only `environment=` in `openclaw.conf` sets `STNORESTART=1,HOME=/home/ubuntu,STNOUPGRADE=1` — none touch Claude vars |
| No wrapper script clears the environment | YES | The supervisor command invokes `uvicorn app:app` directly with no shell wrapper |
| Uvicorn supports `--env-file` behavior | YES | The live process's stderr log shows `INFO: Loading environment from '/home/ubuntu/hermes-runtime-bridge/.env'` (captured at the original process start on 2026-07-14 05:15:33) |
| `app.py` does not delete Claude vars from `os.environ` | YES | `app.py` reads env vars only; no `os.environ.pop` / `del os.environ` calls in the source |
| uvicorn-compatible parser (python-dotenv) loads all 8 vars as non-empty | YES | Tested: `dotenv_values('.env')` returns all 8 with `len > 0` and non-whitespace |

Classification:

```text
Environment Will Load After Restart: YES
```

---

## 11. Runner Environment Projection

A controlled Python process loaded `.env` via `dotenv_values` (the same library uvicorn uses for `--env-file`) and called `build_runner_environment(os.environ)` with the parent env augmented by 5 unrelated secrets (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, DATABASE_URL, BRIDGE_API_KEY, GPT_BRIDGE_API_KEY — synthetic test values) to verify the allow-list isolation.

### Forwarded keys (after projection)

| Key | In `.env` | Loaded by uvicorn parser | Forwarded to Runner | Required |
| --- | --- | --- | --- | --- |
| `ANTHROPIC_AUTH_TOKEN` | YES | YES | YES | YES |
| `ANTHROPIC_BASE_URL` | YES | YES | YES | YES |
| `ANTHROPIC_MODEL` | YES | YES | YES | YES |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | YES | YES | YES | YES |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | YES | YES | YES | YES |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | YES | YES | YES | YES |
| `CLAUDE_CODE_ENTRYPOINT` | YES | YES | YES | YES |
| `CLAUDE_CODE_EXECPATH` | YES | YES | YES | YES |
| `PATH` (PASS_THROUGH_BASE) | n/a (shell) | YES | YES | YES (infrastructure) |
| `HOME` (PASS_THROUGH_BASE) | n/a (supervisor) | YES | YES | YES (infrastructure) |
| `LANG` (PASS_THROUGH_BASE) | n/a (shell) | YES | YES | NO (currently forwarded because present) |
| `LC_ALL` (PASS_THROUGH_BASE) | n/a (shell) | YES | YES | NO (currently forwarded because present) |
| `PYTHONPATH` (PASS_THROUGH_BASE) | n/a (shell) | YES | YES | NO (currently forwarded because present) |

### Disallowed variables (must NOT be forwarded)

| Key | Source | Forwarded? |
| --- | --- | --- |
| `AWS_SECRET_ACCESS_KEY` | Synthetic test | NO (OMITTED — good) |
| `GITHUB_TOKEN` | Synthetic test | NO (OMITTED — good) |
| `DATABASE_URL` | Synthetic test | NO (OMITTED — good) |
| `BRIDGE_API_KEY` | Synthetic test | NO (OMITTED — good) |
| `GPT_BRIDGE_API_KEY` | Synthetic test | NO (OMITTED — good) |

### Empty / whitespace handling

`build_runner_environment()` performs a truthy check (`if v:`), which means:

- Empty values (`""`) → OMITTED
- Whitespace-only values (`"   "`) → **forwarded** (a string of spaces is truthy in Python)

The TASK-M10 §11 contract offers two options for whitespace forwarding. We chose **Option A — No source change**: all 8 provisioned values are non-empty and non-whitespace (verified: each `len(dotenv_values('.env')[k])` is in {3, 16, 16, 16, 16, 18, 49, 57}; all have non-whitespace content). The risk that an operator later inserts a whitespace-only value is documented in §22 (Remaining Risks). A follow-up hardening ticket (Option B) is recommended but is not part of M10.

```text
Runner Environment Projection Verified: YES
```

---

## 12. Secret Exposure Review

Scanned (key-name patterns only; never inserted a real value into a grep):

```text
ANTHROPIC_API_KEY=
ANTHROPIC_AUTH_TOKEN=
CLAUDE_CODE_OAUTH_TOKEN=
CLAUDE_CODE_API_KEY=
Bearer 
Authorization: 
os.environ
environment=
```

| File | Hits |
| --- | --- |
| `/tmp/task-m10-auth-smoke-runs/TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE/stdout.log` | 0 |
| `/tmp/task-m10-auth-smoke-runs/TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE/stderr.log` | 0 |
| `/tmp/task-m10-auth-smoke-runs/TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE/completion.claim.json` | 0 |
| `/tmp/task-m10-auth-smoke-runs/TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE/completion.verified.json` | 0 |
| `/tmp/task-m10-auth-smoke-repo/report.md` | 0 |
| `/tmp/m10-smoke-summary.json` | 0 |
| `/var/log/hermes-runtime-bridge.out.log` | 0 |
| `/var/log/hermes-runtime-bridge.err.log` | 0 |

Independent value-fragment check (8-char prefix of `ANTHROPIC_AUTH_TOKEN`, the actual secret): searched the same files; **0 hits**. The other 8-char fragment hits (model alias `minimax-m3:cloud` in stdout.log and the manifests) are non-secret config that legitimately appears in the executor's `provider` / `session_effective_model` / `requested_model` fields — the alias is not classified as a credential and is by design recorded in the manifest.

`git diff .env.pre-task-m10.20260714200737 .env` shows only key names (no values revealed): the diff is line-based on `KEY=VALUE`; this report never quotes any value.

Classification:

```text
Secret Value Exposure Detected: NO
Credential Name Exposure Only:   YES (in this report — documentation of variable names; values are not present)
```

---

## 13. Real Runner Smoke

### Isolation

| Resource | Value |
| --- | --- |
| Temp repository | `/tmp/task-m10-auth-smoke-repo` (git init, master branch, single README.md initial commit) |
| Runs root | `/tmp/task-m10-auth-smoke-runs` (only this run's directory created) |
| Task ID | `TASK-M10-AUTH-ENV-SMOKE` |
| Run ID  | `RUN-M10-AUTH-ENV-SMOKE` |
| Mode | `coding` |
| Timeout | 300 s |
| Working mode | `existing_worktree` |
| Required artifact | `report.md` |
| Brief | Create `report.md` containing exactly the line `TASK_M10_AUTH_ENV_SMOKE=PASS` |
| Production `/runs` endpoint | NOT used (smoke was driven directly through `ClaudeCodeExecutorAdapter.submit()` in a Python driver) |
| Production DB | NOT used (driver passes a `_Job` shim, not a DB row) |
| Service restart | NOT performed (PID 4414 uptime unchanged at 14:55:35) |
| Production run directory | NOT used (`/home/ubuntu/Abacus/AEE-RUNS` was not touched) |

### Execution path

```text
/tmp/m10_smoke_driver.py
  ↓
ClaudeCodeExecutorAdapter.submit(job)
  ↓
build_runner_environment(os.environ)
  ↓
python3 -m scripts.claude_code_runner --task-id TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE --repo-path /tmp/task-m10-auth-smoke-repo --mode coding --timeout 300 --runs-root /tmp/task-m10-auth-smoke-runs --required-artifact report.md
  ↓
installed claude CLI (v2.1.209, provider minimax-m3:cloud, session_effective_model minimax-m3:cloud)
  ↓
completion.claim.json
completion.verified.json
/tmp/task-m10-auth-smoke-repo/report.md
```

### Required assertions

| Assertion | Result |
| --- | --- |
| `submit()` returns promptly | PASS (returned at 20:10:21Z; process started within 2 s) |
| Adapter name is `claude_code` | PASS (`adapter.name == "claude_code"`, `adapter.runtime_type == "claude_code"`) |
| No fallback to Hermes (only one adapter registered) | PASS — only `claude_code` was used; no Hermes adapter constructed |
| Process reaches terminal state | PASS (status=`completed` at 20:10:27Z, 6 s after submit) |
| Adapter status is `completed` | PASS |
| Manifest exists | PASS (`/tmp/task-m10-auth-smoke-runs/TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE/completion.verified.json`) |
| Manifest is regular, non-symlink | PASS (verified by `regular_file: true` in the manifest's own `artifacts[0]` block and direct stat) |
| Manifest schema version is `1.0.0` | PASS |
| Top-level `status == "COMPLETED"` | PASS |
| Top-level `verdict == "PASS"` | PASS |
| `executor.exit_code == 0` | PASS |
| `executor.is_error == false` | PASS |
| `executor.subtype == "success"` | PASS |
| `executor.terminal_reason == "completed"` | PASS |
| Required artifact exists | PASS (`/tmp/task-m10-auth-smoke-repo/report.md`, 29 bytes) |
| Artifact content is exactly `TASK_M10_AUTH_ENV_SMOKE=PASS\n` | PASS (29 bytes match) |
| Artifact SHA-256 matches manifest | PASS (manifest: `0d64fb710938d28a13822d42755ad222c62f1bdab174813a2c97c5b3584677e1`; recomputed: same) |
| Claim-manifest SHA-256 matches | PASS (manifest: `4d296835651cef96928be382be7b10bb5fa9382fc7ac312d5603a9483ab126ef`; recomputed from disk: same) |
| `process_group.verified_dead == true` | PASS |
| `verification.verification_errors == []` | PASS |
| `safety.violations == []` | PASS |
| No commit was created by the Runner | PASS (`git.head_before == git.head_after == df1a4ef07446cfe91aa5fdcdb9404da5d15a22ea`; `git.commit` is null) |
| No push was performed | PASS (no network push attempted; only the in-process git is in play) |
| No leftover Runner or Claude child process | PASS (`pgrep -af "claude_code_runner.*TASK-M10"` matched only the inspecting shell command itself, not the Runner) |
| No secret value in stdout/stderr/manifest/report | PASS (verified in §12) |

### Manifest observations

- `executor.cli_path`: `/home/ubuntu/.local/bin/claude`
- `executor.cli_version`: `2.1.209 (Claude Code)`
- `executor.provider`: `minimax-m3:cloud`
- `executor.session_effective_model`: `minimax-m3:cloud`
- `executor.session_id`: `883da73a-0737-42cb-8a18-27bdb99ed1f4` (per-run, not a leaked secret)
- `executor.num_turns`: 2
- `executor.total_cost_usd`: 0.03186
- `verification.performed_by`: `claude_code_runner`
- `safety.commit_allowed`: false; `safety.commit_performed`: false; `safety.violations`: `[]`

Independent verification was re-run via `verify_completion_manifest(verified_manifest_path=…, expected_task_id=…, expected_run_dir=…, repo_path=…, required_artifacts=["report.md"], subprocess_exit_code=0)` — returned `verified=True` with empty error list.

```text
Real Runner Smoke Passed: YES
```

---

## 14. Manifest Verification

| Check | Result |
| --- | --- |
| Verified manifest path | `/tmp/task-m10-auth-smoke-runs/TASK-M10-AUTH-ENV-SMOKE--RUN-M10-AUTH-ENV-SMOKE/completion.verified.json` |
| Manifest SHA-256 (file) | not recorded in this report; verifiable via `sha256sum <path>` |
| `executor.cli_path` resolves to installed CLI | YES (`/home/ubuntu/.local/bin/claude`, `command -v claude` matches) |
| `executor.cli_version` matches host CLI version | YES (both `2.1.209 (Claude Code)`) |
| `artifacts[0].path` matches required artifact | YES (`report.md`) |
| `artifacts[0].sha256` matches recomputed | YES |
| `artifacts[0].inside_allowed_directory` | YES |
| `artifacts[0].regular_file` | YES |
| `git.head_before == git.head_after` | YES (no commit) |
| `process_group.verified_dead` | YES |
| `safety.violations` empty | YES |

---

## 15. Process Cleanup Verification

| Probe | Result |
| --- | --- |
| `pgrep -af "claude_code_runner.*TASK-M10"` | 0 leftover Runner subprocesses (only the pgrep's own shell wrapper matched) |
| `pgrep -af "claude.*TASK-M10"` | 0 leftover Claude child processes |
| `ps -p <claude_code_runner_pid>` | process is no longer present |
| `ps -p <claude_cli_pid>` | process is no longer present (CLI was invoked as a one-shot and exited) |
| Adapter in-flight state | cleaned up (`_drop_inflight()` was called by the adapter on the terminal path; subsequent `poll()` would raise `UnknownExternalRunError` per the adapter's documented contract) |

---

## 16. Current Live Process State

| Check | Result |
| --- | --- |
| Live PID | 4414 (unchanged) |
| Live ELAPSED | 14:55:35 (at audit time) — same order of magnitude as the 14:51:10 recorded by M9 §4; supervisor reports the same uptime window |
| Live command | `/home/ubuntu/hermes-runtime-bridge/.venv/bin/python /home/ubuntu/hermes-runtime-bridge/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787 --env-file /home/ubuntu/hermes-runtime-bridge/.env --no-access-log` (unchanged) |
| Live working dir | `/home/ubuntu/hermes-runtime-bridge` (unchanged) |
| Source on disk | `da9f5667473da5c2468c5bc675b89fcef29b9a7a` (unchanged) |
| Live `/openapi.json` version | `1.3.0-aee2` (pre-target, no `metadata` field on `CreateRunRequest`, no `bearerAuth` in securitySchemes — confirms the live process has NOT been restarted) |
| Live `/health` | 200, `version=1.2.0` |
| Live process env (inherited) | unchanged from M9 §9 (live process env still lacks the 8 new Claude vars because no restart has occurred; the M10 update is in `.env` only) |
| Production DB | NOT modified |
| Production `/runs` request | NOT made |
| Production run directory (`/home/ubuntu/Abacus/AEE-RUNS`) | NOT modified |

```text
Live process still PRE-TARGET
Restart Performed: NO
```

---

## 17. Test Results

### Targeted

```text
$ cd /home/ubuntu/hermes-runtime-bridge
$ PYTHONPATH=. .venv/bin/pytest -q \
    tests/test_claude_code_executor.py \
    tests/test_executor_router.py \
    tests/test_claude_executor_integration.py \
    tests/test_openapi_executor_metadata.py
…
59 passed, 2 skipped, 1 warning in 3.42s
```

The 2 skipped tests are pre-existing `pytest.skip` markers (unrelated to M10). Auth helper-focused tests within this set (allow-listed auth forwarding, config forwarding, unrelated secret rejection, no full env copy, no argv / raw-result / exception / routing-decision secret leakage) all pass.

### Impacted regression

```text
$ PYTHONPATH=. .venv/bin/pytest -q \
    tests/test_adapter.py \
    tests/test_aee5_runtime_registry.py \
    tests/test_jobs_api.py \
    tests/test_routing.py \
    tests/test_safety.py
…
78 passed, 1 warning in 1.60s
```

### Broad regression

```text
$ PYTHONPATH=. .venv/bin/pytest -q tests/
…
3 failed, 268 passed, 2 skipped, 1 error, 1 warning in 18.49s
```

| Failure | File | Classification |
| --- | --- | --- |
| `test_log_file_written` | `tests/test_dispatcher.py` | BASELINE-REPRODUCED |
| `test_task_json_written_on_complete` | `tests/test_dispatcher.py` | BASELINE-REPRODUCED |
| `test_log_file_lands_in_canonical_logs_dir_after_external_mutation` | `tests/test_dispatcher.py` | BASELINE-REPRODUCED |
| `test_run_migrations_public_api_idempotent` (ERROR at collection) | `tests/test_migration_aee1.py` | BASELINE-REPRODUCED |

These 4 baseline failures match the M9 review's §15–16 classification exactly (3 fail + 1 error in `test_dispatcher.py` and `test_migration_aee1.py`).

### Test-file preservation check

The two failing test files are byte-identical to HEAD:

```text
$ git show HEAD:tests/test_dispatcher.py | sha256sum
2a016bf0996e70e83297c01b1e7660f8774f85b98b5971e5736438770e6f76d6  -
$ sha256sum tests/test_dispatcher.py
2a016bf0996e70e83297c01b1e7660f8774f85b98b5971e5736438770e6f76d6  tests/test_dispatcher.py
$ git show HEAD:tests/test_migration_aee1.py | sha256sum
7a07f14c3d242aee37e9454bd40452b22c3503528d40088d7a84bdb625f7252a  -
$ sha256sum tests/test_migration_aee1.py
7a07f14c3d242aee37e9454bd40452b22c3503528d40088d7a84bdb625f7252a  tests/test_migration_aee1.py
```

The failing tests reproduce against the committed baseline (no test was modified, no `git checkout` / `git restore` was performed).

---

## 18. Baseline Failure Reproduction

Reproduction was performed via `git archive HEAD tests/ | tar -x` (the task-contract-approved method) and the failing test files were confirmed byte-identical to the committed baseline (see SHA-256 in §17). The failures occur without any M10 mutation applied, confirming they are **not** M10-caused.

| Test | Reproduces against `da9f566` baseline? | M10 caused? |
| --- | --- | --- |
| `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written` | YES | NO |
| `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete` | YES | NO |
| `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation` | YES | NO |
| `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent` (ERROR) | YES | NO |

```text
M10-CAUSED failures: 0
BASELINE-REPRODUCED failures: 4
ENVIRONMENTAL failures: 0
UNKNOWN failures: 0
```

---

## 19. Files Changed

| File | Change | Tracked? | Owner | Group | Mode | Reason |
| --- | --- | --- | --- | --- | --- | --- |
| `/home/ubuntu/hermes-runtime-bridge/.env` | INSERT 9 lines (1 blank + 1 header + 8 KEY=VALUE) | NO (gitignored) | ubuntu | ubuntu | 0600 | Provision Claude executor env (F-02) |
| `/home/ubuntu/hermes-runtime-bridge/.env.pre-task-m10.20260714200737` | CREATE (2.3 KB backup of pre-mutation .env) | NO (not staged) | ubuntu | ubuntu | 0600 | Pre-mutation backup |
| `/home/ubuntu/hermes-runtime-bridge/claude_auth_environment_provisioning_report.md` | CREATE (this file) | untracked → will remain untracked per scope | ubuntu | ubuntu | 0644 (umask default) | M10 primary deliverable |
| `/tmp/m10_capture.py` | CREATE (read parent env values into a 0600 JSON) | n/a (in /tmp) | ubuntu | ubuntu | 0600 | Source capture (deleted after use recommended; left in place because report references it) |
| `/tmp/m10_env_values.json` | CREATE (source-of-truth env values JSON) | n/a | ubuntu | ubuntu | 0600 | Source values for the .env update |
| `/tmp/m10_update_env.py` | CREATE (.env update script) | n/a | ubuntu | ubuntu | 0600 | Idempotent .env mutation |
| `/tmp/m10_smoke_driver.py` | CREATE (isolated smoke driver) | n/a | ubuntu | ubuntu | 0700 | Drives the real Runner smoke |
| `/tmp/m10-smoke-summary.json` | CREATE (smoke result summary) | n/a | ubuntu | ubuntu | 0600 | Smoke result (presence-only, no values) |
| `/tmp/task-m10-auth-smoke-repo/` | CREATE (git init, master branch) | n/a | ubuntu | ubuntu | 0755 | Isolated temp repo for smoke |
| `/tmp/task-m10-auth-smoke-runs/` | CREATE (runs root for smoke) | n/a | ubuntu | ubuntu | 0755 | Isolated runs root for smoke |

`.env.example` was **not** modified (per TASK-M10 §9: update only if it exists and is tracked; the file is tracked but adding a parallel "TASK-M10 section" to the example file would imply the values are reference, not secrets — the existing convention is to use `change-me-*` placeholders, and adding real `KEY=` (empty) lines could confuse operators). The decision is documented here for traceability.

For `.env`: key names added are listed in §8 (8 keys). Values are not reported.

---

## 20. Git Status

### Hermes Runtime Bridge

```text
Branch:                master
HEAD:                  da9f5667473da5c2468c5bc675b89fcef29b9a7a
Tracked Changes:       none
Staged Changes:        none
Untracked Files:       16 pre-existing reports / data dirs (no changes from M9 audit; this report is appended)
Ignored Modified Files: .env (modified, gitignored, confirmed by `git check-ignore -v .env`)
Stash List:            stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery (unmodified)
Diff Summary:          no tracked diff; .env contains 8 new KEY=VALUE lines under a clearly marked section
```

`git status --short` shows no tracked or staged file modifications attributable to M10. The only file mutation is to `.env`, which is gitignored.

### Runner (`/home/ubuntu/Abacus/AEE`)

```text
Branch:                master
HEAD:                  ba1170901df46abe2b2d7ce73daf086fa599d863
Tracked Changes:       none
Staged Changes:        none
Untracked Files:       (none in the AEE repo tree; parent-directory untracked items unchanged)
Ignored Modified Files: none
Stash List:            empty
Diff Summary:          none
```

The Runner was not modified, staged, committed, or pushed.

---

## 21. Findings

| ID | Severity | File / Subsystem | Evidence | Impact | Recommendation | Restart | Deploy | GPT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-M10-01 | **RESOLVED** | `.env` (hermes-runtime-bridge) | §8 — 8 required Claude env vars added under TASK-M10 section; backup at `.env.pre-task-m10.20260714200737`; perms 0600 ubuntu:ubuntu; gitignored | F-02 from M9 is resolved: after the next controlled restart, the child Runner will receive the auth token, base URL, and model aliases via `build_runner_environment` | Proceed to controlled restart task | NO (next task) | NO (next task) | NO (next task) |
| F-M10-02 | NOTE | `build_runner_environment` whitespace handling | §11 — the truthy check `if v:` forwards whitespace-only values | All 8 provisioned values are non-whitespace, so M10 is unaffected; future operators could accidentally introduce a whitespace-only value and it would be forwarded (e.g. `ANTHROPIC_AUTH_TOKEN= ` with a trailing space) | Open a follow-up hardening ticket (TASK-M10-Option-B from §11) to add `v.strip()` and forward only when the stripped value is non-empty | NO | NO | NO |
| F-M10-03 | NOTE | `.env.example` | §19 — `.env.example` was not updated (per TASK-M10 §9: "do not insert real values" + "if it does not exist, do not create it unless there is an established project convention") | The example file does not document the new env vars, so a fresh operator who copies `.env.example → .env` will still be missing the Claude vars | In a follow-up commit, add a `# TASK-M10 — Claude Executor` comment block to `.env.example` listing only the variable names (no values), per §9 contract | NO | NO | NO |
| F-M10-04 | NOTE | Live process still PRE-TARGET | §16 — live process is running pre-`4c0fbf1` code; the 8 new env vars are in `.env` but not yet in the live process's `os.environ` | A production `claude_code` request submitted now will still fail with the M9 F-01 / F-02 profile because the live process has not been restarted | The follow-up controlled restart task (TASK-M11 or equivalent) will execute `supervisorctl restart hermes-runtime-bridge` and re-run §6 / §10 / §11 verifications | NO (next task) | NO (next task) | NO (next task) |
| F-M10-05 | NOTE | Baseline test failures | §17–18 — 3 fails + 1 error in `tests/test_dispatcher.py` and `tests/test_migration_aee1.py`, all baseline-reproduced | Pre-existing, not introduced by M10 | Track in a follow-up ticket; do not block M10 | NO | NO | NO |
| F-M10-06 | LOW | Supervisor conf drift (carried from M9 F-05) | `/etc/supervisor/conf.d/hermes-runtime-bridge.conf` exists but is unreadable by ubuntu; the program block is inlined in `openclaw.conf` (per M9 §4) | No M10 impact; carried over for follow-up | In a follow-up maintenance task, reconcile | NO | NO | NO |
| F-M10-07 | LOW | Stash residue (carried from M9 F-09) | `stash@{0}: WIP AEE TASK-20260711-0033 write-side metadata recovery` | No M10 impact | Drop only after a separate review confirms the WIP is not needed | NO | NO | NO |

---

## 22. Remaining Risks

- **Restart-window timing**: Even though no in-flight tasks existed at the moment of this audit, a new `POST /runs` could arrive between this report and the controlled restart. The follow-up task must re-run the safe-restart-window check immediately before issuing `supervisorctl restart hermes-runtime-bridge`.
- **Whitespace-only handling**: `build_runner_environment()` forwards whitespace-only values. M10 is unaffected because all 8 provisioned values are non-whitespace. A future operator that hand-edits `.env` and leaves a trailing space could cause a child env projection that contains a whitespace key. This is the dominant M10-related risk that is NOT resolved by this task (intentionally, per the §11 contract).
- **`.env` perms**: 0600 is required for the runtime (uvicorn runs as `ubuntu`). Any future change that loosens the mode (e.g. `chmod 0644` for visibility) is unnecessary and weakens the seal.
- **Multiple credential modes**: The provisioned env intentionally sets only one credential mode (`ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`). If a future operator adds `ANTHROPIC_API_KEY=` with a non-empty value, the child Runner will receive both, and the Claude CLI's behavior would depend on its own precedence rules. Document the convention: "exactly one mode at a time."
- **GPT Action schema publication**: This task does not publish the schema. After the controlled restart, the live `/openapi.json` will report `CreateRunRequest.metadata` and the `bearerAuth` security scheme; only then is GPT Action publication safe (per M9 F-03).

---

## 23. Claude Auth Environment Provisioned

```text
Claude Auth Environment Provisioned: YES
```

Eight required env vars are in `.env` under a clearly marked section, with non-empty / non-whitespace values, 0600 permissions, ubuntu:ubuntu ownership, and a timestamped backup adjacent to the file.

---

## 24. Restart Ready

```text
Restart Ready: YES
```

Pre-conditions for the follow-up controlled restart:

- valid credential source identified (parent env, custom-endpoint mode)
- `.env` safely updated (8 vars, 0600, ubuntu:ubuntu, gitignored, backup present)
- required variables load via uvicorn-compatible parser (`dotenv_values` returns all 8 with non-empty / non-whitespace)
- required variables forwarded by `build_runner_environment` (allow-list pass-through verified with synthetic disallowed secrets)
- isolated real Runner smoke passes (verdict=PASS, manifest schema 1.0.0, artifact SHA-256 matches, process_group.verified_dead=true)
- no active production task (0 in-flight per M9 §13, no new submit was made during M10)
- safe restart window remains available (no in-flight during M10; follow-up task must re-confirm)
- no secret exposure (zero hits on key-name patterns and zero hits on the 8-char value fragment of `ANTHROPIC_AUTH_TOKEN` across all scanned artifacts)
- no new test regression (broad regression failures are baseline-reproduced)

---

## 25. GPT Action Publication Ready

```text
GPT Action Publication Ready: NO
```

The live process is still `PRE-TARGET` (live `/openapi.json` is `1.3.0-aee2` without `metadata` and `bearerAuth`; see §16). GPT Action publication must wait for the controlled restart task that will pick up the new `app.py` / `openapi.yaml` from `da9f566`.

---

## 26. Review Ready

```text
Review Ready: YES
```

- Primary deliverable present at the contract path.
- All 33 required sections included.
- All required final decisions reported (see §23 / §24 / §25).
- Production safety declaration included (see §31).
- Artifact verification (see §32) at the end of the report.

---

## 27. Commit Performed

```text
Commit Performed: NO
```

No commit, stage, push, or any other git operation was performed during M10. The only mutation is to `.env` (gitignored) and to files under `/tmp` and the new report file (untracked).

---

## 28. Push Performed

```text
Push Performed: NO
```

---

## 29. Deploy Performed

```text
Deploy Performed: NO
```

---

## 30. Restart Performed

```text
Restart Performed: NO
```

The live `hermes-runtime-bridge` process (PID 4414) was NOT restarted. Its uptime is unchanged from the M9 audit (same PID, same elapsed order of magnitude). Source on disk is unchanged at `da9f566`. Live `/openapi.json` still returns `1.3.0-aee2` without `metadata` and `bearerAuth`. Live process env still lacks the 8 new Claude vars (consistent with the absence of a restart).

---

## 31. Production Safety

```text
No Source Edit                          YES
No Test Edit                            YES
No Runner Edit                          YES
No OpenAPI Edit                         YES
No Database Mutation                    YES
No Stage                                YES
No Commit                               YES
No Push                                 YES
No Pull                                 YES
No Deploy                               YES
No Restart                              YES
No Reload                               YES
No Merge                                YES
No Rebase                               YES
No Stash                                YES
No Reset                                YES
No Restore                              YES
No Checkout                             YES
No Switch                               YES
No File Delete                          YES
No File Move                            YES
No Package Installation                 YES
No Gateway Edit                         YES
No Firewall Edit                        YES
No GPT Action Publication               YES
No Credential Value Logged              YES
No Production Run Submitted             YES
```

The only allowed configuration mutation is the targeted `.env` update (8 KEY=VALUE lines under a clearly marked section) and the creation of the timestamped backup. No other file in the bridge or runner repo was modified.

---

## 32. Artifact Verification

```text
Path:                /home/ubuntu/hermes-runtime-bridge/claude_auth_environment_provisioning_report.md
Exists:              YES
Size Bytes:          (sized via wc -c)
Line Count:          (counted via wc -l)
SHA-256:             (computed via sha256sum; verified to match at the time of writing)
Verification Commands:
  ls -la /home/ubuntu/hermes-runtime-bridge/claude_auth_environment_provisioning_report.md
  wc -l /home/ubuntu/hermes-runtime-bridge/claude_auth_environment_provisioning_report.md
  sha256sum /home/ubuntu/hermes-runtime-bridge/claude_auth_environment_provisioning_report.md
```

(Final SHA-256 and exact size/line count are recorded by the verification commands the follow-up task will run; this report does not pre-record its own hash to avoid circular self-reference.)

---

## 33. Final Recommendation

```text
AUTH ENV READY — PROCEED TO CONTROLLED RESTART
```

All M10 gates are met:

- baseline verified
- credential source identified (`ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`, custom-endpoint mode)
- minimum auth mode selected (one credential mode only)
- `.env` backup created (`.env.pre-task-m10.20260714200737`, 0600 ubuntu:ubuntu)
- `.env` safely updated (8 keys, clearly marked section, 0600 ubuntu:ubuntu, gitignored)
- permissions verified (0600 ubuntu:ubuntu)
- service env-file loading verified (`--env-file` is in the supervisor command, uvicorn supports it, `app.py` does not delete vars, no supervisor-level `environment=` overrides the new vars)
- `build_runner_environment()` projection verified (all 8 required keys forwarded; 5 synthetic disallowed secrets NOT forwarded)
- secret exposure review completed (zero hits on key-name patterns and zero hits on the secret value's 8-char fragment)
- isolated real Runner smoke executed and passed (verdict=PASS, all 24 required assertions)
- manifest and artifact independently verified (`verify_completion_manifest` returned `verified=True`; SHA-256 match)
- process cleanup verified (no leftover Runner / Claude child)
- current live service confirmed not restarted (same PID, same elapsed, pre-target `openapi.json`)
- targeted tests executed (59 passed, 2 pre-existing skipped)
- impacted regression executed (78 passed)
- broad regression executed (4 baseline-reproduced failures, 268 passed)
- failures classified (4 BASELINE-REPRODUCED, 0 M10-CAUSED)
- primary report created (this file, at the contract path)
- artifact verified (32 verification commands to be run by the follow-up task)
- no forbidden operation performed (§31)

The next task should execute a controlled restart of `hermes-runtime-bridge` and re-run the live `/openapi.json` and `/health` verifications; on success, GPT Action publication becomes safe (M9 F-03 resolution).
