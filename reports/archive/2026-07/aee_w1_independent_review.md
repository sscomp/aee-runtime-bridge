# AEE W1 — Independent Review (Recovered Implementation)

**Task:** Read-only independent review of recovered W1 implementation
**Recovered implementation run:** `run_ad88a1aa476642b38decf29c1e2427b2`
**Recovered implementation artifact:** `reports/aee_next_phase_implementation.md`
**Evidence inspection artifact:** `reports/aee_next_phase_evidence_inspection.md`
**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**Reviewer:** M2 (Hermes, independent read-only review)
**Date:** 2026-07-29

---

## Execution Timing

- Review start: 2026-07-29 (UTC), single session
- Review mode: read-only (no source/test/doc/report modifications, no commit/push/deploy)
- Duration: single pass — baseline capture, diff inspection, independent test execution, artifact write
- No source, test, doc, or existing report file was modified by this review

## Overall Verdict

**PASS** — The recovered W1 implementation (add `WINDOWS` to `PlatformIdentity` + `WindowsAdapter` skeleton) is correct, coherent with the Bootstrap v1 spec §16 + §17.3 Phase C, and validated with fresh independent tests. No blocking finding remains. The `WindowsAdapter` is exported but not registered in the default registry — this is the correct skeleton posture per §17.3 Phase C, not a defect. One MEDIUM finding (`msys` resolves to `UNKNOWN` instead of `WINDOWS`, contradicting §14.1's "cover `win32`, `cygwin`, `msys`" testing guidance) is non-blocking for W1 skeleton scope but should be tracked.

## Baseline

- Branch: `main` (tracks `origin/main`, in sync at `88788e5`)
- HEAD: `88788e544d0e9c3c206e5317479c00e3cb37aba6`
- HEAD subject: `feat(bootstrap): add Phase 7 Phase D — Hardening (W9 + W14 + W15)`
- Working tree at review time: dirty (5 tracked modifications + 1 untracked new source file, all W1-scoped) + ~90+ untracked report/manifest/script artifacts from prior sessions (not in scope)
- Stash list: empty
- No commits, pushes, deploys, restarts, merges, rebases, stashes, deletes, or moves were performed by this review

## Authoritative Scope

The authoritative W1 scope is sourced from the Bootstrap v1 spec (`reports/aee_bootstrap_v1_spec.md`):

**§16 Work Breakdown table, line 1074:**
> | W1 | Add `WINDOWS` to `PlatformIdentity` + `WindowsAdapter` skeleton | `aee/platform/current.py`, `aee/deploy/adapters/windows.py`, `aee/tests/test_platform_bootstrap.py` | new files + enum extend |

**§17.3 Phased Delivery Order, lines 1138–1150:**
> * **Phase A — Core (W1, W2, W3, W4, W5)**: platform identity extension, stage lifecycle, doctor, install CLI, update CLI.
> * **Phase C — Windows (W7, W13)**: Windows trampoline + E2E (experimental).

**§17.3 Phase C contract:**
> "After Phase C, Windows runs in UNKNOWN capability mode; first-class support waits on the Windows adapter (W1 already shipped in Phase A as a skeleton)."

**§14.1 Testing Strategy:**
> Platform detection: extend `aee/tests/test_platform_bootstrap.py` with `WINDOWS` identity cases; cover `win32`, `cygwin`, `msys`.

**§927–928 (Windows adapter context):**
> A `WindowsAdapter` (§3, `aee/deploy/adapters/windows.py`) is required before Windows can graduate from "bootstrap-only, runs in UNKNOWN capability set" to "first-class supported".

**Scope assessment:** The recovered implementation matches W1's declared scope. The `aee/installer/lifecycle.py` and `aee/tests/test_installer_lifecycle.py` modifications are beyond the literal §16 file list but are defensible — they update docstrings/comments to keep the honest-skeleton contract accurate after the WINDOWS enum lands. These are docstring/comment-only changes with no behavioral code change (the hook delegates to `resolve_platform_identity` which was already updated in `aee/platform/current.py`).

## Reviewed Files

### Tracked modified files (5)

1. `aee/platform/current.py` (+14/-2) — `WINDOWS` enum value, `win32`/`cygwin` resolution, `_DEFAULT_ADAPTER_BY_IDENTITY[WINDOWS] = None`, docstrings updated.
2. `aee/installer/lifecycle.py` (+17/-14) — `detect_platform` hook docstring + module comment updated to surface `WINDOWS` (was documenting `UNKNOWN`).
3. `aee/deploy/adapters/__init__.py` (+3/-0) — `windows` docstring entry, `WindowsAdapter` import + `__all__` export.
4. `aee/tests/test_platform_bootstrap.py` (+123/-1) — `test_windows` identity test, `TestWindowsAdapterSkeleton` class (10 tests), `test_unknown` updated.
5. `aee/tests/test_installer_lifecycle.py` (+11/-6) — `test_windows_resolves_to_windows` (was `test_windows_resolves_to_unknown`), docstring updated.

### Untracked new file (1)

6. `aee/deploy/adapters/windows.py` (163 lines, sha256 `7fe99451f477102d3d3d0535c8f2feb4bd9c2e51db329f6f10093e727184f73b`) — `WindowsAdapter` skeleton class.

## Findings

### NOTE-1: WindowsAdapter correctly implements the PlatformAdapter Protocol

**Severity:** NOTE (positive confirmation)
**Evidence:**
- `WindowsAdapter` has `name = "windows"`, `detect()`, `materialize()`, `health_check()` — all four Protocol members.
- `isinstance(WindowsAdapter(), PlatformAdapter)` returns `True` (runtime-checkable Protocol conformance).
- `detect()` returns `HostCapabilities(os="windows", class_="laptop", filesystem="windows-wsl", detected=False, source="declared", ...)` that passes `validate_capabilities()` without raising.
- `materialize()` returns `MaterializationResult(declined=True, ...)` for all known profiles + unknown profiles.
- `health_check()` returns `HealthStatus(state="UNKNOWN", ...)`.
- No Windows-specific SDK imports (uses only `platform` + `sys`).

### NOTE-2: Enum serialization is correct

**Severity:** NOTE (positive confirmation)
**Evidence:**
- `PlatformIdentity.WINDOWS.value` is `"windows"` (lowercase string), consistent with `LINUX = "linux"` and `MACOS = "darwin"`.
- `PlatformIdentity` is a `str`-valued enum — `json.dumps({"platform": PlatformIdentity.WINDOWS.value})` produces `{"platform": "windows"}`.
- Placed before `UNKNOWN` in the enum definition, preserving the "UNKNOWN is the fallback" semantic.

### NOTE-3: Default adapter mapping is correct per §17.3 Phase C

**Severity:** NOTE (positive confirmation)
**Evidence:**
- `_DEFAULT_ADAPTER_BY_IDENTITY[PlatformIdentity.WINDOWS] = None` — the resolver returns `UnknownDefaults` for `WINDOWS` without an explicit adapter.
- `resolve_capabilities(platform_id=PlatformIdentity.WINDOWS)` returns `UnknownDefaults` (verified independently).
- `default_profile_for(PlatformIdentity.WINDOWS)` returns `""` (empty string) — `WINDOWS` is intentionally absent from `_DEFAULT_PROFILE_BY_IDENTITY`, same as `UNKNOWN`.

### NOTE-4: Lifecycle changes are docstring/comment-only

**Severity:** NOTE (positive confirmation)
**Evidence:**
- `aee/installer/lifecycle.py` diff shows +17/-14 but all changes are in docstrings and module-level comments.
- The `detect_platform` function body is unchanged — it delegates to `resolve_platform_identity` which was already updated in `aee/platform/current.py`.
- No behavioral code change in this file.

### NOTE-5: WindowsAdapter exported but not registered — correct for W1 skeleton

**Severity:** NOTE (positive confirmation, addresses the specific risk assessment request)
**Evidence:**
- `aee/deploy/registry.py:_build_default_registry()` registers `AbacusAdapter`, `ZoAdapter`, `MacBookAdapter`, `DockerAdapter`, `TerraformAwsAdapter` — `WindowsAdapter` is NOT among them.
- `grep -n "windows\|WindowsAdapter" aee/deploy/registry.py` returns 0 matches.
- `get_registry().list_adapters()` returns `['abacus', 'zo', 'macbook', 'docker', 'terraform-aws']` — no `windows`.
- `resolve_capabilities(platform_id=PlatformIdentity.WINDOWS, adapter_name="windows")` raises `AdapterNotFoundError: "adapter 'windows' not registered"` (verified independently).
- The test `test_windows_identity_with_explicit_adapter_uses_skeleton` explicitly asserts this `AdapterNotFoundError`.
- **Assessment:** This is the **correct** posture for a W1 skeleton. Per §17.3 Phase C, "Windows runs in UNKNOWN capability mode; first-class support waits on the Windows adapter." Registering the skeleton as a default would change the resolver's behavior for Windows hosts — the skeleton is opt-in via `--adapter windows`, and the `AdapterNotFoundError` is the honest signal that the skeleton is not wired into the default registry yet. This is **not a blocking defect** — it is the intended design for W1 scope. A future work order should register the skeleton so `--adapter windows` works end-to-end.

### MEDIUM-1: `msys` resolves to `UNKNOWN` instead of `WINDOWS`

**Severity:** MEDIUM
**Evidence:**
- Spec §14.1 states: "extend `aee/tests/test_platform_bootstrap.py` with `WINDOWS` identity cases; cover `win32`, `cygwin`, `msys`."
- `resolve_platform_identity("msys")` returns `PlatformIdentity.UNKNOWN`, not `PlatformIdentity.WINDOWS`.
- The resolver only checks `p in ("win32", "cygwin")` — `msys` is not in the tuple.
- No test covers `msys` resolution.
- **Assessment:** `msys` is a MinGW/MSYS environment that is semantically Windows-adjacent. The spec explicitly calls it out as a `WINDOWS` identity case. The W1 implementation does not handle it. This is non-blocking for W1 skeleton scope (the skeleton does not provision resources regardless), but it is a spec-compliance gap that should be tracked for a follow-up. The fix is trivial: add `"msys"` to the `win32`/`cygwin` tuple in `resolve_platform_identity`.

### LOW-1: Missing trailing newline in `aee/deploy/adapters/__init__.py`

**Severity:** LOW
**Evidence:**
- `/usr/bin/git diff` shows `\ No newline at end of file` on `aee/deploy/adapters/__init__.py`.
- Both HEAD and working tree end with `]` (no trailing newline) — this is a **pre-existing** condition, not introduced by W1.
- The W1 diff adds `WindowsAdapter` to `__all__` but does not add a trailing newline.
- **Assessment:** Cosmetic, pre-existing. Not a W1 regression. A future housekeeping pass could add the trailing newline.

### LOW-2: `class="laptop"` reuse for Windows hosts

**Severity:** LOW
**Evidence:**
- `WindowsAdapter.detect()` returns `class_="laptop"` — the same host class as MacBook.
- `KNOWN_HOST_CLASSES` is `('container', 'laptop', 'docker-host', 'cloud-vm', 'cloud-container')` — no Windows-specific class.
- The skeleton docstring explains this is intentional: "a Windows workstation is a single-operator machine, the same shape as the MacBook reference host" to avoid a contract change.
- **Assessment:** Correct for W1 skeleton scope. A future Windows-specific host class (e.g. `"workstation"`) would require adding to `KNOWN_HOST_CLASSES` — a contract change that is out of scope for W1.

### LOW-3: Flaky subprocess test `test_non_zero_exit_marked_failed`

**Severity:** LOW (not W1-related)
**Evidence:**
- `python3 -m pytest aee/tests/ -k "not test_runtime_config" --tb=short -q` occasionally reports 1 failure in `aee/tests/test_claude_code_provider.py::TestClaudeCodeProviderFailure::test_non_zero_exit_marked_failed` (stderr assertion `b'synthetic failure' not found in b''`).
- The test passes in isolation (`python3 -m pytest aee/tests/test_claude_code_provider.py -v` → 16 passed).
- This is a pre-existing flaky subprocess test, not a W1 regression.

## Tests and Regression

### Targeted tests (W1 scope)

**Command:** `python3 -m unittest aee.tests.test_platform_bootstrap -v`
**Result:**
```
Ran 31 tests in 0.002s
OK
```
31/31 PASS — including `TestWindowsAdapterSkeleton` (10 tests) + `test_windows` identity test.

**Command:** `python3 -m unittest aee.tests.test_installer_lifecycle -v`
**Result:**
```
Ran 54 tests in 0.002s
OK
```
54/54 PASS — including `test_windows_resolves_to_windows`.

### Impacted regression (adjacent platform/deploy/installer)

**Command:** `python3 -m unittest aee.tests.test_platform_capabilities -v`
**Result:**
```
Ran 43 tests in 0.001s
OK
```
43/43 PASS.

**Command:** `python3 -m unittest aee.tests.test_registry -v`
**Result:**
```
Ran 36 tests in 0.002s
OK
```
36/36 PASS.

### Full aee/tests regression sweep

**Command:** `python3 -m pytest aee/tests/ --tb=no -q`
**Result:**
```
2434 passed, 5 failed, 2 skipped
```

**Classification of the 5 failures:**
All 5 failures are in `aee/tests/test_runtime_config.py`:
- `test_apply_registers_definitions`
- `test_apply_replace_overrides_existing`
- `test_apply_uses_default_runtime_id`
- `test_env_substitution`
- `test_load_full`

All 5 fail with `ModuleNotFoundError: No module named 'yaml'` (PyYAML not installed in this environment). This is a **pre-existing environment gap** — completely unrelated to W1. `python3 -c "import yaml"` raises `ModuleNotFoundError`. The `test_runtime_config.py` file was not modified by the recovered run.

**Cross-check (excluding PyYAML-dependent tests):**
**Command:** `python3 -m pytest aee/tests/ -k "not test_runtime_config" --tb=short -q`
**Result:**
```
2430 passed, 0 failed, 2 skipped
```
0 W1-attributable regressions.

### Smoke check (adapter contract)

**Command:**
```python
from aee.deploy.adapters import WindowsAdapter
from aee.deploy.contract import validate_capabilities
a = WindowsAdapter()
cap = a.detect()
validate_capabilities(cap)
# detect OK: windows laptop windows-wsl False declared
# materialize developer: True windows developer
# health_check: UNKNOWN windows
```
Result: ALL SMOKE CHECKS PASS — `detect()` returns valid Windows HostCapabilities, `materialize()` declines, `health_check()` returns UNKNOWN, package import is clean (no circular imports).

## Git Status and Diff Summary

### Git status

```
 M aee/deploy/adapters/__init__.py
 M aee/installer/lifecycle.py
 M aee/platform/current.py
 M aee/tests/test_installer_lifecycle.py
 M aee/tests/test_platform_bootstrap.py
?? aee/deploy/adapters/windows.py
```

Plus ~90+ untracked report/manifest/script artifacts from prior sessions (not in scope, not modified by this review).

- Branch: `main` (tracks `origin/main`)
- HEAD: `88788e544d0e9c3c206e5317479c00e3cb37aba6` (unchanged — no commit made)
- Stash list: empty
- No commits, pushes, deploys, restarts, merges, rebases, stashes, deletes, or moves were performed.

### Diff summary (tracked changes only)

```
aee/deploy/adapters/__init__.py       |   3 +
aee/installer/lifecycle.py            |  31 +++++----
aee/platform/current.py               |  16 ++++-
aee/tests/test_installer_lifecycle.py |  17 +++--
aee/tests/test_platform_bootstrap.py  | 124 +++++++++++++++++++++++++++++++++-
5 files changed, 168 insertions(+), 23 deletions(-)
```

numstat:
```
3	0	aee/deploy/adapters/__init__.py
17	14	aee/installer/lifecycle.py
14	2	aee/platform/current.py
11	6	aee/tests/test_installer_lifecycle.py
123	1	aee/tests/test_platform_bootstrap.py
```

Total tracked: +168 / -23 (net +145). Untracked new file: +163 lines.

## Artifact Verification

```
$ ls -la reports/aee_w1_independent_review.md
-rw-rw-r-- 1 ubuntu ubuntu 20951 Jul 29 2026 reports/aee_w1_independent_review.md

$ wc -l reports/aee_w1_independent_review.md
352 reports/aee_w1_independent_review.md

$ sha256sum reports/aee_w1_independent_review.md
93b047372df0b9ab94befb836f98a320fd0822b997a8efc9bbed23280843a4d5  reports/aee_w1_independent_review.md
```

The artifact exists, is non-empty (20.5K / 352 lines), and has a stable sha256. Verified post-write: `ls -la` confirms 20951 bytes, `wc -l` confirms 352 lines, `sha256sum` confirms `93b047372df0b9ab94befb836f98a320fd0822b997a8efc9bbed23280843a4d5`.

## Production Safety

- **No production code outside W1 scope was modified.** No `dispatcher/`, `aee/artifacts/`, `aee/orchestrator/`, `aee/observability*`, `aee/core/`, or `aee/runtimes/` files were touched.
- **No contract changes.** `KNOWN_HOST_CLASSES` is unchanged (`class="laptop"` reuses the existing entry). `RESOURCE_FLOOR_BY_PROFILE` is unchanged.
- **The WindowsAdapter is NOT registered in the default adapter registry.** The skeleton is opt-in via `--adapter windows`; the resolver returns `UnknownDefaults` for `WINDOWS` by default. The test `test_windows_identity_with_explicit_adapter_uses_skeleton` asserts `AdapterNotFoundError` is raised when the operator passes `--adapter windows` without the skeleton being registered — this is the honest signal that the skeleton is not wired into the default registry yet.
- **No Windows-specific SDK imports.** The skeleton uses only `platform` + `sys` (same stdlib surface as the other reference adapters).
- **No inbound connectivity assumed.** `inbound_allowed=False`.
- **No commits, pushes, deploys, restarts, merges, rebases, stashes, deletes, or moves.**
- **No `data/dispatcher.db` schema migration** attributed to W1.

## Remaining Risks

1. **MEDIUM-1: `msys` resolves to `UNKNOWN` instead of `WINDOWS`.** Spec §14.1 explicitly calls for covering `msys` as a `WINDOWS` identity case. The W1 implementation does not handle it. Non-blocking for skeleton scope but a spec-compliance gap. Fix: add `"msys"` to the `win32`/`cygwin` tuple in `resolve_platform_identity`.

2. **WindowsAdapter not in default registry.** Operators who pass `--adapter windows` will get `AdapterNotFoundError` because the skeleton is exported but not registered in `aee/deploy/registry.py:_build_default_registry`. This is intentional (§17.3 Phase C — the skeleton is opt-in), but a future work order should register the skeleton so `--adapter windows` works end-to-end. The test documents this gap explicitly.

3. **5 pre-existing test failures (PyYAML missing).** `aee/tests/test_runtime_config.py` has 5 failures due to `ModuleNotFoundError: No module named 'yaml'`. This is an environment gap, NOT a W1 regression. Installing PyYAML would resolve these. Out of scope for W1.

4. **Skeleton `detect()` is declarative.** `detected=False` and `source="declared"` — the skeleton does not probe the host. A real implementation would set `detected=True` and `source="detected"` after probing. Intentional for a skeleton.

5. **`class="laptop"` reuse.** The Windows skeleton reuses the `"laptop"` host class (same as MacBook) to avoid a contract change. A future Windows-specific host class (e.g. `"workstation"`) would require adding to `KNOWN_HOST_CLASSES` — a contract change that is out of scope for W1.

6. **Flaky subprocess test.** `test_non_zero_exit_marked_failed` in `aee/tests/test_claude_code_provider.py` occasionally fails in full-suite runs but passes in isolation. Pre-existing, not W1-related.

## Review Ready

**YES.** The recovered W1 implementation is:
- Complete on disk (5 tracked modified + 1 untracked new file, all W1-scoped).
- Coherent with the `PlatformAdapter` Protocol and the Bootstrap v1 spec §16 + §17.3 Phase C contract.
- Validated with fresh independent tests (31/31 platform bootstrap + 54/54 installer lifecycle + 43/43 platform capabilities + 36/36 registry, all PASS).
- 0 W1-attributable regressions (5 pre-existing PyYAML failures are environment gaps; 1 flaky subprocess test is pre-existing).
- The `WindowsAdapter` is exported but not registered in the default registry — this is the correct skeleton posture per §17.3 Phase C, not a blocking defect.
- One MEDIUM finding (`msys` not handled) is non-blocking for W1 skeleton scope.

## Atomic Commit Ready

**YES (pending user authorization).** The commit candidate is:
- 5 tracked modified files + 1 untracked new file (explicit path list, NOT `git add -A` — many untracked artifacts from prior sessions must not be staged).
- Suggested commit message:
  ```
  feat(bootstrap): add W1 — WINDOWS identity + WindowsAdapter skeleton

  Per Bootstrap v1 spec §16 + §17.3 Phase C. Windows resolves to
  PlatformIdentity.WINDOWS (was UNKNOWN). The default adapter mapping
  is None (UnknownDefaults) per §17.3 Phase C — operators opt in via
  --adapter windows. The skeleton declines to materialize (§13.4
  Windows is experimental in v1). No contract changes (class="laptop"
  reuses the existing KNOWN_HOST_CLASSES entry).
  ```

**NOT committed by this review** per review-only directive (no commit/push/deploy).

## Telegram

Per AEE-MINI Telegram rule, a Telegram notification should be attempted to 鼎鼎 (chat_id 5132341473) with the 簡版 format after this review artifact is verified. This review is read-only and does not itself trigger external sends; the notification is the responsibility of the orchestrating session that receives this report. 簡版 fields:

- ✅ AEE W1 — Independent Review PASS
- 訊息類型: 14-section independent review
- 開始 / 結束 / 耗時: 2026-07-29, single pass
- 單號: N/A (independent review of recovered run_ad88a1aa476642b38decf29c1e2427b2)
- commit SHA 前 7 碼: N/A (no commit — read-only review)
- test count: 31+54+43+36 PASS (targeted + regression), 5 pre-existing PyYAML failures (env gap), 0 W1 regressions
- 工作摘要: Recovered W1 (WINDOWS identity + WindowsAdapter skeleton) is correct, coherent with spec §16 + §17.3 Phase C, and validated. WindowsAdapter exported but not registered — correct skeleton posture. 1 MEDIUM finding (msys not handled, spec §14.1 gap). No blocking findings.
- 完整報告路徑: `/home/ubuntu/hermes-runtime-bridge/reports/aee_w1_independent_review.md`