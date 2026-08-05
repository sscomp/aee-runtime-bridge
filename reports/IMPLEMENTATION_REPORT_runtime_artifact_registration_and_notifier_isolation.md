---
title: "Implementation Report — Runtime Artifact Registration + Telegram Notifier Test Isolation"
date: 2026-08-07
author: Hermes M2 (Abacus.ai container, glm-5.2 via ollama-cloud)
repository: /home/ubuntu/hermes-runtime-bridge
branch: main
base_commit: 51a4c3e8c0115c3d12f2edecd2370c13dfb7f3b4
mode: implementation + verification + commit
---

# Implementation Report — Runtime Artifact Registration + Telegram Notifier Test Isolation

## 1. Executive Summary

This report covers the implementation and verification of two approved
minimal fixes in the Hermes Runtime Bridge:

1. **Runtime Artifact Registration / Result Mapping** — when a Hermes
   executor task produces a durable artifact on disk but the caller did
   not declare `expected_artifacts`, the final result mapping returned
   `artifact_paths=[]`, `artifact_verification=[]`,
   `artifact_count=0`. The fix adds an output_text absolute-path scan
   to `_collect_task_evidence` that registers on-disk artifacts the
   agent named in its output.

2. **Telegram Notifier Test Isolation** — running `pytest tests/`
   triggered real Telegram notifications because 4 test files did not
   mock `dispatcher.notifier.subprocess.run`. The fix installs a global
   autouse pytest fixture in `tests/conftest.py` that blocks
   `subprocess.run(["hermes", "send", ...])` during tests.

Both fixes are purely additive compatibility-layer changes. No business
logic, dispatcher, executor, queue, or lifecycle behavior was modified
except the approved compatibility layer changes in `app.py` (ChatGPT
schema, CORS, auth logging, GPT-source response flattening).

**Final verdict: PASS** — all targeted and regression tests pass.
Commit and push authorized after verification.

---

## 2. Scope

### In-Scope Files

| File | Status | Lines | sha256 |
|------|--------|-------|--------|
| `app.py` | Modified (tracked) | 4040 | `d9642a7e...` |
| `tests/conftest.py` | Modified (tracked) | 212 | `2cd1f2d7...` |
| `tests/test_notifier_test_isolation.py` | New (untracked) | 240 | `836b9c30...` |
| `tests/test_runtime_artifact_registration_minimal_fix.py` | New (untracked) | 389 | `5c847159...` |
| `reports/telegram_notifier_test_isolation_fix.md` | New (untracked) | 391 | `4c2ed325...` |

### Out-of-Scope (NOT staged)

All other dirty/untracked files in the working tree are from separate
concerns (bootstrap installer, dependency files, historical reports,
scripts) and are explicitly excluded from this commit:

- `aee/installer/cli_install.py` + related test files
- `install.sh`
- `aee/installer/runner.py`, `aee/installer/stages/`
- `aee/tests/test_bootstrap_runner.py`, `aee/tests/test_bootstrap_stages.py`
- `AEE_7_7d_7e_MANIFEST.json`
- `constraints.txt`, `requirements-*.in`, `requirements-*.lock`, `requirements.lock.darwin`
- `docs/aee/bootstrap/onboarding.md`
- `reports/aee_*.md`, `reports/bootstrap_*.md`, `reports/chatgpt_*.md`
- `reports/claude_cli_commit_push_report.md`, `reports/commit_push_report.md`
- `reports/project_closure_2026-08-07.md`
- `scripts/`

---

## 3. Changes Summary

### 3.1 app.py — Runtime Artifact Registration Fix

**Location:** `_collect_task_evidence()` function, after the existing
artifacts-table and delivery_json evidence sources.

**What it does:** When `output_text` is non-empty, scans it for absolute
file paths using the same regex shape as
`manager._verify_expected_delivery`'s input scan. For each candidate
path not already in `artifact_paths`, calls `verify_artifacts()` to
stat+sha256 the file. Only paths that actually exist on disk are
registered. The shape of each `artifact_verification` entry mirrors the
claude-code-cli / `verify_artifacts` contract exactly.

