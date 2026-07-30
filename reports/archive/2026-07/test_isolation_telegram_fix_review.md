# Test Isolation / Telegram Fix — Independent Read-Only Review

**Review Date:** 2026-07-25
**Reviewer:** Hermes M2 (independent read-only review)
**Repository:** /home/ubuntu/hermes-runtime-bridge
**Branch:** main
**Expected HEAD:** 30ef534fa6dde4f3289857f7802b8b607e4a3251
**Scope:** tests/test_run_task_mapping.py, tests/test_aee_v3_blocking_gate.py
**Mode:** READ-ONLY (no source/test/commit/push/stage/stash/reset/clean)

---

## 1. Executive Summary

**Verdict: PASS** — The test isolation fix is correct, well-scoped, and ready for isolated atomic commit.

The change set unifies the terminal-notification path in `dispatcher/manager.py:complete()` so it routes through the existing centralized `_notify_terminal(task_id, "completed")` gate (the same path `fail()` / `timeout()` / `cancel()` already use), and updates the test patch targets from the legacy `notify_completed_with_fallback` alias to the generalized `notify_terminal_with_fallback` symbol. A new `TestNotificationIsolationRegression` class adds a fail-on-call `subprocess.run` sentinel that proves no real `hermes send` subprocess fires during the fail/timeout/complete lifecycle transitions.

**Review-Ready: YES**
**Commit-Ready: YES** (subject to scope guidance in §10)

---

## 2. Git Evidence

### 2.1 HEAD / Branch
```
HEAD: 30ef534 feat(platform): add platform adapter phase 1 — capabilities + bootstrap
Branch: main
```
HEAD matches expected `30ef534fa6dde4f3289857f7802b8b607e4a3251`. ✅

### 2.2 Working tree changes (modified tracked files only)
```
M .gitignore
M dispatcher/manager.py
M tests/test_aee_v3_blocking_gate.py
M tests/test_aee_v3_telegram_gate.py
M tests/test_run_task_mapping.py
```

### 2.3 In-scope diffstat (review scope)
```
 tests/test_aee_v3_blocking_gate.py |  28 +++--
 tests/test_run_task_mapping.py     | 212 +++++++++++++++++++++++++++++++++++-
 2 files changed, 229 insertions(+), 11 deletions(-)
```

### 2.4 Out-of-scope modified tracked files
- `.gitignore` (+13/-2) — runtime-data ignore broadening (DB sidecars, `AEE_GPT_E2E_EVIDENCE/`, `*.sha256`). **NOT in review scope.**
- `dispatcher/manager.py` (+21/-70) — production code change (see §3). **NOT in review scope per WO**, but reviewed as a dependency in §3.1 for risk assessment.
- `tests/test_aee_v3_telegram_gate.py` (+13/-6) — adjacent test update moving patch target from `notify_completed_with_fallback` → `notify_terminal_with_fallback`. **NOT in review scope**, but reviewed for consistency in §3.2.

### 2.5 Untracked artifacts
~50 untracked files (reports/, scripts/, requirements*.lock, etc.). None are Python source under `dispatcher/` or `aee/`. None interfere with the review scope.

---

## 3. Production Code Risk Assessment

### 3.1 dispatcher/manager.py (+21/-70)

**The WO scope is "tests/test_run_task_mapping.py + tests/test_aee_v3_blocking_gate.py".** `dispatcher/manager.py` is **NOT** in scope, but it is the production dependency the test fix follows. The production change was reviewed for blast radius only (not for acceptance):

The +21/-70 diff in `complete()` collapses the inline notification gate (try/except + persistence + event emit + structured log, ~70 lines) into a single call:
```python
notif = self._notify_terminal(task_id, "completed")
```

`_notify_terminal` (introduced in commit `6e5d424` "feat(runtime): guarantee terminal outcome notifications", already at HEAD) already encapsulates:
- calls `notify_terminal_with_fallback(task_id, status)` (generalized gate)
- persists `notification_json` into `task_outputs` (idempotent UPDATE-or-INSERT)
- emits `EventKind.NOTIFICATION_COMPLETED` / `NOTIFICATION_PENDING` / `NOTIFICATION_FAILED`
- never raises (3 nested try/except for gate / persist / event-emit)
- returns the notif dict so the blocking gate below can inspect `sent` / `message_id`

