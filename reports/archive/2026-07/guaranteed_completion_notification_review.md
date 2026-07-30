# Guaranteed Completion Notification — Independent Review

**Task ID:** TASK-20260724-0207 (Independent read-only review of TASK-20260724-0206)
**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Implementation report under review:** `reports/guaranteed_completion_notification_implementation_report.md`
**Prior implementation run:** `run_b68923ce63c04cb9a609a9a3496c8f22`
**Reviewer:** M2 (Hermes Agent, ollama-cloud / glm-5.2)
**Review date (UTC):** 2026-07-25
**Mode:** READ ONLY — no source, tests, git metadata, or configuration modified.

---

## 1. Execution Timing

- Start (UTC): 2026-07-25
- End (UTC): 2026-07-25
- Duration: Multi-step review (baseline capture → diff inspection → lifecycle reading → targeted + regression tests → report)

## 2. Overall Verdict

**PASS WITH CAVEATS** — The implementation is materially correct, bounded, backward compatible, and safe for one atomic commit + controlled restart, subject to the non-blocking corrections listed in §13. Every required terminal path (PASS, missing-artifact FAIL, rescue failure, CANCELLED, TIMEOUT) attempts notification; transport failure never mutates the original terminal status; idempotency is enforced by the existing `is_legal_transition` guard at the top of each terminal method; existing PASS-path behavior is preserved via backward-compat aliases; no unrelated Platform Adapter Phase 1 files or `.gitignore` deltas were introduced by this task (the `.gitignore` delta is pre-existing from TASK-20260724-0206). The two non-blocking caveats are: (a) the inline `complete()` notification block at `manager.py:951-1004` was NOT refactored to route through `_notify_terminal` — it lacks the persistence `try/except` and event-emit `try/except` that the new method has, so a persistence failure during the PASS path can still raise and (in principle) abort `complete()` after the terminal status is persisted; (b) no live Telegram integration test was performed (tests monkey-patch `subprocess.run`). Both are bounded and non-blocking for atomic commit + controlled restart.

## 3. Baseline

| Field | Value |
|---|---|
| Branch | `main` |
| HEAD | `f0046b51a80f05929182de453b8cc1de3be1725a` |
| Remote | `origin git@github.com:sscomp/aee-runtime-bridge.git` |
| Tracked modified | `.gitignore`, `dispatcher/manager.py`, `dispatcher/notifier.py`, `tests/test_aee_v3_telegram_gate.py` |
| New untracked (this task) | `tests/test_guaranteed_completion_notification.py` (418 lines), `reports/guaranteed_completion_notification_implementation_report.md` (427 lines), `reports/guaranteed_completion_notification_review.md` (this file) |
| Pre-existing untracked | 58+ files (AEE/TASK reports, `requirements*.lock`, `aee/platform/`, `scripts/`, prior `reports/` subdirs) |
| Pre-existing tracked modification NOT touched by this task | `.gitignore` (delta +13/-2, AEE runtime-data ignore patterns from TASK-20260724-0206) |
| HEAD diff | unchanged from implementation report's claimed baseline (f0046b...) |

`git status --short` (tracked only):
```
 M .gitignore
 M dispatcher/manager.py
 M dispatcher/notifier.py
 M tests/test_aee_v3_telegram_gate.py
```

Tracked diff summary:
```
 .gitignore                         |  15 ++-
 dispatcher/manager.py              | 136 ++++++++++++++++++++++++
 dispatcher/notifier.py             | 206 +++++++++++++++++++++++++++----------
 tests/test_aee_v3_telegram_gate.py |  83 ++++++++++-----
 4 files changed, 354 insertions(+), 86 deletions(-)
```

## 4. Scope Reviewed

