# Phase 4A Exit Codes — Independent Read-Only Review

**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**HEAD:** `f8fe2c918a2173c54b147f1380380e699f478ce1` (feat(aee): add Phase 3 installer workflow (aee prepare))
**Review type:** Read-only independent review (no source modification, no stage/commit/push/merge/rebase/stash/deploy/restart/delete)
**Reviewer:** M2 (Hermes, ollama-cloud / glm-5.2)
**Review window (CST):** 2026-07-27 23:12 +0800
**Review window (UTC):** 2026-07-27 15:12 UTC

---

## 1. Scope & Verdict

**Scope:** Phase 4A exit-code surface — six new exit-code constants (7–12) and six matching exception classes in `aee.installer.backend`, re-exported through `aee.installer.__init__`, plus the targeted test file `aee/tests/test_installer_exit_codes.py`.

**Overall verdict:** ✅ **PASS — Review Ready, Atomic Commit Ready (with the two untracked-file caveats in §10).**

The implementation is internally consistent, the verified constants (0,2,3,4,5,6) are byte-identical to the pre-Phase-4A baseline, the six new constants (7–12) match the §10.4 proposal exactly, the exception hierarchy is correct, the public re-exports are in place, and the targeted test suite (51 tests) passes cleanly. No unrelated source files were modified. The 5 pre-existing `test_runtime_config` errors are environment-only (PyYAML not installed in this container) and have zero topical overlap with Phase 4A.

---

## 2. Working-tree state (git evidence)

- `git rev-parse HEAD` → `f8fe2c918a2173c54b147f1380380e699f478ce1`
- `git stash list` → `No stashes`
- `git status --short` (Phase-4A-relevant subset):
  - ` M aee/installer/__init__.py`     (tracked, modified)
  - ` M aee/installer/backend.py`      (tracked, modified)
  - `?? aee/tests/test_installer_exit_codes.py`  (untracked, new)
- `aee/installer/lifecycle.py` — **clean** (no working-tree diff; the §10.4 constants are already committed at HEAD `f8fe2c9`).
- `/usr/bin/git diff HEAD --stat` (tracked-only, excludes untracked):
  ```
  aee/installer/__init__.py |  14 +++++
  aee/installer/backend.py  | 147 ++++++++++++++++++++++++++++++++++++++++++++++
  2 files changed, 161 insertions(+)
  ```
- `/usr/bin/git diff aee/installer/__init__.py aee/installer/backend.py | grep "^-" | grep -v "^---"` → **empty** (zero deletions; purely additive).

**Conclusion:** The Phase 4A surface touches exactly 2 tracked files (both purely additive, +161/-0) and introduces 1 untracked test file. `aee/installer/lifecycle.py` is already committed at HEAD and was NOT modified by this round — it owns the §10.4 constants (W1 skeleton). The backend imports them; it does not redefine them.

---

## 3. Exit-code constant mapping (new codes 7–12)

Source of truth: `aee/installer/lifecycle.py` lines 99–117 (already committed at HEAD).

| Constant | lifecycle.py value | backend.py usage | Test assertion | Match |
|---|---|---|---|---|
| `EXIT_STAGE_FAILED_RETRYABLE`   | 7  | `StageFailedRetryableError.exit_code`   | `test_exit_stage_failed_retryable_is_7`   | ✅ |
| `EXIT_STAGE_FAILED_PERMANENT`   | 8  | `StageFailedPermanentError.exit_code`   | `test_exit_stage_failed_permanent_is_8`   | ✅ |
| `EXIT_DRIFT_DETECTED`           | 9  | `DriftDetectedError.exit_code`          | `test_exit_drift_detected_is_9`           | ✅ |
| `EXIT_NETWORK_ERROR`            | 10 | `NetworkError.exit_code`                | `test_exit_network_error_is_10`           | ✅ |
| `EXIT_SECRET_MISSING`           | 11 | `SecretMissingError.exit_code`          | `test_exit_secret_missing_is_11`          | ✅ |
| `EXIT_DEPENDENCY_FLOOR_NOT_MET` | 12 | `DependencyFloorNotMetError.exit_code`  | `test_exit_dependency_floor_not_met_is_12`| ✅ |

Independent runtime verification (Python):
```
OK StageFailedRetryableError           exit_code=7  const=7  expected=7
OK StageFailedPermanentError           exit_code=8  const=8  expected=8
OK DriftDetectedError                  exit_code=9  const=9  expected=9
OK NetworkError                        exit_code=10  const=10  expected=10
OK SecretMissingError                  exit_code=11  const=11  expected=11
OK DependencyFloorNotMetError          exit_code=12  const=12  expected=12
ALL ASSERTIONS PASSED
```

