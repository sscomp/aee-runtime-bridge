# AEE Phase 4B — `aee install` CLI Implementation Report

**Date:** 2026-07-28
**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**Head (unchanged):** `770598f feat(installer): add Phase 4A bootstrap v1 exit-code exception hierarchy (§10.4)`
**Status:** Implemented, NOT committed, NOT pushed (per brief)

---

## 1. Scope

Phase 4B adds the `aee install` CLI surface with the four approved flags:

- `--execute` — request shell-level install execution (gated by §21.3 `ExecuteNotAuthorizedError`)
- `--resume` — resume a previously-started install run (audit-only in this slice)
- `--from <ref>` — install from a specific git ref (audit-only)
- `--rollback-to <ref>` — rollback to a specific git ref before installing (audit-only)

**OUT of scope:** `aee update` CLI (explicitly excluded by brief).
**NOT committed, NOT pushed** per brief.

---

## 2. Files Changed

| File | Status | Lines | Description |
|------|--------|-------|-------------|
| `aee/installer/cli_install.py` | NEW | 407 | `run_install` flow + `InstallCliOptions` / `InstallCliResult` dataclasses |
| `aee/cli.py` | MODIFIED | +152 / -0 | argparse flags + `_install_dispatch_phase4b` + routing in `main()` |
| `aee/tests/test_aee_phase4b_install_cli.py` | NEW | 586 | 57 targeted tests across 12 test classes |

**Total:** 3 files, +1145 lines, 0 deletions.

---

## 3. Artifact Verification

```
$ ls -la aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py aee/cli.py
aee/cli.py                              30.6K  (modified)
aee/installer/cli_install.py            15.9K  (new)
aee/tests/test_aee_phase4b_install_cli.py 21.4K  (new)

$ wc -l aee/cli.py aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py
832 cli.py                                  (was 680 → +152)
407 installer/cli_install.py
586 tests/test_aee_phase4b_install_cli.py
Σ 1825

$ sha256sum aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py aee/cli.py
631458f87b5552bafde74ec7d2a6a750de800c084f78bd9fc273ade9762b1ad5  aee/installer/cli_install.py
c351b7e3cd358123bd472399e7e05acc13406a1f5c380735cbad86621743929c  aee/tests/test_aee_phase4b_install_cli.py
b89cb3643af95482c88399713ccd05f2e72ec215b807c2a4469985635c70fb26  aee/cli.py
```

### Git Status

```
$ git status --short aee/cli.py aee/installer/cli_install.py aee/tests/test_aee_phase4b_install_cli.py
 M aee/cli.py
?? aee/installer/cli_install.py
?? aee/tests/test_aee_phase4b_install_cli.py
```

### Diff stat (cli.py — pure additive)

```
$ git diff --stat aee/cli.py
aee/cli.py | 152 +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
1 file changed, 152 insertions(+)
```

**Zero deletions** in `aee/cli.py` — the Phase 9.2 `_install_dispatch` path is preserved verbatim for backward compat.

---

## 4. Design Decisions

### 4.1 Dual-dispatch routing

When NO Phase 4B flags are present, `main()` routes to the existing Phase 9.2 `_install_dispatch` (exact stdout text preserved — existing tests assert on `"profile (resolved)  : full"` etc.). When ANY of the four approved flags is present, `main()` routes to the new `_install_dispatch_phase4b`, which delegates to `run_install`.

This preserves backward compat byte-for-byte: `aee install` and `aee install --profile mini` behave identically to before.

### 4.2 `--execute` → exit 6 (not exit 0)

When `--execute` is passed, `run_install` returns `exit_code=EXIT_EXECUTE_NOT_AUTHORIZED` (6), not 0. This lets an operator distinguish "I asked for execute and it was refused" from "I didn't ask for execute" (exit 0). The `executed` field is always `False` — the §21.3 shell-level execution path remains gated by `ExecuteNotAuthorizedError`.

### 4.3 `--resume` / `--from` / `--rollback-to` are audit-only

These flags are parsed, recorded in `InstallCliOptions` and `InstallCliResult`, and surfaced in notes. They perform NO git operations and NO stage-marker replay. The future shell trampolines (W6/W7) are the separately authorizable follow-up that will consume them.

### 4.4 No subprocess, no os.system

AST scan of `cli_install.py` confirms zero `subprocess` imports and zero `os.system` / `os.popen` calls — matching the §21.3 backend invariant.

---

## 5. Test Results

### 5.1 Phase 4B targeted tests

```
$ PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4b_install_cli -v
Ran 57 tests in 0.020s
OK
```

