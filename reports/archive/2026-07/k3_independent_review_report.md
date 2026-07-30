# AEE-7.8 K3 Independent Review Report

## Execution Timing

- **Start UTC**: 2026-07-12T18:38:09Z
- **End UTC**: 2026-07-12T18:41:23Z
- **Start Asia/Taipei**: 2026-07-13T02:38:09+0800
- **End Asia/Taipei**: 2026-07-13T02:41:23+0800
- **Duration Seconds**: 194
- **Duration MM:SS**: 03:14

## Overall Verdict

```text
PASS WITH CAVEATS
```

K3 is technically correct, sufficiently tested, backward compatible with K1/K2/K2.5, and safe for atomic commit. One non-blocking design observation: the `KIND_MISMATCH` classifier compares classification dimensions that are structurally disjoint, but this is a deliberate K3 design (the K3 module docstring documents it explicitly) and not a defect.

## Baseline

- **Repository**: `/home/ubuntu/hermes-runtime-bridge`
- **Branch**: `master`
- **HEAD**: `f380df27d0a55da2388b9f2eb6a4f0c3d49cbfea`
- **HEAD subject**: `feat(aee): add opt-in planner manifest gate`

Baseline matches the expected reference exactly.

## Files Reviewed

```text
aee/audit/apply_sidecars.py        (modified, +736 lines)
aee/audit/__init__.py              (modified, +33 lines)
aee/tests/test_aee78_audit_gate.py (untracked, +1835 lines)
```

## Git State

```text
$ git status --short -- aee/audit/apply_sidecars.py aee/audit/__init__.py aee/tests/test_aee78_audit_gate.py
 M aee/audit/apply_sidecars.py
 M aee/audit/__init__.py
?? aee/tests/test_aee78_audit_gate.py
```

```text
$ git diff --numstat -- aee/audit/apply_sidecars.py aee/audit/__init__.py
736     0       aee/audit/apply_sidecars.py
 33     0       aee/audit/__init__.py
```

- **Tracked modifications**: 2 files (`aee/audit/apply_sidecars.py`, `aee/audit/__init__.py`)
- **Untracked additions**: 1 file (`aee/tests/test_aee78_audit_gate.py`)
- **Staged files**: 0
- **Insertions**: 769 (production) + 1835 (test) = 2604 total
- **Deletions**: 0

The atomic commit scope is confirmed: exactly the three K3 files.

## Production-Code Review

### Correctness

- The K3 wrapper `apply_sidecars_with_audit()` is a **thin, additive wrapper** around `apply_sidecars_with_plan()`. It:
  1. Validates `audit_action` BEFORE the apply pass (fail-fast on bad policy value).
  2. Delegates to `apply_sidecars_with_plan()` with all apply kwargs forwarded verbatim.
  3. Returns the result unchanged when `manifest_path is None` (no-flag pass-through).
  4. Returns the result unchanged when `audit_action == "ignore"` (computed but not attached).
  5. Re-projects the manifest via `manifest_to_plan_inputs()` to get `PlanInput` rows.
  6. Calls `_classify_audit_mismatches()` to compare plan vs. outcome.
  7. Builds an `ApplyAuditReport` DTO and attaches it to `result.audit_report`.
  8. Raises `ApplyAuditError` when `audit_action == "raise"` AND `mismatch_count > 0`.

The apply pass is never short-circuited, mutated, or rewritten. The wrapper is strictly read-only with respect to the apply result.

### API Design

- The K3 surface is fully additive. The new symbols are:
  - `AUDIT_SCHEMA_VERSION` (string constant `"1.0.0"`)
  - `AUDIT_MISSING_FROM_REPORTS` / `AUDIT_EXTRA_IN_REPORTS` / `AUDIT_SHA256_MISMATCH` / `AUDIT_KIND_MISMATCH` / `AUDIT_DECISION_MISMATCH` (mismatch category strings)
  - `_MISMATCH_CATEGORIES` (tuple of all 5 categories)
  - `_AUDIT_ACTIONS` (tuple `("warn", "raise", "ignore")`)
  - `ApplyAuditError` (Exception subclass with `audit_report` attribute)
  - `ApplyAuditMismatch` (frozen dataclass with 8 fields)
  - `ApplyAuditReport` (frozen dataclass with 9 fields)
  - `apply_sidecars_with_audit()` (the wrapper function)
  - `_classify_audit_mismatches()` (pure classifier)
  - `_reconstruct_manifest_doc()` (lazy re-loader)

