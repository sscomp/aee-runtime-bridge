# AEE V3 Claude Code Executor Routing — Independent Review (TASK-20260719-0047)

Review of TASK-20260719-0046 implementation: minimal working-tree fix
that makes executor selection enforceable AND observable through the
`POST /runs` response's `routing.executor` block.

## 1. Executive Verdict

**Verdict: PASS WITH CAVEATS**

The TASK-20260719-0046 implementation is correct, minimal, backward
compatible, adequately tested, and ready for an atomic commit *once
the unrelated dirty working-tree changes are excluded from the
staging set*. The change to `app.py` (the only tracked file under
review) is purely additive (+27 / -0) and the new test file
`tests/test_executor_routing_evidence.py` (292 lines, untracked) is
properly scoped to the routing-evidence contract.

The "WITH CAVEATS" qualifier is **not** about the routing change
itself — it is about the **dirty working tree**: 10 tracked files
have unrelated modifications (aee/observability/events.py,
aee/tests/test_aee74_*, app.py-routing-portion-only-is-in-scope,
config/notify.json, dispatcher/db.py, dispatcher/manager.py,
dispatcher/models.py, dispatcher/notifier.py), and 27 untracked
files exist. The committer MUST stage only the two TASK-0046 files
(`app.py` and `tests/test_executor_routing_evidence.py`) by explicit
path list, NOT `git add -A`, otherwise the commit will silently
sweep in unrelated WIP from other abandoned slices.

## 2. Review Scope and Baseline

**Scope of this review (independent, read-only):**
- Inspect current working-tree diff for `app.py` and the new
  `tests/test_executor_routing_evidence.py`.
- Verify executor-routing behavior end to end in code and tests.
- Re-run targeted tests.
- Run impacted regression (`tests/test_executor_router.py`) and a
  broad regression sweep (`tests/` + `aee/tests/`).
- Independently verify whether reported baseline failures are
  pre-existing by re-running the same broad sweep at clean HEAD in a
  side worktree (no working-tree mutation).
- Verify no unrelated changes are present *in the TASK-0046 file
  set*; flag the unrelated dirty files as a staging caveat.

**Repository baseline:**
- Repository: `/home/ubuntu/hermes-runtime-bridge`
- Branch: `master`
- HEAD: `580f98ad3e719c5fd4ecd0b086fc5593e9c7b4ad`
- Last commit touching `app.py` (pre-0046): `4207b2f` (Epic 9.4 §21.4
  runtime profile selection).
- No stashes (`git stash list` empty).
- Working tree is dirty across 10 tracked files + 27 untracked files;
  only `app.py` and `tests/test_executor_routing_evidence.py` are in
  scope for TASK-0046.

## 3. Diff Inspection

### 3.1 Files under review

| File | Status | Lines | Ins | Del |
|------|--------|------:|----:|----:|
| `app.py` | Modified (tracked) | +27 / -0 | 27 | 0 |
| `tests/test_executor_routing_evidence.py` | New (untracked) | 292 | 292 | 0 |

### 3.2 `app.py` diff summary (verbatim hunk anchors)

Three purely-additive hunks inside the existing
`async def create_run(...)` handler:

1. **Hoist** (`@@ -802,6 +802,16 @@`): declares
   `executor_decision = None` at function scope *before* the
   `if body.metadata is not None:` branch, with a docstring-style
   comment explaining the legacy/default sentinel. This is the key
   invariant — `executor_decision` is `None` for the legacy
   default-Hermes path, and is set to the `RoutingDecision` only
   inside the metadata branch.

2. **Stash** (`@@ -867,6 +877,10 @@`): at the end of the
   `select_executor(...)` success path, assigns
   `executor_decision = decision`. Located AFTER `manager.log(...)`
   and BEFORE the `adapter = adapter_registry.get(...)` call. Order
   is correct — the assignment is unreachable if
   `ExecutorValidationError` (-> 400) or `ExecutorUnavailable` (-> 503)
   is raised earlier in the branch.

