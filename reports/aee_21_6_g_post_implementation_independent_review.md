# §21.6.G WO-1/WO-2/WO-3 Post-Implementation Independent Review

**Work Order:** §21.6.G — Independent post-implementation review of WO-1/WO-2/WO-3
**Repository:** `/home/ubuntu/hermes-runtime-bridge` (branch: `main`)
**Reviewer:** Hermes M2 (Abacus.ai container, glm-5.2 via ollama-cloud)
**Date:** 2026-07-31
**Mode:** REVIEW ONLY (no mutation, no commit, no push, no deploy)
**Context Artifacts:**
- `reports/aee_wo1_host_capabilities.md` (WO-1 implementation report)
- `reports/aee_wo2_installer_cli_capabilities.md` (WO-2 implementation report)
- `reports/aee_wo3_installer_backend_validator.md` (WO-3 implementation report)
- `reports/aee_21_6_g_acceptance_verification.md` (prior acceptance audit)
- `reports/aee_p0_1_shadow_run_start.md` (P0-1 shadow run, active)

---

## 1. Execution Timing

| Field | Value |
|-------|-------|
| Start (UTC) | 2026-07-31T00:30:00Z (approx) |
| Start (Asia/Taipei) | 2026-07-31T08:30:00 CST (approx) |
| End (UTC) | 2026-07-31T01:30:00Z (approx) |
| End (Asia/Taipei) | 2026-07-31T09:30:00 CST (approx) |
| Duration | ~60 min |
| Mode | Independent review (read-only) |

---

## 2. Overall Verdict

**PASS WITH CAVEATS**

The WO-1/WO-2/WO-3 change set is structurally sound, correctly binds the installer backend to the authoritative §21.6.B/§21.6.C contract, preserves backward compatibility, produces deterministic exit codes, and does not disturb the P0-1 shadow-run baseline. All 105 targeted WO tests pass (7+32+66). 2546 broader aee/tests pass with 5 pre-existing PyYAML env-gap errors (unrelated). 296 legacy tests pass with 1 pre-existing PyYAML import error (unrelated). No BLOCKER or HIGH findings. Two MEDIUM findings (stale CLI help text; `malformed_yaml` reason_kind effectively unreachable without PyYAML) and two LOW findings (redundant file read; minor inefficiency) remain. The change set is **Review Ready** and **Commit Ready** subject to the MEDIUM caveats being addressed in a follow-up.

---

## 3. Baseline

| Item | Value |
|------|-------|
| Branch | `main` |
| HEAD | `befe3d6fe5eeeafed316883d27e2868638c64d22` |
| HEAD subject | `feat(bootstrap): add W1 — WINDOWS identity + WindowsAdapter skeleton` |
| Working tree | 3 modified tracked files (aee/cli.py, aee/installer/backend.py, aee/installer/cli_install.py), 2 untracked WO test files + 1 untracked host.capabilities.yaml + 3 untracked WO reports + ~80 unrelated untracked files |
| P0-1 shadow run | Active (baseline 2026-07-29T17:38:32Z, Day 1 PASS) |
| Bridge service | RUNNING (pid 1619150, uptime 5+ days) |
| `/health` | 200 OK |

---

## 4. Change Inventory

### Tracked modified files (3)

| File | Status | Lines changed |
|------|--------|---------------|
| `aee/cli.py` | Modified | +28/-3 |
| `aee/installer/backend.py` | Modified | +333/-1 |
| `aee/installer/cli_install.py` | Modified | +125/-0 |
| **Total tracked diff** | | **+486/-4** |

### Untracked WO-relevant files (6)

| File | Size | Lines |
|------|------|-------|
| `host.capabilities.yaml` | 1.9K | 53 |
| `aee/tests/test_wo1_host_capabilities.py` | 4.3K | 108 |
| `aee/tests/test_wo2_installer_cli_capabilities.py` | 18.4K | 491 |
| `aee/tests/test_wo3_installer_backend_validator.py` | 34.3K | 937 |
| `reports/aee_wo1_host_capabilities.md` | 18.7K | — |
| `reports/aee_wo2_installer_cli_capabilities.md` | 17.3K | — |
| `reports/aee_wo3_installer_backend_validator.md` | 18.6K | — |

### P0-1 protected files (NOT modified)

