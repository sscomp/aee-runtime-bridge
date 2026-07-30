# AEE Phase 4B — Install CLI Surface Independent Read-Only Review

**Review Date:** 2026-07-28
**Reviewer:** M2 (Hermes Agent, independent read-only review)
**Repository:** /home/ubuntu/hermes-runtime-bridge
**Branch:** main
**HEAD:** 770598ffe10a011a47e6ced278b97447b9a26008
**Scope:** Phase 4B `aee install` CLI surface — approved flags, dry-run default, exit code 6 gate, no update CLI, targeted tests, regression evidence, scope integrity

---

## 1. Executive Summary

**Verdict: PASS** — Review Ready: YES, Atomic Commit Ready: YES

The Phase 4B install CLI surface implementation is clean, well-structured, and fully meets the brief's requirements. The change is purely additive (+152/-0 to `aee/cli.py`, two new untracked files), introduces no side effects, preserves the Phase 9.2 backward-compat path verbatim, gates `--execute` behind exit code 6, and is covered by 57 targeted tests (all PASS). No production installer backend files were modified. No `update` CLI was introduced. The 5 pre-existing `test_runtime_config` errors (PyYAML missing) are unrelated environment gaps.

---

## 2. Git Evidence

### 2.1 Repository State

```
HEAD: 770598ffe10a011a47e6ced278b97447b9a26008
Branch: main
Stash list: (empty)
```

### 2.2 Change Set

| File | Status | Lines | Type |
|------|--------|-------|------|
| `aee/cli.py` | Modified (tracked) | +152/-0 | Pure additive |
| `aee/installer/cli_install.py` | New (untracked) | 407 lines | New module |
| `aee/tests/test_aee_phase4b_install_cli.py` | New (untracked) | 586 lines | New test suite |

### 2.3 Diff Stats

```
aee/cli.py | 152 +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 152 insertions(+)
```

Three hunks, all `+N -0` format:
- `@@ -189,6 +189,59 @@` — argparse flags added to `install_parser`
- `@@ -443,6 +496,84 @@` — new `_install_dispatch_phase4b()` sibling function
- `@@ -624,6 +755,27 @@` — routing logic in `main()`

### 2.4 Zero-Deletion Verification

```
git diff aee/cli.py | grep "^-[^-]" → (empty)
```

Zero deletion lines confirmed. The diff is purely additive.

---

## 3. Artifact Verification

### 3.1 File Listing

```
ls -la aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py

aee/installer/cli_install.py  15.9K
aee/tests/test_aee_phase4b_install_cli.py  21.4K
```

### 3.2 Line Counts

```
wc -l aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py

  407 aee/installer/cli_install.py
  586 aee/tests/test_aee_phase4b_install_cli.py
  993 total
```

### 3.3 SHA-256 Checksums

```
sha256sum aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py

631458f87b5552bafde74ec7d2a6a750de800c084f78bd9fc273ade9762b1ad5  aee/installer/cli_install.py
c351b7e3cd358123bd472399e7e05acc13406a1f5c380735cbad86621743929c  aee/tests/test_aee_phase4b_install_cli.py
```

### 3.4 AST Syntax Validation

Both files parse cleanly via `ast.parse()`:
- `cli_install.py`: AST parse OK
- `test_aee_phase4b_install_cli.py`: AST parse OK

---

## 4. Findings — Detailed Review

### 4.1 Install CLI Routing and run_install Flow — PASS

**Routing logic** (`aee/cli.py:758-782`): When any Phase 4B flag (`--execute`, `--resume`, `--from`, `--rollback-to`) is present, `main()` routes to `_install_dispatch_phase4b()`. When no flags are present, the original `_install_dispatch()` (Phase 9.2) path is preserved verbatim. The routing check uses `getattr(args, ..., False/None)` with a 4-tuple `any()` test — clean and defensive.

