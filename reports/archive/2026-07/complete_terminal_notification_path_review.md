# Independent Review — `complete()` Terminal Notification Path Unification

**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**Expected HEAD:** `30ef534fa6dde4f3289857f7802b8b607e4a3251`
**Review Mode:** READ-ONLY (no source modification, commit, push, restart, stash, reset, clean, or stage)
**Reviewer:** M2 (Hermes Agent)
**Review Date:** 2026-07-25
**Scope Files:**
- `dispatcher/manager.py`
- `tests/test_aee_v3_telegram_gate.py`
- `tests/test_run_task_mapping.py`

---

## 1. Git Evidence

| Field | Value |
|---|---|
| HEAD SHA | `30ef534fa6dde4f3289857f7802b8b607e4a3251` (matches expected) |
| Branch | `main` |
| HEAD commit | `feat(platform): add platform adapter phase 1 — capabilities + bootstrap` (unrelated to this slice) |
| Working tree state | DIRTY (3 in-scope files modified + 1 `.gitignore` modified + many untracked) |
| In-scope diffstat | `dispatcher/manager.py 91 +++--- / tests/test_aee_v3_telegram_gate.py 19 +++--- / tests/test_run_task_mapping.py 11 +++---` |
| Net change | 3 files, +42 / -79 (net −37 lines) |
| Full diff (in-scope, added+removed non-header lines) | 121 lines |
| Staging state | UNSTAGED — no `git add` performed in this review |

The change is **uncommitted working-tree modifications** on top of `30ef534`. The diff is the minimal slice under review; no production files outside the 3 declared are touched.

---

## 2. Change Summary

### `dispatcher/manager.py` (lines 943–963)

The 50-line inline notification block inside `complete()` was replaced with a single call:

```python
notif = self._notify_terminal(task_id, "completed")
```

The removed inline block previously:
1. `from dispatcher.notifier import notify_completed_with_fallback` and called it
2. Wrapped call in try/except to record `gate exception` notifications
3. Performed an idempotent `INSERT`-or-`UPDATE` on `task_outputs.notification_json`
4. Emitted `NOTIFICATION_COMPLETED` / `NOTIFICATION_PENDING` / `NOTIFICATION_FAILED` events
5. Logged the gate's outcome via `log.info("manager.complete: notification gate ...")`

All five responsibilities are now delegated to `TaskManager._notify_terminal(task_id, status)` (defined at line 1563), which is the **same** centralized path used by `fail()` (line 1374), `timeout()` (line 1409), and `cancel()` (line 1439). The downstream blocking-gate logic (lines 964–1042) that reads `notif.get("sent")` / `notif.get("message_id")` and conditionally raises `NotificationBlocked` is **unchanged** — it now consumes the dict returned by `_notify_terminal` instead of the inline `notif` local.

### `tests/test_aee_v3_telegram_gate.py`

- `test_complete_with_gateway_exception_does_not_raise` (line 741): patch target moved from `dispatcher.notifier.notify_completed_with_fallback` → `dispatcher.notifier.notify_terminal_with_fallback`. Docstring updated to reflect the unified path. The assertion contract (`task.status == "completed"`, `notif["sent"] == False`, `"gate exception" in notif["last_error"]`) is preserved.
- No other test in the file changed.

### `tests/test_run_task_mapping.py`

- `TestFixDLifecycleSync` (line 537): the notifier stub patched `notify_completed_with_fallback` and now patches `notify_terminal_with_fallback` with a lambda accepting `(task_id, status, **kw)` to match the generalized signature. Comment updated. The stub's return shape (`sent=True, method=stub, message_id=1, recipient=test`) is preserved.

---

## 3. Verification of Review Criteria

### 3.1 `complete()` now consistently routes through `_notify_terminal()`
**PASS.** Line 963: `notif = self._notify_terminal(task_id, "completed")`. The four terminal finalization paths (`complete`, `fail`, `timeout`, `cancel`) all call the same method with their respective status string. The legacy inline block in `complete()` is fully removed (verified by diff: 50 lines deleted, 1 line added in its place).

### 3.2 PASS/FAIL/CANCELLED/TIMEOUT semantics preserved
**PASS.**
- `complete()` → `_notify_terminal(task_id, "completed")` (line 963)
- `fail()` → `_notify_terminal(task_id, "failed")` (line 1374) — unchanged
- `timeout()` → `_notify_terminal(task_id, "timeout")` (line 1409) — unchanged
- `cancel()` → `_notify_terminal(task_id, "cancelled")` (line 1439) — unchanged
- `notify_terminal_with_fallback` dispatches to `notify_terminal_hermes_gateway` first, then falls back to `_LEGACY_NOTIFIER_BY_STATUS[status]` (`notify_completed` / `notify_failed` / `notify_timeout` / `notify_cancelled`). The legacy backward-compat aliases `notify_completed_with_fallback` and `notify_completed_hermes_gateway` remain in `dispatcher/notifier.py` as thin wrappers (lines 719, 729) — public API surface preserved.