**Production change is a refactor, not a behaviour change** — `complete()` already emitted the same events and wrote the same `notification_json` column; it now routes through the shared helper that `fail()`/`timeout()`/`cancel()` already use. The blocking gate (revert + raise `NotificationBlocked` when `enforcement_gate.blocking == true` AND notif did not confirm) is unchanged and still reads `notif` from the call site.

**Risk to WO scope:** the test fix is the logical consequence of this production refactor — the patch target moves from `notify_completed_with_fallback` (the legacy alias) to `notify_terminal_with_fallback` (the generalized symbol that `_notify_terminal` actually calls). Without the test fix, the stubs would patch a function that `complete()` no longer calls, leaving the real `notify_terminal_with_fallback` unpatched → real `hermes send` subprocess fires in test runs.

### 3.2 tests/test_aee_v3_telegram_gate.py (+13/-6, out of scope)

Single test method update: `test_complete_with_gateway_exception_does_not_raise` moves its `mock.patch` target from `dispatcher.notifier.notify_completed_with_fallback` → `dispatcher.notifier.notify_terminal_with_fallback`. Docstring updated to explain the unification. Consistent with the in-scope fix.

### 3.3 dispatcher/notifier.py — UNCHANGED

sha256(HEAD) = sha256(working tree) = `5a985c2a61601d03fa77296acfbd7b0d248e74ff1824a34d84c8a0eb8cafc20d`. ✅ No review risk.

---

## 4. In-Scope File 1: tests/test_run_task_mapping.py

### 4.1 Changes

**Diffstat:** +209/-3 (1106 → 1311 lines)

**Three change clusters:**

1. **`TestFixDLifecycleSync.test_complete_syncs_executor_runs`** (line ~537):
   - Replaces `notify_completed_with_fallback` stub with `notify_terminal_with_fallback` stub.
   - Lambda signature updated to `lambda task_id, status, **kw: {...}` (the generalized gate takes `status`).
   - Adds explanatory comment describing the unification.

2. **`TestFixDLifecycleSync.test_fail_syncs_executor_runs`** (line ~567) and **`test_timeout_syncs_executor_runs`** (line ~629):
   - **NEW** stub blocks for `notify_terminal_with_fallback`. Previously these tests had NO notifier stub at all — `mgr.fail()` / `mgr.timeout()` would invoke `_notify_terminal` → `notify_terminal_with_fallback` → `notify_terminal_hermes_gateway` → `subprocess.run(["hermes", "send", ...])` with the production `TELEGRAM_CHAT_ID` (injected into `os.environ` by `app.py:load_dotenv()` at import time).
   - Detailed comments explain: `AEE_NOTIFY_DISABLED=1` is decorative (no production consumer), the real isolation is the `notify_terminal_with_fallback` stub.
   - This is the **incident root-cause fix**: fail/timeout tests were silently firing real Telegram messages to the production chat.

3. **NEW class `TestNotificationIsolationRegression`** (line ~681, ~160 lines):
   - 3 test methods: `test_fail_does_not_invoke_subprocess`, `test_timeout_does_not_invoke_subprocess`, `test_complete_does_not_invoke_subprocess`.
   - Shared `_setup_lifecycle_env` helper installs:
     - temp DB + bridge key + `CLIENT_BRIDGE_KEYS` injection
     - `notify_terminal_with_fallback` stub (returns `{"sent": True, "method": "stub", "message_id": 1, "recipient": "test"}`)
     - **fail-on-call `subprocess.run` sentinel** scoped to `argv[0] == "hermes" and argv[1] == "send"` — raises `AssertionError` if the `hermes send` path fires; non-`hermes-send` calls (e.g. `git rev-parse HEAD` for git-evidence capture in `mgr.create()`) fall through to the real `subprocess.run`.
     - returns `(mgr, task_id, calls)` where `calls` is the list of `hermes send` argvs captured by the sentinel.
   - Each test asserts `calls == []` after the lifecycle transition.

### 4.2 Correctness

