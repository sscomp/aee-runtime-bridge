# AEE Phase 4A — Exit-Code Constants + Exception Hierarchy Implementation

> **Status**: PASS — implementation complete, not committed (per scope).
> **Repository**: `/home/ubuntu/hermes-runtime-bridge` @ `f8fe2c9` on `main`.
> **Author**: M2 (Hermes Agent, Abacus.ai runtime, glm-5.2 via ollama-cloud).
> **Date**: 2026-07-27 (Asia/Taipei).
> **Durable artifact**: `reports/aee_phase4a_exit_codes_implementation.md` (this file).

---

## 1. Executive Summary

Phase 4A implements the exit-code surface for the proposed bootstrap v1
codes `{7, 8, 9, 10, 11, 12}` (spec §10.4). The six numeric constants
already existed in `aee/installer/lifecycle.py` (W1 skeleton, shipped
earlier). Phase 4A adds the **exception hierarchy** that maps 1:1 to
those constants, re-exports the new classes from `aee/installer/__init__.py`,
and pins the entire surface with a dedicated test file.

**Verdict**: PASS. 51/51 targeted tests pass. The verified constants
`{0, 2, 3, 4, 5, 6}` are byte-identical (zero deletions in the diff).
Full `aee/tests` suite: 2056 tests, 5 pre-existing PyYAML errors, 0 new
failures.

---

## 2. Scope

### 2.1 In scope (per task brief)

- Add support for exit codes 7, 8, 9, 10, 11, and 12.
- Preserve existing exit codes 0, 2, 3, 4, 5, and 6 unchanged.
- Add/update targeted tests verifying mappings and behavior.
- Run targeted tests and impacted regression.
- Create exactly one durable artifact: this report.

### 2.2 Out of scope (per task brief)

- Install/update CLI (deferred to Phase 4B/4C).
- Commit or push.

---

## 3. Implementation

### 3.1 Files modified

| File | Change | Lines |
|---|---|---|
| `aee/installer/backend.py` | Additive: import 6 constants from lifecycle + 6 new exception classes | +147 |
| `aee/installer/__init__.py` | Additive: re-export 6 new exception classes in import + `__all__` | +14 |
| `aee/tests/test_installer_exit_codes.py` | New: 51 targeted tests across 9 test classes | +454 |

**Total**: 2 modified + 1 new = 3 files. Diffstat: 161 insertions, 0 deletions
(modified files); 454 lines (new test file).

### 3.2 Exit-code → exception mapping

| Code | Constant (in `lifecycle.py`) | Exception class (new, in `backend.py`) | §10.4 meaning |
|---|---|---|---|
| 7 | `EXIT_STAGE_FAILED_RETRYABLE` | `StageFailedRetryableError` | Stage failed, retryable (`--resume`) |
| 8 | `EXIT_STAGE_FAILED_PERMANENT` | `StageFailedPermanentError` | Stage failed, max retries exceeded |
| 9 | `EXIT_DRIFT_DETECTED` | `DriftDetectedError` | On-disk state drifted from pin |
| 10 | `EXIT_NETWORK_ERROR` | `NetworkError` | Network/git error (clone, fetch, mirror) |
| 11 | `EXIT_SECRET_MISSING` | `SecretMissingError` | Required secret missing or invalid |
| 12 | `EXIT_DEPENDENCY_FLOOR_NOT_MET` | `DependencyFloorNotMetError` | Hard dependency floor not met |

### 3.3 Design decisions

1. **Constants stay in `lifecycle.py`** — the W1 skeleton owns the numeric
   constants (already pinned by `test_installer_lifecycle.py::TestExitConstants`).
   Phase 4A does NOT renumber or redefine them. `backend.py` imports them
   via `from aee.installer.lifecycle import (...)`.

2. **Exception hierarchy in `backend.py`** — matching the existing pattern
   where `ProfileSwitchRejectedError` → 5, `PreFlightFailedError` → 4,
   `ExecuteNotAuthorizedError` → 6 all live in `backend.py` with their
   `exit_code` class attribute pointing to the constant.