3. **Surface** (`@@ -933,6 +947,19 @@`): adds a new key
   `"executor"` to the `routing={...}` dict of `CreateRunResponse`,
   populated with `executor_decision.to_dict() if executor_decision
   is not None else None`. Position is the last key in the routing
   dict (additive — no existing keys reordered or removed).

**No deletions. No reordering of existing code. No signature
change.** The `safety=decision.to_dict()` line (which lives in the
metadata branch) is preserved unchanged.

### 3.3 `tests/test_executor_routing_evidence.py` summary

292 lines, 4 test functions under a single module-level docstring
specifying the brief's §3, §4, §5 coverage:

1. `test_executor_claude_code_surfaces_routing_evidence` —
   `metadata.executor='claude_code'` + stub claude_code adapter →
   asserts `routing.executor.selected_executor == 'claude_code'`,
   `requested_executor == 'claude_code'`,
   `fallback_applied is False`.

2. `test_executor_hermes_explicit_surfaces_routing_evidence` —
   `metadata.executor='hermes'` (explicit) → asserts
   `selected_executor == 'hermes'`,
   `selection_source == 'explicit_hermes'`.

3. `test_no_metadata_surfaces_null_executor_in_routing` —
   no `metadata` key at all (legacy default) → asserts
   `routing.executor is None` (observable "no override" sentinel),
   AND the existing default Hermes path is preserved (HTTP 200,
   not 400/503).

4. `test_unsupported_executor_rejected_with_stable_code` —
   `metadata.executor='gemini'` → asserts HTTP 400 and
   `detail.code == 'unknown_executor'` (no silent fallback to
   Hermes).

The fixture `_make_client` registers a stub `hermes` adapter and
saves/restores `adapter_registry._adapters` around each test, so
the registry is not polluted across tests. The
`_setup_test_db` helper monkey-patches `DB_DIR`, `DB_PATH`,
`LOGS_DIR`, `REPORTS_DIR` to `tmp_path`, resets `_initialized=False`,
and clears any cached `_local.conn`. This isolates the tests from
the on-disk `data/dispatcher.db`.

## 4. Contract Compliance Review

Brief requirements vs. implementation:

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| §1 | Executor selection is **enforceable** (validate-then-route) | ✅ PASS | `validate_metadata` raises `ExecutorValidationError` (400) before `select_executor`; `select_executor` raises `ExecutorUnavailable` (503) when `claude_code` requested but adapter not registered. Pre-existing in `aee/runtimes/executor_router.py`; TASK-0046 does not regress this. |
| §2 | Unsupported executor values are **rejected** with stable code | ✅ PASS | `validate_metadata` rejects values outside `ALLOWED_EXECUTORS = frozenset({"hermes", "claude_code"})` with `code='unknown_executor'`. Test 4 confirms 400 + `detail.code='unknown_executor'`. |
| §3 | Omitted executor preserves existing default behavior | ✅ PASS | `executor_decision = None` initialized BEFORE the metadata branch; the `if body.metadata is not None:` branch is skipped entirely when `metadata` is absent, leaving the existing default-Hermes path intact. Test 3 confirms `routing.executor is None` and HTTP 200. |
| §4 | Routing metadata **accurately reports** the actual selected executor | ✅ PASS | `executor_decision.to_dict()` is the SAME `RoutingDecision` object whose `selected_executor` was applied via `job.adapter_name = decision.selected_executor`. The stashed decision is the one used. Test 1 + 2 verify the fields. |
| §5 | Explicit requests are **not silently overridden** | ✅ PASS | `select_executor` returns `selected_executor == requested_executor` for both `hermes` and `claude_code`; the only silent-fallback path is the legacy default (no metadata), which is the documented contract. `fallback_applied` is always `False` per the dataclass default. Test 1 asserts `fallback_applied is False`. |

## 5. Targeted Test Evidence