57 tests across 12 test classes:
- `InstallCliOptionsTests` (4) — dataclass defaults, to_dict, frozen
- `RunInstallDryRunTests` (4) — dry-run default
- `ExecuteFlagTests` (5) — --execute → exit 6
- `ResumeFlagTests` (4) — --resume audit-only
- `FromRefFlagTests` (4) — --from audit-only
- `RollbackToFlagTests` (3) — --rollback-to audit-only
- `AllFlagsCombinedTests` (3) — all flags together
- `UnknownProfileTests` (5) — exit 3
- `ProfileSwitchTests` (3) — exit 5
- `PreFlightFailedTests` (2) — exit 4
- `JsonSerializableTests` (4) — to_dict JSON-serializable
- `CliPlumbingTests` (7) — CLI routing
- `NoSubprocessTests` (2) — AST scan
- `ApprovedFlagsOnlyTests` (2) — --update / --force rejected
- `BackwardCompatTests` (4) — Phase 9.2 dispatch preserved
- `ExitCodeVocabularyTests` (1) — no new exit codes

### 5.2 Impacted regression suites

```
$ PYTHONPATH=. python3 -m unittest \
    aee.tests.test_aee92_unified_cli_ux \
    aee.tests.test_aee93_installer_backend \
    aee.tests.test_aee_phase3_installer_workflow \
    aee.tests.test_installer_exit_codes \
    aee.tests.test_aee_phase4b_install_cli
Ran 233 tests in 0.310s
OK
```

176 pre-existing + 57 new = 233 tests, all PASS.

### 5.3 Full aee.tests suite

```
$ PYTHONPATH=. python3 -m unittest discover -s aee/tests
Ran 2113 tests in 39.208s
FAILED (errors=5, skipped=2)
```

The 5 errors are ALL pre-existing environment-gap failures (`ModuleNotFoundError: No module named 'yaml'` in `test_runtime_config.py`), confirmed by `git stash` baseline (same 5 errors with Phase 4B changes stashed). **Zero regressions introduced by Phase 4B.**

### 5.4 Epic 9.x suite

```
$ PYTHONPATH=. python3 -m unittest discover -s aee/tests -p "test_aee9*.py"
Ran 517 tests in 0.189s
OK
```

---

## 6. Production Safety

| Safety property | Status |
|----------------|--------|
| Dry-run by default | ✅ `run_install` performs plan + read-only pre-flight only |
| `--execute` gated | ✅ Returns exit 6, `executed=False`, `ExecuteNotAuthorizedError` |
| No subprocess | ✅ AST scan confirms zero `subprocess` / `os.system` / `os.popen` |
| No filesystem writes | ✅ Only reads `.aee-profile` marker via backend |
| No git operations | ✅ `--from` / `--rollback-to` recorded but not executed |
| Backward compat | ✅ Phase 9.2 dispatch preserved verbatim (no flags → old path) |
| No new exit codes | ✅ Reuses {0, 3, 4, 5, 6} |
| No commit / push | ✅ Per brief — working tree changes only |
| Protected files untouched | ✅ `aee/installer/backend.py`, `aee/installer/workflow.py`, `aee/installer/lifecycle.py` byte-identical |

---

## 7. CLI Surface Examples

### Dry-run (backward compat, Phase 9.2 path)

```
$ aee install
aee install (dry-run / §21.3 installer backend)
  profile (resolved)  : full
  ...
```

### `--execute` (Phase 4B path, exit 6)

```
$ aee install --execute
aee install (Phase 4B / §21.3 installer CLI)
  profile (resolved)  : full
  execute_requested   : True
  resume              : False
  from_ref            : None
  rollback_to         : None
  executed            : False
  exit_code           : 6
  plan steps          : 5 (preflight, venv, supervisor_conf, health_check, verify)
  preflight ok        : True
  note                : Phase 4B: --execute received but the §21.3 shell-level...
```

### `--resume --from v2.0.0 --rollback-to v1.9.0` (audit-only, exit 0)

```
$ aee install --resume --from v2.0.0 --rollback-to v1.9.0
aee install (Phase 4B / §21.3 installer CLI)
  profile (resolved)  : full
  execute_requested   : False
  resume              : True
  from_ref            : v2.0.0
  rollback_to         : v1.9.0
  executed            : False
  exit_code           : 0
  ...
  note                : Phase 4B: --resume received; recorded for the future shell layer...
  note                : Phase 4B: --from v2.0.0 recorded; no git operations performed.
  note                : Phase 4B: --rollback-to v1.9.0 recorded; no git operations performed.
```

### `--json` output

```
$ aee install --execute --json
{"exit_code": 6, "execute_requested": true, "phase": "4B", ...}
```

---

## 8. Telegram Notification

Telegram notification will be attempted to 鼎鼎 (chat_id 5132341473) per the AEE-MINI Telegram rule. Result recorded below.

---

## 9. Verdict

**PASS** — Phase 4B `aee install` CLI surface implemented with all four approved flags, 57 targeted tests green, 233 impacted regression tests green, 517 Epic 9.x tests green, zero regressions in full suite (5 pre-existing env-gap errors unchanged), zero deletions in `cli.py`, no commit / push per brief.

**Deliverable:** This report at `/home/ubuntu/hermes-runtime-bridge/reports/aee_phase4b_install_cli_implementation.md`.