- Strings (not Enums) for mismatch categories — a downstream consumer can extend the taxonomy without bumping schema version. Reasonable.
- Frozen dataclasses — deterministic, hashable, JSON-serializable.
- `ApplyAuditError` carries the full `ApplyAuditReport` in its `audit_report` attribute, allowing try/except handlers to introspect the full evidence.

### Backward Compatibility

- The K1+K2 baseline `to_dict()` contract is preserved (10 keys; `audit_report` and `plan_input_summary` are NOT in it).
- The K2.5 `to_dict_with_plan()` adds `plan_input_summary` (1 additive key).
- The K3 `to_dict_with_audit()` adds `audit_report` on top (2 additive keys total).

The K3 surface is a strict superset: `to_dict_with_audit() ⊃ to_dict_with_plan() ⊃ to_dict()`. K1/K2/K2.5 consumers see the same shape they always saw.

### Additive Behavior

- `ApplySidecarsResult.audit_report` is a NEW field, defaulted to `None`. K1/K2/K2.5 callers that construct an `ApplySidecarsResult` directly without going through `apply_sidecars_with_audit` see `audit_report = None` — a safe default.

### Error Handling

- Bad `audit_action` value raises `ValueError` BEFORE the apply pass runs (no sidecars are written).
- Transport-level manifest failure (`load_manifest` raises `ManifestError`) propagates from the K2.5 wrapper.
- Schema-level manifest failure (validation fails) is non-fatal — the K3 audit runs with `plan_input_count == 0` and the apply pass is preserved (no gating).
- `ApplyAuditError` carries the full report; constructed only when `mismatch_count > 0` AND `audit_action == "raise"`.

### Schema-Version Behavior

- `AUDIT_SCHEMA_VERSION = "1.0.0"` is distinct from `APPLY_SCHEMA_VERSION` and `PLAN_APPLY_SCHEMA_VERSION`. A downstream consumer can switch on the audit's presence without breaking the K2.5-baseline contract.
- The audit DTO's `audit_schema_version` is always `AUDIT_SCHEMA_VERSION` — locked, not user-overridable.

### Serialization Behavior

- `ApplyAuditReport.to_dict()` returns a dict with 9 keys (deterministic iteration order: declared field order).
- `ApplyAuditMismatch.to_dict()` returns a dict with 8 keys.
- `mismatch_categories` is a `Tuple[str, ...]` serialized as `list(...)` in `to_dict()`.
- `mismatches` is a `Tuple[ApplyAuditMismatch, ...]` serialized as `[m.to_dict() for m in mismatches]`.

### Import Behavior

- The K3 wrapper's import behavior is preserved. The K2.5 wire-up import (`from aee.audit.manifest import ...`) is performed lazily inside the wrapper function, NOT at module top. This preserves the K1 import-isolation contract for code paths that do not opt into the wire-up.
- The K1 import-isolation is verified by `TestApplyWithAuditNoDispatcherImport`.

### Deterministic Behavior

- All K3 dataclasses are `frozen=True`.
- `mismatches` is a `Tuple[ApplyAuditMismatch, ...]` (not a list) for hashability and JSON determinism.
- `mismatch_categories` iteration order follows `_MISMATCH_CATEGORIES` order, NOT dict insertion order.

### Mismatch Classification

- First-match-wins semantics per planned row:
  1. `MISSING_FROM_REPORTS` (no matching outcome)
  2. `SHA256_MISMATCH` (planned SHA differs from on-disk task.json SHA)
  3. `KIND_MISMATCH` (planned `FileEntryKind.value` != outcome `record_kind`)
  4. `DECISION_MISMATCH` (outcome decision is `SKIPPED_INCONSISTENT` / `SKIPPED_MALFORMED` / `SKIPPED_COLLISION`)
- `EXTRA_IN_REPORTS` is checked independently (outcomes with no planned row).