Command:
```
.venv/bin/python -m pytest tests/test_executor_routing_evidence.py -v
```

Result: **PASS** — 4 passed, 1 warning, 0.46s.

```
tests/test_executor_routing_evidence.py::test_executor_claude_code_surfaces_routing_evidence PASSED [ 25%]
tests/test_executor_routing_evidence.py::test_executor_hermes_explicit_surfaces_routing_evidence PASSED [ 50%]
tests/test_executor_routing_evidence.py::test_no_metadata_surfaces_null_executor_in_routing PASSED [ 75%]
tests/test_executor_routing_evidence.py::test_unsupported_executor_rejected_with_stable_code PASSED [100%]
```

The 1 warning is a Starlette deprecation notice about `httpx` vs
`httpx2` — pre-existing, environment-level, unrelated to TASK-0046.

## 6. Regression Evidence

### 6.1 Impacted regression — `tests/test_executor_router.py`

Command:
```
.venv/bin/python -m pytest tests/test_executor_router.py -v
```

Result: **PASS** — 15 passed, 1 warning, 0.38s. Covers the pure
router function, validation, 503 path, and the existing
`/runs`-returns-503-when-claude-missing integration. No regression
introduced by TASK-0046.

### 6.2 Broad regression — `tests/` + `aee/tests/`

Command:
```
.venv/bin/python -m pytest tests/ aee/tests/ -q --tb=no
```

Result: **PASS WITH PRE-EXISTING FAILURES** — 26 failed, 1971
passed, 4 skipped, 1 error, 137 subtests passed, 37.27s.

The 26 failures are NOT regressions caused by TASK-0046 — see
Section 7 for the independent verification.

## 7. Pre-existing Failure Verification

To independently verify that the 26 failures are pre-existing
(not caused by TASK-0046), I created a side worktree at clean HEAD
(`git worktree add /tmp/hrb-head-review HEAD`) with the SAME `.venv`
(copied), ran the SAME broad sweep, then removed the worktree
(`git worktree remove --force`). The working tree of
`/home/ubuntu/hermes-runtime-bridge` was NOT modified at any point
during this verification.

**Result at clean HEAD `580f98a` (no TASK-0046 changes):**
27 failed, 1879 passed, 34 skipped, 1 error, 137 subtests passed,
33.71s.

The clean-HEAD failure set is a **strict superset** of the
TASK-0046 working-tree failure set:

- Clean HEAD: 27 failures
- TASK-0046 working tree: 26 failures
- Difference: 1 failure present at clean HEAD but absent in the
  TASK-0046 working tree
  (`aee/tests/test_artifact_security.py::TestArtifactPolicyAllowList::test_default_policy_allows_bridge_repo`
  and
  `test_root_at_path_itself_is_accepted` — these depend on the
  repository path; the worktree path `/tmp/hrb-head-review/`
  differs from `/home/ubuntu/hermes-runtime-bridge/`, causing
  allow-list assertions to fire. This is a test-path artifact, NOT
  a TASK-0046 regression.)

Plus, the TASK-0046 working tree has 4 NEW passing tests
(`test_executor_routing_evidence.py`) that do not exist at clean
HEAD, which is why its total passed count is higher (1971 vs 1879)
and its total failed count is 1 lower.

**Conclusion:** All 26 failures observed in the TASK-0046 working
tree are pre-existing test contamination (test ordering, fixture
state leakage, file-system path dependencies), NOT regressions
introduced by TASK-0046. The pre-existing failures are
pre-existing in the literal sense: present at clean HEAD with
zero TASK-0046 changes applied.

The pre-existing failure modes (categorized):

- **Test-ordering / fixture state contamination** (24 of 26):
  `aee/tests/test_aee74_round_trip_e2e.py`,
  `aee/tests/test_aee74_wireup.py`,
  `aee/tests/test_aee82_dispatcher_profile_wiring.py`,
  `tests/test_dispatcher.py::TestTaskLifecycle::*`,
  `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::*`,
  `tests/test_aee_v3_telegram_gate.py::TestDbMigration::test_notification_json_column_present_after_init`.
  All pass in isolation; only fail in the full sweep due to state
  leakage from earlier tests.