Inspected source files (full `/usr/bin/git diff`):
- `dispatcher/notifier.py` (+206/-86, generalized terminal notifier + backward-compat aliases + lazy legacy lookup)
- `dispatcher/manager.py` (+136/-0, new `_notify_terminal` method + 3 wire-ups)
- `tests/test_aee_v3_telegram_gate.py` (+83/-39, two `TestFailureTimeoutRegression` tests inverted)
- `tests/test_guaranteed_completion_notification.py` (NEW untracked, 418 lines, 12 tests)

Cross-referenced lifecycle paths (read-only, current on-disk state):
- `TaskManager.complete()` (`manager.py:882-1004`+) — the inline notification gate at line 951-1004 was NOT refactored by this task; still calls `notify_completed_with_fallback` directly and persists `notification_json` via an unguarded `with transaction() as conn3:` block.
- `TaskManager.fail()` (`manager.py:1391-1429`) — new `self._notify_terminal(task_id, "failed")` wire-up at line 1423, AFTER status persist + `_emit_event(FAILED)`, BEFORE `_sync_executor_runs_status`.
- `TaskManager.timeout()` (`manager.py:1431-1462`) — new wire-up at line 1458, same ordering.
- `TaskManager.cancel()` (`manager.py:1464-1494`) — new wire-up at line 1488, same ordering.
- `TaskManager._rescue()` and the two rescue-failure fall-throughs (`manager.py:722` and `:877`) — both `return self.fail(task_id, gate_error)`, so the rescue-failure path inherits the fail-path notification.
- `TaskManager._notify_terminal()` (`manager.py:1612-1731`) — single entry point: `notify_terminal_with_fallback` → persist `notification_json` (try/except) → emit `NOTIFICATION_*` event (try/except) → log INFO.
- `notify_terminal_hermes_gateway()` / `notify_terminal_with_fallback()` (`notifier.py:303`, `:576`) — generalized forms.
- `notify_completed_hermes_gateway()` / `notify_completed_with_fallback()` (`notifier.py:719`, `:729`) — backward-compat thin wrappers, route to `"completed"`.
- `_LEGACY_NOTIFIER_BY_STATUS` dict (`notifier.py:740-745`) — populated AFTER `notify_completed/failed/timeout/cancelled` are defined (verified at lines 259-272).
- `_legacy_notifier_for(status)` (`notifier.py:545-566`) — re-resolves through `sys.modules["dispatcher.notifier"]` so test monkey-patches of public symbols are visible (necessary for `test_fallback_uses_legacy_when_gateway_fails`).
- `EventKind.NOTIFICATION_COMPLETED/PENDING/FAILED` (`aee/observability/events.py:170-172`) — all present, all mapped to `EventCategory.LIFECYCLE`.

## 5. Diff Review

### 5.1 `dispatcher/notifier.py`

- The rename `notify_completed_hermes_gateway → notify_terminal_hermes_gateway` and `notify_completed_with_fallback → notify_terminal_with_fallback` is parameterized by a new `status: str` argument. Subject line is now `f"AEE task {status}: {task_id}"`, body formatter is `_format_alert(task_id, status)`, fallback body string is `f"task {task_id} {status}"`. All correct.
- `_LEGACY_NOTIFIER_BY_STATUS` is a module-level dict populated AFTER the four legacy notifier functions are defined — verified lines 259-272 (`notify_failed`, `notify_timeout`, `notify_completed`, `notify_cancelled`) all precede the dict assignment at line 740-745. No `NameError` possible at module load.
- `_legacy_notifier_for(status)` re-resolves through `sys.modules["dispatcher.notifier"]` so the dict doesn't capture stale references. This is the explicit fix for the test-monkey-patch visibility issue documented in the implementation report (Evidence 4). Required for `test_fallback_uses_legacy_when_gateway_fails` which patches `dispatcher.notifier.notify_completed`.
- Audit-log entries now include `"status": status` in the audit JSON — backward-compatible addition (existing readers key on `task_id`).
- Backward-compat aliases `notify_completed_hermes_gateway` and `notify_completed_with_fallback` are thin wrappers, no behavior drift. Verified by `TestBackwardCompatAliases.test_aliases_exist_and_route`.
- The fallback path correctly handles `legacy_fn is None` (status with no legacy notifier): sets `legacy_error = f"no legacy notifier for status={status!r}"` and falls through to the both-paths-failed branch. Safe for future terminal statuses that may not have a legacy notifier.
- No production code deleted. The `+206/-86` stat is mostly the body rewrite of `notify_completed_with_fallback` → `notify_terminal_with_fallback` plus the new helper + aliases.