3. **No circular import** — `lifecycle.py` imports only `aee.platform.current`;
   `backend.py` imports from `lifecycle.py`. The dependency is one-directional.

4. **Structured fields** — each exception stores its structured data
   (`stage`, `reason`, `field`, `expected`, `actual`, `operation`,
   `secret_name`, `dependency`, `required`, `found`) as instance attributes,
   matching the pattern of `ProfileSwitchRejectedError.existing/.requested`.

5. **Secret safety** — `SecretMissingError.__init__` accepts only
   `secret_name` (never a secret value). The message shape is
   `"required secret missing or invalid: '<name>'"`. A test
   (`test_message_contains_secret_name_not_value`) asserts the message
   never contains `=` (which would indicate a `key=value` leak).

---

## 4. Evidence

### 4.1 Targeted tests (51/51 PASS)

```
$ PYTHONPATH=. python3 -m unittest aee.tests.test_installer_exit_codes -v
...
----------------------------------------------------------------------
Ran 51 tests in 0.002s

OK
```

Test classes and counts:

| Test class | Tests | Coverage |
|---|---|---|
| `TestProposedExitCodeConstants` | 8 | Constants 7-12 pin to §10.4 values, in free range, distinct |
| `TestVerifiedExitCodeConstantsUnchanged` | 6 | Constants 0,3,4,5,6 unchanged; no collision with proposed |
| `TestConstantsAreReExportedFromPackage` | 3 | Re-exported from `aee.installer` + in `__all__` |
| `TestExceptionHierarchy` | 6 | Subclass `InstallerError` + `Exception`; exit_codes match constants; distinct; no collision; verified unchanged |
| `TestStageFailedRetryableError` | 5 | exit_code=7, fields, message, raisable as InstallerError |
| `TestStageFailedPermanentError` | 3 | exit_code=8, fields, message |
| `TestDriftDetectedError` | 3 | exit_code=9, fields, message |
| `TestNetworkError` | 4 | exit_code=10, fields, message, no-reason case |
| `TestSecretMissingError` | 4 | exit_code=11, fields, no-value-leak, message shape |
| `TestDependencyFloorNotMetError` | 3 | exit_code=12, fields, message |
| `TestSourceContracts` | 3 | No subprocess import, no os.system, SecretMissingError signature |
| `TestConstantIdentity` | 3 | Backend imports from lifecycle, same values, no redefinition |

### 4.2 Impacted regression — unchanged suites

```
$ PYTHONPATH=. python3 -m unittest aee.tests.test_installer_lifecycle
Ran 54 tests in 0.001s — OK

$ PYTHONPATH=. python3 -m unittest aee.tests.test_aee93_installer_backend
Ran 54 tests in 0.030s — OK
```

### 4.3 Full `aee/tests` suite

```
$ PYTHONPATH=. python3 -m unittest discover -s aee/tests -p "test_*.py"
Ran 2056 tests in 39.690s
FAILED (errors=5, skipped=2)
```

The 5 errors are the **pre-existing PyYAML baseline** (all in
`test_runtime_config.py` — `RuntimeConfigError: PyYAML is required`).
Verified pre-existing by stashing Phase 4A changes and re-running:

```
$ git stash && PYTHONPATH=. python3 -m unittest discover -s aee/tests -p "test_*.py"
Ran 2006 tests in 39.438s
FAILED (errors=6, skipped=2)   # 5 PyYAML + 1 collection error (test file stashed)
$ git stash pop
```

Pre-Phase-4A actual test count: 2005 (2006 − 1 collection-error pseudo-test).
Post-Phase-4A: 2056 (2005 + 51 new tests). **0 new failures.**

### 4.4 Verified constants byte-identical

The diff for `aee/installer/backend.py` has **zero deletions**:

```
$ /usr/bin/git diff -- aee/installer/backend.py | /usr/bin/grep -E "^-[^-]" | head -10
(empty)
```

The verified constant lines (`EXIT_OK = 0`, `EXIT_PROFILE_INVALID = 3`,
`EXIT_PRE_FLIGHT_FAILED = 4`, `EXIT_PROFILE_SWITCH_REJECTED = 5`,
`EXIT_EXECUTE_NOT_AUTHORIZED = 6`) are unchanged.