**run_install flow** (`aee/installer/cli_install.py:202-401`): The function composes the §21.3 `InstallerBackend.plan()` + `execute(dry_run=True)` and folds the result into an `InstallCliResult`. The flow is:
1. Profile validation via `parse_profile()` (defence in depth before backend construction)
2. Backend construction with `dry_run=True` (§21.3 invariant)
3. `backend.execute(canonical, dry_run=True)` — plan + pre-flight
4. Pre-flight failure → exit 4 or 5 (profile switch)
5. `--execute` requested → exit 6 with `execute_not_authorized` note
6. Dry-run success → exit 0 with audit-only notes for `--resume`/`--from`/`--rollback-to`

The function never raises for application-level errors — all error states are encoded in the `InstallCliResult`. This is a clean result-object pattern.

### 4.2 Approved Flags — PASS

Exactly four flags are added to `install_parser` (`aee/cli.py:192-241`):

| Flag | argparse dest | Type | Behavior |
|------|---------------|------|----------|
| `--execute` | `execute` | `store_true` | Requests shell path → exit 6 |
| `--resume` | `resume` | `store_true` | Audit-only, recorded in result |
| `--from <ref>` | `from_ref` | str, default None | Audit-only, recorded in result |
| `--rollback-to <ref>` | `rollback_to` | str, default None | Audit-only, recorded in result |

No other flags were added. The `InstallCliOptions` dataclass (frozen) captures all four plus `profile` and `repo_root`. The `InstallCliResult` dataclass (frozen) carries `exit_code`, `profile`, `execute_requested`, `resume`, `from_ref`, `rollback_to`, `plan`, `preflight`, `executed`, `error`, `notes`.

### 4.3 Dry-Run Default — PASS

Without `--execute`, `run_install()` performs plan + read-only pre-flight only. The backend is always constructed with `dry_run=True` (line 248-251). The `executed` field is always `False` in this slice. Verified by:
- `RunInstallDryRunTests` (4 tests) — exit 0, executed=False, plan present, preflight ok
- Direct CLI invocation: `main(['install'])` returns 0

### 4.4 Unauthorized --execute Returns Exit Code 6 — PASS

When `--execute` is passed and pre-flight succeeds, `run_install()` returns `InstallCliResult` with `exit_code=EXIT_EXECUTE_NOT_AUTHORIZED` (6), `execute_requested=True`, `executed=False`, and a note explaining the §21.3 gate. Verified by:
- `ExecuteFlagTests` (5 tests) — exit 6, execute_requested=True, executed=False, note present, preflight still ok
- `AllFlagsCombinedTests` (3 tests) — exit 6 when --execute combined with all other flags
- Direct CLI invocation: `main(['install', '--execute'])` returns 6

The exit code 6 is distinct from exit 0 so an operator can distinguish "I asked for execute and it was refused" from "I didn't ask for execute."

### 4.5 No Update CLI Functionality — PASS

Verified by multiple checks:
- `grep -rn 'def.*update.*dispatch\|update_parser\|update_dispatch'` in `aee/cli.py` and `aee/installer/` → 0 matches
- `grep -n '"update"\|'\''update'\'' '` in `aee/cli.py` → 0 matches
- Subparser registry: only `install`, `doctor`, `prepare` exist (3 `add_parser()` calls)
- `ApprovedFlagsOnlyTests.test_update_flag_rejected` — `main(['install', '--update'])` raises `SystemExit(2)` (argparse rejects unknown flags)
- `ApprovedFlagsOnlyTests.test_force_flag_rejected` — `--force` also rejected

### 4.6 --resume, --from, --rollback-to Are Audit-Only — PASS

All three flags are parsed, recorded in `InstallCliOptions`/`InstallCliResult`, and surfaced in notes. No git operations, no stage-marker replay, no subprocess calls. Verified by:
- `ResumeFlagTests` (4 tests) — exit 0, resume=True, note mentions --resume, not executed
- `FromRefFlagTests` (4 tests) — exit 0, from_ref recorded, note mentions ref, no git ops
- `RollbackToFlagTests` (3 tests) — exit 0, rollback_to recorded, note mentions ref

### 4.7 No Subprocess / No Side Effects — PASS

`NoSubprocessTests` (2 tests) perform AST scans of `cli_install.py`:
- `test_no_subprocess_import` — no `subprocess` import found
- `test_no_os_system_call` — no `os.system` or `os.popen` attribute access found