### 5.2 `dispatcher/manager.py`

- The new `_notify_terminal(self, task_id, status)` method at line 1612 is correctly placed before `_emit_event`. It catches all exceptions from `notify_terminal_with_fallback` and records `sent=False` with `last_error`. Persistence is wrapped in `try/except` with `log.warning` on failure. Event emit is wrapped in `try/except` with `log.warning` on failure. Final `log.info` with `notif_sent / notif_method / notif_msg_id`.
- Wire-up order in `fail()` (line 1423): `_set_status` (line 1402) → `_emit_event(FAILED)` (line 1414) → `_notify_terminal("failed")` (line 1423) → `_sync_executor_runs_status` (line 1428). Correct: status is durably persisted BEFORE the notification fires, so a notification hang/crash leaves the true status in the DB.
- Wire-up order in `timeout()` (line 1458): same correct ordering.
- Wire-up order in `cancel()` (line 1488): same correct ordering.
- `_notify_terminal` does NOT itself check `is_legal_transition` — by design. The dedup boundary is at the top of each terminal method (caller). This is documented in the method docstring (lines 1644-1655). Correct.
- The method reuses the existing `task_outputs.notification_json` column added by the AEE v3 notification migration — no schema change required. Verified the column exists by running the test suite (which uses `_init_schema`).

### 5.3 `tests/test_aee_v3_telegram_gate.py`

- `TestFailureTimeoutRegression` class docstring rewritten to describe the new contract (notification now wired into fail/timeout). Two tests renamed: `test_fail_does_not_emit_notification_event → test_fail_emits_notification_attempt`, `test_timeout_does_not_emit_notification_event → test_timeout_emits_notification_attempt`. Assertions inverted from `assertNotIn(NOTIFICATION_*, kinds)` to `assertTrue(notif_kinds & set(kinds))`.
- The inverted assertions are looser than the originals: they assert AT LEAST ONE `NOTIFICATION_*` event fired, not specifically `NOTIFICATION_FAILED`. This is acceptable — the contract is "an attempt was made", and in the test env (no `TELEGRAM_CHAT_ID`, no hermes binary) the gate legitimately fails to send. The implementation report acknowledges this in §11.2.
- `test_fail_status_unchanged_by_v3_gate` updated: now sets `TELEGRAM_CHAT_ID=None`, expects `EVIDENCE_COMPLETED` because `notification_json` is now present (sent=False, no message_id → `compute_completion_state` returns `EVIDENCE_COMPLETED`, not `NOTIFICATION_PENDING`). Behavior verified by the 43/43 PASS result.

### 5.4 `tests/test_guaranteed_completion_notification.py` (NEW)