### 3.3 `missing_expected_artifacts` and rescue notification behavior unchanged
**PASS.** The `missing_expected_artifacts` hard gate (lines 759–877) and the rescue loop (`_rescue`, lines 590–722) are **outside the diff** — verified by reading lines 560–722 and 790–877 in the working-tree file; they are byte-identical to HEAD (the diff hunk starts at line 943). The rescue path calls `self.complete()` and `self.fail()` recursively; both now flow through `_notify_terminal` uniformly, which means a rescued task that reaches `completed` will fire exactly one `completed` notification (via the recursive `complete()` call) and the earlier `incomplete_delivery` transition fires none — this matches the pre-slice behavior (the inline `complete()` block was the only notification producer, and `_rescue` does not notify on its own).

### 3.4 Telegram failure cannot override task outcome
**PASS.** `_notify_terminal` (lines 1607–1681) wraps the entire notification path in three nested try/except blocks (gate call / persistence / event emit), each with `# noqa: BLE001 — never raise` and a `log.warning` continuation. The method returns `notif` (always a dict) and **never raises**. In `complete()`, the `status='completed'` SQL UPDATE (lines 899–905) is committed **before** `_notify_terminal` is called (line 963), so a notification failure cannot roll back the terminal status. The blocking-gate revert (lines 1011–1042) only fires when `enforcement_gate.blocking == true` AND `notif_confirmed == false`, and that revert path is unchanged by this slice.

### 3.5 Idempotency / deduplication preserved
**PASS.** Deduplication is enforced by `is_legal_transition(row["status"], "completed")` at line 741, which raises `IllegalTransition` before `_notify_terminal` is reached. The `_notify_terminal` docstring (lines 1593–1603) explicitly documents that the `is_legal_transition` guard at the top of each terminal method is the dedup boundary. The persistence layer in `_notify_terminal` uses an idempotent `SELECT 1 / INSERT / UPDATE` pattern (lines 1625–1638) mirroring the removed inline code, so a second call (e.g. via rescue re-entry) overwrites `notification_json` in place without clobbering `output_text`/`usage_json`/`raw_json`/`delivery_json` (the `UPDATE` targets only the `notification_json` column).

### 3.6 Exception isolation is consistent
**PASS** with one minor caveat (see §4 Caveat A). `_notify_terminal` isolates three exception sources independently: (1) the gate call (line 1611), (2) the persistence write (line 1639), (3) the event emit (line 1667). Each is logged with a distinct `_notify_terminal:` prefix and the method returns. This is **stricter** isolation than the removed inline code, which wrapped the gate call and persistence together in a single try/except and let a persistence failure propagate (the inline `with transaction() as conn3:` at the old line had no surrounding try/except). The unified path therefore improves isolation for `complete()`.

### 3.7 Minimal scope only
**PASS.** Diffstat: 3 files, +42 / -79. The only production change is the 50-line block in `complete()` collapsing to 1 line + comments. The two test files update patch targets and docstrings to match. No other production code, no config, no schema, no `jobs.json`, no other tests. No scope creep into the blocking-gate logic, the rescue path, the notifier implementation, or the legacy aliases.

### 3.8 Suitable for isolated atomic commit
**PASS.** The 3-file change is self-contained, all tests pass (§5), the diff is purely a refactor-to-shared-helper with no behavioral contract change, and the commit message can be `refactor(dispatcher): unify complete() terminal notification path`. No cross-file dependencies outside the 3 declared files. The untracked files in the working tree (40+ `.md` reports, `reports/`, `scripts/`, `requirements*.lock`) are unrelated residue from prior work and must NOT be staged with this slice — stage by explicit path list (`git add dispatcher/manager.py tests/test_aee_v3_telegram_gate.py tests/test_run_task_mapping.py`), never `git add -A`.

---

## 4. Caveats

### Caveat A — Additive `status` field in `NOTIFICATION_*` event payload (non-blocking)

The removed inline `complete()` code emitted `NOTIFICATION_COMPLETED` / `NOTIFICATION_PENDING` / `NOTIFICATION_FAILED` events **without** a `status` key in the payload. The unified `_notify_terminal` adds `"status": status` to every event payload (lines 1648, 1657, 1663). For the `completed` transition this is a **new** payload field; for `failed` / `timeout` / `cancelled` it was already present (those paths already used `_notify_terminal`).

This is **additive** (no existing payload key removed), so no test that asserts on a strict-equality payload dict will break — and indeed all 73 tests pass. Any downstream consumer that pattern-matches on the exact payload key set for `completed`-transition `NOTIFICATION_*` events would see a new key. No such consumer exists in the reviewed test files. Flagging for transparency; not a blocker.

### Caveat B — `.gitignore` modified outside the declared scope

`git status --short` shows `.gitignore` as modified (` M .gitignore`). This is **outside** the 3 declared scope files and is not part of this review. The commit must stage only the 3 declared files by explicit path; the `.gitignore` change should be reviewed/committed separately or left unstaged. Flagging so the committer does not accidentally include it.

---

## 5. Test Evidence