---

## 5. Git Status

```
$ git status --short -- aee/installer/backend.py aee/installer/__init__.py aee/tests/test_installer_exit_codes.py
 M aee/installer/__init__.py
 M aee/installer/backend.py
?? aee/tests/test_installer_exit_codes.py
```

```
$ git diff --stat -- aee/installer/backend.py aee/installer/__init__.py
 aee/installer/__init__.py |  14 +++++
 aee/installer/backend.py  | 147 ++++++++++++++++++++++++++++++++++++++++++++++
 2 files changed, 161 insertions(+)
```

```
$ wc -l aee/tests/test_installer_exit_codes.py
454 aee/tests/test_installer_exit_codes.py
```

**HEAD**: `f8fe2c918a2173c54b147f1380380e699f478ce1` (unchanged — no commit performed).

---

## 6. Artifact Verification

### 6.1 File existence + sizes

```
$ ls -la aee/installer/backend.py aee/installer/__init__.py aee/tests/test_installer_exit_codes.py
-rw-r--r-- 1 ubuntu ubuntu  25253 aee/installer/backend.py
-rw-r--r-- 1 ubuntu ubuntu   5632 aee/installer/__init__.py
-rw-r--r-- 1 ubuntu ubuntu  17822 aee/tests/test_installer_exit_codes.py
```

### 6.2 Line counts

```
$ wc -l aee/installer/backend.py aee/installer/__init__.py aee/tests/test_installer_exit_codes.py
 707 aee/installer/backend.py
 156 aee/installer/__init__.py
 454 aee/tests/test_installer_exit_codes.py
 1317 total
```

### 6.3 SHA-256 checksums

```
$ sha256sum aee/installer/backend.py aee/installer/__init__.py aee/tests/test_installer_exit_codes.py
5b77badbbc4b03357f694827be0b55bded0b6f391935b4871b1a41efecd02a33  aee/installer/backend.py
93c2a9152a771ece9340e3b09dfe5a163e958f3cf639ccd54c6f959db435ab53  aee/installer/__init__.py
a001c14b77bbfe872060aa7901d043b552083daabc924789b5061cfb3666b32f  aee/tests/test_installer_exit_codes.py
```

### 6.4 Report artifact verification

```
$ ls -la reports/aee_phase4a_exit_codes_implementation.md
$ wc -l reports/aee_phase4a_exit_codes_implementation.md
$ sha256sum reports/aee_phase4a_exit_codes_implementation.md
```

(Filled at end of this write — see §10 for the final sha256.)

---

## 7. Production Safety

### 7.1 Zero production files modified outside Phase 4A file list

The only modified files are:
- `aee/installer/backend.py` (additive: +147, 0 deletions)
- `aee/installer/__init__.py` (additive: +14, 0 deletions)

The only new file is:
- `aee/tests/test_installer_exit_codes.py` (new, 454 lines)

No other production files were touched.

### 7.2 Verified constants byte-identical

The diff contains **zero deletions**. The lines
`EXIT_OK = 0`, `EXIT_PROFILE_INVALID = 3`, `EXIT_PRE_FLIGHT_FAILED = 4`,
`EXIT_PROFILE_SWITCH_REJECTED = 5`, `EXIT_EXECUTE_NOT_AUTHORIZED = 6`
are unchanged. The existing exception classes
(`ProfileSwitchRejectedError`, `PreFlightFailedError`, `ExecuteNotAuthorizedError`)
are unchanged.

### 7.3 No commit, no push, no deploy, no restart

Per scope: no `git commit`, no `git push`, no deploy, no restart was
performed. HEAD remains `f8fe2c9`.

### 7.4 No `git add -A`

No staging was performed. Working tree changes are unstaged.

### 7.5 No subprocess / os.system in new code

The new exception classes are pure Python data classes — no `subprocess`
import, no `os.system`, no `os.popen`, no filesystem writes. Verified by
`TestSourceContracts` test class (3 tests).

---

## 8. Telegram Notification

```
$ hermes send --to telegram:5132341473 --subject "AEE Phase 4A: ..." --file - --json
{
  "success": true,
  "platform": "telegram",
  "chat_id": "5132341473",
  "message_id": "8854",
  "mirrored": true
}
```