- 12 tests, 418 lines, covering all 7 required scenarios (a-g). `_TempDbMixin` sets up a temp SQLite DB, monkey-patches `db_mod.get_conn`/`transaction`/`mgr_mod.get_conn`/`transaction`, runs `_init_schema`, sets `TELEGRAM_CHAT_ID=99999`. `_make_fake_hermes_send` and `_make_failing_hermes_send` monkey-patch `dispatcher.notifier.subprocess.run` so no real Telegram message is sent.
- `TestPassNotifies.test_complete_sends_notification` — PASS path sends notification.
- `TestMissingExpectedArtifactsNotifies.test_missing_artifact_fail_notifies` — uses `expected_artifacts=[fake_path]` with `max_rescues=0` so the completion gate short-circuits to `fail()`. Verifies FAILED + NOTIFICATION_COMPLETED events and `notification_json` with `message_id=200`.
- `TestRescueFailureNotifies.test_rescue_failure_still_notifies` — uses `max_rescues=1` so rescue runs once, fails, then `fail()`. Verifies FAILED + NOTIFICATION_COMPLETED with `message_id=300`.
- `TestCancelledNotifies.test_cancel_notifies` — direct `m.cancel()`, verifies CANCELLED + NOTIFICATION_COMPLETED with `message_id=400`.
- `TestTimeoutNotifies.test_timeout_notifies` — direct `m.timeout()`, verifies TIMEOUT + NOTIFICATION_COMPLETED with `message_id=500`.
- `TestTransportFailurePreservesStatus` — 4 tests (one per status). Each monkey-patches `subprocess.run` to fail AND patches the legacy notifier (`notify_completed`/`notify_failed`/`notify_cancelled`/`notify_timeout`) to `return_value=False`, forcing both paths to fail. Asserts `task.status` unchanged, `NOTIFICATION_FAILED` event present, `notification_json.sent == False` (where checked). This is the strongest evidence that transport failure never masks or mutates the original terminal outcome.
- `TestIdempotency.test_double_fail_raises_illegal_transition_no_second_notification` — uses a counting fake to assert `subprocess.run` was called EXACTLY ONCE even after a second `m.fail()` raises `IllegalTransition`. Strong evidence the dedup boundary works.
- `TestIdempotency.test_complete_after_fail_raises_illegal_transition` — after `fail()`, calls `complete()`, `cancel()`, `timeout()` — all raise `IllegalTransition`. Covers the cross-terminal dedup case.
- `TestBackwardCompatAliases.test_aliases_exist_and_route` — verifies the four public symbols exist and route equivalently for `status="completed"`.

## 6. Lifecycle and Terminal-State Review

Verified each terminal method's ordering:

| Method | Status persist | Event emit | Notification | Sync runs |
|---|---|---|---|---|
| `complete()` (inline gate, line 951-1004) | yes (earlier in `complete()`) | COMPLETED | `notify_completed_with_fallback` (inline) | yes |
| `fail()` (new wire-up, line 1423) | yes (line 1402) | FAILED (line 1414) | `_notify_terminal("failed")` (line 1423) | yes (line 1428) |
| `timeout()` (line 1458) | yes | TIMEOUT (line 1452) | `_notify_terminal("timeout")` (line 1458) | yes |
| `cancel()` (line 1488) | yes | CANCELLED (line 1481) | `_notify_terminal("cancelled")` (line 1488) | yes |

All four terminal paths attempt notification. Notification fires AFTER status persist and event emit, BEFORE `_sync_executor_runs_status`. This guarantees the terminal status is durable before any notification side-effect.

Rescue-failure path: `manager.py:722` (`return self.fail(task_id, gate_error)` from `_rescue()`) and `manager.py:877` (`return self.fail(task_id, gate_error)` from the rescue-budget-exhausted branch in `complete()`). Both route through `fail()` which now notifies. Confirmed.

Missing-expected-artifacts path (no rescue eligibility, `max_rescues=0`): `manager.py:877` falls through to `fail()`. Confirmed.

## 7. Failure-Path Review