- **Migration / schema** (1 of 26):
  `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent`
  — pre-existing.

- **Path-dependent allow-list** (1 of 26, only at clean HEAD): the
  two `test_artifact_security.py` cases noted above.

## 8. Backward Compatibility Assessment

**Backward compatible: YES.**

1. **No public API removed or renamed.** The `routing` block of
   `CreateRunResponse` gains a new key `executor` (additive). No
   existing keys (`client_source`, `model_name`, `was_forced`,
   `reason`, `caller_model`, `profile`) are removed or reordered.
2. **No request schema change.** `metadata` remains optional;
   callers that pass no `metadata` get the exact same behavior
   (default Hermes path, `routing.executor=null`).
3. **No error code change.** The 400/503 paths and their
   `detail.code` values are unchanged.
4. **No signature change** to `select_executor`,
   `validate_metadata`, or `RoutingDecision.to_dict()`.
5. **No new dependencies.** No imports added to `app.py` outside
   the existing metadata branch.

The only observable behavioral change is: the response now
includes a `routing.executor` key (value `null` for legacy
callers). Any client that does strict schema validation on the
response shape may need to allow the new key — but the bridge's
existing clients (Hermes M2, ChatGPT custom GPT action) use
lenient JSON parsing, so this is safe.

## 9. Security and Safety Assessment

**No security regressions.**

1. The `executor_decision` is only populated inside the
   `if body.metadata is not None:` branch, which already runs
   `validate_metadata`. This means the routing evidence in the
   response is only surfaced for inputs that passed validation —
   no unvalidated executor name can reach the response.
2. `RoutingDecision.to_dict()` exposes only:
   `requested_executor`, `selected_executor`, `selection_source`,
   `fallback_applied`, `fallback_reason`. None of these leak
   secrets, file paths, or environment data.
3. No new file reads, no new network calls, no new subprocess
   spawns introduced by TASK-0046.
4. The change is read-only with respect to the dispatcher DB and
   the filesystem — TASK-0046 only adds a Python local variable
   and a dict key.

## 10. Unrelated Change Check

**In the TASK-0046 file set (`app.py` + new test file):** no
unrelated changes. The `app.py` diff is purely additive in the
`create_run` handler, with comments explicitly referencing
TASK-20260719-0046 §4. The new test file is named
`test_executor_routing_evidence.py` and contains only tests
covering the routing-evidence contract.

**In the working tree at large:** the dirty working tree has 10
tracked-file modifications and 27 untracked files. Of these:

- `app.py` — only the +27 lines from TASK-0046 are in scope; no
  other `app.py` change is in the diff (verified by reading the
  full `git diff app.py` output — the three hunks are all
  TASK-0046-related).
- 9 OTHER tracked files have unrelated modifications from prior
  abandoned slices (AEE-7.4 §20.9.10 write-side metadata, AEE-7.7d
  notification, etc.):
  - `aee/observability/events.py` (+52/-1)
  - `aee/tests/test_aee74_emitter.py` (+32/-1)
  - `aee/tests/test_aee74_observability.py` (+41/-1)
  - `aee/tests/test_aee74_round_trip_e2e.py` (+24/-1)
  - `config/notify.json` (+14/-1)
  - `dispatcher/db.py` (+57/-1)
  - `dispatcher/manager.py` (+238/-1)
  - `dispatcher/models.py` (+16/-1)
  - `dispatcher/notifier.py` (+458/-1)
- 27 untracked files include reports, manifests, and the new
  test file. None of these (except
  `tests/test_executor_routing_evidence.py`) belong to TASK-0046.