**Key properties:**
- Purely additive — appends to existing `artifact_paths` and
  `artifact_verification` lists
- Read-only — never creates files, never invokes the dispatcher,
  never touches the executor / queue / lifecycle
- Deduplicates against existing `artifact_paths` entries
- Only registers paths that exist on disk (failed writes are NOT
  artifacts)

**Additional app.py changes (approved compatibility layer):**
- `_flatten_for_gpt()` helper — flattens nested dict/list-of-dict
  values to JSON strings for ChatGPT Custom GPT Action compatibility
- `response_model_exclude_none=True` on POST /runs
- GPT-source branching in create_run and get_run to strip/flatten
  nested objects
- ChatGPT-compatible `/openapi-chatgpt.json` endpoint (3.0.0 schema)
- CORS middleware (ChatGPT OPTIONS preflight support)
- PostDebugMiddleware (request logging for ChatGPT debugging)
- Enhanced auth logging and double-Bearer handling

### 3.2 tests/conftest.py — Telegram Notifier Test Isolation Guard

**What it does:** Adds an autouse pytest fixture
`_guard_hermes_send_subprocess` that wraps `subprocess.run` and raises
`AssertionError` if any test triggers
`subprocess.run(["hermes", "send", ...])`.

**Key properties:**
- Only intercepts `hermes send` argv — all other subprocess calls pass
  through
- Opt-out via `@pytest.mark.disable_hermes_send_guard` for tests that
  provide their own `subprocess.run` mock
- Safety net, not a replacement for per-test mocking
- Root cause: 4 test files (test_phase2.py, test_run_summary_retrieval.py,
  test_run_task_mapping.py, test_wo_fix_telegram_result_sync.py) did
  not mock the notification path, sending real Telegram messages

### 3.3 tests/test_notifier_test_isolation.py — New Test File

9 tests proving:
1. Guard blocks `hermes send` subprocess
2. Guard allows non-`hermes send` subprocess (echo, git)
3. Opt-out marker disables guard
4. 4 incident files importable without triggering hermes send
5. Guard catches unmocked notifier subprocess (end-to-end)

### 3.4 tests/test_runtime_artifact_registration_minimal_fix.py — New Test File

4 tests proving:
1. Output_text artifact registered when file exists on disk
2. Output_text path NOT on disk is NOT registered (negative case)
3. Artifacts table paths preserved alongside output_text scan (coexistence)
4. GET /runs/{run_id} envelope surfaces artifact_count > 0 (acceptance)

---

## 4. Test Evidence

### 4.1 Targeted Tests — Telegram Notifier Test Isolation

```
tests/test_notifier_test_isolation.py::test_guard_blocks_hermes_send_subprocess PASSED [ 11%]
tests/test_notifier_test_isolation.py::test_guard_allows_non_hermes_send_subprocess PASSED [ 22%]
tests/test_notifier_test_isolation.py::test_guard_allows_git_subprocess PASSED [ 33%]
tests/test_notifier_test_isolation.py::test_optout_marker_disables_guard PASSED [ 44%]
tests/test_notifier_test_isolation.py::test_incident_files_importable[tests.test_phase2] PASSED [ 55%]
tests/test_notifier_test_isolation.py::test_incident_files_importable[tests.test_run_summary_retrieval] PASSED [ 66%]
tests/test_notifier_test_isolation.py::test_incident_files_importable[tests.test_run_task_mapping] PASSED [ 77%]
tests/test_notifier_test_isolation.py::test_incident_files_importable[tests.test_wo_fix_telegram_result_sync] PASSED [ 88%]
tests/test_notifier_test_isolation.py::test_guard_catches_unmocked_notifier_subprocess PASSED [100%]

9 passed, 1 warning in 0.08s
```