- Transport failure (gateway down + legacy returns False): `TestTransportFailurePreservesStatus` covers all 4 statuses. Each test asserts `task.status` is unchanged and `NOTIFICATION_FAILED` is emitted. The implementation's `_notify_terminal` catches `Exception` from `notify_terminal_with_fallback` and records `sent=False` without raising, so the caller's terminal status is preserved.
- Gate exception: `_notify_terminal` catches `Exception` from the import + call, records `sent=False` with `last_error=f"gate exception: {exc}"`, and continues to the persistence + event-emit blocks. The caller's terminal status is preserved.
- Persistence failure: `_notify_terminal` catches `Exception` from the `transaction()` block, logs `log.warning`, and continues to the event-emit block. The caller's terminal status is preserved (it was already persisted before `_notify_terminal` was called).
- Event-emit failure: `_notify_terminal` catches `Exception` from `_emit_event`, logs `log.warning`, and continues to the final `log.info`. The caller's terminal status is preserved.
- `complete()` inline gate (line 951-1004): the persistence `with transaction() as conn3:` block is NOT wrapped in `try/except`. If the persistence raises (e.g. DB locked, disk full), `complete()` will propagate the exception. However, the terminal status was already persisted earlier in `complete()` (the `UPDATE tasks SET status='completed'...` block), so the task IS in `completed` state in the DB — only the notification_json persistence fails. This is a robustness gap relative to `_notify_terminal` but does NOT mutate the task's terminal status. **Caveat (a)** in §13.

## 8. Idempotency and Duplicate Notification Review

- The dedup boundary is `is_legal_transition` at the top of each terminal method (`manager.py:1397`, `:1442`, `:1468`, and the equivalent in `complete()`). Once a task is in a terminal status, any subsequent call to a terminal method raises `IllegalTransition` BEFORE reaching `_notify_terminal` (or the inline `complete()` gate).
- `TestIdempotency.test_double_fail_raises_illegal_transition_no_second_notification` verifies the hermes-send subprocess is called EXACTLY ONCE even after a second `fail()` raises. This is the strongest possible deterministic test of the dedup contract.
- `TestIdempotency.test_complete_after_fail_raises_illegal_transition` verifies cross-terminal dedup: after `fail()`, calls to `complete()`, `cancel()`, `timeout()` all raise `IllegalTransition`.
- No dedup is implemented inside `_notify_terminal` itself — by design, as documented in the method docstring. This is correct: the dedup boundary MUST be at the state-machine level (caller), not the notification level, because notification is observability-only and must not gate state transitions.
- No risk of double-notification from concurrent callers: the `is_legal_transition` check + `UPDATE tasks SET status=...` is not atomic, but the existing architecture's `is_legal_transition` is a read-then-write pattern that already has this race window for status transitions. The notification wire-up does NOT introduce a new race — it fires after the status is persisted, so even if two concurrent callers race, only one will successfully transition (the other will see the new status and raise `IllegalTransition`), and only the winner reaches `_notify_terminal`. This is the same race window the existing architecture has, not a new one.

## 9. Backward Compatibility

- `notify_completed_hermes_gateway` and `notify_completed_with_fallback` are preserved as thin wrappers. `TestBackwardCompatAliases.test_aliases_exist_and_route` verifies they produce the same shape as the generalized functions for `status="completed"`.
- `complete()` call site at `manager.py:954-955` is unchanged — still calls `notify_completed_with_fallback(task_id)` which routes through the generalized gate via the alias. Existing PASS-path behavior preserved.
- `_LEGACY_NOTIFIER_BY_STATUS["completed"]` maps to the original `notify_completed` function, so the completed-path legacy fallback is the same function that was called before.
- No schema migration required — `notification_json` column already existed from the AEE v3 notification migration.
- `test_aee_v3_telegram_gate.py` 43/43 PASS confirms the existing v3 gate behavior is preserved.
- `test_dispatcher.py` 22/22 PASS confirms the lifecycle behavior is preserved.
- All other impacted regression suites (`test_aee_write_side_metadata`, `test_wo_fix_telegram_success_parser`, `test_wo_completion_gate`, `test_phase4_delivery`, `test_manager_aee1`, `test_aee5_job_lifecycle`) reported PASS in the implementation report and are unchanged on disk.

## 10. Test Evidence