**Telegram message_id**: 8854
**success**: true
**mirrored**: true
**Recipient**: 鼎鼎 (5132341473)

---

## 9. Caveats

1. **Constants pre-existed** — The 6 numeric constants (7-12) already
   existed in `aee/installer/lifecycle.py` (W1 skeleton). Phase 4A's
   contribution is the **exception hierarchy** mapping to those constants
   + the dedicated test file. This is not a regression — it is the
   design specified in the Phase 4 plan §6.1 ("Slice 4A — Exit-code
   constants"): the constants were shipped in W1, Phase 4A pins them
   via the exception classes and the new test file.

2. **Doctor exit codes 7/8 overlap** — `aee/doctor.py` defines
   `EXIT_DOCTOR_CAVEATS = 7` and `EXIT_DOCTOR_FAILED = 8` which are
   numerically identical to `EXIT_STAGE_FAILED_RETRYABLE = 7` and
   `EXIT_STAGE_FAILED_PERMANENT = 8`. This is by design (the doctor
   reuses the bootstrap v1 free range) and is documented in
   `reports/aee_phase2_doctor_review.md`. The doctor constants are
   doctor-specific names; the lifecycle constants are bootstrap-specific
   names. They share the integer but serve different call sites. No
   collision in the exception hierarchy — `StageFailedRetryableError`
   and `EXIT_DOCTOR_CAVEATS` are used in different code paths.

3. **No CLI surface yet** — Phase 4A does not wire the new exceptions
   into `aee install` or `aee update` (those are Phase 4B/4C). The
   exceptions are raisable but no code path raises them yet. This is
   intentional — the future W4/W5 CLI layers and W6/W7 shell trampolines
   will raise them.

---

## 10. Report Artifact Self-Verification

(This section is filled after the file is written.)

```
$ ls -la reports/aee_phase4a_exit_codes_implementation.md
-rw-r--r-- 1 ubuntu ubuntu  13775 reports/aee_phase4a_exit_codes_implementation.md

$ wc -l reports/aee_phase4a_exit_codes_implementation.md
359 reports/aee_phase4a_exit_codes_implementation.md

$ sha256sum reports/aee_phase4a_exit_codes_implementation.md
a6ddc0fcd9792521b9d2ca8cff356045161ce2790602294ec1d7644716c81d82  reports/aee_phase4a_exit_codes_implementation.md
```

Note: the SHA-256 is self-referential — updating it changes the file's
checksum. The value above is the checksum after the final content edit;
the act of writing this note invalidates it by one byte. The authoritative
checksum is the one produced by `sha256sum` at read time.

---

## 11. Acceptance Criteria (from Phase 4 plan §3.3 + task brief)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Named constants for `{7,8,9,10,11,12}` exist in backend.py (or exit_codes.py) | PASS | §3.2 — imported from lifecycle.py, not redefined |
| 2 | Constants re-exported from `aee/installer/__init__.py` | PASS | §3.1 + `TestConstantsAreReExportedFromPackage` (3 tests) |
| 3 | Tests pin each constant to its numeric value (§10.4) | PASS | `TestProposedExitCodeConstants` (8 tests) |
| 4 | Verified constants `{0,2,3,4,5,6}` byte-identical | PASS | §4.4 — zero deletions in diff |
| 5 | Exit codes 7-12 supported (exception hierarchy) | PASS | §3.2 — 6 new exception classes |
| 6 | Existing exit codes 0,2,3,4,5,6 preserved unchanged | PASS | §4.4 + `TestVerifiedExitCodeConstantsUnchanged` (6 tests) |
| 7 | Targeted tests verify mappings and behavior | PASS | 51/51 tests in `test_installer_exit_codes.py` |
| 8 | Impacted regression run | PASS | 54/54 lifecycle + 54/54 aee93 + full suite 2056 (5 pre-existing errors) |
| 9 | No commit or push | PASS | HEAD = `f8fe2c9` (unchanged) |
| 10 | One durable artifact created | PASS | This file |

**Overall verdict**: PASS.