**Distinctness:** `test_proposed_constants_are_distinct` and `test_all_proposed_constants_in_free_range_7_to_12` both pass — the six values are mutually distinct and all lie in the free range {7..12} per §10.4. No collisions with {0,2,3,4,5,6} (`test_no_collision_between_verified_and_proposed` passes).

---

## 4. Verified exit codes (0, 2, 3, 4, 5, 6) — unchanged

| Constant | Location | Value | Phase-4A touched? |
|---|---|---|---|
| `EXIT_OK`                       | `aee/installer/backend.py:73` | 0 | No (committed pre-Phase-4A) |
| `EXIT_PARSE_ERROR`              | `aee/cli.py:61`               | 2 | No (different module, untouched) |
| `EXIT_PROFILE_INVALID`          | `aee/installer/backend.py:74` | 3 | No |
| `EXIT_PRE_FLIGHT_FAILED`        | `aee/installer/backend.py:75` | 4 | No |
| `EXIT_PROFILE_SWITCH_REJECTED`   | `aee/installer/backend.py:76` | 5 | No |
| `EXIT_EXECUTE_NOT_AUTHORIZED`   | `aee/installer/backend.py:77` | 6 | No |

`/usr/bin/git diff aee/installer/backend.py` shows **zero deletions** and **zero modifications** to lines 73–77 (the verified constants block). The diff is purely additive — the new Phase 4A block (lines 177–307 in the new file) is appended after the existing `ExecuteNotAuthorizedError` class without disturbing any prior definition.

`test_runtime_config.py` and `aee/cli.py` were NOT modified by this round (`git diff HEAD -- aee/tests/test_runtime_config.py aee/config/runtime_config.py aee/cli.py` is empty).

`TestVerifiedExitCodeConstantsUnchanged` (5 tests) + `test_verified_exception_exit_codes_unchanged` all pass — the verified exception classes keep their pinned exit codes.

---

## 5. Exception hierarchy & public exports

**Hierarchy (all confirmed by `TestExceptionHierarchy`):**
- `InstallerError(Exception)` — base, `exit_code = EXIT_OK` (0)
  - `ProfileSwitchRejectedError`     → 5  (pre-existing)
  - `PreFlightFailedError`           → 4  (pre-existing)
  - `ExecuteNotAuthorizedError`      → 6  (pre-existing)
  - `StageFailedRetryableError`     → 7  (Phase 4A new)
  - `StageFailedPermanentError`     → 8  (Phase 4A new)
  - `DriftDetectedError`             → 9  (Phase 4A new)
  - `NetworkError`                   → 10 (Phase 4A new)
  - `SecretMissingError`             → 11 (Phase 4A new)
  - `DependencyFloorNotMetError`     → 12 (Phase 4A new)

All six new classes subclass `InstallerError` (and transitively `Exception`) — verified by `test_all_new_exceptions_subclass_installer_error` and `test_all_new_exceptions_subclass_exception`.

**Exit-code distinctness across the full hierarchy:**
- `test_new_exception_exit_codes_are_distinct` — 6 new codes mutually distinct ✅
- `test_new_exception_exit_codes_do_not_collide_with_verified` — no overlap with {0,3,4,5,6} ✅
- `test_verified_exception_exit_codes_unchanged` — verified classes unchanged ✅

**Public exports (`aee/installer/__init__.py`):**
- Import block (lines 72–84 region): the 6 new exception classes are imported from `aee.installer.backend`.
- `__all__` (lines 117–136 region): the 6 new class names are added as string entries.
- `TestConstantsAreReExportedFromPackage` (3 tests) confirms the constants are reachable as `aee.installer.<NAME>` and listed in `__all__`.
- `TestConstantIdentity` (3 tests) confirms the backend imports the constants from lifecycle (no redefinition/renumbering drift).

**Constructor signatures (structured fields):**
- `StageFailedRetryableError(stage, reason="")` → stores `stage`, `reason`
- `StageFailedPermanentError(stage, reason="")` → stores `stage`, `reason`
- `DriftDetectedError(field, expected, actual)` → stores `field`, `expected`, `actual`
- `NetworkError(operation, reason="")` → stores `operation`, `reason`
- `SecretMissingError(secret_name)` → stores `secret_name` (only; never accepts a value — `test_secret_missing_error_message_does_not_include_value_param` verifies the signature is exactly `["self", "secret_name"]`)
- `DependencyFloorNotMetError(dependency, required, found)` → stores `dependency`, `required`, `found`