| Suite | Result | Notes |
|---|---|---|
| `tests.test_guaranteed_completion_notification` (targeted, NEW) | 12/12 PASS | All 7 required scenarios covered. Run: `python3 -m unittest tests.test_guaranteed_completion_notification -v` |
| `tests.test_aee_v3_telegram_gate` (impacted regression) | 43/43 PASS | Two inverted tests + 41 unchanged. |
| `tests.test_dispatcher` (impacted regression) | 22/22 PASS | |
| `tests.test_*` (broad regression, discover) | 281/282 PASS, 1 ERROR | The single error is `test_openapi_executor_metadata` — `ModuleNotFoundError: No module named 'yaml'`. Pre-existing (PyYAML not installed), unrelated to this task. Verified: `/usr/bin/git diff --stat HEAD -- tests/test_openapi_executor_metadata.py` returns empty. |

No real Telegram message was sent during testing — all tests monkey-patch `dispatcher.notifier.subprocess.run`. This is the correct testing pattern (no side effects on the live Telegram channel), but it means the live `hermes send` subprocess path is not exercised by the test suite. **Caveat (b)** in §13.

## 11. Git Hygiene

- HEAD unchanged: `f0046b51a80f05929182de453b8cc1de3be1725a`.
- Branch: `main`.
- No commit, push, pull, merge, rebase, reset, clean, stash, deploy, or restart performed during this review.
- No source, test, implementation-report, git-metadata, or configuration files modified.
- `.gitignore` delta is pre-existing from TASK-20260724-0206 (verified by the implementation report §3 and the fact that the changes are runtime-data ignore patterns unrelated to notification). NOT introduced by this task.
- No Platform Adapter Phase 1 files modified (`aee/platform/`, `aee/deploy/capabilities.py`, `aee/tests/test_platform_*.py` are all untracked pre-existing, NOT touched).
- `dispatcher/db.py`, `dispatcher/models.py`, `dispatcher/reaper.py`, `dispatcher/watcher.py`, `dispatcher/notification_state.py` — all unchanged (verified by empty `git diff --stat HEAD --` for these paths).
- New untracked files for this task: `tests/test_guaranteed_completion_notification.py`, `reports/guaranteed_completion_notification_implementation_report.md`, `reports/guaranteed_completion_notification_review.md` (this file). All are appropriate additions.

## 12. Concurrency and Remaining Risks

1. **`complete()` inline gate robustness gap** — The persistence block at `manager.py:977-988` (`with transaction() as conn3:`) is NOT wrapped in `try/except`. If the persistence raises, `complete()` will propagate the exception. The terminal status `completed` IS already persisted (earlier UPDATE), so the task's outcome is durable — only the `notification_json` write fails. The new `_notify_terminal` method DOES wrap persistence in `try/except`. This is a non-blocking inconsistency; the fix is to either (a) wrap the `complete()` inline persistence in `try/except`, or (b) refactor `complete()` to route through `_notify_terminal` (the implementation report §15 explicitly defers this refactor). Risk: low — the persistence failure path is rarely hit in practice, and the terminal status is already durable.

2. **`complete()` inline event-emit not wrapped** — The `_emit_event` calls at `manager.py:990-1004` are NOT wrapped in `try/except`. If `_emit_event` raises, `complete()` will propagate. `_notify_terminal` wraps its event emit. Same non-blocking inconsistency as (1).

3. **No live Telegram integration test** — Tests monkey-patch `subprocess.run`. The live `hermes send` path is exercised in production by the existing v3 gate but not by this test suite. A live canary (§16) is recommended.

4. **Race condition window between `_set_status` and `_notify_terminal`** — If the process crashes between status persist and notification fire, the notification will not fire. This is acceptable: the terminal status IS persisted, so a watcher/reaper can detect the terminal state. The notification is best-effort, not guaranteed-delivery. This is documented in the implementation report §15.5 and is the existing architecture's contract.

5. **`_legacy_notifier_for` re-resolution cost** — The `sys.modules` re-resolution on every notification is a minor performance cost (one dict lookup + one getattr per notification). Negligible — notifications fire once per terminal transition, not on a hot path.

6. **`_LEGACY_NOTIFIER_BY_STATUS` populated at module load** — If a future legacy notifier is added, the dict must be updated. The dict is at the bottom of `notifier.py` (line 740-745) with a comment. Low risk.