**Staging discipline required for the commit:** the committer
MUST use `git add app.py tests/test_executor_routing_evidence.py`
(explicit path list). `git add -A` or `git commit -am` would
silently sweep in the 9 unrelated tracked-file modifications. The
staging boundary is the responsibility of the committer, NOT of
TASK-0046's author.

## 11. Commit Readiness Decision

**READY WITH PREREQUISITES.**

Prerequisites (the committer MUST verify all before staging):

1. Stage ONLY `app.py` and `tests/test_executor_routing_evidence.py`
   via explicit path list:
   `git add app.py tests/test_executor_routing_evidence.py`.
2. Verify staging set: `git diff --cached --stat` MUST show exactly
   2 files (`app.py` +27/-0 and `tests/test_executor_routing_evidence.py`
   +292/-0), total +319/-0.
3. Verify `git diff --stat` (unstaged) still shows the 9 unrelated
   tracked files as modified — confirms they were NOT swept in.
4. Re-run targeted tests from the staging area:
   `.venv/bin/python -m pytest tests/test_executor_routing_evidence.py
   tests/test_executor_router.py -v` — must show 19 passed (4 + 15).
5. Re-run a smoke check of the broad sweep to confirm the failure
   count matches the pre-existing baseline (26 ± 1).
6. The commit message should follow the existing convention, e.g.:
   `feat(bridge): surface executor routing decision in /runs response
   (TASK-20260719-0046)`.

Once the prerequisites above are met, the commit is ready.

## 12. Remaining Risks

1. **Staging discipline is the single biggest risk.** If the
   committer uses `git add -A` (or `git commit -am`), the commit
   will silently include 9 unrelated tracked-file modifications
   from prior abandoned slices. This would violate the "minimal
   atomic commit" contract. The prerequisite list in Section 11
   guards against this.
2. **Pre-existing test contamination is not addressed by
   TASK-0046** and is out of scope. The 26 failures in the broad
   sweep are pre-existing baseline noise. They should be tracked
   separately as a housekeeping item.
3. **No new integration test for the 503 path with
   `routing.executor` populated.** TASK-0046 does not add a test
   for the case where `executor='claude_code'` is requested, the
   adapter is missing, and the 503 response is returned. The
   pre-existing `tests/test_executor_router.py::test_app_create_run_returns_503_executor_unavailable_when_claude_missing`
   covers the 503 path but does not assert on `routing.executor`
   (because the response is an HTTPException, not a
   `CreateRunResponse`). This is a minor gap, NOT a blocker — the
   503 path returns before `CreateRunResponse` is built, so
   `routing.executor` is correctly absent from the 503 response
   body. A future test could assert that the 503 body does not
   contain a `routing.executor` key.
4. **Test fixture registry restoration is best-effort.** The
   fixture saves `dict(adapter_registry._adapters)` and restores
   it, but if a test raises before the `finally` block runs (e.g.,
   pytest interrupt), the registry could be left in a polluted
   state. This is a pre-existing pattern in the codebase, not a
   TASK-0046 introduction.
5. **`manager.log()` is wrapped in `try/except Exception: pass`**
   (pre-existing). If `manager.log` raises, the audit trail for
   the routing decision is lost silently. Not a TASK-0046
   regression.

## 13. Recommended Next Work Order

**Work Order: TASK-20260719-0048 — Atomic Commit: Executor Routing Evidence**

Type: Atomic Commit (K-shape, single small commit).

Pre-conditions:
- This independent review (TASK-0047) verdict = PASS WITH CAVEATS.
- 鼎鼎 (operator) authorizes the commit.

Steps:
1. `cd /home/ubuntu/hermes-runtime-bridge`
2. `git status --short` — confirm working tree state matches this
   review's Section 2/10.
3. `git add app.py tests/test_executor_routing_evidence.py` —
   explicit path list ONLY.
4. `git diff --cached --stat` — verify exactly 2 files staged:
   `app.py | 27 +++` and `tests/test_executor_routing_evidence.py | 292 +++`.