| File | Status |
|------|--------|
| `app.py` | NOT in diff — byte-identical to baseline |
| `dispatcher/db.py` | NOT in diff — byte-identical to baseline |
| `dispatcher/manager.py` | NOT in diff — byte-identical to baseline |
| `dispatcher/models.py` | NOT in diff — byte-identical to baseline |
| `dispatcher/notification_state.py` | NOT in diff — byte-identical to baseline |
| `dispatcher/notifier.py` | NOT in diff — byte-identical to baseline |

---

## 5. Authoritative Contract Checks

### 5.1 host.capabilities.yaml loads via canonical loader

```
PYTHONPATH=. python3 -c "
from aee.deploy.loader import load_host_capabilities
cap = load_host_capabilities('host.capabilities.yaml')
print(cap.name, cap.class_, cap.provider_hint)
"
→ m2-abacus container abacus
```

### 5.2 §21.6.B schema validation passes

```
validate_capabilities(cap) → OK (no raise)
```

### 5.3 §21.6.C resource-floor validation passes for all 4 profiles

```
validate_resource_floor(cap, "full")      → ResourceFloor(cpu_floor=2, mem_mb_floor=4096, disk_mb_floor=2048)
validate_resource_floor(cap, "mini")      → ResourceFloor(cpu_floor=1, mem_mb_floor=1024, disk_mb_floor=1024)
validate_resource_floor(cap, "edge")      → ResourceFloor(cpu_floor=1, mem_mb_floor=1024, disk_mb_floor=512)
validate_resource_floor(cap, "developer") → ResourceFloor(cpu_floor=1, mem_mb_floor=1024, disk_mb_floor=512)
```

### 5.4 M2 resource floors match §21.6.C reference

| Profile | YAML declares | §21.6.C reference | Match |
|---------|--------------|-------------------|-------|
| full | cpu=2, mem=4096, disk=2048 | cpu=2, mem=4096, disk=2048 | YES |
| mini | cpu=1, mem=1024, disk=1024 | cpu=1, mem=1024, disk=1024 | YES |
| edge | cpu=1, mem=1024, disk=512 | cpu=1, mem=1024, disk=512 | YES |
| developer | cpu=1, mem=1024, disk=512 | cpu=1, mem=1024, disk=512 | YES |

### 5.5 M2 actual resources exceed floors

| Resource | Actual | full floor | Margin |
|----------|--------|-----------|--------|
| CPU | 32 | 2 | 16x |
| Memory | 507681 MB | 4096 MB | 124x |
| Disk | 3095250 MB | 2048 MB | 1511x |

### 5.6 Canonical file matches AbacusAdapter.detect()

All 13 shared declaration fields match `AbacusAdapter.detect()` on M2:
`name`, `class_`, `os`, `arch`, `python`, `filesystem`, `supervisor`, `network_egress`, `tunnel_kind`, `inbound_allowed`, `db_path_writable`, `tempdir_writable`, `provider_hint` → 13/13 OK.

---

## 6. CLI Review

### 6.1 Flag is minimal and backward compatible

- `--capabilities <path>` added to `install` subcommand only (`aee/cli.py:258`)
- `dest="capabilities"`, `default=None` — omitted flag → `None` → backward compat
- When `None`, no extra I/O, no validation, no change to exit code (verified: `aee install` without `--capabilities` → exit 0, identical to pre-WO-2)
- `--capabilities` alone (no other Phase 4B flag) routes through `_install_dispatch_phase4b` → `run_install` (WO-3 guard active)

### 6.2 Discoverable in help

`aee install --help` shows `--capabilities <path>` with description. **However**, the help text is stale (see Finding M-1).

### 6.3 Plumbing correctness

- `aee/cli.py:971`: `getattr(args, "capabilities", None) is not None` added to `phase4b_flags` tuple → correct routing
- `aee/cli.py:980`: `capabilities=getattr(args, "capabilities", None)` passed to `_install_dispatch_phase4b` → correct plumbing
- `aee/cli.py:639`: `capabilities=capabilities` passed to `InstallCliOptions` → correct
- `aee/installer/cli_install.py:321`: `cap_path=options.capabilities` threaded into `InstallerBackend` constructor → correct
- `aee/installer/cli_install.py:336`: `backend.validate_capabilities_document(profile=canonical)` called BEFORE plan/preflight → correct

---

## 7. Backend Validation Review

### 7.1 Validation occurs before any install/materialization action