- Patch target is correct: `dispatcher.notifier.notify_terminal_with_fallback` is the symbol `_notify_terminal` actually imports (verified at `dispatcher/manager.py:1608`).
- Lambda signature `(task_id, status, **kw)` matches `notify_terminal_with_fallback(task_id, status, chat_id=...)` (verified at `dispatcher/notifier.py:576`).
- Sentinel scoping (`argv[0] == "hermes" and argv[1] == "send"`) correctly distinguishes the notification subprocess from `git rev-parse` and other legitimate subprocess calls. The comment explicitly explains the rationale.
- The regression class is **durable proof**: any future edit that removes the `notify_terminal_with_fallback` stub will trip the sentinel BEFORE a real Telegram message can fire.

### 4.3 Concerns

- **None blocking.** The `AEE_NOTIFY_DISABLED=1` env-var setting is decorative (acknowledged in comments — no production consumer exists, verified by `grep -rn AEE_NOTIFY_DISABLED --include="*.py"` returning only test files). It does not block the gate. It is harmless cruft but not a bug.

---

## 5. In-Scope File 2: tests/test_aee_v3_blocking_gate.py

### 5.1 Changes

**Diffstat:** +20/-8

**Two change clusters, both in `TestBlockingGateFailureRegression`:**

1. **`test_fail_does_not_raise_notification_blocked`** (line ~352):
   - Wraps `m.fail(...)` in `with mock.patch("dispatcher.notifier.notify_terminal_with_fallback", return_value={"sent": True, "message_id": 9999})`.
   - Comment: "Notification path MUST be stubbed so no real hermes send fires (same isolation gap as test_run_task_mapping.py)."

2. **`test_timeout_does_not_raise_notification_blocked`** (line ~371):
   - Same stub pattern for `m.timeout(...)`.

### 5.2 Correctness

- Both tests previously had **no notifier stub** — `fail()` / `timeout()` would fire real `hermes send`. The fix matches the WO's "same isolation gap as test_run_task_mapping.py" framing.
- Stub return value `{"sent": True, "message_id": 9999}` is sufficient to satisfy the blocking-gate inspection (`notif.get("sent") and notif.get("message_id") is not None`) so the test still verifies "fail() must NOT raise `NotificationBlocked`".
- The `with mock.patch(...)` context manager pattern (vs `monkeypatch.setattr`) is consistent with the rest of `test_aee_v3_blocking_gate.py` which is a `unittest.TestCase` (no pytest `monkeypatch` fixture).

### 5.3 Concerns

- **None.**

---

## 6. Verification Criterion 1 — Production Code Unchanged

**Result: N/A in-scope, PASS overall**

- The WO scope is the two test files. No production file is in scope.
- `dispatcher/manager.py` IS modified (+21/-70) but is **out of WO scope**. The modification is a refactor (inline gate → `_notify_terminal` call) that the test fix follows. Reviewed in §3.1 — refactor, not behaviour change.
- `dispatcher/notifier.py` UNCHANGED (sha256 match). ✅

**Verdict on Criterion 1:** The test-only scope itself touches zero production code. The adjacent production refactor is a separate concern and should be committed in its own atomic commit (see §10).

---

## 7. Verification Criterion 2 — Notification Path Fully Mocked/Stubbed

**Result: PASS**

All fail/timeout/complete test paths that previously reached `subprocess.run(["hermes", "send", ...])` now stub `notify_terminal_with_fallback` at the `dispatcher.notifier` module boundary, which is the exact symbol `_notify_terminal` imports (`from dispatcher.notifier import notify_terminal_with_fallback` at `dispatcher/manager.py:1608`). The stub returns a fixed dict and never reaches `notify_terminal_hermes_gateway` (the subprocess caller).

**Files / methods verified:**

| File | Test method | Stub target | Stub shape |
|------|-------------|-------------|------------|
| test_run_task_mapping.py | test_complete_syncs_executor_runs | notify_terminal_with_fallback | lambda (task_id, status, **kw) → dict |
| test_run_task_mapping.py | test_fail_syncs_executor_runs | notify_terminal_with_fallback | lambda (task_id, status, **kw) → dict |
| test_run_task_mapping.py | test_timeout_syncs_executor_runs | notify_terminal_with_fallback | lambda (task_id, status, **kw) → dict |
| test_run_task_mapping.py | TestNotificationIsolationRegression._setup_lifecycle_env | notify_terminal_with_fallback | lambda (task_id, status, **kw) → dict |
| test_aee_v3_blocking_gate.py | test_fail_does_not_raise_notification_blocked | notify_terminal_with_fallback | return_value={"sent": True, "message_id": 9999} |
| test_aee_v3_blocking_gate.py | test_timeout_does_not_raise_notification_blocked | notify_terminal_with_fallback | return_value={"sent": True, "message_id": 9999} |

