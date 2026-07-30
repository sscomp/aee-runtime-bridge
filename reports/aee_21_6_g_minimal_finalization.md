# AEE §21.6.G Minimal Finalization (M-1 Closure)

**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**HEAD (unchanged):** `befe3d6fe5eeeafed316883d27e2868638c64d22`
**Date (Asia/Taipei):** 2026-07-30
**Operator:** M2 (Dingde ChatGPT Orchestrator)
**Scope:** M-1 only (stale `--capabilities` CLI help string). M-2 and LOW findings explicitly out of scope.

---

## Execution Timing

- **Start (CST):** 2026-07-30 (session)
- **End (CST):** 2026-07-30 (session)
- **Duration:** minimal finalization (single-string edit + targeted regression)
- **All times Asia/Taipei.**

## Overall Verdict

**PASS**

M-1 is demonstrably closed: the stale `--capabilities` help string that claimed "Plumbing-only in this slice (WO-2)… is WO-3 and is not performed here" is replaced with accurate wording reflecting the implemented WO-3 behavior (authoritative validation + exit 13 on invalid; legacy behavior on omission). No out-of-scope source/test/report/Master-Plan/capability-file changes were made. Targeted CLI help tests and impacted WO-2/WO-3 regressions pass. The pre-existing PyYAML collection errors (5 in `test_runtime_config`) are unchanged, are the same class as M-2 (explicitly out of scope), and are not caused by this edit. P0-1 shadow-run, protected files, frozen baseline, cron, bridge/runtime state, and HEAD were not disturbed.

## Baseline

- **HEAD (pre):** `befe3d6fe5eeeafed316883d27e2868638c64d22`
- **HEAD (post):** `befe3d6fe5eeeafed316883d27e2868638c64d22` (unchanged)
- **Tracked modified files (pre):** `aee/cli.py`, `aee/installer/backend.py`, `aee/installer/cli_install.py` (pre-existing WO-1/2/3 work, uncommitted per context)
- **Tracked modified files (post):** same 3 files; only `aee/cli.py` received the M-1 string edit
- **Protected files sha256 (pre == post):**
  - `aee/installer/cli_install.py` `f944150f72a74633c5dfd1e50dfcf0e33fb82c573dac3f2d79219d07e5319f35` (unchanged)
  - `aee/installer/backend.py`        `8146662cc9e4d776dde629944b5ded9ea08bc8674d6f9a1a7637764c206f1d2c` (unchanged)
  - `aee/installer/lifecycle.py`      `88efe75d76a6dfa2ec988c5e2dae7e8cb655d56bd8e6d6c91401c9e82eb6bcf3` (unchanged)
  - `aee/installer/macos_bootstrap.py` `2522552e8e3350655aa74d1ae5b646b2a411b2ba7b76d25802c4936a009bb99a` (unchanged)
- **Stash list:** (none)
- **Supervisor state:** `hermes-runtime-bridge` RUNNING (pid 1619150, uptime 5d 8h+); `cloudflared-tunnel` not under supervisor (no such process — pre-existing, unchanged by this task)
- **Bridge endpoint:** `http://127.0.0.1:8787/` → 404 (pre-existing route shape; unchanged)
- **`macro_history.db` / dispatcher.db:** not touched (dispatcher.db 1.3M, pre-existing)

## Finding Addressed

**M-1 (MEDIUM) — Stale CLI help text for `--capabilities`** (from `reports/aee_21_6_g_post_implementation_independent_review.md` §"MEDIUM (2)", line 286-292).

- **File:** `aee/cli.py` (help string for the `install --capabilities` argparse argument)
- **Stale text (before):** "Plumbing-only in this slice (WO-2): the path is recorded in the result and an audit note is emitted; the backend contract binding (load + validate + refuse on invalid) is WO-3 and is not performed here. A read-only os.path.exists check surfaces whether the file is present."
- **Why stale:** WO-3 IS implemented and IS performed (`aee/installer/backend.py` `validate_capabilities_document` + `run_install` gate refuses install with exit 13 on invalid). The help text contradicted actual behavior and could mislead an operator into believing `--capabilities` is non-binding.
- **M-2 (PyYAML-dependent `malformed_yaml` reachability) and L-1/L-2:** explicitly out of scope per brief; not touched.

## Minimal Change Summary

Single targeted edit to the `help=(...)` string of the `install_parser.add_argument("--capabilities", ...)` block in `aee/cli.py`. Replaced the stale "Plumbing-only… not performed here" wording with accurate, concise user-visible wording that reflects current behavior:

- supplying a capabilities file causes authoritative validation before install/materialization;
- an invalid document refuses the install (exit 13);
- omission preserves legacy pre-WO-3 behavior (no loading, no validation, no extra exit code).