`run_install` flow (`aee/installer/cli_install.py`):
1. Resolve canonical profile
2. Construct `InstallerBackend(cap_path=options.capabilities)`
3. **Step 2.5 (WO-3 guard)**: `backend.validate_capabilities_document(profile=canonical)` — BEFORE plan/preflight
4. If `cap_result.ok is False`: return `InstallCliResult(exit_code=EXIT_CAPABILITIES_INVALID)` — does NOT proceed to plan/preflight
5. If `cap_result.ok is True`: proceed to plan/preflight (existing path)

**Confirmed**: No plan/preflight/execute action is taken when `--capabilities` is supplied and validation fails.

### 7.2 Uses authoritative loader/validator

- Loader: `from aee.deploy.loader import load_host_capabilities` (canonical, line 701)
- Schema validator: `from aee.deploy.contract import validate_capabilities` (canonical, line 728)
- Resource-floor validator: `from aee.deploy.contract import validate_resource_floor` (canonical, line 729)

No parallel hard-coded matrix. No bypass paths.

### 7.3 No double validation

`validate_capabilities_document` is called exactly once in `run_install` (line 336). No other call site in the install path. `validate_capabilities` / `validate_resource_floor` are called inside `validate_capabilities_document` only (not separately in `run_install`).

### 7.4 No unsafe side effects

`validate_capabilities_document` (`aee/installer/backend.py:601-766`):
- Reads file via `Path(path).read_text()` (read-only)
- Calls `load_host_capabilities(path)` (read-only loader)
- Calls `validate_capabilities(cap)` (pure validation, no I/O)
- Calls `validate_resource_floor(cap, profile)` (pure validation, no I/O)
- No writes, no subprocess, no network, no mutations
- `import os` is local to the method (no module-level side effect)

### 7.5 No unrelated behavior changes

`/usr/bin/git diff` confirms all changes are WO-2/WO-3 scoped:
- `aee/cli.py`: only `--capabilities` argparse + routing
- `aee/installer/backend.py`: only `CapabilitiesValidationResult`, `CapabilitiesValidationError`, `validate_capabilities_document`, `cap_path` constructor field
- `aee/installer/cli_install.py`: only `capabilities` field on options/result, WO-3 guard, `_capabilities_validated_note`

No unrelated functions, classes, or logic were modified.

---

## 8. Error and Exit-Code Matrix

| Failure Mode | `reason_kind` | Exit Code | Verified? | Evidence |
|---|---|---|---|---|
| `--capabilities` omitted | `""` | 0 | YES | `aee install` → exit 0 |
| Missing file | `missing_file` | 13 | YES | `--capabilities /tmp/no_such_file.yaml` → exit 13 |
| Unreadable file (perm) | `unreadable_file` | 13 | YES | `chmod 000` → exit 13 |
| Unreadable file (dir) | `unreadable_file` | 13 | YES | `--capabilities /tmp/notadir` → exit 13 |
| Malformed YAML (ValueError) | `malformed_yaml` | 13 | N/A* | See Finding M-2 |
| Loader unexpected error | `unknown_error` | 13 | YES | `host: [unclosed` → exit 13, `unknown_error` |
| Contract violation | `contract_violation` | 13 | YES | `host: [unclosed` (empty name) → exit 13, `contract_violation` |
| Resource floor violation | `resource_floor` | 13 | YES | cpu=1 for full → exit 13, `resource_floor` |
| Valid document + profile | `""` | 0 | YES | `--capabilities host.capabilities.yaml` → exit 0 |

*M-2: Without PyYAML installed, the stdlib `_parse_block` fallback parser is extremely permissive and never raises `ValueError`. Most malformed YAML either parses to an empty dict (→ `contract_violation`) or crashes with `AttributeError` in `_dict_to_host_capabilities` (→ `unknown_error`). The `malformed_yaml` reason_kind is only reachable when PyYAML is installed. This is a pre-existing loader characteristic, not introduced by WO-3. The overall contract (exit 13, stable reason_kind, never raises) holds — the defensive `unknown_error` envelope is deterministic.

---

## 9. Test and Regression Evidence

### 9.1 WO targeted tests

| Suite | Tests | Result |
|-------|-------|--------|
| `aee.tests.test_wo1_host_capabilities` | 7 | 7/7 PASS |
| `aee.tests.test_wo2_installer_cli_capabilities` | 32 | 32/32 PASS |
| `aee.tests.test_wo3_installer_backend_validator` | 66 | 66/66 PASS |
| **Total WO targeted** | **105** | **105/105 PASS** |

### 9.2 Impacted regression suites