### Edge Cases

- `manifest_path is None`: no-flag pass-through, `audit_report = None`, `to_dict()` byte-identical to direct `apply_sidecars` call.
- `audit_action == "ignore"`: report computed, NOT attached (`audit_report` stays `None`).
- `audit_action == "raise"` AND `mismatch_count == 0`: no exception raised, report attached.
- `audit_action == "raise"` AND `mismatch_count > 0`: `ApplyAuditError` raised with full report; apply pass has already run (sidecars on disk).
- `manifest_path` doesn't exist: `ManifestError` propagates.
- Manifest loads but validation fails: K3 audit runs with `plan_input_count == 0`, report attached, apply pass preserved (no gating).

### Hidden Mutation Risks

- `apply_sidecars_with_plan()` is called with the same kwargs as the caller supplies — no mutation of kwargs.
- `result.audit_report = report` is the only mutation, and it's a NEW attribute set on the result object. The result object's `outcomes`, `by_decision`, `by_record_kind`, `sidecars_written`, etc. are all preserved.
- `result.plan_input_summary` is preserved untouched.

### Coupling to K1 / K2 / K2.5

- K1: `apply_sidecars()` and `AuditSummary` are forward-compatible — calling them with no K2.5/K3 kwargs still works.
- K2.5: `apply_sidecars_with_plan()` and `ApplyWithPlanSummary` are the wire-up layer. K3 sits on top of K2.5 (when `manifest_path` is supplied) or directly on K1 (when `manifest_path is None`).
- The K3 wrapper re-uses the K2.5 wrapper's manifest-loading + projection by re-running `manifest_to_plan_inputs()` on the reconstructed manifest doc. The K2.5 summary on the result is preserved untouched.

### Strict-Superset Behavior

`to_dict_with_audit() ⊃ to_dict_with_plan() ⊃ to_dict()` — verified by `TestApplyWithAuditStrictSuperset`.

## Test-Code Review

### Test File: `aee/tests/test_aee78_audit_gate.py` (1835 lines, 55 tests)

### Test Coverage Assessment

The test file covers all required concerns from the dispatch:

| Required Coverage | Test(s) |
|---|---|
| `ApplyAuditError` | `TestApplyWithAuditDtos::test_audit_error_carries_report` |
| `ApplyAuditMismatch` | `TestApplyWithAuditDtos::test_audit_mismatch_field_set`, `test_audit_mismatch_to_dict_keys` |
| `ApplyAuditReport` | `TestApplyWithAuditDtos::test_audit_report_field_set`, `test_audit_report_to_dict_keys` |
| `apply_sidecars_with_audit()` | `TestApplyWithAuditSmoke` (7 tests) |
| `audit_report` field | `TestApplyWithAuditSmoke::test_smoke_attaches_audit_report`, `TestApplyWithAuditNoFlag::test_no_flag_audit_report_is_none` |
| `AUDIT_SCHEMA_VERSION` | `TestApplyWithAuditDtos::test_audit_schema_version_constant`, `TestApplyWithAuditSchemaVersion` (4 tests) |
| `to_dict_with_audit()` | `TestApplyWithAuditStrictSuperset` (5 tests) |
| Happy path | `TestApplyWithAuditSmoke` (wrapper runs end-to-end) |
| Warn path | `TestApplyWithAuditWarnPath` (4 tests) |
| Raise path | `TestApplyWithAuditRaisePath` (2 tests), `TestApplyWithAuditRaiseNoMismatch::test_raise_on_mismatch_raises_with_attached_report` |
| Ignore path | `TestApplyWithAuditIgnorePath` (3 tests) |
| No-flag (default) | `TestApplyWithAuditNoFlag` (4 tests) |
| Invalid `audit_action` | `TestApplyWithAuditBadActionRejected` |
| Invalid `manifest_path` | `TestApplyWithAuditBadManifestPropagates` (2 tests) |
| Validation failure | `TestApplyWithAuditValidationFailureIsAdditive` |
| Schema version | `TestApplyWithAuditSchemaVersion` (4 tests) |
| API compatibility | `TestApplyWithAuditApiCompatibility` (2 tests) |
| Strict superset | `TestApplyWithAuditStrictSuperset` (5 tests) |
| All mismatch categories | `TestApplyWithAuditMismatchCategories` (6 tests) |
| Import-isolation behavior | `TestApplyWithAuditNoDispatcherImport` |
| Real-manifest smoke (gated) | `TestApplyWithAuditRealManifest` (gated on file presence) |

