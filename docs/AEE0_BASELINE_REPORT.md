# AEE-0 Baseline Hardening Report

**Date:** 2026-07-10
**Scope:** `hermes-runtime-bridge` → first step toward the Agent
Execution Engine (AEE) outlined in
`Abacus/Hermes_Runtime_Bridge_to_AEE_Implementation_Plan.md`.
**Phase:** AEE-0 — clean up, freeze, and lock in a security baseline
**without changing any public API behaviour.**

---

## 1. Summary

| Item                                | Result |
|-------------------------------------|--------|
| Sensitive files inventoried (no secret text disclosed) | done |
| Permissions locked to 600 on secrets | done |
| `.gitignore` added | done |
| `.tarignore` added (verified against a real `tar`) | done |
| `safety.json` / `__init__.py` DEFAULTS tightened | done |
| `dispatcher/safety.py` now consults regex assignment patterns | done |
| `tests/test_safety.py` rewritten against `evaluate()` | done (20 dangerous, 9 clean) |
| `compileall` of `app.py dispatcher research config cli tests` | OK |
| `tests/test_safety.py` | **ALL PASS** (20/20 dangerous blocked, 9/9 clean allowed) |
| `tests/test_routing.py` | **OK** (24/24) |
| `tests/test_dispatcher.py` | **OK** (21/21) |
| `tests/test_phase2.py` | **OK** (19/19) |
| README "DO NOT pack runtime data" section | added |

**Net result: AEE-0 is green. The bridge's `/runs` and `/tasks` API
surface is unchanged.**

---

## 2. What changed (file-level)

### Added

- `.gitignore` — covers secrets, runtime data (DB / logs / reports /
  backups), build artifacts (`.venv`, `__pycache__`, `*.pyc`, `*.egg-info`),
  and local-only editor cruft.
- `.tarignore` — a separate, tighter exclusion file for handoff tarballs.
  Verified by `tar --exclude-from=.tarignore -czf` against the repo root:
  the resulting tarball shrank from 8.7 MB to ~105 KB, and **zero** of
  the previously-observed sensitive/runtime paths (`.env`,
  `CREDENTIALS.txt`, `data/dispatcher.db`, `logs/`, `reports/TASK-*`,
  `.venv/`, `__pycache__/`) made it into the archive.
- `private/` and `runtime_data/` — empty placeholder dirs reserved for
  future AEE-1+ artifacts (e.g. local SQLite for non-Hermes runtimes).
  Both are ignored by `.gitignore` / `.tarignore`.
- `docs/AEE0_BASELINE_REPORT.md` (this file).

### Modified

- `config/safety.json` — tightened `blocklist_substrings`, added a
  new `blocklist_assignment_patterns` list (regex-based) for
  `export API_SERVER_KEY=...` style leaks, and replaced the
  `curl ... | sh` / `wget ... | sh` placeholders in
  `require_approval_substrings` with real ` | sh` / ` | bash`
  substrings.
- `config/__init__.py` (DEFAULTS for `safety.json`) — kept in lock-step
  with the file so `ensure_defaults()` regeneration produces the same
  policy.
- `dispatcher/safety.py` — added a step 1b that walks
  `blocklist_assignment_patterns` with `re.search(..., re.MULTILINE)`
  so a bare `API_SERVER_KEY=hack` or any very-long literal
  assignment is rejected. A bad regex in config is logged-and-skipped
  rather than crashing the call.
- `tests/test_safety.py` — rewritten to drive `dispatcher.safety.evaluate`
  (the canonical entrypoint) instead of the legacy `app.danger_check`.
  The test now asserts a specific `action` for every dangerous input
  (block vs. require_approval) and asserts `allow` for every clean
  input. It also runs under `python -m` style and exits non-zero on
  failure.
- `README.md` — added a "DO NOT pack runtime data" section that
  documents which paths must never be shipped and includes a one-liner
  to verify any handoff tarball.

### Not modified (intentionally)

- `app.py` — public API behaviour preserved. `DANGEROUS_PATTERNS` is
  retained as defence-in-depth (the new policy engine handles the same
  patterns and more).
- `dispatcher/manager.py`, `dispatcher/db.py`, `dispatcher/watcher.py`,
  `dispatcher/reaper.py` — the runtime model still uses
  `hermes_run_id` as before; AEE-1 will introduce additive
  `runtime_type` / `external_run_id` / `claim_token_hash` columns.