No other source, test, report, Master Plan, or capability-file changes were made. The code comment block immediately above `add_argument` (lines 250-256) was intentionally left untouched — it is an internal implementation comment, not user-visible help, and editing it would expand scope beyond the M-1 help-string fix. (The comment's "plumbing-only" framing describes the WO-2 slice history; the user-facing help is the surface that M-1 flags as stale.)

## Files Changed

| File | Change |
|---|---|
| `aee/cli.py` | `help=` string of `--capabilities` argument replaced (M-1 only) |

No other files modified by this finalization. (`aee/installer/backend.py` and `aee/installer/cli_install.py` remain in their pre-existing WO-2/3 state — not touched by M-1.)

## Insertions/Deletions

For the M-1 edit specifically (the `help=` string block in `aee/cli.py`):

- **Before:** 5 lines of help text (`"Plumbing-only in this slice..."`)
- **After:** 5 lines of help text (`"When supplied, the document is loaded..."`)
- **Net line delta:** 0 (5 replaced with 5)
- **Character delta:** replaced ~290 chars with ~330 chars

The `git diff --stat aee/cli.py` shows a larger delta (+31/-4) because it includes the pre-existing WO-2/3 plumbing work already in the working tree (argparse wiring, dispatch signature, `main()` routing) — that work is NOT part of this M-1 finalization and was present in the baseline before this task began.

## Before/After Help Behavior

**Before (stale):**
```
--capabilities <path>
    Path to a Host Capability Document YAML (§21.6.B). Plumbing-only in
    this slice (WO-2): the path is recorded in the result and an audit
    note is emitted; the backend contract binding (load + validate +
    refuse on invalid) is WO-3 and is not performed here. A read-only
    os.path.exists check surfaces whether the file is present.
```

**After (corrected):**
```
--capabilities <path>
    Path to a Host Capability Document YAML (§21.6.B). When supplied,
    the document is loaded and authoritatively validated against the
    §21.6.B / §21.6.C contract before install/materialization; an
    invalid document refuses the install (exit 13). When omitted,
    legacy pre-WO-3 behavior is preserved (no loading, no validation,
    no extra exit code).
```

**Behavioral contract reflected:** matches `aee/installer/backend.py:validate_capabilities_document` + `run_install` exit-13 gate (WO-3, §21.6.G item 3) and the backward-compat no-flag path (no loading, no extra exit code).

**Verification:**
- `grep -c 'is WO-3 and is not performed here\|Plumbing-only in this slice' aee/cli.py` → **0** (stale phrases gone)
- `grep -c 'authoritatively\|refuses the install' aee/cli.py` → **1** (new wording present)
- `aee install --help` renders the corrected block (verified via parser introspection)

## Tests and Regression

| Suite | Result | Notes |
|---|---|---|
| `aee.tests.test_wo2_installer_cli_capabilities` (incl. `HelpTextTests.test_help_mentions_capabilities`) | **32/32 PASS** | Direct M-1 surface; help text still mentions `--capabilities` and `Host Capability Document` |
| `aee.tests.test_wo3_installer_backend_validator` | **PASS** (all) | WO-3 backend validator regressions |
| `aee.tests.test_wo1_host_capabilities` | **PASS** (all) | WO-1 canonical file regressions |
| `aee/tests` full discover | **2546 tests, 5 errors, 2 skipped** | 5 errors are pre-existing `test_runtime_config` PyYAML collection errors (M-2 class, out of scope); 2 skipped pre-existing. NOT caused by M-1. |

**Test commands:**
- `python3 -m unittest aee.tests.test_wo2_installer_cli_capabilities -v` → `Ran 32 tests … OK`
- `python3 -m unittest aee.tests.test_wo3_installer_backend_validator aee.tests.test_wo1_host_capabilities -v` → `Ran 73 tests … OK`
- `python3 -m unittest discover -s aee/tests` → `Ran 2546 tests … FAILED (errors=5, skipped=2)` — the 5 errors are `test_runtime_config` ImportError/PyYAML-missing (pre-existing, M-2 territory).

**AST check:** `python3 -c 'import ast; ast.parse(open("aee/cli.py").read())'` → OK.

## Shadow-Run Non-Interference

- **P0-1 7-day shadow run:** no disturbance. No `metadata/tasks/` mutation, no `dispatcher.db` write, no cron change, no `macro_history.db` touch, no shadow-run artifact created or modified.
- **Protected files:** sha256 of `cli_install.py`, `backend.py`, `lifecycle.py`, `macos_bootstrap.py` identical pre/post.
- **Frozen baseline:** HEAD `befe3d6` unchanged pre/post.
- **Cron:** no cron job created, modified, or removed.
- **Bridge/runtime state:** `hermes-runtime-bridge` supervisor program RUNNING (pid 1619150) — not restarted; bridge HTTP shape unchanged (404 on `/`, pre-existing). No deploy, restart, merge, rebase, stash, delete, move, cron change, firewall change, or service mutation.

## Git Status and Diff Summary

- **HEAD (pre == post):** `befe3d6fe5eeeafed316883d27e2868638c64d22`
- **`git status --short` (tracked only):**
  - ` M aee/cli.py`
  - ` M aee/installer/backend.py`
  - ` M aee/installer/cli_install.py`
- **`git diff --cached`:** empty (nothing staged)
- **`git stash list`:** empty
- **M-1 scoped diff (aee/cli.py help= block):** 5-line replacement, 0 net line delta for the help string itself. The wider `git diff aee/cli.py` (+31/-4) includes pre-existing WO-2/3 plumbing not authored by this finalization.
- **Untracked files:** pre-existing reports/scripts/tests/reports/ etc. (not staged, not touched).

## Artifact Verification

```
ls -la reports/aee_21_6_g_minimal_finalization.md
wc -l reports/aee_21_6_g_minimal_finalization.md
sha256sum reports/aee_21_6_g_minimal_finalization.md
```
(Filled in at end of this file's creation — see execution log.)

## Production Safety

| Control | Status |
|---|---|
| No commit / push | ✅ confirmed (nothing staged, HEAD unchanged) |
| No deploy / restart / merge / rebase / stash / delete / move | ✅ confirmed |
| No cron change | ✅ confirmed |
| No firewall / service mutation | ✅ confirmed |
| No capability-file change (`host.capabilities.yaml` untouched) | ✅ confirmed |
| No Master Plan edit | ✅ confirmed |
| No secret printed | ✅ confirmed |
| No P0-1 shadow-run disturbance | ✅ confirmed |
| No refactor / cleanup / formatting sweep / dependency install | ✅ confirmed (single help-string replacement) |

## Remaining Risks

1. **M-2 (PyYAML-dependent `malformed_yaml` reachability) — UNADDRESSED, by design.** Out of scope per brief. The 5 pre-existing `test_runtime_config` collection errors stem from the same missing-PyYAML environment gap. These are not introduced by M-1 and are not regressed by it.
2. **L-1 / L-2 — UNADDRESSED, by design.** Low-severity, out of scope.
3. **Internal code comment (aee/cli.py:250-256) still says "plumbing-only… NOT performed in this slice".** This is an internal implementation comment, not user-visible help; M-1 is specifically about the user-visible CLI help string. Leaving the comment untouched keeps scope minimal. A future cleanup slice can reconcile the comment with WO-3 reality.
4. **Pre-existing WO-2/3 plumbing in working tree remains uncommitted.** This finalization does not commit (per brief). The change set is Review Ready / Commit Ready pending the operator's commit authorization.

## Review Ready

**YES.**

M-1 is closed (stale help string replaced with accurate wording; verified via grep + `--help` render + targeted tests). No out-of-scope changes. Targeted and impacted regressions pass. The independent review's M-1 finding is resolved; M-2 and LOW findings remain as documented (out of scope). The full WO-1/2/3 change set (the 3 modified tracked files) is unchanged apart from the M-1 string edit in `aee/cli.py`, so the prior "PASS WITH CAVEATS" verdict now has its sole user-facing-actionable caveat (M-1) closed.

## Commit Ready

**YES — pending operator commit authorization.**

- Working tree is clean apart from the 3 pre-existing WO-2/3 tracked modifications (M-1 edit is contained within `aee/cli.py`).
- Nothing staged; HEAD unchanged.
- Targeted tests pass; pre-existing PyYAML errors documented and out of scope.
- No commit/push performed per brief.
- Recommended commit (when authorized): single atomic commit covering the WO-1/2/3 change set + M-1 finalization, with commit message noting M-1 closure.

## Telegram

Per 鼎鼎's AEE-MINI Telegram rule (2026-07-13), an AEE-MINI work-order notification should be sent on completion. This is a §21.6.G minimal-finalization task (not a pure read-only review). Notification pending operator delivery channel confirmation; the short-form fields:

- ✅ §21.6.G M-1 minimal finalization
- 訊息類型: 17-section minimal-finalization report
- 單號: §21.6.G-M1-FINAL
- commit SHA: (none — no commit per brief)
- test count: 32/32 WO-2 PASS + 73/73 WO-1/3 PASS + 2546 aee/tests (5 pre-existing PyYAML errors, out of scope)
- 1-3 句工作摘要: 修正 `aee/cli.py` 中 `--capabilities` 過時 help 字串（M-1），改為反映 WO-3 已實作的權威驗證行為（exit 13 on invalid）。無其他 source/test/report/Master-Plan 變更。P0-1 shadow run、protected files、HEAD 未受影響。
- 完整報告路徑: `/home/ubuntu/hermes-runtime-bridge/reports/aee_21_6_g_minimal_finalization.md`