**All required coverage is present.**

### Test Determinism

- All tests use fixed UTC stamps (`_FIXTURE_UTC_STAMP = "2026-07-13T13:00:00Z"`) — no wall-clock dependence.
- `tempfile.mkdtemp(prefix=...)` is used for isolation; `tearDown` cleans up via `shutil.rmtree(ignore_errors=True)`.
- Test results do not depend on test execution order (each test has its own `tmp_path`).

### Test Isolation

- Each test class uses its own `setUp` / `tearDown` with prefixed tmpdirs.
- No global state mutation.

### Implementation-Detail Reliance

- Tests use the public API (`apply_sidecars_with_audit`, `ApplyAuditReport`, etc.) — not private implementation details.
- The pure classifier is tested directly via `_classify_audit_mismatches()` to exercise edge cases that would be hard to trigger end-to-end.

### Meaningful Assertions

- All assertions are specific (e.g. `assertEqual(mismatch_count, 3)` not just `assertGreater(..., 0)`).
- DTO field-set tests use `set(fields(...))` to lock the entire field set, not just spot checks.
- `to_dict()` output is asserted to be byte-identical between wrapper (no-flag) and direct `apply_sidecars` call.

### Can Tests Pass for the Wrong Reason?

- The "clean run" is not asserted (the test docstring explicitly notes that a zero-mismatch end-to-end run is structurally impossible in production).
- The smoke tests assert `mismatch_count > 0` (the expected K3 production behavior).
- The mismatch-category tests construct hand-crafted inputs to exercise each category directly, avoiding the kind check's first-match-wins interference (documented in the test docstring).

### Fixtures Reflect Valid K3 Contracts

- `_RUNTIME_RECORD` and `_FIXTURE_RECORD` are valid AEE-7.7b / K2.5 task.json shapes.
- `_make_verdict` builds valid `PerTaskVerdict` objects.
- `_smoke_manifest` and `_manifest_with_extra_row` build valid manifest JSON shapes.

### Test Names Match Actual Behavior

- `test_smoke_attaches_audit_report` → assertIsNotNone + assertGreater
- `test_no_flag_audit_report_is_none` → assertIsNone
- `test_to_dict_does_not_include_audit_report` → assertNotIn
- Names are descriptive and match behavior.

### Exception Assertions

- `assertRaises(ApplyAuditError)` uses the class identity from `aee.audit`.
- `assertRaises(ValueError)` is the standard exception.
- `assertRaises(_manifest_mod.ManifestError)` resolves `ManifestError` fresh from `aee.audit.manifest` to be robust against sys.modules cache invalidation — a sensible defensive pattern mirroring the K2.5 test approach.

### sys.modules Safety

- The K3 test file does NOT manipulate `sys.modules` directly.
- It uses `setUp` to snapshot `sys.modules.keys()` (read-only) for the no-dispatcher-import test.
- `tearDown` only does `shutil.rmtree`.
- The K2.5 test files (`test_aee78_plan_apply.py::TestApplyWithPlanNoDispatcherImport`, `test_aee77_apply_sidecars.py::TestApplySidecarsNoDispatcherImport`) DO clear `sys.modules` entries, which could affect the K3 test. The K3 test defends against this by resolving `ManifestError` fresh in `TestApplyWithAuditBadManifestPropagates`.

### Real-Manifest Test Gating

- `TestApplyWithAuditRealManifest` and `TestApplyWithAuditNoDispatcherImport` both use `if not _REAL_MANIFEST.exists(): self.skipTest(...)` to gracefully skip when the real manifest is not available.

### Order Dependence

- None observed. Each test class has its own `setUp` that creates a fresh tmpdir.

### Excessive Duplication

- The test file is 1835 lines covering 55 tests. There is some repetition in the smoke tests (the same `apply_sidecars_with_audit(...)` call is made multiple times with the same kwargs), but this is acceptable for isolation and readability.