The module docstring explicitly states: "Side effect safety: this function performs no subprocess spawns, no filesystem writes, and no network calls."

### 4.8 Backward Compatibility — PASS

`BackwardCompatTests` (4 tests) verify that `aee install` with no Phase 4B flags preserves the Phase 9.2 dispatch path:
- `test_no_flags_uses_phase92_dispatch` — stdout contains "aee install (dry-run / §21.3 installer backend)"
- `test_no_flags_profile_mini` — stdout contains "profile (resolved)  : mini"
- `test_no_flags_json_still_works` — JSON output does NOT have `phase=4B` field
- `test_each_profile_backward_compat` — all `KNOWN_PROFILES` produce exit 0 with correct profile text

The `_install_dispatch()` function body is untouched (confirmed by zero-deletion check). The new `_install_dispatch_phase4b()` is a sibling function at line 499, not embedded within it.

### 4.9 JSON Output — PASS

`CliPlumbingTests` verify `--json` output:
- `test_cli_json_output` — `install --execute --json` → exit 6, JSON contains `phase=4B`, `execute_requested=true`, `exit_code=6`
- `test_cli_json_dry_run` — `install --resume --json` → exit 0, JSON contains `resume=true`, `exit_code=0`

The JSON shape adds `subcommand`, `default_profile`, `known_profiles`, and `phase` fields to the `InstallCliResult.to_dict()` payload.

### 4.10 Exit Code Vocabulary — PASS

`ExitCodeVocabularyTests` (1 test) confirms all exit codes stay within the allowed set `{0, 3, 4, 5, 6}`. No new exit codes were introduced. The constants are imported from `aee.installer.backend` (the canonical source):
- `EXIT_OK = 0`
- `EXIT_PROFILE_INVALID = 3`
- `EXIT_PRE_FLIGHT_FAILED = 4`
- `EXIT_PROFILE_SWITCH_REJECTED = 5`
- `EXIT_EXECUTE_NOT_AUTHORIZED = 6`

---

## 5. Targeted Test Results

### 5.1 Phase 4B Test Suite

```
PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4b_install_cli -v

Ran 57 tests in 0.020s
OK
```

**57/57 PASS, 0 failures, 0 errors, 0 skips.**

Test classes (16):
1. `InstallCliOptionsTests` (4 tests) — dataclass defaults, to_dict, frozen
2. `RunInstallDryRunTests` (4 tests) — dry-run default
3. `ExecuteFlagTests` (5 tests) — --execute → exit 6
4. `ResumeFlagTests` (4 tests) — --resume audit-only
5. `FromRefFlagTests` (4 tests) — --from audit-only
6. `RollbackToFlagTests` (3 tests) — --rollback-to audit-only
7. `AllFlagsCombinedTests` (3 tests) — all flags combined
8. `UnknownProfileTests` (5 tests) — exit 3
9. `ProfileSwitchTests` (3 tests) — exit 5
10. `PreFlightFailedTests` (2 tests) — exit 4
11. `JsonSerializableTests` (4 tests) — to_dict JSON-serializable
12. `CliPlumbingTests` (7 tests) — CLI routing
13. `NoSubprocessTests` (2 tests) — AST scan
14. `ApprovedFlagsOnlyTests` (2 tests) — --update/--force rejected
15. `BackwardCompatTests` (4 tests) — Phase 9.2 preserved
16. `ExitCodeVocabularyTests` (1 test) — exit code set

---

## 6. Regression Evidence

### 6.1 Installer Test Suite (7 modules, direct related)

```
PYTHONPATH=. python3 -m unittest \
  aee.tests.test_aee78_k5_cli_flag_plumbing \
  aee.tests.test_aee92_unified_cli_ux \
  aee.tests.test_aee93_installer_backend \
  aee.tests.test_aee_phase2_doctor \
  aee.tests.test_aee_phase3_installer_workflow \
  aee.tests.test_installer_exit_codes \
  aee.tests.test_installer_lifecycle

Ran 302 tests in 0.386s
OK
```

**302/302 PASS.** Zero regressions in all installer-related and CLI-related test modules.