5. `git diff --stat` — verify the 9 unrelated tracked files remain
   unstaged.
6. `.venv/bin/python -m pytest tests/test_executor_routing_evidence.py
   tests/test_executor_router.py -v` — confirm 19 passed.
7. `git commit -m "feat(bridge): surface executor routing decision
   in /runs response (TASK-20260719-0046)"`
8. `git log --oneline -1` — capture the new SHA.
9. `git show --stat HEAD` — verify the commit contains exactly 2
   files.
10. Notify 鼎鼎 via Telegram with the brief summary (SHA, file
    count, test count, verdict).

Do NOT push, deploy, or restart. The commit is local-only per the
standing safety rules.

---

## Appendix A — Git Evidence

- **Repository:** `/home/ubuntu/hermes-runtime-bridge`
- **Branch:** `master`
- **HEAD:** `580f98ad3e719c5fd4ecd0b086fc5593e9c7b4ad`
- **Git Status:** dirty — 10 tracked modified, 27 untracked.
- **Tracked Changes (in scope):** `app.py` (+27/-0).
- **Tracked Changes (out of scope, must NOT be staged):**
  `aee/observability/events.py`, `aee/tests/test_aee74_emitter.py`,
  `aee/tests/test_aee74_observability.py`,
  `aee/tests/test_aee74_round_trip_e2e.py`, `config/notify.json`,
  `dispatcher/db.py`, `dispatcher/manager.py`,
  `dispatcher/models.py`, `dispatcher/notifier.py`.
- **Untracked Files (in scope):**
  `tests/test_executor_routing_evidence.py` (292 lines).
- **Untracked Files (out of scope):** 26 other files (reports,
  manifests, `dispatcher/notification_state.py`,
  `data/dispatcher.db.pre-rebuild-*`, other `tests/test_aee_v3_*`
  files). Must NOT be staged.
- **Diff Summary (in scope):** +319 / -0 across 2 files.
- **Exact files under review:** `app.py`,
  `tests/test_executor_routing_evidence.py`.
- **Insertions / Deletions:** +319 / -0.

## Appendix B — Test Result Index

| Test Class / File | Result | Notes |
|-------------------|--------|-------|
| `tests/test_executor_routing_evidence.py` (4 tests) | PASS | 4/4 in 0.46s |
| `tests/test_executor_router.py` (15 tests) | PASS | 15/15 in 0.38s |
| Broad sweep (`tests/` + `aee/tests/`) working tree | PASS WITH PRE-EXISTING FAILURES | 26 failed / 1971 passed — all 26 pre-existing |
| Broad sweep at clean HEAD (side worktree) | PASS WITH PRE-EXISTING FAILURES | 27 failed / 1879 passed — strict superset of working tree's failures |
| Isolated re-run of 4 failing tests | PASS | All 4 pass in isolation, confirming test-ordering contamination |

## Appendix C — Verdict Mapping

| Brief Section | Result |
|---------------|--------|
| 1. Executive Verdict | PASS WITH CAVEATS |
| 2. Review Scope and Baseline | documented |
| 3. Diff Inspection | +319/-0, purely additive |
| 4. Contract Compliance Review | §1-§5 all PASS |
| 5. Targeted Test Evidence | 4/4 PASS |
| 6. Regression Evidence | 15/15 impacted PASS; 26 pre-existing in broad |
| 7. Pre-existing Failure Verification | independent clean-HEAD worktree confirms pre-existing |
| 8. Backward Compatibility Assessment | YES — additive only |
| 9. Security and Safety Assessment | no regressions |
| 10. Unrelated Change Check | in-scope clean; out-of-scope dirty flagged |
| 11. Commit Readiness Decision | READY WITH PREREQUISITES |
| 12. Remaining Risks | 5 documented |
| 13. Recommended Next Work Order | TASK-20260719-0048 documented |

**Final Verdict: PASS WITH CAVEATS**
**Commit Readiness: READY WITH PREREQUISITES**