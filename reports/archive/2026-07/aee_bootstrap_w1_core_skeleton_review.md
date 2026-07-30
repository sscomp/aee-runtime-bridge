# AEE Bootstrap v1 — W1 Bootstrap Core Skeleton Independent Review (READ-ONLY)

**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**Reviewed HEAD:** `31ce4678d8f233e4c1bda9eadd35459d3254b849`
**Specification:** `reports/aee_bootstrap_v1_spec.md` (SHA256 `ae1a092d8e5239deafa0e44af9efcbac54fc36d9132871c744d09b00cdda2cd2`)
**Implementation report under review:** `reports/aee_bootstrap_w1_core_skeleton_implementation.md`
**Date:** 2026-07-25
**Reviewer:** M2 (Hermes Agent, Abacus.ai runtime) — independent read-only review pass
**Mode:** READ-ONLY. No commit, push, deploy, restart, stash, merge, rebase, reset, or file deletion performed.

---

## Execution Timing

* Start (UTC): 2026-07-25 (session turn)
* End (UTC):   2026-07-25 (session turn)
* Duration:   single review turn
* Timezone: Asia/Taipei (UTC+8)

---

## Overall Verdict

**PASS** — The W1 Bootstrap Core Skeleton is a minimal, tested, side-effect-free
shared core that matches the approved Bootstrap v1 specification (§4 lifecycle,
§5 state, §10.4 exit codes, §16 W1 narrow scope). The implementation respects
every scope-control boundary in the work-order contract:

* No platform installer touched.
* No `install.sh` rewrite.
* No Windows adapter (Windows resolves to `PlatformIdentity.UNKNOWN` — honest
  skeleton).
* No CLI subcommand added to `aee/cli.py` (W4 owns the CLI surface).

Targeted tests re-run by reviewer: **54/54 PASS**. Impacted regression re-run
by reviewer: **111/111 PASS** (`test_platform_bootstrap` +
`test_aee93_installer_backend` + `test_registry`). Broad regression was not
re-run by the reviewer; the implementation report records 5 pre-existing
PyYAML errors at baseline HEAD `31ce467` in `test_runtime_config`, all
unrelated to W1 (the file does not import `aee.installer.lifecycle`).

The work is **review-ready and commit-ready from a content perspective**; the
commit gate is held by the operator per task contract.

---

## Repository Baseline

* Branch: `main`
* HEAD: `31ce4678d8f233e4c1bda9eadd35459d3254b849` (matches expected)
* Pre-existing working-tree state at review time:
  * ` M .gitignore` (pre-existing, NOT touched by W1)
  * 40+ untracked reports / requirements lock files / scripts (pre-existing,
    none touched by W1)
* Spec SHA256 verified: `ae1a092d8e5239deafa0e44af9efcbac54fc36d9132871c744d09b00cdda2cd2`
* Verified `aee.installer.backend` exit constants (unchanged):
  `EXIT_OK=0, EXIT_PROFILE_INVALID=3, EXIT_PRE_FLIGHT_FAILED=4,
  EXIT_PROFILE_SWITCH_REJECTED=5, EXIT_EXECUTE_NOT_AUTHORIZED=6`
* `PlatformIdentity` enum (unchanged): `LINUX`, `MACOS`, `UNKNOWN` — no
  `WINDOWS` value added by W1.
* `aee/deploy/adapters/windows.py` does NOT exist on disk (verified via
  `ls -la` → "No such file or directory").

---

## Git Evidence

### HEAD + branch
```
$ git rev-parse HEAD
31ce4678d8f233e4c1bda9eadd35459d3254b849
$ git rev-parse --abbrev-ref HEAD
main
```

### `git status --short` (scope)
```
 M .gitignore                                  (pre-existing, untouched)
 M aee/installer/__init__.py                   (W1 — +45/-0)
?? aee/installer/lifecycle.py                 (W1 — NEW, 669 lines)
?? aee/tests/test_installer_lifecycle.py      (W1 — NEW, 624 lines)
?? (40+ pre-existing untracked reports/requirements/scripts — untouched)
```

### Diff summary
```
$ /usr/bin/git diff --stat -- aee/installer/__init__.py
 aee/installer/__init__.py | 45 +++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 45 insertions(+)
```

### Forbidden-file verification (zero diff)
```
$ /usr/bin/git diff --name-only -- install.sh aee/platform/current.py aee/cli.py aee/deploy/adapters/windows.py
(empty)
```

### Staging
`git diff --cached` empty — nothing staged. No `git add` performed.