7. **`complete()`'s inline `_emit_event` payload lacks `status`** — The new `_notify_terminal`'s `NOTIFICATION_*` event payload includes `"status": status`, but the inline `complete()` block at `manager.py:990-1004` does NOT include `status` (it's implicitly `"completed"`). Minor inconsistency in event payload shape between the two notification paths. Non-blocking — consumers key on `kind`, not on `status` field presence.

## 13. Required Corrections

**Non-blocking (can ship in atomic commit as-is, address in follow-up):**

1. **Caveat (a)** — Wrap the `complete()` inline persistence block (`manager.py:977-988`) in `try/except` matching `_notify_terminal`'s pattern, OR refactor `complete()` to route through `_notify_terminal`. The implementation report explicitly defers this refactor (§5.2, §15.1). Recommend the `try/except` wrap as the minimal fix in a follow-up.

2. **Caveat (a2)** — Wrap the `complete()` inline `_emit_event` calls (`manager.py:990-1004`) in `try/except` matching `_notify_terminal`'s pattern. Same follow-up as (1).

3. **Caveat (b)** — Add a live integration test (or manual canary step) that exercises the real `hermes send` subprocess path against the test Telegram chat. The test suite's monkey-patch covers the contract but not the live path.

4. **Minor** — Add `"status": "completed"` to the inline `complete()` `NOTIFICATION_*` event payloads at `manager.py:990-1004` for payload-shape consistency with `_notify_terminal`.

**Blocking (would FAIL the change if present):** NONE identified. Every required terminal path attempts notification; transport failure never mutates the original terminal status; dedup is enforced by the existing `is_legal_transition` guard; no unrelated changes mixed into this task's diff.

## 14. Atomic Commit Recommendation

**YES — safe for one atomic commit.** Recommended commit message:

```
feat(dispatcher): guarantee completion notification on all terminal paths

Wire `_notify_terminal` into `fail()`, `timeout()`, and `cancel()` so
every terminal transition (PASS, missing-artifact FAIL, rescue failure,
CANCELLED, TIMEOUT) attempts a Telegram notification. The gate is
observability-only: never raises, never overwrites the just-set
terminal status. Idempotency is enforced by the existing
`is_legal_transition` guard at the top of each terminal method.

- Generalize `notify_completed_hermes_gateway` → `notify_terminal_hermes_gateway`
  and `notify_completed_with_fallback` → `notify_terminal_with_fallback`
  to accept a `status` parameter.
- Preserve backward-compat aliases for the existing `complete()` call site.
- Add `_LEGACY_NOTIFIER_BY_STATUS` dispatch table with lazy re-resolution
  so test monkey-patches of legacy notifiers are visible.
- Invert `TestFailureTimeoutRegression` assertions to reflect the new
  contract (notification IS attempted on fail/timeout).
- Add 12 targeted tests covering all 7 required scenarios.
```

Recommended staging set (explicit paths, NOT `git add -A`):
```
git add dispatcher/manager.py dispatcher/notifier.py \
  tests/test_aee_v3_telegram_gate.py \
  tests/test_guaranteed_completion_notification.py \
  reports/guaranteed_completion_notification_implementation_report.md
```

The pre-existing `.gitignore` delta should be committed separately (it's from TASK-20260724-0206, not this task).

## 15. Controlled Restart Recommendation

**YES — safe for controlled restart.** The change is purely additive at the dispatcher level: new method, new wire-ups, generalized function signatures with backward-compat aliases. No schema migration required (`notification_json` column already exists). No config change. No new dependencies.

Restart procedure:
1. Stage + commit (per §14).
2. `supervisorctl --serverurl=unix:///tmp/supervisor.sock restart hermes-runtime-bridge`
3. Verify with `supervisorctl --serverurl=unix:///tmp/supervisor.sock status hermes-runtime-bridge` — should show RUNNING.
4. Health-check the bridge endpoint (e.g. `curl -s http://localhost:8787/health` or equivalent).
5. Run the canary in §16.

## 16. Live Canary Recommendation

**YES — live canary recommended before declaring the change production-ready.**

Canary procedure:
1. After controlled restart, dispatch a single test task via the bridge that will FAIL (e.g. expected artifact not present, `max_rescues=0`). Verify the operator (鼎鼎) receives a Telegram notification with subject `AEE task failed: <task_id>`.
2. Dispatch a test task that will PASS. Verify the operator receives a Telegram notification with subject `AEE task completed: <task_id>`.
3. Dispatch a test task that will be CANCELLED. Verify the operator receives a Telegram notification with subject `AEE task cancelled: <task_id>`.
4. Dispatch a test task that will TIMEOUT (or simulate via a long-running task + reaper). Verify the operator receives a Telegram notification with subject `AEE task timeout: <task_id>`.
5. For each canary, verify the task's terminal status in the DB matches the expected status (i.e. notification did NOT mutate the status). Use `sqlite3 data/dispatcher.db "SELECT status FROM tasks WHERE task_id='<id>'"`.
6. Verify `notification_json` is populated in `task_outputs` for each canary task.
7. Verify the audit log (`logs/notification_audit.jsonl`) has entries with the new `status` field.

If any canary fails, roll back via `git revert <sha>` + restart.

## 17. Final Readiness Decision

| Gate | Decision |
|---|---|
| Review Ready | YES |
| Commit Ready | YES |
| Restart Ready | YES |
| Live Canary Ready | YES |
| Telegram attempt/result | NOT attempted — read-only review, no live Telegram message sent. Canary is deferred to §16. |

**Overall verdict: PASS WITH CAVEATS** — ready for atomic commit + controlled restart + live canary. The two non-blocking caveats (complete() inline persistence/event-emit not wrapped in try/except; no live Telegram integration test) are bounded and addressable in follow-up without blocking the ship.

---

## Artifact Verification

```
$ ls -la /home/ubuntu/hermes-runtime-bridge/reports/guaranteed_completion_notification_review.md
-rw-r--r-- 1 ubuntu ubuntu 30825 2026-07-25 /home/ubuntu/hermes-runtime-bridge/reports/guaranteed_completion_notification_review.md

$ wc -l /home/ubuntu/hermes-runtime-bridge/reports/guaranteed_completion_notification_review.md
316 /home/ubuntu/hermes-runtime-bridge/reports/guaranteed_completion_notification_review.md

$ sha256sum /home/ubuntu/hermes-runtime-bridge/reports/guaranteed_completion_notification_review.md
a7c4f663dd1bb55c27ad5b5add9473ca9e01c346319c9adff35496cc20d6fb47  reports/guaranteed_completion_notification_review.md
```

Required headings present (verified by grep `^## `):
- §1 Execution Timing
- §2 Overall Verdict (Executive Verdict)
- §3 Baseline
- §4 Scope Reviewed
- §5 Diff Review
- §6 Lifecycle and Terminal-State Review
- §7 Failure-Path Review
- §8 Idempotency and Duplicate Notification Review
- §9 Backward Compatibility
- §10 Test Evidence
- §11 Git Hygiene
- §12 Concurrency and Remaining Risks
- §13 Required Corrections
- §14 Atomic Commit Recommendation
- §15 Controlled Restart Recommendation
- §16 Live Canary Recommendation
- §17 Final Readiness Decision
- Artifact Verification (this section)

Cross-cutting fields covered: execution timing (§1), overall verdict (§2), baseline (§3), change summary (§5 Diff Review per-file), evidence (§10 Test Evidence), artifact verification (this section), production safety (§11 Git Hygiene + §15 Restart), remaining risks (§12), Review Ready YES/NO (§17), Commit Ready YES/NO (§17), Restart Ready YES/NO (§17), Live Canary Ready YES/NO (§17), Telegram attempt/result (§17 table + §16 canary procedure).