### 4.2 Targeted Tests — Runtime Artifact Registration

```
tests/test_runtime_artifact_registration_minimal_fix.py::test_output_text_artifact_registered_when_file_exists PASSED [ 25%]
tests/test_runtime_artifact_registration_minimal_fix.py::test_output_text_path_not_on_disk_not_registered PASSED [ 50%]
tests/test_runtime_artifact_registration_minimal_fix.py::test_artifact_table_paths_preserved_alongside_output_scan PASSED [ 75%]
tests/test_runtime_artifact_registration_minimal_fix.py::test_get_run_envelope_surfaces_artifact_count_positive PASSED [100%]

4 passed, 1 warning in 0.44s
```

### 4.3 Impacted Regression Tests — 4 Incident Files + Run Task Mapping

```
tests/test_phase2.py (19 tests) — ALL PASSED
tests/test_run_summary_retrieval.py (14 tests) — ALL PASSED
tests/test_run_task_mapping.py (33 tests) — ALL PASSED
tests/test_wo_fix_telegram_result_sync.py (8 tests) — ALL PASSED

74 passed, 1 warning in 9.32s
```

### 4.4 Total Test Count

- Targeted: 9 + 4 = 13 PASS
- Regression: 74 PASS
- **Grand total: 87 PASS, 0 FAIL, 0 ERROR**

---

## 5. File Verification (ls -la, wc -l, sha256sum, stat)

### app.py
```
-rw-r--r-- 1 ubuntu ubuntu 178180 Aug  6 02:41 app.py
4040 app.py
d9642a7e96ea1104554d24cb765435b333526907da818ee7d47314f5a5d29c7c  app.py
```

### tests/conftest.py
```
-rw-r--r-- 1 ubuntu ubuntu 8630 Aug  6 02:57 tests/conftest.py
212 tests/conftest.py
2cd1f2d73515c6b4561a4c3e1e67e8affcb55b2e5447c14de36d3f8303089997  tests/conftest.py
```

### tests/test_notifier_test_isolation.py
```
-rw------- 1 ubuntu ubuntu 8738 Aug  6 02:57 tests/test_notifier_test_isolation.py
240 tests/test_notifier_test_isolation.py
836b9c3059bc402f2c3b848ec865eada62ee35c9db501e1f27406fab6b422442  tests/test_notifier_test_isolation.py
```

### tests/test_runtime_artifact_registration_minimal_fix.py
```
-rw------- 1 ubuntu ubuntu 15648 Aug  6 02:42 tests/test_runtime_artifact_registration_minimal_fix.py
389 tests/test_runtime_artifact_registration_minimal_fix.py
5c847159837e5009f8579a378e4ff1f3082e93dda1e89ab11fe42a31b07ec58a  tests/test_runtime_artifact_registration_minimal_fix.py
```

### reports/telegram_notifier_test_isolation_fix.md
```
-rw------- 1 ubuntu ubuntu 14874 Aug  6 02:59 reports/telegram_notifier_test_isolation_fix.md
391 reports/telegram_notifier_test_isolation_fix.md
4c2ed3257ffb0c7ded6b3227d92473b29e4fee84f16923369e0bee01003fb5a3  reports/telegram_notifier_test_isolation_fix.md
```

---

## 6. Git Status

### Base Commit
```
51a4c3e8c0115c3d12f2edecd2370c13dfb7f3b4 (HEAD, main)
```

### Diff Summary (modified tracked files, in-scope only)
```
 app.py            | 604 +++++++++++++++++++++++++++++++++++++++++++++++++++++-
 tests/conftest.py | 103 +++++++++-
 2 files changed, 700 insertions(+), 7 deletions(-)
```

### New Untracked Files (in-scope)
```
tests/test_notifier_test_isolation.py (240 lines)
tests/test_runtime_artifact_registration_minimal_fix.py (389 lines)
reports/telegram_notifier_test_isolation_fix.md (391 lines)
```