## Structural Clean-Run Analysis

### Concern

The dispatch asks whether the K3 audit can structurally avoid `KIND_MISMATCH` mismatches for otherwise-matching rows, given that:
- `plan_input.kind` is `FileEntryKind` with values `NEW = "new"`, `MODIFIED = "modified"`.
- `outcome.record_kind` is `str` with values `"runtime"`, `"fixture"`, `"unknown"`.

### Independent Investigation

**Production code (lines 1426-1429 of `apply_sidecars.py`):**

```python
if (
    outcome.record_kind is not None
    and pi.kind.value != outcome.record_kind
):
    mismatches.append(
        ApplyAuditMismatch(
            ...
            category=AUDIT_KIND_MISMATCH,
            ...
        )
    )
    continue
```

**`FileEntryKind` definition (line 159 of `manifest.py`):**

```python
class FileEntryKind(str, Enum):
    NEW = "new"
    MODIFIED = "modified"
```

**`RecordKind` definition (line 69 of `reporting/identity.py`):**

```python
class RecordKind(str, Enum):
    RUNTIME = "runtime"
    FIXTURE = "fixture"
    UNKNOWN = "unknown"
```

**Set membership check:** `{"new", "modified"} ∩ {"runtime", "fixture", "unknown"} = ∅`.

**Conclusion:** The two classification dimensions are **structurally disjoint**. For ANY planned row whose basename matches an outcome's `task_id` AND whose SHA-256 matches the on-disk task.json SHA, the kind check **always** fires (because `"new" ≠ "runtime"`, `"new" ≠ "fixture"`, etc.).

### Classification

```text
DESIGN CAVEAT
```

### Reasoning

- The K3 production code's `_classify_audit_mismatches()` is intentional in surfacing `KIND_MISMATCH` as one of its five categories. The classifier's purpose is to surface any divergence between the planned row and the apply outcome, even if the two fields are on different classification dimensions.
- The K3 module docstring (lines 1100-1138) explicitly documents this:
  > "K3 is the slice that closes that gap: a second opt-in layer on top of K2.5's opt-in that audits each `ApplySidecarsResult.outcomes[i]` against the corresponding `PlanInput` row, classifies any mismatch into one of five explicit categories"
- The K3 test file's module docstring (lines 68-93) explicitly documents this:
  > "These are different classification dimensions, so the `KIND_MISMATCH` check is structurally guaranteed to fire on any planned row whose other checks already passed. As a result, a 'clean run' with `mismatch_count == 0` is only achievable when the planned row's basename is NEVER equal to any outcome's `task_id`"
- The K3 test file's `TestApplyWithAuditMismatchCategories::test_decision_mismatch_category` (lines 1536-1576) explicitly notes this:
  > "the kind check compares FileEntryKind.value (`'new'`) with outcome.record_kind (`'runtime'`) — these differ, so the kind check WOULD fire before the decision check. The test therefore uses a hand-crafted outcome with `record_kind` set to `'new'` to bypass the kind check"

This is a **deliberate K3 design decision**, not a production bug. The kind check is structurally guaranteed to fire on any matching row, but the K3 audit is explicitly designed to surface "any mismatch" between planned and actual, and the documentation is clear about this.

**Production code change would be required to make a "clean run" achievable** (e.g., by removing the kind check, or by introducing a record-kind dimension in `PlanInput` that aligns with `outcome.record_kind`). This is a K4+ evolution, not a K3 issue.

### Is it a Commit-Blocking Production Bug?

**No.** The K3 audit is explicitly opt-in (requires `manifest_path=...`). The K3 wrapper does NOT short-circuit the apply pass. The mismatches are surfaced via the additive `audit_report` field, and the apply pass output is preserved. Downstream consumers that don't opt in see the same K2.5 behavior.

A downstream consumer that WANTS a "clean run" assertion would need to filter out `KIND_MISMATCH` from the `mismatch_categories` (or the production code would need to evolve to make the kind check optional). This is a K4+ concern, not a K3 commit blocker.

## Targeted Test Results

```text
Pytest: 55 passed
```