---

## 8. Verification Criterion 3 — Regression Proves Zero Real hermes send / subprocess Calls

**Result: PASS**

`TestNotificationIsolationRegression` (3 tests) installs a fail-on-call sentinel on `subprocess.run` scoped to the `hermes send` argv shape:
```python
def _sentinel(argv, *args, **kwargs):
    if argv and len(argv) >= 2 and argv[0] == "hermes" and argv[1] == "send":
        calls.append(list(argv))
        raise AssertionError(...)
    return _real_run(argv, *args, **kwargs)
```
Each test asserts `calls == []` after the lifecycle transition. This is **durable proof**: if a future edit removes the `notify_terminal_with_fallback` stub, the sentinel fires `AssertionError` BEFORE the real `hermes send` reaches the network.

**Test run evidence:**
```
$ python3 -m pytest tests/test_run_task_mapping.py::TestNotificationIsolationRegression -v
3 passed
```

Sentinel scoping is correct — non-`hermes-send` subprocess calls (e.g. `git rev-parse HEAD` invoked by `mgr.create()` for git-evidence capture) fall through to the real `subprocess.run` so the lifecycle transition can complete. The comment explicitly documents this rationale.

---

## 9. Verification Criterion 4 — No Unrelated Test Changes

**Result: PASS (in-scope)**

In-scope files contain only:
- notification-path stub additions
- patch-target migration (`notify_completed_with_fallback` → `notify_terminal_with_fallback`)
- new `TestNotificationIsolationRegression` class (purely additive)
- comment/docstring updates explaining the unification

No assertions changed. No test logic rewritten. No fixtures modified. No imports removed (the legacy `notify_completed_with_fallback` import in `test_aee_v3_blocking_gate.py:59` is retained — it's still used by other tests in that file).

**Out-of-scope caution for the committer:**
- `tests/test_aee_v3_telegram_gate.py` (+13/-6) is the same fix applied to a third file — strongly related but outside the WO's stated scope. The committer should decide whether to include it in the same atomic commit (recommended, since it's the same refactor consequence) or split.
- `dispatcher/manager.py` (+21/-70) is the production refactor — should be a SEPARATE atomic commit before the test fix, OR included as a single "refactor + test follow" commit. Either is defensible; the WO scope says tests-only.
- `.gitignore` is unrelated and should NOT be in this commit.

---

## 10. Verification Criterion 5 — Suitable for Isolated Atomic Commit

**Result: PASS with scope guidance**

**If committing strictly per WO scope (tests/test_run_task_mapping.py + tests/test_aee_v3_blocking_gate.py only):**

⚠️ **CAUTION:** The test fix depends on the production refactor in `dispatcher/manager.py` — `complete()` must call `self._notify_terminal(task_id, "completed")` (not the legacy inline gate) for the `notify_terminal_with_fallback` stub to take effect. Committing the tests without the production change will FAIL at HEAD-before-refactor because `complete()` would still call `notify_completed_with_fallback` directly, bypassing the stub.

**Recommended atomic commit shape (one of):**

A. **Refactor + test fix together** (4 files): `dispatcher/manager.py`, `tests/test_run_task_mapping.py`, `tests/test_aee_v3_blocking_gate.py`, `tests/test_aee_v3_telegram_gate.py`. Single commit message: `refactor(dispatcher): unify complete() notification path + update test stubs to generalized gate`. This is the cleanest atomic unit because the test fix is the logical consequence of the refactor.

B. **Production refactor first, test fix second** (2 commits):
   1. `refactor(dispatcher): route complete() notification through _notify_terminal` (1 file: `dispatcher/manager.py`)
   2. `test: stub notify_terminal_with_fallback in fail/timeout/complete lifecycle tests` (3 files: the two in-scope + `test_aee_v3_telegram_gate.py`)