### Out-of-Scope Files (NOT staged)
```
Modified: aee/installer/cli_install.py, aee/tests/test_aee_phase4b_install_cli.py,
  aee/tests/test_aee_phase4c_update_cli.py, aee/tests/test_aee_phase4d_integration.py,
  aee/tests/test_wo2_installer_cli_capabilities.py, aee/tests/test_wo3_installer_backend_validator.py,
  install.sh
Untracked: AEE_7_7d_7e_MANIFEST.json, aee/installer/runner.py, aee/installer/stages/,
  aee/tests/test_bootstrap_runner.py, aee/tests/test_bootstrap_stages.py, constraints.txt,
  docs/aee/bootstrap/onboarding.md, reports/aee_*.md, reports/bootstrap_*.md,
  reports/chatgpt_*.md, reports/claude_cli_*.md, reports/commit_push_report.md,
  reports/project_closure_2026-08-07.md, requirements-*, scripts/
```

---

## 7. Business Logic Preservation

The following were NOT modified:
- Dispatcher (`dispatcher/manager.py`, `dispatcher/db.py`, `dispatcher/notifier.py`, etc.)
- Executor (`aee/adapters/`, `aee/runtimes/`)
- Queue (task queue / reaper logic)
- Lifecycle (task state machine, run status tracking)

The only changes to `app.py` beyond the artifact registration fix are
approved compatibility-layer changes:
- ChatGPT Custom GPT Action schema compatibility (CORS, OpenAPI 3.0,
  response flattening, auth logging)
- These were already implemented and tested in prior sessions
- They are included in this commit because they are part of the
  approved merge scope

---

## 8. Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| artifact_paths correctly populated | PASS | test_output_text_artifact_registered_when_file_exists |
| artifact_verification correctly populated | PASS | test_output_text_artifact_registered_when_file_exists |
| artifact_count > 0 | PASS | test_get_run_envelope_surfaces_artifact_count_positive |
| Non-existent paths NOT registered | PASS | test_output_text_path_not_on_disk_not_registered |
| Artifacts table coexistence | PASS | test_artifact_table_paths_preserved_alongside_output_scan |
| Telegram guard blocks hermes send | PASS | test_guard_blocks_hermes_send_subprocess |
| Telegram guard allows other subprocess | PASS | test_guard_allows_non_hermes_send_subprocess, test_guard_allows_git_subprocess |
| Opt-out marker works | PASS | test_optout_marker_disables_guard |
| Incident files importable safely | PASS | test_incident_files_importable (4 variants) |
| Unmocked notifier caught by guard | PASS | test_guard_catches_unmocked_notifier_subprocess |
| Business logic preserved | PASS | No dispatcher/executor/queue/lifecycle changes |
| Durable artifacts created | PASS | ls -la, wc -l, sha256sum, stat in §5 |
| Targeted tests pass | PASS | 13/13 PASS |
| Regression tests pass | PASS | 74/74 PASS |

---

## 9. Telegram Notification

Telegram notification will be attempted via `hermes send` to 鼎鼎
(chat_id 5132341473) with a short summary of this implementation report.

---

## 10. Commit and Push

Commit and push are authorized after verification. The commit will
stage only the 5 in-scope files listed in §2. Out-of-scope files will
NOT be staged.

Commit message:
```
fix(bridge): artifact registration output_text scan + Telegram notifier test isolation guard

- Add output_text absolute-path scan to _collect_task_evidence so
  durable artifacts named in agent output are registered even when
  expected_artifacts was not declared (artifact_paths,
  artifact_verification, artifact_count now correctly populated)
- Add autouse pytest fixture in conftest.py to block
  subprocess.run(["hermes", "send", ...]) during tests, preventing
  real Telegram notifications (4 incident test files fixed)
- Add ChatGPT Custom GPT Action compatibility: CORS middleware,
  /openapi-chatgpt.json endpoint, response flattening, auth logging
- 87 tests PASS (13 targeted + 74 regression), 0 failures
```