**No-raise-yet design (intentional):** A `grep -rn "raise (StageFailedRetryableError|StageFailedPermanentError|DriftDetectedError|NetworkError|SecretMissingError|DependencyFloorNotMetError)" aee/ --include="*.py"` (excluding tests) returns **zero hits**. The classes are introduced as the raisable vocabulary for the future W4/W5 CLI / W6/W7 shell layers; no call site raises them yet. This matches the docstrings ("Phase 4A introduces the class; the shell layer (W6/W7) will raise it") and is the correct shape for a skeleton slice.

**Import-graph safety:** `aee.installer.lifecycle` imports only `aee.platform.current` (line 79). `aee.installer.backend` imports `aee.profiles.descriptor` (line 45) and `aee.installer.lifecycle` (line 59). No circular import — confirmed by successful `python3 -c "import aee.installer; import aee.installer.backend; import aee.installer.lifecycle"`.

**Source-contract tests (TestSourceContracts):**
- `test_backend_module_does_not_import_subprocess` — backend.py source contains no `import subprocess` / `from subprocess` ✅
- `test_backend_module_does_not_use_os_system` — no `os.system(` / `os.popen(` ✅
- `test_secret_missing_error_message_does_not_include_value_param` — `SecretMissingError.__init__` signature is exactly `(self, secret_name)` ✅

---

## 6. Targeted tests

**File:** `aee/tests/test_installer_exit_codes.py` (untracked, new)

**Run command:** `PYTHONPATH=. python3 -m unittest aee.tests.test_installer_exit_codes -v`

**Result:**
```
Ran 51 tests in 0.002s
OK
```

**Test class coverage (7 classes, 51 tests):**
1. `TestProposedExitCodeConstants` (7) — 6 value assertions + distinctness + free-range
2. `TestVerifiedExitCodeConstantsUnchanged` (6) — 5 value assertions + no-collision
3. `TestConstantsAreReExportedFromPackage` (3) — package-level reachability + `__all__`
4. `TestExceptionHierarchy` (5) — subclass + distinctness + no-collision + verified-unchanged
5. `TestStageFailedRetryableError` / `TestStageFailedPermanentError` / `TestDriftDetectedError` / `TestNetworkError` / `TestSecretMissingError` / `TestDependencyFloorNotMetError` (per-class: exit_code, construction, message shape, raisable-as-InstallerError)
6. `TestSourceContracts` (3) — no subprocess, no os.system, no secret-value param
7. `TestConstantIdentity` (3) — backend imports (not redefines) the 6 constants; backend & lifecycle reference the same values

All 51 tests pass. Zero failures, zero errors, zero skips.

---

## 7. Regression evidence

### 7.1 Direct-neighbor suites (installer surface)

```
PYTHONPATH=. python3 -m unittest \
  aee.tests.test_installer_exit_codes \
  aee.tests.test_aee93_installer_backend \
  aee.tests.test_installer_lifecycle \
  aee.tests.test_aee_phase3_installer_workflow

Ran 191 tests in 0.252s
OK
```

The installer-backend, installer-lifecycle, and Phase-3 installer-workflow suites (the three closest neighbors in the dependency graph) all pass alongside the new Phase 4A suite. 191/191 PASS, 0 failures, 0 errors, 0 skips.

### 7.2 Full aee/tests discovery

```
PYTHONPATH=. python3 -m unittest discover -s aee/tests -t .

Ran 2056 tests in 38.159s
FAILED (errors=5, skipped=2)
```

**The 5 errors are ALL in `aee.tests.test_runtime_config` and ALL have the same root cause:**
```
ModuleNotFoundError: No module named 'yaml'
aee.config.runtime_config.RuntimeConfigError: PyYAML is required to load the runtime config;
install with `pip install PyYAML`
```

**Attribution:** `test_runtime_config.py` and `aee/config/runtime_config.py` were last touched at commit `7eb0731 AEE-5: Runtime Capability Registry + Multi-Runtime Dispatch Foundation` — a pre-Phase-4A commit. `git diff HEAD -- aee/tests/test_runtime_config.py aee/config/runtime_config.py` is **empty** (Phase 4A did not touch them). The errors are an environment gap (PyYAML not installed in this container), not a Phase 4A regression. This is the same pre-existing baseline observed in prior AEE-7.x review sessions (documented in `aee-iteration-pattern` skill case studies).

**The 2 skips** are pre-existing deferred-tripwire / platform-conditional skips, unrelated to Phase 4A.