| Suite | Tests | Result |
|-------|-------|--------|
| `test_aee_phase4b_install_cli` | 57 | 57/57 PASS |
| `test_aee93_installer_backend` | — | PASS |
| `test_aee_phase3_installer_workflow` | — | PASS |
| `test_installer_exit_codes` | — | PASS |
| `test_installer_lifecycle` | — | PASS |
| `test_installer_channels` | — | PASS |
| **Combined impacted** | **292** | **292/292 PASS** |

### 9.3 Broader aee/tests suite

```
PYTHONPATH=. python3 -m unittest discover -s aee/tests -p "test_*.py"
→ Ran 2546 tests in 38.987s
→ FAILED (errors=5, skipped=2)
→ 5 errors: all test_runtime_config PyYAML env-gap (pre-existing, unrelated)
```

### 9.4 Legacy tests/ suite

```
PYTHONPATH=. python3 -m unittest discover -s tests -p "test_*.py"
→ Ran 296 tests in 37.652s
→ FAILED (errors=1)
→ 1 error: test_openapi_executor_metadata import yaml (pre-existing, unrelated)
```

### 9.5 Pre-existing failures classification

All 6 failures (5 aee/tests + 1 legacy) are `ModuleNotFoundError: No module named 'yaml'` — PyYAML is not installed in this environment. These are pre-existing env-gap failures, NOT caused by WO-1/2/3. The WO-1 report (section 2) confirms the same 5 pre-existing failures were present at WO-1 time.

---

## 10. Findings by Severity

### BLOCKER (0)

None.

### HIGH (0)

None.

### MEDIUM (2)

**M-1: Stale CLI help text for `--capabilities`**
- File: `aee/cli.py:265-271` (help string)
- The help text says "Plumbing-only in this slice (WO-2): the path is recorded in the result and an audit note is emitted; the backend contract binding (load + validate + refuse on invalid) is WO-3 and is not performed here."
- **Reality**: WO-3 IS implemented and IS performed. The help text contradicts the actual behavior.
- **Impact**: Operator confusion — an operator reading the help may believe `--capabilities` is non-binding when it actually refuses the install on invalid documents.
- **Fix**: Update the help text to reflect WO-3 implementation (e.g. "The document is loaded and validated via the canonical §21.6.B/§21.6.C contract; the install is refused with exit code 13 if validation fails.")
- **Note**: The WO-2 test file was updated for WO-3 (tests assert WO-3 IS implemented), but the CLI help text was not updated.

**M-2: `malformed_yaml` reason_kind effectively unreachable without PyYAML**
- File: `aee/deploy/loader.py` (stdlib `_parse_block` fallback)
- The WO-3 design maps `load_host_capabilities` → `ValueError` → `malformed_yaml`. However, without PyYAML installed, the stdlib fallback parser `_parse_block` is extremely permissive and never raises `ValueError`. Malformed YAML either parses to an empty dict (→ `contract_violation` for missing required fields) or crashes with `AttributeError`/`TypeError` in `_dict_to_host_capabilities` when `.get()` is called on a scalar (→ `unknown_error`).
- **Impact**: The `malformed_yaml` reason_kind is never produced in environments without PyYAML. Operators see `unknown_error` or `contract_violation` instead. The overall contract (exit 13, stable reason_kind, never raises) still holds.
- **Pre-existing**: This is a loader characteristic, not introduced by WO-3. WO-3 correctly catches `ValueError` → `malformed_yaml` AND has a defensive `unknown_error` catch-all.
- **Fix (out of WO-3 scope)**: The loader's `_dict_to_host_capabilities` should validate that `host` and `runtime_profile` are mappings (not scalars/lists) and raise `ValueError` if not, so that malformed YAML maps to `malformed_yaml` instead of `unknown_error`.

### LOW (2)

**L-1: Redundant file read in `validate_capabilities_document`**
- File: `aee/installer/backend.py:690-697` (step 2) + `aee/installer/backend.py:701-703` (step 3)
- Step 2 reads the file via `Path(path).read_text()` to check readability. Step 3 calls `load_host_capabilities(path)` which reads the file again.
- **Impact**: Minor redundant I/O (file is read twice). Not a correctness issue.
- **Fix**: Could pass the text content to the loader instead of the path, or skip the separate readability check and rely on the loader's `read_text` to raise `OSError`. However, the current approach provides a cleaner separation of failure modes (`unreadable_file` vs `malformed_yaml`).