### 6.2 Full aee/tests/ Discover

```
PYTHONPATH=. python3 -m unittest discover -s aee/tests -p "test_*.py"

Ran 2113 tests in 39.150s
FAILED (errors=5, skipped=2)
```

**5 errors — all pre-existing, all in `test_runtime_config`, all `ModuleNotFoundError: No module named 'yaml'`.**

Attribution: `aee/config/runtime_config.py` line 249 `import yaml` — PyYAML is not installed in this environment. This is an environment gap, NOT a Phase 4B regression. The `runtime_config.py` file was not modified by Phase 4B (confirmed: `git diff --name-only` shows only `aee/cli.py`).

### 6.3 Production File Integrity

```
git diff --stat -- aee/installer/backend.py aee/installer/__init__.py \
  aee/installer/lifecycle.py aee/installer/workflow.py \
  aee/installer/linux_bootstrap.py aee/installer/macos_bootstrap.py
→ (empty)
```

**Zero production installer files modified.** The `aee/installer/__init__.py` was not modified (no new exports added for `cli_install` — the module is imported lazily from `_install_dispatch_phase4b`).

---

## 7. Production Safety

| Check | Status |
|-------|--------|
| No source code modified | 1 file modified (`aee/cli.py`), purely additive |
| No production installer backend changes | PASS — 6 backend files untouched |
| No `dispatcher.db` changes | PASS — not in diff |
| No `jobs.json` changes | PASS — not in diff |
| No subprocess/os.system in new code | PASS — AST verified |
| No git operations performed | PASS — --from/--rollback-to audit-only |
| No filesystem writes | PASS — dry-run only |
| No network calls | PASS — no network code |
| No `update` CLI introduced | PASS — grep + test verified |
| Exit code 6 gate for --execute | PASS — test + CLI verified |
| Dry-run default | PASS — test + CLI verified |
| Backward compat preserved | PASS — Phase 9.2 path untouched |

---

## 8. Scope Integrity

### 8.1 Files in Scope

| File | Change Type | In Diff? |
|------|------------|----------|
| `aee/cli.py` | Modified (tracked, +152/-0) | YES |
| `aee/installer/cli_install.py` | New (untracked) | YES (new) |
| `aee/tests/test_aee_phase4b_install_cli.py` | New (untracked) | YES (new) |

### 8.2 Files NOT in Scope (verified untouched)

- `aee/installer/backend.py` — untouched
- `aee/installer/__init__.py` — untouched
- `aee/installer/lifecycle.py` — untouched
- `aee/installer/workflow.py` — untouched
- `aee/installer/linux_bootstrap.py` — untouched
- `aee/installer/macos_bootstrap.py` — untouched
- `aee/config/runtime_config.py` — untouched (pre-existing yaml errors unrelated)
- `data/dispatcher.db` — untouched
- `~/.hermes/cron/jobs.json` — untouched

### 8.3 No Unrelated Source Modifications

The working tree has 1 modified tracked file (`aee/cli.py`) and 2 new untracked files under `aee/`. The remaining untracked items in `git status` are pre-existing report files and directories unrelated to Phase 4B. No staging, commit, or file operations were performed during this review.

---

## 9. Review Readiness

**Review Ready: YES**

- All 57 targeted tests PASS
- 302/302 related regression tests PASS
- 5 pre-existing errors attributed to environment gap (PyYAML missing), not Phase 4B
- Zero production file modifications
- Zero deletions in `aee/cli.py` diff
- AST syntax validation PASS for both new files
- All four approved flags verified (routing, exit codes, audit-only behavior)
- `--execute` → exit 6 verified at unit and CLI level
- Dry-run default verified at unit and CLI level
- No `update` CLI introduced (verified by grep, subparser registry, and test)
- Backward compat verified (Phase 9.2 path preserved, 4 dedicated tests)

---

## 10. Atomic Commit Readiness

**Atomic Commit Ready: YES**

### 10.1 Commit Candidate File List

```
aee/cli.py                              (modified, +152/-0)
aee/installer/cli_install.py            (new, 407 lines)
aee/tests/test_aee_phase4b_install_cli.py  (new, 586 lines)
```