---

## 3. Risk register (open)

| # | Risk                                                                                            | Mitigation in AEE-1+ |
|---|--------------------------------------------------------------------------------------------------|----------------------|
| 1 | Task model is still bound to `hermes_run_id`; Hermes is the only runtime.                        | AEE-1: introduce `runtime_type` / `adapter_name` / `external_run_id` / `worker_id` / `claim_token_hash` (additive migration, no column drops). |
| 2 | `app.py::DANGEROUS_PATTERNS` (regex list) and `config/safety.json` (substring list) overlap but are not unified. | AEE-1: collapse to a single config-driven policy; `app.py` should call `dispatcher.safety.evaluate` only. |
| 3 | `/runs` response still hard-codes `requires_review=False` even when `evaluate()` returns `require_approval`. | AEE-1: keep external `requires_review=False` for GPT Action compat, but record `approval_required=true` internally and gate Worker claim on it. |
| 4 | Queue is not really a queue — `/runs` calls Hermes immediately. No real Worker Claim Protocol.    | AEE-2: introduce `workers` table + `/workers/register` + `/jobs/claim` + `/jobs/{id}/heartbeat` / `complete` / `fail`. |
| 5 | No long-running audit log; only per-task log files.                                              | AEE-1: add `audit_log` table (action, actor, target, decision, ts). |
| 6 | Backup directory `data.bak.20260707-085710/` still on disk (currently ignored by `.gitignore`).   | Leave in place per plan constraint ("do not delete production DB"). Will be excluded from any future tar. |

---

## 4. Recommended next steps (AEE-1 preview)

Per the implementation plan, **AEE-1** extracts the Core and
`HermesAdapter` so `app.py` is no longer the bridge. Concrete asks
for that phase:

1. Create `aee/core/{job_models,state_machine,dispatcher,registry}.py`
   and `aee/adapters/{base,hermes_adapter}.py`.
2. Define a `RuntimeAdapter` Protocol with `submit / poll / cancel`,
   implemented by `HermesAdapter` wrapping the existing
   `POST /v1/runs`, `GET /v1/runs/{id}`, `POST /v1/runs/{id}/stop`
   calls.
3. Additive SQLite migration for the new columns
   (`runtime_type`, `adapter_name`, `external_run_id`, `worker_id`,
   `heartbeat_at`, `claim_token_hash`, `approval_required`,
   `approval_state`). Old `hermes_run_id` keeps its current semantics
   for compatibility.
4. `app.py` switches to `HermesAdapter` internally. The watcher stops
   hard-coding `HERMES_BASE_URL` and instead routes via
   `adapter_name`.
5. Add `FakeAdapter` for unit tests; rerun all four test suites; add
   a new `tests/test_adapter.py` to lock in adapter contract.
6. Acceptance: existing `/runs` still completes a Hermes job end-to-end;
   `FakeAdapter` drives the same code path with zero upstream calls.

---

## 5. Verification transcript

```text
$ .venv/bin/python -m compileall -q app.py dispatcher research config cli tests
(rc=0, no output)

$ PYTHONPATH=. .venv/bin/python tests/test_safety.py
… (20 dangerous entries all blocked; 9 clean all allowed)
ALL PASS

$ PYTHONPATH=. .venv/bin/python tests/test_routing.py
Ran 24 tests in 0.001s
OK

$ PYTHONPATH=. .venv/bin/python tests/test_dispatcher.py
Ran 21 tests in 0.049s
OK

$ PYTHONPATH=. .venv/bin/python tests/test_phase2.py
Ran 19 tests in 6.125s
OK

$ tar --exclude-from=.tarignore -czf /tmp/check.tgz -C /home/ubuntu/hermes-runtime-bridge .
$ tar -tzf /tmp/check.tgz | wc -l
76
$ tar -tzf /tmp/check.tgz | grep -E '(^\./\.env|api_keys|CREDENTIALS|\.db|\.venv|__pycache__|TASK-)'
(no matches)
```

---

## 6. Constraints honoured

- No secret text was emitted in this report, in commit messages, or in
  any log. Only filenames, permissions, sizes, and counts.
- `data/dispatcher.db` was not deleted, modified, or backed up
  in-place; it remains in `data/` and is now ignored by `.gitignore`
  and `.tarignore`.
- No service was restarted. `compileall` and the test runs are
  read-only against the live tree.