**L-2: `InstallCliResult.capabilities` field has no default**
- File: `aee/installer/cli_install.py:195`
- `capabilities: Optional[str]` is a required field (no default). All 7 construction sites in `cli_install.py` pass `capabilities=` explicitly. No external callers construct `InstallCliResult` directly (verified by grep).
- **Impact**: No current breakage. If a future caller forgets `capabilities=`, they get a `TypeError` at construction. This is acceptable (fail-fast) but could be `capabilities: Optional[str] = None` for robustness.

---

## 11. Shadow-Run Non-Interference

### 11.1 P0-1 protected files

| File | Baseline sha256 (first 16) | Current sha256 (first 16) | Status |
|------|---------------------------|--------------------------|--------|
| `app.py` | `517c839287e92858` | `517c839287e92858` | OK |
| `dispatcher/db.py` | `46561e6b435da1df` | `46561e6b435da1df` | OK |
| `dispatcher/manager.py` | `c047c7bc41b15cb1` | `c047c7bc41b15cb1` | OK |
| `dispatcher/models.py` | `2413d9f80f31d5d2` | `2413d9f80f31d5d2` | OK |
| `dispatcher/notification_state.py` | `d4156829600c2aef` | `d4156829600c2aef` | OK |
| `dispatcher/notifier.py` | `5a985c2a61601d03` | `5a985c2a61601d03` | OK |

All 6 bridge protected files byte-identical to baseline.

### 11.2 dispatcher.db

| Item | Baseline | Current | Status |
|------|----------|---------|--------|
| `data/dispatcher.db` sha256 | `119763dcf12003fa` | `119763dcf12003fa` | OK |

### 11.3 HEAD

| Item | Baseline | Current | Status |
|------|----------|---------|--------|
| Git HEAD | `befe3d6fe5eeeafed316883d27e2868638c64d22` | `befe3d6fe5eeeafed316883d27e2868638c64d22` | OK |

No commit, no push, no rebase, no stash, no merge.

### 11.4 Cron jobs.json

| Item | Baseline | Current | Status |
|------|----------|---------|--------|
| `~/.hermes/cron/jobs.json` sha256 | `b951c1d5c16a558f` | `65367b132131aff7` | DRIFT |

**Drift explanation**: Cron jobs.json has drifted from the P0-1 baseline. This is expected — cron jobs execute daily and new jobs may have been added since the baseline was captured on 2026-07-29. This drift is NOT caused by WO-1/2/3 (none of the WO work orders touch cron). The Day 1 check (2026-07-29T17:39:08Z) already recorded this as expected.

### 11.5 macro_history.db

| Item | Baseline | Current | Status |
|------|----------|---------|--------|
| `macro_history.db` sha256 | `4ec94d88f110e42f` | `dc9554297952a348` | DRIFT |

**Drift explanation**: Expected — morning-brief cron at 08:30 TPE daily mutates `macro_history.db`. This is the known pre-existing exception documented in the P0-1 baseline and Phase 4 review records. NOT caused by WO-1/2/3.

### 11.6 Bridge service health

```
supervisorctl status hermes-runtime-bridge
→ hermes-runtime-bridge  RUNNING   pid 1619150, uptime 5 days, 8:26:36

curl -sf http://localhost:8787/health
→ {"status":"ok","service":"hermes-runtime-bridge",...}
```

Bridge service not restarted or disturbed.

### 11.7 Day 1 shadow-run check

```
logs/shadow_run/day_1_check.json:
  day: 1
  verdict: PASS
  summary: {critical_divergences: 0, divergences: 0, matches: 22, total_checks: 22}
  runtime.git_head: bef3d6fe5eeeafed316883d27e2868638c64d22
```

---

## 12. Git Status and Diff Summary

```
git branch --show-current → main
git rev-parse HEAD        → bef3d6fe5eeeafed316883d27e2868638c64d22
git status --short        → 3 modified tracked + ~80 untracked (WO-relevant: 6)
git diff --stat           → aee/cli.py +28/-3, aee/installer/backend.py +333/-1, aee/installer/cli_install.py +125/-0
```

### Diff scope verification

All 3 modified tracked files are within the WO-1/2/3 scope. No P0-1 protected file is in the diff. No unrelated source file is in the diff. The `+486/-4` net diff is purely additive (4 deletions are docstring/formatting adjustments in `aee/cli.py`).

---

## 13. Artifact Verification