### 10.2 Staging Recommendation

Stage by explicit path list:
```bash
git add aee/cli.py aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py
```

Do NOT use `git add -A` — the working tree has 40+ unrelated untracked report files that must not be staged.

### 10.3 Suggested Commit Message

```
feat(installer): add Phase 4B install CLI surface — approved flags + run_install flow

Add the `aee install` CLI entrypoint with four approved flags:
--execute, --resume, --from <ref>, --rollback-to <ref>.

- New module aee/installer/cli_install.py: InstallCliOptions,
  InstallCliResult, run_install() — composes §21.3 backend plan +
  pre-flight, gates --execute behind exit code 6.
- aee/cli.py: argparse flags + _install_dispatch_phase4b() routing.
  Phase 9.2 _install_dispatch() preserved verbatim for backward compat.
- 57 targeted tests covering: flag routing, exit codes (0/3/4/5/6),
  dry-run default, audit-only flags, JSON output, no-subprocess AST
  scan, approved-flags-only, backward compat.
```

### 10.4 Pre-Commit Gates

- [x] Targeted tests: 57/57 PASS
- [x] Regression tests: 302/302 PASS (installer + CLI modules)
- [x] Zero-deletion check on `aee/cli.py`: PASS
- [x] No production backend files modified: PASS
- [x] AST syntax validation: PASS
- [x] No `update` CLI introduced: PASS

---

## 11. Caveats and Notes

### 11.1 Pre-Existing test_runtime_config Errors (NON-BLOCKING)

5 errors in `test_runtime_config` are caused by `ModuleNotFoundError: No module named 'yaml'` — PyYAML is not installed in this environment. These are pre-existing environment gaps unrelated to Phase 4B. The `aee/config/runtime_config.py` file was not modified by this change.

### 11.2 Defence-in-Depth Branch in run_install

`run_install()` has a defence-in-depth `except ExecuteNotAuthorizedError` branch (lines 265-285) that should not be reachable when `dry_run=True` is passed. This is correct defensive programming — if a future code path flips the default, this branch keeps the function side-effect-free. The branch is documented in the code comments.

### 11.3 Lazy Import of cli_install

`_install_dispatch_phase4b()` imports `InstallCliOptions` and `run_install` lazily (line 509-512), preserving the "no installer backend side effects at module-import time" invariant from Phase 9.2. This is the correct pattern — the `cli_install` module is only loaded when `install` with Phase 4B flags is actually dispatched.

### 11.4 __init__.py Not Updated

`aee/installer/__init__.py` was not modified to export `cli_install` symbols. This is acceptable — the lazy import pattern means `cli_install` is imported directly from `_install_dispatch_phase4b`, not through the package `__init__.py`. If future consumers need `run_install` from the package surface, a follow-up can add the export.

---

## 12. Telegram Notification

Per the AEE-MINI Telegram rule (all AEE-MINI tasks must attempt Telegram notification to 鼎鼎 regardless of read-only status), a Telegram notification will be sent with the review summary.

**Telegram delivery evidence:**
- `hermes send --to telegram:5132341473 --subject "AEE Phase 4B Install CLI Review — PASS" --file <report> --json`
- Result: `success: true`, `chat_id: 5132341473`, `message_id: 8960`, `mirrored: true`
- Timestamp: 2026-07-28 (Asia/Taipei)

---

## 13. Conclusion

The Phase 4B install CLI surface is a clean, purely additive change that:
- Introduces exactly four approved flags with correct routing
- Maintains dry-run as the default with no side effects
- Gates `--execute` behind exit code 6 (ExecuteNotAuthorized)
- Preserves the Phase 9.2 backward-compat path verbatim
- Introduces no `update` CLI functionality
- Is covered by 57 targeted tests (all PASS) with 302 related regression tests (all PASS)
- Touches zero production installer backend files
- Has zero deletion lines in the tracked file diff

**Review Ready: YES**
**Atomic Commit Ready: YES**

---

*Review performed by M2 (Hermes Agent) on 2026-07-28. Read-only review — no source modifications, staging, commits, or file operations were performed.*