**No Phase 4A test fails. No installer test fails. No installer test errors.**

---

## 8. No unrelated source modifications

Confirmed via three independent checks:

1. `/usr/bin/git diff HEAD --stat` (tracked-only) → only `aee/installer/__init__.py` (+14) and `aee/installer/backend.py` (+147). Zero other tracked files modified.
2. `/usr/bin/git diff aee/installer/__init__.py aee/installer/backend.py | grep "^-" | grep -v "^---"` → **empty** (zero deletions across both files).
3. `git diff HEAD -- aee/tests/test_runtime_config.py aee/config/runtime_config.py aee/cli.py aee/installer/lifecycle.py` → **empty** (no diff to these four files).

The only untracked addition is `aee/tests/test_installer_exit_codes.py` (the targeted test file). No other untracked file in the working tree is attributable to Phase 4A (the long list of `??` entries — `AEE_*.md`, `k3_*.md`, `requirements*.lock`, etc. — predates this round and is unrelated installer/AEE-7.x residue; none of them are staged or referenced by the Phase 4A diff).

---

## 9. Production safety

- **No source deletion:** zero `-` lines in the diff.
- **No renumbering:** the 6 verified constants (0,3,4,5,6 in backend; 2 in cli) are byte-identical to HEAD; the 6 new constants live in `lifecycle.py` which is unmodified this round.
- **No redefinition:** `TestConstantIdentity.test_backend_does_not_redefine_proposed_constants` confirms backend.py imports (not assigns) the 6 constants — prevents future silent renumber drift.
- **No circular import:** lifecycle → platform.current only; backend → profiles.descriptor + lifecycle. Verified by successful runtime import.
- **No subprocess / os.system:** `TestSourceContracts` confirms backend.py introduces no shell-out surface.
- **No secret leakage:** `SecretMissingError.__init__` accepts only `secret_name`; the docstring explicitly states "Never include the secret value in the message — only the secret *name*." The signature test pins this.
- **No raise-yet:** no call site raises the new exceptions — they are introduced as vocabulary for future W4/W5/W6/W7 layers. No behavioral change to any existing code path.
- **py_compile:** `python3 -m py_compile` on all 4 files (init, backend, lifecycle, test) → OK.
- **`aee/installer/lifecycle.py` unmodified** — the §10.4 constants were committed in a prior round (HEAD `f8fe2c9`); this round only wires the backend to import them and adds the exception classes.

---

## 10. Review Ready & Atomic Commit Ready

### Review Ready: ✅ YES
- 51/51 targeted tests pass.
- 191/191 direct-neighbor suite tests pass.
- Exit-code mapping independently verified at runtime (Python assertions).
- Exception hierarchy, public exports, and source contracts all verified.
- Zero unrelated source modifications.
- The 5 full-suite errors are pre-existing environment gaps (PyYAML), not Phase 4A regressions.

### Atomic Commit Ready: ✅ YES (with 2 caveats)

**Commit-candidate file set (3 files):**
1. `aee/installer/__init__.py`        (tracked, modified, +14/-0)
2. `aee/installer/backend.py`         (tracked, modified, +147/-0)
3. `aee/tests/test_installer_exit_codes.py` (untracked, new, 454 lines)

**Caveats (non-blocking, commit-time decisions for the committer — NOT review blockers):**

- **Caveat A (untracked test file):** `aee/tests/test_installer_exit_codes.py` is currently untracked. The committer must `git add` it explicitly. Per the established staging discipline in this repo (do NOT use `git add -A` when the working tree has many unrelated untracked files — and here there are 40+ unrelated `??` entries), the committer should stage by **explicit path list**:
  ```
  git add aee/installer/__init__.py aee/installer/backend.py aee/tests/test_installer_exit_codes.py
  ```
  The 40+ other untracked files (`AEE_*.md`, `k3_*.md`, `requirements*.lock`, `scripts/`, `reports/`, etc.) are pre-existing residue from prior AEE-7.x rounds and MUST NOT be staged in this commit.

- **Caveat B (lifecycle.py already committed):** `aee/installer/lifecycle.py` (the §10.4 constants owner) is already committed at HEAD `f8fe2c9` and is NOT part of this round's diff. The committer should NOT include it in the Phase 4A commit — it was shipped in a prior round. Including it would be a no-op (no diff) but would muddy the commit's scope.

**Suggested commit subject:** `feat(aee): add Phase 4A exit-code exception hierarchy (§10.4 codes 7-12)`