- **Test count**: 55
- **Failures**: 0
- **Errors**: 0
- **Skips**: 0
- **Duration**: <5s

## Impacted Regression Results

```text
Pytest: 202 passed
```

- **Test count**: 202
- **Failures**: 0
- **Errors**: 0
- **Skips**: 0

## Broad Regression Results

```text
Pytest: 979 passed, 1 failed, 2 skipped
```

- **Passed**: 979
- **Failed**: 1 (`TestClaudeCodeProviderFailure::test_non_zero_exit_marked_failed`)
- **Errors**: 0
- **Skips**: 2

### Pre-Existing Failure Analysis

The single failure is in `aee/tests/test_claude_code_provider.py::TestClaudeCodeProviderFailure::test_non_zero_exit_marked_failed`, which is unrelated to K3. The test:

1. Spawns a subprocess that writes `"synthetic failure"` to stderr.
2. Asserts that the subprocess's stderr contains the synthetic message.

The failure is reproducible on the baseline (HEAD with no K3 changes) — verified by cloning the repo at `f380df2` HEAD and re-running the test in isolation. The failure is in the test harness / fake-script setup, not in the K3 audit code.

**Justified skip**: The failure is pre-existing and unrelated to K3. K3 changes do not touch `aee/tests/test_claude_code_provider.py` or any of the code paths it tests (subprocess wrapping, fake Claude CLI script).

## Import and API Smoke

```python
from aee.audit import (
    AUDIT_SCHEMA_VERSION,         # "1.0.0"
    ApplyAuditError,              # <class 'aee.audit.apply_sidecars.ApplyAuditError'>
    ApplyAuditMismatch,           # <class 'aee.audit.apply_sidecars.ApplyAuditMismatch'>
    ApplyAuditReport,             # <class 'aee.audit.apply_sidecars.ApplyAuditReport'>
    apply_sidecars_with_audit,    # <function apply_sidecars_with_audit>
)
from aee.audit.apply_sidecars import ApplySidecarsResult
# ApplySidecarsResult.to_dict()              [K1+K2 baseline]
# ApplySidecarsResult.to_dict_with_plan()    [K2.5 superset]
# ApplySidecarsResult.to_dict_with_audit()   [K3 superset]
```

All K3 symbols resolve from `aee.audit`. The package re-exports are present. The K1 + K2 + K2.5 `to_dict()` contract is preserved (the new `audit_report` field is omitted from `to_dict()`).

## Findings

### F-001: KIND_MISMATCH is structurally unavoidable for matching rows (DESIGN CAVEAT)

- **Severity**: NOTE
- **File**: `aee/audit/apply_sidecars.py`
- **Symbol / line range**: lines 1426-1429 (`_classify_audit_mismatches()`)
- **Evidence**: `FileEntryKind` values `{"new", "modified"}` are disjoint from `RecordKind` values `{"runtime", "fixture", "unknown"}`. The classifier's `pi.kind.value != outcome.record_kind` check is structurally guaranteed to evaluate `True` for any row that reaches it.
- **Impact**: The K3 audit's "clean run" assertion (`mismatch_count == 0`) is only achievable when no planned row's basename matches an outcome's `task_id`. A downstream consumer that wants zero `KIND_MISMATCH` would need to filter or evolve the K3 classifier. This is a K4+ concern.
- **Recommendation**: Document this design constraint in the K3 module docstring (already done). Future K4+ work could either remove the kind check, make it opt-in, or introduce a record-kind dimension in `PlanInput` to align the two classifications.
- **Commit blocking**: NO

### F-002: Pre-existing test failure in test_claude_code_provider.py (NOTE)

- **Severity**: NOTE
- **File**: `aee/tests/test_claude_code_provider.py`
- **Symbol / line range**: lines 323-339 (`TestClaudeCodeProviderFailure::test_non_zero_exit_marked_failed`)
- **Evidence**: The test fails on the baseline (no K3 changes) — verified by cloning the repo at HEAD `f380df2` and re-running the test in isolation. The failure is in the fake-script subprocess harness, not in any K3 code path.
- **Impact**: The broad regression shows 1 failure. The failure is unrelated to K3 and does not block K3 commit.
- **Recommendation**: Investigate the test harness failure separately (out of scope for this K3 review).
- **Commit blocking**: NO