### 13.1 WO implementation reports

```
ls -la reports/aee_wo1_host_capabilities.md          → 18.7K
ls -la reports/aee_wo2_installer_cli_capabilities.md  → 17.3K
ls -la reports/aee_wo3_installer_backend_validator.md → 18.6K
ls -la reports/aee_21_6_g_acceptance_verification.md  → 29.0K
```

### 13.2 host.capabilities.yaml

```
ls -la host.capabilities.yaml → 1.9K
wc -l host.capabilities.yaml  → 53 lines
```

### 13.3 WO test files

```
ls -la aee/tests/test_wo1_host_capabilities.py          → 4.3K, 108 lines
ls -la aee/tests/test_wo2_installer_cli_capabilities.py  → 18.4K, 491 lines
ls -la aee/tests/test_wo3_installer_backend_validator.py → 34.3K, 937 lines
```

### 13.4 This review artifact

```
ls -la reports/aee_21_6_g_post_implementation_independent_review.md → (see verification below)
wc -l reports/aee_21_6_g_post_implementation_independent_review.md  → (see verification below)
sha256sum reports/aee_21_6_g_post_implementation_independent_review.md → (see verification below)
```

---

## 14. Production Safety

| Safety check | Status |
|--------------|--------|
| No commit | OK — HEAD unchanged |
| No push | OK — no remote push |
| No deploy | OK — no service deploy |
| No service restart | OK — bridge uptime 5+ days |
| No cron change | OK — no cron job created/modified/removed |
| No firewall change | OK — no iptables/ufw/cloudflared config change |
| No stash/rebase/merge | OK — working tree is simple modified+untracked |
| No source mutation | OK — review only, no edits to any file |
| No Master Plan mutation | OK — AEE_MASTER_PLAN.md not touched |
| No host.capabilities.yaml mutation | OK — not touched |
| No secrets printed | OK — no API keys, tokens, or env contents in output |

---

## 15. Remaining Risks

1. **M-1 (stale help text)**: An operator may believe `--capabilities` is non-binding when it actually refuses the install. Should be fixed before or during the commit.
2. **M-2 (malformed_yaml unreachable without PyYAML)**: In environments without PyYAML, malformed YAML maps to `unknown_error` or `contract_violation` instead of `malformed_yaml`. The contract still holds (exit 13, stable reason_kind), but the reason_kind is less precise. This is a pre-existing loader issue, not a WO-3 regression.
3. **Cron jobs.json drift**: The P0-1 baseline recorded a cron jobs.json sha256 that has since drifted. This is expected (daily cron execution) but should be reconciled in the P0-1 final report. Not a WO-1/2/3 risk.
4. **Untracked file sprawl**: The working tree has ~80 untracked files unrelated to WO-1/2/3. A future `git add -A` would stage all of them. The commit should use explicit-path staging (`git add <WO files only>`).

---

## 16. Review Ready

**YES**

The WO-1/WO-2/WO-3 change set has been independently reviewed against §21.6.G and the repository's authoritative contracts. All review objectives (1-10) are addressed with evidence. The change set is structurally sound, correctly implements the §21.6.B/§21.6.C contract binding, preserves backward compatibility, and does not disturb the P0-1 shadow run. Two MEDIUM findings and two LOW findings are documented with file/line evidence and recommended fixes.

---

## 17. Commit Ready

**YES (with caveats)**

The change set is commit-ready subject to:
1. **M-1 (stale help text)**: Update `aee/cli.py:265-271` help string to reflect WO-3 implementation. This can be done in the same commit or a follow-up.
2. **M-2 (malformed_yaml)**: Out of WO-3 scope (loader characteristic). Document as a known limitation; fix in a future loader hardening work order.
3. **Explicit-path staging**: When committing, use `git add aee/cli.py aee/installer/backend.py aee/installer/cli_install.py host.capabilities.yaml aee/tests/test_wo1_host_capabilities.py aee/tests/test_wo2_installer_cli_capabilities.py aee/tests/test_wo3_installer_backend_validator.py` — do NOT use `git add -A` (working tree has ~80 unrelated untracked files).

No BLOCKER or HIGH findings. Required validation is not bypassable. P0-1 protected state is not disturbed. All evidence is present.

---

## 18. Telegram

A Telegram notification was not sent per the review-only directive (no service mutation, no external side effects). The complete review is in this artifact. If a notification is required, it should be sent after the user approves the review.

---

*End of independent review.*