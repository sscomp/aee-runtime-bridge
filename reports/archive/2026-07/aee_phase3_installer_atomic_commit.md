# AEE Phase 3 Installer — Atomic Commit Report

**Commit SHA:** `f8fe2c918a2173c54b147f1380380e699f478ce1`
**Parent SHA:** `6b2609a473e831648b11ab0d2100b0d8bbd0f0f0` (feat(aee): add Phase 2 'aee doctor' readiness health check)
**Branch:** main
**Commit time:** 2026-07-27 10:47:10 UTC
**Repository:** /home/ubuntu/hermes-runtime-bridge

---

## 1. Summary

Single atomic commit for the approved Phase 3 AEE Installer implementation.
Composes the Phase 2 doctor + §21.3 installer backend + W2/W3 platform
bootstrap detection + directory init + config bootstrap + projected
post-install verification into a single dry-run-by-default workflow exposed
via the new `aee prepare` CLI subcommand.

No production files modified outside the 3-file approved scope. No side
effects (dry-run only). No push.

## 2. Exact File List

| # | Path | Status | Lines | Size |
|---|------|--------|-------|------|
| 1 | `aee/cli.py` | Modified (M) | 680 | 26.7K |
| 2 | `aee/installer/workflow.py` | Added (A) | 918 | 32.6K |
| 3 | `aee/tests/test_aee_phase3_installer_workflow.py` | Added (A) | 766 | 27.8K |

Total: 3 files changed, 1849 insertions(+), 0 deletions(-).

## 3. Insertions / Deletions

```
 aee/cli.py                                      | 163 +++++
 aee/installer/workflow.py                       | 919 ++++++++++++++++++++++++
 aee/tests/test_aee_phase3_installer_workflow.py | 767 ++++++++++++++++++++
 3 files changed, 1849 insertions(+)
```

Purely additive. Zero deletions. `aee/cli.py` is the only modified file
(+163 lines, no removals); the other two are new files.

## 4. git status (post-commit)

Staging area clean (no staged changes). Working tree retains pre-existing
untracked reports/scripts/requirements files that were intentionally NOT
part of this atomic commit (explicit-path staging only — no `git add -A`).

```
M  aee/cli.py                                    (committed)
A  aee/installer/workflow.py                     (committed)
A  aee/tests/test_aee_phase3_installer_workflow.py (committed)
```

Untracked items excluded from commit (partial list): AEE_*.md reports,
k3_*.md, executor_router_*.md, requirements*.lock, scripts/, reports/.
These are pre-existing working-tree residue and out of scope.

## 5. Artifact Verification (ls -la / wc -l / sha256sum)

### ls -la
```
-rw------- 1 ubuntu ubuntu 26.7K  aee/cli.py
-rw------- 1 ubuntu ubuntu 32.6K  aee/installer/workflow.py
-rw------- 1 ubuntu ubuntu 27.8K  aee/tests/test_aee_phase3_installer_workflow.py
```

### wc -l
```
   680 aee/cli.py
   918 aee/installer/workflow.py
   766 aee/tests/test_aee_phase3_installer_workflow.py
  2364 total
```

### sha256sum
```
9fc76b21039d04a3cc8a34f14bd62fe8639c24ebb42dc19a4ab2d66846903ce1  aee/cli.py
385b172472aa5dd33c9c9d1bfe8c06e30b05fea1ade278b7a46d07fb89736843  aee/installer/workflow.py
f3a6c9442013117413926774bc8e92efd9b8911598e3177561c249c570ac5964  aee/tests/test_aee_phase3_installer_workflow.py
```

SHA-256 values are stable pre-commit and post-commit (verified identical).

## 6. Targeted Tests

```
$ python3 -m unittest aee.tests.test_aee_phase3_installer_workflow

Ran 32 tests in 0.225s
OK
```

32/32 PASS. Coverage: workflow composition, dry-run default,
execute-not-authorized guard, profile handling (full/mini/edge/developer/
unknown), stage result DTOs (DirectoryInitPlan, ConfigBootstrapPlan,
PlatformBootstrapPlan, PostInstallVerification, WorkflowSummary,
InstallWorkflowResult), overall verdict folding + exit-code mapping,
CLI plumbing (argparse wiring, --json output, --no-network, exit codes),
and AST source scan (no subprocess import in workflow module).

## 7. Impacted Regression (full aee/tests suite)

```
$ python3 -m unittest discover -s aee/tests -p 'test_*.py'

Ran 2005 tests in 39.020s
FAILED (errors=5, skipped=2)
```

**Pre-existing baseline failures (5 errors, all PyYAML missing — unrelated
to Phase 3):**
- `test_apply_registers_definitions` (test_runtime_config)
- `test_apply_replace_overrides_existing` (test_runtime_config)
- `test_apply_uses_default_runtime_id` (test_runtime_config)
- `test_env_substitution` (test_runtime_config)
- `test_load_full` (test_runtime_config)

All 5 errors are `ModuleNotFoundError: No module named 'yaml'` in
`aee/tests/test_runtime_config.py` — a pre-existing environment gap
(PyYAML not installed). Confirmed via stash-and-rerun: the same 5 errors
are present without the Phase 3 changes (baseline = 5 errors).
Phase 3 adds zero new failures.

**Impacted regression check (installer_lifecycle, the test module most
adjacent to Phase 3 scope):**
```
$ python3 -m unittest aee.tests.test_installer_lifecycle

Ran 54 tests in 0.001s
OK
```
54/54 PASS — no regression in the existing installer lifecycle tests.

## 8. Production Safety

- **No side effects:** `run_workflow` defaults to `dry_run=True`;
  `dry_run=False` raises `ExecuteNotAuthorizedError` (§21.3 guard).
  The `aee prepare` subcommand has no `--execute` flag.
- **No push:** commit only; no `git push` executed.
- **Explicit-path staging:** `git add aee/cli.py aee/installer/workflow.py
  aee/tests/test_aee_phase3_installer_workflow.py` — no `git add -A`.
- **No production files modified outside the 3-file scope.** The only
  modified file (`aee/cli.py`) received purely additive changes (+163/−0).
- **Lazy import:** `aee prepare` imports `aee.installer.workflow` lazily
  so a missing optional dependency cannot break `aee install` or
  `aee doctor`.
- **Exit codes reuse existing vocabulary** (0/4/5/6/7/8) — no new
  exit-code contract introduced.

## 9. Telegram Notification

Attempted via `hermes send` to chat_id 5132341473 (鼎鼎). See final
response for delivery result (success / message_id).

## 10. Durable Artifact

This report is the single durable artifact:
`/home/ubuntu/hermes-runtime-bridge/reports/aee_phase3_installer_atomic_commit.md`