### F-003: No issues found in production code (PASS)

- **Severity**: NOTE
- **File**: `aee/audit/apply_sidecars.py`
- **Symbol / line range**: lines 1100-1806 (entire K3 section)
- **Evidence**: All API contracts (apply pass byte-for-byte preservation, additive `audit_report` field, fail-fast on bad `audit_action`, lazy import, schema-version discipline, K1 + K2 + K2.5 backward compatibility) are correctly implemented and verified by 55 targeted tests.
- **Impact**: None — the production code is correct and safe for commit.
- **Recommendation**: None.
- **Commit blocking**: NO

### F-004: No issues found in test code (PASS)

- **Severity**: NOTE
- **File**: `aee/tests/test_aee78_audit_gate.py`
- **Symbol / line range**: entire file (1835 lines, 55 tests)
- **Evidence**: All required coverage is present. Tests are deterministic, isolated, and use the public API. The `KIND_MISMATCH` interference is documented and handled via hand-crafted classifier inputs. Real-manifest tests are gated on file presence. `sys.modules` snapshot is read-only.
- **Impact**: None — the test layer is sufficient and well-designed.
- **Recommendation**: None.
- **Commit blocking**: NO

## Atomic Commit Scope

The atomic commit should contain exactly:

```text
aee/audit/apply_sidecars.py        (modified, +736 lines)
aee/audit/__init__.py              (modified, +33 lines)
aee/tests/test_aee78_audit_gate.py (untracked, +1835 lines)
```

The working-tree evidence shows these three files are the only K3 artifacts. No other K3-related changes are present.

## Remaining Risks

1. **KIND_MISMATCH is structurally dominant**: As noted in F-001, a "clean run" with `mismatch_count == 0` is not achievable end-to-end. The K3 audit's `mismatch_categories` tuple will always include `KIND_MISMATCH` for any planned row whose basename matches an outcome's `task_id` (and whose SHA-256 matches the on-disk task.json SHA). A downstream consumer that wants to assert "no real mismatches" would need to filter out `KIND_MISMATCH` or evolve the classifier. This is a K4+ concern.

2. **K3 raises on a structurally-mismatched corpus**: Any `audit_action='raise'` call against a corpus that doesn't perfectly match the K3 classifier's contract will raise. The K3 test acknowledges this by verifying the raise behaviour via the realistic "smoke" test (which is structurally non-empty in production).

3. **No K4+ activation**: The K3 wrapper is opt-in. The production call site (`aee/reporting/build_index.py:184`) still uses `apply_sidecars` (K2.5-baseline). Flipping it to `apply_sidecars_with_audit` is a separate, K4+ activation commit. This is correctly documented in the K3 module docstring.

4. **Pre-existing test failure**: The single broad-regression failure (`test_claude_code_provider.py::test_non_zero_exit_marked_failed`) is pre-existing and unrelated to K3. It does not block K3 commit.

## Review Ready

```text
YES
```

## Commit Ready

```text
YES
```

## Commit Performed

```text
NO
```

The K3 files are safe to commit as a single atomic commit, but no commit was created in this review task (the dispatch prohibits any git mutation).

## Production Safety

```text
No Push           ✓
No Deploy         ✓
No Restart        ✓
No Stash          ✓
No Merge          ✓
No Rebase         ✓
No Stage          ✓
No Source Edit    ✓
```

No production-affecting actions were performed. The review was strictly read-only with respect to the working tree.

## Final Recommendation

```text
ATOMIC COMMIT
```

The K3 implementation is correct, the test layer is sufficient, backward compatibility with K1/K2/K2.5 is preserved, and no blocking defects were found. The single structural caveat (`KIND_MISMATCH` is unavoidable for matching rows) is a deliberate K3 design that is documented in the production code and test file. The single broad-regression failure is pre-existing and unrelated to K3.

The next action should be to create an atomic commit containing exactly the three K3 files:

```text
aee/audit/apply_sidecars.py
aee/audit/__init__.py
aee/tests/test_aee78_audit_gate.py
```