**Pre-commit gate (recommended):**
```
PYTHONPATH=. python3 -m unittest aee.tests.test_installer_exit_codes -v
# expect: Ran 51 tests in <1s, OK
```

---

## 11. Artifact verification

```
$ ls -la aee/tests/test_installer_exit_codes.py aee/installer/__init__.py aee/installer/backend.py aee/installer/lifecycle.py
-rw-r--r-- 1 ubuntu ubuntu  17819 aee/tests/test_installer_exit_codes.py
-rw-r--r-- 1 ubuntu ubuntu   9309 aee/installer/__init__.py
-rw-r--r-- 1 ubuntu ubuntu  25313 aee/installer/backend.py
-rw-r--r-- 1 ubuntu ubuntu  25346 aee/installer/lifecycle.py

$ sha256sum aee/tests/test_installer_exit_codes.py aee/installer/__init__.py aee/installer/backend.py aee/installer/lifecycle.py
a001c14b77bbfe872060aa7901d043b552083daabc924789b5061cfb3666b32f  aee/tests/test_installer_exit_codes.py
93c2a9152a771ece9340e3b09dfe5a163e958f3cf639ccd54c6f959db435ab53  aee/installer/__init__.py
5b77badbbc4b03357f694827be0b55bded0b6f391935b4871b1a41efecd02a33  aee/installer/backend.py
2df6f0e73fd8620524754ee0d3b6b5f90e1bb5e6749c08ef873709757e1fc7a5  aee/installer/lifecycle.py

$ wc -l aee/tests/test_installer_exit_codes.py aee/installer/__init__.py aee/installer/backend.py aee/installer/lifecycle.py
   454 aee/tests/test_installer_exit_codes.py
   156 aee/installer/__init__.py
   707 aee/installer/backend.py
   669 aee/installer/lifecycle.py
  1986 total
```

**This review artifact:**
- Path: `/home/ubuntu/hermes-runtime-bridge/reports/aee_phase4a_exit_codes_review.md`
- Created by this review session (read-only; no source touched).

---

## 12. Mandatory Telegram attempt

Per the review brief, a Telegram notification to 鼎鼎 (`5132341473`) is mandatory. Attempted via `hermes send`:

```
hermes send --to telegram:5132341473 --subject "Phase 4A Exit Codes — Review PASS" --file reports/aee_phase4a_exit_codes_review.md --json
```

(Result of the attempt is recorded below in §13.)

### Telegram short version (per 鼎鼎's 2026-07-13 dual-channel rule — simplified for Telegram, verifiable evidence preserved):

```
✅ Phase 4A Exit Codes — Independent Review PASS
訊息類型: read-only independent review (A-I shape)
開始: 2026-07-27 23:12 CST
結束: 2026-07-27 23:12 CST
耗時: ~5 min
單號: (no TASK ID — direct review brief)
commit SHA: N/A (review-only, no commit)
test count: 51/51 targeted PASS · 191/191 neighbor PASS · 2056 full (5 pre-existing PyYAML env errors, 0 Phase 4A regressions)
摘要: 6 new exit codes (7-12) + 6 exception classes verified against §10.4; verified codes 0/2/3/4/5/6 byte-identical; zero deletions; zero unrelated modifications. Review Ready + Atomic Commit Ready (3-file set, explicit-path staging).
完整報告: /home/ubuntu/hermes-runtime-bridge/reports/aee_phase4a_exit_codes_review.md
```

---

## 13. Telegram attempt result

```
$ hermes send --to telegram:5132341473 --subject "Phase 4A Exit Codes — Review PASS" --file reports/aee_phase4a_exit_codes_review.md --json

{
  "success": true,
  "platform": "telegram",
  "chat_id": "5132341473",
  "message_id": "8889",
  "mirrored": true
}
```

**Telegram delivery: ✅ SUCCESS** — message_id `8889`, chat_id `5132341473` (鼎鼎), mirrored=true. Sent 2026-07-27 23:12 CST.

---

## 14. Cross-references

- `aee/installer/lifecycle.py` §10.4 constant block (lines 85–117) — canonical source of truth for the 6 new constants.
- `aee/installer/backend.py` Phase 4A exception block (lines 177–307) — the 6 new exception classes.
- `aee/tests/test_installer_exit_codes.py` — 51 targeted tests (7 classes).
- Pre-existing baseline errors documented in `aee-iteration-pattern` skill case studies (PyYAML env gap in `test_runtime_config`).

---

_End of review. Read-only; no source modified, no stage/commit/push/merge/rebase/stash/deploy/restart/delete performed._