```
$ /usr/bin/python3 -m pytest tests/test_aee_v3_telegram_gate.py tests/test_run_task_mapping.py -q
........................................................................ [ 98%]
.                                                                        [100%]
73 passed, 1 warning in 9.76s
```

Per-file:
- `tests/test_aee_v3_telegram_gate.py` — 43 passed
- `tests/test_run_task_mapping.py` — 30 passed
- Combined — 73 / 73 PASS, 0 failures, 0 errors, 1 warning (unrelated `httpx`/`starlette` deprecation)

The 1 warning is `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` in `tests/test_run_task_mapping.py::TestFixAHermesRunMapping::test_post_runs_creates_executor_runs_row` — pre-existing, unrelated to this slice.

No regressions detected in the two in-scope test files.

---

## 6. Verdict

**PASS WITH CAVEATS**

The minimal implementation correctly unifies `complete()` to the centralized `_notify_terminal()` path. All 8 review criteria are satisfied. The 73 in-scope tests pass. Two non-blocking caveats (additive `status` payload field, out-of-scope `.gitignore` modification) are documented in §4 and do not block commit.

---

## 7. Review-Ready

**YES.** This review is complete and self-contained. All evidence (git state, diff, test output, source-line references) is captured in this artifact.

---

## 8. Commit-Ready

**YES**, with two staging instructions:
1. Stage by explicit path list: `git add dispatcher/manager.py tests/test_aee_v3_telegram_gate.py tests/test_run_task_mapping.py`
2. Do NOT stage `.gitignore` or any of the 40+ untracked files (separate review/commit).

Suggested commit message:
```
refactor(dispatcher): unify complete() terminal notification path

Replace the inline notification block in complete() with a single call
to _notify_terminal(task_id, "completed"), unifying the terminal-
notification behaviour across complete/fail/timeout/cancel. The
blocking-gate logic, rescue path, and notifier implementation are
unchanged. Tests update patch targets from notify_completed_with_fallback
to notify_terminal_with_fallback to match the generalized entry point.

Tests: 73/73 PASS (test_aee_v3_telegram_gate 43, test_run_task_mapping 30).
```

This review did NOT perform the commit (read-only mode).

---

## 9. Telegram Attempt

No Telegram notification sent. This is a **read-only review** — sending a Telegram message is an external side-effect that requires explicit user authorization, and the review mandate did not include a delivery instruction. Per the established `hermes send` SOP, a separate delivery step (with `--file` pointing at this artifact and `--to telegram:5132341473`) can be triggered once the user authorizes commit + delivery.

---

## Appendix A — Source Line References (working tree)

| Symbol | File:Line |
|---|---|
| `complete()` entry | `dispatcher/manager.py:724` |
| `complete()` notification call (unified) | `dispatcher/manager.py:963` |
| Blocking-gate logic (unchanged) | `dispatcher/manager.py:964–1042` |
| `fail()` notification call | `dispatcher/manager.py:1374` |
| `timeout()` notification call | `dispatcher/manager.py:1409` |
| `cancel()` notification call | `dispatcher/manager.py:1439` |
| `_notify_terminal()` definition | `dispatcher/manager.py:1563` |
| `notify_terminal_with_fallback()` | `dispatcher/notifier.py:576` |
| `notify_completed_with_fallback()` (legacy alias) | `dispatcher/notifier.py:729` |
| `_LEGACY_NOTIFIER_BY_STATUS` table | `dispatcher/notifier.py:743` |
| `_rescue()` (unchanged, outside diff) | `dispatcher/manager.py:590` |
| `missing_expected_artifacts` gate (unchanged) | `dispatcher/manager.py:759–877` |

## Appendix B — Method

1. `git log --oneline -5` + `git rev-parse HEAD` (confirmed HEAD = expected).
2. `git status --short` (identified working-tree state, 3 in-scope + `.gitignore` + untracked residue).
3. `/usr/bin/git diff HEAD -- <3 files>` (rtk-bypass for full patch body; 121 changed lines).
4. `read_file` on `dispatcher/manager.py` lines 560–722, 790–1042, 1335–1450, 1555–1684 (rescue path, complete(), fail/timeout/cancel, `_notify_terminal`).
5. `read_file` on `dispatcher/notifier.py` lines 560–748 (generalized gate + legacy aliases).
6. `read_file` on the two in-scope test files at the changed regions.
7. `grep` for symbol references across `*.py` (confirmed `notify_completed_with_fallback` legacy alias still importable; `_notify_terminal` is the sole terminal-notification producer).
8. Ran `pytest tests/test_aee_v3_telegram_gate.py tests/test_run_task_mapping.py -q` (73 passed, 1 unrelated warning).
9. Verified the `INSERT INTO task_outputs (task_id, notification_json) VALUES (?, ?)` in `_notify_terminal` does NOT clobber `output_text` / `usage_json` / `raw_json` / `delivery_json` (the UPDATE path targets only `notification_json`; the INSERT path is only taken when no row exists, in which case it stubs the other columns with NULL — same behavior as the removed inline code).
10. Wrote this artifact via `write_file`; verified with `ls -la`, `wc -l`, `sha256sum` (below).

---

End of review.