Option A is preferred — the refactor and its test fix are inseparable.

**Files to EXCLUDE from any commit:** `.gitignore`, all untracked reports/scripts/requirements files.

**Pre-commit gates (recommended):**
- `python3 -m pytest tests/test_run_task_mapping.py tests/test_aee_v3_blocking_gate.py tests/test_aee_v3_telegram_gate.py tests/test_guaranteed_completion_notification.py -q` → 102 passed (verified this review)
- `git diff --stat` should show only the 4 in-scope files (or 1 production + 3 test, depending on option chosen)

---

## 11. Test Evidence

### 11.1 In-scope test files
```
$ python3 -m pytest tests/test_run_task_mapping.py tests/test_aee_v3_blocking_gate.py -q
47 passed
```

### 11.2 Regression class (durable proof)
```
$ python3 -m pytest tests/test_run_task_mapping.py::TestNotificationIsolationRegression -v
3 passed
```

### 11.3 Blocking-gate failure-regression
```
$ python3 -m pytest tests/test_aee_v3_blocking_gate.py::TestBlockingGateFailureRegression -v
2 passed
```

### 11.4 Adjacent / dependent test suites (no regressions)
```
$ python3 -m pytest tests/test_aee_v3_telegram_gate.py tests/test_guaranteed_completion_notification.py -q
55 passed
```

### 11.5 Combined in-scope + dependent
```
$ python3 -m pytest tests/test_run_task_mapping.py tests/test_aee_v3_blocking_gate.py tests/test_aee_v3_telegram_gate.py tests/test_guaranteed_completion_notification.py -q
102 passed, 1 warning (StarletteDeprecationWarning — pre-existing, unrelated)
```

### 11.6 Full-suite caveat
Full `tests/` collection fails on `tests/test_openapi_executor_metadata.py` (ModuleNotFoundError: `yaml`) — **pre-existing environment gap, unrelated to this review**. A subsequent collection error in `test_phase2.py` (anyio/httpx) is also pre-existing. Neither file is in scope.

---

## 12. Review-Ready

**YES.**

This review is read-only. No source, tests, commits, pushes, staging, stashing, resets, or cleans were performed. The working tree was inspected via `git diff`, `git show`, `git status`, `grep`, `sed`, and `python3 -m pytest` only.

---

## 13. Commit-Ready

**YES — with scope guidance in §10.**

Recommended commit shape: **Option A** (refactor + test fix as one atomic commit, 4 files).

Pre-commit verification recipe:
1. `git diff --stat` — confirm only the 4 in-scope files staged
2. `python3 -m pytest tests/test_run_task_mapping.py tests/test_aee_v3_blocking_gate.py tests/test_aee_v3_telegram_gate.py tests/test_guaranteed_completion_notification.py -q` — expect 102 passed
3. `python3 -m pytest tests/test_run_task_mapping.py::TestNotificationIsolationRegression -v` — expect 3 passed (durable proof)
4. Commit message: `refactor(dispatcher): unify complete() notification path + update test stubs to generalized gate`

---

## 14. Telegram Attempt

Per the WO's read-only constraint and Hermes M2 operating rules, no `hermes send` was attempted during this review (the review is read-only; firing Telegram from a review session risks the same kind of notification-path side-effect the WO is auditing the tests for). The orchestrator (Dingde ChatGPT) may notify 鼎鼎 of review completion via its own channel, or request M2 send a notification in a separate turn.

---

## 15. File Hashes (audit trail)

```
tests/test_run_task_mapping.py      sha256 = 2e1fc2206e04deeda731519e71afe7f150eaec41a3581663108fdf3568734b76
tests/test_aee_v3_blocking_gate.py  sha256 = 132a02dc6dc2e519e3207079072565890ebd3f09b2bffd336da4987bcf110fff
dispatcher/manager.py               sha256 = c047c7bc41b15cb1fe1322306304bea350311fe2899955eda0e031ffda3165a2  (modified, +21/-70)
dispatcher/notifier.py              sha256 = 5a985c2a61601d03fa77296acfbd7b0d248e74ff1824a34d84c8a0eb8cafc20d  (UNCHANGED vs HEAD)
```

---

**Review complete.**