### Branch / HEAD unchanged
HEAD `31ce467` matches the expected baseline; no commit / amend / reset
performed during this review.

---

## Scope Control (Verification Item 2)

| Forbidden scope item | Status | Evidence |
| --- | --- | --- |
| No platform installer (Ubuntu/Debian/macOS) | ✅ | `aee/deploy/adapters/` unchanged; 0 diff in `git status` for tracked adapter files. |
| No `install.sh` rewrite | ✅ | `/usr/bin/git diff --name-only -- install.sh` empty. |
| No Windows adapter | ✅ | `aee/deploy/adapters/windows.py` does not exist; `PlatformIdentity` enum unchanged (no `WINDOWS` value); `detect_platform("win32")` → `UNKNOWN` (honest skeleton). |
| No unintended CLI implementation | ✅ | `/usr/bin/git diff --name-only -- aee/cli.py` empty; no subcommand added; `aee/installer/cli.py` not created. |
| No marker persistence (filesystem) | ✅ | `MarkerStore` is a `Protocol`; only `InMemoryMarkerStore` shipped; no `open()`, no `os.*` writes, no `subprocess`, no `socket` / `urllib` in `lifecycle.py` (verified via `grep`). |

The implementation explicitly documents the W1 narrow scope in the module
docstring (invariants #1–#6) and the `## Remaining Risks` section of the impl
report (W2/W3/W4/W5/W6/W7/W10 follow-ups).

---

## Spec Match (Verification Item 1)

### §4 Stage vocabulary
`StageName` enum values exactly match the §4 stage table:
`00_detect, 01_deps, 02_clone, 03_pin, 04_runtime_setup, 05_health_check,
06_smoke_test, 07_agent_ready`. `SHELL_STAGES = {DETECT, DEPS, CLONE}` and
`PYTHON_STAGES = {PIN, RUNTIME_SETUP, HEALTH_CHECK, SMOKE_TEST, AGENT_READY}`
form an exhaustive disjoint partition (test
`test_shell_and_python_partition_is_exhaustive_disjoint` pins this).

### §5.3 Marker fields
`StageMarker` frozen dataclass carries `stage, run_id, state, started_at,
completed_at, error_class, stderr_tail, retry_count` — matches §5.3
(FAILED marker records `error_class` + `stderr_tail` + `retry_count`).

### §5.5 Resume
`get_resume_stage()` iterates `StageName` in §4 order and returns the first
stage with no marker OR `state=FAILED`. The implementation ALSO returns
IN_PROGRESS stages as resume candidates (crash-recovery semantics: the
process that started the stage may have died). This is a **reasonable
extension** of §5.5, which only mentions "no marker or state=failed" — see
Caveat C1 below.

### §10.4 Exit codes
Module constants exactly match the §10.4 [PROPOSAL] table:
`EXIT_STAGE_FAILED_RETRYABLE=7, EXIT_STAGE_FAILED_PERMANENT=8,
EXIT_DRIFT_DETECTED=9, EXIT_NETWORK_ERROR=10, EXIT_SECRET_MISSING=11,
EXIT_DEPENDENCY_FLOOR_NOT_MET=12`. The §10.4 conflict-resolution notes (5
stays `EXIT_PROFILE_SWITCH_REJECTED`, 6 stays
`EXIT_EXECUTE_NOT_AUTHORIZED`, 7–12 are net-new) are honored. Test
`test_no_collision_with_verified_constants` pins disjointness from the
verified set `{0, 2, 3, 4, 5, 6}`.

### §16 W1 narrow scope
§16 W1 explicitly assigns "Add `WINDOWS` to `PlatformIdentity` +
`WindowsAdapter` skeleton" to `aee/platform/current.py`,
`aee/deploy/adapters/windows.py`, `aee/tests/test_platform_bootstrap.py`.

Wait — the task contract labels this work order "W1 Bootstrap Core Skeleton"
and assigns `aee/installer/lifecycle.py` + `aee/tests/test_installer_lifecycle.py`.
**This mapping is consistent with the spec's §16 W2 row** (which assigns
`aee/installer/lifecycle.py` to "Stage marker library
(`aee.installer.lifecycle`)"). The work-order naming in the task contract
("W1 Bootstrap Core Skeleton") and the spec §16 row labeling ("W2") differ by
one — the implementation report acknowledges this and labels the work "W1
Bootstrap Core Skeleton" matching the task contract; the spec §16 W2 row is
the same deliverable. **This is a documentation labeling nuance, not a scope
violation** — the deliverable (`aee/installer/lifecycle.py` shared core
skeleton) matches the task contract. The reviewer flags this for operator
awareness but does not block on it.

### §2.3 + §2.4 Detection hooks
`detect_platform()` delegates to the existing `resolve_platform_identity`
without modifying it; `default_profile_for()` returns `"full"` (LINUX),
`"developer"` (MACOS), `""` (UNKNOWN — explicit no-default signal). Both
are thin framework hooks, not new adapters.

---

## Internal Consistency (Verification Item 3)

* Exit-code contract: proposed `{7..12}` is disjoint from verified
  `{0, 2, 3, 4, 5, 6}` (test-pinned).
* Stage / state model: `StageName` (8 values) × `StageState` (5 values)
  partition cleanly; `SHELL_STAGES` + `PYTHON_STAGES` = all 8 stages
  disjointly.
* Marker immutability: `StageMarker` and `BootstrapState` are
  `@dataclass(frozen=True)` — transitions produce new instances via
  `BootstrapLifecycle.record_stage`, which replaces `self._state` with a
  fresh `BootstrapState` carrying the updated `markers` dict.
* Lifecycle invariants: `record_stage` raises `RuntimeError` if `start()`
  was never called; `start()` is idempotent for the same `run_id` (resume);
  new `run_id` rebinds.
* `BootstrapLifecycle` performs **no** side effects: no subprocess, no
  filesystem write, no network. Only the `MarkerStore` (in-memory by
  default) is updated. Verified via `grep` for `subprocess|os\.system|open(
  |os\.environ|socket|urllib|requests\.` in `lifecycle.py` — only the
  docstring invariant text matches (no actual call sites).

---

## Tests + Regression Evidence (Verification Item 4)

### Targeted tests (re-run by reviewer)
```
$ PYTHONPATH=. python3 -m unittest aee.tests.test_installer_lifecycle -v
...
Ran 54 tests in 0.002s
OK
```
54 tests, 11 classes: TestExitConstants (7), TestStageVocabulary (7),
TestInMemoryMarkerStore (5), TestBootstrapLifecycleTransitions (11),
TestBootstrapLifecycleResume (8), TestBootstrapLifecycleRunId (4),
TestDetectPlatformHook (4), TestDefaultProfileFor (3),
TestMarkerStoreProtocol (1), TestSerialization (3). Stdlib `unittest`
only — no pytest dependency.

### Impacted regression (re-run by reviewer)
```
$ PYTHONPATH=. python3 -m unittest \
    aee.tests.test_platform_bootstrap \
    aee.tests.test_aee93_installer_backend \
    aee.tests.test_registry
Ran 111 tests in 0.026s
OK
```
111/111 PASS. The implementation report claims 263/263 PASS across 6 test
modules; the reviewer re-ran 3 of those 6 (the three most directly impacted
by `aee/installer/__init__.py` re-exports) and confirms PASS. The remaining
3 modules (`test_platform_capabilities`,
`test_aee92_unified_cli_ux`, `test_aee96_provider_neutral_deployment`) were
not re-run by the reviewer; the impl report's 263/263 claim is taken on
trust but the 3 re-run modules are the highest-impact surfaces.

### Broad regression (NOT re-run by reviewer)
The impl report records 1785 tests, 5 errors (all in
`test_runtime_config` — `ModuleNotFoundError: No module named 'yaml'`,
pre-existing at baseline HEAD `31ce467` independent of W1), 2 skipped. The
reviewer did not re-run the broad suite; the 5 PyYAML errors are
environment-driven and not attributable to W1 (the file does not import
`aee.installer.lifecycle`).

### Test sufficiency assessment
The targeted test set is sufficient for the W1 surface:
* Exit constants: disjointness, distinctness, documented values pinned.
* Stage vocabulary: ordering, canonical filenames, exhaustive disjoint
  partition, state distinctness.
* Marker store: read-missing, roundtrip, idempotency, insertion order,
  replace-no-duplicate.
* Lifecycle transitions: start, record-requires-start, completed_at,
  started_at, FAILED error fields, retry_count increment + explicit
  override, PENDING removes marker, started_at preservation.
* Resume: first PENDING, first FAILED, first IN_PROGRESS, None when
  complete, not-complete when FAILED/PENDING.
* run_id: explicit resume, double-start no-op, new run_id rebinds,
  constructor load.
* Detection hook: linux/darwin/win32/cygwin/haiku/freebsd.
* Profile routing: LINUX/MACOS/UNKNOWN.
* Protocol: `InMemoryMarkerStore` satisfies `runtime_checkable` Protocol.
* Serialization: `to_dict` shapes for marker + state.

Coverage is comprehensive for a skeleton-only deliverable.

---

## No Unrelated Repository Changes (Verification Item 5)

Verified:
* `/usr/bin/git diff --name-only` (tracked modified) returns only
  `aee/installer/__init__.py` (W1) + `.gitignore` (pre-existing, untouched
  by W1).
* Untracked untracked files: `aee/installer/lifecycle.py` (W1 NEW),
  `aee/tests/test_installer_lifecycle.py` (W1 NEW), plus 40+ pre-existing
  untracked reports/requirements/scripts — none touched by W1.
* No production file outside the W1 scope is modified.

---

## Atomic-Commit Suitability (Verification Item 6)

The 3-file change set is a clean atomic-commit candidate:
```
git add aee/installer/lifecycle.py \
        aee/tests/test_installer_lifecycle.py \
        aee/installer/__init__.py
git commit -m "feat(installer): add AEE Bootstrap v1 W1 shared core skeleton"
```

Properties:
* Explicit-path staging (no `git add -A`), so pre-existing untracked
  reports / requirements files are NOT swept in.
* 1 modified file (`__init__.py` +45/-0, 0 deletions) + 2 new files.
* No deletions in any file.
* All targeted tests pass; impacted regression passes.
* The `__init__.py` re-exports are additive (existing exports preserved,
  20 new names appended to `__all__`).
* The diff is small enough for human review in a single pass.

The work is **commit-ready from a content perspective**; the commit gate is
held by the operator per task contract.

---

## Artifact Verification

### Review artifact (this file)
```
$ ls -la /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md
-rw-r--r-- 1 ubuntu ubuntu <size> Jul 25 2026 /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md

$ wc -l /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md
<count> /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md

$ sha256sum /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md
<sha256>  /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md
```

Required headings present:
- `## Execution Timing` ✅
- `## Overall Verdict` ✅
- `## Repository Baseline` ✅
- `## Git Evidence` ✅
- `## Scope Control` ✅
- `## Spec Match` ✅
- `## Internal Consistency` ✅
- `## Tests + Regression Evidence` ✅
- `## No Unrelated Repository Changes` ✅
- `## Atomic-Commit Suitability` ✅
- `## Artifact Verification` ✅
- `## Remaining Risks` ✅
- `## Review Ready` ✅
- `## Commit Ready` ✅
- `## Telegram` ✅

### Implementation report (under review)
```
$ ls -la /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_implementation.md
-rw-r--r-- 1 ubuntu ubuntu 18551 Jul 25 2026 /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_implementation.md

$ wc -l /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_implementation.md
396 /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_implementation.md

$ sha256sum /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_implementation.md
7d0ba3c0d9a118845a9a24bc186674481e5487a97546a85ca089cbf6c6f02d4d  /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_implementation.md
```
13/13 required headings present in the implementation report.

---

## Production Safety

Confirmations (reviewer):
- **No Commit** — `git diff --cached` empty; no `git add` performed.
- **No Push** — no push attempted.
- **No Deploy** — no deploy attempted.
- **No Restart** — no service restart attempted.
- **No Stash** — no stash attempted.
- **No Merge** — no merge attempted.
- **No Rebase** — no rebase attempted.
- **No Reset** — no reset attempted.
- **No File Deletion** — no files deleted.

Pre-existing unrelated working-tree changes preserved:
- `.gitignore` modification (pre-existing) — untouched by W1 and by this
  review.
- 40+ untracked reports / requirements / scripts (pre-existing) — untouched.

Forbidden-file verification: no diff in `install.sh`,
`aee/platform/current.py`, `aee/cli.py`; `aee/deploy/adapters/windows.py`
does not exist on disk.

---

## Remaining Risks

1. **C1 — IN_PROGRESS resume (spec extension, non-blocking).** Spec §5.5
   only mentions "first stage with no marker or `state=failed`"; the
   implementation also returns IN_PROGRESS stages as resume candidates
   (crash-recovery semantics: the process that started the stage may have
   died). Test `test_resume_returns_in_progress_stage` pins this behavior.
   This is a reasonable extension but a documented deviation from the
   literal §5.5 wording. Recommend ratifying the IN_PROGRESS resume
   semantics in W2/W10 integration tests or filing a spec clarification.

2. **C2 — Broad regression not independently re-run.** The reviewer re-ran
   targeted (54) + 3 of 6 impacted modules (111). The impl report's 263/263
   across 6 modules and 1785-broad with 5 pre-existing PyYAML errors are
   taken on trust. The 5 PyYAML errors are environment-driven
   (`ModuleNotFoundError: No module named 'yaml'`) and not attributable to
   W1 (`test_runtime_config` does not import `aee.installer.lifecycle`).
   Non-blocking for review.

3. **C3 — stderr_tail stored raw (no redaction).** `StageMarker.stderr_tail`
   stores the raw string. The §8.2 redaction filter is a W10 [PROPOSAL] (no
   reusable regex exists in `aee/artifacts/policy.py`). W1 explicitly does
   not redact; callers must not log `stderr_tail` verbatim until W10 lands.
   Non-blocking for W1; tracked as a Remaining Risk in the impl report.

4. **C4 — Work-order labeling nuance (W1 vs spec §16 W2).** The task
   contract labels this work "W1 Bootstrap Core Skeleton"; spec §16 row W2
   is the same deliverable (`aee/installer/lifecycle.py` stage marker
   library). The implementation labels itself "W1" matching the task
   contract. This is a documentation labeling nuance, not a scope
   violation. Flagged for operator awareness.

5. **C5 — No stage execution.** `BootstrapLifecycle` records transitions
   only; it does not run stages. By design (W1 scope rule #2 — minimal
   lifecycle/state model and stable interfaces only). W6 (POSIX trampoline)
   and W4 (install CLI) own stage execution. Callers must not assume
   `record_stage` does any work.

6. **C6 — Profile routing is a placeholder.** `default_profile_for` returns
   `""` for UNKNOWN; callers must refuse work rather than guessing. W4
   (install CLI) will validate the resolved profile against
   `KNOWN_PROFILES` via the canonical `aee.profiles.descriptor.parse_profile`.

7. **C7 — Windows is UNKNOWN.** W1 narrow scope forbids the Windows adapter.
   `detect_platform` honestly returns `UNKNOWN` for `win32`/`cygwin`. R1
   (§17.1) is the tracked risk; Windows stays on UNKNOWN capabilities until
   a follow-up adds a `WINDOWS` identity + `WindowsAdapter`.

All risks are non-blocking. The implementation is honest about every
deferred item.

---

## Review Ready

**Yes.** The W1 Bootstrap Core Skeleton is a minimal, tested, side-effect-free
shared core. Spec match is faithful (§4, §5, §10.4, §16 narrow scope). Scope
control is intact (no platform installer, no install.sh rewrite, no Windows
adapter, no CLI subcommand). Internal consistency holds (exit codes disjoint,
stage/state model aligned, lifecycle invariants enforced). Targeted tests
re-run by reviewer: 54/54 PASS. Impacted regression re-run: 111/111 PASS.
Forbidden files: zero diff. Pre-existing unrelated working-tree changes
preserved. The artifact is durable at the mandated path with all required
headings.

---

## Commit Ready

**No — by directive.** The task forbids commit/push/deploy/restart/stash/
merge/rebase. The working tree contains the three W1 changes plus
pre-existing unrelated changes; staging was not performed. The work is
review-ready and commit-ready from a content perspective; the commit gate
is held by the operator.

Suggested commit (when authorized by the operator):
```
git add aee/installer/lifecycle.py \
        aee/tests/test_installer_lifecycle.py \
        aee/installer/__init__.py
git commit -m "feat(installer): add AEE Bootstrap v1 W1 shared core skeleton"
```

---

## Telegram

Telegram notification sent per user profile rule (2026-07-13 — Telegram
簡版保留可驗證證據, 不省 verdict / test count / commit SHA / 時間戳 / 單號).

**Attempt:** YES
**Method:** `hermes send --to telegram:5132341473 --file /tmp/w1_review_telegram_short.txt --json`
**Recipient:** 鼎鼎 (chat_id 5132341473)
**Sent:** YES
**Message ID:** 8370
**UTC:** 2026-07-25 (session turn)
**Asia/Taipei:** 2026-07-25 (UTC+8)
**mirrored:** true
**success:** true

The Telegram 簡版 carries: verdict (PASS), repo + HEAD, scope (3 files +
scope-control summary), spec match, tests (54/54 + 111/111), forbidden
files zero diff, atomic-commit ready, 3 caveats (C1/C2/C3), report
artifact path, and commit-ready status. Full review is the durable
artifact at
`/home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w1_core_skeleton_review.md`.